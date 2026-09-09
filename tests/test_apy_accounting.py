from open_allocator.core.apy_accounting import for_allocation
from open_allocator.core.types import Allocation, AllocationLeg, Vault


def _vault(
    instrument_id: str,
    advertised: float,
    base: float | None,
    reward: float | None,
) -> Vault:
    return Vault(
        instrument_id=instrument_id,
        protocol="test",
        chain_id=8453,
        asset="USDC",
        apy=advertised,
        apy_base=base,
        apy_reward=reward,
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
    assert result.apy_basis == "base"
    assert result.warnings() == ()
