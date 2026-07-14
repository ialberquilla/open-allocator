"""Read-only historical backtest over the ``apy_series`` already fetched.

Compounds a proposed allocation's weighted daily return into a NAV curve and
compares it to a TVL-weighted universe benchmark, reporting realized return,
annualized Sharpe, max-drawdown, and benchmark beat-rate (studies ``034`` /
``040``). Purely descriptive and read-only — no execution-plane risk.

CAVEAT (stated at every surface): these are **yield-path** risk metrics, not
principal / depeg / smart-contract / bridge / withdrawal-liquidity loss.
``max_drawdown`` can read ``0.0`` while implementation risk is real. History is
bounded by the 1Tx ``metrics_bulk`` window and is biased by whatever market
regime it covers.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from math import sqrt

from open_allocator.core import riskmetrics
from open_allocator.core.types import FrozenModel, Unknown

BACKTEST_CAVEAT = (
    "yield-path only; descriptive not predictive; excludes principal/depeg/"
    "contract/bridge/withdrawal loss; window-bounded and regime-biased"
)

RiskValue = riskmetrics.RiskValue


class CurveStats(FrozenModel):
    days: int
    total_return_pct: RiskValue
    annualized_return_pct: RiskValue
    max_drawdown: RiskValue
    volatility_daily: RiskValue
    sharpe_annualized: RiskValue


class BacktestReport(FrozenModel):
    label: str = "descriptive-not-predictive"
    caveat: str = BACKTEST_CAVEAT
    days: int
    portfolio: CurveStats
    benchmark: CurveStats | None
    beat_rate: RiskValue
    warnings: tuple[str, ...]


def _daily_rate(apy_percent: float) -> float:
    return (1 + apy_percent / 100) ** (1 / 365) - 1


def _window(series: Sequence[float], length: int) -> tuple[float, ...]:
    return tuple(series[-length:])


def _blended_daily_returns(
    weights: Mapping[str, float],
    apy_series_by_id: Mapping[str, Sequence[float]],
    length: int,
) -> tuple[float, ...]:
    windows = {
        instrument_id: _window(apy_series_by_id[instrument_id], length)
        for instrument_id in weights
    }
    return tuple(
        sum(
            weight * _daily_rate(windows[instrument_id][day])
            for instrument_id, weight in weights.items()
        )
        for day in range(length)
    )


def _nav_curve(daily_returns: Sequence[float], principal: float) -> tuple[float, ...]:
    value = principal
    curve: list[float] = []
    for daily_return in daily_returns:
        value *= 1 + daily_return
        curve.append(value)
    return tuple(curve)


def _curve_stats(daily_returns: Sequence[float]) -> CurveStats:
    length = len(daily_returns)
    nav = _nav_curve(daily_returns, 1.0)
    total_return = (nav[-1] - 1) * 100 if nav else Unknown
    annualized = (nav[-1] ** (365 / length) - 1) * 100 if nav else Unknown
    volatility = riskmetrics.stddev(daily_returns)
    if volatility == Unknown or volatility == 0:
        sharpe: RiskValue = Unknown
    else:
        mean_daily = sum(daily_returns) / length
        sharpe = mean_daily / float(volatility) * sqrt(365)
    return CurveStats(
        days=length,
        total_return_pct=_round(total_return),
        annualized_return_pct=_round(annualized),
        max_drawdown=_round(riskmetrics.max_drawdown(nav)),
        volatility_daily=_round(volatility),
        sharpe_annualized=_round(sharpe),
    )


def run(
    weights: Mapping[str, float],
    apy_series_by_id: Mapping[str, Sequence[float]],
    tvl_by_id: Mapping[str, float],
) -> BacktestReport:
    """Backtest ``weights`` against a TVL-weighted benchmark of the universe.

    ``apy_series_by_id`` / ``tvl_by_id`` cover the whole discovered universe;
    the benchmark is the TVL-weighted subset with enough history to align.
    """
    warnings: list[str] = []

    participating = {
        instrument_id: weight
        for instrument_id, weight in weights.items()
        if weight > 0 and len(apy_series_by_id.get(instrument_id, ())) > 0
    }
    missing = [
        instrument_id
        for instrument_id, weight in weights.items()
        if weight > 0 and instrument_id not in participating
    ]
    for instrument_id in sorted(missing):
        warnings.append(f"no_history:{instrument_id}:excluded_from_backtest")

    if not participating:
        raise ValueError("backtest requires at least one weighted leg with history")

    length = min(
        len(apy_series_by_id[instrument_id]) for instrument_id in participating
    )
    portfolio_weights = _renormalize(participating)
    portfolio_returns = _blended_daily_returns(
        portfolio_weights, apy_series_by_id, length
    )

    benchmark_stats: CurveStats | None = None
    beat_rate: RiskValue = Unknown
    benchmark_ids = {
        instrument_id: tvl
        for instrument_id, tvl in tvl_by_id.items()
        if tvl > 0 and len(apy_series_by_id.get(instrument_id, ())) >= length
    }
    if benchmark_ids:
        total_tvl = sum(benchmark_ids.values())
        benchmark_weights = {
            instrument_id: tvl / total_tvl
            for instrument_id, tvl in benchmark_ids.items()
        }
        benchmark_returns = _blended_daily_returns(
            benchmark_weights, apy_series_by_id, length
        )
        benchmark_stats = _curve_stats(benchmark_returns)
        beat_days = sum(
            1
            for day in range(length)
            if portfolio_returns[day] > benchmark_returns[day]
        )
        beat_rate = _round(beat_days / length)
    else:
        warnings.append("no_benchmark:insufficient_universe_history")

    return BacktestReport(
        days=length,
        portfolio=_curve_stats(portfolio_returns),
        benchmark=benchmark_stats,
        beat_rate=beat_rate,
        warnings=tuple(warnings),
    )


def _renormalize(weights: Mapping[str, float]) -> dict[str, float]:
    total = sum(weights.values())
    if total <= 0:
        raise ValueError("participating weights sum to zero")
    return {instrument_id: weight / total for instrument_id, weight in weights.items()}


def _round(value: RiskValue, digits: int = 6) -> RiskValue:
    if value == Unknown:
        return Unknown
    return round(float(value), digits)


__all__ = ["BACKTEST_CAVEAT", "BacktestReport", "CurveStats", "run"]
