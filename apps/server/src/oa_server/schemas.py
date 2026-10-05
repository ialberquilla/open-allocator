"""The typed shapes of what the HTTP API returns.

They describe the library's review dicts so OpenAPI, and the web app's types
generated from it, carry them exactly. `extra="forbid"` turns a review field the
library adds or renames into a failure here rather than a silent gap on the page.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

JsonObject = dict[str, Any]

PlanStatus = Literal["pending", "expired", "applying", "applied", "failed", "rejected"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TokenAmount(Strict):
    # Raw token units, or `max` for a whole position.
    raw: str
    # Decimal token units; None when the token's decimals are unknown or for `max`.
    amount: str | None
    symbol: str | None
    token: str | None


class ReviewLeg(Strict):
    leg_index: int
    instrument_id: str
    target_usd: float | None
    # None when planning skipped the leg.
    deposit_usd: float | None
    leverage: float | None


class ReviewBridge(Strict):
    to_chain_id: int
    fast: bool
    max_fee: TokenAmount | None


class ReviewBundle(Strict):
    bundle_id: str
    leg_index: int
    instrument_id: str
    action: str
    chain_id: int
    amount_in: TokenAmount | None
    expected_out: TokenAmount | None
    min_out: TokenAmount | None
    # Step kinds in submission order.
    steps: list[str]
    # Unix seconds the embedded quote expires; None when nothing expires.
    expires_at: int | None
    bridge: ReviewBridge | None


class ReviewFunding(Strict):
    chain_id: int
    required: TokenAmount | None
    available: TokenAmount | None
    shortfall: TokenAmount | None
    includes_gas_charge: bool
    ok: bool


class ReviewViolation(Strict):
    rule: str
    entity: str
    limit: Any
    actual: Any


class ReviewPolicy(Strict):
    ok: bool
    violations: list[ReviewViolation]


class ExecuteReview(Strict):
    kind: Literal["execute"]
    account: str
    target_usd: float | None
    deposit_usd: float
    legs: list[ReviewLeg]
    bundles: list[ReviewBundle]
    # Levered operations, as the CLI announces them.
    loops: list[JsonObject]
    funding: list[ReviewFunding]
    # The policy result the plan was built under. Approval checks it again
    # against the operator's policy.
    policy: ReviewPolicy
    notes: list[str]
    # Reasons the plan cannot be submitted as it stands.
    blockers: list[str]
    transactions: int


class WithdrawReview(Strict):
    kind: Literal["withdraw"]
    account: str
    instrument_id: str
    protocol: str
    chain_id: int
    symbol: str
    full_exit: bool
    # None for a full exit asked without an amount.
    requested_usd: float | None
    # The position's value when the plan was built.
    current_usd: float
    # Yield-token shares sold, and the position's share balance, in token units.
    shares: str
    share_balance: str
    share_symbol: str | None
    # USDC the exit is quoted to pay out; None when it does not pay out in USDC.
    expected_usdc: str | None
    bundles: list[ReviewBundle]
    funding: list[ReviewFunding]
    notes: list[str]
    blockers: list[str]
    transactions: int


class ReviewTrade(Strict):
    trade_index: int
    instrument_id: str
    action: Literal["sell", "buy"]
    usd: float
    current_usd: float
    target_usd: float
    current_weight: float
    target_weight: float
    # A buy's spend after sizing; None for a sell or a buy planning skipped.
    deposit_usd: float | None


class ReviewSkipped(Strict):
    instrument_id: str
    action: Literal["sell", "buy"]
    delta_usd: float


class RebalanceReview(Strict):
    kind: Literal["rebalance"]
    account: str
    # The book's value when the plan was built, idle USDC included.
    book_usd: float
    target_usd: float | None
    total_sell_usd: float
    total_buy_usd: float
    min_trade_usd: float
    trades: list[ReviewTrade]
    # Deltas under `min_trade_usd`, left alone.
    skipped: list[ReviewSkipped]
    bundles: list[ReviewBundle]
    funding: list[ReviewFunding]
    # The policy result the target was planned under. Approval checks it again
    # against the operator's policy.
    policy: ReviewPolicy
    notes: list[str]
    blockers: list[str]
    transactions: int


class LoopOpenReview(Strict):
    kind: Literal["loop-open"]
    account: str
    loop_id: str
    # Idle USDC supplied, and the leverage it is levered to.
    equity_usd: float
    leverage: float
    bundles: list[ReviewBundle]
    # The levered operation, as the CLI announces it.
    loop: JsonObject
    # The policy result the open was planned under, scored against the book it
    # joins. Approval checks it again against the operator's policy.
    policy: ReviewPolicy
    notes: list[str]
    transactions: int


class LoopCloseReview(Strict):
    kind: Literal["loop-close"]
    account: str
    loop_id: str
    bundles: list[ReviewBundle]
    # The levered operation, as the CLI announces it.
    loop: JsonObject
    transactions: int


class BridgeReview(Strict):
    kind: Literal["bridge"]
    account: str
    from_chain_id: int
    to_chain_id: int
    amount_usdc: float
    ref: str | None
    # The transfer under way this plan advances; None for a new burn.
    advances: JsonObject | None
    bundles: list[ReviewBundle]
    funding: list[ReviewFunding]
    notes: list[str]
    blockers: list[str]
    transactions: int


Review = Annotated[
    ExecuteReview
    | WithdrawReview
    | RebalanceReview
    | LoopOpenReview
    | LoopCloseReview
    | BridgeReview,
    Field(discriminator="kind"),
]


class PlanSummary(BaseModel):
    plan_hash: str
    kind: str
    status: PlanStatus
    created_at: datetime
    expires_at: datetime


class PlanResponse(PlanSummary):
    # What the approval would submit, read from the stored plan. None when the
    # stored plan cannot be described; `review_error` says why.
    review: Review | None
    review_error: str | None
    plan: JsonObject
    used_at: datetime | None
    result: JsonObject | None
    error: str | None


class PlanHashRequest(BaseModel):
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class ApproveResponse(BaseModel):
    plan_hash: str
    result: JsonObject


class RejectResponse(BaseModel):
    plan_hash: str
    status: PlanStatus


# The dashboard.


class BookPosition(BaseModel):
    instrument_id: str
    protocol: str
    chain_id: int
    chain: str
    symbol: str
    name: str
    yield_token_symbol: str | None
    usd: float
    # Of the deployed value, 0-1.
    weight: float
    # Current APY in percent, as 1Tx reports it; descriptive, not predictive.
    apy: float | None
    leverage: float | None


class IdleBalance(BaseModel):
    chain_id: int
    chain: str
    usd: float


class BookSlice(BaseModel):
    label: str
    usd: float
    weight: float


class BookResponse(BaseModel):
    account: str
    read_at: datetime
    total_usd: float
    deployed_usd: float
    idle_usd: float
    # USD-weighted current APY of the positions that report one, in percent.
    blended_apy: float | None
    income_per_year_usd: float | None
    # 1/sum(w^2) over position weights: concentration, not independence.
    effective_positions: float | None
    positions: list[BookPosition]
    idle: list[IdleBalance]
    by_protocol: list[BookSlice]
    by_chain: list[BookSlice]
    warnings: list[str]


NavStatus = Literal["ok", "opened", "unknown", "empty"]


class NavPoint(BaseModel):
    day: date
    # None on a day that could not be read: a gap, never a zero.
    nav_usd: float | None
    # Into positions, negative out of them. On the opening day, the whole NAV.
    flow_usd: float | None
    # Value of one unit, opened at 100; flows mint units, so they do not move it.
    unit_price: float | None
    # NAV change less flows; gross of gas.
    yield_usd: float | None
    status: NavStatus
    reason: str | None


class NavChain(BaseModel):
    chain_id: int
    chain: str
    days_read: int
    days_unknown: int
    first_day: date | None
    last_day: date | None
    # Why the last backfill could not read this chain, if it could not.
    error: str | None


class NavSummary(BaseModel):
    since: date | None
    last_day: date | None
    unit_price: float | None
    nav_usd: float | None
    # Since the current ledger opened; an earlier, emptied one is not counted.
    total_return: float | None
    # Compounded to a year; None under a week of history.
    annualized_return: float | None
    yield_usd: float
    net_flow_usd: float
    days: int
    unknown_days: int


class JobRun(BaseModel):
    id: int
    job: str
    started_at: datetime
    finished_at: datetime | None
    status: str
    detail: JsonObject | None


class PositionYield(BaseModel):
    chain_id: int
    chain: str
    instrument_id: str
    protocol: str
    symbol: str
    # Earned while held, over the days its flow could be split from its
    # return; gross of gas.
    yield_usd: float
    # Days held at both ends whose return could not be split from a flow.
    unknown_days: int


class ProtocolYield(BaseModel):
    protocol: str
    yield_usd: float
    unknown_days: int


class NavResponse(BaseModel):
    account: str
    start_day: date | None
    start_notes: list[str]
    days: list[NavPoint]
    chains: list[NavChain]
    summary: NavSummary
    # The current ledger's return by position and by protocol, largest first.
    # It need not add up to the summary's yield: the part of a day earned by
    # a position entering or leaving is in NAV but in no position.
    by_position: list[PositionYield]
    by_protocol: list[ProtocolYield]
    last_run: JobRun | None
    backfilling: bool


JobName = Literal["nav", "shelf", "rewards"]


class JobStartResponse(BaseModel):
    job: JobName
    # False when that job was already running.
    started: bool


class JobsResponse(BaseModel):
    # The last run of each job, by name; a job that never ran is absent.
    latest: dict[str, JobRun]
    # Every job's runs, newest first.
    runs: list[JobRun]
    running: list[JobName]


class Execution(BaseModel):
    """One executed action from the allocation log."""

    id: int
    logged_at: datetime
    instrument_id: str
    chain_id: int
    chain: str
    action_type: str
    tx_hash: str
    usd: float | None
    shares: str | None
    share_price: str | None
    basis: str


class Reward(BaseModel):
    provider: str
    chain_id: int
    chain: str
    token: str
    symbol: str
    claimable: str
    pending: str
    # What claiming and swapping to USDC would bring, as 1Tx quotes it; None
    # when there is no route or quote. A USDC reward is its own amount.
    usd: float | None
    swap_status: str
    instrument_ids: list[str]


class RewardsResponse(BaseModel):
    wallet: str
    read_at: datetime
    rewards: list[Reward]
    # The sum of the rewards that have a USD value; the others are unpriced.
    claimable_usd: float
    unpriced: int
    errors: list[str]


class ShelfVault(BaseModel):
    instrument_id: str
    protocol: str
    chain_id: int
    chain: str
    asset: str
    # 1Tx's name for the instrument, else its asset.
    name: str
    yield_token_symbol: str | None
    curator: str | None
    sector: str | None
    # Percent, as 1Tx advertises it; descriptive, not predictive. A fixed-term
    # row's is the rate locked to maturity.
    apy: float
    base_apy: float | None
    reward_apy: float | None
    # The reward APY this allocator counts; None where it is priced at an
    # emission schedule rather than a fillable quote.
    priced_reward_apy: float | None
    # Share of the APY paid in rewards, 0-1.
    reward_dependence: float | None
    tvl_usd: float
    levered: bool
    max_leverage: float | None
    maturity: datetime | None
    days_to_maturity: int | None
    # The allocator's score, 0-1.
    score: float
    # Yield-path metrics over the APY history; never principal or contract loss.
    history_days: int | None
    sharpe: float | None
    # Non-positive fraction of the compounded APY path.
    max_drawdown: float | None
    # Population standard deviation of the APY, in percentage points.
    volatility: float | None
    realized_apy: float | None
    # Realized less advertised APY, percentage points; never positive.
    delivery_gap: float | None


class ShelfResponse(BaseModel):
    read_at: datetime
    # Best score first.
    vaults: list[ShelfVault]
    warnings: list[str]
