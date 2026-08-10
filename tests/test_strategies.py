from __future__ import annotations

import random
from datetime import date, timedelta

import pytest

from open_allocator.core import diversify, strategies
from open_allocator.core.allocator import ScoredVault, build_allocation
from open_allocator.core.strategies import StrategyError
from open_allocator.core.types import FactorScore, Vault, VaultScore


def vault(instrument_id: str, **updates: object) -> Vault:
    base = Vault(
        instrument_id=instrument_id,
        protocol="protocol-" + instrument_id,
        chain_id=8453,
        asset="USDC",
        apy=0.05,
        tvl_usd=10_000_000,
    )
    return base.model_copy(update=updates)


def score(instrument_id: str, value: float) -> VaultScore:
    return VaultScore(
        instrument_id=instrument_id,
        score=value,
        factors={
            "manual": FactorScore(
                raw_input=value, normalized_value=value, weight=1, unknown=False
            )
        },
    )


def scored(instrument_id: str, score_value: float, **updates: object) -> ScoredVault:
    return ScoredVault(
        score=score(instrument_id, score_value),
        vault=vault(instrument_id, **updates),
    )


def weights(
    records: list[ScoredVault], strategy: str, **params: object
) -> dict[str, float]:
    desired, _ = strategies.desired_weights(
        strategy,
        records,
        params=params,
    )
    return {records[i].vault.instrument_id: desired[i] for i in range(len(records))}


# --- registry --------------------------------------------------------------


def test_available_includes_all_named_strategies() -> None:
    assert set(strategies.available()) == {
        "score_weighted",
        "equal_weight",
        "risk_parity",
        "inverse_vol",
        "decorrelated",
        "core_satellite",
        "sleeves",
        "ladder",
    }


def test_unknown_strategy_raises() -> None:
    with pytest.raises(StrategyError):
        strategies.desired_weights("nope", [scored("a", 0.5)])


def test_desired_weights_sum_to_one() -> None:
    records = [scored("a", 0.9), scored("b", 0.5), scored("c", 0.1)]
    for name in strategies.available():
        desired, _ = strategies.desired_weights(name, records)
        assert sum(desired) == pytest.approx(1.0), name


# --- equal_weight ----------------------------------------------------------


def test_equal_weight_is_uniform() -> None:
    records = [scored("a", 0.9), scored("b", 0.1)]
    assert weights(records, "equal_weight") == pytest.approx({"a": 0.5, "b": 0.5})


def test_equal_weight_top_n_keeps_best_scores() -> None:
    records = [scored("a", 0.9), scored("b", 0.5), scored("c", 0.1)]
    result = weights(records, "equal_weight", top_n=2)
    assert result["a"] == pytest.approx(0.5)
    assert result["b"] == pytest.approx(0.5)
    assert result["c"] == pytest.approx(0.0)


# --- risk_parity -----------------------------------------------------------


def test_risk_parity_favors_lower_volatility() -> None:
    calm = scored("calm", 0.5, apy_series=(5.0, 5.1, 4.9, 5.0))
    wild = scored("wild", 0.5, apy_series=(2.0, 8.0, 1.0, 9.0))
    result = weights([calm, wild], "risk_parity")
    assert result["calm"] > result["wild"]


def test_risk_parity_falls_back_to_equal_without_history() -> None:
    records = [scored("a", 0.5), scored("b", 0.5)]  # no apy_series
    result = weights(records, "risk_parity")
    assert result == pytest.approx({"a": 0.5, "b": 0.5})


def test_inverse_vol_is_alias_of_risk_parity() -> None:
    calm = scored("calm", 0.5, apy_series=(5.0, 5.1, 4.9))
    wild = scored("wild", 0.5, apy_series=(1.0, 9.0, 2.0))
    assert weights([calm, wild], "inverse_vol") == pytest.approx(
        weights([calm, wild], "risk_parity")
    )


