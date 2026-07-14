from __future__ import annotations

import pytest

from open_allocator.core import riskmetrics
from open_allocator.core.types import Unknown, Vault


def vault(apy_series: tuple[float, ...] = (), **updates: object) -> Vault:
    base = Vault(
        instrument_id="vault-a",
        protocol="protocol-a",
        chain_id=1,
        asset="USDC",
        apy=4.0,
        tvl_usd=1_000_000,
        apy_series=apy_series,
    )
    return base.model_copy(update=updates)


def test_mean_and_stddev_population_convention() -> None:
    assert riskmetrics.mean((2.0, 4.0, 6.0)) == pytest.approx(4.0)
    # Population variance of (2,4,6) about 4 is (4+0+4)/3 -> sqrt(8/3).
    assert riskmetrics.stddev((2.0, 4.0, 6.0)) == pytest.approx((8 / 3) ** 0.5)


def test_short_history_is_unknown() -> None:
    assert riskmetrics.stddev((5.0,)) == Unknown
    assert riskmetrics.sharpe((5.0,)) == Unknown
    assert riskmetrics.sortino((5.0,)) == Unknown
    assert riskmetrics.max_drawdown((5.0,)) == Unknown
    assert riskmetrics.mean(()) == Unknown


def test_zero_volatility_sharpe_is_unknown() -> None:
    assert riskmetrics.sharpe((5.0, 5.0, 5.0)) == Unknown


def test_sharpe_uses_excess_over_risk_free() -> None:
    series = (2.0, 4.0, 6.0)
    volatility = (8 / 3) ** 0.5
    expected = (4.0 - riskmetrics.RISK_FREE_RATE) / volatility
    assert riskmetrics.sharpe(series) == pytest.approx(expected)


def test_downside_deviation_zero_when_no_shortfall() -> None:
    assert riskmetrics.downside_deviation((5.0, 6.0, 7.0), target=0.0) == 0.0


def test_downside_deviation_penalizes_only_below_target() -> None:
    # Only -2 and -4 fall below target 0; rms = sqrt((4+16)/2).
    value = riskmetrics.downside_deviation((-2.0, 10.0, -4.0), target=0.0)
    assert value == pytest.approx(((4 + 16) / 2) ** 0.5)


def test_nav_curve_compounds_and_is_monotonic_for_positive_apy() -> None:
    curve = riskmetrics.nav_curve((10.0, 10.0), principal=1.0)
    assert curve[0] < curve[1]
    assert curve[1] == pytest.approx((1 + 10 / 100) ** (2 / 365))


def test_max_drawdown_on_dip_then_recover() -> None:
    # Peak 100, trough 80 -> -20%.
    assert riskmetrics.max_drawdown((100.0, 80.0, 120.0)) == pytest.approx(-0.2)


def test_max_drawdown_zero_when_monotonic_up() -> None:
    assert riskmetrics.max_drawdown((1.0, 2.0, 3.0)) == 0.0


def test_realized_below_advertised_gives_nonpositive_gap() -> None:
    series = (2.0, 8.0)  # volatile -> geometric < arithmetic
    gap = riskmetrics.delivery_gap(series)
    assert gap == pytest.approx(
        riskmetrics.realized_apy(series) - 5.0
    )
    assert gap <= 0


def test_delivery_gap_zero_for_flat_series() -> None:
    assert riskmetrics.delivery_gap((5.0, 5.0, 5.0)) == pytest.approx(0.0)


def test_tvl_weighted_average() -> None:
    assert riskmetrics.tvl_weighted_average((10.0, 20.0), (1.0, 3.0)) == pytest.approx(
        (10 + 60) / 4
    )
    assert riskmetrics.tvl_weighted_average((10.0,), (0.0,)) == Unknown


def test_summary_marks_unknown_on_thin_history() -> None:
    result = riskmetrics.summary(vault(apy_series=()))
    assert result["history_days"] == 0
    assert result["sharpe"] == Unknown
    assert result["max_drawdown"] == Unknown


def test_summary_computes_on_rich_history() -> None:
    result = riskmetrics.summary(vault(apy_series=(4.0, 5.0, 4.5, 5.5, 4.0)))
    assert result["history_days"] == 5
    assert isinstance(result["sharpe"], float)
    assert isinstance(result["realized_apy"], float)
    assert result["delivery_gap"] <= 0


def test_summary_is_deterministic() -> None:
    series = (4.0, 5.1, 4.7, 6.2, 3.9)
    assert riskmetrics.summary(vault(apy_series=series)) == riskmetrics.summary(
        vault(apy_series=series)
    )
