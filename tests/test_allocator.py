from __future__ import annotations

import pytest

from open_allocator.core.allocator import ScoredVault, build_allocation
from open_allocator.core.schema import validate
from open_allocator.core.types import FactorScore, PolicyCaps, Vault, VaultScore


def vault(**updates: object) -> Vault:
    base = Vault(
        instrument_id="vault-a",
        protocol="morpho",
        chain_id=8453,
        asset="USDC",
        apy=0.04,
        tvl_usd=10_000_000,
        curator="curator-a",
        reward_dependence=0.1,
        oracle="chainlink",
        fee=0.05,
    )
    return base.model_copy(update=updates)


def score(instrument_id: str, value: float) -> VaultScore:
    return VaultScore(
        instrument_id=instrument_id,
        score=value,
        factors={
            "manual": FactorScore(
                raw_input=value,
                normalized_value=value,
                weight=1,
                unknown=False,
            )
        },
    )


def scored(vault_model: Vault, score_value: float) -> ScoredVault:
    return ScoredVault(
        score=score(vault_model.instrument_id, score_value),
        vault=vault_model,
    )


def weights_by_instrument(allocation: object) -> dict[str, float]:
    return {leg.instrument_id: leg.weight for leg in allocation.legs}


def assert_policy_caps_compatible(
    allocation: object,
    known_vaults: list[Vault],
    caps: PolicyCaps,
) -> None:
    vault_by_id = {item.instrument_id: item for item in known_vaults}
    protocol_weights: dict[str, float] = {}
    curator_weights: dict[str, float] = {}
    chain_weights: dict[int, float] = {}

    for leg in allocation.legs:
        vault_model = vault_by_id[leg.instrument_id]
        assert leg.weight <= caps.max_weight_per_instrument + 1e-9
        protocol_weights[vault_model.protocol] = (
            protocol_weights.get(vault_model.protocol, 0) + leg.weight
        )
        curator_key = str(vault_model.curator)
        curator_weights[curator_key] = curator_weights.get(curator_key, 0) + leg.weight
        chain_weights[vault_model.chain_id] = (
            chain_weights.get(vault_model.chain_id, 0) + leg.weight
        )

    assert all(
        weight <= caps.max_weight_per_protocol + 1e-9
        for weight in protocol_weights.values()
    )
    assert all(
        weight <= caps.max_weight_per_curator + 1e-9
        for weight in curator_weights.values()
    )
    assert all(
        weight <= caps.max_weight_per_chain + 1e-9
        for weight in chain_weights.values()
    )


def test_weights_and_usd_amounts_sum_to_requested_amount() -> None:
    vaults = [
        vault(instrument_id="vault-a", apy=0.04),
        vault(instrument_id="vault-b", apy=0.05, protocol="aave", curator="curator-b"),
        vault(
            instrument_id="vault-c",
            apy=0.03,
            protocol="compound",
            curator="curator-c",
        ),
    ]

    allocation = build_allocation(
        [scored(vaults[0], 0.8), scored(vaults[1], 0.7), scored(vaults[2], 0.6)],
        10_000,
    )

    assert sum(leg.weight for leg in allocation.legs) == pytest.approx(1.0)
    assert sum(leg.usd for leg in allocation.legs) == pytest.approx(10_000)
    assert allocation.total_usd == 10_000
    assert allocation.legs[0].instrument_id == "vault-a"