# --- core_satellite --------------------------------------------------------


def test_core_satellite_splits_weight_by_core_weight() -> None:
    records = [
        scored("hi1", 0.9),
        scored("hi2", 0.8),
        scored("lo1", 0.2),
        scored("lo2", 0.1),
    ]
    result = weights(records, "core_satellite", core_weight=0.75, core_count=2)
    core = result["hi1"] + result["hi2"]
    satellite = result["lo1"] + result["lo2"]
    assert core == pytest.approx(0.75)
    assert satellite == pytest.approx(0.25)


def test_core_satellite_redistributes_empty_satellite() -> None:
    records = [scored("a", 0.9), scored("b", 0.8)]
    # core_count == all -> satellite empty, its weight flows to core.
    result = weights(records, "core_satellite", core_weight=0.8, core_count=2)
    assert sum(result.values()) == pytest.approx(1.0)


# --- sleeves ---------------------------------------------------------------


def test_sleeves_default_tiers_hit_target_weights() -> None:
    records = [
        scored("safe", 0.8),
        scored("med", 0.45),
        scored("risky", 0.1),
    ]
    result = weights(records, "sleeves")
    assert result["safe"] == pytest.approx(0.5)
    assert result["med"] == pytest.approx(0.3)
    assert result["risky"] == pytest.approx(0.2)


def test_sleeves_rejects_malformed_tier() -> None:
    with pytest.raises(StrategyError):
        strategies.desired_weights(
            "sleeves",
            [scored("a", 0.5)],
            params={"tiers": [{"name": "x"}]},
        )


# --- sleeves: min_positions ------------------------------------------------


def tiered(**mins: int) -> list[dict[str, object]]:
    """The default ladder, with a `min_positions` floor on the named tiers."""
    ladder = [
        {"name": "safe", "min_score": 0.6, "max_score": 1.01, "weight": 0.5},
        {"name": "med", "min_score": 0.3, "max_score": 0.6, "weight": 0.3},
        {"name": "risky", "min_score": 0.0, "max_score": 0.3, "weight": 0.2},
    ]
    for tier in ladder:
        if str(tier["name"]) in mins:
            tier["min_positions"] = mins[str(tier["name"])]
    return ladder


def by_tier(result: dict[str, float]) -> dict[str, float]:
    """Sum weights by the tier prefix in each instrument id."""
    totals = {"safe": 0.0, "med": 0.0, "risky": 0.0}
    for instrument_id, weight in result.items():
        totals[instrument_id.split("-")[0]] += weight
    return totals


def full_ladder() -> list[ScoredVault]:
    return [
        scored("safe-1", 0.9),
        scored("safe-2", 0.7),
        scored("med-1", 0.5),
        scored("med-2", 0.35),
        scored("risky-1", 0.2),
        scored("risky-2", 0.05),
    ]


def warnings_for(
    records: list[ScoredVault], strategy: str, **params: object
) -> list[str]:
    _, found = strategies.desired_weights(strategy, records, params=params)
    return found


def test_sleeves_min_positions_met_leaves_targets_untouched() -> None:
    records = full_ladder()
    result = weights(records, "sleeves", tiers=tiered(safe=2, med=2, risky=2))
    assert by_tier(result) == pytest.approx({"safe": 0.5, "med": 0.3, "risky": 0.2})
    assert warnings_for(records, "sleeves", tiers=tiered(safe=2, med=2, risky=2)) == []


def test_sleeves_underfilled_tier_is_dropped_whole_not_clamped() -> None:
    # One risky instrument against a floor of 12: the sleeve is not held at a
    # twelfth of its target, it is not held at all.
    records = full_ladder()[:5]
    params = {"tiers": tiered(risky=12)}
    result = by_tier(weights(records, "sleeves", **params))

    assert result["risky"] == pytest.approx(0.0)
    # 0.2 splits over safe and med in proportion to their own targets (5:3).
    assert result["safe"] == pytest.approx(0.625)
    assert result["med"] == pytest.approx(0.375)
    assert sum(result.values()) == pytest.approx(1.0)
    assert "sleeve_underfilled:risky:1/12:weight_redistributed" in warnings_for(
        records, "sleeves", **params
    )


