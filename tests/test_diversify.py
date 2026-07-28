from __future__ import annotations

import math
import random
from datetime import date, timedelta

import pytest

from open_allocator.core import diversify
from open_allocator.core.types import Unknown

START = date(2026, 1, 1)


def dated(values, *, start=START, step_days=1):
    return {
        start + timedelta(days=index * step_days): value
        for index, value in enumerate(values)
    }


def walk(count, *, slope=0.0, wobble=1.0, seed=1):
    """Deterministic pseudo-random level series, no numpy.

    Uses :class:`random.Random` rather than a hand-rolled LCG: sequences from a
    single-multiplier LCG under different seeds stay affine-related, so the
    "independent" walks in these tests were not independent and read a tail
    lift of 8.0.
    """
    rng = random.Random(seed)
    values = []
    level = 5.0
    for _ in range(count):
        level += slope + wobble * (rng.random() - 0.5)
        values.append(level)
    return values


class TestDailyChanges:
    def test_keys_changes_by_the_later_date(self):
        changes = diversify.daily_changes(dated([1.0, 3.0, 6.0]))
        assert changes == {
            START + timedelta(days=1): 2.0,
            START + timedelta(days=2): 3.0,
        }

    def test_drops_changes_spanning_a_long_gap(self):
        series = {
            START: 1.0,
            START + timedelta(days=1): 2.0,
            START + timedelta(days=30): 99.0,
        }
        changes = diversify.daily_changes(series)
        # the 29-day jump is missing data, not a daily move
        assert list(changes) == [START + timedelta(days=1)]

    def test_unordered_input_is_sorted_first(self):
        series = {
            START + timedelta(days=2): 6.0,
            START: 1.0,
            START + timedelta(days=1): 3.0,
        }
        assert diversify.daily_changes(series) == diversify.daily_changes(
            dated([1.0, 3.0, 6.0])
        )


class TestCorrelation:
    def test_identical_series_correlate_perfectly(self):
        series = walk(80)
        assert diversify.correlation(series, series) == pytest.approx(1.0)

    def test_mirrored_series_correlate_negatively(self):
        series = walk(80)
        mirrored = [-value for value in series]
        assert diversify.correlation(series, mirrored) == pytest.approx(-1.0)

    def test_flat_series_is_unknown_not_zero(self):
        """Zero would read as 'independent' and buy free diversification."""
        assert diversify.correlation([1.0] * 40, walk(40)) is Unknown

    def test_too_short_is_unknown(self):
        assert diversify.correlation([1.0, 2.0], [1.0, 3.0]) is Unknown

    def test_mismatched_lengths_are_unknown(self):
        assert diversify.correlation(walk(40), walk(30)) is Unknown


class TestTailLift:
    def test_identical_series_share_every_bad_day(self):
        series = walk(100)
        lift = diversify.tail_lift(series, series)
        # k=10, n=100 -> expected 1.0 joint, observed 10
        assert lift == pytest.approx(10.0)

    def test_independent_construction_lands_near_one(self):
        lift = diversify.tail_lift(walk(200, seed=7), walk(200, seed=99))
        assert 0.0 <= float(lift) <= 3.0

    def test_opposite_series_never_share_a_bad_day(self):
        series = walk(100)
        assert diversify.tail_lift(series, [-value for value in series]) == 0.0

    def test_too_short_is_unknown(self):
        assert diversify.tail_lift([1.0, 2.0], [1.0, 3.0]) is Unknown


class TestPairCoMovement:
    def test_short_overlap_reports_unscored_with_the_overlap_it_had(self):
        pair = diversify.pair_co_movement(
            "a", dated(walk(20)), "b", dated(walk(20, seed=5))
        )
        assert pair.scored is False
        assert pair.correlation is None
        assert pair.overlap_days == 19

    def test_ids_are_stored_sorted_and_correlation_is_symmetric(self):
        left, right = dated(walk(90)), dated(walk(90, seed=3))
        forward = diversify.pair_co_movement("zzz", left, "aaa", right)
        backward = diversify.pair_co_movement("aaa", right, "zzz", left)
        assert forward.instrument_a == backward.instrument_a == "aaa"
        assert forward.correlation == pytest.approx(backward.correlation)

    def test_only_common_dates_are_compared(self):
        """Series observed on different days must not be index-aligned."""
        shared = walk(90)
        offset = {
            day + timedelta(days=500): value for day, value in dated(shared).items()
        }
        pair = diversify.pair_co_movement("a", dated(shared), "b", offset)
        assert pair.overlap_days == 0
        assert pair.correlation is None


