from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Literal
from urllib.parse import quote
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

JsonValue = dict[str, Any] | list[Any] | str | int | float | bool | None
JsonObject = dict[str, Any]


class OneTxModel(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True, populate_by_name=True)


class Pagination(OneTxModel):
    total: int
    limit: int
    offset: int
    has_more: bool = Field(alias="hasMore")


class Instrument(OneTxModel):
    instrument_id: str = Field(alias="instrumentId")
    protocol: str
    chain_id: int = Field(alias="chainId")
    token_symbol: str | None = Field(default=None, alias="tokenSymbol")
    yield_token_symbol: str | None = Field(default=None, alias="yieldTokenSymbol")
    description: str | None = None
    current_apy: float | None = Field(default=None, alias="currentApy")
    tvl: float | None = None
    is_active: bool = Field(alias="isActive")
    is_stablecoin: bool = Field(alias="isStablecoin")
    asset_category: str | None = Field(default=None, alias="assetCategory")


class InstrumentsListResponse(OneTxModel):
    data: tuple[Instrument, ...]
    pagination: Pagination


class MetricDataPoint(OneTxModel):
    timestamp: str | None = None
    tvl_usd: float | None = Field(default=None, alias="tvlUsd")
    apy: float | None = None
    apy_base: float | None = Field(default=None, alias="apyBase")
    apy_reward: float | None = Field(default=None, alias="apyReward")


class InstrumentMetrics(OneTxModel):
    instrument_id: str = Field(alias="instrumentId")
    metrics: tuple[MetricDataPoint, ...]


class InstrumentYield(OneTxModel):
    current_apy_pct: float | None = Field(default=None, alias="currentApyPct")
    apy_mean_30d_pct: float | None = Field(default=None, alias="apyMean30dPct")
    reward_share_pct: float | None = Field(default=None, alias="rewardSharePct")


class InstrumentStability(OneTxModel):
    coefficient_of_variation: float | None = Field(
        default=None,
        alias="coefficientOfVariation",
    )
    yield_drawdown_pct: float | None = Field(default=None, alias="yieldDrawdownPct")
    downside_freq_pct: float | None = Field(default=None, alias="downsideFreqPct")
    trend_pct_per_week: float | None = Field(default=None, alias="trendPctPerWeek")
    history_days: int | None = Field(default=None, alias="historyDays")


class InstrumentLiquidity(OneTxModel):
    tvl_usd: float | None = Field(default=None, alias="tvlUsd")
    low_liquidity: bool | None = Field(default=None, alias="lowLiquidity")


class InstrumentAnalysis(OneTxModel):
    instrument_id: str | None = Field(default=None, alias="instrumentId")
    id: str | None = None
    name: str | None = None
    protocol: str | None = None
    chain_id: int | None = Field(default=None, alias="chainId")
    yield_: InstrumentYield | None = Field(default=None, alias="yield")
    stability: InstrumentStability | None = None
    liquidity: InstrumentLiquidity | None = None
    price_risk: bool | None = Field(default=None, alias="priceRisk")
    tier: Literal["Core", "Yield", "Frontier"] | None = None
    headline: str | None = None
    caveats: tuple[str, ...] = ()


class PortfolioAllocation(OneTxModel):
    instrument_id: str = Field(alias="instrumentId")
    weight_bps: int = Field(alias="weightBps")


class GroupItem(OneTxModel):
    key: str
    weight_bps: int = Field(alias="weightBps")


class GroupBreakdown(OneTxModel):
    items: tuple[GroupItem, ...]
    effective_groups: float = Field(alias="effectiveGroups")
    top_weight_bps: int = Field(alias="topWeightBps")


class PortfolioYield(OneTxModel):
    net_apy_pct: float = Field(alias="netApyPct")
    gross_apy_pct: float = Field(alias="grossApyPct")
    weighted_apy_mean_30d_pct: float = Field(alias="weightedApyMean30dPct")


class PortfolioStability(OneTxModel):
    coefficient_of_variation: float = Field(alias="coefficientOfVariation")
    yield_drawdown_pct: float = Field(alias="yieldDrawdownPct")
    days_within_band_pct: float = Field(alias="daysWithinBandPct")
    coverage_pct: float = Field(alias="coveragePct")


