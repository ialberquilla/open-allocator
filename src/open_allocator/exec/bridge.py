"""Cross-chain calldata deposits: CCTP burn, attestation, and atomic settlement.

1Tx builds only the source half of a bridge — approve and ``depositForBurn``,
with the Safe as mint recipient and destination caller. The rest happens here,
across as many invocations of ``execute --confirm`` as Circle takes:

1. The burn rides in the source chain's operation, and the leg's record moves
   to ``source_submitted`` before its completion is marked.
2. Once the source transaction is included, its ``MessageSent`` log identifies
   the burn (``awaiting_attestation``).
3. Circle is asked once per invocation. An unready attestation reports the leg
   in progress; a ready one is checked against the burn (``destination_ready``).
4. Fresh deposit calldata is requested only now, sized to the destination USDC
   plus the attested mint less the paymaster's bounded charge, and submitted
   with ``receiveMessage`` in front of it as one operation
   (``destination_submitted``). A reverting deposit reverts the redemption, so
   the nonce stays unused and the operation can be rebuilt.
5. The leg completes when that operation is included, or when the nonce turns
   out to be used already — never on submission alone.

A source operation is observed and never rebuilt; a destination operation is
reconciled and never blindly resent. Each chain's calls stay one atomic
operation; the leg as a whole cannot be.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from open_allocator.core import amounts
from open_allocator.core import policy as policy_core
from open_allocator.core.types import TxBundle
from open_allocator.exec import (
    bundle_execution,
    calldata,
    cctp,
    chains,
    deposit_sizing,
    funding,
)
from open_allocator.exec.bridge_state import BridgeState, load, now_text, save
from open_allocator.exec.circle import CircleClient, CircleError
from open_allocator.exec.client import CctpChainConfig, CctpConfigResponse
from open_allocator.exec.execute import (
    ExecutionStepReport,
    TransactionPlanError,
    WalletPreparation,
    _store_mark_completed,
)
from open_allocator.exec.funding import FundingRequirement
from open_allocator.exec.paymaster_types import UserOperationReverted
from open_allocator.exec.signer import Receipt


class BridgeUnavailableError(calldata.CalldataUnsupportedError):
    """A leg would need a bridge this signer or deployment cannot carry."""


def supports_cross_chain(signer: object) -> bool:
    """Whether the signer can take a leg from burn to destination settlement.

    It must batch each chain's calls into one operation that executes when
    sent, bound the paymaster charge the destination mint pays, and look up an
    earlier operation by hash when a rerun resumes.
    """
    check = getattr(signer, "supports_cross_chain", None)
    return (
        callable(check)
        and bool(check())
        and callable(getattr(signer, "send_batch", None))
        and callable(getattr(signer, "operation_receipt", None))
        and not getattr(signer, "proposes_asynchronously", False)
    )


def require_cross_chain(signer: object, why: str) -> None:
    if not supports_cross_chain(signer):
        raise BridgeUnavailableError(
            f"{why}, but this signer cannot carry a bridged leg: cross-chain "
            "calldata deposits need a Safe submitting through "
            "SIGNER_SUBMISSION=erc4337-paymaster with a provider that estimates "
            "operations and bounds its gas charge (PAYMASTER_PROVIDER=pimlico)"
        )


def leg_id(leg_index: int, instrument_id: str) -> str:
    return f"leg:{leg_index}:{instrument_id}"


def burn_completion_key(bundle: TxBundle) -> str:
    """Marked when a burn is submitted; it never completes the leg itself."""
    return f"bridge:{leg_id(bundle.leg_index, bundle.instrument_id)}:burn"


def check_route(
    bundle: TxBundle,
    steps: Sequence[object],
    routes: CctpConfigResponse,
) -> None:
    """Refuse a burn whose contracts or domains are not 1Tx's CCTP route.

    Checked before the burn is sent: after it, a burn through the wrong
    messenger or to the wrong domain cannot be undone.
    """
    assert bundle.bridge is not None
    source = routes.chain(bundle.chain_id)
    destination = routes.chain(bundle.bridge.to_chain_id)
    problems: list[str] = []
    if source is None:
        problems.append(f"{_chain(bundle.chain_id)} is not a CCTP route")
    if destination is None:
        problems.append(f"{_chain(bundle.bridge.to_chain_id)} is not a CCTP route")
    if source is not None:
        if source.cctp_domain != bundle.bridge.source_domain:
            problems.append("source domain")
        if (
            source.token_messenger.casefold()
            != str(getattr(steps[-1], "to", "")).casefold()
        ):
            problems.append("source TokenMessenger")
    if destination is not None:
        if destination.cctp_domain != bundle.bridge.destination_domain:
            problems.append("destination domain")
    if problems:
        raise TransactionPlanError(
            f"bridge bundle {bundle.bundle_id} does not match 1Tx's CCTP "
            f"configuration: {', '.join(problems)}"
        )


def planned_state(
    bundle: TxBundle,
    *,
    source_token_messenger: str,
    wanted_deposit_raw: int,
) -> BridgeState:
    if bundle.action != "bridge" or bundle.bridge is None:
        raise TransactionPlanError(f"bundle {bundle.bundle_id} is not a bridge burn")
    stamp = now_text()
    return BridgeState(
        leg_id=leg_id(bundle.leg_index, bundle.instrument_id),
        leg_index=bundle.leg_index,
        instrument_id=bundle.instrument_id,
        state="bridge_planned",
        account=bundle.account,
        source_chain_id=bundle.chain_id,
        destination_chain_id=bundle.bridge.to_chain_id,
        source_domain=bundle.bridge.source_domain,
        destination_domain=bundle.bridge.destination_domain,
        source_token=bundle.token_in.address,
        source_token_messenger=source_token_messenger,
        burn_amount_raw=bundle.amount,
        max_fee_raw=bundle.bridge.max_fee,
        min_finality_threshold=bundle.bridge.min_finality_threshold,
        fast=bundle.bridge.fast,
        wanted_deposit_raw=str(wanted_deposit_raw),
        source_bundle_id=bundle.bundle_id,
        source_bundle_digest=bundle.digest,
        created_at=stamp,
        updated_at=stamp,
    )


def submitted_state(
    state: BridgeState,
    item: bundle_execution.PlannedBundle,
    operation: bundle_execution.Operation,
    receipt: Receipt | None,
) -> BridgeState:
    """The record once its burn has gone out in ``operation``."""
    burns = [entry for entry in operation.bundles if entry.bundle.action == "bridge"]
    position = next(
        index
        for index, entry in enumerate(burns)
        if entry.bundle.bundle_id == item.bundle.bundle_id
    )
    operation_hash, transaction_hash = _hashes(receipt)
    return state.advanced(
        "source_submitted",
        source_bundle_digest=item.bundle.digest,
        burn_index=position,
        burns_in_operation=len(burns),
        source_operation_hash=operation_hash,
        source_transaction_hash=transaction_hash,
    )


@dataclass
class BridgeProgress:
    """What advancing the bridged legs did in this invocation."""

    states: list[BridgeState] = field(default_factory=list)
    steps: list[ExecutionStepReport] = field(default_factory=list)
    receipts: list[Receipt] = field(default_factory=list)
    preparations: list[WalletPreparation] = field(default_factory=list)
    funding: list[FundingRequirement] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    completed_keys: list[str] = field(default_factory=list)
    errors: bool = False

    @property
    def in_progress(self) -> bool:
        return any(item.state not in ("completed", "failed") for item in self.states)

    @property
    def failed(self) -> bool:
        return self.errors or any(item.state == "failed" for item in self.states)


# The destination deposit token for a chain, resolved from discovery.
TokenFor = Callable[[int], calldata.DepositToken]


@dataclass
class BridgeRunner:
    client: object
    signer: object
    store: object | None
    token_for: TokenFor
    policy_result: policy_core.PolicyResult
    config: object | None = None
    circle: object | None = None
    reader: cctp.CctpChainReader | None = None
    _cctp: CctpConfigResponse | None = None

    def advance(self, states: Sequence[BridgeState]) -> BridgeProgress:
        """Take each leg as far as it can go now, one valid transition at a time."""
        progress = BridgeProgress()
        for state in states:
            progress.states.append(self._advance_leg(state, progress))
        return progress

    def _advance_leg(self, state: BridgeState, progress: BridgeProgress) -> BridgeState:
        label = f"bridged {state.leg_id}"
        # Each state is entered at most twice in one invocation (re-attestation
        # or a reverted destination); anything beyond that waits for a rerun.
        for _transition in range(2 * len(_ORDER)):
            if state.state in ("completed", "failed"):
                break
            before = state
            try:
                state = self._transition(state, progress)
            except cctp.CctpValidationError as error:
                state = state.advanced("failed", last_error=str(error))
                progress.messages.append(
                    f"{label} failed and will not be redeemed automatically: {error}"
                )
            except Exception as error:  # noqa: BLE001 - recorded; the leg resumes
                # A hook may have recorded a submission before the error; never
                # overwrite that with the state the attempt started from.
                state = _furthest(state, load(self.store, state.leg_id))
                state = state.noted(last_error=_error_text(error))
                progress.errors = True
                progress.messages.append(
                    f"{label} stopped at {state.state}: {_error_text(error)}; "
                    f"rerun {_rerun(state)} to resume it"
                )
                save(self.store, state)
                break
            save(self.store, state)
            if state.state == before.state:
                break
        if state.state == "completed":
            key = state.leg_id
            _store_mark_completed(self.store, key, True)
            progress.completed_keys.append(key)
        elif state.state not in ("failed",) and not state.last_error:
            progress.messages.append(_waiting_message(state))
        return state

    def _transition(self, state: BridgeState, progress: BridgeProgress) -> BridgeState:
        if state.state == "source_submitted":
            return self._observe_source(state, progress)
        if state.state == "awaiting_attestation":
            return self._await_attestation(state)
        if state.state == "destination_ready":
            return self._settle_destination(state, progress)
        if state.state == "destination_submitted":
            return self._reconcile_destination(state, progress)
        raise TransactionPlanError(
            f"bridge {state.leg_id} is {state.state}, which a rerun cannot advance"
        )

    # --- source -----------------------------------------------------------

    def _observe_source(
        self, state: BridgeState, progress: BridgeProgress
    ) -> BridgeState:
        if state.source_transaction_hash is None:
            if state.source_operation_hash is None:
                raise TransactionPlanError(
                    f"bridge {state.leg_id} was submitted without an operation hash"
                )
            try:
                receipt = self._operation_receipt(
                    state.source_chain_id, state.source_operation_hash
                )
            except UserOperationReverted as error:
                # Nothing burned: the whole operation reverted, so the leg may
                # be planned again.
                progress.messages.append(
                    f"bridged {state.leg_id}: the source burn reverted, so nothing "
                    "was bridged; a rerun plans the leg afresh"
                )
                return state.advanced("failed", last_error=str(error))
            if receipt is None or receipt.pending:
                return state
            state = state.noted(source_transaction_hash=receipt.transaction_hash)

        assert state.source_transaction_hash is not None
        logs = self._reader().transaction_logs(
            state.source_chain_id, state.source_transaction_hash
        )
        if logs is None:
            return state
        source = self._chain(state.source_chain_id)
        found = cctp.message_sent_logs(logs)
        expected = self._expectation(state)
        selected = cctp.select_source_log(
            found,
            expected,
            message_transmitter=source.message_transmitter,
            burn_index=state.burn_index,
            burns_in_operation=state.burns_in_operation,
        )
        own = [
            item
            for item in found
            if item.emitter.casefold() == source.message_transmitter.casefold()
        ]
        wanted = cctp.unattested(selected.message)
        ordinal = sum(
            1
            for item in own
            if item.log_index < selected.log_index
            and _safe_unattested(item.message) == wanted
        )
        return state.advanced(
            "awaiting_attestation",
            source_message_transmitter=selected.emitter,
            source_log_index=selected.log_index,
            source_message=selected.message,
            message_ordinal=ordinal,
        )

    def _await_attestation(self, state: BridgeState) -> BridgeState:
        assert state.source_transaction_hash is not None
        assert state.source_message is not None
        response = self._circle().messages(  # type: ignore[attr-defined]
            state.source_domain, state.source_transaction_hash
        )
        attested = cctp.select_attestation(
            response.messages,
            state.source_message,
            self._expectation(state),
            burn_index=state.message_ordinal,
        )
        replaced = (
            attested is not None
            and state.attestation_status == "reattestation_requested"
            and attested.message.body.expiration_block == state.expiration_block
        )
        if attested is None or replaced:
            if replaced:
                # Circle still serves the expired attestation; wait for the new one.
                return state.noted(last_error=None)
            statuses = sorted({item.status for item in response.messages})
            reasons = cctp.delay_reasons(response.messages)
            return state.noted(
                attestation_status=", ".join(statuses) or "not_found",
                delay_reason=", ".join(reasons) or None,
                last_error=None,
            )
        message = attested.message
        return state.advanced(
            "destination_ready",
            attestation_status="complete",
            delay_reason=None,
            message_hash=cctp.message_hash(message.raw),
            nonce=message.nonce,
            message="0x" + message.raw.hex(),
            attestation=attested.attestation,
            fee_executed_raw=str(message.body.fee_executed),
            net_mint_raw=str(attested.net_mint),
            expiration_block=message.body.expiration_block or None,
            last_error=None,
        )

    # --- destination --------------------------------------------------------

    def _settle_destination(
        self, state: BridgeState, progress: BridgeProgress
    ) -> BridgeState:
        assert state.nonce and state.message and state.attestation
        assert state.net_mint_raw is not None
        destination = self._chain(state.destination_chain_id)
        reader = self._reader()
        if reader.nonce_used(
            state.destination_chain_id, destination.message_transmitter, state.nonce
        ):
            return self._already_redeemed(state, progress)
        if state.expiration_block is not None and (
            reader.block_number(state.destination_chain_id) >= state.expiration_block
        ):
            self._circle().reattest(state.nonce)  # type: ignore[attr-defined]
            progress.messages.append(
                f"bridged {state.leg_id}: the attestation expired at block "
                f"{state.expiration_block}; Circle was asked to re-attest nonce "
                f"{state.nonce}, and nothing is burned again"
            )
            # The expired block is kept, so the old attestation is not taken up
            # again while Circle still serves it.
            return state.advanced(
                "awaiting_attestation",
                attestation=None,
                message=None,
                attestation_status="reattestation_requested",
            )

        require_cross_chain(self.signer, f"{state.leg_id} is bridged")
        token = self.token_for(state.destination_chain_id)
        address = state.account
        key = funding.key_for(state.destination_chain_id, address, token.address)
        held, _notes = funding.read_balances((key,), self.config)
        balance = held.get(key)
        if balance is None:
            raise TransactionPlanError(
                f"the Safe's USDC on {_chain(state.destination_chain_id)} cannot be "
                "read, so the bridged deposit cannot be sized"
            )
        mint = int(state.net_mint_raw)
        wanted = min(int(state.wanted_deposit_raw), balance + mint)
        step = cctp.receive_message_step(
            state.destination_chain_id,
            destination.message_transmitter,
            state.message,
            state.attestation,
        )
        receive = bundle_execution.PlannedBundle(
            bundle=calldata.receive_bundle(
                step,
                leg_index=state.leg_index,
                instrument_id=state.instrument_id,
                account=address,
                token=token,
                net_mint_raw=mint,
            ),
            steps=(step,),
        )
        if not state.deposit:
            return self._redeem(state, receive, progress)
        fitted = deposit_sizing.fit(
            self.client,
            self.signer,
            address,
            deposits=[
                deposit_sizing.DepositRequest(
                    index=state.leg_index,
                    instrument_id=state.instrument_id,
                    chain_id=state.destination_chain_id,
                    token=token,
                    wanted_raw=wanted,
                )
            ],
            withdrawals={state.destination_chain_id: [receive]},
            withdrawal_usd={
                state.leg_index: float(amounts.from_raw_units(mint, token.decimals))
            },
            summary=lambda ordered: (
                f"Redeem the CCTP transfer for {state.leg_id} and deposit it in "
                f"{state.instrument_id} as one operation"
            ),
            config=self.config,
            idempotency_store=self.store,
        )
        progress.preparations.extend(fitted.preparation.preparations)
        progress.funding.extend(fitted.preparation.funding)
        progress.messages.extend(fitted.messages)
        actions = [bundle.action for bundle in fitted.plan.bundles]
        if actions != ["cctp_receive", "deposit"]:
            raise TransactionPlanError(
                f"the attested mint for {state.leg_id} does not cover a deposit "
                "after the destination paymaster's charge; redeeming without "
                "depositing is not submitted"
            )
        charges = [
            item.max_gas_token_charge_raw for item in fitted.preparation.preparations
        ]
        if not charges or any(charge is None for charge in charges):
            raise TransactionPlanError(
                f"the paymaster charge for {state.leg_id}'s destination operation "
                "could not be bounded, and a bridged deposit pays it out of the mint"
            )
        if fitted.preparation.blockers:
            raise TransactionPlanError("; ".join(fitted.preparation.blockers))

        deposit_usd = fitted.deposit_usd.get(state.leg_index)
        submitted: list[BridgeState] = []

        def on_submitted(
            item: bundle_execution.PlannedBundle,
            _operation: bundle_execution.Operation,
            receipt: Receipt | None,
        ) -> None:
            if item.bundle.action != "deposit":
                return
            operation_hash, transaction_hash = _hashes(receipt)
            after = state.advanced(
                "destination_submitted",
                destination_bundle_digest=item.bundle.digest,
                deposit_amount_raw=item.bundle.amount,
                destination_operation_hash=operation_hash,
                destination_transaction_hash=transaction_hash,
                last_error=None,
            )
            save(self.store, after)
            submitted.append(after)

        result = bundle_execution.execute_plan(
            self.client,
            self.signer,
            fitted.plan,
            stage="execute",
            policy_result=self.policy_result,
            completion_key=lambda bundle: (
                f"bridge:{state.leg_id}:destination:{bundle.action}"
            ),
            log=lambda bundle, _receipt: (
                bundle_execution.BundleLog(action_type="buy", usd=deposit_usd)
                if bundle.action == "deposit"
                else None
            ),
            config=self.config,
            idempotency_store=self.store,
            on_submitted=on_submitted,
        )
        progress.steps.extend(result.steps)
        progress.receipts.extend(result.receipts)
        progress.messages.extend(result.messages)
        if not submitted:
            raise TransactionPlanError(
                f"the destination operation for {state.leg_id} was not submitted"
            )
        return submitted[-1]

    def _redeem(
        self,
        state: BridgeState,
        receive: bundle_execution.PlannedBundle,
        progress: BridgeProgress,
    ) -> BridgeState:
        """Submit ``receiveMessage`` alone: a transfer's mint stays in the Safe.

        The paymaster's charge is paid out of the mint in the same operation,
        so it must be bounded before anything is sent, as for a deposit.
        """
        tx_plan = bundle_execution.assemble_plan(
            [receive],
            f"Redeem the CCTP transfer {state.leg_id} into the Safe on "
            f"{_chain(state.destination_chain_id)}",
        )
        preparation = bundle_execution.prepare_plan(
            self.signer, tx_plan, self.config, self.store
        )
        progress.preparations.extend(preparation.preparations)
        progress.funding.extend(preparation.funding)
        charges = [item.max_gas_token_charge_raw for item in preparation.preparations]
        if not charges or any(charge is None for charge in charges):
            raise TransactionPlanError(
                f"the paymaster charge for {state.leg_id}'s destination operation "
                "could not be bounded, and a bridged transfer pays it out of the mint"
            )
        if preparation.blockers:
            raise TransactionPlanError("; ".join(preparation.blockers))

        submitted: list[BridgeState] = []

        def on_submitted(
            item: bundle_execution.PlannedBundle,
            _operation: bundle_execution.Operation,
            receipt: Receipt | None,
        ) -> None:
            operation_hash, transaction_hash = _hashes(receipt)
            after = state.advanced(
                "destination_submitted",
                destination_bundle_digest=item.bundle.digest,
                destination_operation_hash=operation_hash,
                destination_transaction_hash=transaction_hash,
                last_error=None,
            )
            save(self.store, after)
            submitted.append(after)

        result = bundle_execution.execute_plan(
            self.client,
            self.signer,
            tx_plan,
            stage="bridge",
            policy_result=self.policy_result,
            completion_key=lambda bundle: (
                f"bridge:{state.leg_id}:destination:{bundle.action}"
            ),
            log=lambda _bundle, _receipt: None,
            config=self.config,
            idempotency_store=self.store,
            on_submitted=on_submitted,
        )
        progress.steps.extend(result.steps)
        progress.receipts.extend(result.receipts)
        progress.messages.extend(result.messages)
        if not submitted:
            raise TransactionPlanError(
                f"the destination operation for {state.leg_id} was not submitted"
            )
        return submitted[-1]

    def _reconcile_destination(
        self, state: BridgeState, progress: BridgeProgress
    ) -> BridgeState:
        if state.destination_transaction_hash is not None:
            return state.advanced("completed", last_error=None)
        assert state.nonce is not None
        destination = self._chain(state.destination_chain_id)
        if state.destination_operation_hash is not None:
            try:
                receipt = self._operation_receipt(
                    state.destination_chain_id, state.destination_operation_hash
                )
            except UserOperationReverted as error:
                progress.messages.append(
                    f"bridged {state.leg_id}: the destination operation reverted, "
                    "which leaves the CCTP nonce unused; it is rebuilt with fresh "
                    "deposit calldata"
                )
                return state.advanced(
                    "destination_ready",
                    destination_operation_hash=None,
                    destination_bundle_digest=None,
                    deposit_amount_raw=None,
                    last_error=str(error),
                )
            if receipt is not None and not receipt.pending:
                return state.advanced(
                    "completed",
                    destination_transaction_hash=receipt.transaction_hash,
                    last_error=None,
                )
        if self._reader().nonce_used(
            state.destination_chain_id, destination.message_transmitter, state.nonce
        ):
            return state.advanced("completed", last_error=None)
        return state

    def _already_redeemed(
        self, state: BridgeState, progress: BridgeProgress
    ) -> BridgeState:
        token = self.token_for(state.destination_chain_id)
        key = funding.key_for(state.destination_chain_id, state.account, token.address)
        held, _notes = funding.read_balances((key,), self.config)
        balance = held.get(key)
        progress.messages.append(
            f"bridged {state.leg_id}: CCTP nonce {state.nonce} is already used on "
            f"{_chain(state.destination_chain_id)}, so the mint has landed and is not "
            "redeemed again; the Safe holds "
            + (
                "an unreadable amount of"
                if balance is None
                else f"{balance} raw units of"
            )
            + " USDC there"
            + (
                " — check `positions` to see whether it was deposited"
                if state.deposit
                else ""
            )
        )
        return state.advanced("completed", last_error=None)

    # --- helpers -------------------------------------------------------------

    def _operation_receipt(self, chain_id: int, operation_hash: str) -> Receipt | None:
        lookup = getattr(self.signer, "operation_receipt", None)
        if not callable(lookup):
            raise TransactionPlanError(
                "this signer cannot look up an earlier operation by hash, so a "
                "submitted bridge cannot be observed"
            )
        return lookup(chain_id, operation_hash)

    def _expectation(self, state: BridgeState) -> cctp.BurnExpectation:
        source = self._chain(state.source_chain_id)
        destination = self._chain(state.destination_chain_id)
        mismatched = [
            name
            for name, actual, wanted in (
                ("source domain", source.cctp_domain, state.source_domain),
                (
                    "destination domain",
                    destination.cctp_domain,
                    state.destination_domain,
                ),
                (
                    "source TokenMessenger",
                    source.token_messenger.casefold(),
                    state.source_token_messenger.casefold(),
                ),
            )
            if actual != wanted
        ]
        if mismatched:
            raise cctp.CctpValidationError(
                f"1Tx's CCTP configuration disagrees with the submitted burn on "
                f"{', '.join(mismatched)}"
            )
        return cctp.BurnExpectation(
            source_domain=state.source_domain,
            destination_domain=state.destination_domain,
            source_token_messenger=state.source_token_messenger,
            destination_token_messenger=destination.token_messenger,
            account=state.account,
            burn_token=state.source_token,
            amount_raw=state.burn_amount_raw,
            max_fee_raw=state.max_fee_raw,
            min_finality_threshold=state.min_finality_threshold,
        )

    def _chain(self, chain_id: int) -> CctpChainConfig:
        if self._cctp is None:
            self._cctp = cctp_config(self.client)
        found = self._cctp.chain(chain_id)
        if found is None:
            raise TransactionPlanError(
                f"1Tx reports no CCTP configuration for {_chain(chain_id)}"
            )
        return found

    def _circle(self) -> object:
        if self.circle is None:
            injected = _config_value(self.config, "circle_client")
            self.circle = (
                injected
                if injected is not None
                else CircleClient.from_config(self.config)
            )
        return self.circle

    def _reader(self) -> cctp.CctpChainReader:
        if self.reader is None:
            injected = _config_value(self.config, "cctp_reader")
            self.reader = (
                injected  # type: ignore[assignment]
                if injected is not None
                else cctp.RpcCctpReader(self.config)
            )
        assert self.reader is not None
        return self.reader


def cctp_config(client: object) -> CctpConfigResponse:
    fetch = getattr(client, "cctp_config", None)
    if not callable(fetch):
        raise TransactionPlanError("the 1Tx client cannot read the CCTP configuration")
    return fetch()


def load_states(
    store: object | None,
    legs: Sequence[tuple[int, str]],
) -> dict[int, BridgeState]:
    states: dict[int, BridgeState] = {}
    for index, instrument_id in legs:
        state = load(store, leg_id(index, instrument_id))
        if state is not None:
            states[index] = state
    return states


def route_note(
    leg_index: int,
    instrument_id: str,
    *,
    source_chain_id: int,
    destination_chain_id: int,
    amount_usdc: object,
    fast: bool,
    pinned: bool,
) -> str:
    why = (
        "sources from"
        if pinned
        else "holds too little USDC there, so it is funded from"
    )
    return (
        f"leg {leg_index} ({instrument_id}) deposits on "
        f"{_chain(destination_chain_id)} and {why} {_chain(source_chain_id)}: it "
        f"burns up to {amount_usdc} USDC over CCTP "
        f"({'fast' if fast else 'standard'} transfer); the deposit is built once "
        "Circle attests the burn, sized to the attested mint less Circle's fee and "
        "the destination paymaster's charge, and needs `execute --confirm` rerun "
        "until it settles"
    )


def state_note(state: BridgeState) -> str:
    return (
        f"{state.leg_id} is bridging from {_chain(state.source_chain_id)} to "
        f"{_chain(state.destination_chain_id)} and is {state.state}; it is not "
        "planned again, and `execute --confirm` advances it"
    )


def _waiting_message(state: BridgeState) -> str:
    if state.state == "source_submitted":
        return (
            f"bridged {state.leg_id}: source operation "
            f"{state.source_operation_hash} is not included yet"
        )
    if state.state == "awaiting_attestation":
        detail = state.attestation_status or "not_found"
        if state.delay_reason:
            detail += f", delay: {state.delay_reason}"
        return (
            f"bridged {state.leg_id}: waiting for Circle to attest the burn in "
            f"{state.source_transaction_hash} ({detail}); rerun {_rerun(state)}"
        )
    if state.state == "destination_submitted":
        return (
            f"bridged {state.leg_id}: destination operation "
            f"{state.destination_operation_hash} is not included yet"
        )
    return f"bridged {state.leg_id} is {state.state}"


_ORDER = (
    "bridge_planned",
    "source_submitted",
    "awaiting_attestation",
    "destination_ready",
    "destination_submitted",
    "completed",
)


def _rerun(state: BridgeState) -> str:
    return "`execute --confirm`" if state.deposit else "`bridge --confirm`"


def _furthest(state: BridgeState, stored: BridgeState | None) -> BridgeState:
    if stored is None or stored.state == "failed" or state.state == "failed":
        return state
    return stored if _ORDER.index(stored.state) > _ORDER.index(state.state) else state


def _hashes(receipt: Receipt | None) -> tuple[str | None, str | None]:
    """(operation hash, transaction hash); the latter only once included."""
    if receipt is None:
        return None, None
    operation = receipt.safe_tx_hash or receipt.transaction_hash
    return operation, None if receipt.pending else receipt.transaction_hash


def _safe_unattested(message: str) -> bytes | None:
    try:
        return cctp.unattested(message)
    except cctp.CctpValidationError:
        return None


def _chain(chain_id: int) -> str:
    return f"{chains.chain_name(chain_id)} (chain {chain_id})"


def _error_text(error: Exception) -> str:
    if isinstance(
        error, CircleError | TransactionPlanError | calldata.CalldataUnsupportedError
    ):
        return str(error)
    text = str(error)
    # Provider errors can quote RPC URLs, which carry API keys.
    if "http://" in text or "https://" in text:
        return type(error).__name__
    return f"{type(error).__name__}: {text}" if text else type(error).__name__


def _config_value(config: object | None, attr: str) -> object | None:
    if config is None:
        return None
    if isinstance(config, Mapping):
        return config.get(attr)
    return getattr(config, attr, None)


__all__ = [
    "BridgeProgress",
    "BridgeRunner",
    "BridgeUnavailableError",
    "burn_completion_key",
    "cctp_config",
    "check_route",
    "leg_id",
    "load_states",
    "planned_state",
    "require_cross_chain",
    "route_note",
    "state_note",
    "submitted_state",
    "supports_cross_chain",
]
