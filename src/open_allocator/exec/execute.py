from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, MutableMapping, Sequence
from dataclasses import replace
from typing import (
    TYPE_CHECKING,
    Any,
    Literal,
    Protocol,
    overload,
    runtime_checkable,
)

from pydantic import Field
from web3 import HTTPProvider, Web3

from open_allocator.core import checkpoint as checkpoint_core
from open_allocator.core import policy as policy_core
from open_allocator.core.state import backend_from_config
from open_allocator.core.types import (
    Allocation,
    FrozenModel,
    Policy,
    PolicyCaps,
    TxPlan,
    TxStep,
    Vault,
)
from open_allocator.exec import calldata, chains, funding, loops
from open_allocator.exec.bridge_state import BridgeState
from open_allocator.exec.erc4337_paymaster import (
    paymaster_cost_notes,
    submits_via_paymaster,
    validate_paymaster_preflight,
)
from open_allocator.exec.funding import FundingRequirement
from open_allocator.exec.loops import LoopAnnouncement
from open_allocator.exec.paymaster_types import AssumedBalance
from open_allocator.exec.signer import Receipt, Signer

if TYPE_CHECKING:
    # Imported for annotations only: both modules import this one.
    from open_allocator.exec.bundle_execution import PlannedBundle
    from open_allocator.exec.deposit_sizing import FittedPlan as DepositFit


class GasCheck(FrozenModel):
    chain_id: int
    ok: bool
    balance_wei: int | None = Field(default=None, ge=0)
    required_wei: int = Field(default=1, ge=0)
    message: str


class ExecutionStepReport(FrozenModel):
    leg_index: int
    step_index: int
    instrument_id: str
    status: Literal["planned", "sent", "skipped"]
    step: TxStep | None = None
    receipt: Receipt | None = None
    idempotency_key: str | None = None


class WalletPreparation(FrozenModel):
    """The wallet-aware estimate of one Safe operation, prepared but not sent.

    Keyed by the plan bundles it carries. The UserOperation itself is left out:
    its nonce, fees, and sponsorship expire before submission.
    """

    bundle_ids: tuple[str, ...] = Field(min_length=1)
    chain_id: int = Field(ge=1)
    sender: str
    # Whether the operation carries counterfactual Safe deployment.
    includes_deployment: bool
    # Final wallet gas for the whole operation — deployment, the bundles' calls,
    # and the paymaster approval — never the protocol-bundle simulation gas.
    call_gas_limit: int | None = Field(default=None, ge=0)
    verification_gas_limit: int | None = Field(default=None, ge=0)
    pre_verification_gas: int | None = Field(default=None, ge=0)
    paymaster_verification_gas_limit: int | None = Field(default=None, ge=0)
    paymaster_post_op_gas_limit: int | None = Field(default=None, ge=0)
    max_fee_per_gas: int | None = Field(default=None, ge=0)
    max_priority_fee_per_gas: int | None = Field(default=None, ge=0)
    paymaster_address: str | None = None
    paymaster_token: str | None = None
    # Whether the paymaster's token approval rides in front of the calls.
    paymaster_approval_included: bool | None = None
    # None when the adapter cannot bound the charge defensibly.
    max_gas_token_charge_raw: str | None = Field(default=None, pattern=r"^\d+$")
    # Balances assumed because the operation reverted against the real ones;
    # ``funding`` says what is missing.
    assumed_balances: tuple[AssumedBalance, ...] = ()
    simulation_revert: str | None = None


class ExecutionReport(FrozenModel):
    status: Literal["planned", "success", "in_progress", "failed"]
    policy_result: policy_core.PolicyResult
    plan: TxPlan
    steps: tuple[ExecutionStepReport, ...] = Field(default_factory=tuple)
    receipts: tuple[Receipt, ...] = Field(default_factory=tuple)
    gas_checks: tuple[GasCheck, ...] = Field(default_factory=tuple)
    preparations: tuple[WalletPreparation, ...] = Field(default_factory=tuple)
    # What the plan spends per chain and token, against the balance read.
    funding: tuple[FundingRequirement, ...] = Field(default_factory=tuple)
    in_progress: bool = False
    messages: tuple[str, ...] = Field(default_factory=tuple)
    # Bridged legs as this run left them; the idempotency store holds the
    # records a rerun resumes from.
    bridges: tuple[BridgeState, ...] = Field(default_factory=tuple)
    # One per levered operation: collateral, debt, leverage, modelled and
    # simulated health factor, kill switch, and what else it re-prices.
    loops: tuple[LoopAnnouncement, ...] = Field(default_factory=tuple)


class ExecutionError(RuntimeError):
    pass


