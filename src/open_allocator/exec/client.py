from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from typing import Annotated, Any, Literal, Self
from urllib.parse import quote
from uuid import uuid4

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    model_validator,
)

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
    token_address: str | None = Field(default=None, alias="tokenAddress")
    token_symbol: str | None = Field(default=None, alias="tokenSymbol")
    token_decimals: int | None = Field(default=None, alias="tokenDecimals", ge=0)
    yield_token_address: str | None = Field(default=None, alias="yieldTokenAddress")
    yield_token_symbol: str | None = Field(default=None, alias="yieldTokenSymbol")
    yield_token_decimals: int | None = Field(
        default=None,
        alias="yieldTokenDecimals",
        ge=0,
    )
    description: str | None = None
    current_apy: float | None = Field(default=None, alias="currentApy")
    apy_base: float | None = Field(default=None, alias="apyBase")
    apy_reward: float | None = Field(default=None, alias="apyReward")
    reward_tokens: tuple[str, ...] | None = Field(default=None, alias="rewardTokens")
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


class RewardTransaction(OneTxModel):
    to: str
    data: str
    value: str
    type: Literal["claim", "approve", "swap"]


class RewardToken(OneTxModel):
    address: str
    chain_id: int | None = Field(default=None, alias="chainId", ge=0, strict=True)
    decimals: int = Field(ge=0, strict=True)
    symbol: str


class RewardSwap(OneTxModel):
    status: Literal["ready", "not-needed", "no-route", "unavailable"]
    venue: str
    token_out: str = Field(alias="tokenOut")
    fee: int | None = Field(default=None, ge=0)
    expected_amount_out: str | None = Field(default=None, alias="expectedAmountOut")
    minimum_amount_out: str | None = Field(default=None, alias="minimumAmountOut")
    reason: str | None = None
    transactions: tuple[RewardTransaction, ...] = ()


class RewardItem(OneTxModel):
    provider: str
    chain_id: int = Field(alias="chainId", ge=0, strict=True)
    reward_token: RewardToken = Field(alias="rewardToken")
    claimable_amount: str = Field(alias="claimableAmount", pattern=r"^\d+$")
    pending_amount: str = Field(alias="pendingAmount", pattern=r"^\d+$")
    claim: RewardTransaction
    swap: RewardSwap
    instrument_ids: tuple[str, ...] = Field(default=(), alias="instrumentIds")

    @property
    def claimable_amount_normalized(self) -> str:
        return _normalized_token_amount(
            self.claimable_amount,
            self.reward_token.decimals,
        )

    @property
    def pending_amount_normalized(self) -> str:
        return _normalized_token_amount(
            self.pending_amount,
            self.reward_token.decimals,
        )


class RewardsResponse(OneTxModel):
    wallet: str
    rewards: tuple[RewardItem, ...]
    # The API currently returns strings, but the provider-error wire shape is
    # intentionally kept opaque until a real partial failure can be captured.
    errors: tuple[Any, ...]
    expires_at: int = Field(alias="expiresAt", ge=0, strict=True)


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


# Execution responses are parsed strictly: an unknown or missing field is a
# contract change, and it must fail here rather than after the calls have been
# handed to a signer. Discovery models above stay forward-compatible.
class OneTxExecutionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


EvmAddress = Annotated[str, StringConstraints(pattern=r"^0x[0-9a-fA-F]{40}$")]
HexData = Annotated[str, StringConstraints(pattern=r"^0x(?:[0-9a-fA-F]{2})*$")]
RawUint = Annotated[str, StringConstraints(pattern=r"^(?:0|[1-9][0-9]*)$")]
PositiveRawUint = Annotated[str, StringConstraints(pattern=r"^[1-9][0-9]*$")]
RawAmountOrMax = Annotated[str, StringConstraints(pattern=r"^(?:max|[1-9][0-9]*)$")]
ChainId = Annotated[int, Field(strict=True, ge=1)]
BlockNumber = Annotated[int, Field(strict=True, ge=0)]

CalldataAction = Literal["deposit", "withdraw"]
CalldataCallType = Literal[
    "approve", "swap", "deposit", "withdraw", "bridge_burn", "fee"
]