class PortfolioDiversification(OneTxModel):
    effective_positions: float = Field(alias="effectivePositions")
    effective_independent_bets: float | None = Field(alias="effectiveIndependentBets")
    avg_pairwise_correlation: float | None = Field(alias="avgPairwiseCorrelation")
    coverage_bps: int = Field(alias="coverageBps")


class ConcentrationLimitFlag(OneTxModel):
    dimension: str
    key: str
    weight_bps: int = Field(alias="weightBps")
    cap_bps: int = Field(alias="capBps")


class PortfolioConcentration(OneTxModel):
    effective_positions: float = Field(alias="effectivePositions")
    hhi: float
    top_weight_bps: int = Field(alias="topWeightBps")
    by_protocol: GroupBreakdown = Field(alias="byProtocol")
    by_chain: GroupBreakdown = Field(alias="byChain")
    by_asset_category: GroupBreakdown = Field(alias="byAssetCategory")
    by_underlying: GroupBreakdown = Field(alias="byUnderlying")
    limit_flags: tuple[ConcentrationLimitFlag, ...] = Field(alias="limitFlags")


class TailLiquidity(OneTxModel):
    weighted_tvl_usd: float = Field(alias="weightedTvlUsd")
    illiquid_weight_bps: int = Field(alias="illiquidWeightBps")


class PortfolioTail(OneTxModel):
    one_failure_cost_bps: int = Field(alias="oneFailureCostBps")
    sleeve_wipe_bps: int = Field(alias="sleeveWipeBps")
    worst_protocol_bps: int = Field(alias="worstProtocolBps")
    worst_asset_category_bps: int = Field(alias="worstAssetCategoryBps")
    weighted_reward_share_pct: float = Field(alias="weightedRewardSharePct")
    liquidity: TailLiquidity


class PortfolioTranche(OneTxModel):
    name: Literal["Core", "Yield", "Frontier"]
    instrument_ids: tuple[str, ...] = Field(alias="instrumentIds")
    weight_bps: int = Field(alias="weightBps")
    net_apy_pct: float = Field(alias="netApyPct")
    stability_cv: float = Field(alias="stabilityCV")
    rationale: str


class PortfolioAnalysis(OneTxModel):
    resolved_count: int = Field(alias="resolvedCount")
    warnings: tuple[str, ...]
    yield_: PortfolioYield = Field(alias="yield")
    stability: PortfolioStability
    diversification: PortfolioDiversification
    concentration: PortfolioConcentration
    tail: PortfolioTail
    tranches: tuple[PortfolioTranche, ...]
    headline: str
    caveats: tuple[str, ...]


class MetricDelta(OneTxModel):
    before: float | None
    after: float | None
    delta: float | None


class FactorDelta(OneTxModel):
    dimension: str
    key: str
    before_bps: int = Field(alias="beforeBps")
    after_bps: int = Field(alias="afterBps")
    delta_bps: int = Field(alias="deltaBps")


class CompareResult(OneTxModel):
    before: PortfolioAnalysis
    after: PortfolioAnalysis
    deltas: dict[str, MetricDelta]
    factor_deltas: tuple[FactorDelta, ...] = Field(alias="factorDeltas")
    headline: str


class SimulationBenchmark(OneTxModel):
    kind: Literal["flatRate", "instrument", "index"]
    label: str
    final_value_usd: float = Field(alias="finalValueUsd")
    annualized_pct: float = Field(alias="annualizedPct")
    outperformance_pct: float = Field(alias="outperformancePct")


class SimulationResult(OneTxModel):
    resolved_count: int = Field(alias="resolvedCount")
    warnings: tuple[str, ...]
    lookback_days: int = Field(alias="lookbackDays")
    principal_usd: float = Field(alias="principalUsd")
    final_value_usd: float = Field(alias="finalValueUsd")
    realized_return_pct: float = Field(alias="realizedReturnPct")
    annualized_pct: float = Field(alias="annualizedPct")
    blended_apy_vol_pct: float = Field(alias="blendedApyVolPct")
    max_yield_drawdown_pct: float = Field(alias="maxYieldDrawdownPct")
    benchmark: SimulationBenchmark
    coverage_pct: float = Field(alias="coveragePct")
    days_simulated: int = Field(alias="daysSimulated")
    headline: str
    caveats: tuple[str, ...]


