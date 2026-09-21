"""Explicit advertised-versus-accruing APY accounting."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from open_allocator.core.types import Allocation, FrozenModel, Vault


def priced_reward_apy(vault: Vault) -> float | None:
    """The reward APY we are willing to count for ``vault``, or ``None``.

    Counting a reward APY requires two separate facts, and the second is the
    one that is usually missing:

    1. It was **reported** at all. An absent ``apy_reward`` is unknown, exactly
       as an absent ``apy_base`` is.
    2. It was **priced at a traded quote**. Upstream feeds price a reward APY
       at the *emission schedule* — what the programme says it pays — not at
       what the reward token sells for. On a thin token those are not the same
       number, and the error is directional: it floats reward-heavy rows to
       the top of a ranking rather than scattering them. Leverage multiplies
       it.

    A **zero** reward APY is covered whatever basis was reported: there is no
    reward to misprice. Everything else needs ``reward_price_basis ==
    "traded"``, and an absent basis is treated as not-traded — absent reads as
    unpriced here, never as verified.
    """
    if vault.apy_reward is None:
        return None
    if vault.apy_reward == 0:
        return 0.0
    return vault.apy_reward if vault.reward_price_basis == "traded" else None


class ApyAccounting(FrozenModel):
    advertised_blended_apy_pct: float | None
    measured_base_apy_pct: float | None
    measured_reward_apy_pct: float | None
    base_apy_coverage_bps: int
    # Weight whose reward APY is both reported *and* priced at a traded quote
    # (or is zero). The two ways of missing it are reported separately below,
    # because "nobody told us" and "we were told a number we do not believe"
    # are different problems with different fixes.
    reward_apy_coverage_bps: int
    unknown_base_weight_bps: int
    unknown_reward_weight_bps: int
    unpriced_reward_weight_bps: int
    accruing_apy_pct: float | None
    # Base plus the reward we are willing to count, over the whole allocation.
    # None unless *both* coverages are complete — the same rule accruing_apy_pct
    # already follows, so an incomplete book reports nothing rather than an
    # optimistic headline.
    priced_blended_apy_pct: float | None
    apy_basis: Literal["base", "mixed_unknown"]
    reward_basis: Literal["traded", "none", "mixed_unpriced"]

    def warnings(self) -> tuple[str, ...]:
        warnings: list[str] = []
        if self.unknown_base_weight_bps:
            warnings.append(
                f"apy_base_unknown:weight_bps={self.unknown_base_weight_bps}:"
                "accruing-income and breakeven unavailable"
            )
        if self.unknown_reward_weight_bps:
            warnings.append(
                f"apy_reward_unknown:weight_bps={self.unknown_reward_weight_bps}:"
                "reward-inclusive decomposition incomplete"
            )
        if self.unpriced_reward_weight_bps:
            warnings.append(
                f"apy_reward_unpriced:weight_bps={self.unpriced_reward_weight_bps}:"
                "reward APY priced at emission, not at a traded quote — "
                "not counted"
            )
        return tuple(warnings)


def for_allocation(allocation: Allocation, vaults: Sequence[Vault]) -> ApyAccounting:
    """Measure APY coverage over allocation USD, never with a mixed fallback."""
    by_id = {vault.instrument_id: vault for vault in vaults}
    legs = [leg for leg in allocation.legs if leg.usd > 0]
    total = sum(leg.usd for leg in legs)
    if total <= 0:
        return ApyAccounting(
            advertised_blended_apy_pct=None,
            measured_base_apy_pct=None,
            measured_reward_apy_pct=None,
            base_apy_coverage_bps=0,
            reward_apy_coverage_bps=0,
            unknown_base_weight_bps=0,
            unknown_reward_weight_bps=0,
            unpriced_reward_weight_bps=0,
            accruing_apy_pct=None,
            priced_blended_apy_pct=None,
            apy_basis="mixed_unknown",
            reward_basis="mixed_unpriced",
        )

    advertised_numerator = base_numerator = reward_numerator = 0.0
    advertised_usd = base_usd = reward_usd = 0.0
    unpriced_usd = 0.0
    for leg in legs:
        vault = by_id.get(leg.instrument_id)
        if vault is None:
            continue
        advertised_numerator += leg.usd * vault.apy
        advertised_usd += leg.usd
        if vault.apy_base is not None:
            base_numerator += leg.usd * vault.apy_base
            base_usd += leg.usd
        priced_reward = priced_reward_apy(vault)
        if priced_reward is not None:
            reward_numerator += leg.usd * priced_reward
            reward_usd += leg.usd
        elif vault.apy_reward is not None:
            unpriced_usd += leg.usd

    base_coverage = _coverage_bps(base_usd, total)
    reward_coverage = _coverage_bps(reward_usd, total)
    unpriced_coverage = _coverage_bps(unpriced_usd, total)
    advertised = advertised_numerator / total if advertised_usd == total else None
    measured_base = base_numerator / base_usd if base_usd else None
    measured_reward = reward_numerator / reward_usd if reward_usd else None
    fully_priced = base_coverage == 10_000 and reward_coverage == 10_000
    return ApyAccounting(
        advertised_blended_apy_pct=_rounded(advertised),
        measured_base_apy_pct=_rounded(measured_base),
        measured_reward_apy_pct=_rounded(measured_reward),
        base_apy_coverage_bps=base_coverage,
        reward_apy_coverage_bps=reward_coverage,
        unknown_base_weight_bps=10_000 - base_coverage,
        unknown_reward_weight_bps=max(0, 10_000 - reward_coverage - unpriced_coverage),
        unpriced_reward_weight_bps=unpriced_coverage,
        accruing_apy_pct=_rounded(measured_base) if base_coverage == 10_000 else None,
        priced_blended_apy_pct=(
            _rounded((measured_base or 0.0) + (measured_reward or 0.0))
            if fully_priced
            else None
        ),
        apy_basis="base" if base_coverage == 10_000 else "mixed_unknown",
        reward_basis=_reward_basis(reward_coverage, measured_reward),
    )


def _reward_basis(
    reward_coverage_bps: int,
    measured_reward: float | None,
) -> Literal["traded", "none", "mixed_unpriced"]:
    if reward_coverage_bps < 10_000:
        return "mixed_unpriced"
    return "none" if not measured_reward else "traded"


def _coverage_bps(covered: float, total: float) -> int:
    return min(10_000, max(0, int(round(covered / total * 10_000))))


def _rounded(value: float | None) -> float | None:
    return round(value, 6) if value is not None else None


__all__ = ["ApyAccounting", "for_allocation", "priced_reward_apy"]
