"""Building an allocation and describing one."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from open_allocator.core import allocator as allocation_core
from open_allocator.core import apy_accounting, eligibility, fixed_rate, metrics
from open_allocator.core import backtest as backtest_core
from open_allocator.core import costs as costs_core
from open_allocator.core import policy as policy_core
from open_allocator.core import positions as positions_core
from open_allocator.core import screen as screen_core
from open_allocator.core import simulate as simulate_core
from open_allocator.core import strategies as strategies_core
from open_allocator.core.policy_loader import load_policy
from open_allocator.core.schema import validate
from open_allocator.core.types import Allocation, Policy, Vault
from open_allocator.exec import gas as gas_module
from open_allocator.exec.client import OneTxClient
from open_allocator.exec.config import ReadOnlyOneTxConfig
from open_allocator.service._common import JsonObject
from open_allocator.service.errors import ServiceError
from open_allocator.service.universe import (
    HISTORY_DAYS,
    OnWarning,
    discover_vaults,
    discover_vaults_from_client,
    score_by_instrument,
    screen_criteria,
)

DEFAULT_POLICY_PATH = Path("policy.yaml")


def parse_allocation(payload: object) -> Allocation:
    """An allocation from its JSON object, as `build-allocation` returns it."""
    if isinstance(payload, Allocation):
        return payload
    validate(payload, "allocation")
    return Allocation.model_validate(payload)


def build_allocation(
    amount: float | None = None,
    *,
    risk: str = "balanced",
    policy: Policy | Path = DEFAULT_POLICY_PATH,
    spec: Mapping[str, Any] | None = None,
    strategy: str = allocation_core.DEFAULT_STRATEGY,
    strategy_params: Mapping[str, Any] | None = None,
    criteria: screen_core.ScreenCriteria | None = None,
    max_positions: int | None = None,
    min_position_usd: float | None = None,
    score_power: float | None = None,
    apy_weight: float | None = None,
    caps_headroom_bps: float = 0.0,
    exclude: Sequence[str] | None = None,
    pins: Mapping[str, float] | None = None,
    source_chain_id: int | None = None,
    on_warning: OnWarning | None = None,
) -> JsonObject:
    """A policy-checked allocation of `amount` USD over the live universe.

    `spec` is an allocation-spec object; what it sets wins over the matching
    arguments, except `amount`, which wins over the spec's `amount_usd`.
    """
    if strategy in {"list", "help"}:
        # Discovery convenience: enumerate strategies without needing an amount.
        return {"strategies": list(strategies_core.available())}
    overrides = dict(pins) if pins else None
    if spec is not None:
        validate(dict(spec), "allocation-spec")
        selection = spec.get("selection", {})
        amount = amount if amount is not None else spec.get("amount_usd")
        risk = spec.get("risk", risk)
        strategy = spec.get("strategy", strategy)
        strategy_params = spec.get("params", strategy_params)
        weights = spec.get("weights")
        if weights:
            overrides = {str(key): float(value) for key, value in weights.items()}
        exclude = selection.get("exclude", exclude)
        max_positions = selection.get("max_positions", max_positions)
        min_position_usd = selection.get("min_position_usd", min_position_usd)
        criteria = screen_criteria(
            min_sharpe=selection.get("min_sharpe"),
            max_drawdown=selection.get("max_drawdown"),
            max_reward_dependence=selection.get("max_reward_dependence"),
            min_history_days=selection.get("min_history_days"),
            curators=selection.get("curators"),
            min_tvl_usd=selection.get("min_tvl_usd"),
        )

    if amount is None:
        raise ServiceError(
            "invalid_input",
            "amount required: pass --amount or set amount_usd in the spec",
        )

    if not isinstance(policy, Policy):
        policy = load_policy(policy)
    discovered = discover_vaults(enrich=True, on_warning=on_warning)
    candidates, exclusions = _policy_candidate_vaults(discovered, policy)
    if criteria is not None and criteria.active:
        screened = screen_core.screen(candidates, criteria)
        candidates = list(screened.kept)
        exclusions = [*exclusions, *screened.warnings()]
    scores = score_by_instrument(discovered)
    allocation = allocation_core.build_allocation(
        [(vault, scores[vault.instrument_id]) for vault in candidates],
        amount,
        risk=risk,
        caps=policy.caps,
        caps_headroom_bps=caps_headroom_bps,
        strategy=strategy,
        strategy_params=dict(strategy_params) if strategy_params else None,
        max_positions=max_positions,
        min_position_usd=min_position_usd,
        overrides=overrides,
        exclude=list(exclude) if exclude else None,
        score_power=score_power,
        apy_weight=apy_weight,
    )
    result = policy_core.check(allocation, policy, discovered)
    chain_by_instrument = {v.instrument_id: v.chain_id for v in discovered}
    cost_estimate = costs_core.estimate_from_allocation_legs(
        [leg.model_dump() for leg in allocation.legs],
        chain_by_instrument=chain_by_instrument,
        apy_by_instrument={v.instrument_id: v.apy for v in discovered},
        base_apy_by_instrument={v.instrument_id: v.apy_base for v in discovered},
        term_days_by_instrument={
            v.instrument_id: float(days)
            for v in discovered
            if (days := fixed_rate.days_to_maturity(v)) is not None
        },
        source_chain_id=source_chain_id,
        params=_live_cost_params(allocation, chain_by_instrument, source_chain_id),
    )
    return _allocation_payload_with_policy_result(
        allocation,
        result,
        discovered=discovered,
        candidates=candidates,
        exclusions=exclusions,
        cost_estimate=cost_estimate,
    )


def simulate(
    allocation: Allocation | Mapping[str, Any],
    *,
    benchmark: str | None = None,
    on_warning: OnWarning | None = None,
) -> JsonObject:
    allocation = parse_allocation(allocation)
    # Discovery supplies the sector labels; without it the output would show
    # yield and stability but say nothing about how many sleeves the capital
    # actually sits in. The dated history on top is what turns that label into
    # a measurement — one extra bulk call, not one per instrument.
    with OneTxClient(ReadOnlyOneTxConfig()) as client:
        vaults = metrics.attach_series(
            client,
            discover_vaults_from_client(client, enrich=False, on_warning=on_warning),
            days=HISTORY_DAYS,
        )
        return simulate_core.simulate(
            client,
            allocation,
            benchmark=benchmark,
            vaults=vaults,
        ).model_dump(mode="json")


def backtest(
    allocation: Allocation | Mapping[str, Any],
    *,
    on_warning: OnWarning | None = None,
) -> JsonObject:
    """Read-only daily-compounded NAV backtest of an allocation vs. a
    TVL-weighted universe benchmark. Yield-path only; descriptive not
    predictive."""
    allocation = parse_allocation(allocation)
    discovered = discover_vaults(enrich=True, on_warning=on_warning)
    apy_series_by_id = {vault.instrument_id: vault.apy_series for vault in discovered}
    tvl_by_id = {vault.instrument_id: vault.tvl_usd for vault in discovered}
    weights = {leg.instrument_id: leg.weight for leg in allocation.legs}
    return backtest_core.run(weights, apy_series_by_id, tvl_by_id).model_dump(
        mode="json"
    )


def check_policy(
    allocation: Allocation | Mapping[str, Any],
    *,
    policy: Policy | Path = DEFAULT_POLICY_PATH,
    against: positions_core.Positions | Mapping[str, Any] | None = None,
    on_warning: OnWarning | None = None,
) -> JsonObject:
    """The allocation scored against the policy on today's shelf.

    With `against`, a positions book, scores the book the allocation would leave
    behind instead of the buy in isolation.
    """
    allocation = parse_allocation(allocation)
    if not isinstance(policy, Policy):
        policy = load_policy(policy)
    known_instruments = discover_vaults(enrich=True, on_warning=on_warning)
    if against is None:
        result = policy_core.check(allocation, policy, known_instruments)
    else:
        result = policy_core.check_incremental(
            allocation,
            policy,
            known_instruments,
            positions_core.held_usd_by_instrument(against),
        )
    return result.model_dump(mode="json")


def _live_cost_params(
    allocation: Allocation,
    chain_by_instrument: Mapping[str, int],
    source_chain_id: int | None,
) -> costs_core.CostParams:
    """Cost params with gas priced from live chain state where possible.

    Only the *source* chain's gas is charged (every deposit signs there), but the
    source may be inferred from the legs, so price every chain the allocation
    touches and let ``CostParams`` pick. A failed read is not fatal: the params
    fall back to static constants and the estimate reports
    ``gas_priced_live: false`` rather than passing a guess off as a measurement.
    """
    chain_ids = [
        chain_id
        for chain_id in (
            chain_by_instrument.get(leg.instrument_id) for leg in allocation.legs
        )
        if chain_id is not None
    ]
    if source_chain_id is not None:
        chain_ids.append(source_chain_id)
    return costs_core.CostParams(gas=gas_module.live_pricing(chain_ids))


def _policy_candidate_vaults(
    vaults: Sequence[Vault],
    policy: Policy,
) -> tuple[list[Vault], list[str]]:
    candidates: list[Vault] = []
    exclusions: list[str] = []

    for vault in vaults:
        # Single source of truth for per-vault policy eligibility.
        rule = eligibility.candidate_exclusion(vault, policy)
        if rule is None:
            candidates.append(vault)
        else:
            exclusions.append(f"policy_excluded:{vault.instrument_id}:{rule}")

    return candidates, exclusions


def _policy_violation_summary(result: policy_core.PolicyResult) -> list[str]:
    return [
        f"{violation.rule}:{violation.entity}:limit={violation.limit}:actual={violation.actual}"
        for violation in result.violations
    ]


def _allocation_payload_with_policy_result(
    allocation: Allocation,
    result: policy_core.PolicyResult,
    *,
    discovered: Sequence[Vault],
    candidates: Sequence[Vault],
    exclusions: Sequence[str],
    cost_estimate: costs_core.CostEstimate | None = None,
) -> JsonObject:
    metadata = dict(allocation.metadata)
    warnings = [str(item) for item in metadata.get("warnings", [])]
    warnings.extend(exclusions)
    accounting = apy_accounting.for_allocation(allocation, discovered)
    warnings.extend(accounting.warnings())
    # Selection remains headline-based in this compatibility release.  The
    # accounting block separately states whether accruing yield is measurable.
    metadata["apy_basis"] = "advertised"
    metadata["apy_accounting"] = accounting.model_dump(mode="json")
    if cost_estimate is not None:
        metadata["cost_estimate"] = cost_estimate.as_metadata()
        cost_warning = cost_estimate.warning()
        if cost_warning is not None:
            warnings.append(cost_warning)
    metadata.update(
        {
            "warnings": sorted(set(warnings)),
            "policy_ok": result.ok,
            "policy_violations": _policy_violation_summary(result),
            "discovered_instruments": [vault.instrument_id for vault in discovered],
            "candidate_instruments": [vault.instrument_id for vault in candidates],
        }
    )
    payload = allocation.model_copy(update={"metadata": metadata}).model_dump(
        mode="json"
    )
    validate(payload, "allocation")
    return payload
