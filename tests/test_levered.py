"""Levered-loop economics, pinned against a fixed rate snapshot.

The fixture is one lending pool holding two stablecoins, ``A`` and ``B``, whose
rewards are paid in a single token priced at a traded quote. Every expected
value below is arithmetic on these constants and nothing else — no test here
touches the network, because a quote has to be reproducible from a stored
snapshot alone.
"""

from __future__ import annotations

import math

import pytest

from open_allocator.core import levered

# --- the snapshot ----------------------------------------------------------
A_SUPPLY_BASE = 2.055
A_BORROW_BASE = 7.134
B_SUPPLY_BASE = 3.244
B_BORROW_BASE = 8.002

# Reward APRs at the reward token's traded price.
REWARD_PRICE_USD = 0.03627
A_SUPPLY_REWARD = 4.497
A_BORROW_REWARD = 2.372
B_SUPPLY_REWARD = 0.995
B_BORROW_REWARD = 5.201
REWARD_VOLUME_USD = 635.0

# The collateral reserve's own parameters, and the higher pair of an
# efficiency-mode category the same pair can be read under instead.
RESERVE_LTV = 0.85
RESERVE_LIQUIDATION_THRESHOLD = 0.90
EMODE_LTV = 0.93
EMODE_LIQUIDATION_THRESHOLD = 0.94


def _spec(
    *,
    collateral: str = "A",
    debt: str = "A",
    ltv: float = RESERVE_LTV,
    liquidation_threshold: float = RESERVE_LIQUIDATION_THRESHOLD,
    params_basis: str = "default",
    reward_price_basis: str | None = "traded",
    reward_liquidity_usd: float | None = REWARD_VOLUME_USD,
) -> levered.LeveredSpec:
    supply_base = A_SUPPLY_BASE if collateral == "A" else B_SUPPLY_BASE
    supply_reward = A_SUPPLY_REWARD if collateral == "A" else B_SUPPLY_REWARD
    borrow_base = A_BORROW_BASE if debt == "A" else B_BORROW_BASE
    borrow_reward = A_BORROW_REWARD if debt == "A" else B_BORROW_REWARD
    return levered.LeveredSpec(
        loop_id=f"loop:{collateral}/{debt}",
        chain_id=1,
        protocol="lender",
        collateral_instrument_id=f"lender-{collateral.lower()}",
        debt_instrument_id=f"lender-{debt.lower()}",
        collateral_asset=collateral,
        debt_asset=debt,
        supply_apy_base_pct=supply_base,
        supply_apy_reward_pct=supply_reward,
        borrow_apy_base_pct=borrow_base,
        borrow_apy_reward_pct=borrow_reward,
        ltv=ltv,
        liquidation_threshold=liquidation_threshold,
        params_basis=params_basis,
        oracle_source="pool-oracle",
        reward_price_basis=reward_price_basis,  # type: ignore[arg-type]
        reward_price_usd=REWARD_PRICE_USD,
        reward_liquidity_usd=reward_liquidity_usd,
    )


def _emode_spec(**kwargs: object) -> levered.LeveredSpec:
    """The same pair under the efficiency-mode parameters, which reach L=8.

    The reserve's own 0.85 LTV tops out at 6.67x.
    """
    return _spec(
        ltv=EMODE_LTV,
        liquidation_threshold=EMODE_LIQUIDATION_THRESHOLD,
        params_basis="emode:2",
        **kwargs,  # type: ignore[arg-type]
    )


# --- the per-turn gradient, which is the whole question -------------------


@pytest.mark.parametrize(
    ("collateral", "debt", "supply", "borrow_cost", "gradient"),
    [
        ("A", "A", 6.55, 4.76, 1.79),
        ("A", "B", 6.55, 2.80, 3.75),
        ("B", "A", 4.24, 4.76, -0.52),
    ],
)
def test_gradient_is_supply_less_net_borrow_cost(
    collateral: str,
    debt: str,
    supply: float,
    borrow_cost: float,
    gradient: float,
) -> None:
    quoted = levered.quote(_spec(collateral=collateral, debt=debt), turns=1)

    assert round(quoted.supply_apy_pct, 2) == supply
    assert round(quoted.borrow_cost_pct, 2) == borrow_cost
    assert round(quoted.gradient_pct, 2) == gradient