def test_caps_clamp_and_renormalize_with_concentration_warnings() -> None:
    vaults = [
        vault(
            instrument_id="vault-a",
            protocol="morpho",
            curator="curator-a",
            chain_id=8453,
        ),
        vault(
            instrument_id="vault-b",
            protocol="aave",
            curator="curator-b",
            chain_id=8453,
        ),
        vault(
            instrument_id="vault-c",
            protocol="compound",
            curator="curator-c",
            chain_id=10,
        ),
    ]
    allocation = build_allocation(
        [scored(vaults[0], 1.0), scored(vaults[1], 0.5), scored(vaults[2], 0.4)],
        1_000,
        caps={
            "max_weight_per_instrument": 0.5,
            "max_weight_per_protocol": 1.0,
            "max_weight_per_curator": 1.0,
            "max_weight_per_chain": 0.7,
        },
    )

    weights = weights_by_instrument(allocation)
    warnings = allocation.metadata["warnings"]

    assert weights["vault-a"] == pytest.approx(0.5)
    assert weights["vault-b"] + weights["vault-c"] == pytest.approx(0.5)
    assert weights["vault-a"] + weights["vault-b"] <= 0.7 + 1e-9
    assert sum(leg.weight for leg in allocation.legs) == pytest.approx(1.0)
    assert sum(leg.usd for leg in allocation.legs) == pytest.approx(1_000)
    assert any("cap_clamped:instrument:vault-a" in warning for warning in warnings)
    assert any("concentration:chain:8453" in warning for warning in warnings)


def test_risk_presets_are_deterministic_and_rank_differently() -> None:
    safer_low_yield = vault(instrument_id="safer-low-yield", apy=0.01, protocol="aave")
    riskier_high_yield = vault(
        instrument_id="riskier-high-yield",
        apy=0.10,
        protocol="morpho",
        curator="curator-b",
    )
    inputs = [scored(safer_low_yield, 0.9), scored(riskier_high_yield, 0.55)]

    first = build_allocation(inputs, 1_000, risk="aggressive")
    second = build_allocation(inputs, 1_000, risk="aggressive")
    conservative = build_allocation(inputs, 1_000, risk="conservative")

    assert first == second
    assert conservative.legs[0].instrument_id == "safer-low-yield"
    assert first.legs[0].instrument_id == "riskier-high-yield"
    assert conservative.metadata["preset"] == {"score_power": 3.0, "apy_weight": 0.0}
    assert first.metadata["preset"] == {"score_power": 1.0, "apy_weight": 2.0}


def test_empty_universe_returns_clear_empty_allocation() -> None:
    allocation = build_allocation([], 123.45)

    assert allocation.legs == ()
    assert allocation.total_usd == 0
    assert allocation.metadata["requested_amount_usd"] == 123.45
    assert allocation.metadata["unallocated_usd"] == 123.45
    assert allocation.metadata["warnings"] == ["empty_universe:no allocation built"]


def test_one_vault_universe_allocates_all_without_caps_and_warns() -> None:
    only_vault = vault(instrument_id="only-vault")

    allocation = build_allocation([scored(only_vault, 0.7)], 42)

    assert [(leg.instrument_id, leg.weight, leg.usd) for leg in allocation.legs] == [
        ("only-vault", 1.0, 42.0)
    ]
    assert (
        "concentration:single_vault:allocation has one instrument"
        in allocation.metadata["warnings"]
    )


def test_binding_caps_degrade_to_unallocated_instead_of_raising() -> None:
    only_vault = vault(instrument_id="only-vault")

    allocation = build_allocation(
        [scored(only_vault, 0.7)],
        42,
        caps={"max_weight_per_instrument": 0.5},
    )

    assert [(leg.instrument_id, leg.weight, leg.usd) for leg in allocation.legs] == [
        ("only-vault", 0.5, 21.0)
    ]
    assert allocation.total_usd == 21.0
    assert allocation.metadata["unallocated_usd"] == 21.0
    assert any(
        warning.startswith("caps_binding:unallocatable_weight")
        for warning in allocation.metadata["warnings"]
    )


def test_allocation_validates_against_schema() -> None:
    vaults = [
        vault(instrument_id="vault-a"),
        vault(instrument_id="vault-b", protocol="aave", curator="curator-b"),
    ]

    allocation = build_allocation(
        [scored(vaults[0], 0.8), scored(vaults[1], 0.6)],
        999.99,
    )
    payload = allocation.model_dump(mode="json")

    assert validate(payload, "allocation") == payload


