from __future__ import annotations

import pytest

from open_allocator.core import backtest
from open_allocator.core.types import Unknown


def test_single_leg_backtest_compounds_history() -> None:
    report = backtest.run(
        weights={"a": 1.0},
        apy_series_by_id={"a": (10.0, 10.0, 10.0, 10.0)},
        tvl_by_id={"a": 1_000_000},
    )
    assert report.days == 4
    assert report.portfolio.total_return_pct > 0
    # 4 days at a positive rate: NAV strictly rises -> no drawdown.
    assert report.portfolio.max_drawdown == 0.0


def test_missing_history_leg_is_excluded_with_warning() -> None:
    report = backtest.run(
        weights={"a": 0.5, "b": 0.5},
        apy_series_by_id={"a": (5.0, 5.0, 5.0), "b": ()},
        tvl_by_id={"a": 1_000_000, "b": 500_000},
    )
    assert any(w.startswith("no_history:b") for w in report.warnings)
    # 'a' carries the whole (renormalized) book.
    assert report.portfolio.days == 3


def test_all_legs_missing_history_raises() -> None:
    with pytest.raises(ValueError, match="at least one weighted leg"):
        backtest.run(
            weights={"a": 1.0},
            apy_series_by_id={"a": ()},
            tvl_by_id={"a": 1_000_000},
        )


def test_beat_rate_and_benchmark_present() -> None:
    report = backtest.run(
        weights={"hi": 1.0},
        apy_series_by_id={
            "hi": (20.0, 20.0, 20.0, 20.0),
            "lo": (1.0, 1.0, 1.0, 1.0),
        },
        tvl_by_id={"hi": 1_000, "lo": 1_000_000},
    )
    assert report.benchmark is not None
    # Portfolio is the high-yield leg; TVL benchmark is dominated by 'lo'.
    assert report.beat_rate == 1.0
    assert (
        report.portfolio.annualized_return_pct
        > report.benchmark.annualized_return_pct
    )


def test_short_window_yields_unknown_sharpe() -> None:
    report = backtest.run(
        weights={"a": 1.0},
        apy_series_by_id={"a": (5.0,)},
        tvl_by_id={"a": 1_000_000},
    )
    assert report.days == 1
    assert report.portfolio.sharpe_annualized == Unknown
    assert report.portfolio.max_drawdown == Unknown


def test_report_is_deterministic() -> None:
    kwargs = dict(
        weights={"a": 0.6, "b": 0.4},
        apy_series_by_id={"a": (4.0, 5.0, 4.5, 6.0), "b": (3.0, 3.2, 3.1, 3.5)},
        tvl_by_id={"a": 2_000_000, "b": 1_000_000},
    )
    assert backtest.run(**kwargs).model_dump() == backtest.run(**kwargs).model_dump()


def test_caveat_and_label_present() -> None:
    report = backtest.run(
        weights={"a": 1.0},
        apy_series_by_id={"a": (5.0, 5.0)},
        tvl_by_id={"a": 1_000_000},
    )
    assert report.label == "descriptive-not-predictive"
    assert "yield-path only" in report.caveat