def test_the_borrow_side_reward_is_the_whole_cross_asset_edge() -> None:
    """B's debt pays borrowers more than twice what A's does.

    That asymmetry is invisible to anything reading only supply-side APY — both
    loops below supply the same asset at the same rate.
    """
    same_asset = levered.quote(_spec(debt="A"), turns=1)
    cross_asset = levered.quote(_spec(debt="B"), turns=1)

    assert same_asset.supply_apy_pct == cross_asset.supply_apy_pct
    assert cross_asset.gradient_pct > 2 * same_asset.gradient_pct


# --- looped figures, at traded reward prices ------------------------------


def test_five_turns_at_reserve_ltv() -> None:
    quoted = levered.quote(_spec(), turns=5)

    assert round(quoted.leverage, 6) == 4.152337
    assert round(quoted.levered_apy_pct, 2) == 12.19
    assert round(quoted.unlevered_apy_pct, 2) == 6.55


def test_cross_asset_at_l8_in_emode() -> None:
    quoted = levered.quote(_emode_spec(debt="B"), leverage=8.0)

    assert round(quoted.levered_apy_pct, 2) == 32.81
    assert round(quoted.gradient_pct, 2) == 3.75


def test_the_unlevered_leg_is_f_of_one() -> None:
    quoted = levered.quote(_spec(collateral="B", debt="A"), turns=0)

    assert quoted.leverage == 1.0
    assert round(quoted.levered_apy_pct, 2) == 4.24
    assert quoted.levered_apy_pct == quoted.supply_apy_pct
    assert quoted.debt_usd == 0.0
    assert quoted.health_factor is None
    assert quoted.depeg_buffer_bps is None


# --- linearity, which the module's whole argument rests on -----------------


def test_f_of_l_is_linear_in_l() -> None:
    spec = _spec(debt="B")
    quotes = [levered.quote(spec, leverage=leverage) for leverage in (1.0, 2.0, 4.0)]
    gradient = quotes[0].gradient_pct

    for earlier, later in zip(quotes, quotes[1:], strict=False):
        step = later.leverage - earlier.leverage
        assert later.levered_apy_pct - earlier.levered_apy_pct == pytest.approx(
            step * gradient
        )


def test_a_negative_gradient_loop_is_negative_at_every_leverage() -> None:
    """B collateral against A debt: leverage cannot rescue it."""
    spec = _spec(collateral="B", debt="A")
    unlevered = levered.quote(spec, turns=0)

    assert unlevered.gradient_pct < 0
    previous = unlevered.levered_apy_pct
    for leverage in (2.0, 4.0, 6.0):
        quoted = levered.quote(spec, leverage=leverage)
        assert quoted.levered_apy_pct < previous
        assert any(
            warning.startswith("levered_gradient_nonpositive")
            for warning in quoted.warnings()
        )
        previous = quoted.levered_apy_pct


# --- the reward basis decides whether there is a trade at all --------------


def test_with_rewards_excluded_the_same_pair_is_a_value_destroying_loop() -> None:
    """Base rates alone borrow at a loss; the edge is the reward."""
    spec = _spec(debt="B")

    with_rewards = levered.quote(spec, turns=5, count_rewards=True)
    without = levered.quote(spec, turns=5, count_rewards=False)

    assert with_rewards.gradient_pct > 0
    assert without.gradient_pct == pytest.approx(
        A_SUPPLY_BASE - B_BORROW_BASE, abs=1e-9
    )
    assert without.gradient_pct < 0
    assert without.levered_apy_pct < without.unlevered_apy_pct
    assert without.apy_basis == "base"


def test_an_emission_priced_snapshot_counts_no_rewards_by_default() -> None:
    quoted = levered.quote(_spec(reward_price_basis="emission"), turns=5)

    assert quoted.rewards_counted is False
    assert quoted.apy_basis == "base"
    assert quoted.supply_apy_pct == A_SUPPLY_BASE
    assert quoted.borrow_cost_pct == A_BORROW_BASE
    assert any(
        warning.startswith("levered_reward_unpriced") for warning in quoted.warnings()
    )


