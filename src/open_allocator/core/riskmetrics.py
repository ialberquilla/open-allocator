"""Yield-path risk metrics over the ``apy_series`` / ``tvl_usd_series`` that
``core.metrics.enrich`` already fetches.

Pure functions, stdlib only, deterministic. Ported from
``yield-analysis/utils/metrics.py`` and studies ``034``/``037``/``040`` but kept
consistent with open-allocator conventions:

- Standard deviation is **population** (``ddof=0``), matching the coefficient of
  variation already computed in :mod:`open_allocator.core.metrics`.
- ``apy_series`` values are APY **in percent** (e.g. ``4.0`` == 4%/yr), matching
  the 1Tx ``metrics_bulk`` payload.
- A metric is :data:`Unknown` when history is too short to compute it honestly;
  it is never guessed.

CAVEAT (carried verbatim from yield-analysis): these are **yield-path** risk
metrics, not principal / depeg / smart-contract / bridge / withdrawal-liquidity
loss. ``max_drawdown`` can read ``0.0`` while implementation risk is real.
"""

from __future__ import annotations

from collections.abc import Sequence
from math import sqrt

from open_allocator.core.types import Unknown, UnknownValue, Vault

# Annual risk-free rate in the same (percent) units as the APY series.
RISK_FREE_RATE = 3.0

# Minimum observations required for dispersion-based metrics (variance, Sharpe,
# Sortino, drawdown). Below this we report Unknown rather than a fragile number.
_MIN_HISTORY = 2

RiskValue = float | UnknownValue


def mean(series: Sequence[float]) -> RiskValue:
    if not series:
        return Unknown
    return sum(series) / len(series)


def stddev(series: Sequence[float]) -> RiskValue:
    """Population standard deviation; Unknown below :data:`_MIN_HISTORY`."""
    if len(series) < _MIN_HISTORY:
        return Unknown
    avg = sum(series) / len(series)
    variance = sum((value - avg) ** 2 for value in series) / len(series)
    return sqrt(variance)


def downside_deviation(
    series: Sequence[float],
    target: float = 0.0,
) -> RiskValue:
    """Root-mean-square of shortfalls below ``target``.

    Zero when no observation falls below target (no downside seen), Unknown when
    there is no history at all.
    """
    if not series:
        return Unknown
    shortfalls = [value - target for value in series if value < target]
    if not shortfalls:
        return 0.0
    return sqrt(sum(shortfall**2 for shortfall in shortfalls) / len(shortfalls))


def sharpe(
    series: Sequence[float],
    risk_free_rate: float = RISK_FREE_RATE,
) -> RiskValue:
    """Excess mean per unit of total volatility. Unknown if volatility is zero
    or history is too short."""
    volatility = stddev(series)
    if volatility == Unknown or volatility == 0:
        return Unknown
    return (sum(series) / len(series) - risk_free_rate) / volatility


def sortino(
    series: Sequence[float],
    risk_free_rate: float = RISK_FREE_RATE,
) -> RiskValue:
    """Excess mean per unit of downside volatility. Unknown if downside
    volatility is zero or history is too short."""
    if len(series) < _MIN_HISTORY:
        return Unknown
    downside = downside_deviation(series, target=risk_free_rate)
    if downside == Unknown or downside == 0:
        return Unknown
    return (sum(series) / len(series) - risk_free_rate) / downside


def nav_curve(
    apy_series: Sequence[float],
    principal: float = 1.0,
) -> tuple[float, ...]:
    """Daily-compounded NAV curve from a series of (percent) annualized APYs.

    Each observation is treated as one day at that annualized rate:
    ``daily_rate = (1 + apy/100) ** (1/365) - 1``.
    """
    value = principal
    curve: list[float] = []
    for apy in apy_series:
        daily_rate = (1 + apy / 100) ** (1 / 365) - 1
        value *= 1 + daily_rate
        curve.append(value)
    return tuple(curve)


def max_drawdown(values: Sequence[float]) -> RiskValue:
    """Largest peak-to-trough decline of a value/NAV series, as a non-positive
    fraction (``-0.05`` == a 5% dip). Unknown below :data:`_MIN_HISTORY`."""
    if len(values) < _MIN_HISTORY:
        return Unknown
    peak = values[0]
    worst = 0.0
    for value in values:
        if value > peak:
            peak = value
        if peak > 0:
            drawdown = (value - peak) / peak
            worst = min(worst, drawdown)
    return worst


def realized_apy(apy_series: Sequence[float]) -> RiskValue:
    """Realized annualized APY (percent) from daily compounding — the geometric
    mean of the observed APYs. Unknown with no history."""
    if not apy_series:
        return Unknown
    growth = 1.0
    for apy in apy_series:
        growth *= 1 + apy / 100
    return (growth ** (1 / len(apy_series)) - 1) * 100


def delivery_gap(apy_series: Sequence[float]) -> RiskValue:
    """Realized minus advertised APY (percent).

    Advertised is the arithmetic mean of the series; realized is the geometric
    (compounded) mean. The gap is <= 0 by AM-GM: a large negative gap means the
    headline APY overstated what compounding actually delivered.
    """
    if not apy_series:
        return Unknown
    advertised = sum(apy_series) / len(apy_series)
    realized = realized_apy(apy_series)
    if realized == Unknown:
        return Unknown
    return realized - advertised


def tvl_weighted_average(
    values: Sequence[float],
    weights: Sequence[float],
) -> RiskValue:
    """TVL-weighted average of ``values``. Unknown if weights sum to zero."""
    if not values or len(values) != len(weights):
        return Unknown
    total = sum(weights)
    if total <= 0:
        return Unknown
    weighted = sum(
        value * weight for value, weight in zip(values, weights, strict=True)
    )
    return weighted / total


def summary(vault: Vault, *, digits: int = 6) -> dict[str, RiskValue]:
    """Surface-ready risk metrics for a vault, rounded for stable JSON output.

    Every field is :data:`Unknown` when the underlying series is too thin. This
    is the shape rendered on ``score-vault`` / ``list-vaults``.
    """
    apy_series = vault.apy_series
    metrics: dict[str, RiskValue] = {
        "history_days": len(apy_series),
        "sharpe": sharpe(apy_series),
        "sortino": sortino(apy_series),
        "max_drawdown": max_drawdown(nav_curve(apy_series)),
        "downside_deviation": downside_deviation(apy_series),
        "volatility": stddev(apy_series),
        "realized_apy": realized_apy(apy_series),
        "advertised_apy": mean(apy_series),
        "delivery_gap": delivery_gap(apy_series),
    }
    return {name: _round(value, digits) for name, value in metrics.items()}


def _round(value: RiskValue, digits: int) -> RiskValue:
    if isinstance(value, UnknownValue):
        return value
    if isinstance(value, int):
        return value
    return round(value, digits)


__all__ = [
    "RISK_FREE_RATE",
    "RiskValue",
    "delivery_gap",
    "downside_deviation",
    "max_drawdown",
    "mean",
    "nav_curve",
    "realized_apy",
    "sharpe",
    "sortino",
    "stddev",
    "summary",
    "tvl_weighted_average",
]
