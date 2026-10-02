from __future__ import annotations

import time
from collections.abc import Iterable, Mapping, Sequence
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import Literal

from pydantic import Field

from open_allocator.core import amounts
from open_allocator.core import policy as policy_core
from open_allocator.core import rebalance as rebalance_core
from open_allocator.core.state import backend_from_config
from open_allocator.core.types import (
    Allocation,
    FrozenModel,
    Policy,
    TxBundle,
    TxPlan,
    Vault,
)
from open_allocator.exec import bundle_execution, calldata, deposit_sizing
from open_allocator.exec.execute import (
    ExecutionStepReport,
    GasCheck,
    TransactionPlanError,
    WalletPreparation,
    _amount_usdc,
    _config_value,
    _leg_key,
    _store_completed,
    _vaults_by_id,
    _write_checkpoint,
)
from open_allocator.exec.funding import FundingRequirement
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
    preparations: tuple[WalletPreparation, ...] = Field(default_factory=tuple)
    funding: tuple[FundingRequirement, ...] = Field(default_factory=tuple)
    in_progress: bool = False
    messages: tuple[str, ...] = Field(default_factory=tuple)


def _refuse_levered_trades(
    positions: object,
    trades: Iterable[object],
    known: Sequence[Vault | Mapping[str, object]],
) -> None:
    """Rebalance trades plain deposits and withdrawals, never a loop.

    A loop is opened by ``execute`` as one bundle to a chosen leverage and
    unwound by a loop close; a rebalance sell of its collateral would fail the
    pool's health check, and a buy would deposit unlevered into a levered id.
    A levered position the rebalance leaves alone is no obstacle.
    """
    levered = {
        vault.instrument_id
        for vault in (
            item if isinstance(item, Vault) else Vault.model_validate(item)
            for item in known
        )
        if vault.is_levered
    }
    levered.update(
        str(getattr(holding, "instrument_id", ""))
        for holding in getattr(positions, "holdings", ()) or ()
        if getattr(holding, "levered", None) is not None
    )
    touched = sorted(
        {str(getattr(trade, "instrument_id", "")) for trade in trades} & levered
    )
    if touched:
        raise TransactionPlanError(
            f"levered positions {', '.join(touched)} are not traded by rebalance: "
            "a loop is opened by `execute` and unwound by a loop close"
        )


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
    _refuse_levered_trades(positions, rebalance_plan.trades, known)
    should_execute = confirm or autonomous
    if autonomous and not confirm:
        _require_autonomous_rebalance(rebalance_plan, policy)

    return _calldata_rebalance(
        client,
        signer,
        positions,
        rebalance_plan,
        known,
        config,
        idempotency_store,
        execute=should_execute,
    )


def _calldata_rebalance(
    client: object,
    signer: Signer,
    positions: object,
    rebalance_plan: rebalance_core.RebalancePlan,
    known: Sequence[Vault | Mapping[str, object]],
    config: object | None,
    idempotency_store: object | None,
    *,
    execute: bool,
) -> RebalanceExecutionReport:
    """A same-chain rebalance: each chain's withdrawals, then its deposits.

    Each chain's calls go in one atomic operation. Deposits are logged with the
    shares read from positions afterwards, not the simulated amounts.
    """
    address = signer.address()
    built = _calldata_rebalance_plan(
        client,
        signer,
        address,
        positions,
        rebalance_plan,
        known,
        config,
        idempotency_store,
    )
    if not execute:
        return RebalanceExecutionReport(
            status="planned",
            rebalance_plan=rebalance_plan,
            policy_result=rebalance_plan.policy_result,
            plan=built.plan,
            preparations=built.preparation.preparations,
            funding=built.preparation.funding,
            messages=(
                "dry-run only; no transactions broadcast",
                *built.messages,
                *built.preparation.messages,
                *built.preparation.blockers,
            ),
        )

    attribution_required = (
        backend_from_config(config, needs="allocation_log_path") is not None
    )
    share_baselines = _share_balances(positions)
    unobserved: list[str] = []

    def log(bundle: TxBundle, receipt: Receipt) -> bundle_execution.BundleLog:
        trade = rebalance_plan.trades[bundle.leg_index]
        if bundle.action == "withdraw":
            return bundle_execution.BundleLog(
                action_type="sell",
                usd=float(trade.usd),
                shares=trade.yield_token_amount,
            )
        shares = None
        if attribution_required:
            if receipt.status == 1 and not receipt.pending:
                shares = _settled_buy_delta(
                    client,
                    address,
                    positions,
                    bundle.instrument_id,
                    bundle.chain_id,
                    config,
                    share_baselines,
                )
            if shares is None:
                unobserved.append(
                    f"buy cost basis is not yet observable: {bundle.instrument_id}; "
                    "the allocation log records the USDC it spent without shares"
                )
        return bundle_execution.BundleLog(
            action_type="buy",
            usd=built.deposit_usd.get(bundle.leg_index),
            shares=shares,
        )

    result = bundle_execution.execute_plan(
        client,
        signer,
        built.plan,
        stage="rebalance",
        policy_result=rebalance_plan.policy_result,
        completion_key=lambda bundle: _leg_key(bundle.leg_index, bundle.instrument_id),
        log=log,
        config=config,
        idempotency_store=idempotency_store,
    )
    # Trades finished by an earlier run were never planned, but they are still
    # complete; the checkpoint is a snapshot of the whole rebalance.
    earlier = tuple(
        key
        for index, trade in enumerate(rebalance_plan.trades)
        if (key := _leg_key(index, trade.instrument_id)) not in result.completed_keys
        and _store_completed(idempotency_store, key)
    )
    report = RebalanceExecutionReport(
        status=result.status,
        rebalance_plan=rebalance_plan,
        policy_result=rebalance_plan.policy_result,
        plan=result.plan,
        steps=result.steps,
        receipts=result.receipts,
        gas_checks=result.gas_checks,
        preparations=result.preparations,
        funding=result.funding,
        in_progress=result.in_progress,
        messages=(*built.messages, *result.messages, *unobserved),
    )
    _write_checkpoint(
        config,
        "rebalance",
        report,
        completed_keys=(*earlier, *result.completed_keys),
    )
    return report


