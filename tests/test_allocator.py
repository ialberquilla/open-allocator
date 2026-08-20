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
        weight <= caps.max_weight_per_chain + 1e-9 for weight in chain_weights.values()
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
        for index, protocol in enumerate(["morpho", "aave", "fluid", "morpho", "aave"])
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


def test_equal_weight_dust_drop_breaks_ties_on_score_not_instrument_id() -> None:
    """Equal weighting makes the id tiebreak the selection rule, so it must not decide.

    `equal_weight` gives every leg an identical `usd`, so `min(sub_min, key=usd)`
    is always a tie and whatever comes next in the key chooses which instrument
    the book holds. When that was `instrument_id`, an `equal_weight` sleeve
    trimmed to fit `min_position_usd` selected its holdings alphabetically — on
    a live shelf it kept 2.17%/2.40%/2.63% APY names over an 8.50% one, and
    nothing in the output said the choice was arbitrary.

    The ids here are ordered ADVERSARIALLY against score: the best vault sorts
    first, so the old behaviour drops exactly the one that should be kept.
    """
    vaults = [
        vault(instrument_id="vault-a", protocol="morpho", curator="curator-a"),
        vault(instrument_id="vault-b", protocol="aave", curator="curator-b"),
        vault(instrument_id="vault-c", protocol="compound", curator="curator-c"),
    ]
    allocation = build_allocation(
        [
            scored(vaults[0], 0.95),  # best, and sorts FIRST
            scored(vaults[1], 0.50),
            scored(vaults[2], 0.10),  # worst, and sorts LAST
        ],
        300,
        strategy="equal_weight",
        min_position_usd=150,
    )

    held = {leg.instrument_id for leg in allocation.legs}
    assert held == {"vault-a", "vault-b"}, (
        "the worst-scoring vault must be dropped; holding vault-c means the "
        "tiebreak selected on instrument_id"
    )
    assert allocation.metadata["dropped_below_min_position"] == ["vault-c"]
    assert all(leg.usd >= 150 for leg in allocation.legs)


def test_dust_drop_still_prefers_the_genuinely_smallest_leg() -> None:
    """Score is a TIEBREAK, not the ordering. A smaller leg goes first regardless.

    Guards the obvious over-correction: dropping by score alone would evict a
    well-sized weak leg while leaving true dust in place.
    """
    vaults = [
        vault(instrument_id="vault-a", protocol="morpho", curator="curator-a"),
        vault(instrument_id="vault-b", protocol="aave", curator="curator-b"),
        vault(instrument_id="vault-c", protocol="compound", curator="curator-c"),
    ]
    allocation = build_allocation(
        [
            scored(vaults[0], 0.90),
            scored(vaults[1], 0.88),
            # Lowest score of the three, but score_weighted still gives it a
            # substantial leg — the dust is elsewhere.
            scored(vaults[2], 0.80),
        ],
        1_000,
        min_position_usd=100,
    )

    assert all(leg.usd >= 100 for leg in allocation.legs)
    assert sum(leg.usd for leg in allocation.legs) == pytest.approx(1_000)


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


def test_sector_cap_clamps_weight_and_reports_the_unplaceable_remainder() -> None:
    # A monoculture shelf with a binding sector cap: the allocator must refuse
    # to place the excess rather than quietly ignore the dimension.
    vaults = [
        vault(instrument_id="vault-a", sector="VARIABLE_RATE_LENDING"),
        vault(
            instrument_id="vault-b",
            protocol="aave",
            curator="curator-b",
            sector="VARIABLE_RATE_LENDING",
        ),
    ]

    allocation = build_allocation(
        [scored(vaults[0], 0.8), scored(vaults[1], 0.7)],
        10_000,
        caps={"max_weight_per_sector": 0.6},
    )

    assert sum(leg.weight for leg in allocation.legs) == pytest.approx(0.6)
    assert any(
        warning.startswith("caps_binding:unallocatable_weight")
        for warning in allocation.metadata["warnings"]
    )


