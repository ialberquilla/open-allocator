from __future__ import annotations

import pytest

from open_allocator.core.scoring import DEFAULT_WEIGHTS, score_vault
from open_allocator.core.types import Unknown, Vault


def vault(**updates: object) -> Vault:
    base = Vault(
        instrument_id="vault-a",
        protocol="morpho",
        chain_id=8453,
        asset="USDC",
        apy=0.04,
        tvl_usd=10_000_000,
        apy_stability=0.2,
        reward_dependence=0.2,
        liquidity=5_000_000,
        oracle="chainlink",
        fee=0.05,
        curator="example-curator",
        market_concentration=0.4,
        collateral_mix={"USDC": 0.6, "USDT": 0.4},
    )
    return base.model_copy(update=updates)


def recompute(score: object) -> float:
    known_factors = [
        factor for factor in score.factors.values() if not factor.unknown
    ]
    total_weight = sum(factor.weight for factor in known_factors)
    return (
        sum(
            factor.normalized_value * factor.weight
            for factor in known_factors
            if factor.normalized_value is not None
        )
        / total_weight
    )


def test_score_vault_is_exactly_deterministic() -> None:
    weights = dict(DEFAULT_WEIGHTS)

    first = score_vault(vault(), weights)
    second = score_vault(vault(), weights)

    assert first == second


def test_composite_is_recomputable_from_factor_breakdown() -> None:
    scored = score_vault(vault())

    assert scored.score == pytest.approx(recompute(scored))


def test_unknown_factors_are_excluded_and_weights_redistribute() -> None:
    scored = score_vault(
        vault(reward_dependence=Unknown),
        weights={"tvl": 1, "reward_dependence": 3},
    )

    reward_factor = scored.factors["reward_dependence"]

    assert reward_factor.raw_input == Unknown
    assert reward_factor.normalized_value is None
    assert reward_factor.weight == 3
    assert reward_factor.unknown is True
    assert scored.score == scored.factors["tvl"].normalized_value


def test_all_unknown_factors_do_not_crash_or_invent_values() -> None:
    scored = score_vault(vault(oracle=Unknown), weights={"oracle": 1})

    assert scored.score == 0
    assert scored.factors["oracle"].unknown is True
    assert scored.factors["oracle"].normalized_value is None


def test_higher_tvl_is_not_worse_all_else_equal() -> None:
    lower = score_vault(vault(tvl_usd=1_000_000), weights={"tvl": 1})
    higher = score_vault(vault(tvl_usd=20_000_000), weights={"tvl": 1})

    assert higher.score >= lower.score


def test_lower_reward_dependence_is_not_worse_all_else_equal() -> None:
    higher_dependence = score_vault(
        vault(reward_dependence=0.8),
        weights={"reward_dependence": 1},
    )
    lower_dependence = score_vault(
        vault(reward_dependence=0.1),
        weights={"reward_dependence": 1},
    )

    assert lower_dependence.score >= higher_dependence.score


def test_replaced_weight_vector_changes_ranking_as_expected() -> None:
    deep_emissions_vault = vault(
        instrument_id="deep-emissions",
        tvl_usd=50_000_000,
        reward_dependence=1.0,
    )
    small_organic_vault = vault(
        instrument_id="small-organic",
        tvl_usd=100_000,
        reward_dependence=0.0,
    )

    tvl_weighted = {"tvl": 1, "reward_dependence": 0.1}
    reward_weighted = {"tvl": 0.1, "reward_dependence": 1}

    assert score_vault(deep_emissions_vault, tvl_weighted).score > score_vault(
        small_organic_vault,
        tvl_weighted,
    ).score
    assert score_vault(small_organic_vault, reward_weighted).score > score_vault(
        deep_emissions_vault,
        reward_weighted,
    ).score
