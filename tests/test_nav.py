"""The NAV unit ledger, ported with darex's agent-nav cases."""

from __future__ import annotations

from decimal import Decimal

from open_allocator.core.nav import (
    UNIT_PRICE_BASE,
    LedgerState,
    LedgerStep,
    PositionClose,
    close_flows,
    derive_nav,
    div_round,
    unit_step,
)


def usd(value: str) -> int:
    return int(Decimal(value).scaleb(6))


def ledger(value: str) -> int:
    return int(Decimal(value).scaleb(12))


# A reference ledger from agent-showcase's `nav_daily`: day, total_usd,
# external_flow_usd, units, unit_price (quantized to 1e-8). It checks the unit
# math alone, given each close's NAV and flow.
REFERENCE_LEDGER = [
    ("2026-08-17", "104.891396", "0.000000", "1.048913960000", "100.00000000"),
    ("2026-08-18", "104.908311", "0.000000", "1.048913960000", "100.01612620"),
    ("2026-08-19", "104.918792", "0.000000", "1.048913960000", "100.02611844"),
    ("2026-08-20", "104.929882", "0.000000", "1.048913960000", "100.03669128"),
    ("2026-08-21", "104.790334", "0.000000", "1.048913960000", "99.90365082"),
    ("2026-08-24", "104.810607", "0.000000", "1.048913960000", "99.92297843"),
    ("2026-09-22", "105.182247", "0.000000", "1.048913960000", "100.27728776"),
    ("2026-09-23", "117.655368", "12.500000", "1.173600171930", "100.25166220"),
    ("2026-09-24", "117.651674", "0.000000", "1.173600171930", "100.24851463"),
    ("2026-09-25", "117.659149", "0.000000", "1.173600171930", "100.25488392"),
]


def test_unit_step_reproduces_the_reference_ledger() -> None:
    previous: LedgerState | None = None
    for day, total, flow, units, price in REFERENCE_LEDGER:
        step = unit_step(previous, nav_micro=usd(total), flow_micro=usd(flow))
        assert (day, step.units) == (day, ledger(units))
        assert step.unit_price is not None
        assert (day, div_round(step.unit_price, 10**4)) == (
            day,
            int(Decimal(price).scaleb(8)),
        )
        previous = LedgerState(nav_micro=usd(total), units=step.units)


def test_opens_a_new_ledger_at_100_with_no_yield() -> None:
    assert unit_step(None, nav_micro=usd("50"), flow_micro=0) == LedgerStep(
        units=ledger("0.5"), unit_price=UNIT_PRICE_BASE, yield_micro=None
    )


def test_publishes_a_days_return_and_its_yield() -> None:
    step = unit_step(
        LedgerState(usd("100"), ledger("1")), nav_micro=usd("100.05"), flow_micro=0
    )
    assert step == LedgerStep(ledger("1"), ledger("100.05"), usd("0.05"))


def test_mints_units_for_a_flow_in_and_prices_only_the_return() -> None:
    step = unit_step(
        LedgerState(usd("100"), ledger("1")),
        nav_micro=usd("150.05"),
        flow_micro=usd("50"),
    )
    assert step == LedgerStep(
        div_round(ledger("1") * usd("150.05"), usd("100.05")),
        ledger("100.05"),
        usd("0.05"),
    )


def test_burns_units_for_a_flow_out() -> None:
    step = unit_step(
        LedgerState(usd("100"), ledger("1")),
        nav_micro=usd("50.02"),
        flow_micro=usd("-50"),
    )
    assert step.unit_price == ledger("100.02")
    assert step.yield_micro == usd("0.02")
    assert step.units == div_round(ledger("1") * usd("50.02"), usd("100.02"))


def test_takes_gas_off_the_return_and_keeps_nav_whole() -> None:
    step = unit_step(
        LedgerState(usd("100"), ledger("1")),
        nav_micro=usd("100.05"),
        flow_micro=0,
        gas_micro=usd("0.03"),
    )
    assert step.unit_price == ledger("100.02")
    assert step.yield_micro == usd("0.02")
    assert div_round(step.units * ledger("100.02"), 10**18) == usd("100.05")


def test_withholds_the_price_while_gas_or_a_flow_is_unknown() -> None:
    previous = LedgerState(usd("100"), ledger("1"))
    for step in (
        unit_step(previous, nav_micro=usd("100.05"), flow_micro=0, gas_micro=None),
        unit_step(previous, nav_micro=usd("100.05"), flow_micro=0, flow_unknown=True),
    ):
        assert step == LedgerStep(ledger("1"), None, None)


