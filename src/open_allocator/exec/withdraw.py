from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal

from pydantic import Field

from open_allocator.core import amounts
from open_allocator.core import withdraw as withdraw_core
from open_allocator.core.positions import PositionHolding
from open_allocator.core.types import FrozenModel, Policy, TxBundle, TxPlan
from open_allocator.exec import bundle_execution, calldata, chains
from open_allocator.exec.bundle_execution import PlanPreparation
from open_allocator.exec.execute import (
    ExecutionStepReport,
    GasCheck,
    TransactionPlanError,
    WalletPreparation,
    _store_completed,
    _write_checkpoint,
)
from open_allocator.exec.funding import FundingRequirement
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
    preparations: tuple[WalletPreparation, ...] = Field(default_factory=tuple)
    funding: tuple[FundingRequirement, ...] = Field(default_factory=tuple)
    in_progress: bool = False
    messages: tuple[str, ...] = Field(default_factory=tuple)


class WithdrawalPlan(FrozenModel):
    """A withdrawal, complete enough to execute as it stands.

    What a dry run shows and what a confirmation executes: applying it submits
    this bundle and never plans again. ``position`` and ``amount`` are what it
    was planned from, kept so the caller can bind the same idempotency scope.
    """

    kind: Literal["withdraw"] = "withdraw"
    account: str
    position: PositionHolding
    amount: float | str | None = None
    withdraw_plan: withdraw_core.WithdrawPlan
    sell: WithdrawSellDetails
    plan: TxPlan
    preparation: PlanPreparation


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
    planned = plan_withdrawal(
        client,
        signer,
        position,
        policy,
        amount,
        config=config,
        idempotency_store=idempotency_store,
    )
    if not confirm:
        return dry_run_report(planned)
    return apply_withdrawal_plan(
        client,
        signer,
        planned,
        config=config,
        idempotency_store=idempotency_store,
    )


def plan_withdrawal(
    client: object,
    signer: Signer,
    position: object,
    policy: Policy | Mapping[str, object],
    amount: float | str | None = None,
    *,
    config: object | None = None,
    idempotency_store: object | None = None,
) -> WithdrawalPlan:
    """The withdrawal ``withdraw`` would submit for ``position``, not sent."""
    _refuse_levered(position)
    holding = (
        position
        if isinstance(position, PositionHolding)
        else PositionHolding.model_validate(position)
    )
    withdraw_plan = withdraw_core.plan_withdraw(holding, policy, amount=amount)
    address = signer.address()
    tx_plan, sell = _calldata_tx_plan(
        client,
        address,
        withdraw_plan,
        config,
        idempotency_store,
    )
    return WithdrawalPlan(
        account=address,
        position=holding,
        amount=amount,
        withdraw_plan=withdraw_plan,
        sell=sell,
        plan=tx_plan,
        preparation=bundle_execution.prepare_plan(
            signer, tx_plan, config, idempotency_store
        ),
    )


def dry_run_report(planned: WithdrawalPlan) -> WithdrawExecutionReport:
    """The ``withdraw`` dry-run report: the plan, its preparation and blockers."""
    preparation = planned.preparation
    return WithdrawExecutionReport(
        status="planned",
        withdraw_plan=planned.withdraw_plan,
        sell=planned.sell,
        plan=planned.plan,
        preparations=preparation.preparations,
        funding=preparation.funding,
        messages=(
            "dry-run only; no transactions broadcast",
            *preparation.messages,
            *preparation.blockers,
        ),
    )


def apply_withdrawal_plan(
    client: object,
    signer: Signer,
    planned: WithdrawalPlan,
    *,
    config: object | None = None,
    idempotency_store: object | None = None,
) -> WithdrawExecutionReport:
    """Execute exactly ``planned``, refusing it when it no longer describes the
    account: another signer, or the withdrawal submitted since it was built.
    """
    address = signer.address()
    if address.casefold() != planned.account.casefold():
        raise TransactionPlanError(
            f"the plan was built for {planned.account}, but the signer is {address}"
        )
    # Planning skips a withdrawal already sent; one sent since means a resend.
    if planned.plan.bundles and _store_completed(
        idempotency_store, _withdraw_key(planned.withdraw_plan)
    ):
        instrument_id = planned.withdraw_plan.instrument_id
        raise TransactionPlanError(
            f"the plan is stale: the withdrawal of {instrument_id} was sent after "
            "it was built; plan again"
        )
    return _execute_calldata_withdraw(
        client,
        signer,
        planned.plan,
        planned.withdraw_plan,
        planned.sell,
        config,
        idempotency_store,
    )