class InstrumentCalldataQuery(OneTxExecutionModel):
    action: CalldataAction
    account: EvmAddress
    amount: RawAmountOrMax
    slippage_bps: int | None = Field(
        default=None,
        alias="slippageBps",
        strict=True,
        ge=0,
        le=10_000,
    )
    token_in: EvmAddress | None = Field(default=None, alias="tokenIn")
    token_out: EvmAddress | None = Field(default=None, alias="tokenOut")

    @model_validator(mode="after")
    def _max_is_withdraw_only(self) -> Self:
        if self.amount == "max" and self.action != "withdraw":
            raise ValueError("amount=max is only valid for withdrawals")
        return self


class BridgeCalldataQuery(OneTxExecutionModel):
    from_chain_id: ChainId = Field(alias="fromChainId")
    to_chain_id: ChainId = Field(alias="toChainId")
    amount: PositiveRawUint
    account: EvmAddress
    fast: bool = Field(strict=True)

    @model_validator(mode="after")
    def _distinct_chains(self) -> Self:
        if self.from_chain_id == self.to_chain_id:
            raise ValueError("bridge source and destination chains must differ")
        return self


class CalldataTokenInfo(OneTxExecutionModel):
    address: EvmAddress
    symbol: str | None
    decimals: int = Field(strict=True, ge=0, le=255)


class CalldataBalanceRequirement(OneTxExecutionModel):
    token: EvmAddress
    amount: RawUint


class CalldataLeftover(OneTxExecutionModel):
    token: EvmAddress
    max_amount: RawUint = Field(alias="maxAmount")


class CalldataCall(OneTxExecutionModel):
    to: EvmAddress
    data: HexData
    value: RawUint
    chain_id: ChainId = Field(alias="chainId")
    type: CalldataCallType


class BundleSimulation(OneTxExecutionModel):
    ok: Literal[True]
    scope: Literal["protocol_bundle"]
    engine: Literal["wallet_neutral_atomic"]
    # Gas of the bare protocol calls under 1Tx's ephemeral executor. It is not
    # the Safe/UserOperation gas, which Open Allocator estimates separately.
    gas_used: RawUint = Field(alias="gasUsed")
    quote_block: BlockNumber = Field(alias="quoteBlock")
    token_out_delta: RawUint = Field(alias="tokenOutDelta")
    assumed_balances: bool = Field(alias="assumedBalances", strict=True)


def _check_bundle_chain(
    calls: tuple[CalldataCall, ...],
    chain_id: int,
    simulation: BundleSimulation,
    quote_block: int,
) -> None:
    for index, call in enumerate(calls):
        if call.chain_id != chain_id:
            raise ValueError(
                f"call {index} targets chain {call.chain_id}, "
                f"bundle chain is {chain_id}"
            )
    if simulation.quote_block != quote_block:
        raise ValueError(
            f"simulation quote block {simulation.quote_block} does not match "
            f"bundle quote block {quote_block}"
        )


class InstrumentCalldataResponse(OneTxExecutionModel):
    instrument_id: str = Field(alias="instrumentId", min_length=1)
    chain_id: ChainId = Field(alias="chainId")
    account: EvmAddress
    action: CalldataAction
    token_in: CalldataTokenInfo = Field(alias="tokenIn")
    token_out: CalldataTokenInfo = Field(alias="tokenOut")
    amount_in: RawAmountOrMax = Field(alias="amountIn")
    deposit_amount: RawUint | None = Field(alias="depositAmount")
    expected_out: RawUint | None = Field(alias="expectedOut")
    min_out: RawUint | None = Field(alias="minOut")
    # Unix seconds; set only when the bundle embeds an expiring swap quote.
    expires_at: int | None = Field(alias="expiresAt", strict=True, ge=0)
    quote_block: BlockNumber = Field(alias="quoteBlock")
    requires: tuple[CalldataBalanceRequirement, ...]
    leftovers: tuple[CalldataLeftover, ...]
    calls: tuple[CalldataCall, ...] = Field(min_length=1)
    simulation: BundleSimulation

    @model_validator(mode="after")
    def _consistent_bundle(self) -> Self:
        _check_bundle_chain(
            self.calls, self.chain_id, self.simulation, self.quote_block
        )
        if self.amount_in == "max" and self.action != "withdraw":
            raise ValueError("amountIn=max is only valid for withdrawals")
        return self


