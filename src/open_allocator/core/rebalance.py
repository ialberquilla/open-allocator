from __future__ import annotations

from collections.abc import Iterable, Mapping
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from math import isfinite
from typing import Literal

from pydantic import Field

from open_allocator.core import policy as policy_core
from open_allocator.core import positions as positions_core
from open_allocator.core.types import Allocation, FrozenModel, Policy, Vault

_USDC_QUANTUM = Decimal("0.000001")
_EPSILON = Decimal("0.0000005")


class RebalancePolicyError(ValueError):
    def __init__(self, result: policy_core.PolicyResult) -> None:
        self.result = result
        violations = ", ".join(
            f"{violation.rule}:{violation.entity}"
            for violation in result.violations
        )
        super().__init__(f"policy check failed: {violations}")


class RebalanceTrade(FrozenModel):
    instrument_id: str
    action: Literal["sell", "buy"]
    usd: float = Field(ge=0)
    current_usd: float = Field(ge=0)
    target_usd: float = Field(ge=0)
    delta_usd: float
    current_weight: float = Field(ge=0)
    target_weight: float = Field(ge=0)
    yield_token_amount: str | None = None


class RebalancePlan(FrozenModel):
    target: Allocation
    policy_result: policy_core.PolicyResult
    diff: positions_core.Diff
    trades: tuple[RebalanceTrade, ...]
    skipped_deltas: tuple[positions_core.PositionDelta, ...] = Field(
        default_factory=tuple,
    )
    min_trade_usd: float = Field(ge=0)
    total_buy_usd: float = Field(ge=0)
    total_sell_usd: float = Field(ge=0)
    total_trade_usd: float = Field(ge=0)
    deploy_usdc: float = Field(ge=0)


def plan_rebalance(
    positions: positions_core.Positions | Mapping[str, object],
    target: Allocation | Mapping[str, object],
    policy: Policy | Mapping[str, object],
    *,
    known_instruments: Iterable[Vault | Mapping[str, object]] | None = None,
    min_trade_usd: float = 1.0,
) -> RebalancePlan:
    current = _positions(positions)
    target_model = _allocation(target)
    policy_model = _policy(policy)
    threshold = _finite_nonnegative(min_trade_usd, "min_trade_usd")

    policy_result = policy_core.check(
        target_model,
        policy_model,
        tuple(known_instruments or ()),
    )
    if not policy_result.ok:
        raise RebalancePolicyError(policy_result)

    diff = positions_core.reconcile(current, target_model)
    tradable, skipped = _threshold_deltas(diff.deltas, threshold)
    trades = tuple(_trades(current, tradable))
    total_buy = _sum_trade_usd(trades, "buy")
    total_sell = _sum_trade_usd(trades, "sell")

    return RebalancePlan(
        target=target_model,
        policy_result=policy_result,
        diff=diff,
        trades=trades,
        skipped_deltas=tuple(skipped),
        min_trade_usd=threshold,
        total_buy_usd=_amount(total_buy),
        total_sell_usd=_amount(total_sell),
        total_trade_usd=_amount(total_buy + total_sell),
        deploy_usdc=_amount(max(total_buy - total_sell, Decimal("0"))),
    )


def _positions(
    positions: positions_core.Positions | Mapping[str, object],
) -> positions_core.Positions:
    if isinstance(positions, positions_core.Positions):
        return positions
    return positions_core.Positions.model_validate(positions)


def _allocation(allocation: Allocation | Mapping[str, object]) -> Allocation:
    if isinstance(allocation, Allocation):
        return allocation
    return Allocation.model_validate(allocation)


def _policy(policy: Policy | Mapping[str, object]) -> Policy:
    if isinstance(policy, Policy):
        return policy
    return Policy.model_validate(policy)


def _threshold_deltas(
    deltas: tuple[positions_core.PositionDelta, ...],
    threshold: float,
) -> tuple[list[positions_core.PositionDelta], list[positions_core.PositionDelta]]:
    threshold_decimal = _money_decimal(threshold, "min_trade_usd")
    tradable: list[positions_core.PositionDelta] = []
    skipped: list[positions_core.PositionDelta] = []
    for delta in deltas:
        amount = _money_decimal(
            delta.sell_usd if delta.action == "sell" else delta.buy_usd,
            "delta.usd",
        )
        if amount < threshold_decimal:
            skipped.append(delta)
        else:
            tradable.append(delta)
    return tradable, skipped


def _trades(
    positions: positions_core.Positions,
    deltas: Iterable[positions_core.PositionDelta],
) -> list[RebalanceTrade]:
    sells = sorted(
        (delta for delta in deltas if delta.action == "sell"),
        key=lambda delta: delta.instrument_id,
    )
    buys = sorted(
        (delta for delta in deltas if delta.action == "buy"),
        key=lambda delta: delta.instrument_id,
    )
    trades: list[RebalanceTrade] = []
    for delta in (*sells, *buys):
        usd = delta.sell_usd if delta.action == "sell" else delta.buy_usd
        trades.append(
            RebalanceTrade(
                instrument_id=delta.instrument_id,
                action=delta.action,
                usd=usd,
                current_usd=delta.current_usd,
                target_usd=delta.target_usd,
                delta_usd=delta.delta_usd,
                current_weight=delta.current_weight,
                target_weight=delta.target_weight,
                yield_token_amount=(
                    _sell_share_amount(positions, delta)
                    if delta.action == "sell"
                    else None
                ),
            )
        )
    return trades


def _sell_share_amount(
    positions: positions_core.Positions,
    delta: positions_core.PositionDelta,
) -> str:
    holdings = tuple(
        holding
        for holding in positions.holdings
        if holding.instrument_id == delta.instrument_id
    )
    if not holdings:
        raise ValueError(f"cannot sell missing position: {delta.instrument_id}")

    current_usd = sum(
        (
            _money_decimal(holding.usd_value, "holding.usd_value")
            for holding in holdings
        ),
        Decimal("0"),
    )
    if current_usd <= 0:
        raise ValueError(f"cannot sell zero-value position: {delta.instrument_id}")

    total_shares = sum(
        (
            _money_decimal(holding.share_balance, "holding.share_balance")
            for holding in holdings
        ),
        Decimal("0"),
    )
    sell_usd = _money_decimal(delta.sell_usd, "delta.sell_usd")
    if sell_usd + _EPSILON >= current_usd:
        return _format_decimal(total_shares)

    share_decimals = max(holding.share_decimals for holding in holdings)
    quantum = Decimal(1).scaleb(-share_decimals)
    shares = (total_shares * sell_usd / current_usd).quantize(
        quantum,
        rounding=ROUND_DOWN,
    )
    return _format_decimal(shares)


def _sum_trade_usd(
    trades: Iterable[RebalanceTrade],
    action: Literal["sell", "buy"],
) -> Decimal:
    return sum(
        (
            _money_decimal(trade.usd, "trade.usd")
            for trade in trades
            if trade.action == action
        ),
        Decimal("0"),
    )


def _money_decimal(value: object, name: str) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"{name} must be a finite non-negative number") from error
    if not amount.is_finite() or amount < 0:
        raise ValueError(f"{name} must be a finite non-negative number")
    return amount


def _finite_nonnegative(raw: object, name: str) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be a finite non-negative number") from error
    if not isfinite(value) or value < 0:
        raise ValueError(f"{name} must be a finite non-negative number")
    return value


def _amount(value: Decimal) -> float:
    return float(value.quantize(_USDC_QUANTUM))


def _format_decimal(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


__all__ = [
    "RebalancePlan",
    "RebalancePolicyError",
    "RebalanceTrade",
    "plan_rebalance",
]