class VaultPosition(OneTxModel):
    instrument_id: str = Field(alias="instrumentId")
    protocol: str
    symbol: str
    yield_token_symbol: str | None = Field(default=None, alias="yieldTokenSymbol")
    description: str | None = None
    balance: str
    balance_raw: str = Field(alias="balanceRaw")
    decimals: int
    share_balance: str | None = Field(default=None, alias="shareBalance")
    share_balance_raw: str | None = Field(default=None, alias="shareBalanceRaw")
    share_decimals: int | None = Field(default=None, alias="shareDecimals")
    current_apy: float | None = Field(default=None, alias="currentApy")
    yield_token_address: str = Field(alias="yieldTokenAddress")
    chain_id: int = Field(alias="chainId")


class PositionsResponse(OneTxModel):
    address: str
    chain_id: int = Field(alias="chainId")
    usdc_balance: str = Field(alias="usdcBalance")
    positions: tuple[VaultPosition, ...]


class Balance(OneTxModel):
    chain_id: int = Field(alias="chainId")
    chain_name: str = Field(alias="chainName")
    usdc_balance: str = Field(alias="usdcBalance")
    usdc_balance_raw: str = Field(alias="usdcBalanceRaw")


class BalancesResponse(OneTxModel):
    address: str
    balances: tuple[Balance, ...]
    total_usdc_usd: str = Field(alias="totalUsdcUsd")


class AccountGrant(OneTxModel):
    status: str
    scope: JsonObject
    expires_at: str = Field(alias="expiresAt")


class AccountResponse(OneTxModel):
    account_address: str = Field(alias="accountAddress")
    deployed_chains: tuple[int, ...] = Field(alias="deployedChains")
    authorized_chain_ids: tuple[int, ...] = Field(alias="authorizedChainIds")
    grant: AccountGrant | None


class OneTxClientError(RuntimeError):
    pass


class OneTxHTTPError(OneTxClientError):
    def __init__(
        self,
        method: str,
        path: str,
        status_code: int,
        response_text: str,
    ) -> None:
        self.method = method
        self.path = path
        self.status_code = status_code
        self.response_text = response_text
        super().__init__(
            f"{method} {path} failed ({status_code}): {response_text}"
        )


class OneTxDecodeError(OneTxClientError):
    pass



