import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from open_allocator.exec.client import (
    BridgeCalldataQuery,
    InstrumentCalldataQuery,
    InstrumentCalldataResponse,
    OneTxClient,
    OneTxDecodeError,
    OneTxHTTPError,
)

FIXTURES = Path(__file__).parent / "fixtures"


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
                "tokenAddress": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                "tokenSymbol": "USDC",
                "tokenDecimals": 6,
                "yieldTokenAddress": "0x944766f715b51967E56aFdE5f0Aa76cEaCc9E7f9",
                "yieldTokenSymbol": "mUSDC",
                "yieldTokenDecimals": 18,
                "description": "Morpho USDC vault",
                "currentApy": 4.2,
                "apyBase": 3.7,
                "apyReward": 0.5,
                "rewardTokens": ["0x0000000000000000000000000000000000000001"],
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


def reward_payload(name: str = "rewards-bearing.json") -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


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
    assert result.data[0].apy_base == 3.7
    assert result.data[0].apy_reward == 0.5
    assert result.data[0].reward_tokens == (
        "0x0000000000000000000000000000000000000001",
    )
    assert result.pagination.has_more is False


def test_instrument_parses_token_address_and_decimal_pairs() -> None:
    payload = instrument_list_payload()

    result = make_client(
        httpx.MockTransport(lambda _request: httpx.Response(200, json=payload))
    ).list_instruments()

    instrument = result.data[0]
    assert instrument.token_address == "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
    assert instrument.token_decimals == 6
    assert instrument.yield_token_address == (
        "0x944766f715b51967E56aFdE5f0Aa76cEaCc9E7f9"
    )
    assert instrument.yield_token_decimals == 18


def test_instrument_token_metadata_is_optional_for_discovery() -> None:
    payload = instrument_list_payload()
    for key in (
        "tokenAddress",
        "tokenDecimals",
        "yieldTokenAddress",
        "yieldTokenDecimals",
    ):
        payload["data"][0].pop(key)

    result = make_client(
        httpx.MockTransport(lambda _request: httpx.Response(200, json=payload))
    ).list_instruments()

    assert result.data[0].token_address is None
    assert result.data[0].token_decimals is None
    assert result.data[0].yield_token_address is None
    assert result.data[0].yield_token_decimals is None


def test_instrument_split_fields_are_optional_and_preserve_null_vs_zero() -> None:
    old_payload = instrument_list_payload()
    instrument = old_payload["data"][0]
    instrument.pop("apyBase")
    instrument["apyReward"] = None
    instrument.pop("rewardTokens")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=old_payload)

    result = make_client(httpx.MockTransport(handler)).list_instruments()

    assert result.data[0].current_apy == 4.2
    assert result.data[0].apy_base is None
    assert result.data[0].apy_reward is None
    assert result.data[0].reward_tokens is None

    zero_reward = instrument_list_payload()
    zero_reward["data"][0]["apyReward"] = 0
    result = make_client(
        httpx.MockTransport(lambda _request: httpx.Response(200, json=zero_reward))
    ).list_instruments()

    assert result.data[0].apy_reward == 0


def test_instrument_accepts_null_reward_tokens() -> None:
    payload = instrument_list_payload()
    payload["data"][0]["rewardTokens"] = None

    result = make_client(
        httpx.MockTransport(lambda _request: httpx.Response(200, json=payload))
    ).list_instruments()

    assert result.data[0].reward_tokens is None


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
            request.url.raw_path == b"/api/v1/instruments/vault%2Fwith%2Fslash/analysis"
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


def test_positions_gets_query_and_parses() -> None:
    body = {"address": "0x0000000000000000000000000000000000000001", "chainId": 8453}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/v1/positions"
        assert dict(request.url.params) == {
            "address": body["address"],
            "chainId": str(body["chainId"]),
        }
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


def test_rewards_gets_query_parses_raw_amounts_and_preserves_no_route() -> None:
    wallet = "0x1111111111111111111111111111111111111111"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/v1/rewards"
        assert dict(request.url.params) == {"wallet": wallet, "chainId": "8453"}
        assert_common_headers(request)
        return httpx.Response(200, json=reward_payload())

    result = make_client(httpx.MockTransport(handler)).rewards(wallet, 8453)

    assert result.wallet == wallet
    assert result.errors == ("chain 42161: provider unavailable",)
    assert result.rewards[0].claimable_amount == "750"
    assert result.rewards[0].claimable_amount_normalized == "0.00075"
    assert result.rewards[0].pending_amount_normalized == "0.00005"
    assert result.rewards[0].swap.status == "no-route"
    assert result.rewards[0].claim.data == "0xabcdef"