def test_sector_cap_lets_a_two_sleeve_shelf_stay_fully_invested() -> None:
    vaults = [
        vault(instrument_id="vault-a", sector="VARIABLE_RATE_LENDING"),
        vault(
            instrument_id="vault-b",
            protocol="sky",
            curator="curator-b",
            sector="SAVINGS_RATE",
        ),
    ]

    allocation = build_allocation(
        [scored(vaults[0], 0.8), scored(vaults[1], 0.7)],
        10_000,
        caps={"max_weight_per_sector": 0.6},
    )

    assert sum(leg.weight for leg in allocation.legs) == pytest.approx(1.0)
    assert all(leg.weight <= 0.6 + 1e-9 for leg in allocation.legs)


def test_unclassified_sectors_bind_the_cap_collectively() -> None:
    vaults = [
        vault(instrument_id="vault-a", sector=None),
        vault(instrument_id="vault-b", protocol="aave", curator="curator-b"),
    ]

    allocation = build_allocation(
        [scored(vaults[0], 0.8), scored(vaults[1], 0.7)],
        10_000,
        caps={"max_weight_per_sector": 0.6},
    )

    assert sum(leg.weight for leg in allocation.legs) == pytest.approx(0.6)


def test_omitted_sector_cap_leaves_allocation_unchanged() -> None:
    vaults = [
        vault(instrument_id="vault-a", sector=None),
        vault(instrument_id="vault-b", protocol="aave", curator="curator-b"),
    ]

    allocation = build_allocation(
        [scored(vaults[0], 0.8), scored(vaults[1], 0.7)],
        10_000,
    )

    assert sum(leg.weight for leg in allocation.legs) == pytest.approx(1.0)
    assert allocation.metadata["caps"]["max_weight_per_sector"] == 1.0


# ── caps headroom: build UNDER the caps, gate AT them ────────────────────────
#
# The bug these pin down (agent-showcase A7): `build-allocation` clamped a leg to
# exactly `max_weight_per_instrument` and `check-policy` then scored that same
# book against that same number, so the first dollar of execution friction put
# the book in permanent violation. Weights are built; dollars are executed.


def _six_vault_shelf() -> list[Vault]:
    """The shape of agent-showcase's live book: 4 morpho + 2 singletons.

    Under caps of 0.20/instrument and 0.60/protocol this shelf can place
    0.60 + 0.20 + 0.20 = EXACTLY 1.0. Fully deployed only by sitting on every
    ceiling at once, which is what makes it unable to carry headroom.
    """
    return [
        vault(instrument_id="v-a", protocol="avantis", curator="c-a", chain_id=8453),
        vault(instrument_id="v-b", protocol="tokemak", curator="c-b", chain_id=8453),
        vault(instrument_id="v-c", protocol="morpho", curator="c-c", chain_id=143),
        vault(instrument_id="v-d", protocol="morpho", curator="c-d", chain_id=8453),
        vault(instrument_id="v-e", protocol="morpho", curator="c-e", chain_id=8453),
        vault(instrument_id="v-f", protocol="morpho", curator="c-f", chain_id=143),
    ]


def _seven_vault_shelf() -> list[Vault]:
    """The same book plus a leg on a fourth protocol — 1.20 placeable."""
    return [
        *_six_vault_shelf(),
        vault(instrument_id="v-g", protocol="fluid", curator="c-g", chain_id=8453),
    ]


def _after_friction(
    allocation: object, lost_usd: float, instrument_id: str
) -> dict[str, float]:
    """Weights as `positions` will report them once a leg lands short.

    The book keeps every other leg's dollars and loses `lost_usd` off the total,
    which is what a paymaster reserve or an undelivered bridge leg actually does.
    """
    held = {
        leg.instrument_id: (
            leg.usd - lost_usd if leg.instrument_id == instrument_id else leg.usd
        )
        for leg in allocation.legs  # type: ignore[attr-defined]
    }
    total = sum(held.values())
    return {key: value / total for key, value in held.items()}