def test_policy_caps_used_directly_for_future_policy_gate_compatibility() -> None:
    vaults = [
        vault(
            instrument_id="vault-a",
            protocol="morpho",
            curator="curator-a",
            chain_id=8453,
        ),
        vault(
            instrument_id="vault-b",
            protocol="aave",
            curator="curator-b",
            chain_id=8453,
        ),
        vault(
            instrument_id="vault-c",
            protocol="compound",
            curator="curator-c",
            chain_id=10,
        ),
        vault(
            instrument_id="vault-d",
            protocol="spark",
            curator="curator-d",
            chain_id=42161,
        ),
    ]
    caps = PolicyCaps(
        max_weight_per_instrument=0.4,
        max_weight_per_protocol=0.6,
        max_weight_per_curator=0.6,
        max_weight_per_chain=0.6,
        min_instrument_tvl_usd=5_000_000,
        max_reward_dependence=0.5,
    )

    allocation = build_allocation(
        [
            scored(vaults[0], 1.0),
            scored(vaults[1], 0.8),
            scored(vaults[2], 0.7),
            scored(vaults[3], 0.6),
        ],
        2_500,
        caps=caps,
    )

    assert_policy_caps_compatible(allocation, vaults, caps)
    assert sum(leg.weight for leg in allocation.legs) == pytest.approx(1.0)


def test_score_vault_pairs_are_accepted_in_either_order() -> None:
    first = vault(instrument_id="vault-a")
    second = vault(instrument_id="vault-b", protocol="aave", curator="curator-b")

    allocation = build_allocation(
        [
            (score(first.instrument_id, 0.8), first),
            (second, score(second.instrument_id, 0.6)),
        ],
        100,
    )

    assert [leg.instrument_id for leg in allocation.legs] == ["vault-a", "vault-b"]


def _unknown_curator_vaults() -> list[Vault]:
    # Many instruments on the same chain with an undisclosed curator: the
    # realistic 1Tx shape that used to make the curator cap infeasible.
    return [
        vault(
            instrument_id=f"vault-{index}",
            protocol=protocol,
            curator="Unknown",
            chain_id=8453,
        )
        for index, protocol in enumerate(
            ["morpho", "aave", "fluid", "morpho", "aave"]
        )
    ]


def test_unknown_curator_does_not_collapse_into_one_capped_bucket() -> None:
    vaults = _unknown_curator_vaults()
    caps = {
        "max_weight_per_instrument": 0.3,
        "max_weight_per_protocol": 1.0,
        "max_weight_per_curator": 0.4,
        "max_weight_per_chain": 1.0,
    }

    allocation = build_allocation(
        [scored(v, 0.6) for v in vaults],
        10_000,
        caps=caps,
    )

    # Full deployment succeeds despite every vault's curator being Unknown.
    assert sum(leg.weight for leg in allocation.legs) == pytest.approx(1.0)
    assert sum(leg.usd for leg in allocation.legs) == pytest.approx(10_000)
    assert allocation.metadata["unallocated_usd"] == 0
    assert not any(
        "caps_binding" in warning for warning in allocation.metadata["warnings"]
    )


def test_max_positions_keeps_only_top_n() -> None:
    vaults = [
        vault(instrument_id="vault-a", protocol="morpho", curator="curator-a"),
        vault(instrument_id="vault-b", protocol="aave", curator="curator-b"),
        vault(instrument_id="vault-c", protocol="compound", curator="curator-c"),
        vault(instrument_id="vault-d", protocol="spark", curator="curator-d"),
    ]
    allocation = build_allocation(
        [
            scored(vaults[0], 0.9),
            scored(vaults[1], 0.8),
            scored(vaults[2], 0.4),
            scored(vaults[3], 0.2),
        ],
        10_000,
        max_positions=2,
    )

    ids = [leg.instrument_id for leg in allocation.legs]
    assert ids == ["vault-a", "vault-b"]
    assert sum(leg.weight for leg in allocation.legs) == pytest.approx(1.0)
    assert any(
        "max_positions:kept=2:dropped=2" in warning
        for warning in allocation.metadata["warnings"]
    )


