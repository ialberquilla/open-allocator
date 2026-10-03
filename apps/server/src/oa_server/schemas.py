"""The typed shapes of what the HTTP API returns.

They describe the library's review dicts so OpenAPI, and the web app's types
generated from it, carry them exactly. `extra="forbid"` turns a review field the
library adds or renames into a failure here rather than a silent gap on the page.
"""

from __future__ import annotations

from datetime import datetime
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
