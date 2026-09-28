"""Fixed-term (Pendle PT) instruments: what the rate history means to a holder.

A principal token trades below par and redeems at par on its maturity date. The
APY upstream publishes for it is the **implied rate**: what buying *today* and
holding to maturity returns, annualized. That makes its history a different kind
of series from every other instrument's, and reading it the ordinary way is
wrong in a specific, flattering direction.

For an open-ended vault the APY history is the holder's own return path: each
day's APY is what a unit held that day accrued. For a PT it is not — it is the
rate a *new* buyer would lock each day. A holder's return is the change in the
PT's price, and that price is set by the implied rate and the time left:

    price_t = (1 + implied_t) ** -(years to maturity at t)

So when the implied rate rises, a holder marks to market *down*, by roughly the
rate move times the remaining duration. The implied-rate series is smooth, and
read as an accrual series it reports near-zero volatility and no drawdown for an
instrument whose early exit carries real rate risk.

:func:`holder_return_daily` rebuilds the holder's path from the implied-rate
history and the maturity, and expresses each day's price change as an
annualized percent, **the same units every other instrument's series uses**. So
the one substitution at the point history is attached puts Sharpe, drawdown,
the stability factor, the backtest and measured diversification on the holder's
footing without any of them learning what a PT is. Two properties anchor it:

- A constant implied rate returns that rate every day: held to maturity, a PT
  earns exactly what it locked. The transform only moves the numbers when the
  rate moves.
- The path ends at maturity. Nothing after it is invented; a PT pays nothing
  once matured, and that is the drift gate's concern, not a number here.

The implied rate on the day a holding is *reviewed* is also the right yield to
compare it against alternatives with: continuing to hold earns today's implied
rate on today's price until maturity, whatever the entry rate was. The rate
locked at entry is sunk and decides nothing, which is why no per-position
locked rate is kept.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime

from open_allocator.core.types import Vault

_DAYS_PER_YEAR = 365.0


def pt_price(implied_apy_pct: float, years_to_maturity: float) -> float:
    """PT price in units of its accounting asset, par = 1."""
    return (1 + implied_apy_pct / 100) ** -max(years_to_maturity, 0.0)


def holder_return_daily(
    implied_daily: Sequence[tuple[date, float]],
    maturity: datetime,
) -> tuple[tuple[date, float], ...]:
    """A PT holder's daily return path, as annualized percent per observation.

    ``implied_daily`` is the dated implied-rate history (one observation per UTC
    date, oldest first, percent). Each output point is dated at the later of
    the two observations it spans, and annualizes the price change over the
    whole-day gap between them, so a missing day does not read as a spike.

    Observations on or after the maturity date are dropped: a matured PT is
    at par and its "implied rate" is no longer a price.
    """
    maturity_day = _utc_date(maturity)
    live = [(day, apy) for day, apy in implied_daily if day < maturity_day]
    returns: list[tuple[date, float]] = []
    for (previous_day, previous_apy), (day, apy) in zip(live, live[1:], strict=False):
        gap_days = (day - previous_day).days
        if gap_days <= 0:
            continue
        previous_price = pt_price(
            previous_apy, (maturity_day - previous_day).days / _DAYS_PER_YEAR
        )
        price = pt_price(apy, (maturity_day - day).days / _DAYS_PER_YEAR)
        growth = price / previous_price
        returns.append((day, (growth ** (_DAYS_PER_YEAR / gap_days) - 1) * 100))
    return tuple(returns)


def with_holder_path(vault: Vault) -> Vault:
    """``vault`` with its history restated as the holder's return path.

    A no-op for an open-ended instrument. For a fixed-term one, ``apy_daily``
    and ``apy_series`` both become the holder path: the raw series is
    replaced by the daily one because the price change between two raw
    observations needs their dates, and a raw series at sub-daily cadence has
    none this module can trust.
    """
    if vault.maturity is None or not vault.apy_daily:
        return vault
    path = holder_return_daily(vault.apy_daily, vault.maturity)
    return vault.model_copy(
        update={
            "apy_daily": path,
            "apy_series": tuple(value for _, value in path),
        }
    )


def days_to_maturity(vault: Vault, *, as_of: datetime | None = None) -> int | None:
    """Whole days left before ``vault`` matures, 0 once matured; None if open-ended.

    Computed from the maturity date rather than read from ``days_to_maturity``,
    which is a snapshot from whenever the shelf was synced.
    """
    if vault.maturity is None:
        return None
    now = as_of or datetime.now(UTC)
    return max((_utc_date(vault.maturity) - _utc_date(now)).days, 0)


def _utc_date(moment: datetime) -> date:
    if moment.tzinfo is None:
        return moment.date()
    return moment.astimezone(UTC).date()


__all__ = [
    "days_to_maturity",
    "holder_return_daily",
    "pt_price",
    "with_holder_path",
]
