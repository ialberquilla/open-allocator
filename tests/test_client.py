import json
import os
from dataclasses import dataclass
from typing import Any

import httpx
import pytest

from open_allocator.exec.client import OneTxClient, OneTxHTTPError


@dataclass(frozen=True)
class ClientConfig:
    onetx_api_url: str = "https://1tx.test/api/v1/"
    onetx_api_key: str = "test-api-key"


def make_client(
    handler: httpx.MockTransport,
    *,
    max_retries: int = 0,
    backoff_factor: float = 0,
    sleep: Any = lambda _delay: None,
) -> OneTxClient:
    return OneTxClient(
        ClientConfig(),
        transport=handler,
        max_retries=max_retries,
        backoff_factor=backoff_factor,
        sleep=sleep,
    )


def assert_common_headers(request: httpx.Request) -> None:
    assert request.headers["x-api-key"] == "test-api-key"
    assert request.headers["content-type"] == "application/json"
    assert request.headers["x-request-id"]


def instrument_list_payload() -> dict[str, Any]:
    return {
        "data": [
            {
                "instrumentId": "morpho-base-usdc-1",
                "protocol": "morpho",
                "chainId": 8453,
                "tokenSymbol": "USDC",
                "yieldTokenSymbol": "mUSDC",
                "description": "Morpho USDC vault",
                "currentApy": 4.2,
                "tvl": 1_000_000,
                "isActive": True,
                "isStablecoin": True,
                "assetCategory": "USD",
            }
        ],
        "pagination": {"total": 1, "limit": 10, "offset": 0, "hasMore": False},
    }


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
                    "capBps": 10000,
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
                "instrumentIds": ["morpho-base-usdc-1"],
                "weightBps": 10000,
                "netApyPct": 4.1,
                "stabilityCV": 0.1,
                "rationale": "single core sleeve",
            }
        ],
        "headline": headline,
        "caveats": [],
    }


def read_json_body(request: httpx.Request) -> dict[str, Any]:
    return json.loads(request.content.decode())


def test_list_instruments_gets_filters_auth_and_parses() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/v1/instruments"
        assert request.url.params["chainId"] == "8453"
        assert request.url.params["protocol"] == "morpho"
        assert request.url.params["isActive"] == "true"
        assert request.url.params["isStablecoin"] == "true"
        assert request.url.params["assetCategory"] == "USD"
        assert request.url.params["sortBy"] == "apy"
        assert_common_headers(request)
        return httpx.Response(200, json=instrument_list_payload())

    client = make_client(httpx.MockTransport(handler))

    result = client.list_instruments(
        chain_id=8453,
        protocol="morpho",
        is_active=True,
        is_stablecoin=True,
        asset_category="USD",
        sort_by="apy",
        limit=10,
    )

    assert result.data[0].instrument_id == "morpho-base-usdc-1"
    assert result.data[0].chain_id == 8453
    assert result.data[0].current_apy == 4.2
    assert result.pagination.has_more is False


def test_metrics_bulk_gets_repeated_query_params_and_parses() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/v1/metrics/bulk"
        assert request.url.params.get_list("instrumentIds") == ["vault-a", "vault-b"]
        assert request.url.params["days"] == "14"
        assert_common_headers(request)
        return httpx.Response(
            200,
            json=[
                {
                    "instrumentId": "vault-a",
                    "metrics": [
                        {
                            "timestamp": "2026-01-01T00:00:00Z",
                            "tvlUsd": 1000,
                            "apy": 4.0,
                            "apyBase": 3.5,
                            "apyReward": 0.5,
                        }
                    ],
                }
            ],
        )

    client = make_client(httpx.MockTransport(handler))

    result = client.metrics_bulk(["vault-a", "vault-b"], days=14)

    assert result[0].instrument_id == "vault-a"
    assert result[0].metrics[0].tvl_usd == 1000
    assert result[0].metrics[0].apy_reward == 0.5


def test_instrument_analysis_gets_path_and_parses() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert (
            request.url.raw_path
            == b"/api/v1/instruments/vault%2Fwith%2Fslash/analysis"
        )
        assert_common_headers(request)
        return httpx.Response(
            200,
            json={
                "instrumentId": "vault/with/slash",
                "id": "vault/with/slash",
                "name": "Vault With Slash",
                "protocol": "morpho",
                "chainId": 8453,
                "yield": {
                    "currentApyPct": 4.1,
                    "apyMean30dPct": 4.0,
                    "rewardSharePct": 5.0,
                },
                "stability": {
                    "coefficientOfVariation": 0.1,
                    "yieldDrawdownPct": 0.2,
                    "downsideFreqPct": 1.0,
                    "trendPctPerWeek": 0.05,
                    "historyDays": 30,
                },
                "liquidity": {"tvlUsd": 1_000_000, "lowLiquidity": False},
                "priceRisk": False,
                "tier": "Core",
                "headline": "stable",
                "caveats": ["descriptive only"],
            },
        )

    client = make_client(httpx.MockTransport(handler))

    result = client.instrument_analysis("vault/with/slash")

    assert result.instrument_id == "vault/with/slash"
    assert result.yield_.current_apy_pct == 4.1
    assert result.liquidity.low_liquidity is False


