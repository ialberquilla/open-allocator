from __future__ import annotations

import time
from collections.abc import Iterable, Mapping, Sequence
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import Literal, NamedTuple

from pydantic import Field

from open_allocator.core import policy as policy_core
from open_allocator.core import rebalance as rebalance_core
from open_allocator.core.state import backend_from_config
from open_allocator.core.types import (
    Allocation,
    AllocationLeg,
    FrozenModel,
    Policy,
    TxPlan,
    TxStep,
    Vault,
)
from open_allocator.exec import calldata
from open_allocator.exec.execute import (
    ExecutionBroadcastError,
    ExecutionReport,
    ExecutionStepReport,
    FundingLedger,
    GasCheck,
    _amount_usdc,
    _append_allocation_log,
    _build_buy,
    _buy_body,
    _completed_keys,
    _config_value,
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
    # Only set where the venue quoted a price. A buy cannot know it: 1Tx's
    # build endpoint neither takes nor returns a share amount, and the receipt
    # carries no logs, so the price is unknown until the position is next read.
    share_price: str | None = None
    settlement_chain_id: int | None = None
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
    if calldata.uses_calldata_api(config):
        # Sell proceeds funding buys needs the calldata funding ledger and
        # sell-before-buy bundling; until then, refuse rather than fall back.
        raise calldata.CalldataUnsupportedError(
            "rebalance is not supported with ONE_TX_TRANSACTION_API=calldata yet; "
            "use ONE_TX_TRANSACTION_API=legacy"
        )
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
    sells = tuple(t for t in rebalance_plan.trades if t.action == "sell")
    buys = tuple(t for t in rebalance_plan.trades if t.action == "buy")
    # 🔑 STAGE WHEN A SELL PAYS FOR A BUY. 1Tx builds calldata against the USDC
    # the wallet holds RIGHT NOW, so a buy larger than any chain's current
    # balance cannot be built — however correct the plan is about what the sells
    # will free. Building everything up front and broadcasting afterwards is
    # therefore the thing that has to change, not the sizing: broadcast the
    # sells, let the money land, then build the buys against balances that
    # actually exist. See `_execute_staged`.
    staged = bool(sells) and bool(buys)
    if staged and not should_execute:
        # Preview. Show the whole thing when the buys can be quoted from what
        # the wallet already holds; when they cannot — 1Tx quotes against
        # balances that do not include the sells' proceeds — show the sells and
        # SAY what follows, rather than failing the command on a buy that is
        # only unbuildable because nothing has been sold yet.
        try:
            whole = _build_tx_plan(
                client,
                address,
                positions,
                rebalance_plan,
                known,
                config,
                idempotency_store,
            )
        except Exception:  # noqa: BLE001 - the venue refused to quote a buy
            pass
        else:
            return RebalanceExecutionReport(
                status="planned",
                rebalance_plan=rebalance_plan,
                policy_result=rebalance_plan.policy_result,
                plan=whole.plan,
                messages=(
                    "dry-run only; no transactions broadcast",
                    *whole.messages,
                ),
                in_progress=bool(whole.messages),
            )
        built = _build_tx_plan(
            client,
            address,
            positions,
            rebalance_plan,
            known,
            config,
            idempotency_store,
            trades=sells,
            ledger=FundingLedger({}),
        )
        deferred = tuple(
            f"buy deferred until the sells settle: {trade.instrument_id} "
            f"${float(trade.usd):.2f}"
            for trade in buys
        )
        return RebalanceExecutionReport(
            status="planned",
            rebalance_plan=rebalance_plan,
            policy_result=rebalance_plan.policy_result,
            plan=built.plan,
            messages=(
                "dry-run only; no transactions broadcast",
                *built.messages,
                *deferred,
            ),
            in_progress=bool(built.messages),
        )
    if staged:
        return _execute_staged(
            client,
            signer,
            address,
            positions,
            rebalance_plan,
            known,
            config,
            idempotency_store,
            sells,
            buys,
        )

    tx_plan, step_refs, messages, _funded = _build_tx_plan(
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
    share_baselines = _share_balances(positions)
    attribution_messages = _broadcast(
        client,
        address,
        positions,
        signer,
        step_refs,
        rpc_urls,
        gas_checks,
        config,
        idempotency_store,
        rebalance_plan,
        tx_plan,
        messages,
        execution_steps,
        receipts,
        share_baselines,
    )

    unconfirmed = pending_receipt_messages(receipts)
    in_progress = in_progress or bool(unconfirmed) or bool(attribution_messages)
    report = RebalanceExecutionReport(
        status="in_progress" if in_progress else "success",
        rebalance_plan=rebalance_plan,
        policy_result=rebalance_plan.policy_result,
        plan=tx_plan,
        steps=tuple(execution_steps),
        receipts=tuple(receipts),
        gas_checks=gas_checks,
        in_progress=in_progress,
        messages=(*messages, *unconfirmed, *attribution_messages),
    )
    _write_checkpoint(
        config,
        "rebalance",
        report,
        completed_keys=_completed_keys(step_refs, execution_steps),
    )
    return report


# 1Tx holds back roughly this much USDC per chain to sponsor gas. Measured on
# 2026-08-16: a $13.00 leg was accepted and $13.05 rejected against an idle
# balance of $13.788208. Overridable per deployment via config.
_DEFAULT_PAYMASTER_RESERVE_USD = 0.75
_MAX_FUNDING_ROUNDS = 4
_MIN_OUTSTANDING_USD = 0.05
_SETTLE_SECONDS = 4.0


def _execute_staged(
    client: object,
    signer: Signer,
    address: str,
    positions: object,
    rebalance_plan: rebalance_core.RebalancePlan,
    known: Sequence[Vault | Mapping[str, object]],
    config: object | None,
    idempotency_store: object | None,
    sells: Sequence[rebalance_core.RebalanceTrade],
    buys: Sequence[rebalance_core.RebalanceTrade],
) -> RebalanceExecutionReport:
    """Sells first, then buys built against the balances the sells created.

    The single-batch path builds every transaction before broadcasting any, which
    means a buy is quoted against balances that predate the sells paying for it —
    1Tx rejects it with "No chain has sufficient USDC balance". Here the sells go
    out, the wallet is re-read, and only then are the buys built.

    Buys are built in ROUNDS, each taking what is fundable now, because a leg may
    need several sources and a cross-chain source arrives over CCTP rather than
    immediately. A round that funds nothing new ends the loop: better to report an
    unfinished rebalance than to spin. Whatever is left is reported as outstanding
    and picked up by the next run, which re-plans from the book as it then is.
    """
    steps: list[ExecutionStepReport] = []
    receipts: list[Receipt] = []
    all_refs: list[_StepRef] = []
    all_steps: list[TxStep] = []
    messages: list[str] = []
    share_baselines = _share_balances(positions)

    def run(built: _BuiltPlan) -> None:
        rpc_urls, gas = _preflight(address, built.step_refs, config, idempotency_store)
        all_refs.extend(built.step_refs)
        all_steps.extend(built.plan.steps)
        messages.extend(built.messages)
        attribution_messages = _broadcast(
            client,
            address,
            positions,
            signer,
            built.step_refs,
            rpc_urls,
            gas,
            config,
            idempotency_store,
            rebalance_plan,
            TxPlan(steps=tuple(all_steps), summary="staged rebalance"),
            tuple(messages),
            steps,
            receipts,
            share_baselines,
        )
        messages.extend(attribution_messages)

    run(
        _build_tx_plan(
            client,
            address,
            positions,
            rebalance_plan,
            known,
            config,
            idempotency_store,
            trades=sells,
            # Sells need no funding, so do not read balances for them — and do
            # not let that read happen before the sells have even been built.
            ledger=FundingLedger({}),
        )
    )

    outstanding = {
        index: float(trade.usd)
        for index, trade in enumerate(rebalance_plan.trades)
        if trade.action == "buy"
    }
    by_index = {index: t for index, t in enumerate(rebalance_plan.trades)}
    rounds = 0
    while outstanding and rounds < _MAX_FUNDING_ROUNDS:
        rounds += 1
        _settle(config)
        balances = _idle_usdc_by_chain(client, address)
        pending = [by_index[i] for i in sorted(outstanding)]
        if not balances:
            # The venue cannot tell us what the wallet holds. "Unknown" is not
            # "empty": fall back to unpinned buys and let 1Tx route them, which
            # is exactly what the single-batch path does.
            built = _build_tx_plan(
                client,
                address,
                positions,
                rebalance_plan,
                known,
                config,
                idempotency_store,
                trades=pending,
                ledger=FundingLedger({}),
                amounts=dict(outstanding),
            )
            outstanding.clear()
            if built.step_refs:
                run(built)
            break
        built = _build_tx_plan(
            client,
            address,
            positions,
            rebalance_plan,
            known,
            config,
            idempotency_store,
            trades=pending,
            ledger=FundingLedger(balances, reserve_usd=_reserve_usd(config)),
            amounts=dict(outstanding),
            allow_partial=True,
        )
        progressed = False
        for index, got in built.funded_usd.items():
            if got <= 0:
                continue
            progressed = True
            left = outstanding.get(index, 0.0) - got
            if left <= _MIN_OUTSTANDING_USD:
                outstanding.pop(index, None)
            else:
                outstanding[index] = left
        if not built.step_refs:
            break
        run(built)
        if not progressed:
            break

    for index, left in outstanding.items():
        messages.append(
            f"buy not fully funded: {by_index[index].instrument_id} "
            f"${left:.2f} of ${float(by_index[index].usd):.2f} outstanding"
        )

    unconfirmed = pending_receipt_messages(receipts)
    in_progress = (
        bool(unconfirmed)
        or bool(outstanding)
        or any("cost basis" in message for message in messages)
    )
    tx_plan = TxPlan(
        steps=tuple(all_steps),
        summary=(
            f"Staged rebalance: {len(sells)} sells then {len(buys)} buys "
            f"across {len(all_steps)} transaction steps"
        ),
    )
    report = RebalanceExecutionReport(
        status="in_progress" if in_progress else "success",
        rebalance_plan=rebalance_plan,
        policy_result=rebalance_plan.policy_result,
        plan=tx_plan,
        steps=tuple(steps),
        receipts=tuple(receipts),
        gas_checks=(),
        in_progress=in_progress,
        messages=(*messages, *unconfirmed),
    )
    _write_checkpoint(
        config,
        "rebalance",
        report,
        completed_keys=_completed_keys(tuple(all_refs), steps),
    )
    return report


def _reserve_usd(config: object | None) -> float:
    value = _config_value(config, "paymaster_reserve_usd")
    if value is None:
        return _DEFAULT_PAYMASTER_RESERVE_USD
    try:
        return max(0.0, float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return _DEFAULT_PAYMASTER_RESERVE_USD


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


def _broadcast(
    client: object,
    address: str,
    initial_positions: object,
    signer: Signer,
    step_refs: Sequence[_StepRef],
    rpc_urls: Mapping[int, str],
    gas_checks: object,
    config: object | None,
    idempotency_store: object | None,
    rebalance_plan: rebalance_core.RebalancePlan,
    tx_plan: TxPlan,
    messages: Sequence[str],
    execution_steps: list[ExecutionStepReport],
    receipts: list[Receipt],
    share_baselines: dict[str, Decimal],
) -> tuple[str, ...]:
    """Send every step, appending to `execution_steps`/`receipts` as it goes.

    Mutates the two lists rather than returning them so that a failure part-way
    through a STAGED run still reports the sells that already landed — the
    caller holds the accumulator, and the checkpoint written on the way out
    carries everything sent so far, not just this stage.
    """
    attribution_messages: list[str] = []
    attribution_required = (
        backend_from_config(config, needs="allocation_log_path") is not None
    )
    for ref in step_refs:
        if _store_completed(idempotency_store, ref.idempotency_key):
            allocation_key = f"{ref.idempotency_key}:allocation-log"
            if attribution_required and not _store_completed(
                idempotency_store, allocation_key
            ):
                receipt = _stored_receipt(idempotency_store, ref.idempotency_key)
                if receipt is not None and receipt.status == 1:
                    logged_ref = ref
                    if ref.action_type == "buy" and ref.step.kind != "approve":
                        shares = _settled_buy_delta(
                            client,
                            address,
                            initial_positions,
                            ref,
                            config,
                            share_baselines,
                        )
                        if shares is None:
                            attribution_messages.append(
                                "confirmed buy cost basis is not yet observable: "
                                f"{ref.instrument_id}"
                            )
                        else:
                            logged_ref = ref.model_copy(update={"shares": shares})
                    if logged_ref.action_type != "buy" or logged_ref.shares is not None:
                        _append_allocation_log(config, logged_ref, receipt)
                        _store_mark_completed(
                            idempotency_store, allocation_key, receipt.transaction_hash
                        )
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
            _mark_leg_if_complete(
                ref, step_refs, idempotency_store, attribution_required
            )
            continue

        try:
            raw_receipt = signer.send(ref.step, rpc_urls[ref.step.chain_id])
            receipt = (
                raw_receipt
                if isinstance(raw_receipt, Receipt)
                else Receipt.model_validate(raw_receipt)
            )
        except Exception as error:
            partial_report = ExecutionReport(
                status="failed",
                policy_result=rebalance_plan.policy_result,
                plan=tx_plan,
                steps=tuple(execution_steps),
                receipts=tuple(receipts),
                gas_checks=gas_checks,
                in_progress=False,
                messages=tuple(messages),
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
        logged_ref = ref
        if (
            ref.action_type == "buy"
            and ref.step.kind != "approve"
            and receipt.status == 1
            and attribution_required
        ):
            shares = _settled_buy_delta(
                client,
                address,
                initial_positions,
                ref,
                config,
                share_baselines,
            )
            if shares is not None:
                logged_ref = ref.model_copy(update={"shares": shares})
        if receipt.status != 1:
            pass
        elif (
            attribution_required
            and ref.action_type == "buy"
            and ref.step.kind != "approve"
            and logged_ref.shares is None
        ):
            attribution_messages.append(
                f"confirmed buy cost basis is not yet observable: {ref.instrument_id}"
            )
        else:
            _append_allocation_log(config, logged_ref, receipt)
            if attribution_required:
                _store_mark_completed(
                    idempotency_store,
                    f"{ref.idempotency_key}:allocation-log",
                    receipt.transaction_hash,
                )
        _mark_leg_if_complete(ref, step_refs, idempotency_store, attribution_required)
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
    return tuple(attribution_messages)


def _stored_receipt(store: object | None, key: str) -> Receipt | None:
    value: object | None = None
    getter = getattr(store, "completed_value", None)
    if callable(getter):
        value = getter(key)
    elif isinstance(store, Mapping):
        value = store.get(key)
    else:
        completed = getattr(store, "completed", None)
        if isinstance(completed, Mapping):
            value = completed.get(key)
    if isinstance(value, Receipt):
        return value
    if isinstance(value, Mapping):
        try:
            return Receipt.model_validate(value)
        except Exception:  # noqa: BLE001 - old stores may contain only True
            return None
    return None


def _settled_buy_delta(
    client: object,
    address: str,
    initial_positions: object,
    ref: _StepRef,
    config: object | None,
    share_baselines: dict[str, Decimal],
) -> str | None:
    """Return the exact newly settled shares for one confirmed buy."""
    read = getattr(client, "positions", None)
    if not callable(read):
        return None
    baseline = share_baselines.get(
        ref.instrument_id,
        _share_balance(initial_positions, ref.instrument_id) or Decimal("0"),
    )
    attempts_value = _config_value(config, "position_settlement_attempts")
    try:
        attempts = max(1, int(attempts_value or 1))
    except (TypeError, ValueError):
        attempts = 1
    for attempt in range(attempts):
        response = read(
            {
                "address": address,
                "chainId": ref.settlement_chain_id or ref.step.chain_id,
            }
        )
        observed = _share_balance(response, ref.instrument_id)
        if observed is not None:
            delta = observed - baseline
            if delta > 0:
                share_baselines[ref.instrument_id] = observed
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


class _BuiltPlan(NamedTuple):
    plan: TxPlan
    step_refs: tuple[_StepRef, ...]
    messages: tuple[str, ...]
    funded_usd: dict[int, float]
    """How much of each buy this round actually found funding for, by trade index."""


def _build_tx_plan(
    client: object,
    address: str,
    positions: object,
    plan: rebalance_core.RebalancePlan,
    known_instruments: Sequence[Vault | Mapping[str, object]],
    config: object | None,
    idempotency_store: object | None,
    *,
    trades: Sequence[rebalance_core.RebalanceTrade] | None = None,
    ledger: FundingLedger | None = None,
    amounts: Mapping[int, float] | None = None,
    allow_partial: bool = False,
) -> _BuiltPlan:
    """Build calldata for `trades` (default: all of them).

    `trades` is a subset so the executor can build the sells, broadcast them, and
    only then build the buys they pay for — see `_execute_staged`. `amounts`
    overrides a trade's size by its index in `plan.trades`, which is how a buy
    that could only be part-funded this round comes back for the remainder.
    """
    vaults_by_id = _vaults_by_id(known_instruments)
    buy_allocation = _buy_allocation(plan)
    buy_index_by_action = _buy_index_by_action(plan)
    plan_steps: list[TxStep] = []
    step_refs: list[_StepRef] = []
    build_payloads: list[object] = []

    # 🔑 The balances a buy is planned against are the ones that exist BY THE TIME
    # IT RUNS, not the ones the wallet held before the batch. `plan.trades` is
    # ordered sells-first (core.rebalance._trades), so crediting each sell as it
    # is planned is what lets a sell fund a buy on the same chain — and lets 1Tx
    # bridge the proceeds onward when the destination differs.
    if ledger is None:
        ledger = FundingLedger(
            _idle_usdc_by_chain(client, address),
            reserve_usd=_reserve_usd(config),
        )
    selected = plan.trades if trades is None else trades
    indexed = {id(trade): index for index, trade in enumerate(plan.trades)}
    funded: dict[int, float] = {}

    for trade in selected:
        action_index = indexed[id(trade)]
        leg_key = _leg_key(action_index, trade.instrument_id)
        if _store_completed(idempotency_store, leg_key):
            continue

        responses: list[tuple[object, str, float]] = []
        if trade.action == "sell":
            responses.append(
                (
                    _build_sell(
                        client,
                        _sell_body(address, positions, trade, config),
                    ),
                    leg_key,
                    float(trade.usd),
                )
            )
            # Proceeds land as USDC on the chain the position was held on.
            held_chain = _trade_chain(trade, positions, vaults_by_id)
            ledger.credit(held_chain, float(trade.usd))
        else:
            buy_index = buy_index_by_action[action_index]
            vault = vaults_by_id.get(trade.instrument_id)
            wanted = float(
                trade.usd if amounts is None else amounts.get(action_index, trade.usd)
            )
            sources = ledger.plan_sources(
                vault.chain_id if vault is not None else None,
                wanted,
                allow_partial=allow_partial,
            )
            # What was handed to the venue for this trade this round. With no
            # sources the buy still goes out unpinned for its full size, so it
            # is not outstanding — it is 1Tx's call whether it can be filled.
            funded[action_index] = sum(usd for _, usd in sources) or wanted
            if not sources and allow_partial:
                # Staged round with real balances that show nothing available.
                # Sending it unpinned here would ask the venue for money we can
                # see is not there; recording no progress ends the loop instead.
                funded[action_index] = 0.0
                continue
            if not sources:
                # No balance information at all — leave the source unpinned and
                # let 1Tx report any shortfall itself.
                responses.append(
                    (
                        _build_buy(
                            client,
                            _buy_body(
                                address,
                                buy_allocation,
                                buy_index,
                                vaults_by_id,
                                config,
                                ledger.available,
                            ),
                        ),
                        leg_key,
                        wanted,
                    )
                )
            else:
                # One op per funding chain. Several small buys settle where a
                # single large one has no chain that can cover it alone.
                for source_index, (chain_id, usd) in enumerate(sources):
                    responses.append(
                        (
                            _build_buy(
                                client,
                                _buy_body(
                                    address,
                                    buy_allocation,
                                    buy_index,
                                    vaults_by_id,
                                    config,
                                    ledger.available,
                                    amount_usd=usd,
                                    source_chain_id_override=chain_id,
                                ),
                            ),
                            f"{leg_key}:src:{chain_id}"
                            if len(sources) > 1
                            else leg_key,
                            usd,
                        )
                    )
                    del source_index

        for response, response_key, operation_usd in responses:
            build_payloads.append(response)
            raw_steps = _raw_transactions(response)
            for step_index, raw_step in enumerate(raw_steps):
                step = _rebalance_tx_step(
                    raw_step,
                    step_index,
                    len(raw_steps),
                    trade.action,
                )
                step_key = f"{response_key}:step:{step_index}"
                plan_steps.append(step)
                step_refs.append(
                    _StepRef(
                        leg_index=action_index,
                        step_index=len(step_refs),
                        instrument_id=trade.instrument_id,
                        step=step,
                        idempotency_key=step_key,
                        usd=operation_usd,
                        shares=trade.yield_token_amount,
                        settlement_chain_id=(
                            vaults_by_id[trade.instrument_id].chain_id
                            if trade.action == "buy"
                            and trade.instrument_id in vaults_by_id
                            else _trade_chain(trade, positions, vaults_by_id)
                        ),
                        action_type=trade.action,
                    )
                )

    tx_plan = TxPlan(
        steps=tuple(plan_steps),
        summary=(
            f"Build rebalance transactions for {len(selected)} delta trades "
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
    return _BuiltPlan(tx_plan, tuple(step_refs), messages, funded)


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
    require_attribution: bool = False,
) -> None:
    leg_refs = [item for item in step_refs if item.leg_index == ref.leg_index]
    if leg_refs and all(
        _store_completed(store, item.idempotency_key)
        and (
            not require_attribution
            or item.step.kind == "approve"
            or _store_completed(store, f"{item.idempotency_key}:allocation-log")
        )
        for item in leg_refs
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