def test_publishes_a_large_drop_with_no_flow_as_a_loss() -> None:
    step = unit_step(
        LedgerState(usd("100"), ledger("1")), nav_micro=usd("40"), flow_micro=0
    )
    assert step == LedgerStep(ledger("1"), ledger("40"), usd("-60"))


def test_closes_when_emptied_and_reopens_at_the_given_price() -> None:
    emptied = unit_step(
        LedgerState(usd("100"), ledger("1")), nav_micro=0, flow_micro=usd("-100.01")
    )
    assert emptied == LedgerStep(0, ledger("100.01"), usd("0.01"))
    reopened = unit_step(
        LedgerState(0, 0), nav_micro=usd("20"), flow_micro=0, open_price=ledger("102")
    )
    assert reopened.unit_price == ledger("102")
    assert reopened.units == div_round(usd("20") * 10**18, ledger("102"))


VAULT = "0x" + "22" * 32
AAVE = "0x" + "33" * 32


def leg(
    usd_value: str, *, instrument: str = VAULT, shares: int | None = 1000
) -> PositionClose:
    return PositionClose(8453, instrument, shares, usd(usd_value))


def test_unchanged_shares_are_return_whatever_the_value_did() -> None:
    flows = close_flows([leg("100")], [leg("100.05")])
    assert (flows.nav_micro, flows.flow_micro, flows.unknown) == (
        usd("100.05"),
        0,
        False,
    )


def test_a_change_in_shares_is_valued_at_todays_price_per_share() -> None:
    flows = close_flows([leg("100")], [leg("150.075", shares=1500)])
    assert (flows.nav_micro, flows.flow_micro) == (usd("150.075"), usd("50.025"))


def test_a_new_position_flows_in_and_a_gone_one_flows_out() -> None:
    flows = close_flows([leg("100")], [leg("99.9", instrument=AAVE)])
    assert (flows.nav_micro, flows.flow_micro, flows.unknown) == (
        usd("99.9"),
        usd("-0.1"),
        False,
    )


def test_keys_ignore_address_case() -> None:
    flows = close_flows([leg("100")], [leg("100.01", instrument=VAULT.upper())])
    assert flows.flow_micro == 0


def test_a_position_with_no_share_count_is_unknown() -> None:
    flows = close_flows([leg("100", shares=None)], [leg("100.05")])
    assert (flows.flow_micro, flows.unknown) == (usd("0.05"), True)


def test_div_round_is_half_even_both_signs() -> None:
    assert [div_round(n, 10) for n in (5, 15, 25, 26, -5, -15, -26)] == [
        0,
        2,
        2,
        3,
        0,
        -2,
        -3,
    ]


def test_derive_nav_opens_then_publishes_and_bridges_a_gap() -> None:
    rows = derive_nav(
        [
            ("2026-09-01", [], None),
            ("2026-09-02", [leg("100")], None),
            ("2026-09-03", [leg("100.02")], None),
            ("2026-09-04", None, "no archive RPC for chain 143"),
            ("2026-09-05", [leg("150.06", shares=1500)], None),
        ]
    )
    assert [row.status for row in rows] == ["empty", "opened", "ok", "unknown", "ok"]
    assert rows[1].unit_price == UNIT_PRICE_BASE
    assert rows[1].flow_micro == usd("100")
    assert rows[2].unit_price == ledger("100.02")
    assert (
        rows[3].nav_micro is None and rows[3].reason == "no archive RPC for chain 143"
    )
    # The return over the gap lands on the next day read: 500 shares came in
    # worth 50.02, and the rest is two days' return on the units held.
    assert rows[4].flow_micro == usd("50.02")
    assert rows[4].unit_price == ledger("100.04")


def test_derive_nav_reopens_at_the_last_price() -> None:
    rows = derive_nav(
        [
            ("2026-09-01", [leg("100")], None),
            ("2026-09-02", [leg("101")], None),
            ("2026-09-03", [], None),
            ("2026-09-04", [leg("20", instrument=AAVE)], None),
        ]
    )
    assert [row.status for row in rows] == ["opened", "ok", "ok", "opened"]
    assert rows[2].units == 0
    assert rows[3].unit_price == rows[2].unit_price


def test_derive_nav_withholds_a_price_whose_flow_is_unknown() -> None:
    rows = derive_nav(
        [
            ("2026-09-01", [leg("100", shares=None)], None),
            ("2026-09-02", [leg("100.05", shares=None)], None),
        ]
    )
    assert rows[1].status == "unknown"
    assert rows[1].unit_price is None
    assert rows[1].reason == "flow not separable from return"
