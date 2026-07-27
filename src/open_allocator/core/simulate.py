from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from fractions import Fraction
from typing import Literal, Protocol

from open_allocator.core.types import (
    UNKNOWN_SECTOR,
    Allocation,
    AllocationLeg,
    FrozenModel,
    Vault,
    sector_bucket,
)
from open_allocator.exec.client import (
    CompareResult,
    ConcentrationLimitFlag,
    MetricDelta,
    PortfolioAnalysis,
    SimulationResult,
)

DESCRIPTIVE_LABEL: Literal["descriptive-not-predictive"] = (
    "descriptive-not-predictive"
)


class SectorItem(FrozenModel):
    key: str
    weight_bps: int


class SectorConcentration(FrozenModel):
    """Sector (yield-source) breakdown of an allocation.

    Computed here rather than read off ``PortfolioAnalysis.concentration``:
    the 1Tx portfolio endpoint groups by protocol/chain/assetCategory/
    underlying and has no ``bySector``, so the sleeve view would otherwise wait
    on an API deploy. Grouping the same per-leg bps the API is sent keeps the
    two consistent.
    """

    items: tuple[SectorItem, ...]
    # Inverse HHI over sector weights: 1.0 = one sleeve, N = N equal sleeves.
    # This is the number the allocator must show before it signs.
    effective_sectors: float
    top_weight_bps: int
    # Weight sitting in instruments upstream has not classified. Reported
    # separately because it is not a sleeve — it is a hole in the measurement,
    # and it is counted as ONE bucket above so it can never flatter the count.
    unclassified_weight_bps: int


class PortfolioScorecard(FrozenModel):
    label: Literal["descriptive-not-predictive"] = DESCRIPTIVE_LABEL
    analysis: PortfolioAnalysis
    concentration_flags: tuple[ConcentrationLimitFlag, ...]
    # None when no universe was supplied — absent, not "diversified".
    sector_concentration: SectorConcentration | None = None


class PortfolioComparison(FrozenModel):
    label: Literal["descriptive-not-predictive"] = DESCRIPTIVE_LABEL
    result: CompareResult
    deltas: dict[str, MetricDelta]


class PortfolioSimulation(FrozenModel):
    label: Literal["descriptive-not-predictive"] = DESCRIPTIVE_LABEL
    simulation: SimulationResult
    sector_concentration: SectorConcentration | None = None


class _PortfolioClient(Protocol):
    def analyze_portfolio(
        self,
        allocations: Sequence[Mapping[str, object]],
    ) -> object: ...

    def compare_portfolios(
        self,
        before: Sequence[Mapping[str, object]],
        after: Sequence[Mapping[str, object]],
    ) -> object: ...

    def simulate_portfolio(self, body: Mapping[str, object]) -> object: ...


def sector_concentration(
    allocation: Allocation,
    vaults: Sequence[Vault],
) -> SectorConcentration:
    """Effective-sector count for an allocation, from the universe it came from.

    Legs whose instrument is absent from ``vaults`` are treated exactly like
    instruments upstream never classified: they join the unknown bucket. A leg
    we cannot look up is a leg whose sleeve we do not know, and this function
    must never turn missing information into apparent diversity.
    """
    sector_by_id = {vault.instrument_id: vault.sector for vault in vaults}
    weights: dict[str, int] = {}
    for leg, leg_bps in _leg_bps(allocation):
        key = sector_bucket(sector_by_id.get(leg.instrument_id))
        weights[key] = weights.get(key, 0) + leg_bps

    total = sum(weights.values())
    hhi = sum((value / total) ** 2 for value in weights.values()) if total else 0.0
    items = tuple(
        SectorItem(key=key, weight_bps=value)
        for key, value in sorted(weights.items(), key=lambda item: (-item[1], item[0]))
    )
    return SectorConcentration(
        items=items,
        effective_sectors=(1.0 / hhi) if hhi > 0 else 0.0,
        top_weight_bps=items[0].weight_bps if items else 0,
        unclassified_weight_bps=weights.get(UNKNOWN_SECTOR, 0),
    )


def analyze(
    client: _PortfolioClient,
    allocation: Allocation,
    vaults: Sequence[Vault] | None = None,
) -> PortfolioScorecard:
    analysis = PortfolioAnalysis.model_validate(
        client.analyze_portfolio(_allocation_payload(allocation))
    )
    return PortfolioScorecard(
        analysis=analysis,
        concentration_flags=analysis.concentration.limit_flags,
        sector_concentration=(
            sector_concentration(allocation, vaults) if vaults is not None else None
        ),
    )


def compare(
    client: _PortfolioClient,
    before: Allocation,
    after: Allocation,
) -> PortfolioComparison:
    result = CompareResult.model_validate(
        client.compare_portfolios(
            _allocation_payload(before),
            _allocation_payload(after),
        )
    )
    return PortfolioComparison(result=result, deltas=result.deltas)


def simulate(
    client: _PortfolioClient,
    allocation: Allocation,
    benchmark: object | None = None,
    vaults: Sequence[Vault] | None = None,
) -> PortfolioSimulation:
    body: dict[str, object] = {
        "allocations": _allocation_payload(allocation),
        "principalUsd": allocation.total_usd,
    }
    if benchmark is not None:
        body["benchmark"] = benchmark

    simulation = SimulationResult.model_validate(client.simulate_portfolio(body))
    return PortfolioSimulation(
        simulation=simulation,
        sector_concentration=(
            sector_concentration(allocation, vaults) if vaults is not None else None
        ),
    )


def _leg_bps(allocation: Allocation) -> list[tuple[AllocationLeg, int]]:
    """Per-leg weights in bps, largest-remainder rounded to sum to 10,000.

    The single place that turns float weights into integer bps, so the payload
    the API scores and the sector view reported next to it are the same split.
    """
    if not allocation.legs:
        raise ValueError("allocation must contain at least one leg")

    weights = [Fraction(Decimal(str(leg.weight))) for leg in allocation.legs]
    total_weight = sum(weights, Fraction(0))
    if total_weight <= 0:
        raise ValueError("allocation must have positive total weight")

    raw_bps = [weight / total_weight * 10_000 for weight in weights]
    bps = [value.numerator // value.denominator for value in raw_bps]
    remainder = 10_000 - sum(bps)
    order = sorted(
        range(len(allocation.legs)),
        key=lambda index: (
            -(raw_bps[index] - bps[index]),
            allocation.legs[index].instrument_id,
            index,
        ),
    )
    for index in order[:remainder]:
        bps[index] += 1

    return list(zip(allocation.legs, bps, strict=True))


def _allocation_payload(allocation: Allocation) -> list[dict[str, object]]:
    return [
        {"instrumentId": leg.instrument_id, "weightBps": leg_bps}
        for leg, leg_bps in _leg_bps(allocation)
    ]


__all__ = [
    "DESCRIPTIVE_LABEL",
    "PortfolioComparison",
    "PortfolioScorecard",
    "PortfolioSimulation",
    "SectorConcentration",
    "SectorItem",
    "analyze",
    "compare",
    "sector_concentration",
    "simulate",
]
