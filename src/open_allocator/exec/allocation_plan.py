from __future__ import annotations

from typing import Literal

from pydantic import Field

from open_allocator.core import policy as policy_core
from open_allocator.core.types import Allocation, FrozenModel, TxPlan
from open_allocator.exec.bundle_execution import PlanPreparation
from open_allocator.exec.calldata import DepositToken
from open_allocator.exec.loops import LoopAnnouncement


class AllocationPlan(FrozenModel):
    """An allocation's deposit plan, complete enough to execute as it stands.

    What a dry run shows and what a confirmation executes: applying it submits
    these bundles and never plans again. Calldata within its minimum lifetime is
    re-quoted for the same leg, account and amount right before signing.
    """

    kind: Literal["execute"] = "execute"
    account: str
    allocation: Allocation
    policy_result: policy_core.PolicyResult
    plan: TxPlan
    preparation: PlanPreparation
    # Sizing and routing notes, one per deposit sized down, skipped or bridged.
    messages: tuple[str, ...] = ()
    # What each planned deposit actually spends, by leg index.
    deposit_usd: dict[int, float] = Field(default_factory=dict)
    loops: tuple[LoopAnnouncement, ...] = ()
    # The USDC each discovered chain deposits in, resolved while planning so
    # applying needs no discovery.
    deposit_tokens: dict[int, DepositToken] = Field(default_factory=dict)
