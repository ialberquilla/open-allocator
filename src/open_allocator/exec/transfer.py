"""Move the Safe's USDC between chains over CCTP, with no deposit at the end.

The ``bridge`` command. It is the bridged deposit leg of ``exec.bridge`` less
its deposit: 1Tx builds the source burn (``/bridge/calldata``), the burn goes
out as the source chain's operation, and once Circle attests it the
destination operation carries ``receiveMessage`` alone. The mint stays in the
Safe as the destination chain's USDC, where a later ``execute`` spends it —
for a loop, which is built same-chain only, that is the only way in.

The burn is sized like a deposit: down to what the source chain holds less
the paymaster's maximum charge, never up. A transfer is one record in its own
idempotency scope, so rerunning the same ``bridge --confirm`` resumes it until
it settles and never burns twice.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from open_allocator.core import amounts
from open_allocator.core import policy as policy_core
from open_allocator.core.types import Vault
from open_allocator.exec import (
    bridge,
    bridge_state,
    bundle_execution,
    calldata,
    chains,
    deposit_sizing,
)
from open_allocator.exec.execute import (
    ExecutionReport,
    TransactionPlanError,
    _write_checkpoint,
)
from open_allocator.exec.signer import Receipt

# The destination of a transfer is the Safe's USDC, not an instrument; the
# record and its bundles are keyed by this in the transfer's own scope.
TRANSFER_INSTRUMENT = "usdc"
TRANSFER_INDEX = 0
TRANSFER_LEG = bridge.leg_id(TRANSFER_INDEX, TRANSFER_INSTRUMENT)

# A transfer touches no instrument, so there is no allocation to judge.
_NO_ALLOCATION = policy_core.PolicyResult(ok=True)


def execute_transfer(
    client: object,
    signer: object,
    *,
    from_chain_id: int,
    to_chain_id: int,
    amount_usdc: float,
    known_instruments: Sequence[Vault],
    confirm: bool,
    config: object | None = None,
    idempotency_store: object | None = None,
) -> ExecutionReport:
    """Plan a transfer, or send it and take it as far as it can go now.

    Without ``confirm`` nothing is sent and the store is only read: a
    transfer already under way is reported as it stands, not planned again.
    """
    if from_chain_id == to_chain_id:
        raise ValueError("bridge source and destination chains must differ")
    bridge.require_cross_chain(signer, "a transfer is bridged")
    address = str(signer.address())  # type: ignore[attr-defined]

    def token_for(chain_id: int) -> calldata.DepositToken:
        return calldata.deposit_token(chain_id, known_instruments, config)

    existing = bridge_state.load(idempotency_store, TRANSFER_LEG)
    if existing is not None and existing.active:
        if existing.state in ("completed", "failed") or not confirm:
            return _existing_report(existing, confirm=confirm)
        return _advance(
            client,
            signer,
            existing,
            token_for=token_for,
            config=config,
            idempotency_store=idempotency_store,
        )

    source = token_for(from_chain_id)
    destination = token_for(to_chain_id)
    wanted_raw = int(calldata.deposit_amount_raw(amount_usdc, source))
    fitted = deposit_sizing.fit(
        client,
        signer,
        address,
        deposits=[
            deposit_sizing.DepositRequest(
                index=TRANSFER_INDEX,
                instrument_id=TRANSFER_INSTRUMENT,
                chain_id=from_chain_id,
                token=source,
                wanted_raw=wanted_raw,
                bridge_to_chain_id=to_chain_id,
            )
        ],
        summary=lambda _ordered: (
            f"Bridge {amount_usdc} USDC from {_chain(from_chain_id)} to "
            f"{_chain(to_chain_id)} over CCTP into the Safe"
        ),
        config=config,
        idempotency_store=idempotency_store,
    )
    burns = [item for item in fitted.plan.bundles if item.action == "bridge"]
    if not burns:
        raise TransactionPlanError(
            f"nothing to bridge: {'; '.join(fitted.messages) or 'no burn was built'}"
        )
    (burn,) = burns
    burn_steps = [fitted.plan.steps[index] for index in burn.step_indexes]
    bridge.check_route(burn, burn_steps, bridge.cctp_config(client))
    assert burn.bridge is not None
    notes = (
        _route_note(
            burn.amount, source, from_chain_id, to_chain_id, fast=burn.bridge.fast
        ),
        *fitted.messages,
    )

    if not confirm:
        return ExecutionReport(
            status="planned",
            policy_result=_NO_ALLOCATION,
            plan=fitted.plan,
            preparations=fitted.preparation.preparations,
            funding=fitted.preparation.funding,
            messages=(
                "dry-run only; no transactions broadcast",
                *notes,
                *fitted.preparation.messages,
                *fitted.preparation.blockers,
            ),
        )
    if fitted.preparation.blockers:
        raise TransactionPlanError("; ".join(fitted.preparation.blockers))
    if idempotency_store is None:
        raise TransactionPlanError(
            "a transfer spans several runs and needs an idempotency store to "
            "resume from; without one its burn could not be redeemed"
        )

    planned = bridge.planned_state(
        burn,
        source_token_messenger=burn_steps[-1].to,
        # What reaches the Safe is the attested mint; this only bounds it.
        wanted_deposit_raw=int(
            calldata.deposit_amount_raw(
                amounts.from_raw_units(int(burn.amount), source.decimals),
                destination,
            )
        ),
    ).model_copy(update={"deposit": False})
    bridge_state.save(idempotency_store, planned)

    def on_submitted(
        item: bundle_execution.PlannedBundle,
        operation: bundle_execution.Operation,
        receipt: Receipt | None,
    ) -> None:
        if item.bundle.action == "bridge":
            bridge_state.save(
                idempotency_store,
                bridge.submitted_state(planned, item, operation, receipt),
            )

    result = bundle_execution.execute_plan(
        client,
        signer,
        fitted.plan,
        stage="bridge",
        policy_result=_NO_ALLOCATION,
        completion_key=bridge.burn_completion_key,
        log=lambda _bundle, _receipt: None,
        config=config,
        idempotency_store=idempotency_store,
        on_submitted=on_submitted,
    )
    return _advance(
        client,
        signer,
        bridge_state.load(idempotency_store, TRANSFER_LEG),
        token_for=token_for,
        config=config,
        idempotency_store=idempotency_store,
        sent=result,
        notes=notes,
    )


def _advance(
    client: object,
    signer: object,
    state: bridge_state.BridgeState | None,
    *,
    token_for: bridge.TokenFor,
    config: object | None,
    idempotency_store: object | None,
    sent: bundle_execution.BundleExecution | None = None,
    notes: Sequence[str] = (),
) -> ExecutionReport:
    runner = bridge.BridgeRunner(
        client=client,
        signer=signer,
        store=idempotency_store,
        token_for=token_for,
        policy_result=_NO_ALLOCATION,
        config=config,
    )
    progress = runner.advance(
        [state] if state is not None and state.state != "bridge_planned" else []
    )
    in_progress = (sent is not None and sent.in_progress) or progress.in_progress
    status: Literal["success", "in_progress", "failed"]
    if progress.failed:
        status = "failed"
    else:
        status = "in_progress" if in_progress else "success"
    report = ExecutionReport(
        status=status,
        policy_result=_NO_ALLOCATION,
        plan=(
            sent.plan
            if sent is not None
            else bundle_execution.assemble_plan([], "Advance a CCTP transfer")
        ),
        steps=(*(sent.steps if sent else ()), *progress.steps),
        receipts=(*(sent.receipts if sent else ()), *progress.receipts),
        gas_checks=sent.gas_checks if sent else (),
        preparations=(*(sent.preparations if sent else ()), *progress.preparations),
        funding=(*(sent.funding if sent else ()), *progress.funding),
        in_progress=in_progress,
        messages=(
            *notes,
            *(sent.messages if sent else ()),
            *progress.messages,
        ),
        bridges=tuple(progress.states),
    )
    _write_checkpoint(
        config,
        "bridge",
        report,
        completed_keys=(
            *(sent.completed_keys if sent else ()),
            *progress.completed_keys,
        ),
    )
    return report


def _existing_report(
    state: bridge_state.BridgeState, *, confirm: bool
) -> ExecutionReport:
    if state.state == "completed":
        message = (
            f"this transfer already settled on {_chain(state.destination_chain_id)} "
            "and is not sent again; pass a different --ref to bridge the same "
            "amount again"
        )
    elif state.state == "failed":
        message = (
            f"this transfer failed after burning and is not redeemed "
            f"automatically: {state.last_error}; its burn is "
            f"{state.source_transaction_hash} on {_chain(state.source_chain_id)}"
        )
    else:
        message = (
            f"this transfer is already under way ({state.state}) and is not "
            "planned again; `bridge --confirm` with the same arguments advances it"
        )
    status: Literal["planned", "success", "in_progress", "failed"]
    if state.state == "completed":
        status = "success"
    elif state.state == "failed":
        status = "failed"
    else:
        status = "planned" if not confirm else "in_progress"
    return ExecutionReport(
        status=status,
        policy_result=_NO_ALLOCATION,
        plan=bundle_execution.assemble_plan([], "Report a CCTP transfer"),
        in_progress=state.state not in ("completed", "failed"),
        messages=(message,),
        bridges=(state,),
    )


def _route_note(
    burn_raw: str,
    token: calldata.DepositToken,
    from_chain_id: int,
    to_chain_id: int,
    *,
    fast: bool,
) -> str:
    return (
        f"burns {amounts.from_raw_units(int(burn_raw), token.decimals)} USDC on "
        f"{_chain(from_chain_id)} over CCTP "
        f"({'fast' if fast else 'standard'} transfer) and mints it, less Circle's "
        f"fee and the destination paymaster's charge, into the Safe on "
        f"{_chain(to_chain_id)}; rerun `bridge --confirm` with the same arguments "
        "until the transfer is completed"
    )


def _chain(chain_id: int) -> str:
    return f"{chains.chain_name(chain_id)} (chain {chain_id})"


__all__ = [
    "TRANSFER_INSTRUMENT",
    "TRANSFER_LEG",
    "execute_transfer",
]