def test_analyze_portfolio_posts_body_and_parses() -> None:
    allocations = [{"instrumentId": "morpho-base-usdc-1", "weightBps": 10000}]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/v1/portfolios/analyze"
        assert read_json_body(request) == {"allocations": allocations}
        assert_common_headers(request)
        return httpx.Response(200, json=portfolio_analysis_payload())

    client = make_client(httpx.MockTransport(handler))

    result = client.analyze_portfolio(allocations)

    assert result.resolved_count == 1
    assert result.yield_.net_apy_pct == 4.1
    assert result.concentration.by_protocol.items[0].key == "morpho"


def test_compare_portfolios_posts_body_and_parses() -> None:
    before = [{"instrumentId": "vault-a", "weightBps": 10000}]
    after = [{"instrumentId": "vault-b", "weightBps": 10000}]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/v1/portfolios/compare"
        assert read_json_body(request) == {"before": before, "after": after}
        assert_common_headers(request)
        return httpx.Response(
            200,
            json={
                "before": portfolio_analysis_payload("before"),
                "after": portfolio_analysis_payload("after"),
                "deltas": {"netApyPct": {"before": 4.1, "after": 4.5, "delta": 0.4}},
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
            },
        )

    client = make_client(httpx.MockTransport(handler))

    result = client.compare_portfolios(before, after)
    assert result.before.headline == "before"
    assert result.after.headline == "after"
    assert result.deltas["netApyPct"].delta == 0.4
    assert result.factor_deltas[0].delta_bps == -10000


