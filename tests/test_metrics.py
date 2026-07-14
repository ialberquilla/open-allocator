from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import pytest

from open_allocator.core import metrics
from open_allocator.core.types import Unknown, Vault
from open_allocator.exec.client import OneTxClient


@dataclass(frozen=True)
class ClientConfig:
    onetx_api_url: str
    onetx_api_key: str


class StubClient:
    def __init__(
        self,
        metric_response: object,
        analyses: dict[str, object] | None = None,
    ) -> None:
        self.metric_response = metric_response
        self.analyses = analyses or {}
        self.calls: list[tuple[str, object]] = []

    def metrics_bulk(self, instrument_ids: tuple[str, ...], days: int) -> object:
        self.calls.append(("metrics_bulk", (instrument_ids, days)))
        return self.metric_response

    def instrument_analysis(self, instrument_id: str) -> object:
        self.calls.append(("instrument_analysis", instrument_id))
        return self.analyses.get(instrument_id, {})


def vault(instrument_id: str = "vault-a") -> Vault:
    return Vault(
        instrument_id=instrument_id,
        protocol="protocol-a",
        chain_id=1,
        asset="USDC",
        apy=4.0,
        tvl_usd=100,
    )


def metric_response(*points: dict[str, Any]) -> list[dict[str, object]]:
    return [{"instrumentId": "vault-a", "metrics": list(points)}]


def test_metrics_are_parsed_into_apy_and_tvl_series() -> None:
    client = StubClient(
        metric_response(
            {"timestamp": "2026-01-01T00:00:00Z", "apy": 4.0, "tvlUsd": 1000},
            {"timestamp": "2026-01-02T00:00:00Z", "apy": 5.0, "tvlUsd": 1100},
        ),
        analyses={"vault-a": {"liquidity": {"tvlUsd": 1200}}},
    )

    enriched = metrics.enrich(client, [vault()], days=2)

    assert enriched[0].apy_series == (4.0, 5.0)
    assert enriched[0].tvl_usd_series == (1000.0, 1100.0)
    assert enriched[0].tvl_usd == 1200


def test_apy_stability_cv_is_computed_for_known_series() -> None:
    client = StubClient(
        metric_response(
            {"apy": 2},
            {"apy": 4},
            {"apy": 4},
            {"apy": 4},
            {"apy": 5},
            {"apy": 5},
            {"apy": 7},
            {"apy": 9},
        )
    )

    enriched = metrics.enrich(client, [vault()], days=8)

    assert enriched[0].apy_stability == pytest.approx(0.4)


def test_absent_analysis_fields_become_unknown_not_default_numbers() -> None:
    client = StubClient(metric_response({"apy": 4}))

    enriched = metrics.enrich(client, [vault()], days=1)

    assert enriched[0].reward_dependence == Unknown
    assert enriched[0].liquidity == Unknown
    assert enriched[0].curator == Unknown
    assert enriched[0].oracle == Unknown
    assert enriched[0].fee == Unknown
    assert enriched[0].market_concentration == Unknown
    assert enriched[0].collateral_mix == Unknown


def test_absent_analysis_preserves_existing_known_risk_fields() -> None:
    original = vault().model_copy(
        update={
            "curator": "known-curator",
            "reward_dependence": 0.2,
            "liquidity": 1_000_000,
        }
    )
    client = StubClient(metric_response({"apy": 4}))

    enriched = metrics.enrich(client, [original], days=1)

    assert enriched[0].curator == "known-curator"
    assert enriched[0].reward_dependence == 0.2
    assert enriched[0].liquidity == 1_000_000


def test_analysis_reward_share_and_low_liquidity_are_derived_when_present() -> None:
    client = StubClient(
        metric_response({"apy": 4, "tvlUsd": 1000}),
        analyses={
            "vault-a": {
                "yield": {"rewardSharePct": 25},
                "liquidity": {"tvlUsd": 1000, "lowLiquidity": True},
            }
        },
    )

    enriched = metrics.enrich(client, [vault()], days=1)

    assert enriched[0].reward_dependence == pytest.approx(0.25)
    assert enriched[0].liquidity == 1.0


def test_enrichment_is_deterministic_for_fixed_responses_and_immutable() -> None:
    original = vault()
    metric_payload = metric_response(
        {"apy": 4, "apyReward": 1, "tvlUsd": 1000},
        {"apy": 6, "apyReward": 3, "tvlUsd": 1100},
    )
    analysis = {"liquidity": {"tvlUsd": 1200, "lowLiquidity": False}}

    first = metrics.enrich(
        StubClient(metric_payload, {"vault-a": analysis}),
        [original],
        days=2,
    )
    second = metrics.enrich(
        StubClient(metric_payload, {"vault-a": analysis}),
        [original],
        days=2,
    )

    assert first == second
    assert first[0] is not original
    assert original.apy_series == ()
    assert original.tvl_usd == 100


@pytest.mark.integration
def test_live_onetx_analysis_field_audit_skips_without_creds(
    record_property: pytest.RecordProperty,
) -> None:
    api_url = os.environ.get("ONE_TX_API_URL")
    api_key = os.environ.get("ONE_TX_API_KEY")
    if not api_url or not api_key:
        pytest.skip("ONE_TX_API_URL and ONE_TX_API_KEY are required for live audit")

    with OneTxClient(ClientConfig(api_url, api_key), max_retries=1) as client:
        instruments = client.list_instruments(limit=5).data
        vaults = [
            Vault(
                instrument_id=instrument.instrument_id,
                protocol=instrument.protocol,
                chain_id=instrument.chain_id,
                asset=(
                    instrument.token_symbol
                    or instrument.yield_token_symbol
                    or "Unknown"
                ),
                apy=instrument.current_apy or 0,
                tvl_usd=instrument.tvl or 0,
            )
            for instrument in instruments
        ]
        enriched = metrics.enrich(client, vaults, days=30)

    fields = (
        "apy_series",
        "tvl_usd_series",
        "apy_stability",
        "reward_dependence",
        "liquidity",
        "curator",
        "oracle",
        "fee",
        "market_concentration",
        "collateral_mix",
    )
    report_lines = ["# Live 1Tx field availability", ""]
    for field in fields:
        present = sum(
            1 for item in enriched if getattr(item, field) not in ((), Unknown)
        )
        report_lines.append(f"- `{field}`: {present}/{len(enriched)} present")

    report = "\n".join(report_lines)
    print(report)
    record_property("onetx_analysis_field_availability", report)
    assert enriched
