from __future__ import annotations

import pytest

from open_allocator.core import strategies
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