def test_simulate_portfolio_posts_body_and_parses() -> None:
    body = {
        "allocations": [{"instrumentId": "vault-a", "weightBps": 10000}],
        "lookbackDays": 90,
        "principalUsd": 1000,
        "benchmark": "USD_INDEX",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/v1/portfolios/simulate"
        assert read_json_body(request) == body
        assert_common_headers(request)
        return httpx.Response(
            200,
            json={
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
                "headline": "outperformed",
                "caveats": ["descriptive only"],
            },
        )

    client = make_client(httpx.MockTransport(handler))

    result = client.simulate_portfolio(body)
    assert result.lookback_days == 90
    assert result.benchmark.label == "USD_INDEX"
    assert result.final_value_usd == 1010


def test_build_buy_posts_body_and_preserves_transaction_order_and_fields() -> None:
    body = {
        "userAddress": "0x0000000000000000000000000000000000000001",
        "instrumentId": "morpho-base-usdc-1",
        "amountUsdc": "100.00",
        "sourceChainId": 8453,
    }
    first_tx = {
        "to": "0x0000000000000000000000000000000000000002",
        "data": "0xabcdef",
        "value": "0",
        "chainId": 8453,
    }
    second_tx = {
        "to": "0x0000000000000000000000000000000000000003",
        "data": "0x123456",
        "value": "999",
        "chainId": 8453,
        "type": "deposit",
        "description": "Deposit into vault",
    }
    response_payload = {
        "operationId": "op-1",
        "sourceChainId": 8453,
        "destinationChainId": 8453,
        "isCrossChain": False,
        "transactions": [first_tx, second_tx],
        "quote": {"route": "direct"},
        "expiresAt": 123456789,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/v1/transactions/buy"
        assert read_json_body(request) == body
        assert_common_headers(request)
        return httpx.Response(200, json=response_payload)

    client = make_client(httpx.MockTransport(handler))

    result = client.build_buy(body)

    assert result == response_payload
    assert result["transactions"] == [first_tx, second_tx]
    assert list(result["transactions"][0]) == ["to", "data", "value", "chainId"]
    assert list(result["transactions"][1]) == [
        "to",
        "data",
        "value",
        "chainId",
        "type",
        "description",
    ]


def test_build_sell_posts_body_and_returns_raw_payload() -> None:
    body = {
        "userAddress": "0x0000000000000000000000000000000000000001",
        "instrumentId": "morpho-base-usdc-1",
        "yieldTokenAmount": "1.0",
    }
    response_payload = {
        "operationId": "op-sell",
        "transactions": [
            {
                "to": "0x0000000000000000000000000000000000000002",
                "data": "0xsell",
                "value": "0",
                "chainId": 8453,
            }
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/v1/transactions/sell"
        assert read_json_body(request) == body
        assert_common_headers(request)
        return httpx.Response(200, json=response_payload)

    client = make_client(httpx.MockTransport(handler))

    assert client.build_sell(body) == response_payload


def test_positions_posts_body_and_parses() -> None:
    body = {"address": "0x0000000000000000000000000000000000000001", "chainId": 8453}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/v1/positions"
        assert read_json_body(request) == body
        assert_common_headers(request)
        return httpx.Response(
            200,
            json={
                "address": body["address"],
                "chainId": 8453,
                "usdcBalance": "10.0",
                "positions": [
                    {
                        "instrumentId": "morpho-base-usdc-1",
                        "protocol": "morpho",
                        "symbol": "USDC",
                        "yieldTokenSymbol": "mUSDC",
                        "description": "Morpho vault",
                        "balance": "5.0",
                        "balanceRaw": "5000000",
                        "decimals": 6,
                        "shareBalance": "4.9",
                        "shareBalanceRaw": "4900000",
                        "shareDecimals": 6,
                        "currentApy": 4.2,
                        "yieldTokenAddress": (
                            "0x0000000000000000000000000000000000000002"
                        ),
                        "chainId": 8453,
                    }
                ],
            },
        )

    client = make_client(httpx.MockTransport(handler))

    result = client.positions(body)
    assert result.chain_id == 8453
    assert result.positions[0].instrument_id == "morpho-base-usdc-1"
    assert result.positions[0].share_balance == "4.9"


def test_balances_gets_address_path_and_parses() -> None:
    address = "0x0000000000000000000000000000000000000001"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == f"/api/v1/transactions/balances/{address}"
        assert_common_headers(request)
        return httpx.Response(
            200,
            json={
                "address": address,
                "balances": [
                    {
                        "chainId": 8453,
                        "chainName": "Base",
                        "usdcBalance": "10.0",
                        "usdcBalanceRaw": "10000000",
                    }
                ],
                "totalUsdcUsd": "10.0",
            },
        )

    client = make_client(httpx.MockTransport(handler))

    result = client.balances(address)
    assert result.balances[0].chain_id == 8453
    assert result.total_usdc_usd == "10.0"


def test_account_gets_owner_query_and_parses() -> None:
    owner = "0x0000000000000000000000000000000000000001"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/v1/account"
        assert request.url.params["ownerEoa"] == owner
        assert_common_headers(request)
        return httpx.Response(
            200,
            json={
                "accountAddress": "0x0000000000000000000000000000000000000002",
                "deployedChains": [8453],
                "authorizedChainIds": [8453, 42161],
                "grant": {
                    "status": "active",
                    "scope": {"spendCap": "1000"},
                    "expiresAt": "2026-01-01T00:00:00Z",
                },
            },
        )

    client = make_client(httpx.MockTransport(handler))

    result = client.account(owner)
    assert result.account_address == "0x0000000000000000000000000000000000000002"
    assert result.authorized_chain_ids == (8453, 42161)
    assert result.grant is not None
    assert result.grant.scope == {"spendCap": "1000"}


def test_retries_429_with_backoff_then_succeeds() -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert_common_headers(request)
        if calls == 1:
            return httpx.Response(429, json={"error": "rate limited"})
        return httpx.Response(200, json=instrument_list_payload())

    client = make_client(
        httpx.MockTransport(handler),
        max_retries=2,
        backoff_factor=0.5,
        sleep=sleeps.append,
    )

    result = client.list_instruments()
    assert result.data[0].instrument_id == "morpho-base-usdc-1"
    assert calls == 2
    assert sleeps == [0.5]


def test_retries_5xx_then_gives_up_with_typed_error() -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert_common_headers(request)
        return httpx.Response(503, text="unavailable")

    client = make_client(
        httpx.MockTransport(handler),
        max_retries=2,
        backoff_factor=0.25,
        sleep=sleeps.append,
    )

    with pytest.raises(OneTxHTTPError) as error:
        client.list_instruments()

    assert error.value.status_code == 503
    assert error.value.method == "GET"
    assert error.value.path == "/instruments"
    assert calls == 3
    assert sleeps == [0.25, 0.5]


@pytest.mark.integration
def test_live_list_instruments_smoke_skips_without_creds() -> None:
    api_url = os.environ.get("ONE_TX_API_URL")
    api_key = os.environ.get("ONE_TX_API_KEY")
    if not api_url or not api_key:
        pytest.skip(
            "ONE_TX_API_URL and ONE_TX_API_KEY are required for live smoke test"
        )

    with OneTxClient(ClientConfig(api_url, api_key), max_retries=1) as client:
        result = client.list_instruments(limit=1)

    assert result.pagination.total >= 0
