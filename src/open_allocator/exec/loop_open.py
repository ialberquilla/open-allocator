"""Opening one loop, on its own.

The only other way in is :mod:`open_allocator.exec.execute` with an allocation,
and an allocation is scored as if it were the whole book: one loop leg is 100%
of itself, so every concentration cap and the diversification floor refuse it
however small it is next to what is already held. Adding a loop to a held book
is its own decision, and asking for it should not require restating the book.

Policy is checked the way ``check-policy --against`` checks a top-up: the caps
are scored against the book this open produces — the held positions, each loop
at its equity, plus this one — and the per-cycle gates against the open alone.
The venue's measurement is then held against the levered caps before signing,
as on every loop path.

The three rules in :mod:`loops` hold here as everywhere: the bundle goes out
whole or not at all, the venue's measurement is checked against the model
before signing, and a bundle that changes account-wide pool state is announced
with every other position it re-prices.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Literal

from open_allocator.core import policy as policy_core
from open_allocator.core import positions as positions_core
from open_allocator.core.types import (
    Allocation,
    AllocationLeg,
    FrozenModel,
    Policy,
    TxPlan,
    Vault,
)
from open_allocator.exec import bundle_execution
from open_allocator.exec import loops as loops_exec
from open_allocator.exec.execute import (
    ExecutionStepReport,
    GasCheck,
    PolicyCheckFailed,
    TransactionPlanError,
    WalletPreparation,
    _store_completed,
    _write_checkpoint,
)
from open_allocator.exec.funding import FundingRequirement
from open_allocator.exec.loop_close import _pool_positions, _target
from open_allocator.exec.signer import Receipt, Signer


class LoopOpenReport(FrozenModel):
    status: str
    loop_id: str
    account: str
    equity_usd: float
    leverage: float
    policy_result: policy_core.PolicyResult
    announcement: loops_exec.LoopAnnouncement
    plan: TxPlan
    steps: tuple[ExecutionStepReport, ...] = ()
    receipts: tuple[Receipt, ...] = ()
    gas_checks: tuple[GasCheck, ...] = ()
    preparations: tuple[WalletPreparation, ...] = ()
    funding: tuple[FundingRequirement, ...] = ()
    in_progress: bool = False
    messages: tuple[str, ...] = ()


class LoopOpeningPlan(FrozenModel):
    """An open, complete enough to execute as it stands.

    What a dry run shows and what a confirmation executes: applying it submits
    this bundle and never plans again. ``loop_id``, ``equity_usd`` and
    ``leverage`` are what it was planned from, kept so applying logs the open
    and a re-check can score it again.
    """

    kind: Literal["loop-open"] = "loop-open"
    account: str
    loop_id: str
    equity_usd: float
    leverage: float
    policy_result: policy_core.PolicyResult
    announcement: loops_exec.LoopAnnouncement
    plan: TxPlan
    # Book read warnings from planning.
    messages: tuple[str, ...] = ()


def open_loop(
    client: object,
    signer: Signer,
    loop_id: str,
    *,
    equity_usd: float,
    leverage: float,
    policy: Policy,
    known_instruments: Sequence[Vault],
    confirm: bool = False,
    config: object | None = None,
    idempotency_store: object | None = None,
) -> LoopOpenReport:
    """Plan, policy-check, announce and — with ``confirm`` — send one open."""
    planned = plan_loop_opening(
        client,
        signer,
        loop_id,
        equity_usd=equity_usd,
        leverage=leverage,
        policy=policy,
        known_instruments=known_instruments,
        config=config,
    )
    if not confirm:
        return dry_run_report(planned)
    return apply_loop_opening_plan(
        client,
        signer,
        planned,
        config=config,
        idempotency_store=idempotency_store,
    )


def plan_loop_opening(
    client: object,
    signer: Signer,
    loop_id: str,
    *,
    equity_usd: float,
    leverage: float,
    policy: Policy,
    known_instruments: Sequence[Vault],
    config: object | None = None,
) -> LoopOpeningPlan:
    """The open ``loop-open`` would submit, policy-checked and announced, not sent."""
    if leverage < 1:
        raise TransactionPlanError(f"leverage {leverage} is below 1")
    address = signer.address()
    rows = loops_exec.loop_rows(client)
    vaults, _skipped = loops_exec.loop_vaults(rows, known_instruments)
    target = _target(loop_id, vaults, verb="opened")
    universe = list(known_instruments) + vaults

    book, warnings = loops_exec.read_book(client, address, config, rows=rows)
    _require_idle(book, target.chain_id, equity_usd)
    policy_result = _open_policy_result(
        loop_id, vaults, universe, book, equity_usd, leverage, policy
    )
    if not policy_result.ok:
        raise PolicyCheckFailed(policy_result)

    steps, bundle = loops_exec.request_loop_bundle(
        client,
        target=target,
        action="open",
        account=address,
        amount=loops_exec.equity_raw(equity_usd, _vault(loop_id, vaults)),
        leverage=leverage,
        leg_index=0,
        config=config,
        caps=policy.caps,
    )

    announcement = loops_exec.announce(
        bundle,
        vaults=universe,
        row=next((row for row in rows if row.loop_id == loop_id), None),
        pool_positions=_pool_positions(client, address, target, vaults),
    )
    if not announcement.confirmable:
        raise TransactionPlanError("; ".join(announcement.blockers))

    return LoopOpeningPlan(
        account=address,
        loop_id=loop_id,
        equity_usd=equity_usd,
        leverage=leverage,
        policy_result=policy_result,
        announcement=announcement,
        plan=bundle_execution.assemble_plan(
            [bundle_execution.PlannedBundle(bundle=bundle, steps=steps)],
            f"open loop {loop_id}",
        ),
        messages=tuple(warnings),
    )


def dry_run_report(planned: LoopOpeningPlan) -> LoopOpenReport:
    """The ``loop-open`` dry-run report."""
    return LoopOpenReport(
        status="planned",
        loop_id=planned.loop_id,
        account=planned.account,
        equity_usd=planned.equity_usd,
        leverage=planned.leverage,
        policy_result=planned.policy_result,
        announcement=planned.announcement,
        plan=planned.plan,
        messages=("dry-run only; no transactions broadcast", *planned.messages),
    )


def apply_loop_opening_plan(
    client: object,
    signer: Signer,
    planned: LoopOpeningPlan,
    *,
    config: object | None = None,
    idempotency_store: object | None = None,
) -> LoopOpenReport:
    """Execute exactly ``planned``, refusing it when it no longer describes the
    account: another signer, or the open sent since it was built.
    """
    address = signer.address()
    if address.casefold() != planned.account.casefold():
        raise TransactionPlanError(
            f"the plan was built for {planned.account}, but the signer is {address}"
        )
    key = opening_key(planned)
    if _store_completed(idempotency_store, key):
        raise TransactionPlanError(
            f"the plan is stale: the open of {planned.loop_id} was sent after it "
            "was built; plan again"
        )

    equity_usd = planned.equity_usd
    result = bundle_execution.execute_plan(
        client,
        signer,
        planned.plan,
        stage="execute",
        policy_result=planned.policy_result,
        completion_key=lambda _bundle: key,
        log=lambda _logged, _receipt: bundle_execution.BundleLog(
            action_type="loop_open",
            usd=equity_usd,
        ),
        config=config,
        idempotency_store=idempotency_store,
    )
    completed_keys = result.completed_keys
    if not result.plan.bundles and _store_completed(idempotency_store, key):
        completed_keys = (key,)
    report = LoopOpenReport(
        status=result.status,
        loop_id=planned.loop_id,
        account=address,
        equity_usd=equity_usd,
        leverage=planned.leverage,
        policy_result=planned.policy_result,
        announcement=planned.announcement,
        plan=result.plan,
        steps=result.steps,
        receipts=result.receipts,
        gas_checks=result.gas_checks,
        preparations=result.preparations,
        funding=result.funding,
        in_progress=result.in_progress,
        messages=(*result.messages, *planned.messages),
    )
    _write_checkpoint(config, "loop-open", report, completed_keys=completed_keys)
    return report


def check_opening_policy(
    client: object,
    planned: LoopOpeningPlan,
    policy: Policy,
    *,
    known_instruments: Sequence[Vault],
    config: object | None = None,
) -> policy_core.PolicyResult:
    """The planning check of ``planned``, against today's book and loop screen."""
    rows = loops_exec.loop_rows(client)
    vaults, _skipped = loops_exec.loop_vaults(rows, known_instruments)
    _target(planned.loop_id, vaults, verb="opened")
    book, _warnings = loops_exec.read_book(client, planned.account, config, rows=rows)
    return _open_policy_result(
        planned.loop_id,
        vaults,
        list(known_instruments) + vaults,
        book,
        planned.equity_usd,
        planned.leverage,
        policy,
    )