class PolicyCheckFailed(ExecutionError):
    def __init__(self, result: policy_core.PolicyResult) -> None:
        self.result = result
        violations = ", ".join(
            f"{violation.rule}:{violation.entity}" for violation in result.violations
        )
        super().__init__(f"policy check failed: {violations}")


class TransactionPlanError(ExecutionError):
    pass


class GasPreflightError(ExecutionError):
    def __init__(self, checks: Sequence[GasCheck]) -> None:
        self.checks = tuple(checks)
        failures = "; ".join(check.message for check in self.checks if not check.ok)
        super().__init__(f"gas preflight failed: {failures}")


class ExecutionBroadcastError(ExecutionError):
    def __init__(
        self,
        message: str,
        *,
        leg_index: int,
        step_index: int,
        partial_report: ExecutionReport,
    ) -> None:
        self.leg_index = leg_index
        self.step_index = step_index
        self.partial_report = partial_report
        super().__init__(message)


@runtime_checkable
class IdempotencyStore(Protocol):
    def is_completed(self, key: str) -> bool: ...

    def mark_completed(self, key: str, value: object | None = None) -> None: ...


GasChecker = Callable[[str, int, str, object | None], GasCheck | bool]


# USDC is 6dp, so anything under a cent is rounding rather than money. Sourcing
# a leg from a chain holding dust produces an op that costs more gas than it moves.


@overload
def execute_allocation(
    client: object,
    signer: Signer,
    allocation: Allocation | Mapping[str, object],
    policy: Policy | Mapping[str, object],
    confirm: Literal[False] = False,
    known_instruments: Iterable[Vault | Mapping[str, object]] | None = None,
    config: object | None = None,
    idempotency_store: object | None = None,
) -> TxPlan: ...


@overload
def execute_allocation(
    client: object,
    signer: Signer,
    allocation: Allocation | Mapping[str, object],
    policy: Policy | Mapping[str, object],
    confirm: Literal[True],
    known_instruments: Iterable[Vault | Mapping[str, object]] | None = None,
    config: object | None = None,
    idempotency_store: object | None = None,
) -> ExecutionReport: ...


def execute_allocation(
    client: object,
    signer: Signer,
    allocation: Allocation | Mapping[str, object],
    policy: Policy | Mapping[str, object],
    confirm: bool = False,
    known_instruments: Iterable[Vault | Mapping[str, object]] | None = None,
    config: object | None = None,
    idempotency_store: object | None = None,
) -> ExecutionReport | TxPlan:
    allocation_model = _allocation(allocation)
    policy_model = _policy(policy)
    known: tuple[Vault | Mapping[str, object], ...] = tuple(known_instruments or ())
    address = signer.address()
    known = _with_effective_loop_parameters(
        client, known, allocation_model, address, config, idempotency_store
    )
    policy_result = policy_core.check(allocation_model, policy_model, known)
    if not policy_result.ok:
        raise PolicyCheckFailed(policy_result)

    vaults_by_id = _vaults_by_id(known)
    fitted = _calldata_deposit_plan(
        client,
        signer,
        address,
        allocation_model,
        vaults_by_id,
        config,
        idempotency_store,
        caps=policy_model.caps,
    )
    if not confirm:
        return fitted.plan
    return _execute_calldata_deposits(
        client,
        signer,
        fitted,
        allocation_model,
        vaults_by_id,
        policy_result,
        config,
        idempotency_store,
    )


def plan_calldata_allocation(
    client: object,
    signer: Signer,
    allocation: Allocation | Mapping[str, object],
    policy: Policy | Mapping[str, object],
    *,
    known_instruments: Iterable[Vault | Mapping[str, object]] | None = None,
    config: object | None = None,
    idempotency_store: object | None = None,
) -> DepositFit:
    """The calldata deposit plan a dry run reports, prepared and sized.

    What ``execute_allocation(confirm=False)`` plans, plus the preparation and
    sizing notes it leaves out, so a dry run need not prepare a second time.
    """
    allocation_model = _allocation(allocation)
    policy_model = _policy(policy)
    address = signer.address()
    known = _with_effective_loop_parameters(
        client,
        tuple(known_instruments or ()),
        allocation_model,
        address,
        config,
        idempotency_store,
    )
    policy_result = policy_core.check(allocation_model, policy_model, known)
    if not policy_result.ok:
        raise PolicyCheckFailed(policy_result)
    fitted = _calldata_deposit_plan(
        client,
        signer,
        address,
        allocation_model,
        _vaults_by_id(known),
        config,
        idempotency_store,
        caps=policy_model.caps,
    )
    return replace(fitted, policy_result=policy_result)


