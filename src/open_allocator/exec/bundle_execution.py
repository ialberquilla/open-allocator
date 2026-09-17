"""Wallet-aware preparation and submission of calldata bundle plans.

A calldata plan is a sequence of bundles, each an ordered run of calls Darex
simulated together. What gets estimated, submitted, deduplicated, and reported
here is the wallet operation that carries them: consecutive bundles on one chain
ride in one Safe operation when the signer can batch. Inner calls stay visible in
reports but are never marked complete on their own inside a batch — they share
one receipt and cannot partially settle.

Before anything is sent, and again before each later operation, the plan is
checked against what the account actually holds (``funding``): Darex simulated
the calls with assumed balances, so a passing simulation says nothing about
whether this Safe can pay for them.

Two measurements must not be confused. A bundle's ``protocol_gas`` is Darex's
wallet-neutral simulation of the bare calls. A ``WalletPreparation`` is this
repo's estimate of the real envelope — deployment, paymaster approval, Safe
batching — and is the one that says whether the operation can run.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

from open_allocator.core import policy as policy_core
from open_allocator.core.types import FrozenModel, TxBundle, TxPlan, TxStep
from open_allocator.exec import calldata, chains, funding
from open_allocator.exec.execute import (
    ExecutionBroadcastError,
    ExecutionReport,
    ExecutionStepReport,
    GasCheck,
    TransactionPlanError,
    WalletPreparation,
    _append_allocation_log,
    _preflight,
    _StepRef,
    _store_completed,
    _store_mark_completed,
    _write_checkpoint,
    pending_receipt_messages,
    supports_batching,
)
from open_allocator.exec.funding import FundingRequirement
from open_allocator.exec.paymaster_types import (
    PaymasterError,
    PaymasterPreparationUnavailable,
    PreparedUserOperation,
)
from open_allocator.exec.signer import Receipt


class SubmissionModeError(TransactionPlanError):
    """The configured signer cannot submit this plan safely as it was built."""


class UnderfundedPlanError(TransactionPlanError):
    """The account does not hold what the plan spends; nothing was sent for it."""

    def __init__(
        self,
        message: str,
        requirements: Sequence[FundingRequirement] = (),
    ) -> None:
        self.requirements = tuple(requirements)
        super().__init__(message)


@dataclass(frozen=True)
class PlannedBundle:
    bundle: TxBundle
    steps: tuple[TxStep, ...]

    @property
    def key(self) -> str:
        """Operation-level idempotency key, bound to these exact calls.

        A rebuilt bundle keeps its ID but gets a new digest, so completion
        recorded here is never read as completion of different calldata.
        """
        return f"{self.bundle.bundle_id}:digest:{self.bundle.digest}"

    def call_key(self, index: int) -> str:
        return f"{self.key}:call:{index}"


@dataclass(frozen=True)
class Operation:
    """Bundles that go out as one wallet operation, in plan order."""

    chain_id: int
    bundles: tuple[PlannedBundle, ...]

    @property
    def steps(self) -> tuple[TxStep, ...]:
        return tuple(step for item in self.bundles for step in item.steps)

    @property
    def bundle_ids(self) -> tuple[str, ...]:
        return tuple(item.bundle.bundle_id for item in self.bundles)


class PlanPreparation(FrozenModel):
    """What a dry run can say about executing a plan with this signer."""

    preparations: tuple[WalletPreparation, ...] = ()
    # Every token the plan spends, against the balance read for it.
    funding: tuple[FundingRequirement, ...] = ()
    messages: tuple[str, ...] = ()
    # Reasons the plan cannot be submitted by this signer at all. A dry run
    # reports them; execution refuses before anything is sent.
    blockers: tuple[str, ...] = ()

    @property
    def funded(self) -> bool:
        return all(item.ok for item in self.funding)


@dataclass(frozen=True)
class BundleLog:
    """What the allocation log records for one settled bundle."""

    action_type: str
    usd: float | None = None
    shares: str | None = None
    share_price: str | None = None


@dataclass(frozen=True)
class BundleExecution:
    plan: TxPlan
    steps: tuple[ExecutionStepReport, ...]
    receipts: tuple[Receipt, ...]
    gas_checks: tuple[GasCheck, ...]
    preparations: tuple[WalletPreparation, ...]
    funding: tuple[FundingRequirement, ...]
    in_progress: bool
    messages: tuple[str, ...]
    completed_keys: tuple[str, ...]

    @property
    def status(self) -> Literal["success", "in_progress"]:
        return "in_progress" if self.in_progress else "success"


def planned_bundles(plan: TxPlan) -> tuple[PlannedBundle, ...]:
    """The plan's bundles with their steps, in plan order.

    A calldata plan is bundles and nothing else: a step outside every bundle has
    no simulation, digest, or expiry behind it, so it is refused rather than
    submitted.
    """
    covered = sorted(index for bundle in plan.bundles for index in bundle.step_indexes)
    if covered != list(range(len(plan.steps))):
        raise TransactionPlanError(
            "every step of a calldata plan must belong to exactly one bundle"
        )
    ordered = sorted(plan.bundles, key=lambda bundle: bundle.step_indexes[0])
    return tuple(
        PlannedBundle(
            bundle=bundle,
            steps=tuple(plan.steps[index] for index in bundle.step_indexes),
        )
        for bundle in ordered
    )


def operations(
    bundles: Sequence[PlannedBundle],
    *,
    batching: bool,
) -> tuple[Operation, ...]:
    """Consecutive same-chain bundles merged into one operation when batching.

    Only consecutive runs merge, so plan order survives. A signer that cannot
    batch gets one operation per bundle and sends its calls one at a time.
    """
    grouped: list[Operation] = []
    for item in bundles:
        last = grouped[-1] if grouped else None
        if batching and last is not None and last.chain_id == item.bundle.chain_id:
            grouped[-1] = Operation(last.chain_id, (*last.bundles, item))
        else:
            grouped.append(Operation(item.bundle.chain_id, (item,)))
    return tuple(grouped)


def assemble_plan(bundles: Sequence[PlannedBundle], summary: str) -> TxPlan:
    """A plan from bundles in order, with their step indexes laid out afresh."""
    steps: list[TxStep] = []
    placed: list[TxBundle] = []
    for item in bundles:
        first = len(steps)
        steps.extend(item.steps)
        placed.append(
            item.bundle.model_copy(
                update={"step_indexes": tuple(range(first, first + len(item.steps)))}
            )
        )
    return TxPlan(steps=tuple(steps), summary=summary, bundles=tuple(placed))


def prepare_plan(
    signer: object,
    plan: TxPlan,
    config: object | None = None,
    idempotency_store: object | None = None,
) -> PlanPreparation:
    """Estimate every operation the plan would submit; sign and send nothing.

    For an ERC-4337 signer each operation is built exactly as submission would
    build it — factory data for a counterfactual Safe, the paymaster approval,
    the calls in order — and estimated by the bundler. A proposal-only Safe is
    checked for what it cannot do: propose to a Safe that does not exist yet, or
    park an expiring quote in a queue with no execution deadline.

    Then the whole plan is walked against the account's real balances: every
    bundle's ``requires``, aggregated per chain and token in execution order,
    plus each operation's bounded paymaster charge. A shortfall is a blocker.
    """
    preparation, _checked = _prepare(signer, plan, config, idempotency_store)
    return preparation


def _prepare(
    signer: object,
    plan: TxPlan,
    config: object | None,
    idempotency_store: object | None,
) -> tuple[PlanPreparation, tuple[str, ...]]:
    """The preparation, plus which of its blockers are about submission mode."""
    bundles = planned_bundles(plan)
    if not bundles:
        return PlanPreparation(), ()

    blockers = [
        *_submission_mode_blockers(signer, bundles),
        *_deployment_blockers(signer, bundles, config),
    ]
    preparations: list[WalletPreparation] = []
    messages: list[str] = []
    grouped = operations(bundles, batching=supports_batching(signer))
    prepare = getattr(signer, "prepare_batch", None)
    if callable(prepare):
        for operation in grouped:
            try:
                prepared = prepare(
                    operation.steps,
                    chains.rpc_url(operation.chain_id, config) or "",
                )
            except PaymasterPreparationUnavailable as error:
                messages.append(
                    f"wallet gas is not estimated before submission: {error}"
                )
                break
            preparations.append(_wallet_preparation(operation, prepared))

    ledger, ledger_messages = _ledger_operations(
        grouped, preparations, signer, idempotency_store
    )
    check = funding.check_operations(ledger, config)
    preparation = PlanPreparation(
        preparations=tuple(preparations),
        funding=check.requirements,
        messages=(*messages, *ledger_messages, *check.messages),
        blockers=(*blockers, *check.blockers),
    )
    return preparation, tuple(blockers)


def execute_plan(
    client: object,
    signer: object,
    plan: TxPlan,
    *,
    stage: Literal["execute", "withdraw"],
    policy_result: policy_core.PolicyResult,
    completion_key: Callable[[TxBundle], str],
    log: Callable[[TxBundle], BundleLog],
    config: object | None = None,
    idempotency_store: object | None = None,
    clock: Callable[[], float] = time.time,
) -> BundleExecution:
    """Submit a calldata plan, one wallet operation at a time.

    Refuses up front — before any operation is sent — when the signer cannot
    submit the plan as built. Immediately before each operation is signed, any
    bundle within ``ONE_TX_MIN_CALLDATA_TTL_SECONDS`` of expiry is rebuilt from
    fresh calldata for the same leg. The ERC-4337 signer then prepares the
    operation again itself, so nonce, fees, and paymaster data are current.

    ``completion_key`` names the leg a bundle serves; it is marked when the
    operation is submitted, so a rerun never builds that leg again.
    """
    bundles = planned_bundles(plan)
    if not bundles:
        return BundleExecution(
            plan=plan,
            steps=(),
            receipts=(),
            gas_checks=(),
            preparations=(),
            funding=(),
            in_progress=False,
            messages=(),
            completed_keys=(),
        )

    preparation, mode_blockers = _prepare(signer, plan, config, idempotency_store)
    if mode_blockers:
        raise SubmissionModeError("; ".join(preparation.blockers))
    if not preparation.funded:
        raise UnderfundedPlanError("; ".join(preparation.blockers), preparation.funding)

    address = str(signer.address())  # type: ignore[attr-defined]
    rpc_urls, gas_checks = _preflight(
        address,
        [
            _step_ref(item, index, step)
            for item in bundles
            for index, step in enumerate(item.steps)
        ],
        config,
        idempotency_store,
    )

    batching = supports_batching(signer)
    min_ttl = calldata.min_ttl_seconds(config)
    pending = list(operations(bundles, batching=batching))
    settled: list[PlannedBundle] = []
    execution_steps: list[ExecutionStepReport] = []
    receipts: list[Receipt] = []
    completed: list[str] = []
    messages: list[str] = list(preparation.messages)
    # Operations this run sent that are not yet included; a fresh balance read
    # does not show what they spend.
    unsettled: list[funding.LedgerOperation] = []

    def write_partial(operation: Operation) -> ExecutionReport:
        partial = ExecutionReport(
            status="failed",
            policy_result=policy_result,
            plan=assemble_plan(
                [*settled, *operation.bundles, *_bundles_of(pending[1:])],
                plan.summary,
            ),
            steps=tuple(execution_steps),
            receipts=tuple(receipts),
            gas_checks=gas_checks,
            preparations=preparation.preparations,
            funding=preparation.funding,
            messages=tuple(messages),
        )
        _write_checkpoint(config, stage, partial, completed_keys=completed)
        return partial

    while pending:
        try:
            operation, rebuilt = _refreshed(
                client,
                pending[0],
                config=config,
                min_ttl_seconds=min_ttl,
                clock=clock,
            )
        except Exception:
            # Nothing of this operation was sent, but earlier ones may have been.
            write_partial(pending[0])
            raise
        messages.extend(
            f"bundle {bundle_id} was rebuilt before signing: its quote was within "
            f"{min_ttl}s of expiry"
            for bundle_id in rebuilt
        )
        if settled or rebuilt:
            # Balances moved since the plan was checked — earlier operations
            # spent and produced — and a rebuilt bundle may require other
            # amounts. Recheck everything still to be sent.
            ledger, _notes = _ledger_operations(
                (operation, *pending[1:]),
                preparation.preparations,
                signer,
                idempotency_store,
            )
            check = funding.check_operations(ledger, config, unsettled=unsettled)
            if not check.ok:
                write_partial(operation)
                raise UnderfundedPlanError(
                    "; ".join(check.blockers), check.requirements
                )
        rpc_url = rpc_urls[operation.chain_id]
        offset = sum(len(item.steps) for item in settled)
        try:
            sent_receipts = len(receipts)
            if batching:
                receipt = signer.send_batch(operation.steps, rpc_url)  # type: ignore[attr-defined]
                receipts.append(receipt)
                for item in operation.bundles:
                    for index, step in enumerate(item.steps):
                        execution_steps.append(
                            _step_report(
                                item, offset, index, step, "sent", receipt, batched=True
                            )
                        )
                    offset += len(item.steps)
                    completed.extend(
                        _complete(
                            item,
                            receipt,
                            idempotency_store,
                            config,
                            completion_key,
                            log,
                        )
                    )
            else:
                for item in operation.bundles:
                    last_receipt: Receipt | None = None
                    for index, step in enumerate(item.steps):
                        key = item.call_key(index)
                        if _store_completed(idempotency_store, key):
                            execution_steps.append(
                                _step_report(
                                    item, offset, index, step, "skipped", batched=False
                                )
                            )
                            continue
                        last_receipt = signer.send(step, rpc_url)  # type: ignore[attr-defined]
                        receipts.append(last_receipt)
                        _store_mark_completed(idempotency_store, key, last_receipt)
                        completed.append(key)
                        execution_steps.append(
                            _step_report(
                                item,
                                offset,
                                index,
                                step,
                                "sent",
                                last_receipt,
                                batched=False,
                            )
                        )
                    offset += len(item.steps)
                    completed.extend(
                        _complete(
                            item,
                            last_receipt,
                            idempotency_store,
                            config,
                            completion_key,
                            log,
                        )
                    )
        except Exception as error:
            partial = write_partial(operation)
            if isinstance(error, PaymasterError):
                raise
            raise ExecutionBroadcastError(
                "transaction broadcast failed",
                leg_index=operation.bundles[0].bundle.leg_index,
                step_index=sum(len(item.steps) for item in settled),
                partial_report=partial,
            ) from error

        if pending_receipt_messages(receipts[sent_receipts:]):
            sent, _notes = _ledger_operations(
                (operation,), preparation.preparations, signer, None
            )
            unsettled.extend(sent)
        settled.extend(operation.bundles)
        pending.pop(0)

    unconfirmed = pending_receipt_messages(receipts)
    return BundleExecution(
        plan=assemble_plan(settled, plan.summary),
        steps=tuple(execution_steps),
        receipts=tuple(receipts),
        gas_checks=gas_checks,
        preparations=preparation.preparations,
        funding=preparation.funding,
        in_progress=bool(unconfirmed),
        messages=(*messages, *unconfirmed),
        completed_keys=tuple(completed),
    )


def _submission_mode_blockers(
    signer: object,
    bundles: Sequence[PlannedBundle],
) -> Iterable[str]:
    if not getattr(signer, "proposes_asynchronously", False):
        return ()
    expiring = [
        item.bundle.bundle_id for item in bundles if item.bundle.expires_at is not None
    ]
    if not expiring:
        return ()
    return (
        f"bundles {', '.join(expiring)} carry expiring quotes, but this signer "
        "only proposes to the Safe Transaction Service, where a transaction waits "
        "for co-signers with no deadline; use a submission path that executes "
        "immediately (SIGNER_SUBMISSION=erc4337-paymaster) and rebuild the plan",
    )


def _deployment_blockers(
    signer: object,
    bundles: Sequence[PlannedBundle],
    config: object | None,
) -> Iterable[str]:
    is_deployed = getattr(signer, "is_deployed", None)
    if not callable(is_deployed):
        return ()
    blockers: list[str] = []
    for chain_id in dict.fromkeys(item.bundle.chain_id for item in bundles):
        name = f"{chains.chain_name(chain_id)} (chain {chain_id})"
        rpc_url = chains.rpc_url(chain_id, config)
        if rpc_url is None:
            blockers.append(
                f"no RPC for {name}, so the Safe cannot be confirmed deployed there; "
                f"set RPC_URL_{chain_id}"
            )
        elif not is_deployed(chain_id, rpc_url):
            blockers.append(
                f"the Safe is not deployed on {name}; `safe + rpc` can only propose "
                "to an existing Safe — deploy it first, or use "
                "SIGNER_SUBMISSION=erc4337-paymaster, whose first operation "
                "deploys it"
            )
    return blockers


def _ledger_operations(
    grouped: Sequence[Operation],
    preparations: Sequence[WalletPreparation],
    signer: object,
    store: object | None,
) -> tuple[tuple[funding.LedgerOperation, ...], tuple[str, ...]]:
    """Operations as the funding ledger walks them, each with its gas charge."""
    charges = {item.bundle_ids: item for item in preparations}
    paymaster = callable(getattr(signer, "prepare_batch", None))
    ledger: list[funding.LedgerOperation] = []
    messages: list[str] = []
    for operation in grouped:
        # A bundle whose calls were partly sent one at a time by an earlier run
        # has already spent some of what it requires; checking it again would
        # refuse the rerun that finishes it.
        bundles = tuple(
            item.bundle
            for item in operation.bundles
            if not any(
                _store_completed(store, item.call_key(index))
                for index in range(len(item.steps))
            )
        )
        for item in operation.bundles:
            if item.bundle not in bundles:
                messages.append(
                    f"bundle {item.bundle.bundle_id} is partly sent; its "
                    "requirements are not rechecked"
                )
        prepared = charges.get(
            tuple(item.bundle.bundle_id for item in operation.bundles)
        )
        gas_token = gas_charge = None
        if prepared is not None and prepared.paymaster_token is not None:
            if prepared.max_gas_token_charge_raw is None:
                messages.append(
                    f"the paymaster's gas charge for {', '.join(prepared.bundle_ids)} "
                    "could not be bounded, so no USDC is reserved for it"
                )
            else:
                gas_token = prepared.paymaster_token
                gas_charge = int(prepared.max_gas_token_charge_raw)
        elif paymaster and prepared is None:
            messages.append(
                f"the paymaster's gas charge for "
                f"{', '.join(item.bundle.bundle_id for item in operation.bundles)} "
                "was not estimated, so no USDC is reserved for it"
            )
        ledger.append(
            funding.LedgerOperation(
                chain_id=operation.chain_id,
                bundles=bundles,
                gas_token=gas_token,
                gas_charge_raw=gas_charge,
            )
        )
    return tuple(ledger), tuple(messages)


def _wallet_preparation(
    operation: Operation,
    prepared: PreparedUserOperation,
) -> WalletPreparation:
    if prepared.chain_id != operation.chain_id:
        raise TransactionPlanError(
            f"prepared operation is on chain {prepared.chain_id}, its bundles on "
            f"chain {operation.chain_id}"
        )
    for item in operation.bundles:
        # Darex built and simulated the calls for this account: approvals,
        # receivers, and swap recipients all name it. Sent from any other
        # sender they do something else.
        if item.bundle.account.casefold() != prepared.sender.casefold():
            raise TransactionPlanError(
                f"bundle {item.bundle.bundle_id} was built for {item.bundle.account} "
                f"but the operation is sent from {prepared.sender}"
            )
    gas = prepared.gas
    return WalletPreparation(
        bundle_ids=operation.bundle_ids,
        chain_id=prepared.chain_id,
        sender=prepared.sender,
        includes_deployment=prepared.includes_deployment,
        call_gas_limit=gas.call_gas_limit,
        verification_gas_limit=gas.verification_gas_limit,
        pre_verification_gas=gas.pre_verification_gas,
        paymaster_verification_gas_limit=gas.paymaster_verification_gas_limit,
        paymaster_post_op_gas_limit=gas.paymaster_post_op_gas_limit,
        max_fee_per_gas=gas.max_fee_per_gas,
        max_priority_fee_per_gas=gas.max_priority_fee_per_gas,
        paymaster_address=prepared.paymaster.paymaster,
        paymaster_token=prepared.paymaster.token,
        paymaster_approval_included=prepared.paymaster.approval_included,
        max_gas_token_charge_raw=prepared.max_gas_token_charge_raw,
    )


def _refreshed(
    client: object,
    operation: Operation,
    *,
    config: object | None,
    min_ttl_seconds: int,
    clock: Callable[[], float],
) -> tuple[Operation, tuple[str, ...]]:
    items: list[PlannedBundle] = []
    rebuilt: list[str] = []
    for item in operation.bundles:
        if calldata.needs_refresh(
            item.bundle,
            min_ttl_seconds=min_ttl_seconds,
            now=clock(),
        ):
            steps, bundle = calldata.refresh_bundle(
                client,
                item.bundle,
                config=config,
                now=clock(),
            )
            item = PlannedBundle(bundle=bundle, steps=steps)
            rebuilt.append(bundle.bundle_id)
        items.append(item)
    return Operation(operation.chain_id, tuple(items)), tuple(rebuilt)


def _complete(
    item: PlannedBundle,
    receipt: Receipt | None,
    store: object | None,
    config: object | None,
    completion_key: Callable[[TxBundle], str],
    log: Callable[[TxBundle], BundleLog],
) -> tuple[str, ...]:
    """Mark a submitted bundle and its leg; log it once, not once per call."""
    bundle = item.bundle
    leg_key = completion_key(bundle)
    _store_mark_completed(store, item.key, receipt)
    _store_mark_completed(store, leg_key, True)
    if receipt is not None:
        entry = log(bundle)
        # The bundle's final call is the one it exists for; approvals and swaps
        # before it only clear the way.
        _append_allocation_log(
            config,
            _StepRef(
                leg_index=bundle.leg_index,
                step_index=len(item.steps) - 1,
                instrument_id=bundle.instrument_id,
                step=item.steps[-1],
                idempotency_key=item.key,
                usd=entry.usd,
                shares=entry.shares,
                share_price=entry.share_price,
                action_type=entry.action_type,
            ),
            receipt,
        )
    return (item.key, leg_key)


def _step_ref(item: PlannedBundle, index: int, step: TxStep) -> _StepRef:
    return _StepRef(
        leg_index=item.bundle.leg_index,
        step_index=index,
        instrument_id=item.bundle.instrument_id,
        step=step,
        idempotency_key=item.call_key(index),
        action_type=item.bundle.action,
    )


def _step_report(
    item: PlannedBundle,
    offset: int,
    index: int,
    step: TxStep,
    status: Literal["sent", "skipped"],
    receipt: Receipt | None = None,
    *,
    batched: bool,
) -> ExecutionStepReport:
    return ExecutionStepReport(
        leg_index=item.bundle.leg_index,
        step_index=offset + index,
        instrument_id=item.bundle.instrument_id,
        status=status,
        step=step,
        receipt=receipt,
        # A batched call shares its operation's key; only a call sent on its
        # own has a completion of its own.
        idempotency_key=item.key if batched else item.call_key(index),
    )


def _bundles_of(pending: Sequence[Operation]) -> list[PlannedBundle]:
    return [item for operation in pending for item in operation.bundles]


__all__ = [
    "BundleExecution",
    "BundleLog",
    "Operation",
    "PlanPreparation",
    "PlannedBundle",
    "SubmissionModeError",
    "UnderfundedPlanError",
    "assemble_plan",
    "execute_plan",
    "operations",
    "planned_bundles",
    "prepare_plan",
]
