"""Raw token-unit arithmetic for calldata requests.

The calldata API takes integer token units, not human-readable amounts. Every
conversion here is exact (``Decimal``/``Fraction``, never binary floats) and
rounds toward zero, so a request can never ask for more than was selected or
held.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from fractions import Fraction

from open_allocator.core.positions import PositionHolding

# The calldata API's full-exit amount. It withdraws the whole position at
# execution time, which an exact raw amount read earlier cannot.
WITHDRAW_ALL = "max"


def to_raw_units(amount: object, decimals: int, *, name: str = "amount") -> int:
    """Convert a human-readable token amount into raw units, rounding down.

    ``100.25`` at 6 decimals is ``100250000``. A float goes through its shortest
    ``repr`` so ``100.25`` is read as written rather than as its binary value.
    """
    if isinstance(decimals, bool) or not isinstance(decimals, int) or decimals < 0:
        raise ValueError(f"{name} decimals must be a non-negative integer")
    if isinstance(amount, bool):
        raise ValueError(f"{name} must be a finite non-negative number")
    try:
        value = Decimal(str(amount))
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"{name} must be a finite non-negative number") from error
    if not value.is_finite() or value < 0:
        raise ValueError(f"{name} must be a finite non-negative number")
    return int(value.scaleb(decimals).to_integral_value(rounding=ROUND_DOWN))


def from_raw_units(raw: object, decimals: int, *, name: str = "amount") -> str:
    """Raw token units as an exact human-readable amount: ``100250000`` at 6
    decimals is ``100.25``. No exponent notation and no trailing zeros."""
    if isinstance(decimals, bool) or not isinstance(decimals, int) or decimals < 0:
        raise ValueError(f"{name} decimals must be a non-negative integer")
    value = Decimal(parse_raw_units(raw, name=name)).scaleb(-decimals)
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def parse_raw_units(value: object, *, name: str) -> int:
    text = str(value)
    if not text.isascii() or not text.isdigit():
        raise ValueError(f"{name} must be a non-negative integer string, got {text!r}")
    return int(text)


def proportional_raw_units(
    balance_raw: int,
    requested: Decimal,
    current: Decimal,
) -> int:
    """``floor(balance_raw * requested / current)``, computed exactly."""
    if balance_raw < 0:
        raise ValueError("balance_raw must be non-negative")
    if current <= 0:
        raise ValueError("current value must be greater than zero")
    if requested < 0:
        raise ValueError("requested value must be non-negative")
    return (Fraction(balance_raw) * Fraction(requested)) // Fraction(current)


def underlying_withdraw_amount(
    holdings: Sequence[PositionHolding],
    *,
    requested_usd: Decimal,
    current_usd: Decimal,
) -> str | None:
    """The calldata ``amount`` for a partial exit: raw underlying-asset units.

    Uses the positions API's ``balance_raw`` (underlying units), never the share
    balance: the calldata withdraw is ``withdraw(assets)``, not
    ``redeem(shares)``. Returns None when no positive raw amount can be derived —
    a holding lacks ``balance_raw`` or ``decimals``, holdings disagree on
    decimals, or the exit rounds down to zero units — so a calldata request for
    it fails closed at the point one is actually built.

    It never raises for an underivable amount, because both transaction APIs
    plan through here: a venue reporting ``balance_raw`` as "0" against a live
    ``usd_value`` must not fail a legacy plan, which sells shares and never
    reads this.
    """
    if not holdings:
        raise ValueError("cannot withdraw from no holdings")
    decimals = {holding.decimals for holding in holdings}
    if None in decimals or len(decimals) != 1:
        return None
    if any(holding.balance_raw is None for holding in holdings):
        return None

    balance_raw = sum(
        parse_raw_units(holding.balance_raw, name="balance_raw") for holding in holdings
    )
    raw = proportional_raw_units(balance_raw, requested_usd, current_usd)
    if raw <= 0:
        return None
    return str(raw)


__all__ = [
    "WITHDRAW_ALL",
    "from_raw_units",
    "parse_raw_units",
    "proportional_raw_units",
    "to_raw_units",
    "underlying_withdraw_amount",
]
