"""Unwinding one loop, on its own.

A loop cannot be taken off through the other exits.
:mod:`open_allocator.exec.withdraw` refuses a levered position — its collateral
is pledged against its debt, so withdrawing it would fail the pool's health
check — and :mod:`open_allocator.exec.rebalance` closes one only as a
consequence of a target allocation that no longer holds the leg. Taking a loop
off is its own decision, and asking for it should not require also saying where
the proceeds go.

The three rules in :mod:`loops` hold here as everywhere: the bundle goes out
whole or not at all, the venue's measurement is checked against the model
before signing, and a bundle that changes account-wide pool state is announced
with every other position it re-prices.

Policy is not re-checked against an allocation, because there is none: the
caller is removing a position, not proposing a book. The policy is still read,
since its caps are what the venue's measurement of the resulting state is held
against.
"""

from __future__ import annotations

from collections.abc import Sequence

from open_allocator.core import amounts
from open_allocator.core import positions as positions_core
from open_allocator.core.types import FrozenModel, Policy, TxBundle, TxPlan, Vault
from open_allocator.exec import bundle_execution, calldata
from open_allocator.exec import loops as loops_exec
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


class LoopCloseReport(FrozenModel):
    status: str
    loop_id: str
    account: str
    announcement: loops_exec.LoopAnnouncement
    plan: TxPlan
    steps: tuple[ExecutionStepReport, ...] = ()
    receipts: tuple[Receipt, ...] = ()
    gas_checks: tuple[GasCheck, ...] = ()
    preparations: tuple[WalletPreparation, ...] = ()
    funding: tuple[FundingRequirement, ...] = ()
    in_progress: bool = False
    messages: tuple[str, ...] = ()


def close(
    client: object,
    signer: Signer,
    loop_id: str,
    *,
    policy: Policy,
    known_instruments: Sequence[Vault],
    confirm: bool = False,
    config: object | None = None,
    idempotency_store: object | None = None,
) -> LoopCloseReport:
    """Plan, announce and — with ``confirm`` — send the close of one loop."""
    address = signer.address()
    rows = loops_exec.loop_rows(client)
    vaults, _skipped = loops_exec.loop_vaults(rows, known_instruments)
    target = _target(loop_id, vaults)

    steps, bundle = loops_exec.request_loop_bundle(
        client,
        target=target,
        action="close",
        account=address,
        amount=None,
        leverage=None,
        leg_index=0,
        config=config,
        caps=policy.caps,
    )

    announcement = loops_exec.announce(
        bundle,
        vaults=list(known_instruments) + vaults,
        row=next((row for row in rows if row.loop_id == loop_id), None),
        pool_positions=_pool_positions(client, address, target, vaults),
    )
    if not announcement.confirmable:
        raise TransactionPlanError("; ".join(announcement.blockers))

    plan = bundle_execution.assemble_plan(
        [bundle_execution.PlannedBundle(bundle=bundle, steps=steps)],
        f"close loop {loop_id}",
    )
    if not confirm:
        return LoopCloseReport(
            status="planned",
            loop_id=loop_id,
            account=address,
            announcement=announcement,
            plan=plan,
        )

    key = f"loop-close:{loop_id}:{bundle.digest}"
    result = bundle_execution.execute_plan(
        client,
        signer,
        plan,
        stage="withdraw",
        policy_result=_close_policy_result(),
        completion_key=lambda _bundle: key,
        log=lambda logged, _receipt: bundle_execution.BundleLog(
            action_type="loop_close",
            usd=_returned_usd(logged),
        ),
        config=config,
        idempotency_store=idempotency_store,
    )
    completed_keys = result.completed_keys
    if not result.plan.bundles and _store_completed(idempotency_store, key):
        completed_keys = (key,)
    report = LoopCloseReport(
        status=result.status,
        loop_id=loop_id,
        account=address,
        announcement=announcement,
        plan=result.plan,
        steps=result.steps,
        receipts=result.receipts,
        gas_checks=result.gas_checks,
        preparations=result.preparations,
        funding=result.funding,
        in_progress=result.in_progress,
        messages=result.messages,
    )
    _write_checkpoint(config, "loop-close", report, completed_keys=completed_keys)
    return report


def _target(loop_id: str, vaults: Sequence[Vault]) -> loops_exec.LoopTarget:
    """The loop being closed, as the screen describes it.

    A loop the screen no longer lists cannot be closed through this path: its
    legs and collateral token are what every check downstream is held against,
    and guessing them would defeat the checks rather than pass them.
    """
    wanted = loop_id.casefold()
    for vault in vaults:
        if vault.instrument_id.casefold() == wanted:
            return loops_exec.LoopTarget.from_vault(vault)
    raise calldata.CalldataValidationError(
        f"loop {loop_id} is not in the discovered loop screen; it cannot be "
        "closed through the loop path"
    )


def _pool_positions(
    client: object,
    address: str,
    target: loops_exec.LoopTarget,
    vaults: Sequence[Vault],
) -> tuple[loops_exec.PoolPosition, ...] | None:
    """What else the account holds in this loop's pool, or None if unreadable.

    ``None`` is not an error here — :func:`loops.announce` turns it into a
    blocker when the bundle changes account-wide state, which is where that
    judgement belongs.
    """
    if target.pool is None:
        return ()
    try:
        holdings = positions_core.read_positions(client, address).holdings
    except Exception:  # noqa: BLE001 - reported as an unknown pool, not raised
        return None
    return loops_exec.pool_positions(
        holdings,
        pool=target.pool,
        chain_id=target.chain_id,
        vaults=list(vaults),
    )


def _returned_usd(bundle: TxBundle) -> float | None:
    """What the unwind pays back, for the ledger.

    The allocation log needs ``usd`` or ``shares``, and a close has no share
    amount to give: it spends the pool's yield token and the receipt carries no
    logs. Its expected output is the equity coming back, which
    :func:`checkpoint._signed_usd` records as a negative, an unwind returning
    capital rather than deploying it.
    """
    if bundle.expected_out is None:
        return None
    try:
        return float(
            amounts.from_raw_units(bundle.expected_out, bundle.token_out.decimals)
        )
    except (ArithmeticError, ValueError, TypeError):
        return None


def _close_policy_result() -> object:
    """A close passes: the caps it could violate are the ones it removes.

    Every levered cap — ``min_health_factor``, ``max_gross_leverage``,
    ``max_book_gross_exposure``, ``min_depeg_buffer_bps`` — bounds risk the
    loop carries while it is open, which is why
    :func:`loops.check_measurement` does not apply them to a close either.
    :func:`bundle_execution.execute_plan` wants an allocation-level verdict,
    and an unwind has no allocation to give one.
    """
    from open_allocator.core.policy import PolicyResult

    return PolicyResult(ok=True, violations=())


__all__ = ["LoopCloseReport", "close"]