def _with_effective_loop_parameters(
    client: object,
    known: tuple[Vault | Mapping[str, object], ...],
    allocation: Allocation,
    address: str,
    config: object | None,
    idempotency_store: object | None,
) -> tuple[Vault | Mapping[str, object], ...]:
    """The known instruments, with each levered leg's pair priced by the venue.

    Policy judges a loop's health factor on the pair's effective threshold,
    which only the loop endpoint reads; the screen's pool default can only
    bound it from below. Legs already sent are not re-read.
    """
    vaults = _vaults_by_id(known)
    if not any(
        (vault := vaults.get(leg.instrument_id)) is not None and vault.is_levered
        for leg in allocation.legs
    ):
        return known
    done = [
        index
        for index, leg in enumerate(allocation.legs)
        if _store_completed(idempotency_store, _leg_key(index, leg.instrument_id))
    ]
    try:
        refined = loops.refine_levered_vaults(
            client,
            list(vaults.values()),
            allocation,
            account=address,
            config=config,
            skip=done,
            store=idempotency_store,
        )
    except (calldata.CalldataValidationError, calldata.CalldataAmountError) as error:
        raise TransactionPlanError(str(error)) from error
    return tuple(refined)


def _calldata_deposit_plan(
    client: object,
    signer: Signer,
    address: str,
    allocation: Allocation,
    vaults_by_id: Mapping[str, Vault],
    config: object | None,
    idempotency_store: object | None,
    *,
    caps: PolicyCaps | None = None,
) -> DepositFit:
    """A deposit plan built from calldata bundles, one per unfinished leg.

    A leg deposits from its own chain's USDC when that chain holds enough, and
    otherwise — or when its source is pinned elsewhere — burns USDC over CCTP
    on a chain that does; its deposit is settled later by ``exec.bridge``. A
    leg already bridging is not planned again. Each chain's spends are sized to
    leave the paymaster's maximum charge in the Safe; a chain short by more
    than that keeps full size and is a blocker.

    A levered leg is one loop ``open`` bundle on its own chain, spending its
    equity in the chain's USDC; it is never bridged, and it is announced with
    its pool-wide impact.
    """
    from open_allocator.exec import bridge, deposit_sizing

    calldata.ensure_calldata_supported(config)
    active = {
        index: state
        for index, state in bridge.load_states(
            idempotency_store,
            [(index, leg.instrument_id) for index, leg in enumerate(allocation.legs)],
        ).items()
        if state.active and state.state != "completed"
    }
    notes = [bridge.state_note(state) for state in active.values()]
    pinned = _pinned_source_chain_id(allocation, config)
    deposits: list[deposit_sizing.DepositRequest] = []
    for leg_index, leg in enumerate(allocation.legs):
        if _store_completed(idempotency_store, _leg_key(leg_index, leg.instrument_id)):
            continue
        if leg_index in active:
            continue
        vault = vaults_by_id.get(leg.instrument_id)
        if vault is None:
            raise TransactionPlanError(
                f"instrument {leg.instrument_id} is not in the discovered universe; "
                "its chain and deposit token are unknown"
            )
        token = calldata.deposit_token(vault.chain_id, vaults_by_id.values(), config)
        if vault.is_levered:
            deposits.append(_loop_request(leg_index, leg, vault, token, pinned, caps))
            continue
        request = deposit_sizing.DepositRequest(
            index=leg_index,
            instrument_id=leg.instrument_id,
            chain_id=vault.chain_id,
            token=token,
            wanted_raw=int(calldata.deposit_amount_raw(leg.usd, token)),
        )
        if pinned is not None and pinned != vault.chain_id:
            bridge.require_cross_chain(
                signer,
                f"leg {leg_index} ({leg.instrument_id}) is on chain {vault.chain_id} "
                f"but sources from chain {pinned}",
            )
            source = calldata.deposit_token(pinned, vaults_by_id.values(), config)
            request = deposit_sizing.DepositRequest(
                index=leg_index,
                instrument_id=leg.instrument_id,
                chain_id=pinned,
                token=source,
                wanted_raw=int(calldata.deposit_amount_raw(leg.usd, source)),
                bridge_to_chain_id=vault.chain_id,
            )
            notes.append(_bridge_note(request, leg.usd, config, pinned=True))
        deposits.append(request)

    if pinned is None:
        deposits, routed = _route_short_legs(
            client, signer, address, deposits, vaults_by_id, allocation, config
        )
        notes.extend(routed)

    def summary(ordered: Sequence[PlannedBundle]) -> str:
        return (
            f"Build calldata deposit bundles for {len(ordered)} allocation legs "
            f"across {sum(len(item.steps) for item in ordered)} transaction steps"
        )

    fitted = deposit_sizing.fit(
        client,
        signer,
        address,
        deposits=deposits,
        summary=summary,
        config=config,
        idempotency_store=idempotency_store,
    )
    burns = [bundle for bundle in fitted.plan.bundles if bundle.action == "bridge"]
    if burns:
        routes = bridge.cctp_config(client)
        for bundle in burns:
            bridge.check_route(
                bundle,
                [fitted.plan.steps[index] for index in bundle.step_indexes],
                routes,
            )
    announcements = _loop_announcements(
        client, address, fitted.plan, list(vaults_by_id.values())
    )
    if announcements:
        blockers = [item for loop in announcements for item in loop.blockers]
        fitted = replace(
            fitted,
            loops=announcements,
            preparation=fitted.preparation.model_copy(
                update={"blockers": (*fitted.preparation.blockers, *blockers)}
            ),
        )
    return replace(fitted, messages=(*notes, *fitted.messages))