def test_an_absent_basis_is_not_traded() -> None:
    assert levered.quote(_spec(reward_price_basis=None), turns=5).rewards_counted is (
        False
    )


# --- health factor and the depeg budget ------------------------------------


def test_health_factor_and_depeg_buffer_at_l8_in_emode() -> None:
    quoted = levered.quote(_emode_spec(debt="B"), leverage=8.0)

    assert quoted.health_factor is not None
    assert round(quoted.health_factor, 3) == 1.074
    assert quoted.depeg_buffer_bps == 743
    assert quoted.params_basis == "emode:2"


def test_a_same_asset_loop_publishes_no_depeg_budget() -> None:
    """There is a health factor, but no price ratio to budget for."""
    quoted = levered.quote(_spec(debt="A"), leverage=4.0)

    assert quoted.health_factor is not None
    assert quoted.depeg_buffer_bps is None
    assert quoted.swap_notional_usd == 0.0
    assert not any(
        warning.startswith("levered_depeg_budget") for warning in quoted.warnings()
    )


def test_a_cross_asset_loop_swaps_the_debt_not_the_equity() -> None:
    quoted = levered.quote(_emode_spec(debt="B"), leverage=8.0, equity_usd=105.0)

    assert quoted.collateral_usd == pytest.approx(840.0)
    assert quoted.debt_usd == pytest.approx(735.0)
    assert quoted.swap_notional_usd == pytest.approx(735.0)
    assert any(
        warning.startswith("levered_depeg_budget") for warning in quoted.warnings()
    )


def test_liquidation_happens_at_the_health_factor() -> None:
    """HF_0 is exactly the adverse debt/collateral move that liquidates."""
    leverage = 8.0
    hf = levered.health_factor(
        leverage=leverage,
        liquidation_threshold=EMODE_LIQUIDATION_THRESHOLD,
    )
    assert hf is not None

    at_liquidation = levered.health_factor(
        leverage=leverage,
        liquidation_threshold=EMODE_LIQUIDATION_THRESHOLD,
        debt_price_ratio=hf,
    )
    assert at_liquidation == pytest.approx(1.0)


def test_a_health_factor_floor_inverts_to_a_leverage_ceiling() -> None:
    ceiling = levered.leverage_for_health_factor(
        target_health_factor=1.0742857142857143,
        liquidation_threshold=EMODE_LIQUIDATION_THRESHOLD,
    )

    assert ceiling == pytest.approx(8.0)


def test_a_floor_at_or_below_the_liquidation_threshold_does_not_bind() -> None:
    with pytest.raises(ValueError, match="does not bind"):
        levered.leverage_for_health_factor(
            target_health_factor=0.9,
            liquidation_threshold=EMODE_LIQUIDATION_THRESHOLD,
        )


# --- leverage, turns and the ltv ceiling -----------------------------------


def test_max_leverage_is_the_ltv_ceiling() -> None:
    assert levered.max_leverage(RESERVE_LTV) == pytest.approx(6.666667)
    assert levered.max_leverage(EMODE_LTV) == pytest.approx(14.285714)


def test_leverage_and_turns_are_inverses() -> None:
    for turns in range(0, 12):
        leverage = levered.leverage_for_turns(RESERVE_LTV, turns)
        assert levered.turns_for_leverage(RESERVE_LTV, leverage) == turns


def test_leverage_at_the_ltv_ceiling_is_rejected_not_clamped() -> None:
    with pytest.raises(ValueError, match="not reachable"):
        levered.quote(_spec(), leverage=levered.max_leverage(RESERVE_LTV))

    with pytest.raises(ValueError, match="not reachable"):
        levered.quote(_spec(), leverage=9.0)


def test_a_quote_takes_exactly_one_of_leverage_and_turns() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        levered.quote(_spec())
    with pytest.raises(ValueError, match="exactly one"):
        levered.quote(_spec(), leverage=2.0, turns=1)