def _refuse_levered(position: object) -> None:
    """A loop is not withdrawn: its collateral is pledged against its debt."""
    levered = (
        position.get("levered")
        if isinstance(position, Mapping)
        else getattr(position, "levered", None)
    )
    if levered is not None:
        instrument_id = (
            position.get("instrument_id")
            if isinstance(position, Mapping)
            else getattr(position, "instrument_id", None)
        )
        raise TransactionPlanError(
            f"{instrument_id} is a levered loop; withdrawing its collateral would "
            "fail the pool's health check. It is unwound by a loop close, which "
            "this command does not build"
        )


def _calldata_tx_plan(
    client: object,
    address: str,
    withdraw_plan: withdraw_core.WithdrawPlan,
    config: object | None,
    idempotency_store: object | None,
) -> tuple[TxPlan, WithdrawSellDetails]:
    """A withdrawal planned from one calldata bundle on the position's chain."""
    sell = _sell_details(address, withdraw_plan, None)
    if _store_completed(idempotency_store, _withdraw_key(withdraw_plan)):
        tx_plan = TxPlan(
            steps=(),
            summary=f"Withdraw already completed for {withdraw_plan.instrument_id}",
        )
        return tx_plan, sell

    calldata.ensure_calldata_supported(config)
    steps, bundle = calldata.request_bundle(
        client,
        instrument_id=withdraw_plan.instrument_id,
        action="withdraw",
        account=address,
        chain_id=withdraw_plan.chain_id,
        amount=withdraw_core.calldata_withdraw_amount(withdraw_plan),
        leg_index=0,
        first_step_index=0,
        config=config,
    )
    tx_plan = TxPlan(
        steps=steps,
        summary=(
            f"Build calldata withdraw bundle for {withdraw_plan.instrument_id} "
            f"across {len(steps)} transaction steps"
        ),
        bundles=(bundle,),
    )
    return tx_plan, sell.model_copy(
        update={"expected_usdc": _expected_usdc(bundle, config)}
    )


def _execute_calldata_withdraw(
    client: object,
    signer: Signer,
    tx_plan: TxPlan,
    withdraw_plan: withdraw_core.WithdrawPlan,
    sell: WithdrawSellDetails,
    config: object | None,
    idempotency_store: object | None,
) -> WithdrawExecutionReport:
    """Submit the withdrawal bundle as one wallet operation."""
    withdraw_key = _withdraw_key(withdraw_plan)
    result = bundle_execution.execute_plan(
        client,
        signer,
        tx_plan,
        stage="withdraw",
        policy_result=_ok_policy_result(),  # type: ignore[arg-type]
        completion_key=lambda _bundle: withdraw_key,
        log=lambda _bundle, _receipt: bundle_execution.BundleLog(
            action_type="withdraw",
            shares=withdraw_plan.yield_token_amount,
            share_price=withdraw_plan.share_price_usd,
        ),
        config=config,
        idempotency_store=idempotency_store,
    )
    if result.plan.bundles:
        # The bundle may have been rebuilt before signing; report what was sent.
        sell = sell.model_copy(
            update={
                "status": "sent",
                "expected_usdc": _expected_usdc(result.plan.bundles[0], config),
            }
        )
    completed_keys = result.completed_keys
    if not result.plan.bundles and _store_completed(idempotency_store, withdraw_key):
        completed_keys = (withdraw_key,)
    report = WithdrawExecutionReport(
        status=result.status,
        withdraw_plan=withdraw_plan,
        sell=sell,
        plan=result.plan,
        steps=result.steps,
        receipts=result.receipts,
        gas_checks=result.gas_checks,
        preparations=result.preparations,
        funding=result.funding,
        in_progress=result.in_progress,
        messages=result.messages,
    )
    _write_checkpoint(config, "withdraw", report, completed_keys=completed_keys)
    return report


def _expected_usdc(bundle: TxBundle, config: object | None) -> str | None:
    """The bundle's expected output in USDC, when that is what it pays out in."""
    usdc = chains.usdc_address(bundle.chain_id, config)
    if (
        bundle.expected_out is None
        or usdc is None
        or bundle.token_out.address.casefold() != usdc.casefold()
    ):
        return None
    return amounts.from_raw_units(bundle.expected_out, bundle.token_out.decimals)


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


def _withdraw_key(plan: withdraw_core.WithdrawPlan) -> str:
    return f"withdraw:0:{plan.instrument_id}:{plan.yield_token_amount}"


def _ok_policy_result() -> object:
    from open_allocator.core.policy import PolicyResult

    return PolicyResult(ok=True, violations=())


__all__ = [
    "WithdrawExecutionReport",
    "WithdrawSellDetails",
    "WithdrawalPlan",
    "apply_withdrawal_plan",
    "dry_run_report",
    "plan_withdrawal",
    "withdraw",
]
