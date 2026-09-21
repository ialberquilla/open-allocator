from open_allocator.core.apy_accounting import for_allocation, priced_reward_apy
from open_allocator.core.types import Allocation, AllocationLeg, RewardPriceBasis, Vault


def _vault(
    instrument_id: str,
    advertised: float,
    base: float | None,
    reward: float | None,
    reward_basis: RewardPriceBasis | None = "traded",
) -> Vault:
    return Vault(
        instrument_id=instrument_id,
        protocol="test",
        chain_id=8453,
        asset="USDC",
        apy=advertised,
        apy_base=base,
        apy_reward=reward,
        reward_price_basis=reward_basis,
        tvl_usd=1_000_000,
    )


def _allocation() -> Allocation:
    return Allocation(
        legs=(
            AllocationLeg(instrument_id="known", weight=0.6, usd=60),
            AllocationLeg(instrument_id="unknown", weight=0.4, usd=40),
        ),
        total_usd=100,
    )


def test_partial_base_coverage_never_manufactures_whole_book_accruing_apy() -> None:
    result = for_allocation(
        _allocation(),
        [
            _vault("known", 5.0, 4.0, 1.0),
            _vault("unknown", 8.0, None, None),
        ],
    )

    assert result.advertised_blended_apy_pct == 6.2
    assert result.measured_base_apy_pct == 4.0
    assert result.base_apy_coverage_bps == 6000
    assert result.unknown_base_weight_bps == 4000
    assert result.accruing_apy_pct is None
    assert result.apy_basis == "mixed_unknown"
    assert any("breakeven unavailable" in warning for warning in result.warnings())


def test_complete_split_exposes_base_and_reward_without_changing_headline() -> None:
    result = for_allocation(
        _allocation(),
        [
            _vault("known", 5.0, 4.0, 1.0),
            _vault("unknown", 8.0, 8.0, 0.0),
        ],
    )

    assert result.advertised_blended_apy_pct == 6.2
    assert result.measured_base_apy_pct == 5.6
    assert result.measured_reward_apy_pct == 0.6
    assert result.base_apy_coverage_bps == 10_000
    assert result.reward_apy_coverage_bps == 10_000
    assert result.accruing_apy_pct == 5.6
    assert result.priced_blended_apy_pct == 6.2
    assert result.apy_basis == "base"
    assert result.reward_basis == "traded"
    assert result.warnings() == ()


def test_an_emission_priced_reward_is_not_counted_and_says_so() -> None:
    """An emission-priced reward APY is reported but not counted."""
    result = for_allocation(
        Allocation(
            legs=(AllocationLeg(instrument_id="emission-priced", weight=1.0, usd=100),),
            total_usd=100,
        ),
        [
            _vault(
                "emission-priced",
                9.60,
                2.055,
                7.545,
                reward_basis="emission",
            )
        ],
    )

    # What upstream advertises is still reported — descriptive, not hidden.
    assert result.advertised_blended_apy_pct == 9.6
    # What we are willing to count is not.
    assert result.measured_reward_apy_pct is None
    assert result.reward_apy_coverage_bps == 0
    assert result.unpriced_reward_weight_bps == 10_000
    assert result.unknown_reward_weight_bps == 0
    assert result.priced_blended_apy_pct is None
    assert result.reward_basis == "mixed_unpriced"
    # Base coverage is complete, so the accruing view is unaffected: this
    # rule narrows the reward claim and nothing else.
    assert result.accruing_apy_pct == 2.055
    assert result.apy_basis == "base"
    assert any("apy_reward_unpriced" in warning for warning in result.warnings())


def test_an_unreported_basis_is_unpriced_rather_than_assumed_traded() -> None:
    result = for_allocation(
        Allocation(
            legs=(AllocationLeg(instrument_id="silent", weight=1.0, usd=100),),
            total_usd=100,
        ),
        [_vault("silent", 5.0, 4.0, 1.0, reward_basis=None)],
    )

    assert result.reward_apy_coverage_bps == 0
    assert result.unpriced_reward_weight_bps == 10_000
    assert result.priced_blended_apy_pct is None


def test_unreported_and_unpriced_rewards_are_counted_as_different_misses() -> None:
    result = for_allocation(
        _allocation(),
        [
            _vault("known", 5.0, 4.0, 1.0, reward_basis="emission"),
            _vault("unknown", 8.0, 8.0, None),
        ],
    )

    assert result.unpriced_reward_weight_bps == 6000
    assert result.unknown_reward_weight_bps == 4000
    assert result.reward_apy_coverage_bps == 0
    assert {warning.split(":")[0] for warning in result.warnings()} == {
        "apy_reward_unknown",
        "apy_reward_unpriced",
    }


def test_a_zero_reward_apy_needs_no_price() -> None:
    """Nothing is being overstated by pricing zero, whatever the basis says."""
    no_rewards = _vault("plain", 4.0, 4.0, 0.0, reward_basis="none")
    assert priced_reward_apy(no_rewards) == 0.0

    result = for_allocation(
        Allocation(
            legs=(AllocationLeg(instrument_id="plain", weight=1.0, usd=100),),
            total_usd=100,
        ),
        [no_rewards],
    )

    assert result.reward_apy_coverage_bps == 10_000
    assert result.unpriced_reward_weight_bps == 0
    assert result.reward_basis == "none"
    assert result.priced_blended_apy_pct == 4.0
    assert result.warnings() == ()


def test_a_leg_with_no_vault_is_unknown_not_unpriced() -> None:
    result = for_allocation(
        _allocation(),
        [_vault("known", 5.0, 4.0, 1.0)],
    )

    assert result.reward_apy_coverage_bps == 6000
    assert result.unknown_reward_weight_bps == 4000
    assert result.unpriced_reward_weight_bps == 0