def test_rewards_accepts_empty_results_without_a_chain_filter() -> None:
    wallet = "0x1111111111111111111111111111111111111111"

    def handler(request: httpx.Request) -> httpx.Response:
        assert dict(request.url.params) == {"wallet": wallet}
        return httpx.Response(200, json=reward_payload("rewards-empty.json"))

    result = make_client(httpx.MockTransport(handler)).rewards(wallet)

    assert result.rewards == ()


def test_reward_normalization_is_exact_for_large_integer_amounts() -> None:
    payload = reward_payload()
    payload["rewards"][0]["claimableAmount"] = "123456789012345678901234567890"
    payload["rewards"][0]["rewardToken"]["decimals"] = 18
    client = make_client(
        httpx.MockTransport(lambda _request: httpx.Response(200, json=payload))
    )

    result = client.rewards(
        "0x1111111111111111111111111111111111111111",
        8453,
    )

    assert (
        result.rewards[0].claimable_amount_normalized
        == "123456789012.34567890123456789"
    )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda payload: payload.update(
                wallet="0x9999999999999999999999999999999999999999"
            ),
            "wallet",
        ),
        (
            lambda payload: payload["rewards"][0].update(chainId=42161),
            "requested chain",
        ),
        (
            lambda payload: payload["rewards"][0]["rewardToken"].update(chainId=42161),
            "reward token chain",
        ),
    ],
)
def test_rewards_rejects_wallet_and_chain_mismatches(
    mutation: Any,
    message: str,
) -> None:
    payload = reward_payload()
    mutation(payload)
    client = make_client(
        httpx.MockTransport(lambda _request: httpx.Response(200, json=payload))
    )

    with pytest.raises(OneTxDecodeError, match=message):
        client.rewards("0x1111111111111111111111111111111111111111", 8453)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("claimableAmount", "-1"),
        ("claimableAmount", "1.5"),
        ("pendingAmount", "nan"),
    ],
)
def test_rewards_rejects_non_integer_raw_amounts(field: str, value: str) -> None:
    payload = reward_payload()
    payload["rewards"][0][field] = value
    client = make_client(
        httpx.MockTransport(lambda _request: httpx.Response(200, json=payload))
    )

    with pytest.raises(ValueError, match=field):
        client.rewards("0x1111111111111111111111111111111111111111", 8453)


def test_rewards_rejects_negative_expiry() -> None:
    payload = reward_payload()
    payload["expiresAt"] = -1
    client = make_client(
        httpx.MockTransport(lambda _request: httpx.Response(200, json=payload))
    )

    with pytest.raises(ValueError, match="expiresAt"):
        client.rewards("0x1111111111111111111111111111111111111111")


def test_rewards_rejects_non_numeric_expiry_from_the_wire() -> None:
    payload = reward_payload()
    payload["expiresAt"] = "4102444800"
    client = make_client(
        httpx.MockTransport(lambda _request: httpx.Response(200, json=payload))
    )

    with pytest.raises(ValueError, match="expiresAt"):
        client.rewards("0x1111111111111111111111111111111111111111")


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


CALLDATA_INSTRUMENT_ID = "0x" + "ab" * 32
CALLDATA_SAFE = "0x1111111111111111111111111111111111111111"


