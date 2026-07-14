import pytest
from pydantic import ValidationError

from open_allocator.core.types import (
    Allocation,
    AllocationLeg,
    FactorScore,
    Policy,
    PolicyAllowed,
    PolicyCaps,
    PolicyGates,
    PolicyWallet,
    TxPlan,
    TxStep,
    Unknown,
    Vault,
    VaultScore,
)


def sample_vault() -> Vault:
    return Vault(
        instrument_id="morpho-base-usdc-1",
        protocol="morpho",
        chain_id=8453,
        asset="USDC",
        apy=0.041,
        tvl_usd=7_500_000,
        curator="example-curator",
        reward_dependence=0.15,
        oracle="chainlink",
        fee=0.05,
    )


def sample_vault_score() -> VaultScore:
    return VaultScore(
        instrument_id="morpho-base-usdc-1",
        score=0.6,
        factors={
            "tvl": FactorScore(
                raw_input=7_500_000,
                normalized_value=0.8,
                weight=2,
                unknown=False,
            ),
            "reward_dependence": FactorScore(
                raw_input=0.15,
                normalized_value=0.2,
                weight=1,
                unknown=False,
            ),
            "oracle": FactorScore(
                raw_input=Unknown,
                normalized_value=None,
                weight=1,
                unknown=True,
            ),
        },
    )


def sample_policy() -> Policy:
    return Policy(
        wallet=PolicyWallet(mode="self-custody", signer="local-eoa"),
        allowed=PolicyAllowed(
            protocols=None,
            chains=None,
            assets=("USDC", "USDT", "DAI"),
            curators=None,
        ),
        caps=PolicyCaps(
            max_weight_per_instrument=0.30,
            max_weight_per_protocol=0.50,
            max_weight_per_curator=0.40,
            max_weight_per_chain=0.70,
            min_instrument_tvl_usd=5_000_000,
            max_reward_dependence=0.50,
        ),
        gates=PolicyGates(
            new_instrument_needs_approval=True,
            autonomous_rebalance=False,
            max_deploy_per_cycle_usd=25_000,
        ),
    )


@pytest.mark.parametrize(
    ("model", "model_type"),
    [
        (sample_vault(), Vault),
        (sample_vault_score(), VaultScore),
        (
            Allocation(
                legs=(
                    AllocationLeg(
                        instrument_id="morpho-base-usdc-1",
                        weight=1,
                        usd=1_000,
                    ),
                ),
                total_usd=1_000,
                metadata={"risk": "balanced"},
            ),
            Allocation,
        ),
        (
            TxPlan(
                steps=(
                    TxStep(
                        to="0x0000000000000000000000000000000000000001",
                        data="0x1234",
                        value=0,
                        chain_id=8453,
                        kind="approve",
                    ),
                    TxStep(
                        to="0x0000000000000000000000000000000000000002",
                        data="0xabcd",
                        value=0,
                        chain_id=8453,
                        kind="buy",
                    ),
                ),
                summary="Approve then buy morpho-base-usdc-1",
            ),
            TxPlan,
        ),
        (sample_policy(), Policy),
    ],
)
def test_models_round_trip_through_model_dump(model: object, model_type: type) -> None:
    assert model_type.model_validate(model.model_dump()) == model


def test_vault_risk_fields_accept_none_and_unknown() -> None:
    vault = Vault(
        instrument_id="unknown-risk-vault",
        protocol="morpho",
        chain_id=8453,
        asset="USDC",
        apy=0,
        tvl_usd=0,
        curator=None,
        reward_dependence=None,
        oracle=Unknown,
        fee=None,
    )

    assert vault.curator is None
    assert vault.reward_dependence is None
    assert vault.oracle == Unknown
    assert vault.fee is None


def test_extra_keys_are_forbidden() -> None:
    data = sample_vault().model_dump()
    data["unexpected"] = "typo"

    with pytest.raises(ValidationError) as error:
        Vault.model_validate(data)

    assert "Extra inputs are not permitted" in str(error.value)


def test_vault_score_reconstructs_score_from_known_factors() -> None:
    score = sample_vault_score()

    reconstructed = ((0.8 * 2) + (0.2 * 1)) / 3

    assert score.score == pytest.approx(reconstructed)


def test_vault_score_rejects_non_reconstructable_score() -> None:
    with pytest.raises(ValidationError) as error:
        VaultScore(
            instrument_id="morpho-base-usdc-1",
            score=0.1,
            factors={
                "tvl": FactorScore(
                    raw_input=7_500_000,
                    normalized_value=0.8,
                    weight=2,
                    unknown=False,
                ),
                "reward_dependence": FactorScore(
                    raw_input=0.15,
                    normalized_value=0.2,
                    weight=1,
                    unknown=False,
                ),
            },
        )

    assert "score is not reconstructable" in str(error.value)


def test_known_factor_requires_normalized_value() -> None:
    with pytest.raises(ValidationError) as error:
        FactorScore(raw_input=7_500_000, normalized_value=None, weight=1, unknown=False)

    assert "known factors require normalized_value" in str(error.value)