class OneTxClient:
    def __init__(
        self,
        config: object,
        *,
        timeout: httpx.Timeout | float = 10.0,
        max_retries: int = 2,
        backoff_factor: float = 0.25,
        backoff_cap: float = 5.0,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        api_url = str(getattr(config, "onetx_api_url")).rstrip("/")
        api_key = _secret_or_str(getattr(config, "onetx_api_key"))
        self._max_retries = max_retries
        self._backoff_factor = backoff_factor
        self._backoff_cap = backoff_cap
        self._sleep = sleep
        self._http = httpx.Client(
            base_url=api_url,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "x-api-key": api_key,
            },
            timeout=timeout,
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "OneTxClient":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def list_instruments(self, **filters: object) -> InstrumentsListResponse:
        payload = self._request_json("GET", "/instruments", query=_aliases(filters))
        return InstrumentsListResponse.model_validate(payload)

    def metrics_bulk(
        self,
        instrument_ids: Sequence[str],
        days: int = 30,
    ) -> tuple[InstrumentMetrics, ...]:
        payload = self._request_json(
            "GET",
            "/metrics/bulk",
            query={"instrumentIds": instrument_ids, "days": days},
        )
        return TypeAdapter(tuple[InstrumentMetrics, ...]).validate_python(payload)

    def instrument_analysis(self, instrument_id: str) -> InstrumentAnalysis:
        escaped_id = quote(instrument_id, safe="")
        payload = self._request_json("GET", f"/instruments/{escaped_id}/analysis")
        return InstrumentAnalysis.model_validate(payload)

    def analyze_portfolio(
        self,
        allocations: Sequence[Mapping[str, object] | PortfolioAllocation],
    ) -> PortfolioAnalysis:
        payload = self._request_json(
            "POST",
            "/portfolios/analyze",
            body={"allocations": allocations},
        )
        return PortfolioAnalysis.model_validate(payload)

    def compare_portfolios(
        self,
        before: Sequence[Mapping[str, object] | PortfolioAllocation],
        after: Sequence[Mapping[str, object] | PortfolioAllocation],
    ) -> CompareResult:
        payload = self._request_json(
            "POST",
            "/portfolios/compare",
            body={"before": before, "after": after},
        )
        return CompareResult.model_validate(payload)

    def simulate_portfolio(self, body: Mapping[str, object]) -> SimulationResult:
        payload = self._request_json("POST", "/portfolios/simulate", body=body)
        return SimulationResult.model_validate(payload)

    def build_buy(self, body: Mapping[str, object]) -> JsonValue:
        return self._request_json("POST", "/transactions/buy", body=body)

    def build_sell(self, body: Mapping[str, object]) -> JsonValue:
        return self._request_json("POST", "/transactions/sell", body=body)

    def positions(self, body: Mapping[str, object]) -> PositionsResponse:
        payload = self._request_json("POST", "/positions", body=body)
        return PositionsResponse.model_validate(payload)

    def balances(self, address: str) -> BalancesResponse:
        escaped_address = quote(address, safe="")
        payload = self._request_json(
            "GET",
            f"/transactions/balances/{escaped_address}",
        )
        return BalancesResponse.model_validate(payload)

    def account(self, owner_eoa: str) -> AccountResponse:
        payload = self._request_json(
            "GET",
            "/account",
            query={"ownerEoa": owner_eoa},
        )
        return AccountResponse.model_validate(payload)

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, object] | None = None,
        body: object = None,
    ) -> JsonValue:
        query_items = _query_items(query or {})
        request_body = None if body is None else _json_compatible(body)
        attempts = self._max_retries + 1

        for attempt in range(attempts):
            response = self._http.request(
                method,
                path,
                params=query_items,
                json=request_body,
                headers={"X-Request-Id": str(uuid4())},
            )
            if _retryable(response.status_code) and attempt < self._max_retries:
                self._sleep(self._backoff_delay(attempt))
                continue

            if response.is_error:
                raise OneTxHTTPError(method, path, response.status_code, response.text)

            try:
                return response.json() if response.content else None
            except ValueError as error:
                raise OneTxDecodeError(
                    f"{method} {path} returned non-JSON ({response.status_code}): "
                    f"{response.text}"
                ) from error

        raise AssertionError("unreachable retry loop exit")

    def _backoff_delay(self, attempt: int) -> float:
        delay = self._backoff_factor * (2**attempt)
        return min(delay, self._backoff_cap)


def _secret_or_str(value: object) -> str:
    get_secret_value = getattr(value, "get_secret_value", None)
    if callable(get_secret_value):
        return str(get_secret_value())
    return str(value)


def _aliases(query: Mapping[str, object]) -> dict[str, object]:
    aliases = {
        "chain_id": "chainId",
        "is_active": "isActive",
        "is_stablecoin": "isStablecoin",
        "asset_category": "assetCategory",
        "sort_by": "sortBy",
        "sort_order": "sortOrder",
    }
    return {aliases.get(key, key): value for key, value in query.items()}


def _query_items(query: Mapping[str, object]) -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    for key, value in query.items():
        if value is None:
            continue
        if isinstance(value, Sequence) and not isinstance(
            value, str | bytes | bytearray
        ):
            items.extend((key, _query_value(item)) for item in value)
        else:
            items.append((key, _query_value(value)))
    return items


def _query_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _json_compatible(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(by_alias=True, exclude_none=True)
    if isinstance(value, Mapping):
        return {key: _json_compatible(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_json_compatible(item) for item in value]
    return value


def _retryable(status_code: int) -> bool:
    return status_code == 429 or 500 <= status_code <= 599