def test_sleeves_underfilled_middle_tier_moves_weight_up_only() -> None:
    # The discriminating case: med is dropped, and risky must not grow. Spread
    # proportionally across every survivor, risky would go 0.2 -> 0.286.
    records = [
        scored("safe-1", 0.9),
        scored("safe-2", 0.7),
        scored("med-1", 0.5),
        scored("risky-1", 0.2),
        scored("risky-2", 0.05),
    ]
    params = {"tiers": tiered(med=2)}
    result = by_tier(weights(records, "sleeves", **params))

    assert result["risky"] == pytest.approx(0.2)
    assert result["safe"] == pytest.approx(0.8)
    assert result["med"] == pytest.approx(0.0)


def test_sleeves_empty_middle_tier_also_moves_weight_up_only() -> None:
    # An empty tier is the extreme of an underfilled one and takes the same
    # path, so a vacant middle band cannot buy the book more risk either.
    records = [scored("safe-1", 0.9), scored("safe-2", 0.7), scored("risky-1", 0.2)]
    result = by_tier(weights(records, "sleeves"))

    assert result["risky"] == pytest.approx(0.2)
    assert result["safe"] == pytest.approx(0.8)
    assert "sleeve_empty:med:weight_redistributed" in warnings_for(records, "sleeves")


def test_sleeves_underfilled_safest_tier_warns_that_it_redistributed_down() -> None:
    # Nothing sits above `safe`, so its target has to go down the ladder. That
    # raises the book's risk and must be said out loud.
    records = [
        scored("safe-1", 0.9),
        scored("med-1", 0.5),
        scored("med-2", 0.35),
        scored("risky-1", 0.2),
        scored("risky-2", 0.05),
    ]
    params = {"tiers": tiered(safe=3)}
    result = by_tier(weights(records, "sleeves", **params))
    found = warnings_for(records, "sleeves", **params)

    assert result["med"] == pytest.approx(0.6)
    assert result["risky"] == pytest.approx(0.4)
    assert "sleeve_underfilled:safe:1/3:weight_redistributed" in found
    assert "sleeve_no_safer_tier:safe:weight_redistributed_down" in found


def test_sleeves_redistribution_is_independent_of_drop_order() -> None:
    # safe and med both fall short; each absorbs against original targets, so
    # the survivor's share cannot depend on which was processed first.
    records = [scored("safe-1", 0.9), scored("med-1", 0.5), scored("risky-1", 0.2)]
    result = by_tier(weights(records, "sleeves", tiers=tiered(safe=2, med=2)))
    assert result["risky"] == pytest.approx(1.0)


def test_sleeves_all_tiers_underfilled_falls_back_to_equal_weights() -> None:
    records = full_ladder()
    params = {"tiers": tiered(safe=9, med=9, risky=9)}
    result = weights(records, "sleeves", **params)
    found = warnings_for(records, "sleeves", **params)

    assert list(result.values()) == pytest.approx([1 / 6] * 6)
    assert "sleeves:no_populated_tiers:using_equal_weights" in found
    assert "sleeve_underfilled:safe:2/9:weight_redistributed" in found


def test_sleeves_min_positions_does_not_change_the_within_tier_split() -> None:
    # Dropping a tier reweights the bands; it must not disturb how the
    # surviving band ranks its own instruments.
    records = full_ladder()[:5]
    base = weights(records, "sleeves")
    dropped = weights(records, "sleeves", tiers=tiered(risky=12))
    assert dropped["safe-1"] / dropped["safe-2"] == pytest.approx(
        base["safe-1"] / base["safe-2"]
    )