def _calldata_rebalance_plan(
    client: object,
    signer: Signer,
    address: str,
    positions: object,
    plan: rebalance_core.RebalancePlan,
    known: Sequence[Vault | Mapping[str, object]],
    config: object | None,
    idempotency_store: object | None,
) -> deposit_sizing.FittedPlan:
    """Withdrawals first, then deposits sized to what each chain will hold.

    A chain's deposits are funded by its USDC balance plus the conservative
    proceeds of its withdrawals, and are sized down only for proceeds rounding
    and the paymaster charge. Buys that need another chain's sells are refused;
    otherwise an underfunded chain is left at full size for the funding check.
    """
    calldata.ensure_calldata_supported(config)
    vaults_by_id = _vaults_by_id(known)
    open_trades = [
        (index, trade)
        for index, trade in enumerate(plan.trades)
        if not _store_completed(idempotency_store, _leg_key(index, trade.instrument_id))
    ]
    sells: dict[int, list[bundle_execution.PlannedBundle]] = {}
    for index, trade in open_trades:
        if trade.action != "sell":
            continue
        chain_id = _trade_chain(trade, positions, vaults_by_id)
        if chain_id is None:
            raise TransactionPlanError(
                f"the chain of position {trade.instrument_id} is unknown"
            )
        if trade.calldata_amount is None:
            raise calldata.CalldataAmountError(
                f"cannot build a partial withdrawal of {trade.instrument_id}: no "
                "positive raw underlying amount could be derived from the position "
                "(missing balance_raw or decimals, or an amount that rounds down to "
                "zero units), and a share amount must never be sent as the calldata "
                "amount"
            )
        steps, bundle = calldata.request_bundle(
            client,
            instrument_id=trade.instrument_id,
            action="withdraw",
            account=address,
            chain_id=chain_id,
            amount=trade.calldata_amount,
            leg_index=index,
            first_step_index=0,
            config=config,
        )
        sells.setdefault(chain_id, []).append(
            bundle_execution.PlannedBundle(bundle=bundle, steps=steps)
        )

    deposits: list[deposit_sizing.DepositRequest] = []
    for index, trade in open_trades:
        if trade.action != "buy":
            continue
        vault = vaults_by_id.get(trade.instrument_id)
        if vault is None:
            raise TransactionPlanError(
                f"instrument {trade.instrument_id} is not in the discovered universe; "
                "its chain and deposit token are unknown"
            )
        token = calldata.deposit_token(vault.chain_id, vaults_by_id.values(), config)
        deposits.append(
            deposit_sizing.DepositRequest(
                index=index,
                instrument_id=trade.instrument_id,
                chain_id=vault.chain_id,
                token=token,
                wanted_raw=amounts.to_raw_units(
                    trade.usd, token.decimals, name="buy amount"
                ),
            )
        )

    def summary(ordered: Sequence[bundle_execution.PlannedBundle]) -> str:
        withdrawals = sum(len(items) for items in sells.values())
        return (
            f"Same-chain rebalance from calldata bundles: {withdrawals} withdrawals "
            f"before {len(ordered) - withdrawals} deposits across "
            f"{sum(len(item.steps) for item in ordered)} transaction steps"
        )

    return deposit_sizing.fit(
        client,
        signer,
        address,
        deposits=deposits,
        withdrawals=sells,
        withdrawal_usd={index: float(trade.usd) for index, trade in open_trades},
        summary=summary,
        config=config,
        idempotency_store=idempotency_store,
    )


