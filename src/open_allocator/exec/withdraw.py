from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal

from pydantic import Field

from open_allocator.core import withdraw as withdraw_core
from open_allocator.core.types import FrozenModel, Policy, TxPlan, TxStep
from open_allocator.exec.execute import (
    ExecutionBroadcastError,
    ExecutionReport,
    ExecutionStepReport,
    GasCheck,
    _append_allocation_log,
    _completed_keys,
    _copy_config_value,
    _is_in_progress_payload,
    _messages,
    _preflight,
    _raw_transactions,
    _store_completed,
    _store_mark_completed,
    _tx_step,
    _write_checkpoint,
)
from open_allocator.exec.signer import Receipt, Signer


class WithdrawSellDetails(FrozenModel):
    status: Literal["planned", "sent"]
    user_address: str
    instrument_id: str
    yield_token_amount: str
    requested_usd: float | None = Field(default=None, ge=0)
    full_exit: bool
    expected_usdc: str | None = None
    realized_usdc: str | None = None


class WithdrawExecutionReport(FrozenModel):
    status: Literal["planned", "success", "in_progress", "failed"]
    withdraw_plan: withdraw_core.WithdrawPlan
    sell: WithdrawSellDetails
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


def withdraw(
    client: object,
    signer: Signer,
    position: object,
    policy: Policy | Mapping[str, object],
    amount: float | str | None = None,
    *,
    confirm: bool = False,
    config: object | None = None,
    idempotency_store: object | None = None,
) -> WithdrawExecutionReport:
    withdraw_plan = withdraw_core.plan_withdraw(position, policy, amount=amount)
    address = signer.address()
    tx_plan, step_refs, sell, messages = _build_tx_plan(
        client,
        address,
        withdraw_plan,
        config,
        idempotency_store,
    )
    in_progress = bool(messages)
    if not confirm:
        return WithdrawExecutionReport(
            status="planned",
            withdraw_plan=withdraw_plan,
            sell=sell,
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
            _mark_withdraw_if_complete(ref, step_refs, idempotency_store)
            continue

        try:
            receipt = signer.send(ref.step, rpc_urls[ref.step.chain_id])
        except Exception as error:
            partial_report = ExecutionReport(
                status="failed",
                policy_result=_ok_policy_result(),
                plan=tx_plan,
                steps=tuple(execution_steps),
                receipts=tuple(receipts),
                gas_checks=gas_checks,
                in_progress=False,
                messages=messages,
            )
            _write_checkpoint(
                config,
                "withdraw",
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
        _mark_withdraw_if_complete(ref, step_refs, idempotency_store)
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

    report = WithdrawExecutionReport(
        status="in_progress" if in_progress else "success",
        withdraw_plan=withdraw_plan,
        sell=sell.model_copy(update={"status": "sent"}),
        plan=tx_plan,
        steps=tuple(execution_steps),
        receipts=tuple(receipts),
        gas_checks=gas_checks,
        in_progress=in_progress,
        messages=messages,
    )
    _write_checkpoint(
        config,
        "withdraw",
        report,
        completed_keys=_completed_keys(step_refs, execution_steps),
    )
    return report


def _build_tx_plan(
    client: object,
    address: str,
    withdraw_plan: withdraw_core.WithdrawPlan,
    config: object | None,
    idempotency_store: object | None,
) -> tuple[TxPlan, tuple[_StepRef, ...], WithdrawSellDetails, tuple[str, ...]]:
    leg_key = _withdraw_key(withdraw_plan)
    if _store_completed(idempotency_store, leg_key):
        tx_plan = TxPlan(
            steps=(),
            summary=f"Withdraw already completed for {withdraw_plan.instrument_id}",
        )
        return tx_plan, (), _sell_details(address, withdraw_plan, None), ()

    response = _build_sell(
        client,
        _sell_body(address, withdraw_plan, config),
    )
    raw_steps = _raw_transactions(response)
    plan_steps: list[TxStep] = []
    step_refs: list[_StepRef] = []
    for step_index, raw_step in enumerate(raw_steps):
        step = _withdraw_tx_step(raw_step, step_index, len(raw_steps))
        step_key = f"{leg_key}:step:{step_index}"
        plan_steps.append(step)
        step_refs.append(
            _StepRef(
                leg_index=0,
                step_index=step_index,
                instrument_id=withdraw_plan.instrument_id,
                step=step,
                idempotency_key=step_key,
                shares=withdraw_plan.yield_token_amount,
                action_type="withdraw",
            )
        )

    tx_plan = TxPlan(
        steps=tuple(plan_steps),
        summary=(
            f"Build withdraw sell transaction for {withdraw_plan.instrument_id} "
            f"across {len(plan_steps)} transaction steps"
        ),
    )
    messages = _withdraw_messages(response)
    return (
        tx_plan,
        tuple(step_refs),
        _sell_details(address, withdraw_plan, response),
        messages,
    )


def _build_sell(client: object, body: Mapping[str, object]) -> object:
    build_sell = getattr(client, "build_sell", None)
    if not callable(build_sell):
        raise TypeError("client does not implement build_sell")
    return build_sell(body)


def _sell_body(
    address: str,
    plan: withdraw_core.WithdrawPlan,
    config: object | None,
) -> dict[str, object]:
    body: dict[str, object] = {
        "userAddress": address,
        "instrumentId": plan.instrument_id,
        "yieldTokenAmount": plan.yield_token_amount,
    }
    _copy_config_value(body, "slippageBps", config, "slippage_bps")
    return body


def _withdraw_tx_step(
    raw_step: Mapping[str, object],
    step_index: int,
    step_count: int,
) -> TxStep:
    step = _tx_step(raw_step, step_index, step_count)
    if step.kind == "buy":
        return step.model_copy(update={"kind": "sell"})
    return step


def _sell_details(
    address: str,
    plan: withdraw_core.WithdrawPlan,
    response: object | None,
) -> WithdrawSellDetails:
    return WithdrawSellDetails(
        status="planned",
        user_address=address,
        instrument_id=plan.instrument_id,
        yield_token_amount=plan.yield_token_amount,
        requested_usd=plan.requested_usd,
        full_exit=plan.full_exit,
        expected_usdc=_payload_amount(
            response,
            {
                "expectedUsdc",
                "expectedUsdcAmount",
                "expectedAmountUsdc",
                "expectedUsdcOut",
                "estimatedUsdc",
                "amountUsdc",
                "outputAmountUsdc",
                "usdcAmount",
                "minUsdcOut",
            },
        ),
        realized_usdc=_payload_amount(
            response,
            {
                "realizedUsdc",
                "receivedUsdc",
                "amountOutUsdc",
                "usdcReceived",
            },
        ),
    )


def _payload_amount(value: object, names: set[str]) -> str | None:
    for key, item in _walk_mapping_values(value):
        if key in names and item is not None:
            return str(item)
    return None


def _walk_mapping_values(value: object) -> Sequence[tuple[str, object]]:
    pairs: list[tuple[str, object]] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            pairs.append((str(key), item))
            pairs.extend(_walk_mapping_values(item))
    elif isinstance(value, Sequence) and not isinstance(
        value,
        str | bytes | bytearray,
    ):
        for item in value:
            pairs.extend(_walk_mapping_values(item))
    return tuple(pairs)


def _withdraw_messages(response: object) -> tuple[str, ...]:
    messages = _messages((response,))
    if _is_in_progress_payload(response):
        return tuple(
            "cross-chain withdraw is in progress"
            if message == "cross-chain buy is in progress"
            else message
            for message in messages
        )
    return messages


def _mark_withdraw_if_complete(
    ref: _StepRef,
    step_refs: Sequence[_StepRef],
    store: object | None,
) -> None:
    if step_refs and all(
        _store_completed(store, item.idempotency_key) for item in step_refs
    ):
        _store_mark_completed(store, _withdraw_key_from_ref(ref), True)


def _withdraw_key(plan: withdraw_core.WithdrawPlan) -> str:
    return f"withdraw:0:{plan.instrument_id}:{plan.yield_token_amount}"


def _withdraw_key_from_ref(ref: _StepRef) -> str:
    prefix = ref.idempotency_key.rsplit(":step:", 1)[0]
    return prefix


def _ok_policy_result() -> object:
    from open_allocator.core.policy import PolicyResult

    return PolicyResult(ok=True, violations=())


__all__ = [
    "WithdrawExecutionReport",
    "WithdrawSellDetails",
    "withdraw",
]
