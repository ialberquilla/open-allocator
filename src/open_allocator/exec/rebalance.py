from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import Literal

from pydantic import Field

from open_allocator.core import policy as policy_core
from open_allocator.core import rebalance as rebalance_core
from open_allocator.core.types import (
    Allocation,
    AllocationLeg,
    FrozenModel,
    Policy,
    TxPlan,
    TxStep,
    Vault,
)
from open_allocator.exec.execute import (
    ExecutionBroadcastError,
    ExecutionReport,
    ExecutionStepReport,
    GasCheck,
    _amount_usdc,
    _append_allocation_log,
    _build_buy,
    _buy_body,
    _completed_keys,
    _copy_config_value,
    _idle_usdc_by_chain,
    _is_in_progress_payload,
    _leg_key,
    _messages,
    _preflight,
    _raw_transactions,
    _store_completed,
    _store_mark_completed,
    _tx_step,
    _vaults_by_id,
    _write_checkpoint,
    pending_receipt_messages,
)
from open_allocator.exec.signer import Receipt, Signer


class RebalanceAuthorizationError(PermissionError):
    pass


class RebalanceExecutionReport(FrozenModel):
    status: Literal["planned", "success", "in_progress", "failed"]
    rebalance_plan: rebalance_core.RebalancePlan
    policy_result: policy_core.PolicyResult
    plan: TxPlan
    steps: tuple[ExecutionStepReport, ...] = Field(default_factory=tuple)
    receipts: tuple[Receipt, ...] = Field(default_factory=tuple)
    gas_checks: tuple[GasCheck, ...] = Field(default_factory=tuple)
    in_progress: bool = False
    messages: tuple[str, ...] = Field(default_factory=tuple)


class _StepRef(FrozenModel):
    leg_index: int
    step_index: int
    instrument_id: str
    step: TxStep
    idempotency_key: str
    usd: float | None = None
    shares: str | None = None
    action_type: str


def execute_rebalance(
    client: object,
    signer: Signer,
    positions: object,
    target: Allocation | Mapping[str, object],
    policy: Policy | Mapping[str, object],
    *,
    confirm: bool = False,
    autonomous: bool = False,
    known_instruments: Iterable[Vault | Mapping[str, object]] | None = None,
    config: object | None = None,
    idempotency_store: object | None = None,
    min_trade_usd: float = 1.0,
) -> RebalanceExecutionReport:
    known = tuple(known_instruments or ())
    rebalance_plan = rebalance_core.plan_rebalance(
        positions,
        target,
        policy,
        known_instruments=known,
        min_trade_usd=min_trade_usd,
    )
    should_execute = confirm or autonomous
    if autonomous and not confirm:
        _require_autonomous_rebalance(rebalance_plan, policy)

    address = signer.address()
    tx_plan, step_refs, messages = _build_tx_plan(
        client,
        address,
        positions,
        rebalance_plan,
        known,
        config,
        idempotency_store,
    )
    in_progress = bool(messages)
    if not should_execute:
        return RebalanceExecutionReport(
            status="planned",
            rebalance_plan=rebalance_plan,
            policy_result=rebalance_plan.policy_result,
            plan=tx_plan,
            messages=("dry-run only; no transactions broadcast", *messages),
            in_progress=in_progress,
        )

    rpc_urls, gas_checks = _preflight(
        address,
        step_refs,
        config,
        idempotency_store,
    )

    execution_steps: list[ExecutionStepReport] = []
    receipts: list[Receipt] = []
    for ref in step_refs:
        if _store_completed(idempotency_store, ref.idempotency_key):
            execution_steps.append(
                ExecutionStepReport(
                    leg_index=ref.leg_index,
                    step_index=ref.step_index,
                    instrument_id=ref.instrument_id,
                    status="skipped",
                    step=ref.step,
                    idempotency_key=ref.idempotency_key,
                )
            )
            _mark_leg_if_complete(ref, step_refs, idempotency_store)
            continue

        try:
            receipt = signer.send(ref.step, rpc_urls[ref.step.chain_id])
        except Exception as error:
            partial_report = ExecutionReport(
                status="failed",
                policy_result=rebalance_plan.policy_result,
                plan=tx_plan,
                steps=tuple(execution_steps),
                receipts=tuple(receipts),
                gas_checks=gas_checks,
                in_progress=False,
                messages=messages,
            )
            _write_checkpoint(
                config,
                "rebalance",
                partial_report,
                completed_keys=_completed_keys(step_refs, execution_steps),
            )
            raise ExecutionBroadcastError(
                "transaction broadcast failed",
                leg_index=ref.leg_index,
                step_index=ref.step_index,
                partial_report=partial_report,
            ) from error

        receipts.append(receipt)
        _store_mark_completed(idempotency_store, ref.idempotency_key, receipt)
        _append_allocation_log(config, ref, receipt)
        _mark_leg_if_complete(ref, step_refs, idempotency_store)
        execution_steps.append(
            ExecutionStepReport(
                leg_index=ref.leg_index,
                step_index=ref.step_index,
                instrument_id=ref.instrument_id,
                status="sent",
                step=ref.step,
                receipt=receipt,
                idempotency_key=ref.idempotency_key,
            )
        )

    unconfirmed = pending_receipt_messages(receipts)
    in_progress = in_progress or bool(unconfirmed)
    report = RebalanceExecutionReport(
        status="in_progress" if in_progress else "success",
        rebalance_plan=rebalance_plan,
        policy_result=rebalance_plan.policy_result,
        plan=tx_plan,
        steps=tuple(execution_steps),
        receipts=tuple(receipts),
        gas_checks=gas_checks,
        in_progress=in_progress,
        messages=(*messages, *unconfirmed),
    )
    _write_checkpoint(
        config,
        "rebalance",
        report,
        completed_keys=_completed_keys(step_refs, execution_steps),
    )
    return report