def opening_key(planned: LoopOpeningPlan) -> str:
    """The completion key of the planned bundle: marked once the open is sent."""
    digest = planned.plan.bundles[0].digest if planned.plan.bundles else ""
    return f"loop-open:{planned.loop_id}:{digest}"


def _vault(loop_id: str, vaults: Sequence[Vault]) -> Vault:
    return next(v for v in vaults if v.instrument_id.casefold() == loop_id.casefold())


def _open_policy_result(
    loop_id: str,
    vaults: Sequence[Vault],
    universe: Sequence[Vault],
    book: positions_core.Positions,
    equity_usd: float,
    leverage: float,
    policy: Policy,
) -> policy_core.PolicyResult:
    """The open's caps scored against the book it joins, its gates on itself."""
    leg = Allocation(
        legs=(
            AllocationLeg(
                instrument_id=_vault(loop_id, vaults).instrument_id,
                weight=1.0,
                usd=equity_usd,
                leverage=leverage,
            ),
        ),
        total_usd=equity_usd,
        metadata={},
    )
    return policy_core.check_incremental(
        leg,
        policy,
        list(universe),
        positions_core.held_usd_by_instrument(book),
    )


def _require_idle(
    book: positions_core.Positions, chain_id: int, equity_usd: float
) -> None:
    """The open is funded from idle USDC on the loop's chain, and only from it.

    Anything else would mean selling or bridging a held position, which is a
    rebalance, not an open.
    """
    idle = sum(
        (
            Decimal(balance.usdc_balance)
            for balance in book.idle_balances
            if balance.chain_id == chain_id
        ),
        Decimal("0"),
    )
    if Decimal(str(equity_usd)) > idle:
        raise TransactionPlanError(
            f"opening with {equity_usd} USDC needs that much idle on chain "
            f"{chain_id}; the wallet holds {idle}"
        )


__all__ = [
    "LoopOpenReport",
    "LoopOpeningPlan",
    "apply_loop_opening_plan",
    "check_opening_policy",
    "dry_run_report",
    "open_loop",
    "opening_key",
    "plan_loop_opening",
]