def test_a_threshold_below_the_ltv_is_rejected() -> None:
    with pytest.raises(ValueError, match="below ltv"):
        _spec(ltv=0.90, liquidation_threshold=0.85)


# --- the kill switch is a price, not a judgement ---------------------------


def test_the_kill_switch_is_where_the_gradient_crosses_zero() -> None:
    quoted = levered.quote(_spec(debt="B"), turns=5)

    assert quoted.kill_switch_reward_price_ratio is not None
    assert round(quoted.kill_switch_reward_price_usd or 0.0, 4) == 0.0222
    # A 39% drawdown buffer against the snapshot price.
    assert round(1 - quoted.kill_switch_reward_price_ratio, 2) == 0.39


def test_at_the_kill_switch_price_the_gradient_is_zero() -> None:
    spec = _spec(debt="B")
    ratio = levered.zero_gradient_reward_ratio(spec)
    assert ratio is not None

    repriced = spec.model_copy(
        update={
            "supply_apy_reward_pct": spec.supply_apy_reward_pct * ratio,
            "borrow_apy_reward_pct": spec.borrow_apy_reward_pct * ratio,
        }
    )
    assert levered.quote(repriced, turns=5).gradient_pct == pytest.approx(0.0, abs=1e-9)


def test_a_loop_that_carries_without_rewards_has_no_kill_switch_price() -> None:
    spec = _spec(debt="B").model_copy(update={"borrow_apy_base_pct": 1.0})

    assert levered.zero_gradient_reward_ratio(spec) == 0.0


def test_a_pair_with_no_rewards_has_no_price_to_trigger_on() -> None:
    spec = _spec().model_copy(
        update={"supply_apy_reward_pct": 0.0, "borrow_apy_reward_pct": 0.0}
    )

    assert levered.zero_gradient_reward_ratio(spec) is None


# --- capacity binds on the reward token's exit, not the pool's cash --------


def test_equity_is_capped_by_what_the_reward_token_can_absorb() -> None:
    quoted = levered.quote(_emode_spec(debt="B"), leverage=8.0, equity_usd=1_000.0)

    assert quoted.reward_income_usd_per_day is not None
    cap = levered.equity_cap_for_reward_liquidity(quoted, max_share_of_volume=0.05)

    assert cap is not None
    # 5% of the reward token's daily volume sellable, against reward income
    # that scales with equity.
    assert round(cap) == 16_010


def test_unmeasured_reward_liquidity_caps_nothing_and_says_so() -> None:
    quoted = levered.quote(
        _emode_spec(debt="B", reward_liquidity_usd=None),
        leverage=8.0,
    )

    assert levered.equity_cap_for_reward_liquidity(
        quoted, max_share_of_volume=0.05
    ) is (None)
    assert any(
        warning.startswith("levered_reward_liquidity_unknown")
        for warning in quoted.warnings()
    )


def test_reward_income_scales_with_equity() -> None:
    small = levered.quote(_emode_spec(debt="B"), leverage=8.0, equity_usd=100.0)
    large = levered.quote(_emode_spec(debt="B"), leverage=8.0, equity_usd=1_000.0)

    assert small.reward_income_usd_per_day is not None
    assert large.reward_income_usd_per_day is not None
    assert large.reward_income_usd_per_day == pytest.approx(
        10 * small.reward_income_usd_per_day
    )


def test_no_rewards_counted_means_no_reward_income_reported() -> None:
    quoted = levered.quote(_emode_spec(debt="B"), leverage=8.0, count_rewards=False)

    assert quoted.reward_income_usd_per_day is None
    assert quoted.kill_switch_reward_price_usd is None
    assert levered.equity_cap_for_reward_liquidity(
        quoted, max_share_of_volume=0.05
    ) is (None)


# --- the quote is an artifact, so it has to serialise ----------------------


def test_a_quote_round_trips_as_json() -> None:
    quoted = levered.quote(_emode_spec(debt="B"), leverage=8.0, equity_usd=105.0)
    payload = quoted.model_dump(mode="json")

    assert all(
        not isinstance(value, float) or math.isfinite(value)
        for value in payload.values()
    )
    assert levered.LeveredQuote.model_validate(payload) == quoted