class TestEffectivePositions:
    @staticmethod
    def _matrix(correlation_value, ids=("a", "b")):
        first, second = sorted(ids)
        return {
            (first, second): diversify.PairCoMovement(
                instrument_a=first,
                instrument_b=second,
                overlap_days=90,
                correlation=correlation_value,
                tail_lift=1.0,
            )
        }

    def test_two_perfectly_correlated_positions_are_one_bet(self):
        result = diversify.effective_positions(
            {"a": 5000, "b": 5000}, self._matrix(1.0)
        )
        assert result == pytest.approx(1.0)

    def test_two_independent_positions_are_two_bets(self):
        result = diversify.effective_positions(
            {"a": 5000, "b": 5000}, self._matrix(0.0)
        )
        assert result == pytest.approx(2.0)

    def test_unmeasured_pairs_fail_closed(self):
        """Missing information must never become apparent diversity."""
        assert diversify.effective_positions(
            {"a": 5000, "b": 5000}, {}
        ) == pytest.approx(1.0)

    def test_concentrated_weights_score_below_position_count(self):
        result = diversify.effective_positions(
            {"a": 9000, "b": 1000}, self._matrix(0.0)
        )
        assert 1.0 < result < 2.0

    def test_zero_weight_legs_are_ignored(self):
        held = diversify.effective_positions({"a": 5000, "b": 5000}, self._matrix(0.0))
        padded = diversify.effective_positions(
            {"a": 5000, "b": 5000, "c": 0}, self._matrix(0.0)
        )
        assert held == pytest.approx(padded)

    def test_empty_allocation_is_zero_not_one(self):
        assert diversify.effective_positions({}, {}) == 0.0


class TestHiddenTailPairs:
    @staticmethod
    def _pair(first, second, correlation_value, lift):
        return diversify.PairCoMovement(
            instrument_a=first,
            instrument_b=second,
            overlap_days=120,
            correlation=correlation_value,
            tail_lift=lift,
        )

    def test_surfaces_decoupled_pairs_that_crash_together(self):
        matrix = {
            ("a", "b"): self._pair("a", "b", 0.05, 3.4),
            ("a", "c"): self._pair("a", "c", 0.02, 1.1),
            ("b", "c"): self._pair("b", "c", 0.80, 4.0),
        }
        found = diversify.hidden_tail_pairs({"a": 3400, "b": 3300, "c": 3300}, matrix)
        # a/c is genuinely decoupled; b/c already reads as correlated
        assert [(pair.instrument_a, pair.instrument_b) for pair in found] == [
            ("a", "b")
        ]

    def test_ignores_pairs_that_are_not_held(self):
        matrix = {("a", "b"): self._pair("a", "b", 0.05, 3.4)}
        assert diversify.hidden_tail_pairs({"a": 10000}, matrix) == ()

    def test_worst_offenders_come_first(self):
        matrix = {
            ("a", "b"): self._pair("a", "b", 0.05, 2.5),
            ("a", "c"): self._pair("a", "c", 0.05, 6.0),
        }
        found = diversify.hidden_tail_pairs({"a": 3400, "b": 3300, "c": 3300}, matrix)
        assert [pair.tail_lift for pair in found] == [6.0, 2.5]


class TestReport:
    def test_reports_measured_count_and_unmeasured_weight(self):
        series = {
            "a": dated(walk(120, seed=1)),
            "b": dated(walk(120, seed=2)),
            "c": dated(walk(120, seed=3)),
        }
        matrix = diversify.co_movement_matrix(series)
        result = diversify.report({"a": 3400, "b": 3300, "c": 3300}, matrix)
        assert result.position_count == 3
        assert result.unmeasured_weight_bps == 0
        assert 1.0 <= result.effective_positions <= 3.0
        assert result.median_tail_lift is not None

    def test_an_unmeasurable_holding_is_charged_as_correlated(self):
        series = {"a": dated(walk(120, seed=1)), "b": dated(walk(120, seed=2))}
        matrix = diversify.co_movement_matrix(series)
        with_ghost = diversify.report({"a": 3400, "b": 3300, "ghost": 3300}, matrix)
        assert with_ghost.unmeasured_weight_bps == 3300
        # the ghost cannot buy diversification it has not demonstrated
        assert (
            with_ghost.effective_positions
            < diversify.report({"a": 5000, "b": 5000}, matrix).effective_positions + 1.0
        )

    def test_identical_histories_collapse_to_one_position(self):
        shared = dated(walk(150))
        matrix = diversify.co_movement_matrix({"a": shared, "b": dict(shared)})
        result = diversify.report({"a": 5000, "b": 5000}, matrix)
        assert result.effective_positions == pytest.approx(1.0)
        # every bad day is shared: k joint hits against a k^2/n baseline
        changes = len(shared) - 1
        tail = int(changes * diversify.TAIL_FRACTION)
        assert math.isclose(result.median_tail_lift, changes / tail, rel_tol=1e-6)


class TestCoMovementMatrix:
    def test_keys_are_sorted_pairs_and_cover_every_combination(self):
        series = {
            name: dated(walk(90, seed=index)) for index, name in enumerate("abcd")
        }
        matrix = diversify.co_movement_matrix(series)
        assert len(matrix) == 6
        assert all(first < second for first, second in matrix)