def _loop_request(
    leg_index: int,
    leg: Any,
    vault: Vault,
    token: calldata.DepositToken,
    pinned: int | None,
    caps: PolicyCaps | None,
) -> Any:
    """The deposit request for a levered leg: a same-chain loop open."""
    from open_allocator.exec import deposit_sizing

    if pinned is not None and pinned != vault.chain_id:
        raise TransactionPlanError(
            f"leg {leg_index} ({leg.instrument_id}) is a loop on chain "
            f"{vault.chain_id} but sources from chain {pinned}; loops are built "
            "same-chain only"
        )
    if leg.leverage is None:
        raise TransactionPlanError(
            f"leg {leg_index} ({leg.instrument_id}) is levered but names no leverage"
        )
    target = loops.LoopTarget.from_vault(vault)
    if target.collateral_token.casefold() != token.address.casefold():
        raise TransactionPlanError(
            f"loop {leg.instrument_id} supplies {vault.asset} "
            f"({target.collateral_token}), not the chain's USDC; a loop's equity "
            "is spent in its collateral token and nothing here swaps into it"
        )
    return deposit_sizing.DepositRequest(
        index=leg_index,
        instrument_id=leg.instrument_id,
        chain_id=vault.chain_id,
        token=token,
        wanted_raw=int(loops.equity_raw(leg.usd, vault)),
        loop=deposit_sizing.LoopOpen(target=target, leverage=leg.leverage, caps=caps),
    )


def _loop_announcements(
    client: object,
    address: str,
    plan: TxPlan,
    vaults: Sequence[Vault],
) -> tuple[loops.LoopAnnouncement, ...]:
    """The levered announcement for every loop bundle in the plan.

    The account's positions are read once; when they cannot be, every loop
    that changes account-wide pool state carries a blocker instead of a guess.
    """
    bundles = [bundle for bundle in plan.bundles if bundle.is_loop]
    if not bundles:
        return ()
    from open_allocator.core import positions as positions_core

    try:
        rows = {row.loop_id.casefold(): row for row in loops.loop_rows(client)}
    except Exception:  # noqa: BLE001 - the kill switch says it is unavailable
        rows = {}
    try:
        holdings = positions_core.read_positions(client, address).holdings
    except Exception:  # noqa: BLE001 - an unread book is announced as unknown
        holdings = None
    announcements: list[loops.LoopAnnouncement] = []
    for bundle in bundles:
        assert bundle.loop is not None
        in_pool = (
            None
            if holdings is None
            else loops.pool_positions(
                holdings,
                pool=bundle.loop.pool,
                chain_id=bundle.chain_id,
                vaults=vaults,
            )
        )
        announcements.append(
            loops.announce(
                bundle,
                vaults=vaults,
                row=rows.get(bundle.loop.loop_id.casefold()),
                pool_positions=in_pool,
            )
        )
    return tuple(announcements)


