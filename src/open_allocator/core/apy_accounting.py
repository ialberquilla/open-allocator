"""Explicit advertised-versus-accruing APY accounting."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from open_allocator.core.types import Allocation, FrozenModel, Vault


class ApyAccounting(FrozenModel):
    advertised_blended_apy_pct: float | None
    measured_base_apy_pct: float | None
    measured_reward_apy_pct: float | None
    base_apy_coverage_bps: int
    reward_apy_coverage_bps: int
    unknown_base_weight_bps: int
    unknown_reward_weight_bps: int
    accruing_apy_pct: float | None
    apy_basis: Literal["base", "mixed_unknown"]

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
            accruing_apy_pct=None,
            apy_basis="mixed_unknown",
        )

    advertised_numerator = base_numerator = reward_numerator = 0.0
    advertised_usd = base_usd = reward_usd = 0.0
    for leg in legs:
        vault = by_id.get(leg.instrument_id)
        if vault is None:
            continue
        advertised_numerator += leg.usd * vault.apy
        advertised_usd += leg.usd
        if vault.apy_base is not None:
            base_numerator += leg.usd * vault.apy_base
            base_usd += leg.usd
        if vault.apy_reward is not None:
            reward_numerator += leg.usd * vault.apy_reward
            reward_usd += leg.usd

    base_coverage = _coverage_bps(base_usd, total)
    reward_coverage = _coverage_bps(reward_usd, total)
    advertised = advertised_numerator / total if advertised_usd == total else None
    measured_base = base_numerator / base_usd if base_usd else None
    measured_reward = reward_numerator / reward_usd if reward_usd else None
    return ApyAccounting(
        advertised_blended_apy_pct=_rounded(advertised),
        measured_base_apy_pct=_rounded(measured_base),
        measured_reward_apy_pct=_rounded(measured_reward),
        base_apy_coverage_bps=base_coverage,
        reward_apy_coverage_bps=reward_coverage,
        unknown_base_weight_bps=10_000 - base_coverage,
        unknown_reward_weight_bps=10_000 - reward_coverage,
        accruing_apy_pct=_rounded(measured_base) if base_coverage == 10_000 else None,
        apy_basis="base" if base_coverage == 10_000 else "mixed_unknown",
    )


def _coverage_bps(covered: float, total: float) -> int:
    return min(10_000, max(0, int(round(covered / total * 10_000))))


def _rounded(value: float | None) -> float | None:
    return round(value, 6) if value is not None else None


__all__ = ["ApyAccounting", "for_allocation"]
