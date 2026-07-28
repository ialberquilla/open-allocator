"""Measured diversification — what an allocation actually holds.

``effective_sectors`` counts an allocation's *labels*. This module measures its
instruments' behaviour instead, so an allocation cannot look diversified by
holding many names for the same position.

Two numbers, because each is wrong on its own:

- :func:`effective_positions` — inverse of the weighted average pair
  correlation. Answers *"will my yield path smooth out?"*. It is the
  **optimistic** error: two vaults lending into the same market can wiggle
  independently day to day and still fail together.
- :func:`tail_lift` — how much more often two instruments have a bad day *on the
  same day* than independence predicts. Answers *"can one event take several
  positions at once?"*.

The honest form of the second question is composition overlap: which positions
share an underlying market. The 1Tx API serves no composition data, so tail lift
is a **proxy and must never be described as overlap** — it detects that two
instruments crash together without explaining why.

Conventions follow :mod:`open_allocator.core.riskmetrics`: pure functions,
stdlib only, deterministic, population statistics, and :data:`Unknown` rather
than a fragile number when history cannot support one.

CAVEAT, and it must survive into anything public: effective-N is an **upper
bound** on independence. Instruments in one basket can share stablecoin, oracle
and bridge risk that no APY series can see, and depeg, principal loss and
smart-contract failure are in none of these numbers.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from math import sqrt

from open_allocator.core.types import FrozenModel, Unknown, UnknownValue

RiskValue = float | UnknownValue

# Minimum shared observations before a pair is scored. Below this a correlation
# is noise wearing a number's clothes, and the pair reports Unknown instead.
MIN_OVERLAP = 60

# A change spanning a longer gap than this is dropped rather than treated as a
# daily move: the feed is not guaranteed to observe every instrument every day,
# and a 9-day jump is not a daily return.
MAX_GAP_DAYS = 3

# Fraction of each instrument's own worst days used as its tail.
TAIL_FRACTION = 0.10

# Pairs below this correlation are the ones a correlation-only view calls
# diversified; used by :func:`hidden_tail_pairs` to surface the ones that are
# not.
DECOUPLED_CORRELATION = 0.2

# ...and this is the joint-tail lift at which "they crash together" stops being
# a rounding artefact. 1.0 == exactly what independence predicts.
TAIL_LIFT_ALERT = 2.0


class PairCoMovement(FrozenModel):
    """How two instruments moved together, measured — never assumed."""

    instrument_a: str
    instrument_b: str
    overlap_days: int
    # Pearson correlation of daily APY *changes*. Levels correlate on shared
    # trend and would flatter every pair on the shelf.
    correlation: float | None = None
    # Joint worst-decile rate over the k^2/n independence baseline. 1.0 ==
    # independent, 3.7 == same-day bad days 3.7x more often than chance.
    tail_lift: float | None = None

    @property
    def scored(self) -> bool:
        return self.correlation is not None


class DiversificationReport(FrozenModel):
    """The measured counterpart to ``SectorConcentration``.

    ``effective_positions`` is the number this replaces ``effective_sectors``
    with as the binding view. The sector count stays reported next to it — it
    explains *why* positions differ, it just cannot say *whether* they do.
    """

    # (sum w)^2 / (w' C w) over the correlation matrix: 1.0 = one bet,
    # N = N independent bets.
    effective_positions: float
    position_count: int
    # Weight whose co-movement could not be measured, in bps. Reported because
    # it is a hole in the measurement, and it is charged as fully correlated
    # below so it can never flatter the count.
    unmeasured_weight_bps: int
    # Pairs that ordinary correlation calls diversified and the tail does not.
    # This is the list a reviewer should read before signing.
    hidden_tail_pairs: tuple[PairCoMovement, ...] = ()
    median_tail_lift: float | None = None


def daily_changes(
    dated: Mapping[date, float],
    *,
    max_gap_days: int = MAX_GAP_DAYS,
) -> dict[date, float]:
    """Day-over-day APY changes, keyed by the later date.

    Changes spanning more than ``max_gap_days`` are dropped rather than scaled:
    a gap is missing data, and pretending otherwise manufactures a large move
    out of an absent observation.
    """
    ordered = sorted(dated)
    changes: dict[date, float] = {}
    for previous, current in zip(ordered, ordered[1:]):
        if (current - previous).days <= max_gap_days:
            changes[current] = dated[current] - dated[previous]
    return changes


def correlation(xs: Sequence[float], ys: Sequence[float]) -> RiskValue:
    """Population Pearson correlation; Unknown when it cannot be computed.

    A flat series has no variance and therefore no correlation — that is
    Unknown, not zero. Zero would read as "independent" and let a constant-rate
    instrument dilute the effective count for free.
    """
    if len(xs) != len(ys) or len(xs) < 3:
        return Unknown

    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    dev_x = sqrt(sum((value - mean_x) ** 2 for value in xs))
    dev_y = sqrt(sum((value - mean_y) ** 2 for value in ys))
    if dev_x == 0 or dev_y == 0:
        return Unknown

    covariance = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True))
    return covariance / (dev_x * dev_y)


def tail_lift(xs: Sequence[float], ys: Sequence[float]) -> RiskValue:
    """How much more often both series have a worst-decile day *on the same day*.

    Each series contributes its own ``k = ceil(n * TAIL_FRACTION)`` worst days,
    so the measure is scale-free and does not need a shared threshold. Under
    independence the expected joint count is ``k^2 / n``; the return is the
    observed count over that.
    """
    if len(xs) != len(ys) or len(xs) < 3:
        return Unknown

    count = len(xs)
    tail_size = max(1, int(count * TAIL_FRACTION))
    worst_x = set(sorted(range(count), key=lambda i: xs[i])[:tail_size])
    worst_y = set(sorted(range(count), key=lambda i: ys[i])[:tail_size])

    expected = tail_size * tail_size / count
    if expected <= 0:
        return Unknown
    return len(worst_x & worst_y) / expected


def pair_co_movement(
    instrument_a: str,
    series_a: Mapping[date, float],
    instrument_b: str,
    series_b: Mapping[date, float],
    *,
    min_overlap: int = MIN_OVERLAP,
) -> PairCoMovement:
    """Score one pair over the dates both instruments were actually observed."""
    key_a, key_b = sorted((instrument_a, instrument_b))
    changes_a = daily_changes(series_a)
    changes_b = daily_changes(series_b)
    common = sorted(set(changes_a) & set(changes_b))

    if len(common) < min_overlap:
        return PairCoMovement(
            instrument_a=key_a,
            instrument_b=key_b,
            overlap_days=len(common),
        )

    xs = [changes_a[day] for day in common]
    ys = [changes_b[day] for day in common]
    if (key_a, key_b) != (instrument_a, instrument_b):
        xs, ys = ys, xs

    measured_correlation = correlation(xs, ys)
    measured_lift = tail_lift(xs, ys)
    return PairCoMovement(
        instrument_a=key_a,
        instrument_b=key_b,
        overlap_days=len(common),
        correlation=(
            None if measured_correlation is Unknown else float(measured_correlation)
        ),
        tail_lift=(None if measured_lift is Unknown else float(measured_lift)),
    )


def co_movement_matrix(
    series_by_id: Mapping[str, Mapping[date, float]],
    *,
    min_overlap: int = MIN_OVERLAP,
) -> dict[tuple[str, str], PairCoMovement]:
    """Every pair, keyed by its instrument ids in sorted order."""
    ids = sorted(series_by_id)
    matrix: dict[tuple[str, str], PairCoMovement] = {}
    for index, first in enumerate(ids):
        for second in ids[index + 1 :]:
            matrix[first, second] = pair_co_movement(
                first,
                series_by_id[first],
                second,
                series_by_id[second],
                min_overlap=min_overlap,
            )
    return matrix


def effective_positions(
    weights_bps: Mapping[str, int],
    matrix: Mapping[tuple[str, str], PairCoMovement],
) -> float:
    """``(sum w)^2 / (w' C w)`` — the effective number of independent positions.

    An unmeasured pair is charged as **fully correlated** (1.0), matching the
    unknown-sector rule: missing information must never become apparent
    diversity. Two positions we cannot compare are one bet until proven
    otherwise.
    """
    ids = [key for key, value in weights_bps.items() if value > 0]
    if not ids:
        return 0.0

    total = sum(weights_bps[key] for key in ids)
    variance = 0.0
    for first in ids:
        for second in ids:
            if first == second:
                pair_correlation = 1.0
            else:
                pair = matrix.get(tuple(sorted((first, second))))
                pair_correlation = (
                    1.0
                    if pair is None or pair.correlation is None
                    else pair.correlation
                )
            variance += weights_bps[first] * weights_bps[second] * pair_correlation

    if variance <= 0:
        return 0.0
    return (total * total) / variance


def hidden_tail_pairs(
    weights_bps: Mapping[str, int],
    matrix: Mapping[tuple[str, str], PairCoMovement],
    *,
    max_correlation: float = DECOUPLED_CORRELATION,
    min_tail_lift: float = TAIL_LIFT_ALERT,
) -> tuple[PairCoMovement, ...]:
    """Held pairs that look decoupled and share bad days anyway.

    These are the positions ``effective_positions`` counts as diversification
    and a bad week would not. Worst offenders first.
    """
    held = {key for key, value in weights_bps.items() if value > 0}
    found = [
        pair
        for (first, second), pair in matrix.items()
        if first in held
        and second in held
        and pair.correlation is not None
        and pair.tail_lift is not None
        and pair.correlation < max_correlation
        and pair.tail_lift >= min_tail_lift
    ]
    return tuple(
        sorted(
            found,
            key=lambda pair: (-(pair.tail_lift or 0.0), pair.instrument_a),
        )
    )


def report(
    weights_bps: Mapping[str, int],
    matrix: Mapping[tuple[str, str], PairCoMovement],
) -> DiversificationReport:
    """The measured diversification view for one allocation."""
    held = {key: value for key, value in weights_bps.items() if value > 0}
    unmeasured = sum(
        value
        for key, value in held.items()
        if not any(
            key in pair_key and pair.correlation is not None
            for pair_key, pair in matrix.items()
        )
    )
    lifts = sorted(
        pair.tail_lift
        for (first, second), pair in matrix.items()
        if first in held and second in held and pair.tail_lift is not None
    )
    median_lift: float | None = None
    if lifts:
        middle = len(lifts) // 2
        median_lift = (
            lifts[middle] if len(lifts) % 2 else (lifts[middle - 1] + lifts[middle]) / 2
        )

    return DiversificationReport(
        effective_positions=effective_positions(held, matrix),
        position_count=len(held),
        unmeasured_weight_bps=unmeasured,
        hidden_tail_pairs=hidden_tail_pairs(held, matrix),
        median_tail_lift=median_lift,
    )


__all__ = [
    "DECOUPLED_CORRELATION",
    "MAX_GAP_DAYS",
    "MIN_OVERLAP",
    "TAIL_FRACTION",
    "TAIL_LIFT_ALERT",
    "DiversificationReport",
    "PairCoMovement",
    "co_movement_matrix",
    "correlation",
    "daily_changes",
    "effective_positions",
    "hidden_tail_pairs",
    "pair_co_movement",
    "report",
    "tail_lift",
]