def _route_short_legs(
    client: object,
    signer: Signer,
    address: str,
    deposits: Sequence[Any],
    vaults_by_id: Mapping[str, Vault],
    allocation: Allocation,
    config: object | None,
) -> tuple[list[Any], list[str]]:
    """Fund a leg its own chain cannot cover from a chain that can, over CCTP.

    Legs are taken in order against the Safe's USDC read on chain; a leg whose
    chain is short is moved, whole, to the best-funded CCTP chain that covers
    it. A leg nothing covers stays on its chain for the funding check to
    report. Needs a signer that can carry a bridge and 1Tx's CCTP chains; with
    neither, every leg stays where it is.
    """
    from open_allocator.exec import bridge, deposit_sizing

    if not deposits or not bridge.supports_cross_chain(signer):
        return list(deposits), []
    try:
        routes = bridge.cctp_config(client)
    except Exception as error:  # noqa: BLE001 - routing is an optimisation
        return list(deposits), [
            "cross-chain funding was not considered: the CCTP configuration could "
            f"not be read ({type(error).__name__})"
        ]
    tokens: dict[int, calldata.DepositToken] = {}
    for route in routes.supported_chains:
        try:
            tokens[route.chain_id] = calldata.deposit_token(
                route.chain_id, vaults_by_id.values(), config
            )
        except calldata.CalldataAmountError:
            continue
    for request in deposits:
        tokens.setdefault(request.chain_id, request.token)
    keys = {
        chain_id: funding.key_for(chain_id, address, token.address)
        for chain_id, token in tokens.items()
    }
    read, _unread = funding.read_balances(keys.values(), config)
    available = {chain_id: read.get(key) for chain_id, key in keys.items()}

    routed: list[Any] = []
    notes: list[str] = []
    for request in deposits:
        held = available.get(request.chain_id)
        if request.loop is not None:
            # A loop is built same-chain; a short chain is the funding check's
            # to report.
            if held is not None:
                available[request.chain_id] = held - request.wanted_raw
            routed.append(request)
            continue
        if held is None or held >= request.wanted_raw:
            if held is not None:
                available[request.chain_id] = held - request.wanted_raw
            routed.append(request)
            continue
        leg = allocation.legs[request.index]
        sources = [
            (amount, chain_id)
            for chain_id, amount in available.items()
            if chain_id != request.chain_id
            and chain_id in tokens
            and amount is not None
            and routes.chain(request.chain_id) is not None
            and routes.chain(chain_id) is not None
            and amount >= int(calldata.deposit_amount_raw(leg.usd, tokens[chain_id]))
        ]
        if not sources:
            available[request.chain_id] = held - request.wanted_raw
            routed.append(request)
            continue
        _amount, source = max(sources)
        token = tokens[source]
        bridged = deposit_sizing.DepositRequest(
            index=request.index,
            instrument_id=request.instrument_id,
            chain_id=source,
            token=token,
            wanted_raw=int(calldata.deposit_amount_raw(leg.usd, token)),
            bridge_to_chain_id=request.chain_id,
        )
        available[source] = int(available[source] or 0) - bridged.wanted_raw
        routed.append(bridged)
        notes.append(_bridge_note(bridged, leg.usd, config, pinned=False))
    return routed, notes


def _bridge_note(
    request: Any,
    usd: float,
    config: object | None,
    *,
    pinned: bool,
) -> str:
    from open_allocator.exec import bridge

    return bridge.route_note(
        request.index,
        request.instrument_id,
        source_chain_id=request.chain_id,
        destination_chain_id=request.bridge_to_chain_id,
        amount_usdc=usd,
        fast=bool(_config_value(config, "fast_transfer")),
        pinned=pinned,
    )


