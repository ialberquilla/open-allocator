from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from fractions import Fraction
from typing import Literal, Protocol

from open_allocator.core.types import Allocation, FrozenModel
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


class PortfolioScorecard(FrozenModel):
    label: Literal["descriptive-not-predictive"] = DESCRIPTIVE_LABEL
    analysis: PortfolioAnalysis
    concentration_flags: tuple[ConcentrationLimitFlag, ...]


class PortfolioComparison(FrozenModel):
    label: Literal["descriptive-not-predictive"] = DESCRIPTIVE_LABEL
    result: CompareResult
    deltas: dict[str, MetricDelta]


class PortfolioSimulation(FrozenModel):
    label: Literal["descriptive-not-predictive"] = DESCRIPTIVE_LABEL
    simulation: SimulationResult


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


def analyze(client: _PortfolioClient, allocation: Allocation) -> PortfolioScorecard:
    analysis = PortfolioAnalysis.model_validate(
        client.analyze_portfolio(_allocation_payload(allocation))
    )
    return PortfolioScorecard(
        analysis=analysis,
        concentration_flags=analysis.concentration.limit_flags,
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
) -> PortfolioSimulation:
    body: dict[str, object] = {
        "allocations": _allocation_payload(allocation),
        "principalUsd": allocation.total_usd,
    }
    if benchmark is not None:
        body["benchmark"] = benchmark

    simulation = SimulationResult.model_validate(client.simulate_portfolio(body))
    return PortfolioSimulation(simulation=simulation)


def _allocation_payload(allocation: Allocation) -> list[dict[str, object]]:
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

    return [
        {"instrumentId": leg.instrument_id, "weightBps": leg_bps}
        for leg, leg_bps in zip(allocation.legs, bps, strict=True)
    ]


__all__ = [
    "DESCRIPTIVE_LABEL",
    "PortfolioComparison",
    "PortfolioScorecard",
    "PortfolioSimulation",
    "analyze",
    "compare",
    "simulate",
]