def test_min_position_usd_drops_dust_legs() -> None:
    vaults = [
        vault(instrument_id="vault-a", protocol="morpho", curator="curator-a"),
        vault(instrument_id="vault-b", protocol="aave", curator="curator-b"),
        vault(instrument_id="vault-c", protocol="compound", curator="curator-c"),
    ]
    allocation = build_allocation(
        [
            scored(vaults[0], 0.9),
            scored(vaults[1], 0.85),
            scored(vaults[2], 0.05),
        ],
        1_000,
        min_position_usd=100,
    )

    assert "vault-c" not in {leg.instrument_id for leg in allocation.legs}
    assert all(leg.usd >= 100 for leg in allocation.legs)
    assert sum(leg.usd for leg in allocation.legs) == pytest.approx(1_000)
    assert allocation.metadata["dropped_below_min_position"] == ["vault-c"]


def test_score_power_and_apy_weight_override_preset() -> None:
    vaults = [
        vault(instrument_id="vault-a", apy=0.02, curator="curator-a"),
        vault(instrument_id="vault-b", apy=0.20, protocol="aave", curator="curator-b"),
    ]
    inputs = [scored(vaults[0], 0.9), scored(vaults[1], 0.5)]

    tilted = build_allocation(inputs, 1_000, score_power=1.0, apy_weight=5.0)

    assert tilted.metadata["preset"] == {"score_power": 1.0, "apy_weight": 5.0}
    # A heavy APY tilt lifts the high-APY vault above the higher-scored one.
    assert tilted.legs[0].instrument_id == "vault-b"


def test_exclude_vetoes_instruments() -> None:
    vaults = [
        vault(instrument_id="vault-a", curator="curator-a"),
        vault(instrument_id="vault-b", protocol="aave", curator="curator-b"),
    ]
    allocation = build_allocation(
        [scored(vaults[0], 0.9), scored(vaults[1], 0.6)],
        1_000,
        exclude=["vault-a"],
    )

    assert [leg.instrument_id for leg in allocation.legs] == ["vault-b"]
    assert allocation.metadata["excluded"] == ["vault-a"]


def test_pins_are_honored_and_remainder_distributed() -> None:
    vaults = [
        vault(instrument_id="vault-a", curator="curator-a"),
        vault(instrument_id="vault-b", protocol="aave", curator="curator-b"),
        vault(instrument_id="vault-c", protocol="compound", curator="curator-c"),
    ]
    allocation = build_allocation(
        [scored(vaults[0], 0.5), scored(vaults[1], 0.5), scored(vaults[2], 0.5)],
        1_000,
        overrides={"vault-a": 0.6},
    )

    weights = weights_by_instrument(allocation)
    assert weights["vault-a"] == pytest.approx(0.6)
    # Remaining 0.4 split evenly between the two equally-scored vaults.
    assert weights["vault-b"] == pytest.approx(0.2)
    assert weights["vault-c"] == pytest.approx(0.2)
    assert allocation.metadata["pinned"] == ["vault-a"]


def test_pins_summing_over_one_are_rejected() -> None:
    vaults = [
        vault(instrument_id="vault-a", curator="curator-a"),
        vault(instrument_id="vault-b", protocol="aave", curator="curator-b"),
    ]

    with pytest.raises(ValueError, match="sum to"):
        build_allocation(
            [scored(vaults[0], 0.7), scored(vaults[1], 0.7)],
            1_000,
            overrides={"vault-a": 0.6, "vault-b": 0.6},
        )