def _require_autonomous_rebalance(
    plan: rebalance_core.RebalancePlan,
    policy: Policy | Mapping[str, object],
) -> None:
    policy_model = (
        policy if isinstance(policy, Policy) else Policy.model_validate(policy)
    )
    if not policy_model.gates.autonomous_rebalance:
        raise RebalanceAuthorizationError(
            "autonomous rebalance requires policy.gates.autonomous_rebalance=true",
        )
    if plan.total_buy_usd > policy_model.gates.max_deploy_per_cycle_usd:
        raise RebalanceAuthorizationError(
            "autonomous rebalance exceeds policy.gates.max_deploy_per_cycle_usd",
        )


def _build_tx_plan(
    client: object,
    address: str,
    positions: object,
    plan: rebalance_core.RebalancePlan,
    known_instruments: Sequence[Vault | Mapping[str, object]],
    config: object | None,
    idempotency_store: object | None,
) -> tuple[TxPlan, tuple[_StepRef, ...], tuple[str, ...]]:
    vaults_by_id = _vaults_by_id(known_instruments)
    balances_by_chain = _idle_usdc_by_chain(client, address)
    buy_allocation = _buy_allocation(plan)
    buy_index_by_action = _buy_index_by_action(plan)
    plan_steps: list[TxStep] = []
    step_refs: list[_StepRef] = []
    build_payloads: list[object] = []

    for action_index, trade in enumerate(plan.trades):
        leg_key = _leg_key(action_index, trade.instrument_id)
        if _store_completed(idempotency_store, leg_key):
            continue

        if trade.action == "sell":
            response = _build_sell(
                client,
                _sell_body(address, positions, trade, config),
            )
        else:
            buy_index = buy_index_by_action[action_index]
            response = _build_buy(
                client,
                _buy_body(
                    address,
                    buy_allocation,
                    buy_index,
                    vaults_by_id,
                    config,
                    balances_by_chain,
                ),
            )

        build_payloads.append(response)
        raw_steps = _raw_transactions(response)
        for step_index, raw_step in enumerate(raw_steps):
            step = _rebalance_tx_step(
                raw_step,
                step_index,
                len(raw_steps),
                trade.action,
            )
            step_key = f"{leg_key}:step:{step_index}"
            plan_steps.append(step)
            step_refs.append(
                _StepRef(
                    leg_index=action_index,
                    step_index=step_index,
                    instrument_id=trade.instrument_id,
                    step=step,
                    idempotency_key=step_key,
                    usd=trade.usd,
                    shares=trade.yield_token_amount,
                    action_type=trade.action,
                )
            )

    tx_plan = TxPlan(
        steps=tuple(plan_steps),
        summary=(
            f"Build rebalance transactions for {len(plan.trades)} delta trades "
            f"across {len(plan_steps)} transaction steps"
        ),
    )
    messages = _messages(build_payloads)
    if any(_is_in_progress_payload(payload) for payload in build_payloads):
        messages = tuple(
            "cross-chain rebalance is in progress"
            if message == "cross-chain buy is in progress"
            else message
            for message in messages
        )
    return tx_plan, tuple(step_refs), messages