@pytest.mark.parametrize("bad", [-1, 1.5, "3", True])
def test_sleeves_rejects_invalid_min_positions(bad: object) -> None:
    with pytest.raises(StrategyError):
        strategies.desired_weights(
            "sleeves",
            [scored("a", 0.5)],
            params={
                "tiers": [
                    {
                        "name": "only",
                        "min_score": 0.0,
                        "max_score": 1.01,
                        "weight": 1.0,
                        "min_positions": bad,
                    }
                ]
            },
        )


# --- integration through build_allocation ----------------------------------


def test_build_allocation_dispatches_on_strategy() -> None:
    records = [scored("a", 0.9), scored("b", 0.5)]
    allocation = build_allocation(records, 1000.0, strategy="equal_weight")
    legs = {leg.instrument_id: leg.weight for leg in allocation.legs}
    assert legs == pytest.approx({"a": 0.5, "b": 0.5})
    assert allocation.metadata["strategy"] == "equal_weight"


def test_build_allocation_rejects_unknown_strategy() -> None:
    with pytest.raises(ValueError, match="unsupported strategy"):
        build_allocation([scored("a", 0.5)], 1000.0, strategy="bogus")


def test_default_strategy_is_score_weighted_and_recorded() -> None:
    allocation = build_allocation([scored("a", 0.5)], 1000.0)
    assert allocation.metadata["strategy"] == "score_weighted"


# --- decorrelated ----------------------------------------------------------


def _dated(values: list[float]) -> tuple[tuple[date, float], ...]:
    start = date(2026, 1, 1)
    return tuple((start + timedelta(days=i), v) for i, v in enumerate(values))


def _walk(count: int, *, seed: int) -> list[float]:
    rng = random.Random(seed)
    level = 5.0
    out = []
    for _ in range(count):
        level += rng.random() - 0.5
        out.append(level)
    return out


def _mirror(values: list[float]) -> list[float]:
    """Same path, opposite sign — perfectly negatively correlated."""
    return [10.0 - v for v in values]


def test_decorrelated_without_history_is_score_weighted() -> None:
    """Construction degrades loudly; the policy layer is what fails closed."""
    records = [scored("a", 0.9), scored("b", 0.5)]
    desired, warnings = strategies.desired_weights("decorrelated", records)

    assert desired == strategies.desired_weights("score_weighted", records)[0]
    assert "decorrelated:no_history:using_score_weights" in warnings


def test_decorrelated_penalises_a_duplicate_position() -> None:
    """Two names for one position should not earn two positions' worth of weight."""
    shared = _dated(_walk(120, seed=1))
    records = [
        scored("dup-a", 0.9, apy_daily=shared),
        scored("dup-b", 0.9, apy_daily=shared),
        scored("independent", 0.9, apy_daily=_dated(_walk(120, seed=2))),
    ]
    result = weights(records, "decorrelated")

    assert result["independent"] > result["dup-a"]
    assert result["dup-a"] == pytest.approx(result["dup-b"])
    # the duplicated pair together should not swamp the independent leg
    assert result["dup-a"] + result["dup-b"] < 2 * result["independent"]


def test_decorrelated_does_not_punish_a_hedge() -> None:
    """A negatively correlated pair diversifies; it must not be down-weighted."""
    path = _walk(120, seed=3)
    records = [
        scored("long", 0.9, apy_daily=_dated(path)),
        scored("hedge", 0.9, apy_daily=_dated(_mirror(path))),
    ]
    result = weights(records, "decorrelated")

    assert result["long"] == pytest.approx(result["hedge"])


def test_decorrelated_selection_prefers_a_new_bet_over_a_better_duplicate() -> None:
    """The whole point: rank against the book, not one instrument at a time."""
    shared = _dated(_walk(150, seed=4))
    records = [
        scored("best", 0.95, apy_daily=shared),
        scored("copy-of-best", 0.90, apy_daily=shared),
        scored("different", 0.60, apy_daily=_dated(_walk(150, seed=5))),
    ]
    result = weights(records, "decorrelated", top_n=2)

    assert result["best"] > 0
    assert result["different"] > 0
    assert result["copy-of-best"] == 0