def test_without_headroom_a_capped_leg_breaches_on_the_first_dollar_of_friction() -> (
    None
):
    """A7, reproduced. This is the behaviour, not a bug in the test."""
    vaults = _six_vault_shelf()
    caps = {"max_weight_per_instrument": 0.20, "max_weight_per_protocol": 0.60}

    allocation = build_allocation(
        [scored(v, 1.0 - index * 0.05) for index, v in enumerate(vaults)],
        100.0,
        caps=caps,
    )

    built = weights_by_instrument(allocation)
    assert built["v-a"] == pytest.approx(0.20), (
        "the cap binds, which is the precondition"
    )

    # $0.78 of the Monad leg never arrives — the measured 2026-08-16 shortfall.
    held = _after_friction(allocation, 0.78, "v-f")

    assert held["v-a"] > 0.20
    assert held["v-a"] == pytest.approx(0.20 / (1 - 0.0078), rel=1e-6), (
        "the breach is exactly cap/(1-s): dollars held, book shrunk"
    )


def test_headroom_is_cancelled_when_the_caps_sum_to_exactly_one() -> None:
    """🔴 The trap. Headroom is not free room — it has to be spendable.

    Caps bound a share of the DEPLOYED book. Weight the caps refuse to place
    leaves as idle cash rather than diluting the book, so it shrinks the
    denominator by exactly the amount the numerator was tightened.
    """
    vaults = _six_vault_shelf()
    caps = {"max_weight_per_instrument": 0.20, "max_weight_per_protocol": 0.60}

    allocation = build_allocation(
        [scored(v, 1.0 - index * 0.05) for index, v in enumerate(vaults)],
        100.0,
        caps=caps,
        caps_headroom_bps=300,
    )

    assert allocation.total_usd == pytest.approx(97.0), "3% could not be placed"
    assert weights_by_instrument(allocation)["v-a"] == pytest.approx(0.194)

    held = _after_friction(allocation, 0.0, "v-f")
    assert held["v-a"] == pytest.approx(0.20), "straight back at the cap, 0 bps gained"

    assert allocation.metadata["effective_caps_headroom_bps"] == pytest.approx(0.0)
    assert any(
        warning.startswith("caps_headroom_cancelled:")
        for warning in allocation.metadata["warnings"]
    ), "a bare caps_binding warning does not say the headroom was cancelled"


def test_headroom_survives_the_same_friction_once_an_axis_is_unbound() -> None:
    vaults = _seven_vault_shelf()
    caps = {"max_weight_per_instrument": 0.20, "max_weight_per_protocol": 0.60}

    allocation = build_allocation(
        [scored(v, 1.0 - index * 0.05) for index, v in enumerate(vaults)],
        100.0,
        caps=caps,
        caps_headroom_bps=300,
    )

    assert sum(leg.usd for leg in allocation.legs) == pytest.approx(100.0), (
        "headroom must not leave capital idle when an axis can absorb it"
    )
    assert "effective_caps_headroom_bps" not in allocation.metadata

    held = _after_friction(allocation, 0.78, "v-f")

    assert max(held.values()) <= 0.20
    morpho = sum(held[key] for key in ("v-c", "v-d", "v-e", "v-f"))
    assert morpho <= 0.60


def test_headroom_tolerance_is_exactly_the_bps_it_was_given() -> None:
    """The promise: the book may shrink this much, relative, and caps still hold."""
    vaults = _seven_vault_shelf()
    allocation = build_allocation(
        [scored(v, 1.0 - index * 0.05) for index, v in enumerate(vaults)],
        100.0,
        caps={"max_weight_per_instrument": 0.20},
        caps_headroom_bps=300,
    )

    for shrink, still_holds in ((0.0299, True), (0.0301, False)):
        held = _after_friction(allocation, 100.0 * shrink, "v-f")
        assert (max(held.values()) <= 0.20) is still_holds