class BridgeCalldataResponse(OneTxExecutionModel):
    from_chain_id: ChainId = Field(alias="fromChainId")
    to_chain_id: ChainId = Field(alias="toChainId")
    source_domain: int = Field(alias="sourceDomain", strict=True, ge=0)
    destination_domain: int = Field(alias="destinationDomain", strict=True, ge=0)
    account: EvmAddress
    amount: PositiveRawUint
    token: EvmAddress
    fast: bool = Field(strict=True)
    max_fee: RawUint = Field(alias="maxFee")
    min_finality_threshold: Literal[1000, 2000] = Field(alias="minFinalityThreshold")
    quote_block: BlockNumber = Field(alias="quoteBlock")
    requires: tuple[CalldataBalanceRequirement, ...]
    calls: tuple[CalldataCall, ...] = Field(min_length=1)
    simulation: BundleSimulation

    @model_validator(mode="after")
    def _consistent_bundle(self) -> Self:
        _check_bundle_chain(
            self.calls,
            self.from_chain_id,
            self.simulation,
            self.quote_block,
        )
        if self.from_chain_id == self.to_chain_id:
            raise ValueError("bridge source and destination chains must differ")
        # CCTP V2: 1000 is Fast Transfer, 2000 is Standard.
        if self.fast != (self.min_finality_threshold == 1000):
            raise ValueError(
                f"minFinalityThreshold {self.min_finality_threshold} does not "
                f"match fast={self.fast}"
            )
        return self


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
        super().__init__(f"{method} {path} failed ({status_code}): {response_text}")


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

    def instrument_calldata(
        self,
        instrument_id: str,
        query: InstrumentCalldataQuery | Mapping[str, object],
    ) -> InstrumentCalldataResponse:
        request = _execution_query(InstrumentCalldataQuery, query)
        escaped_id = quote(instrument_id, safe="")
        path = f"/instruments/{escaped_id}/calldata"
        payload = self._request_json(
            "GET",
            path,
            query=request.model_dump(by_alias=True, exclude_none=True),
        )
        return _parse_execution_response(InstrumentCalldataResponse, payload, path)

    def bridge_calldata(
        self,
        query: BridgeCalldataQuery | Mapping[str, object],
    ) -> BridgeCalldataResponse:
        request = _execution_query(BridgeCalldataQuery, query)
        path = "/bridge/calldata"
        payload = self._request_json(
            "GET",
            path,
            query=request.model_dump(by_alias=True, exclude_none=True),
        )
        return _parse_execution_response(BridgeCalldataResponse, payload, path)

    def positions(self, body: Mapping[str, object]) -> PositionsResponse:
        payload = self._request_json("GET", "/positions", query=_aliases(body))
        return PositionsResponse.model_validate(payload)

    def rewards(self, wallet: str, chain_id: int | None = None) -> RewardsResponse:
        query: dict[str, object] = {"wallet": wallet}
        if chain_id is not None:
            query["chainId"] = chain_id
        payload = self._request_json("GET", "/rewards", query=query)
        response = RewardsResponse.model_validate(payload)
        _validate_rewards_response(response, wallet=wallet, chain_id=chain_id)
        return response

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


def _normalized_token_amount(raw_amount: str, decimals: int) -> str:
    if decimals == 0:
        return raw_amount.lstrip("0") or "0"
    padded = raw_amount.zfill(decimals + 1)
    whole = padded[:-decimals].lstrip("0") or "0"
    fraction = padded[-decimals:].rstrip("0")
    return f"{whole}.{fraction}" if fraction else whole


def _validate_rewards_response(
    response: RewardsResponse,
    *,
    wallet: str,
    chain_id: int | None,
) -> None:
    if response.wallet.casefold() != wallet.casefold():
        raise OneTxDecodeError(
            f"rewards response wallet {response.wallet!r} does not match {wallet!r}"
        )

    for reward in response.rewards:
        if chain_id is not None and reward.chain_id != chain_id:
            raise OneTxDecodeError(
                f"reward chain {reward.chain_id} does not match requested "
                f"chain {chain_id}"
            )
        token_chain_id = reward.reward_token.chain_id
        if token_chain_id is not None and token_chain_id != reward.chain_id:
            raise OneTxDecodeError(
                f"reward token chain {token_chain_id} does not match reward chain "
                f"{reward.chain_id}"
            )


def _execution_query[QueryT: OneTxExecutionModel](
    model: type[QueryT],
    query: QueryT | Mapping[str, object],
) -> QueryT:
    if isinstance(query, model):
        return query
    return model.model_validate(query)


def _parse_execution_response[ResponseT: OneTxExecutionModel](
    model: type[ResponseT],
    payload: JsonValue,
    path: str,
) -> ResponseT:
    try:
        return model.model_validate(payload)
    except ValidationError as error:
        raise OneTxDecodeError(
            f"GET {path} returned a response outside the execution contract: {error}"
        ) from error


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
