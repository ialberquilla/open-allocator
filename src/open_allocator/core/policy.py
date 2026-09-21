from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from decimal import Decimal
from typing import TypeAlias

from pydantic import Field, model_validator

from open_allocator.core import diversify, eligibility, levered
from open_allocator.core.types import (
    Allocation,
    AllocationLeg,
    FrozenModel,
    Policy,
    Vault,
    curator_bucket,
    levered_ltv_floor,
    sector_bucket,
)

PolicyScalar: TypeAlias = str | int | float | bool | None
PolicyValue: TypeAlias = PolicyScalar | tuple[PolicyScalar, ...]

_EPSILON = 1e-9

# Rules that describe one cycle's action rather than the resulting book, and so
# are scored against the buy even when a book is supplied.
_CYCLE_SCOPED_RULES = ("max_deploy_per_cycle_usd",)


class PolicyViolation(FrozenModel):
    rule: str
    entity: str
    limit: PolicyValue
    actual: PolicyValue

    @property
    def offending_entity(self) -> str:
        return self.entity


class PolicyResult(FrozenModel):
    ok: bool
    violations: tuple[PolicyViolation, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def _ok_matches_violations(self) -> "PolicyResult":
        if self.ok == bool(self.violations):
            raise ValueError("ok must be true only when there are no violations")
        return self


def check(
    allocation: Allocation | Mapping[str, object],
    policy: Policy | Mapping[str, object],
    known_instruments: Iterable[Vault | Mapping[str, object]],
) -> PolicyResult:
    allocation_model = _allocation(allocation)
    policy_model = _policy(policy)
    vault_by_id = _vault_by_id(known_instruments)
    violations: list[PolicyViolation] = []

    missing_instruments = tuple(
        sorted(
            leg.instrument_id
            for leg in allocation_model.legs
            if leg.instrument_id not in vault_by_id
        )
    )
    if policy_model.gates.new_instrument_needs_approval:
        for instrument_id in missing_instruments:
            violations.append(
                _violation(
                    "new_instrument_needs_approval",
                    instrument_id,
                    "approved instrument",
                    "unseen instrument",
                )
            )

    allocated_vaults = {
        leg.instrument_id: vault_by_id[leg.instrument_id]
        for leg in allocation_model.legs
        if leg.instrument_id in vault_by_id
    }
    _check_allowlists(allocation_model, policy_model, allocated_vaults, violations)
    _check_caps(allocation_model, policy_model, allocated_vaults, violations)
    _check_leverage(allocation_model, policy_model, allocated_vaults, violations)
    _check_diversification(allocation_model, policy_model, allocated_vaults, violations)
    _check_quality_caps(allocated_vaults, policy_model, violations)
    _check_gates(allocation_model, policy_model, violations)

    return PolicyResult(ok=not violations, violations=tuple(violations))


def _allocation(allocation: Allocation | Mapping[str, object]) -> Allocation:
    if isinstance(allocation, Allocation):
        return allocation
    return Allocation.model_validate(allocation)


def _policy(policy: Policy | Mapping[str, object]) -> Policy:
    if isinstance(policy, Policy):
        return policy
    return Policy.model_validate(policy)


def _vault_by_id(
    known_instruments: Iterable[Vault | Mapping[str, object]],
) -> dict[str, Vault]:
    vaults: dict[str, Vault] = {}
    for instrument in known_instruments:
        vault = (
            instrument
            if isinstance(instrument, Vault)
            else Vault.model_validate(instrument)
        )
        vaults[vault.instrument_id] = vault
    return vaults


def _check_allowlists(
    allocation: Allocation,
    policy: Policy,
    vault_by_id: Mapping[str, Vault],
    violations: list[PolicyViolation],
) -> None:
    for leg in allocation.legs:
        vault = vault_by_id.get(leg.instrument_id)
        if vault is None:
            continue
        for finding in eligibility.allowlist_findings(vault, policy.allowed):
            violations.append(
                _violation(
                    finding.rule, leg.instrument_id, finding.limit, finding.actual
                )
            )


def _check_caps(
    allocation: Allocation,
    policy: Policy,
    vault_by_id: Mapping[str, Vault],
    violations: list[PolicyViolation],
) -> None:
    instrument_weights: defaultdict[str, float] = defaultdict(float)
    protocol_weights: defaultdict[str, float] = defaultdict(float)
    curator_weights: defaultdict[str, float] = defaultdict(float)
    chain_weights: defaultdict[int, float] = defaultdict(float)
    sector_weights: defaultdict[str, float] = defaultdict(float)

    for leg in allocation.legs:
        instrument_weights[leg.instrument_id] += leg.weight
        vault = vault_by_id.get(leg.instrument_id)
        if vault is None:
            continue
        protocol_weights[vault.protocol] += leg.weight
        curator_weights[curator_bucket(vault.instrument_id, vault.curator)] += (
            leg.weight
        )
        chain_weights[vault.chain_id] += leg.weight
        sector_weights[sector_bucket(vault.sector)] += leg.weight

    caps = policy.caps
    _check_weight_cap(
        "max_weight_per_instrument",
        caps.max_weight_per_instrument,
        instrument_weights,
        violations,
    )
    _check_weight_cap(
        "max_weight_per_protocol",
        caps.max_weight_per_protocol,
        protocol_weights,
        violations,
    )
    _check_weight_cap(
        "max_weight_per_curator",
        caps.max_weight_per_curator,
        curator_weights,
        violations,
    )
    _check_weight_cap(
        "max_weight_per_chain",
        caps.max_weight_per_chain,
        chain_weights,
        violations,
    )
    # Absent = not enforced. The dimension is still reported by simulate/
    # concentration, so an unset cap cannot hide a monoculture — it only
    # declines to block one.
    if caps.max_weight_per_sector is not None:
        _check_weight_cap(
            "max_weight_per_sector",
            caps.max_weight_per_sector,
            sector_weights,
            violations,
        )


def _check_leverage(
    allocation: Allocation,
    policy: Policy,
    vault_by_id: Mapping[str, Vault],
    violations: list[PolicyViolation],
) -> None:
    """Gross exposure and health-factor checks on levered legs.

    The enforcement half of the synthetic-instrument design: a levered row's
    weight is its equity, so every weight cap above reads its net value and
    none reads its gross exposure. 30% of the book at L=8 passes
    ``max_weight_per_instrument = 0.30`` and is 240% of the book in gross
    exposure. These checks are what read the leverage back out.

    A levered leg that names no leverage is charged at its row's declared
    ``max_leverage``: an L nobody chose is whatever the venue allows, and
    charging it at 1 is exactly the lie by omission the design admits to.

    Without a published liquidation threshold the health factor is computed
    from the LTV the row's ceiling implies — a lower bound, so the check can
    reject a position the venue would accept but never the other way round.
    """
    caps = policy.caps
    levered_weight = 0.0
    gross_exposure = 0.0

    for leg in allocation.legs:
        vault = vault_by_id.get(leg.instrument_id)
        leverage = _leg_leverage(leg, vault)
        gross_exposure += leg.weight * leverage
        if vault is None or leg.weight <= 0:
            continue
        if not vault.is_levered:
            if leg.leverage is not None and leg.leverage > 1.0 + _EPSILON:
                violations.append(
                    _violation(
                        "leverage_on_unlevered_instrument",
                        leg.instrument_id,
                        1.0,
                        leg.leverage,
                    )
                )
            continue

        levered_weight += leg.weight
        ceiling = vault.max_leverage or 1.0
        if leverage > ceiling + _EPSILON:
            violations.append(
                _violation(
                    "max_leverage_declared", leg.instrument_id, ceiling, leverage
                )
            )
            continue
        if (
            caps.max_gross_leverage is not None
            and leverage > caps.max_gross_leverage + _EPSILON
        ):
            violations.append(
                _violation(
                    "max_gross_leverage",
                    leg.instrument_id,
                    caps.max_gross_leverage,
                    leverage,
                )
            )

        health_factor = leg_health_factor(vault, leverage)
        if health_factor is None:
            continue
        if (
            caps.min_health_factor is not None
            and health_factor < caps.min_health_factor - _EPSILON
        ):
            violations.append(
                _violation(
                    "min_health_factor",
                    leg.instrument_id,
                    caps.min_health_factor,
                    round(health_factor, 6),
                )
            )
        buffer_bps = levered.depeg_buffer_bps(health_factor)
        if (
            caps.min_depeg_buffer_bps is not None
            and vault.cross_asset
            and buffer_bps is not None
            and buffer_bps < caps.min_depeg_buffer_bps - _EPSILON
        ):
            violations.append(
                _violation(
                    "min_depeg_buffer_bps",
                    leg.instrument_id,
                    caps.min_depeg_buffer_bps,
                    buffer_bps,
                )
            )

    if (
        caps.max_weight_levered is not None
        and levered_weight > caps.max_weight_levered + _EPSILON
    ):
        violations.append(
            _violation(
                "max_weight_levered",
                "allocation",
                caps.max_weight_levered,
                levered_weight,
            )
        )
    if (
        caps.max_book_gross_exposure is not None
        and gross_exposure > caps.max_book_gross_exposure + _EPSILON
    ):
        violations.append(
            _violation(
                "max_book_gross_exposure",
                "allocation",
                caps.max_book_gross_exposure,
                round(gross_exposure, 9),
            )
        )


def _leg_leverage(leg: AllocationLeg, vault: Vault | None) -> float:
    if vault is None or not vault.is_levered:
        return 1.0
    if leg.leverage is not None:
        return leg.leverage
    return vault.max_leverage or 1.0


def leg_health_factor(vault: Vault, leverage: float) -> float | None:
    """A levered row's health factor at ``leverage``, or ``None`` with no debt.

    Uses the published liquidation threshold when there is one, else the LTV
    the declared ceiling implies (see :func:`levered_ltv_floor`), which bounds
    the real health factor from below.
    """
    if leverage <= 1.0 + _EPSILON:
        return None
    threshold = vault.liquidation_threshold or levered_ltv_floor(vault)
    if threshold is None:
        return None
    return levered.health_factor(leverage=leverage, liquidation_threshold=threshold)


def _check_diversification(
    allocation: Allocation,
    policy: Policy,
    vault_by_id: Mapping[str, Vault],
    violations: list[PolicyViolation],
) -> None:
    """Enforce the measured effective-position floor.

    Distinct from the weight caps in two ways that matter. It is a **floor**
    over the whole allocation rather than a ceiling on one bucket, and it is
    computed from history rather than from labels — so an allocation cannot
    pass it by holding twelve names that move as one.

    When the cap is set and the history needed to evaluate it is absent, that
    is a violation in its own right rather than a pass. A policy demanding
    measured diversification is not satisfied by an allocator that did not
    measure; the alternative is a cap that silently stops binding whenever the
    caller forgets to attach series, which is exactly the failure mode
    `max_weight_per_curator` is already in.
    """
    floor = policy.caps.min_effective_positions
    if floor is None:
        return

    weights_bps = {
        leg.instrument_id: int(round(leg.weight * 10_000))
        for leg in allocation.legs
        if leg.weight > 0
    }
    if not weights_bps:
        return

    series_by_id = {
        instrument_id: dict(vault.apy_daily)
        for instrument_id, vault in vault_by_id.items()
        if instrument_id in weights_bps and vault.apy_daily
    }
    if not series_by_id:
        violations.append(
            _violation(
                "min_effective_positions:unmeasurable",
                "allocation",
                floor,
                "no instrument carries dated history",
            )
        )
        return

    matrix = diversify.co_movement_matrix(series_by_id)
    actual = diversify.effective_positions(weights_bps, matrix)
    if actual < floor - _EPSILON:
        violations.append(
            _violation("min_effective_positions", "allocation", floor, actual)
        )


def _check_weight_cap(
    rule: str,
    limit: float,
    weights: Mapping[object, float],
    violations: list[PolicyViolation],
) -> None:
    for entity, actual in sorted(weights.items(), key=lambda item: str(item[0])):
        if actual > limit + _EPSILON:
            violations.append(_violation(rule, str(entity), limit, actual))


def _check_quality_caps(
    vault_by_id: Mapping[str, Vault],
    policy: Policy,
    violations: list[PolicyViolation],
) -> None:
    for instrument_id, vault in sorted(vault_by_id.items()):
        for finding in eligibility.quality_findings(vault, policy.caps):
            violations.append(
                _violation(finding.rule, instrument_id, finding.limit, finding.actual)
            )


def _check_gates(
    allocation: Allocation,
    policy: Policy,
    violations: list[PolicyViolation],
) -> None:
    if allocation.total_usd > policy.gates.max_deploy_per_cycle_usd + _EPSILON:
        violations.append(
            _violation(
                "max_deploy_per_cycle_usd",
                "allocation",
                policy.gates.max_deploy_per_cycle_usd,
                allocation.total_usd,
            )
        )

    mode = _autonomous_mode(allocation.metadata)
    if mode is not None and not policy.gates.autonomous_rebalance:
        violations.append(
            _violation(
                "autonomous_rebalance",
                "allocation",
                policy.gates.autonomous_rebalance,
                mode,
            )
        )


def resulting_book(
    allocation: Allocation | Mapping[str, object],
    held_usd: Mapping[str, float],
) -> Allocation:
    """The book that exists once ``allocation`` is bought on top of what is held.

    Metadata travels from the incremental allocation, not from the holdings --
    ``autonomous`` describes the action being gated, and the book being added to
    did not have an execution mode.
    """
    allocation_model = _allocation(allocation)

    usd: dict[str, Decimal] = defaultdict(Decimal)
    for instrument_id, amount in held_usd.items():
        usd[instrument_id] += Decimal(str(amount))
    for leg in allocation_model.legs:
        usd[leg.instrument_id] += Decimal(str(leg.usd))

    # A bought leg keeps the leverage it was built at. A top-up of a held
    # levered position does not: the held leverage is not recorded here, and
    # a blend of two unknown Ls is unknown, so it falls back to the row's
    # declared ceiling like any levered leg that names none.
    leverage = {
        leg.instrument_id: leg.leverage
        for leg in allocation_model.legs
        if not held_usd.get(leg.instrument_id)
    }

    total = sum(usd.values(), Decimal("0"))
    legs = tuple(
        AllocationLeg(
            instrument_id=instrument_id,
            weight=float(amount / total) if total > 0 else 0.0,
            usd=float(amount),
            leverage=leverage.get(instrument_id),
        )
        for instrument_id, amount in sorted(usd.items())
    )
    return Allocation(
        legs=legs,
        total_usd=float(total),
        metadata=dict(allocation_model.metadata),
    )


def check_incremental(
    allocation: Allocation | Mapping[str, object],
    policy: Policy | Mapping[str, object],
    known_instruments: Iterable[Vault | Mapping[str, object]],
    held_usd: Mapping[str, float],
) -> PolicyResult:
    """Gate a buy that is added to an existing book rather than replacing it.

    A top-up is only safe or unsafe as part of the book it produces: one $13 leg
    is 100% of itself and 13% of the book it joins, so scoring it alone rejects
    every corrective action a partially-executed book needs.

    The split is the whole point. **Caps are properties of a book** -- weights,
    diversification, eligibility -- so they are scored against the result.
    **Gates are properties of a cycle**, so ``max_deploy_per_cycle_usd`` is
    scored against the buy; charging the book's total against a per-cycle deploy
    limit would reject every top-up of a book larger than one cycle's budget.
    """
    book = check(resulting_book(allocation, held_usd), policy, known_instruments)
    cycle = check(allocation, policy, known_instruments)

    violations = tuple(
        violation
        for violation in book.violations
        if violation.rule not in _CYCLE_SCOPED_RULES
    ) + tuple(
        violation
        for violation in cycle.violations
        if violation.rule in _CYCLE_SCOPED_RULES
    )
    return PolicyResult(ok=not violations, violations=violations)


def _autonomous_mode(metadata: Mapping[str, object]) -> str | bool | None:
    for key in ("autonomous", "unattended"):
        if metadata.get(key) is True:
            return True

    for key in ("execution_mode", "mode"):
        value = metadata.get(key)
        if isinstance(value, str) and value.casefold() in {"autonomous", "unattended"}:
            return value

    return None


def _violation(
    rule: str,
    entity: str,
    limit: PolicyValue,
    actual: PolicyValue,
) -> PolicyViolation:
    return PolicyViolation(rule=rule, entity=entity, limit=limit, actual=actual)


__all__ = [
    "PolicyResult",
    "PolicyViolation",
    "check",
    "check_incremental",
    "leg_health_factor",
    "resulting_book",
]