def test_headroom_is_relative_so_it_scales_with_the_cap() -> None:
    """A flat bps subtraction would under-protect the big caps. This does not."""
    vaults = _six_vault_shelf()
    allocation = build_allocation(
        [scored(v, 1.0 - index * 0.05) for index, v in enumerate(vaults)],
        100.0,
        caps={"max_weight_per_instrument": 0.20, "max_weight_per_protocol": 0.60},
        caps_headroom_bps=500,
    )

    caps = allocation.metadata["caps"]
    assert caps["max_weight_per_instrument"] == pytest.approx(0.19)  # 100 bps of room
    assert caps["max_weight_per_protocol"] == pytest.approx(0.57)  # 300 bps of room


def test_headroom_records_the_gate_caps_so_the_artifact_is_not_misread() -> None:
    vaults = _six_vault_shelf()
    allocation = build_allocation(
        [scored(v, 1.0 - index * 0.05) for index, v in enumerate(vaults)],
        100.0,
        caps={"max_weight_per_instrument": 0.20},
        caps_headroom_bps=300,
    )

    assert allocation.metadata["caps_headroom_bps"] == 300
    assert allocation.metadata["gate_caps"]["max_weight_per_instrument"] == 0.20
    assert allocation.metadata["caps"]["max_weight_per_instrument"] == pytest.approx(
        0.194
    )
    validate(allocation.model_dump(mode="json"), "allocation")


def test_zero_headroom_leaves_metadata_and_weights_byte_for_byte_unchanged() -> None:
    vaults = _six_vault_shelf()
    scored_vaults = [scored(v, 1.0 - index * 0.05) for index, v in enumerate(vaults)]
    caps = {"max_weight_per_instrument": 0.20}

    without = build_allocation(scored_vaults, 100.0, caps=caps)
    explicit_zero = build_allocation(
        scored_vaults, 100.0, caps=caps, caps_headroom_bps=0
    )

    assert without.model_dump() == explicit_zero.model_dump()
    assert "caps_headroom_bps" not in without.metadata


def test_headroom_does_not_invent_a_cap_where_the_policy_set_none() -> None:
    """An unset cap is 1.0. Shrinking it would be a constraint nobody asked for."""
    vaults = _six_vault_shelf()
    allocation = build_allocation(
        [scored(v, 1.0 - index * 0.05) for index, v in enumerate(vaults)],
        100.0,
        caps={"max_weight_per_instrument": 0.20},
        caps_headroom_bps=1_000,
    )

    assert allocation.metadata["caps"]["max_weight_per_sector"] == 1.0
    assert allocation.metadata["caps"]["max_weight_per_protocol"] == 1.0


def test_headroom_is_ignored_under_pins_and_says_so() -> None:
    vaults = _six_vault_shelf()
    allocation = build_allocation(
        [scored(v, 1.0 - index * 0.05) for index, v in enumerate(vaults)],
        100.0,
        caps={"max_weight_per_instrument": 0.20},
        caps_headroom_bps=300,
        overrides={"v-a": 0.5},
    )

    assert weights_by_instrument(allocation)["v-a"] == pytest.approx(0.5)
    assert "caps_headroom_ignored:overrides_present" in allocation.metadata["warnings"]


@pytest.mark.parametrize("bad", [-1, 10_000, 12_000, float("nan"), float("inf")])
def test_headroom_outside_zero_to_ten_thousand_bps_is_rejected(bad: float) -> None:
    vaults = _six_vault_shelf()
    with pytest.raises(ValueError, match="caps_headroom_bps"):
        build_allocation(
            [scored(v, 1.0) for v in vaults],
            100.0,
            caps={"max_weight_per_instrument": 0.20},
            caps_headroom_bps=bad,
        )