def _execute_calldata_deposits(
    client: object,
    signer: Signer,
    fitted: DepositFit,
    allocation: Allocation,
    vaults_by_id: Mapping[str, Vault],
    policy_result: policy_core.PolicyResult,
    config: object | None,
    idempotency_store: object | None,
) -> ExecutionReport:
    """Submit a calldata deposit plan, then advance every bridged leg.

    Same-chain deposits and CCTP burns go out one wallet operation per chain
    run, refused before anything is sent when the Safe does not hold what they
    require plus each operation's paymaster charge. A burn's leg is recorded as
    submitted before its completion is marked; that leg and any leg an earlier
    run left bridging are then taken as far as they can go now.
    """
    from open_allocator.exec import bridge, bridge_state, bundle_execution

    unconfirmable = [item for loop in fitted.loops for item in loop.blockers]
    if unconfirmable:
        raise TransactionPlanError("; ".join(unconfirmable))

    def token_for(chain_id: int) -> calldata.DepositToken:
        return calldata.deposit_token(chain_id, vaults_by_id.values(), config)

    planned: dict[str, BridgeState] = {}
    for bundle in fitted.plan.bundles:
        if bundle.action != "bridge":
            continue
        assert bundle.bridge is not None
        leg = allocation.legs[bundle.leg_index]
        destination = token_for(bundle.bridge.to_chain_id)
        steps = [fitted.plan.steps[index] for index in bundle.step_indexes]
        state = bridge.planned_state(
            bundle,
            source_token_messenger=steps[-1].to,
            wanted_deposit_raw=int(calldata.deposit_amount_raw(leg.usd, destination)),
        )
        bridge_state.save(idempotency_store, state)
        planned[bundle.bundle_id] = state

    def on_submitted(
        item: PlannedBundle,
        operation: Any,
        receipt: Receipt | None,
    ) -> None:
        loops.save_params(idempotency_store, item.bundle)
        state = planned.get(item.bundle.bundle_id)
        if state is not None:
            bridge_state.save(
                idempotency_store,
                bridge.submitted_state(state, item, operation, receipt),
            )

    result = bundle_execution.execute_plan(
        client,
        signer,
        fitted.plan,
        stage="execute",
        policy_result=policy_result,
        completion_key=lambda bundle: (
            bridge.burn_completion_key(bundle)
            if bundle.action == "bridge"
            else _leg_key(bundle.leg_index, bundle.instrument_id)
        ),
        log=lambda bundle, _receipt: (
            None
            if bundle.action == "bridge"
            else bundle_execution.BundleLog(
                action_type="buy",
                usd=fitted.deposit_usd.get(bundle.leg_index),
            )
        ),
        config=config,
        idempotency_store=idempotency_store,
        on_submitted=on_submitted,
    )

    states = bridge.load_states(
        idempotency_store,
        [(index, leg.instrument_id) for index, leg in enumerate(allocation.legs)],
    )
    runner = bridge.BridgeRunner(
        client=client,
        signer=signer,
        store=idempotency_store,
        token_for=token_for,
        policy_result=policy_result,
        config=config,
    )
    progress = runner.advance(
        [
            state
            for state in states.values()
            if state.active and state.state not in ("completed", "failed")
        ]
    )
    # Legs finished by an earlier run were never planned, but they are still
    # complete; the checkpoint is a snapshot of the whole allocation.
    done = {*result.completed_keys, *progress.completed_keys}
    earlier = tuple(
        _leg_key(index, leg.instrument_id)
        for index, leg in enumerate(allocation.legs)
        if _leg_key(index, leg.instrument_id) not in done
        and _store_completed(idempotency_store, _leg_key(index, leg.instrument_id))
    )
    in_progress = result.in_progress or progress.in_progress
    # A leg that failed after burning stays failed until someone looks at it.
    stuck = [
        state for state in states.values() if state.state == "failed" and state.active
    ]
    stuck_messages = tuple(
        f"bridged {state.leg_id} failed and is not redeemed automatically: "
        f"{state.last_error}; its burn is {state.source_transaction_hash} on "
        f"{chains.chain_name(state.source_chain_id)}"
        for state in stuck
    )
    if idempotency_store is None:
        bridges = tuple(progress.states)
    else:
        bridges = tuple(
            bridge.load_states(
                idempotency_store,
                [(state.leg_index, state.instrument_id) for state in states.values()],
            ).values()
        )
    if progress.failed or stuck:
        status: Literal["success", "in_progress", "failed"] = "failed"
    else:
        status = "in_progress" if in_progress else "success"
    report = ExecutionReport(
        status=status,
        policy_result=policy_result,
        plan=result.plan,
        steps=(*result.steps, *progress.steps),
        receipts=(*result.receipts, *progress.receipts),
        gas_checks=result.gas_checks,
        preparations=(*result.preparations, *progress.preparations),
        funding=(*result.funding, *progress.funding),
        in_progress=in_progress,
        messages=(
            *fitted.messages,
            *result.messages,
            *progress.messages,
            *stuck_messages,
        ),
        bridges=bridges,
        loops=fitted.loops,
    )
    _write_checkpoint(
        config,
        "execute",
        report,
        completed_keys=(*earlier, *result.completed_keys, *progress.completed_keys),
    )
    return report


def _pinned_source_chain_id(
    allocation: Allocation,
    config: object | None,
) -> int | None:
    configured = _config_value(config, "source_chain_id")
    if configured is not None:
        return int(configured)  # type: ignore[call-overload]
    for key in ("source_chain_id", "sourceChainId"):
        value = allocation.metadata.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def supports_batching(signer: object) -> bool:
    """Whether this signer can put several steps in one transaction.

    A smart account can; an EOA cannot, and must keep sending one at a time.
    """
    return callable(getattr(signer, "send_batch", None))


def pending_receipt_messages(
    receipts: Sequence[Receipt | Mapping[str, object]],
) -> tuple[str, ...]:
    """One message per receipt that was submitted but has no on-chain result.

    A Safe transaction awaiting co-signers and a user operation the bundler has
    not included yet are both real submissions that have settled nothing, so a
    report carrying either must not read as a completed spend.

    The idempotency key is still marked completed: the submission happened, and
    re-running must not propose or send it a second time. What changes is what
    the report claims — `in_progress`, never `success`.
    """
    messages: list[str] = []
    for receipt in receipts:
        # Signers hand back a Receipt; the mapping form is what a raw adapter
        # response looks like before it is validated.
        if not _attr(receipt, "pending"):
            continue
        tx_hash = _attr(receipt, "transaction_hash", "transactionHash")
        execution_status = _attr(receipt, "execution_status", "executionStatus")
        if execution_status == "safe_proposed":
            messages.append(
                f"Safe transaction {tx_hash} is proposed and awaiting "
                "threshold signatures/execution"
            )
        elif execution_status == "user_operation_submitted":
            messages.append(
                f"user operation {tx_hash} was submitted but is not confirmed on chain"
            )
        else:
            messages.append(
                f"transaction {tx_hash} was submitted but is not confirmed on chain"
            )
    return tuple(messages)