def calldata_payload(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def calldata_client(payload: object) -> OneTxClient:
    return make_client(
        httpx.MockTransport(lambda _request: httpx.Response(200, json=payload))
    )


def request_instrument_calldata(client: OneTxClient) -> InstrumentCalldataResponse:
    return client.instrument_calldata(
        CALLDATA_INSTRUMENT_ID,
        {"action": "deposit", "account": CALLDATA_SAFE, "amount": "100250000"},
    )


def test_instrument_calldata_gets_path_and_query_aliases() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert (
            request.url.path == f"/api/v1/instruments/{CALLDATA_INSTRUMENT_ID}/calldata"
        )
        assert dict(request.url.params) == {
            "action": "deposit",
            "account": CALLDATA_SAFE,
            "amount": "100250000",
            "slippageBps": "75",
        }
        assert "executor" not in request.url.params
        assert request.content == b""
        assert_common_headers(request)
        return httpx.Response(
            200,
            json=calldata_payload("calldata-instrument-deposit-swap.json"),
        )

    result = make_client(httpx.MockTransport(handler)).instrument_calldata(
        CALLDATA_INSTRUMENT_ID,
        InstrumentCalldataQuery(
            action="deposit",
            account=CALLDATA_SAFE,
            amount="100250000",
            slippage_bps=75,
        ),
    )

    assert [call.type for call in result.calls] == [
        "approve",
        "swap",
        "approve",
        "approve",
        "deposit",
    ]
    assert result.calls[0].chain_id == 8453
    assert result.token_in.decimals == 6
    assert result.deposit_amount == "100180000000000000000"
    assert result.expires_at == 1789650060
    assert result.requires[0].amount == "100250000"
    assert result.leftovers[0].max_amount == "100250000"
    assert result.simulation.scope == "protocol_bundle"
    assert result.simulation.engine == "wallet_neutral_atomic"
    assert result.simulation.gas_used == "412345"


def test_instrument_calldata_parses_full_withdrawal() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert dict(request.url.params) == {
            "action": "withdraw",
            "account": CALLDATA_SAFE,
            "amount": "max",
        }
        return httpx.Response(
            200,
            json=calldata_payload("calldata-instrument-withdraw-max.json"),
        )

    result = make_client(httpx.MockTransport(handler)).instrument_calldata(
        CALLDATA_INSTRUMENT_ID,
        {"action": "withdraw", "account": CALLDATA_SAFE, "amount": "max"},
    )

    assert result.amount_in == "max"
    assert result.expires_at is None
    assert result.leftovers == ()
    assert [call.type for call in result.calls] == ["withdraw"]


def test_bridge_calldata_gets_path_and_query_aliases() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/v1/bridge/calldata"
        assert dict(request.url.params) == {
            "fromChainId": "8453",
            "toChainId": "42161",
            "amount": "100000000",
            "account": CALLDATA_SAFE,
            "fast": "true",
        }
        assert_common_headers(request)
        return httpx.Response(200, json=calldata_payload("calldata-bridge-fast.json"))

    result = make_client(httpx.MockTransport(handler)).bridge_calldata(
        BridgeCalldataQuery(
            from_chain_id=8453,
            to_chain_id=42161,
            amount="100000000",
            account=CALLDATA_SAFE,
            fast=True,
        )
    )

    assert [call.type for call in result.calls] == ["approve", "bridge_burn"]
    assert result.source_domain == 6
    assert result.destination_domain == 3
    assert result.max_fee == "12000"
    assert result.min_finality_threshold == 1000


@pytest.mark.parametrize(
    "query",
    [
        {"action": "deposit", "account": CALLDATA_SAFE, "amount": "max"},
        {"action": "deposit", "account": CALLDATA_SAFE, "amount": "0"},
        {"action": "deposit", "account": CALLDATA_SAFE, "amount": "100.25"},
        {"action": "deposit", "account": "0x1234", "amount": "1"},
        {"action": "stake", "account": CALLDATA_SAFE, "amount": "1"},
        {
            "action": "deposit",
            "account": CALLDATA_SAFE,
            "amount": "1",
            "slippageBps": 10_001,
        },
        {
            "action": "deposit",
            "account": CALLDATA_SAFE,
            "amount": "1",
            "executor": CALLDATA_SAFE,
        },
    ],
)
def test_instrument_calldata_rejects_invalid_queries_before_requesting(
    query: dict[str, object],
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("an invalid query must not reach the API")

    with pytest.raises(ValidationError):
        make_client(httpx.MockTransport(handler)).instrument_calldata(
            CALLDATA_INSTRUMENT_ID,
            query,
        )


def test_bridge_calldata_rejects_same_chain_route() -> None:
    with pytest.raises(ValidationError, match="must differ"):
        BridgeCalldataQuery(
            from_chain_id=8453,
            to_chain_id=8453,
            amount="1",
            account=CALLDATA_SAFE,
            fast=False,
        )


def _set(path: tuple[object, ...], value: object) -> Any:
    def mutate(payload: dict[str, Any]) -> None:
        target: Any = payload
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value

    return mutate


def _delete(path: tuple[object, ...]) -> Any:
    def mutate(payload: dict[str, Any]) -> None:
        target: Any = payload
        for key in path[:-1]:
            target = target[key]
        del target[path[-1]]

    return mutate


@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param(_set(("executor",), CALLDATA_SAFE), id="unknown-field"),
        pytest.param(
            _set(("simulation", "executor"), CALLDATA_SAFE), id="nested-extra"
        ),
        pytest.param(_delete(("leftovers",)), id="missing-field"),
        pytest.param(_delete(("expiresAt",)), id="missing-nullable-field"),
        pytest.param(_set(("simulation", "ok"), False), id="simulation-not-ok"),
        pytest.param(_set(("simulation", "scope"), "wallet"), id="unsupported-scope"),
        pytest.param(
            _set(("simulation", "engine"), "safe_adapter"),
            id="unsupported-engine",
        ),
        pytest.param(_set(("simulation", "quoteBlock"), 1), id="quote-block-mismatch"),
        pytest.param(_set(("calls",), []), id="empty-calls"),
        pytest.param(_set(("calls", 1, "chainId"), 42161), id="call-chain-mismatch"),
        pytest.param(_set(("calls", 1, "type"), "buy"), id="unknown-call-type"),
        pytest.param(_set(("calls", 0, "to"), "0x1234"), id="short-address"),
        pytest.param(_set(("calls", 0, "data"), "0x095ea7b"), id="odd-hex"),
        pytest.param(_set(("calls", 0, "data"), "095ea7b3"), id="unprefixed-hex"),
        pytest.param(_set(("calls", 0, "value"), "-1"), id="negative-value"),
        pytest.param(_set(("calls", 0, "value"), 0), id="numeric-value"),
        pytest.param(_set(("calls", 0, "value"), "01"), id="non-canonical-value"),
        pytest.param(_set(("chainId",), "8453"), id="string-chain-id"),
        pytest.param(_set(("expiresAt",), "1789650060"), id="string-expiry"),
        pytest.param(_set(("requires", 0, "amount"), "1.5"), id="fractional-amount"),
        pytest.param(_set(("amountIn",), "max"), id="max-deposit"),
    ],
)
def test_instrument_calldata_fails_closed_on_contract_drift(mutation: Any) -> None:
    payload = calldata_payload("calldata-instrument-deposit-swap.json")
    mutation(payload)

    with pytest.raises(OneTxDecodeError, match="outside the execution contract"):
        request_instrument_calldata(calldata_client(payload))


