"""Fixed-term (Pendle PT) support: the history a holder actually lives through.

The failure these guard against is specific and flattering: a PT's implied-rate
history read as an accrual series reports near-zero volatility and no drawdown,
which ranks a rate-sensitive instrument as if it were riskless.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from open_allocator.core import apy_accounting, costs, fixed_rate, metrics, universe
from open_allocator.core.types import Allocation, AllocationLeg, Vault

MATURITY = datetime(2027, 3, 1, tzinfo=UTC)


def days(start: date, rates: list[float]) -> tuple[tuple[date, float], ...]:
    return tuple((start + timedelta(days=i), rate) for i, rate in enumerate(rates))


def pt(**updates: object) -> Vault:
    payload: dict[str, object] = {
        "instrument_id": "pt-a",
        "protocol": "Pendle",
        "chain_id": 42161,
        "asset": "yUSD",
        "apy": 10.0,
        "apy_base": 10.0,
        "tvl_usd": 3_000_000,
        "sector": "FIXED_RATE",
        "maturity": MATURITY,
        "days_to_maturity": 120,
        "term_return_pct": 3.2,
    }
    payload.update(updates)
    return Vault.model_validate(payload)


def test_a_constant_implied_rate_is_the_holders_return_every_day() -> None:
    """Held to maturity, a PT earns exactly what it locked."""
    path = fixed_rate.holder_return_daily(days(date(2026, 9, 1), [10.0] * 5), MATURITY)

    assert len(path) == 4
    assert all(value == pytest.approx(10.0, abs=1e-9) for _, value in path)


def test_a_rising_implied_rate_marks_the_holder_down() -> None:
    """The move the implied-rate series hides: rates up, PT price down."""
    path = fixed_rate.holder_return_daily(
        days(date(2026, 9, 1), [10.0, 11.0]), MATURITY
    )

    ((_, value),) = path
    assert value < 0


def test_a_missing_day_is_annualized_over_the_gap_not_read_as_a_spike() -> None:
    implied = ((date(2026, 9, 1), 10.0), (date(2026, 9, 4), 10.0))

    ((day, value),) = fixed_rate.holder_return_daily(implied, MATURITY)

    assert day == date(2026, 9, 4)
    assert value == pytest.approx(10.0, abs=1e-9)


def test_observations_at_or_after_maturity_are_dropped() -> None:
    implied = days(MATURITY.date() - timedelta(days=2), [10.0, 10.0, 10.0, 10.0])

    path = fixed_rate.holder_return_daily(implied, MATURITY)

    assert [day for day, _ in path] == [MATURITY.date() - timedelta(days=1)]


def test_an_open_ended_vault_keeps_its_history() -> None:
    vault = pt(maturity=None, apy_daily=days(date(2026, 9, 1), [4.0, 5.0]))

    assert fixed_rate.with_holder_path(vault) is vault


def test_with_holder_path_restates_both_series() -> None:
    vault = pt(
        apy_daily=days(date(2026, 9, 1), [10.0, 11.0, 10.5]),
        apy_series=(10.0, 10.2, 11.0, 10.5),
    )

    restated = fixed_rate.with_holder_path(vault)

    assert len(restated.apy_daily) == 2
    assert restated.apy_series == tuple(value for _, value in restated.apy_daily)


def test_days_to_maturity_counts_from_the_date_and_floors_at_zero() -> None:
    vault = pt()

    assert (
        fixed_rate.days_to_maturity(vault, as_of=datetime(2027, 2, 19, 18, tzinfo=UTC))
        == 10
    )
    assert (
        fixed_rate.days_to_maturity(vault, as_of=datetime(2027, 3, 5, tzinfo=UTC)) == 0
    )
    assert fixed_rate.days_to_maturity(pt(maturity=None)) is None


def test_discovery_reads_the_maturity_fields() -> None:
    [vault], skipped = universe.discover_instruments(
        _ListClient(
            [
                {
                    "instrumentId": "pt-a",
                    "protocol": "Pendle",
                    "chainId": 42161,
                    "tokenSymbol": "yUSD",
                    "currentApy": 10.0,
                    "apyBase": 10.0,
                    "tvl": 3_000_000,
                    "sector": "FIXED_RATE",
                    "maturity": "2027-03-01T00:00:00.000Z",
                    "daysToMaturity": 120,
                    "termReturnPct": 3.2,
                }
            ]
        )
    )

    assert skipped == ()
    assert vault.maturity == MATURITY
    assert vault.days_to_maturity == 120
    assert vault.term_return_pct == pytest.approx(3.2)
    assert vault.fixed_term is True


def test_held_off_shelf_reads_missing_rows_by_id_and_reports_failures() -> None:
    shelf = [pt(instrument_id="listed")]
    client = _ByIdClient({"matured": {**_row("matured"), "isActive": False}})

    found, skipped = universe.held_off_shelf(
        client, ["listed", "matured", "gone"], shelf
    )

    assert [vault.instrument_id for vault in found] == ["matured"]
    assert found[0].maturity is not None
    assert [item.instrument_id for item in skipped] == ["gone"]
    assert client.requested == ["matured", "gone"]


def test_attached_history_is_the_holder_path_for_a_pt_only() -> None:
    implied = [10.0, 10.0, 12.0, 12.0]
    client = _MetricsClient(
        {
            "pt-a": implied,
            "open": implied,
        }
    )
    open_ended = pt(instrument_id="open", maturity=None)

    attached = {
        vault.instrument_id: vault
        for vault in metrics.attach_series(client, [pt(), open_ended], days=30)
    }

    assert attached["open"].apy_series == tuple(implied)
    holder = attached["pt-a"].apy_series
    assert len(holder) == 3
    # The rate jump is a loss to a holder, not a better day.
    assert min(holder) < 0


def test_enrich_measures_stability_on_the_holder_path() -> None:
    client = _MetricsClient({"pt-a": [10.0, 10.0, 12.0, 12.0]})

    [vault] = metrics.enrich(client, [pt()], days=30)

    # A smooth implied-rate series would give a CV near 0.1; the holder path
    # swings through a loss, so it is far less stable than that.
    assert vault.apy_stability > 1


def test_rollover_is_charged_to_year1_for_a_short_term_leg() -> None:
    legs = [
        costs.LegInput(
            instrument_id="pt",
            chain_id=42161,
            usd=10_000,
            apy_pct=10.0,
            base_apy_pct=10.0,
        )
    ]
    open_ended = costs.estimate(legs, source_chain_id=42161)
    fixed = costs.estimate(
        [
            costs.LegInput(
                instrument_id="pt",
                chain_id=42161,
                usd=10_000,
                apy_pct=10.0,
                base_apy_pct=10.0,
                term_days=73,
            )
        ],
        source_chain_id=42161,
    )

    assert open_ended is not None and fixed is not None
    assert open_ended.rollover_cost_usd_year1 == 0
    # 365/73 = 5 entries a year: this one plus four re-entries.
    assert fixed.rollover_cost_usd_year1 == pytest.approx(
        4 * open_ended.total_expected_cost_usd, rel=1e-3
    )
    assert fixed.total_expected_cost_usd == open_ended.total_expected_cost_usd
    assert fixed.net_apy_pct_year1 < open_ended.net_apy_pct_year1
    assert fixed.fixed_term_leg_count == 1


def test_a_term_longer_than_a_year_is_not_charged_a_rollover() -> None:
    estimate = costs.estimate(
        [
            costs.LegInput(
                instrument_id="pt",
                chain_id=42161,
                usd=10_000,
                apy_pct=10.0,
                term_days=500,
            )
        ],
        source_chain_id=42161,
    )

    assert estimate is not None
    assert estimate.rollover_cost_usd_year1 == 0


def test_apy_accounting_warns_on_fixed_term_weight() -> None:
    open_ended = pt(instrument_id="open", maturity=None)
    allocation = Allocation(
        legs=(
            AllocationLeg(instrument_id="pt-a", weight=0.25, usd=250),
            AllocationLeg(instrument_id="open", weight=0.75, usd=750),
        ),
        total_usd=1_000,
    )

    accounting = apy_accounting.for_allocation(allocation, [pt(), open_ended])

    assert accounting.fixed_term_weight_bps == 2_500
    assert accounting.earliest_maturity == MATURITY.date()
    assert any(warning.startswith("fixed_term:") for warning in accounting.warnings())


def _row(instrument_id: str) -> dict[str, object]:
    return {
        "instrumentId": instrument_id,
        "protocol": "Pendle",
        "chainId": 42161,
        "tokenSymbol": "yUSD",
        "currentApy": 10.0,
        "tvl": 1_000_000,
        "maturity": "2026-10-08T00:00:00.000Z",
    }


class _ListClient:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    def list_instruments(self, **_: object) -> list[dict[str, object]]:
        return self.rows


class _ByIdClient:
    def __init__(self, rows: dict[str, dict[str, object]]) -> None:
        self.rows = rows
        self.requested: list[str] = []

    def get_instrument(self, instrument_id: str) -> dict[str, object]:
        self.requested.append(instrument_id)
        if instrument_id not in self.rows:
            raise LookupError(f"404 {instrument_id}")
        return self.rows[instrument_id]


class _MetricsClient:
    def __init__(self, rates: dict[str, list[float]]) -> None:
        self.rates = rates

    def metrics_bulk(self, ids: tuple[str, ...], days: int) -> list[object]:
        start = datetime(2026, 9, 1, 23, tzinfo=UTC)
        return [
            {
                "instrumentId": instrument_id,
                "metrics": [
                    {
                        "timestamp": (start + timedelta(days=i)).isoformat(),
                        "apy": rate,
                        "tvlUsd": 1_000_000,
                    }
                    for i, rate in enumerate(self.rates.get(instrument_id, []))
                ],
            }
            for instrument_id in ids
        ]

    def instrument_analysis(self, instrument_id: str) -> dict[str, object]:
        return {}
