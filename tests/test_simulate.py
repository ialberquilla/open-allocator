from __future__ import annotations

from typing import Any

import pytest

from open_allocator.core.simulate import analyze, compare, simulate
from open_allocator.core.types import Allocation, AllocationLeg
from open_allocator.exec.client import (
    CompareResult,
    PortfolioAnalysis,
    SimulationResult,
)


def allocation(*weights: tuple[str, float], total_usd: float = 1_000) -> Allocation:
    return Allocation(
        legs=tuple(
            AllocationLeg(
                instrument_id=instrument_id,
                weight=weight,
                usd=round(total_usd * weight, 2),
            )
            for instrument_id, weight in weights
        ),
        total_usd=total_usd,
    )


def group_payload(key: str = "morpho") -> dict[str, Any]:
    return {
        "items": [{"key": key, "weightBps": 10000}],
        "effectiveGroups": 1,
        "topWeightBps": 10000,
    }


def portfolio_analysis_payload(headline: str = "portfolio ok") -> dict[str, Any]:
    return {
        "resolvedCount": 1,
        "warnings": ["descriptive only"],
        "yield": {
            "netApyPct": 4.1,
            "grossApyPct": 4.2,
            "weightedApyMean30dPct": 4.0,
        },
        "stability": {
            "coefficientOfVariation": 0.1,
            "yieldDrawdownPct": 0.2,
            "daysWithinBandPct": 98.0,
            "coveragePct": 100.0,
        },
        "diversification": {
            "effectivePositions": 1,
            "effectiveIndependentBets": None,
            "avgPairwiseCorrelation": None,
            "coverageBps": 10000,
        },
        "concentration": {
            "effectivePositions": 1,
            "hhi": 10000,
            "topWeightBps": 10000,
            "byProtocol": group_payload("morpho"),
            "byChain": group_payload("8453"),
            "byAssetCategory": group_payload("USD"),
            "byUnderlying": group_payload("USDC"),
            "limitFlags": [
                {
                    "dimension": "protocol",
                    "key": "morpho",
                    "weightBps": 10000,
                    "capBps": 5000,
                }
            ],
        },
        "tail": {
            "oneFailureCostBps": 10000,
            "sleeveWipeBps": 10000,
            "worstProtocolBps": 10000,
            "worstAssetCategoryBps": 10000,
            "weightedRewardSharePct": 5.0,
            "liquidity": {"weightedTvlUsd": 1_000_000, "illiquidWeightBps": 0},
        },
        "tranches": [
            {
                "name": "Core",
                "instrumentIds": ["vault-a"],
                "weightBps": 10000,
                "netApyPct": 4.1,
                "stabilityCV": 0.1,
                "rationale": "single core sleeve",
            }
        ],
        "headline": headline,
        "caveats": [],
    }


def simulation_payload() -> dict[str, Any]:
    return {
        "resolvedCount": 1,
        "warnings": [],
        "lookbackDays": 90,
        "principalUsd": 1000,
        "finalValueUsd": 1010,
        "realizedReturnPct": 1.0,
        "annualizedPct": 4.0,
        "blendedApyVolPct": 0.2,
        "maxYieldDrawdownPct": 0.1,
        "benchmark": {
            "kind": "index",
            "label": "USD_INDEX",
            "finalValueUsd": 1008,
            "annualizedPct": 3.2,
            "outperformancePct": 0.8,
        },
        "coveragePct": 100,
        "daysSimulated": 90,
        "headline": "descriptive backtest",
        "caveats": ["descriptive only"],
    }


class MockPortfolioClient:
    def __init__(self) -> None:
        self.analyze_allocations: list[dict[str, object]] | None = None
        self.compare_before: list[dict[str, object]] | None = None
        self.compare_after: list[dict[str, object]] | None = None
        self.simulate_body: dict[str, object] | None = None

    def analyze_portfolio(
        self,
        allocations: list[dict[str, object]],
    ) -> dict[str, Any]:
        self.analyze_allocations = allocations
        return portfolio_analysis_payload()

    def compare_portfolios(
        self,
        before: list[dict[str, object]],
        after: list[dict[str, object]],
    ) -> dict[str, Any]:
        self.compare_before = before
        self.compare_after = after
        return {
            "before": portfolio_analysis_payload("before"),
            "after": portfolio_analysis_payload("after"),
            "deltas": {
                "netApyPct": {"before": 4.1, "after": 4.5, "delta": 0.4},
                "oneFailureCostBps": {
                    "before": 10000,
                    "after": 5000,
                    "delta": -5000,
                },
            },
            "factorDeltas": [
                {
                    "dimension": "protocol",
                    "key": "morpho",
                    "beforeBps": 10000,
                    "afterBps": 0,
                    "deltaBps": -10000,
                }
            ],
            "headline": "changed",
        }

    def simulate_portfolio(self, body: dict[str, object]) -> dict[str, Any]:
        self.simulate_body = body
        return simulation_payload()


def test_allocation_maps_to_weight_bps_payload_summing_to_10000() -> None:
    client = MockPortfolioClient()
    book = allocation(
        ("vault-a", 0.333333),
        ("vault-b", 0.333333),
        ("vault-c", 0.333334),
    )

    analyze(client, book)

    assert client.analyze_allocations == [
        {"instrumentId": "vault-a", "weightBps": 3333},
        {"instrumentId": "vault-b", "weightBps": 3333},
        {"instrumentId": "vault-c", "weightBps": 3334},
    ]
    assert sum(item["weightBps"] for item in client.analyze_allocations) == 10000


def test_mocked_analyze_response_parses_to_typed_scorecard_and_surfaces_flags() -> None:
    result = analyze(MockPortfolioClient(), allocation(("vault-a", 1.0)))

    assert result.label == "descriptive-not-predictive"
    assert isinstance(result.analysis, PortfolioAnalysis)
    assert result.analysis.yield_.net_apy_pct == 4.1
    assert result.concentration_flags[0].dimension == "protocol"
    assert result.concentration_flags[0].cap_bps == 5000


def test_compare_returns_per_metric_deltas_for_before_after() -> None:
    client = MockPortfolioClient()
    before = allocation(("vault-a", 1.0))
    after = allocation(("vault-b", 0.4), ("vault-c", 0.6))

    result = compare(client, before, after)

    assert isinstance(result.result, CompareResult)
    assert result.deltas["netApyPct"].delta == pytest.approx(0.4)
    assert result.deltas["oneFailureCostBps"].delta == -5000
    assert client.compare_before == [{"instrumentId": "vault-a", "weightBps": 10000}]
    assert client.compare_after == [
        {"instrumentId": "vault-b", "weightBps": 4000},
        {"instrumentId": "vault-c", "weightBps": 6000},
    ]


def test_simulate_output_is_marked_descriptive() -> None:
    client = MockPortfolioClient()
    result = simulate(client, allocation(("vault-a", 1.0)), benchmark="USD_INDEX")

    assert result.label == "descriptive-not-predictive"
    assert isinstance(result.simulation, SimulationResult)
    assert "descriptive" in result.label
    assert "predictive" in result.label
    assert client.simulate_body == {
        "allocations": [{"instrumentId": "vault-a", "weightBps": 10000}],
        "principalUsd": 1_000,
        "benchmark": "USD_INDEX",
    }