def _safe_rpc_chains_without_service(
    config: object | None,
    chain_ids: Sequence[int],
) -> tuple[int, ...]:
    """Plan chains a Safe could not propose on. Empty for every other signer."""
    if config is None:
        return ()
    if getattr(config, "account", None) != "safe":
        return ()
    if getattr(config, "submission", None) != "rpc":
        return ()

    named = getattr(config, "safe_chain_id", None)
    explicit = getattr(config, "safe_transaction_service_url", None)
    return tuple(
        chain_id
        for chain_id in chain_ids
        if chains.safe_tx_service_url(chain_id) is None
        and not (explicit and (named is None or int(named) == chain_id))
    )


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
    action_type: str


def _allocation(allocation: Allocation | Mapping[str, object]) -> Allocation:
    if isinstance(allocation, Allocation):
        return allocation
    return Allocation.model_validate(allocation)


def _policy(policy: Policy | Mapping[str, object]) -> Policy:
    if isinstance(policy, Policy):
        return policy
    return Policy.model_validate(policy)


def _vaults_by_id(
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


def _amount_usdc(value: float) -> str:
    return format(value, ".6f").rstrip("0").rstrip(".")


def _attr(obj: object, *names: str) -> object | None:
    if isinstance(obj, Mapping):
        for name in names:
            if name in obj:
                return obj[name]
        return None
    for name in names:
        if hasattr(obj, name):
            return getattr(obj, name)
    return None


def _config_value(config: object | None, attr: str) -> object | None:
    if config is None:
        return None
    if isinstance(config, Mapping):
        return config.get(attr)
    return getattr(config, attr, None)


def _paymaster_gas_message(note: Mapping[str, object]) -> str:
    chain_id = note["chain_id"]
    provider = note.get("provider")
    message = f"gas paid in USDC via {provider} on chain {chain_id}"
    # The provider's fee is inside the quoted rate, so naming the rate is the
    # only honest way to quote the cost — a flat percentage here would be a
    # number we made up.
    if note.get("exchange_rate") is not None:
        message += " (live quote, provider fee included in rate)"
    else:
        message += " (rate quoted at submission, provider fee included)"
    return message


def _preflight(
    address: str,
    step_refs: Sequence[_StepRef],
    config: object | None,
    idempotency_store: object | None,
) -> tuple[dict[int, str], tuple[GasCheck, ...]]:
    chain_ids = sorted(
        {
            ref.step.chain_id
            for ref in step_refs
            if not _store_completed(idempotency_store, ref.idempotency_key)
        }
    )
    rpc_urls: dict[int, str] = {}
    checks: list[GasCheck] = []

    if submits_via_paymaster(config):
        rpc_urls = validate_paymaster_preflight(config, chain_ids)
        for note in paymaster_cost_notes(config, chain_ids):
            checks.append(
                GasCheck(
                    chain_id=int(note["chain_id"]),
                    ok=True,
                    required_wei=0,
                    message=_paymaster_gas_message(note),
                )
            )
        return rpc_urls, tuple(checks)

    # A Safe proposing over RPC needs a Transaction Service per chain the plan
    # touches. Checked here, against the real chain ids, because config cannot
    # know them: the alternative is discovering it mid-execution, after earlier
    # chains have already been proposed.
    for chain_id in _safe_rpc_chains_without_service(config, chain_ids):
        checks.append(
            GasCheck(
                chain_id=chain_id,
                ok=False,
                message=(
                    f"no Safe Transaction Service for "
                    f"{chains.chain_name(chain_id)} (chain {chain_id}); "
                    f"set SAFE_TRANSACTION_SERVICE_URL with "
                    f"SAFE_CHAIN_ID={chain_id}, or drop the chain from the plan"
                ),
            )
        )

    for chain_id in chain_ids:
        try:
            rpc_url = chains.require_rpc_url(chain_id, config)
        except chains.MissingRPCError:
            checks.append(
                GasCheck(
                    chain_id=chain_id,
                    ok=False,
                    message=f"missing RPC for chain {chain_id}",
                )
            )
            continue

        rpc_urls[chain_id] = rpc_url
        try:
            checks.append(_run_gas_checker(address, chain_id, rpc_url, config))
        except Exception as error:
            checks.append(
                GasCheck(
                    chain_id=chain_id,
                    ok=False,
                    message=f"native gas check failed on chain {chain_id}: {error}",
                )
            )

    failed = tuple(check for check in checks if not check.ok)
    if failed:
        raise GasPreflightError(checks)
    return rpc_urls, tuple(checks)


def _run_gas_checker(
    address: str,
    chain_id: int,
    rpc_url: str,
    config: object | None,
) -> GasCheck:
    checker = _config_value(config, "gas_checker")
    if checker is None:
        return _default_gas_check(address, chain_id, rpc_url, config)

    check_method = getattr(checker, "check", None)
    result = (
        check_method(address, chain_id, rpc_url, config)
        if callable(check_method)
        else checker(address, chain_id, rpc_url, config)
    )
    if isinstance(result, GasCheck):
        return result
    if isinstance(result, bool):
        return GasCheck(
            chain_id=chain_id,
            ok=result,
            message=(
                f"native gas available on chain {chain_id}"
                if result
                else f"insufficient native gas on chain {chain_id}"
            ),
        )
    raise TypeError("gas_checker must return GasCheck or bool")


def _default_gas_check(
    address: str,
    chain_id: int,
    rpc_url: str,
    config: object | None,
) -> GasCheck:
    required_wei = int(_config_value(config, "min_native_gas_wei") or 1)
    balance_wei = int(Web3(HTTPProvider(rpc_url)).eth.get_balance(address))
    ok = balance_wei >= required_wei
    return GasCheck(
        chain_id=chain_id,
        ok=ok,
        balance_wei=balance_wei,
        required_wei=required_wei,
        message=(
            f"native gas available on chain {chain_id}"
            if ok
            else f"insufficient native gas on chain {chain_id}"
        ),
    )


def _store_completed(store: object | None, key: str) -> bool:
    if store is None:
        return False

    is_completed = getattr(store, "is_completed", None)
    if callable(is_completed):
        return bool(is_completed(key))

    completed = getattr(store, "completed", None)
    if isinstance(completed, set | frozenset | list | tuple):
        return key in completed

    if isinstance(store, Mapping):
        return bool(store.get(key))

    contains = getattr(store, "__contains__", None)
    if callable(contains):
        return bool(key in store)  # type: ignore[operator]
    return False


def _store_mark_completed(
    store: object | None,
    key: str,
    value: object | None = None,
) -> None:
    if store is None:
        return

    mark_completed = getattr(store, "mark_completed", None)
    if callable(mark_completed):
        try:
            mark_completed(key, value)
        except TypeError:
            mark_completed(key)
        return

    complete = getattr(store, "complete", None)
    if callable(complete):
        try:
            complete(key, value)
        except TypeError:
            complete(key)
        return

    if isinstance(store, MutableMapping):
        store[key] = value if value is not None else True
        return

    completed = getattr(store, "completed", None)
    add = getattr(completed, "add", None)
    if callable(add):
        add(key)


def _write_checkpoint(
    config: object | None,
    stage: str,
    report: object,
    *,
    completed_keys: Iterable[str] = (),
) -> None:
    backend = backend_from_config(config, needs="checkpoint_dir")
    if backend is None:
        return
    status = getattr(report, "status", None)
    checkpoint_status: checkpoint_core.CheckpointStatus
    if status == "success":
        checkpoint_status = "completed"
    elif status == "failed":
        checkpoint_status = "failed"
    else:
        checkpoint_status = "in_progress"
    checkpoint_core.write_checkpoint(
        stage,
        checkpoint_status,
        report,
        artifact_type=f"{stage}-report",
        completed_keys=completed_keys,
        backend=backend,
    )


def _append_allocation_log(
    config: object | None,
    ref: _StepRef,
    receipt: Receipt,
) -> None:
    if ref.step.kind == "approve":
        return
    backend = backend_from_config(config, needs="allocation_log_path")
    if backend is None:
        return
    checkpoint_core.write_allocation_log_entry(
        instrument_id=ref.instrument_id,
        chain_id=ref.step.chain_id,
        action_type=ref.action_type,
        tx_hash=receipt.transaction_hash,
        usd=ref.usd,
        shares=ref.shares,
        share_price=ref.share_price,
        backend=backend,
    )


def _leg_key(leg_index: int, instrument_id: str) -> str:
    return f"leg:{leg_index}:{instrument_id}"


def _walk_values(value: object) -> Iterable[object]:
    if isinstance(value, Mapping):
        for key, item in value.items():
            yield key
            yield from _walk_values(item)
        return
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        for item in value:
            yield from _walk_values(item)
        return
    yield value


__all__ = [
    "ExecutionBroadcastError",
    "ExecutionError",
    "ExecutionReport",
    "ExecutionStepReport",
    "GasCheck",
    "GasPreflightError",
    "IdempotencyStore",
    "PolicyCheckFailed",
    "TransactionPlanError",
    "WalletPreparation",
    "execute_allocation",
    "pending_receipt_messages",
    "plan_calldata_allocation",
]