# 1Tx holds back roughly this much USDC per chain to sponsor gas. Measured on
# 2026-08-16: a $13.00 leg was accepted and $13.05 rejected against an idle
# balance of $13.788208. Overridable per deployment via config.
_SETTLE_SECONDS = 4.0


def _settle(config: object | None) -> None:
    """Give the venue's balance view time to catch up with a broadcast sell.

    Injectable so tests do not sleep. A no-op by default would race; a long wait
    would stall the daily job, so the default is short and the loop simply tries
    again on the next round.
    """
    waiter = getattr(config, "settle_waiter", None)
    if callable(waiter):
        waiter()
        return
    time.sleep(_SETTLE_SECONDS)


def _settled_buy_delta(
    client: object,
    address: str,
    initial_positions: object,
    instrument_id: str,
    chain_id: int,
    config: object | None,
    share_baselines: dict[str, Decimal],
) -> str | None:
    """Return the exact newly settled shares for one confirmed buy."""
    read = getattr(client, "positions", None)
    if not callable(read):
        return None
    baseline = share_baselines.get(
        instrument_id,
        _share_balance(initial_positions, instrument_id) or Decimal("0"),
    )
    attempts_value = _config_value(config, "position_settlement_attempts")
    try:
        attempts = max(1, int(attempts_value or 1))
    except (TypeError, ValueError):
        attempts = 1
    for attempt in range(attempts):
        response = read({"address": address, "chainId": chain_id})
        observed = _share_balance(response, instrument_id)
        if observed is not None:
            delta = observed - baseline
            if delta > 0:
                share_baselines[instrument_id] = observed
                return format(delta, "f")
            if delta < 0:
                return None
        if attempt + 1 < attempts:
            _settle(config)
    return None


def _share_balances(value: object) -> dict[str, Decimal]:
    balances: dict[str, Decimal] = {}
    holdings = getattr(value, "holdings", ()) or ()
    for holding in holdings:
        instrument_id = getattr(holding, "instrument_id", None)
        balance = getattr(holding, "share_balance", None)
        if instrument_id is not None and balance is not None:
            balances[str(instrument_id)] = balances.get(
                str(instrument_id), Decimal("0")
            ) + Decimal(str(balance))
    return balances


def _share_balance(value: object, instrument_id: str) -> Decimal | None:
    holdings = getattr(value, "holdings", None)
    if holdings is None and isinstance(value, Mapping):
        holdings = value.get("positions", value.get("holdings", ()))
    for holding in holdings or ():
        if isinstance(holding, Mapping):
            held_id = holding.get("instrumentId", holding.get("instrument_id"))
            balance = holding.get("shareBalance", holding.get("share_balance"))
        else:
            held_id = getattr(holding, "instrument_id", None)
            balance = getattr(holding, "share_balance", None)
        if str(held_id) == instrument_id and balance is not None:
            try:
                return Decimal(str(balance))
            except InvalidOperation:
                return None
    return None


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


def _trade_chain(
    trade: rebalance_core.RebalanceTrade,
    positions: object,
    vaults_by_id: Mapping[str, Vault],
) -> int | None:
    """The chain a sold position sat on — where its USDC proceeds appear.

    Prefer the position itself over the shelf: the book is the record of what is
    actually held, and a vault missing from `known_instruments` would otherwise
    silently drop the credit and put the planner back where it started.
    """
    for holding in getattr(positions, "holdings", ()) or ():
        if getattr(holding, "instrument_id", None) == trade.instrument_id:
            chain = getattr(holding, "chain_id", None)
            if isinstance(chain, int) and not isinstance(chain, bool):
                return chain
    vault = vaults_by_id.get(trade.instrument_id)
    return vault.chain_id if vault is not None else None


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