@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param(_set(("leftovers",), []), id="unknown-field"),
        pytest.param(_set(("minFinalityThreshold",), 2000), id="finality-mismatch"),
        pytest.param(_set(("minFinalityThreshold",), 500), id="unknown-finality"),
        pytest.param(_set(("toChainId",), 8453), id="same-chain"),
        pytest.param(_set(("calls", 1, "chainId"), 42161), id="destination-call"),
        pytest.param(_set(("amount",), "0"), id="zero-amount"),
        pytest.param(_set(("simulation", "engine"), "kernel"), id="unsupported-engine"),
    ],
)
def test_bridge_calldata_fails_closed_on_contract_drift(mutation: Any) -> None:
    payload = calldata_payload("calldata-bridge-fast.json")
    mutation(payload)

    with pytest.raises(OneTxDecodeError, match="outside the execution contract"):
        calldata_client(payload).bridge_calldata(
            {
                "fromChainId": 8453,
                "toChainId": 42161,
                "amount": "100000000",
                "account": CALLDATA_SAFE,
                "fast": True,
            }
        )


def test_instrument_calldata_retries_like_other_requests() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, json={"message": "Simulation unavailable"})
        return httpx.Response(
            200,
            json=calldata_payload("calldata-instrument-deposit-swap.json"),
        )

    client = make_client(httpx.MockTransport(handler), max_retries=1)

    assert request_instrument_calldata(client).quote_block == 35123456
    assert calls == 2


def test_instrument_calldata_surfaces_reverted_simulation_as_http_error() -> None:
    client = make_client(
        httpx.MockTransport(
            lambda _request: httpx.Response(
                400,
                json={"message": "Atomic simulation produced no output tokens"},
            )
        )
    )

    with pytest.raises(OneTxHTTPError) as error:
        request_instrument_calldata(client)

    assert error.value.status_code == 400
