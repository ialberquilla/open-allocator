from __future__ import annotations

from decimal import Decimal

import pytest

from open_allocator.core.amounts import (
    from_raw_units,
    proportional_raw_units,
    to_raw_units,
    underlying_withdraw_amount,
)
from open_allocator.core.positions import PositionHolding


def holding(
    *,
    balance: str = "100",
    balance_raw: str | None = "100000000",
    decimals: int | None = 6,
) -> PositionHolding:
    return PositionHolding(
        instrument_id="vault",
        protocol="protocol",
        chain_id=8453,
        symbol="USDC",
        balance=balance,
        balance_raw=balance_raw,
        decimals=decimals,
        usd_value=float(balance),
        share_balance="1",
        share_balance_raw="1",
        share_decimals=0,
    )


@pytest.mark.parametrize(
    ("amount", "decimals", "raw"),
    [
        (100.25, 6, 100_250_000),
        ("100.25", 6, 100_250_000),
        (Decimal("100.25"), 6, 100_250_000),
        # Binary 0.1 + 0.2 is 0.30000000000000004; it must not round up.
        (0.1 + 0.2, 6, 300_000),
        ("1.2345679", 6, 1_234_567),
        ("0.0000009", 6, 0),
        ("1", 18, 10**18),
        ("123456789.123456789123456789", 18, 123456789123456789123456789),
        ("5", 0, 5),
    ],
)
def test_to_raw_units_is_exact_and_rounds_down(
    amount: object,
    decimals: int,
    raw: int,
) -> None:
    assert to_raw_units(amount, decimals) == raw


@pytest.mark.parametrize("amount", [-1, "-0.1", float("nan"), float("inf"), "x", True])
def test_to_raw_units_rejects_non_amounts(amount: object) -> None:
    with pytest.raises(ValueError):
        to_raw_units(amount, 6)


@pytest.mark.parametrize("decimals", [-1, True, 6.0])
def test_to_raw_units_rejects_invalid_decimals(decimals: object) -> None:
    with pytest.raises(ValueError, match="decimals"):
        to_raw_units("1", decimals)  # type: ignore[arg-type]


def test_proportional_raw_units_floors_exactly_beyond_decimal_precision() -> None:
    # 28 significant digits is Decimal's default context; an 18-decimal balance
    # times a fractional ratio exceeds it and would round silently.
    balance_raw = 123_456_789_012_345_678_901_234_567_890
    requested = Decimal("33.333333")
    current = Decimal("100.000001")

    expected = balance_raw * 33_333_333 // 100_000_001

    assert proportional_raw_units(balance_raw, requested, current) == expected


def test_underlying_withdraw_amount_is_proportional_to_raw_balance() -> None:
    amount = underlying_withdraw_amount(
        (holding(balance="100", balance_raw="100000000"),),
        requested_usd=Decimal("25.5"),
        current_usd=Decimal("100"),
    )

    assert amount == "25500000"


def test_underlying_withdraw_amount_sums_holdings_of_one_instrument() -> None:
    amount = underlying_withdraw_amount(
        (
            holding(balance="60", balance_raw="60000000"),
            holding(balance="40", balance_raw="40000000"),
        ),
        requested_usd=Decimal("50"),
        current_usd=Decimal("100"),
    )

    assert amount == "50000000"


@pytest.mark.parametrize(
    "holdings",
    [
        (holding(balance_raw=None),),
        (holding(decimals=None),),
        (holding(decimals=6), holding(decimals=18)),
    ],
)
def test_underlying_withdraw_amount_is_unknown_without_raw_balance_or_decimals(
    holdings: tuple[PositionHolding, ...],
) -> None:
    assert (
        underlying_withdraw_amount(
            holdings,
            requested_usd=Decimal("1"),
            current_usd=Decimal("100"),
        )
        is None
    )


def test_underlying_withdraw_amount_is_unknown_when_it_rounds_to_zero_units() -> None:
    # Underivable, not an error: a legacy plan plans through here too and sells
    # shares, so it must not fail on an amount only a calldata request reads.
    assert (
        underlying_withdraw_amount(
            (holding(balance="100", balance_raw="100"),),
            requested_usd=Decimal("0.5"),
            current_usd=Decimal("100"),
        )
        is None
    )


def test_underlying_withdraw_amount_is_unknown_for_a_zero_raw_balance() -> None:
    # A venue reporting no raw balance against a live usd_value.
    assert (
        underlying_withdraw_amount(
            (holding(balance="100", balance_raw="0"),),
            requested_usd=Decimal("50"),
            current_usd=Decimal("100"),
        )
        is None
    )


def test_underlying_withdraw_amount_rejects_malformed_raw_balance() -> None:
    with pytest.raises(ValueError, match="balance_raw"):
        underlying_withdraw_amount(
            (holding(balance_raw="1.5"),),
            requested_usd=Decimal("1"),
            current_usd=Decimal("100"),
        )


@pytest.mark.parametrize(
    ("raw", "decimals", "human"),
    [
        ("100250000", 6, "100.25"),
        ("50001234", 6, "50.001234"),
        ("100000000", 6, "100"),
        ("0", 6, "0"),
        ("1", 18, "0.000000000000000001"),
        ("123456789123456789123456789", 18, "123456789.123456789123456789"),
        ("500", 0, "500"),
        (7, 2, "0.07"),
    ],
)
def test_from_raw_units_is_exact_without_exponent_notation(
    raw: object,
    decimals: int,
    human: str,
) -> None:
    assert from_raw_units(raw, decimals) == human
    assert to_raw_units(human, decimals) == int(raw)  # type: ignore[call-overload]


@pytest.mark.parametrize("raw", ["-1", "1.5", "", "0x10"])
def test_from_raw_units_rejects_non_integer_strings(raw: str) -> None:
    with pytest.raises(ValueError):
        from_raw_units(raw, 6)
