from __future__ import annotations

from collections.abc import Mapping
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from math import isfinite

from pydantic import Field

from open_allocator.core.positions import PositionHolding
from open_allocator.core.types import FrozenModel, Policy


class WithdrawPlan(FrozenModel):
    instrument_id: str
    protocol: str
    chain_id: int
    symbol: str
    requested_usd: float | None = Field(default=None, ge=0)
    full_exit: bool
    current_usd: float = Field(ge=0)
    share_balance: str
    share_decimals: int = Field(ge=0)
    share_price_usd: str
    yield_token_amount: str
    yield_token_symbol: str | None = None
    yield_token_address: str | None = None


def plan_withdraw(
    position: PositionHolding | Mapping[str, object],
    policy: Policy | Mapping[str, object],
    *,
    amount: float | str | Decimal | None = None,
) -> WithdrawPlan:
    holding = _position(position)
    _policy(policy)

    total_shares = _money_decimal(holding.share_balance, "share_balance")
    current_usd = _money_decimal(holding.usd_value, "usd_value")
    if total_shares <= 0:
        raise ValueError("cannot withdraw a zero-share position")
    if current_usd <= 0:
        raise ValueError("cannot withdraw a zero-value position")

    share_price = current_usd / total_shares
    requested_usd: Decimal | None = None
    if amount is None:
        full_exit = True
        shares_to_sell = holding.share_balance
    else:
        requested_usd = _positive_money_decimal(amount, "amount")
        if requested_usd >= current_usd:
            full_exit = True
            shares_to_sell = holding.share_balance
        else:
            full_exit = False
            quantum = Decimal(1).scaleb(-holding.share_decimals)
            rounded_shares = (requested_usd / share_price).quantize(
                quantum,
                rounding=ROUND_DOWN,
            )
            if rounded_shares > total_shares:
                rounded_shares = total_shares
            if rounded_shares <= 0:
                raise ValueError("amount rounds down to zero yield-token shares")
            shares_to_sell = _format_decimal(rounded_shares)

    return WithdrawPlan(
        instrument_id=holding.instrument_id,
        protocol=holding.protocol,
        chain_id=holding.chain_id,
        symbol=holding.symbol,
        requested_usd=(float(requested_usd) if requested_usd is not None else None),
        full_exit=full_exit,
        current_usd=float(current_usd),
        share_balance=holding.share_balance,
        share_decimals=holding.share_decimals,
        share_price_usd=_format_decimal(share_price),
        yield_token_amount=shares_to_sell,
        yield_token_symbol=holding.yield_token_symbol,
        yield_token_address=holding.yield_token_address,
    )


def withdraw(*args: object, **kwargs: object) -> object:
    from open_allocator.exec.withdraw import withdraw as execute_withdraw

    return execute_withdraw(*args, **kwargs)


def _position(position: PositionHolding | Mapping[str, object]) -> PositionHolding:
    if isinstance(position, PositionHolding):
        return position
    return PositionHolding.model_validate(position)


def _policy(policy: Policy | Mapping[str, object]) -> Policy:
    if isinstance(policy, Policy):
        return policy
    return Policy.model_validate(policy)


def _money_decimal(value: object, name: str) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"{name} must be a finite non-negative number") from error
    if not amount.is_finite() or amount < 0:
        raise ValueError(f"{name} must be a finite non-negative number")
    return amount


def _positive_money_decimal(value: object, name: str) -> Decimal:
    amount = _money_decimal(value, name)
    if amount <= 0:
        raise ValueError(f"{name} must be greater than zero")
    if isinstance(value, float) and not isfinite(value):
        raise ValueError(f"{name} must be a finite non-negative number")
    return amount


def _format_decimal(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


__all__ = ["WithdrawPlan", "plan_withdraw", "withdraw"]