def test_score_weighted_top_n_takes_the_duplicate_instead() -> None:
    """Contrast fixture: this is the behaviour `decorrelated` exists to fix."""
    shared = _dated(_walk(150, seed=4))
    records = [
        scored("best", 0.95, apy_daily=shared),
        scored("copy-of-best", 0.90, apy_daily=shared),
        scored("different", 0.60, apy_daily=_dated(_walk(150, seed=5))),
    ]
    allocation = build_allocation(records, 1000.0, max_positions=2)
    held = {leg.instrument_id for leg in allocation.legs if leg.weight > 0}

    assert held == {"best", "copy-of-best"}


def test_decorrelated_raises_the_measured_effective_position_count() -> None:
    shared = _dated(_walk(150, seed=6))
    records = [
        scored("dup-a", 0.9, apy_daily=shared),
        scored("dup-b", 0.9, apy_daily=shared),
        scored("dup-c", 0.9, apy_daily=shared),
        scored("independent", 0.5, apy_daily=_dated(_walk(150, seed=7))),
    ]
    series = {r.vault.instrument_id: dict(r.vault.apy_daily) for r in records}
    matrix = diversify.co_movement_matrix(series)

    def effective(strategy: str) -> float:
        desired, _ = strategies.desired_weights(strategy, records)
        return diversify.effective_positions(
            {
                records[i].vault.instrument_id: int(round(desired[i] * 10_000))
                for i in range(len(records))
            },
            matrix,
        )

    assert effective("decorrelated") > effective("score_weighted")


def test_decorrelated_treats_unmeasurable_instruments_as_correlated() -> None:
    """Unknown co-movement must not buy weight it has not demonstrated.

    The penalty is relative to an *independent* book: charged as correlated
    with everything, an unmeasurable leg carries the load of the whole book
    while each independent leg carries only its own plus that one. In a bare
    pair the two are symmetric — both are one bet — so this needs a book to
    show up at all.
    """
    records = [
        scored("indep-a", 0.9, apy_daily=_dated(_walk(120, seed=8))),
        scored("indep-b", 0.9, apy_daily=_dated(_walk(120, seed=12))),
        scored("indep-c", 0.9, apy_daily=_dated(_walk(120, seed=13))),
        scored("unmeasured", 0.9),
    ]
    result = weights(records, "decorrelated")

    assert result["unmeasured"] < result["indep-a"]
    assert result["unmeasured"] < result["indep-b"]
    assert result["unmeasured"] < result["indep-c"]


def test_decorrelated_unknown_correlation_is_tunable() -> None:
    records = [
        scored("indep-a", 0.9, apy_daily=_dated(_walk(120, seed=8))),
        scored("indep-b", 0.9, apy_daily=_dated(_walk(120, seed=12))),
        scored("unmeasured", 0.9),
    ]
    charged = weights(records, "decorrelated")
    trusting = weights(records, "decorrelated", unknown_correlation=0.0)

    assert charged["unmeasured"] < trusting["unmeasured"]


def test_decorrelated_rejects_an_out_of_range_unknown_correlation() -> None:
    records = [scored("a", 0.9, apy_daily=_dated(_walk(120, seed=9)))]
    with pytest.raises(StrategyError):
        strategies.desired_weights(
            "decorrelated", records, params={"unknown_correlation": 1.5}
        )


def test_decorrelated_top_n_larger_than_universe_keeps_everything() -> None:
    records = [
        scored("a", 0.9, apy_daily=_dated(_walk(120, seed=10))),
        scored("b", 0.8, apy_daily=_dated(_walk(120, seed=11))),
    ]
    result = weights(records, "decorrelated", top_n=99)

    assert all(value > 0 for value in result.values())