def _build_sell(client: object, body: Mapping[str, object]) -> object:
    build_sell = getattr(client, "build_sell", None)
    if not callable(build_sell):
        raise TypeError("client does not implement build_sell")
    return build_sell(body)


def _sell_body(
    address: str,
    positions: object,
    trade: rebalance_core.RebalanceTrade,
    config: object | None,
) -> dict[str, object]:
    amount = trade.yield_token_amount or _sell_share_amount(positions, trade)
    body: dict[str, object] = {
        "userAddress": address,
        "instrumentId": trade.instrument_id,
        "yieldTokenAmount": amount,
    }
    _copy_config_value(body, "slippageBps", config, "slippage_bps")
    return body


def _sell_share_amount(
    positions: object,
    trade: rebalance_core.RebalanceTrade,
) -> str:
    positions_model = rebalance_core._positions(positions)  # noqa: SLF001
    holdings = tuple(
        holding
        for holding in positions_model.holdings
        if holding.instrument_id == trade.instrument_id
    )
    if not holdings:
        raise ValueError(f"cannot sell missing position: {trade.instrument_id}")

    current_usd = sum(
        (
            _money_decimal(holding.usd_value, "holding.usd_value")
            for holding in holdings
        ),
        Decimal("0"),
    )
    total_shares = sum(
        (
            _money_decimal(holding.share_balance, "holding.share_balance")
            for holding in holdings
        ),
        Decimal("0"),
    )
    sell_usd = _money_decimal(trade.usd, "trade.usd")
    if sell_usd >= current_usd:
        return _amount_usdc(float(total_shares))

    share_decimals = max(holding.share_decimals for holding in holdings)
    quantum = Decimal(1).scaleb(-share_decimals)
    shares = (total_shares * sell_usd / current_usd).quantize(
        quantum,
        rounding=ROUND_DOWN,
    )
    return _amount_usdc(float(shares))


def _buy_allocation(plan: rebalance_core.RebalancePlan) -> Allocation:
    buy_trades = tuple(trade for trade in plan.trades if trade.action == "buy")
    total = sum((trade.usd for trade in buy_trades), 0.0)
    return Allocation(
        legs=tuple(
            AllocationLeg(
                instrument_id=trade.instrument_id,
                weight=trade.usd / total if total > 0 else 0,
                usd=trade.usd,
            )
            for trade in buy_trades
        ),
        total_usd=total,
        metadata=plan.target.metadata,
    )


def _buy_index_by_action(plan: rebalance_core.RebalancePlan) -> dict[int, int]:
    indexes: dict[int, int] = {}
    buy_index = 0
    for action_index, trade in enumerate(plan.trades):
        if trade.action == "buy":
            indexes[action_index] = buy_index
            buy_index += 1
    return indexes


def _rebalance_tx_step(
    raw_step: Mapping[str, object],
    step_index: int,
    step_count: int,
    action: Literal["sell", "buy"],
) -> TxStep:
    step = _tx_step(raw_step, step_index, step_count)
    if action == "sell" and step.kind == "buy":
        return step.model_copy(update={"kind": "sell"})
    return step


def _mark_leg_if_complete(
    ref: _StepRef,
    step_refs: Sequence[_StepRef],
    store: object | None,
) -> None:
    leg_refs = [item for item in step_refs if item.leg_index == ref.leg_index]
    if leg_refs and all(
        _store_completed(store, item.idempotency_key) for item in leg_refs
    ):
        _store_mark_completed(store, _leg_key(ref.leg_index, ref.instrument_id), True)


def _money_decimal(value: object, name: str) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"{name} must be a finite non-negative number") from error
    if not amount.is_finite() or amount < 0:
        raise ValueError(f"{name} must be a finite non-negative number")
    return amount


__all__ = [
    "RebalanceAuthorizationError",
    "RebalanceExecutionReport",
    "execute_rebalance",
]
