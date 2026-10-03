"""Execution, split into a plan and the application of exactly that plan.

`plan_*` builds and prices a plan and broadcasts nothing. `apply_*` executes a plan
`plan_*` returned, as it stands: it never discovers, sizes or plans again, and it
refuses a plan the wallet has moved past. Approval sits between the two, which is
what lets a human approve the plan that then runs.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any

from open_allocator.core import policy as policy_core
from open_allocator.core import positions as positions_core
from open_allocator.core.policy_loader import load_policy
from open_allocator.core.rebalance import RebalancePolicyError
from open_allocator.core.state import ScopedIdempotencyStore, backend_from_config
from open_allocator.core.types import (
    Allocation,
    BundleToken,
    FrozenModel,
    Policy,
    TxBundle,
    TxPlan,
    TxStep,
)
from open_allocator.core.withdraw import WithdrawPlan
from open_allocator.exec.allocation_plan import AllocationPlan
from open_allocator.exec.client import OneTxClient
from open_allocator.exec.config import AllocatorConfig
from open_allocator.exec.execute import (
    ExecutionReport,
    PolicyCheckFailed,
    apply_allocation_plan,
    check_allocation_policy,
    plan_allocation,
)
from open_allocator.exec.funding import FundingRequirement
from open_allocator.exec.loop_close import (
    LoopClosingPlan,
    apply_loop_closing_plan,
    plan_loop_closing,
)
from open_allocator.exec.loop_close import dry_run_report as loop_close_dry_run_report
from open_allocator.exec.loop_open import (
    LoopOpeningPlan,
    apply_loop_opening_plan,
    check_opening_policy,
    plan_loop_opening,
)
from open_allocator.exec.loop_open import dry_run_report as loop_open_dry_run_report
from open_allocator.exec.rebalance import (
    RebalancingPlan,
    apply_rebalancing_plan,
    plan_rebalancing,
)
from open_allocator.exec.rebalance import dry_run_report as rebalance_dry_run_report
from open_allocator.exec.transfer import (
    TransferPlan,
    apply_transfer_plan,
    plan_transfer,
)
from open_allocator.exec.transfer import dry_run_report as transfer_dry_run_report
from open_allocator.exec.withdraw import (
    WithdrawalPlan,
    apply_withdrawal_plan,
    plan_withdrawal,
)
from open_allocator.exec.withdraw import dry_run_report as withdraw_dry_run_report
from open_allocator.service._common import (
    JsonObject,
    model_payload,
    signer_address,
    signer_from_config,
)
from open_allocator.service.allocation import DEFAULT_POLICY_PATH, parse_allocation
from open_allocator.service.errors import ServiceError
from open_allocator.service.plan_store import PlanStore, plan_hash
from open_allocator.service.universe import OnWarning, discover_vaults_from_client

EXECUTE = "execute"
WITHDRAW = "withdraw"
REBALANCE = "rebalance"
LOOP_OPEN = "loop-open"
LOOP_CLOSE = "loop-close"
BRIDGE = "bridge"


def idempotency_store(config: object, scope: str) -> ScopedIdempotencyStore | None:
    """The store that decides whether a step is re-sent, bound to its scope.

    Where it *lives* is `core.state`'s problem: on a laptop that is a JSON file,
    and in a container whose filesystem does not survive a retry it is whatever
    backend the caller injected. This is the seam that stops a retried run from
    broadcasting trades that already landed.
    """
    backend = backend_from_config(config, needs="idempotency_store_path")
    if backend is None:
        return None
    return ScopedIdempotencyStore(backend, scope)


def allocation_scope(allocation: Allocation) -> str:
    payload = allocation.model_dump(mode="json")
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def withdraw_scope(
    position: positions_core.PositionHolding,
    *,
    amount: float | str | None,
) -> str:
    payload = {"position": position.model_dump(mode="json"), "amount": amount}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def rebalance_scope(
    positions: positions_core.Positions,
    target: Allocation,
    *,
    min_trade_usd: float,
) -> str:
    payload = {
        "positions": positions.model_dump(mode="json"),
        "target": target.model_dump(mode="json"),
        "min_trade_usd": min_trade_usd,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def loop_open_scope(loop_id: str, account: str) -> str:
    payload = {"loop_id": loop_id, "account": account, "action": "open"}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def loop_close_scope(loop_id: str, account: str) -> str:
    payload = {"loop_id": loop_id, "account": account}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def bridge_scope(
    address: str,
    from_chain_id: int,
    to_chain_id: int,
    amount: float,
    ref: str | None,
) -> str:
    """The same arguments resume the same transfer; another `ref` starts another."""
    payload = {
        "bridge": {
            "account": address.casefold(),
            "from": from_chain_id,
            "to": to_chain_id,
            # A float, as the CLI passes it, so `100` and `100.0` are one scope.
            "amount": float(amount),
            "ref": ref,
        }
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def plan_allocation_execution(
    allocation: Allocation | Mapping[str, Any],
    *,
    policy: Policy | Path = DEFAULT_POLICY_PATH,
    on_warning: OnWarning | None = None,
) -> AllocationPlan:
    """The deposit plan `execute` would submit for `allocation`, not sent."""
    allocation_model = parse_allocation(allocation)
    policy_model = policy if isinstance(policy, Policy) else load_policy(policy)
    config = AllocatorConfig()
    signer = signer_from_config(config)
    with OneTxClient(config) as client:
        known_instruments = discover_vaults_from_client(
            client, enrich=True, loops=True, on_warning=on_warning
        )
        return plan_allocation(
            client,
            signer,  # type: ignore[arg-type]
            allocation_model,
            policy_model,
            known_instruments=known_instruments,
            config=config,
            # Read only: legs already sent or bridging are not planned again.
            idempotency_store=idempotency_store(
                config, allocation_scope(allocation_model)
            ),
        )


def dry_run_report(planned: AllocationPlan) -> JsonObject:
    """The `execute` dry-run report: the plan, its preparation and blockers."""
    preparation = planned.preparation
    return model_payload(
        ExecutionReport(
            status="planned",
            policy_result=planned.policy_result,
            plan=planned.plan,
            preparations=preparation.preparations,
            funding=preparation.funding,
            loops=planned.loops,
            messages=(
                "dry-run only; no transactions broadcast",
                *planned.messages,
                *preparation.messages,
                *preparation.blockers,
            ),
        )
    )


def plan_execute(
    allocation: Allocation | Mapping[str, Any],
    *,
    policy: Policy | Path = DEFAULT_POLICY_PATH,
    on_warning: OnWarning | None = None,
) -> JsonObject:
    """`{"kind", "plan", "plan_hash", "report"}` for `allocation`; sends nothing.

    `plan` is what `apply_execute` takes, and `plan_hash` binds an approval to it.
    `report` is the dry run `execute` prints without `--confirm`.
    """
    planned = plan_allocation_execution(
        allocation, policy=policy, on_warning=on_warning
    )
    document = planned.model_dump(mode="json")
    return {
        "kind": EXECUTE,
        "plan": document,
        "plan_hash": plan_hash(EXECUTE, document),
        "report": dry_run_report(planned),
    }


def apply_execute(
    plan: AllocationPlan | Mapping[str, Any],
    *,
    expected_hash: str | None = None,
) -> JsonObject:
    """Execute a plan from `plan_execute` exactly; the `ExecutionReport` payload.

    With `expected_hash`, a plan that does not hash to it is refused before
    anything is read or sent.
    """
    document = (
        plan.model_dump(mode="json") if isinstance(plan, AllocationPlan) else dict(plan)
    )
    if expected_hash is not None and plan_hash(EXECUTE, document) != expected_hash:
        raise ServiceError(
            "plan_mismatch", "the plan does not match the approved plan hash"
        )
    planned = (
        plan
        if isinstance(plan, AllocationPlan)
        else AllocationPlan.model_validate(document)
    )
    config = AllocatorConfig()
    signer = signer_from_config(config)
    with OneTxClient(config) as client:
        report = apply_allocation_plan(
            client,
            signer,  # type: ignore[arg-type]
            planned,
            config=config,
            idempotency_store=idempotency_store(
                config, allocation_scope(planned.allocation)
            ),
        )
    return model_payload(report)


def recheck_execute_policy(
    plan: AllocationPlan | Mapping[str, Any],
    *,
    policy: Policy | Path = DEFAULT_POLICY_PATH,
    on_warning: OnWarning | None = None,
) -> JsonObject:
    """Check a stored plan's allocation against `policy` on today's shelf.

    The plan carries the policy result it was built under, against the policy the
    caller chose then. Approval checks it again against the operator's policy
    before anything is sent. Raises `ServiceError` `policy_violation`.
    """
    planned = (
        plan
        if isinstance(plan, AllocationPlan)
        else AllocationPlan.model_validate(plan)
    )
    policy_model = policy if isinstance(policy, Policy) else load_policy(policy)
    config = AllocatorConfig()
    signer = signer_from_config(config)
    with OneTxClient(config) as client:
        known_instruments = discover_vaults_from_client(
            client, enrich=True, loops=True, on_warning=on_warning
        )
        try:
            result = check_allocation_policy(
                client,
                signer,  # type: ignore[arg-type]
                planned.allocation,
                policy_model,
                known_instruments=known_instruments,
                config=config,
                idempotency_store=idempotency_store(
                    config, allocation_scope(planned.allocation)
                ),
            )
        except PolicyCheckFailed as failed:
            raise ServiceError("policy_violation", str(failed)) from failed
    return model_payload(result)


def _token_amount(raw: str | None, token: BundleToken | None) -> JsonObject | None:
    """A raw amount with its token, and the decimal amount when it is known."""
    if raw is None:
        return None
    amount = None
    if raw != "max" and token is not None:
        amount = format(Decimal(raw).scaleb(-token.decimals).normalize(), "f")
    return {
        "raw": raw,
        "amount": amount,
        "symbol": token.symbol if token is not None else None,
        "token": token.address if token is not None else None,
    }


def _funding(item: FundingRequirement, token: BundleToken | None) -> JsonObject:
    return {
        "chain_id": item.chain_id,
        "required": _token_amount(item.required_raw, token),
        "available": _token_amount(item.available_raw, token),
        "shortfall": _token_amount(item.shortfall_raw, token),
        "includes_gas_charge": item.includes_gas_charge,
        "ok": item.ok,
    }


def _tokens(bundles: tuple[TxBundle, ...]) -> dict[tuple[int, str], BundleToken]:
    """Every token the bundles move, by chain and lowercased address."""
    tokens: dict[tuple[int, str], BundleToken] = {}
    for bundle in bundles:
        for token in (bundle.token_in, bundle.token_out):
            tokens.setdefault((bundle.chain_id, token.address.lower()), token)
    return tokens


def _bundle(bundle: TxBundle, steps: tuple[TxStep, ...]) -> JsonObject:
    return {
        "bundle_id": bundle.bundle_id,
        "leg_index": bundle.leg_index,
        "instrument_id": bundle.instrument_id,
        "action": bundle.action,
        "chain_id": bundle.chain_id,
        "amount_in": _token_amount(bundle.amount, bundle.token_in),
        "expected_out": _token_amount(bundle.expected_out, bundle.token_out),
        "min_out": _token_amount(bundle.min_out, bundle.token_out),
        "steps": [steps[index].kind for index in bundle.step_indexes],
        "expires_at": bundle.expires_at,
        "bridge": (
            None
            if bundle.bridge is None
            else {
                "to_chain_id": bundle.bridge.to_chain_id,
                "fast": bundle.bridge.fast,
                "max_fee": _token_amount(bundle.bridge.max_fee, bundle.token_in),
            }
        ),
    }


def review_execute(plan: AllocationPlan | Mapping[str, Any]) -> JsonObject:
    """What a person approving a stored `execute` plan reads, from the plan alone.

    Amounts are the plan's own: nothing is re-read or re-quoted, so the review
    describes exactly what an approval would submit.
    """
    planned = (
        plan
        if isinstance(plan, AllocationPlan)
        else AllocationPlan.model_validate(plan)
    )
    bundles = planned.plan.bundles
    tokens = _tokens(bundles)
    for chain_id, deposit_token in planned.deposit_tokens.items():
        tokens.setdefault(
            (chain_id, deposit_token.address.lower()),
            BundleToken(
                address=deposit_token.address,
                symbol="USDC",
                decimals=deposit_token.decimals,
            ),
        )
    steps = planned.plan.steps
    preparation = planned.preparation
    return {
        "kind": EXECUTE,
        "account": planned.account,
        "target_usd": planned.allocation.total_usd,
        "deposit_usd": sum(planned.deposit_usd.values()),
        "legs": [
            {
                "leg_index": index,
                "instrument_id": leg.instrument_id,
                "target_usd": leg.usd,
                # None when the leg was skipped while planning.
                "deposit_usd": planned.deposit_usd.get(index),
                "leverage": leg.leverage,
            }
            for index, leg in enumerate(planned.allocation.legs)
        ],
        "bundles": [_bundle(bundle, steps) for bundle in bundles],
        "loops": [loop.model_dump(mode="json") for loop in planned.loops],
        "funding": [
            _funding(item, tokens.get((item.chain_id, item.token.lower())))
            for item in preparation.funding
        ],
        "policy": planned.policy_result.model_dump(mode="json"),
        "notes": [*planned.messages, *preparation.messages],
        "blockers": list(preparation.blockers),
        "transactions": len(steps),
    }


def select_position(
    source: positions_core.Positions | positions_core.PositionHolding,
    position_id: str,
) -> positions_core.PositionHolding:
    """The one holding of `source` with instrument id `position_id`."""
    if isinstance(source, positions_core.PositionHolding):
        if source.instrument_id != position_id:
            raise ServiceError("not_found", f"position not found: {position_id}")
        return source
    matches = [
        holding for holding in source.holdings if holding.instrument_id == position_id
    ]
    if not matches:
        raise ServiceError("not_found", f"position not found: {position_id}")
    if len(matches) > 1:
        raise ServiceError("invalid_input", f"position id is ambiguous: {position_id}")
    return matches[0]


def plan_withdraw(
    position: str,
    *,
    amount: float | None = None,
    policy: Policy | Path = DEFAULT_POLICY_PATH,
    on_warning: Callable[[str], None] | None = None,
) -> JsonObject:
    """`{"kind", "plan", "plan_hash", "report"}` for withdrawing `position`.

    `position` is an instrument id in the signer's current book, read live.
    `amount` is in USD; omitted, or at least the position's value, it is a full
    exit. Sends nothing; `report` is the dry run `withdraw` prints.
    """
    policy_model = policy if isinstance(policy, Policy) else load_policy(policy)
    config = AllocatorConfig()
    signer = signer_from_config(config)
    from open_allocator.exec import loops as loops_exec

    with OneTxClient(config) as client:
        book, warnings = loops_exec.read_book(client, signer_address(config), config)
        if on_warning is not None:
            for warning in warnings:
                on_warning(warning)
        holding = select_position(book, position)
        planned = plan_withdrawal(
            client,
            signer,  # type: ignore[arg-type]
            holding,
            policy_model,
            amount,
            config=config,
            # Read only: a withdrawal already sent is not planned again.
            idempotency_store=idempotency_store(
                config, withdraw_scope(holding, amount=amount)
            ),
        )
    document = planned.model_dump(mode="json")
    return {
        "kind": WITHDRAW,
        "plan": document,
        "plan_hash": plan_hash(WITHDRAW, document),
        "report": model_payload(withdraw_dry_run_report(planned)),
    }


def apply_withdraw(
    plan: WithdrawalPlan | Mapping[str, Any],
    *,
    expected_hash: str | None = None,
) -> JsonObject:
    """Execute a plan from `plan_withdraw` exactly; the withdraw report payload.

    With `expected_hash`, a plan that does not hash to it is refused before
    anything is read or sent.
    """
    document = (
        plan.model_dump(mode="json") if isinstance(plan, WithdrawalPlan) else dict(plan)
    )
    if expected_hash is not None and plan_hash(WITHDRAW, document) != expected_hash:
        raise ServiceError(
            "plan_mismatch", "the plan does not match the approved plan hash"
        )
    planned = (
        plan
        if isinstance(plan, WithdrawalPlan)
        else WithdrawalPlan.model_validate(document)
    )
    config = AllocatorConfig()
    signer = signer_from_config(config)
    with OneTxClient(config) as client:
        report = apply_withdrawal_plan(
            client,
            signer,  # type: ignore[arg-type]
            planned,
            config=config,
            idempotency_store=idempotency_store(
                config, withdraw_scope(planned.position, amount=planned.amount)
            ),
        )
    return model_payload(report)


def _withdraw_bundle(
    bundle: TxBundle, steps: tuple[TxStep, ...], exit_plan: WithdrawPlan
) -> JsonObject:
    """A withdraw bundle, its amount in the underlying asset it is denominated in.

    1Tx takes a partial withdrawal's amount in raw underlying units, not in the
    yield token the bundle spends; `max` is the whole position either way.
    """
    reviewed = _bundle(bundle, steps)
    decimals = exit_plan.underlying_decimals
    if bundle.amount != "max" and decimals is not None:
        reviewed["amount_in"] = {
            "raw": bundle.amount,
            "amount": format(Decimal(bundle.amount).scaleb(-decimals).normalize(), "f"),
            "symbol": exit_plan.symbol,
            "token": None,
        }
    return reviewed


def review_withdraw(plan: WithdrawalPlan | Mapping[str, Any]) -> JsonObject:
    """What a person approving a stored `withdraw` plan reads, from the plan alone."""
    planned = (
        plan
        if isinstance(plan, WithdrawalPlan)
        else WithdrawalPlan.model_validate(plan)
    )
    exit_plan = planned.withdraw_plan
    bundles = planned.plan.bundles
    tokens = _tokens(bundles)
    steps = planned.plan.steps
    preparation = planned.preparation
    return {
        "kind": WITHDRAW,
        "account": planned.account,
        "instrument_id": exit_plan.instrument_id,
        "protocol": exit_plan.protocol,
        "chain_id": exit_plan.chain_id,
        "symbol": exit_plan.symbol,
        "full_exit": exit_plan.full_exit,
        "requested_usd": exit_plan.requested_usd,
        "current_usd": exit_plan.current_usd,
        "shares": exit_plan.yield_token_amount,
        "share_balance": exit_plan.share_balance,
        "share_symbol": exit_plan.yield_token_symbol,
        "expected_usdc": planned.sell.expected_usdc,
        "bundles": [_withdraw_bundle(bundle, steps, exit_plan) for bundle in bundles],
        "funding": [
            _funding(item, tokens.get((item.chain_id, item.token.lower())))
            for item in preparation.funding
        ],
        "notes": list(preparation.messages),
        "blockers": list(preparation.blockers),
        "transactions": len(steps),
    }


def plan_rebalance(
    target: Allocation | Mapping[str, Any],
    *,
    positions: positions_core.Positions | Mapping[str, Any] | None = None,
    min_trade_usd: float = 1.0,
    policy: Policy | Path = DEFAULT_POLICY_PATH,
    on_warning: OnWarning | None = None,
) -> JsonObject:
    """`{"kind", "plan", "plan_hash", "report"}` for rebalancing toward `target`.

    `positions` is the book to trade from; omitted, the signer's book is read
    live. Trades under `min_trade_usd` are skipped. Sends nothing; `report` is
    the dry run `rebalance` prints.
    """
    target_model = parse_allocation(target)
    policy_model = policy if isinstance(policy, Policy) else load_policy(policy)
    config = AllocatorConfig()
    signer = signer_from_config(config)
    from open_allocator.exec import loops as loops_exec

    with OneTxClient(config) as client:
        if positions is None:
            book, warnings = loops_exec.read_book(
                client, signer_address(config), config
            )
            if on_warning is not None:
                for warning in warnings:
                    on_warning({"warning": "unread_position", "message": warning})
        elif isinstance(positions, positions_core.Positions):
            book = positions
        else:
            book = positions_core.Positions.model_validate(positions)
        known_instruments = discover_vaults_from_client(
            client, enrich=True, loops=True, on_warning=on_warning
        )
        try:
            planned = plan_rebalancing(
                client,
                signer,  # type: ignore[arg-type]
                book,
                target_model,
                policy_model,
                known_instruments=known_instruments,
                config=config,
                # Read only: trades already sent are not planned again.
                idempotency_store=idempotency_store(
                    config,
                    rebalance_scope(book, target_model, min_trade_usd=min_trade_usd),
                ),
                min_trade_usd=min_trade_usd,
            )
        except RebalancePolicyError as failed:
            raise ServiceError("policy_violation", str(failed)) from failed
    document = planned.model_dump(mode="json")
    return {
        "kind": REBALANCE,
        "plan": document,
        "plan_hash": plan_hash(REBALANCE, document),
        "report": model_payload(rebalance_dry_run_report(planned)),
    }


def apply_rebalance(
    plan: RebalancingPlan | Mapping[str, Any],
    *,
    expected_hash: str | None = None,
) -> JsonObject:
    """Execute a plan from `plan_rebalance` exactly; the rebalance report payload.

    With `expected_hash`, a plan that does not hash to it is refused before
    anything is read or sent.
    """
    document = (
        plan.model_dump(mode="json")
        if isinstance(plan, RebalancingPlan)
        else dict(plan)
    )
    if expected_hash is not None and plan_hash(REBALANCE, document) != expected_hash:
        raise ServiceError(
            "plan_mismatch", "the plan does not match the approved plan hash"
        )
    planned = (
        plan
        if isinstance(plan, RebalancingPlan)
        else RebalancingPlan.model_validate(document)
    )
    config = AllocatorConfig()
    signer = signer_from_config(config)
    with OneTxClient(config) as client:
        report = apply_rebalancing_plan(
            client,
            signer,  # type: ignore[arg-type]
            planned,
            config=config,
            idempotency_store=idempotency_store(
                config,
                rebalance_scope(
                    planned.positions,
                    planned.rebalance_plan.target,
                    min_trade_usd=planned.min_trade_usd,
                ),
            ),
        )
    return model_payload(report)


def recheck_rebalance_policy(
    plan: RebalancingPlan | Mapping[str, Any],
    *,
    policy: Policy | Path = DEFAULT_POLICY_PATH,
    on_warning: OnWarning | None = None,
) -> JsonObject:
    """Check a stored rebalance's target against `policy` on today's shelf.

    The same check planning ran, so a target built under one policy or shelf
    does not run once either has moved against it. Raises `ServiceError`
    `policy_violation`.
    """
    planned = (
        plan
        if isinstance(plan, RebalancingPlan)
        else RebalancingPlan.model_validate(plan)
    )
    policy_model = policy if isinstance(policy, Policy) else load_policy(policy)
    with OneTxClient(AllocatorConfig()) as client:
        known_instruments = discover_vaults_from_client(
            client, enrich=True, loops=True, on_warning=on_warning
        )
    result = policy_core.check(
        planned.rebalance_plan.target, policy_model, known_instruments
    )
    if not result.ok:
        raise ServiceError("policy_violation", str(RebalancePolicyError(result)))
    return model_payload(result)


def review_rebalance(plan: RebalancingPlan | Mapping[str, Any]) -> JsonObject:
    """What a person approving a stored `rebalance` plan reads, from the plan alone.

    Withdrawal amounts are shown in the underlying, as for a withdrawal: 1Tx
    takes a partial sell in raw underlying units, not in the yield token.
    """
    planned = (
        plan
        if isinstance(plan, RebalancingPlan)
        else RebalancingPlan.model_validate(plan)
    )
    trades_plan = planned.rebalance_plan
    bundles = planned.plan.bundles
    tokens = _tokens(bundles)
    steps = planned.plan.steps
    preparation = planned.preparation
    holdings: dict[str, positions_core.PositionHolding] = {}
    for holding in planned.positions.holdings:
        holdings.setdefault(holding.instrument_id, holding)

    def reviewed(bundle: TxBundle) -> JsonObject:
        entry = _bundle(bundle, steps)
        holding = holdings.get(bundle.instrument_id)
        if (
            bundle.action == "withdraw"
            and bundle.amount != "max"
            and holding is not None
            and holding.decimals is not None
        ):
            entry["amount_in"] = {
                "raw": bundle.amount,
                "amount": format(
                    Decimal(bundle.amount).scaleb(-holding.decimals).normalize(), "f"
                ),
                "symbol": holding.symbol,
                "token": None,
            }
        return entry

    return {
        "kind": REBALANCE,
        "account": planned.account,
        "book_usd": planned.positions.total_usd,
        "target_usd": trades_plan.target.total_usd,
        "total_sell_usd": trades_plan.total_sell_usd,
        "total_buy_usd": trades_plan.total_buy_usd,
        "min_trade_usd": planned.min_trade_usd,
        "trades": [
            {
                "trade_index": index,
                "instrument_id": trade.instrument_id,
                "action": trade.action,
                "usd": trade.usd,
                "current_usd": trade.current_usd,
                "target_usd": trade.target_usd,
                "current_weight": trade.current_weight,
                "target_weight": trade.target_weight,
                # A buy's actual spend after sizing; None when it was skipped.
                "deposit_usd": (
                    planned.deposit_usd.get(index) if trade.action == "buy" else None
                ),
            }
            for index, trade in enumerate(trades_plan.trades)
        ],
        "skipped": [
            {
                "instrument_id": delta.instrument_id,
                "action": delta.action,
                "delta_usd": delta.delta_usd,
            }
            for delta in trades_plan.skipped_deltas
        ],
        "bundles": [reviewed(bundle) for bundle in bundles],
        "funding": [
            _funding(item, tokens.get((item.chain_id, item.token.lower())))
            for item in preparation.funding
        ],
        "policy": trades_plan.policy_result.model_dump(mode="json"),
        "notes": [*planned.messages, *preparation.messages],
        "blockers": list(preparation.blockers),
        "transactions": len(steps),
    }


def _approved_document(
    kind: str,
    plan: FrozenModel | Mapping[str, Any],
    expected_hash: str | None,
) -> JsonObject:
    """`plan` as JSON, refused when it does not hash to `expected_hash`."""
    document = (
        plan.model_dump(mode="json") if isinstance(plan, FrozenModel) else dict(plan)
    )
    if expected_hash is not None and plan_hash(kind, document) != expected_hash:
        raise ServiceError(
            "plan_mismatch", "the plan does not match the approved plan hash"
        )
    return document


def _loop_bundles(plan: TxPlan) -> list[JsonObject]:
    return [_bundle(bundle, plan.steps) for bundle in plan.bundles]


def plan_loop_open(
    loop: str,
    *,
    equity_usd: float,
    leverage: float,
    policy: Policy | Path = DEFAULT_POLICY_PATH,
    on_warning: OnWarning | None = None,
) -> JsonObject:
    """`{"kind", "plan", "plan_hash", "report"}` for opening loop `loop`.

    `equity_usd` is idle USDC on the loop's chain, levered to `leverage`. The
    open is scored against the signer's book, read live. Sends nothing; `report`
    is the dry run `loop-open` prints.
    """
    policy_model = policy if isinstance(policy, Policy) else load_policy(policy)
    config = AllocatorConfig()
    signer = signer_from_config(config)
    with OneTxClient(config) as client:
        known_instruments = discover_vaults_from_client(
            client, enrich=True, on_warning=on_warning
        )
        try:
            planned = plan_loop_opening(
                client,
                signer,  # type: ignore[arg-type]
                loop,
                equity_usd=equity_usd,
                leverage=leverage,
                policy=policy_model,
                known_instruments=known_instruments,
                config=config,
            )
        except PolicyCheckFailed as failed:
            raise ServiceError("policy_violation", str(failed)) from failed
    document = planned.model_dump(mode="json")
    return {
        "kind": LOOP_OPEN,
        "plan": document,
        "plan_hash": plan_hash(LOOP_OPEN, document),
        "report": model_payload(loop_open_dry_run_report(planned)),
    }


def apply_loop_open(
    plan: LoopOpeningPlan | Mapping[str, Any],
    *,
    expected_hash: str | None = None,
) -> JsonObject:
    """Execute a plan from `plan_loop_open` exactly; the loop-open report payload."""
    document = _approved_document(LOOP_OPEN, plan, expected_hash)
    planned = LoopOpeningPlan.model_validate(document)
    config = AllocatorConfig()
    signer = signer_from_config(config)
    with OneTxClient(config) as client:
        report = apply_loop_opening_plan(
            client,
            signer,  # type: ignore[arg-type]
            planned,
            config=config,
            idempotency_store=idempotency_store(
                config, loop_open_scope(planned.loop_id, planned.account)
            ),
        )
    return model_payload(report)


def recheck_loop_open_policy(
    plan: LoopOpeningPlan | Mapping[str, Any],
    *,
    policy: Policy | Path = DEFAULT_POLICY_PATH,
    on_warning: OnWarning | None = None,
) -> JsonObject:
    """Score a stored open against `policy`, today's book and loop screen.

    The check planning ran, so an open built under one policy or book does not
    run once either has moved against it. Raises `ServiceError`
    `policy_violation`.
    """
    planned = LoopOpeningPlan.model_validate(
        plan.model_dump(mode="json") if isinstance(plan, LoopOpeningPlan) else plan
    )
    policy_model = policy if isinstance(policy, Policy) else load_policy(policy)
    config = AllocatorConfig()
    with OneTxClient(config) as client:
        known_instruments = discover_vaults_from_client(
            client, enrich=True, on_warning=on_warning
        )
        result = check_opening_policy(
            client,
            planned,
            policy_model,
            known_instruments=known_instruments,
            config=config,
        )
    if not result.ok:
        raise ServiceError("policy_violation", str(PolicyCheckFailed(result)))
    return model_payload(result)


def review_loop_open(plan: LoopOpeningPlan | Mapping[str, Any]) -> JsonObject:
    """What a person approving a stored `loop-open` plan reads, from the plan alone."""
    planned = LoopOpeningPlan.model_validate(
        plan.model_dump(mode="json") if isinstance(plan, LoopOpeningPlan) else plan
    )
    return {
        "kind": LOOP_OPEN,
        "account": planned.account,
        "loop_id": planned.loop_id,
        "equity_usd": planned.equity_usd,
        "leverage": planned.leverage,
        "bundles": _loop_bundles(planned.plan),
        "loop": planned.announcement.model_dump(mode="json"),
        "policy": planned.policy_result.model_dump(mode="json"),
        "notes": list(planned.messages),
        "transactions": len(planned.plan.steps),
    }


def plan_loop_close(
    loop: str,
    *,
    policy: Policy | Path = DEFAULT_POLICY_PATH,
    on_warning: OnWarning | None = None,
) -> JsonObject:
    """`{"kind", "plan", "plan_hash", "report"}` for closing loop `loop`.

    The policy's caps bound the venue's measurement of the unwind. Sends
    nothing; `report` is the dry run `loop-close` prints.
    """
    policy_model = policy if isinstance(policy, Policy) else load_policy(policy)
    config = AllocatorConfig()
    signer = signer_from_config(config)
    with OneTxClient(config) as client:
        known_instruments = discover_vaults_from_client(
            client, enrich=True, on_warning=on_warning
        )
        planned = plan_loop_closing(
            client,
            signer,  # type: ignore[arg-type]
            loop,
            policy=policy_model,
            known_instruments=known_instruments,
            config=config,
        )
    document = planned.model_dump(mode="json")
    return {
        "kind": LOOP_CLOSE,
        "plan": document,
        "plan_hash": plan_hash(LOOP_CLOSE, document),
        "report": model_payload(loop_close_dry_run_report(planned)),
    }


def apply_loop_close(
    plan: LoopClosingPlan | Mapping[str, Any],
    *,
    expected_hash: str | None = None,
) -> JsonObject:
    """Execute a plan from `plan_loop_close` exactly; the loop-close report payload."""
    document = _approved_document(LOOP_CLOSE, plan, expected_hash)
    planned = LoopClosingPlan.model_validate(document)
    config = AllocatorConfig()
    signer = signer_from_config(config)
    with OneTxClient(config) as client:
        report = apply_loop_closing_plan(
            client,
            signer,  # type: ignore[arg-type]
            planned,
            config=config,
            idempotency_store=idempotency_store(
                config, loop_close_scope(planned.loop_id, planned.account)
            ),
        )
    return model_payload(report)


def review_loop_close(plan: LoopClosingPlan | Mapping[str, Any]) -> JsonObject:
    """What a person approving a stored `loop-close` plan reads, from the plan alone."""
    planned = LoopClosingPlan.model_validate(
        plan.model_dump(mode="json") if isinstance(plan, LoopClosingPlan) else plan
    )
    return {
        "kind": LOOP_CLOSE,
        "account": planned.account,
        "loop_id": planned.loop_id,
        "bundles": _loop_bundles(planned.plan),
        "loop": planned.announcement.model_dump(mode="json"),
        "transactions": len(planned.plan.steps),
    }


def plan_bridge(
    from_chain_id: int,
    to_chain_id: int,
    amount: float,
    *,
    ref: str | None = None,
    on_warning: OnWarning | None = None,
) -> JsonObject:
    """`{"kind", "plan", "plan_hash", "report"}` for moving `amount` USDC.

    A CCTP transfer of the Safe's USDC from one chain to another, with no
    deposit. The same arguments name the same transfer: one already under way is
    planned as itself, to be advanced; a settled or failed one is refused, and a
    new `ref` starts another. Sends nothing; `report` is the dry run `bridge`
    prints.
    """
    config = AllocatorConfig()
    signer = signer_from_config(config)
    address = signer_address(config)
    with OneTxClient(config) as client:
        known_instruments = discover_vaults_from_client(client, on_warning=on_warning)
        planned = plan_transfer(
            client,
            signer,
            from_chain_id=from_chain_id,
            to_chain_id=to_chain_id,
            amount_usdc=amount,
            known_instruments=known_instruments,
            ref=ref,
            config=config,
            # Read only: a transfer under way is advanced, not burned again.
            idempotency_store=idempotency_store(
                config,
                bridge_scope(address, from_chain_id, to_chain_id, amount, ref),
            ),
        )
    report = model_payload(transfer_dry_run_report(planned))
    existing = planned.existing
    if existing is not None and existing.state in ("completed", "failed"):
        raise ServiceError("invalid_input", "; ".join(report["messages"]))
    document = planned.model_dump(mode="json")
    return {
        "kind": BRIDGE,
        "plan": document,
        "plan_hash": plan_hash(BRIDGE, document),
        "report": report,
    }


def apply_bridge(
    plan: TransferPlan | Mapping[str, Any],
    *,
    expected_hash: str | None = None,
) -> JsonObject:
    """Execute a plan from `plan_bridge` exactly; the bridge report payload.

    The burn and whatever of the transfer can follow it now. A transfer waiting
    on Circle is advanced by planning and approving it again.
    """
    document = _approved_document(BRIDGE, plan, expected_hash)
    planned = TransferPlan.model_validate(document)
    config = AllocatorConfig()
    signer = signer_from_config(config)
    with OneTxClient(config) as client:
        report = apply_transfer_plan(
            client,
            signer,
            planned,
            config=config,
            idempotency_store=idempotency_store(
                config,
                bridge_scope(
                    planned.account,
                    planned.from_chain_id,
                    planned.to_chain_id,
                    planned.amount_usdc,
                    planned.ref,
                ),
            ),
        )
    return model_payload(report)


def review_bridge(plan: TransferPlan | Mapping[str, Any]) -> JsonObject:
    """What a person approving a stored `bridge` plan reads, from the plan alone."""
    planned = TransferPlan.model_validate(
        plan.model_dump(mode="json") if isinstance(plan, TransferPlan) else plan
    )
    bundles = planned.plan.bundles
    # A burn spends the chain's USDC, which 1Tx returns without a symbol; the
    # plan resolved it, so name it.
    usdc = {
        (chain_id, token.address.lower()): BundleToken(
            address=token.address, symbol="USDC", decimals=token.decimals
        )
        for chain_id, token in planned.deposit_tokens.items()
    }
    tokens = {**_tokens(bundles), **usdc}

    def reviewed(bundle: TxBundle) -> JsonObject:
        named = usdc.get((bundle.chain_id, bundle.token_in.address.lower()))
        if named is not None:
            bundle = bundle.model_copy(update={"token_in": named})
        return _bundle(bundle, planned.plan.steps)

    preparation = planned.preparation
    existing = planned.existing
    return {
        "kind": BRIDGE,
        "account": planned.account,
        "from_chain_id": planned.from_chain_id,
        "to_chain_id": planned.to_chain_id,
        "amount_usdc": planned.amount_usdc,
        "ref": planned.ref,
        # The transfer under way this plan advances; None for a new burn.
        "advances": None if existing is None else existing.model_dump(mode="json"),
        "bundles": [reviewed(bundle) for bundle in bundles],
        "funding": [
            _funding(item, tokens.get((item.chain_id, item.token.lower())))
            for item in preparation.funding
        ],
        "notes": [*planned.messages, *preparation.messages],
        "blockers": list(preparation.blockers),
        "transactions": len(planned.plan.steps),
    }


def review_plan(kind: str, plan: Mapping[str, Any]) -> JsonObject:
    """The review of a stored plan of any kind."""
    review = _REVIEW.get(kind)
    if review is None:
        raise ServiceError("invalid_input", f"no plan kind {kind}")
    return review(plan)


# What applies each kind of stored plan, re-checks its policy first, and
# describes it to the person approving it. A withdrawal or a loop close has no
# re-check: the policy bounds what is entered, and an exit is never refused for
# it. A rebalance or a loop open enters positions, so it is re-checked. A
# transfer touches no instrument, so there is nothing to check it against.
_APPLY: dict[str, Callable[..., JsonObject]] = {
    EXECUTE: apply_execute,
    WITHDRAW: apply_withdraw,
    REBALANCE: apply_rebalance,
    LOOP_OPEN: apply_loop_open,
    LOOP_CLOSE: apply_loop_close,
    BRIDGE: apply_bridge,
}
_RECHECK: dict[str, Callable[..., JsonObject]] = {
    EXECUTE: recheck_execute_policy,
    REBALANCE: recheck_rebalance_policy,
    LOOP_OPEN: recheck_loop_open_policy,
}
_REVIEW: dict[str, Callable[..., JsonObject]] = {
    EXECUTE: review_execute,
    WITHDRAW: review_withdraw,
    REBALANCE: review_rebalance,
    LOOP_OPEN: review_loop_open,
    LOOP_CLOSE: review_loop_close,
    BRIDGE: review_bridge,
}


def propose(store: PlanStore, proposal: Mapping[str, Any]) -> JsonObject:
    """Store a `plan_*` proposal for approval; what an execution tool returns.

    The model sees the plan and its hash, never a way to apply it.
    """
    stored = store.put(proposal["kind"], proposal["plan"])
    if stored.plan_hash != proposal["plan_hash"]:
        raise ServiceError("plan_mismatch", "the stored plan hashes differently")
    return {
        "plan_required": True,
        "kind": stored.kind,
        "plan_hash": stored.plan_hash,
        "expires_at": stored.expires_at.isoformat(),
        "plan": proposal["report"],
    }


def apply_approved(
    store: PlanStore,
    approved_hash: str,
    *,
    policy: Policy | Path | None = None,
) -> JsonObject:
    """Apply the stored plan a human approved by hash, at most once.

    The plan is marked used before anything else, so a repeated approval cannot
    send it twice; a refused or failed run needs a new plan. With `policy`, a
    kind that has a re-check is checked against it on today's shelf before it is
    applied, and refused on a violation.
    """
    stored = store.take(approved_hash)
    apply = _APPLY.get(stored.kind)
    if apply is None:
        raise ServiceError("invalid_input", f"no plan kind {stored.kind}")
    if plan_hash(stored.kind, stored.plan) != approved_hash:
        raise ServiceError(
            "plan_mismatch", "the stored plan does not match the approved hash"
        )
    recheck = _RECHECK.get(stored.kind)
    if policy is not None and recheck is not None:
        recheck(stored.plan, policy=policy)
    return apply(stored.plan, expected_hash=approved_hash)


def reject(store: PlanStore, rejected_hash: str) -> None:
    """Retire a stored plan without applying it. Same refusals as an approval."""
    store.take(rejected_hash)
