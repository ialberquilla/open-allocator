import pytest

from open_allocator.core import costs


def _legs() -> list[costs.LegInput]:
    # Source assumed = Base (8453): most USD, so only the Unichain leg bridges.
    return [
        costs.LegInput("base-a", 8453, 40.0, 4.0),
        costs.LegInput("base-b", 8453, 30.0, 4.0),
        costs.LegInput("uni", 130, 30.0, 16.0),
    ]


def test_default_source_chain_is_largest_usd_share() -> None:
    assert costs.default_source_chain_id(_legs()) == 8453


def test_estimate_prices_gas_bridge_and_verdict() -> None:
    est = costs.estimate(_legs())
    assert est is not None
    # 3 legs * 2 txs, at whatever the L2 fallback price is. Pinned to the
    # constant rather than a literal: the fallback is re-calibrated when live
    # measurements move, and this test is about the arithmetic, not the price.
    assert est.gas_cost_usd == round(3 * 2 * costs.DEFAULT_L2_GAS_USD_PER_TX, 4)
    # No live pricing was supplied, so the estimate must say so.
    assert est.gas_priced_live is False
    # Only the $30 Unichain leg bridges, at 1bp.
    assert est.bridged_usd == 30.0
    assert est.bridged_leg_count == 1
    assert est.bridge_fee_usd == round(30.0 * 1.0 / 10_000, 4)
    # Blended gross APY is USD-weighted: 0.4*4 + 0.3*4 + 0.3*16.
    assert est.gross_blended_apy_pct == 7.6
    assert est.net_apy_pct_year1 < est.gross_blended_apy_pct
    assert est.verdict == "ok"  # ~0.2% drag on $100


def test_small_deploy_flagged_uneconomic() -> None:
    tiny = [
        costs.LegInput("a", 8453, 1.0, 4.0),
        costs.LegInput("b", 130, 1.0, 16.0),
    ]
    # Gas is pinned explicitly: the property under test is "fixed cost dwarfs a
    # tiny deploy", which must not depend on how cheap L2 gas happens to be.
    params = costs.CostParams(l2_gas_usd_per_tx=0.03)
    est = costs.estimate(tiny, params=params)
    assert est is not None
    # $0.12 fixed gas on a $2 deploy -> ~6% drag, uneconomic, with a warning.
    assert est.verdict == "uneconomic"
    assert est.warning() is not None
    assert est.warning().startswith("viability:uneconomic")


def test_live_gas_pricing_overrides_the_static_fallback() -> None:
    # 190k gas at 1 gwei with ETH at $2,000 = $0.38/tx, far above the L2 default.
    pricing = costs.GasPricing(gas_price_wei={8453: 10**9}, native_usd={8453: 2000.0})
    params = costs.CostParams(gas=pricing)
    est = costs.estimate(_legs(), source_chain_id=8453, params=params)
    assert est is not None
    expected_per_tx = costs.DEFAULT_GAS_UNITS_PER_TX * 1e-9 * 2000.0
    assert est.gas_cost_usd == round(3 * 2 * expected_per_tx, 4)
    assert est.gas_priced_live is True


def test_live_pricing_falls_back_per_chain_when_a_chain_is_missing() -> None:
    # Base priced live, Unichain absent -> Unichain must use the static constant,
    # not silently inherit Base's price.
    pricing = costs.GasPricing(gas_price_wei={8453: 10**9}, native_usd={8453: 2000.0})
    params = costs.CostParams(gas=pricing)
    assert params.gas_priced_live(8453) is True
    assert params.gas_priced_live(130) is False
    assert params.gas_usd_per_tx(130) == costs.DEFAULT_L2_GAS_USD_PER_TX


def test_live_pricing_ignored_when_the_token_quote_is_unusable() -> None:
    pricing = costs.GasPricing(gas_price_wei={8453: 10**9}, native_usd={8453: 0.0})
    params = costs.CostParams(gas=pricing)
    assert params.gas_priced_live(8453) is False
    assert params.gas_usd_per_tx(8453) == costs.DEFAULT_L2_GAS_USD_PER_TX


def test_min_economic_leg_scales_with_gas() -> None:
    cheap = costs.CostParams(l2_gas_usd_per_tx=0.006)
    dear = costs.CostParams(l1_gas_usd_per_tx=10.0)
    # 2 txs * $0.006 / 0.1% = $12 floor on an L2.
    assert costs.min_economic_leg_usd(8453, params=cheap) == 12.0
    # 2 txs * $10 / 0.1% = $20,000 floor on mainnet at a punishing base fee.
    assert costs.min_economic_leg_usd(1, params=dear) == 20_000.0
    assert costs.min_economic_leg_usd(1, params=dear) > costs.min_economic_leg_usd(
        8453, params=cheap
    )


def test_mainnet_source_is_pricier() -> None:
    l2 = costs.estimate(_legs(), source_chain_id=8453)
    l1 = costs.estimate(_legs(), source_chain_id=1)
    assert l1 is not None and l2 is not None
    assert l1.gas_cost_usd > l2.gas_cost_usd
    # Every non-mainnet leg now bridges from mainnet.
    assert l1.bridged_leg_count == 3


def test_non_positive_yield_never_breaks_even() -> None:
    est = costs.estimate([costs.LegInput("z", 8453, 100.0, 0.0)])
    assert est is not None
    assert est.breakeven_days is None
    assert "breakeven_days" not in est.as_metadata()


def test_empty_allocation_returns_none() -> None:
    assert costs.estimate([]) is None
    assert costs.estimate([costs.LegInput("a", 8453, 0.0, 4.0)]) is None


def test_from_allocation_legs_skips_unknown_chain() -> None:
    est = costs.estimate_from_allocation_legs(
        [
            {"instrument_id": "known", "usd": 100.0},
            {"instrument_id": "ghost", "usd": 5.0},
        ],
        chain_by_instrument={"known": 8453},
        apy_by_instrument={"known": 4.0},
    )
    assert est is not None
    assert est.leg_count == 1
    assert est.deploy_usd == 100.0


def test_incomplete_base_coverage_hides_income_and_breakeven() -> None:
    est = costs.estimate_from_allocation_legs(
        [
            {"instrument_id": "known", "usd": 60.0},
            {"instrument_id": "unknown", "usd": 40.0},
        ],
        chain_by_instrument={"known": 8453, "unknown": 8453},
        apy_by_instrument={"known": 5.0, "unknown": 8.0},
        base_apy_by_instrument={"known": 4.0, "unknown": None},
    )

    assert est is not None
    assert est.gross_blended_apy_pct == 6.2
    assert est.measured_base_apy_pct == 4.0
    assert est.base_apy_coverage_bps == 6000
    assert est.accruing_income_available is False
    assert est.net_apy_pct_year1 is None
    assert est.breakeven_days is None


def test_a_chain_is_priced_in_its_own_token_not_in_eth() -> None:
    """A cheap token at a high gas price is not an expensive chain.

    The same gas price converted at ETH's price rather than the chain's own is
    not a mis-calibration to be tuned away — it is a different token, and the
    error is the whole ratio between the two.
    """
    gwei_108 = 108_733_597_232
    ethish = costs.GasPricing(gas_price_wei={143: gwei_108}, native_usd={143: 2683.0})
    honest = costs.GasPricing(gas_price_wei={143: gwei_108}, native_usd={143: 0.0255})

    wrong = ethish.usd_per_tx(143)
    right = honest.usd_per_tx(143)
    assert wrong is not None and right is not None
    assert wrong > 50.0
    assert right < 0.01
    assert wrong / right > 100_000


def test_a_chain_with_no_token_quote_is_unpriced_rather_than_guessed() -> None:
    """Absent from native_usd means "no quote", never "same as the others"."""
    pricing = costs.GasPricing(
        gas_price_wei={8453: 10**9, 143: 10**11}, native_usd={8453: 2000.0}
    )
    params = costs.CostParams(gas=pricing)
    assert params.gas_priced_live(8453) is True
    assert params.gas_priced_live(143) is False
    assert params.gas_usd_per_tx(143) == costs.DEFAULT_L2_GAS_USD_PER_TX


# ─────────────────────────────────────────────────────────────────────────────
# estimate_rebalance


def _book() -> list[costs.MoveInput]:
    """A two-leg book worth $100, moving $10 from a 4% leg into an 8% one."""
    return [
        costs.MoveInput("keep", 8453, current_usd=50.0, target_usd=40.0, apy_pct=4.0),
        costs.MoveInput("grow", 8453, current_usd=50.0, target_usd=60.0, apy_pct=8.0),
    ]


def test_rebalance_prices_only_the_legs_that_move() -> None:
    est = costs.estimate_rebalance(_book())
    assert est is not None
    assert est.moved_leg_count == 2
    assert est.buy_usd == 10.0
    assert est.sell_usd == 10.0
    assert est.turnover_usd == 20.0
    # One buy at txs_per_leg=2, one sell at txs_per_exit=1. NOT 2 per leg: a
    # rebalance pays both directions and they are not the same number.
    assert est.tx_count == 3


def test_rebalance_ignores_untouched_legs_in_cost_but_not_in_yield() -> None:
    moves = [*_book(), costs.MoveInput("still", 8453, 100.0, 100.0, 6.0)]
    est = costs.estimate_rebalance(moves)
    assert est is not None
    assert est.moved_leg_count == 2  # the unchanged leg costs nothing
    # ...but it is in the denominator: a $200 book improving by the same $0.40
    # is a smaller rate change than a $100 one.
    smaller = costs.estimate_rebalance(_book())
    assert smaller is not None
    assert est.annual_gain_usd == smaller.annual_gain_usd
    assert est.apy_delta_pct < smaller.apy_delta_pct


def test_compliance_only_rebalance_has_no_payback() -> None:
    """🔑 The case `estimate` gets wrong. Same yields, different weights."""
    moves = [
        costs.MoveInput("a", 8453, current_usd=60.0, target_usd=50.0, apy_pct=5.0),
        costs.MoveInput("b", 8453, current_usd=40.0, target_usd=50.0, apy_pct=5.0),
    ]
    est = costs.estimate_rebalance(moves)
    assert est is not None
    assert est.annual_gain_usd == 0.0
    assert est.payback_days is None
    assert est.verdict == "no_yield_gain"
    # And the flat dict must not turn "never repays" into a number.
    assert "payback_days" not in est.as_metadata()


def test_rebalance_that_lowers_yield_still_reports_no_payback() -> None:
    moves = [
        costs.MoveInput("rich", 8453, current_usd=50.0, target_usd=20.0, apy_pct=9.0),
        costs.MoveInput("poor", 8453, current_usd=50.0, target_usd=80.0, apy_pct=3.0),
    ]
    est = costs.estimate_rebalance(moves)
    assert est is not None
    assert est.annual_gain_usd < 0
    assert est.payback_days is None
    assert est.verdict == "no_yield_gain"


def test_deploying_idle_counts_as_gain_even_though_the_rate_barely_moves() -> None:
    """Buys with no sells: the book gets bigger, not better."""
    moves = [
        costs.MoveInput("a", 8453, current_usd=50.0, target_usd=55.0, apy_pct=6.0),
        costs.MoveInput("b", 8453, current_usd=50.0, target_usd=55.0, apy_pct=6.0),
    ]
    est = costs.estimate_rebalance(moves, idle_usd_by_chain={8453: 10.0})
    assert est is not None
    assert est.sell_usd == 0.0
    assert est.buy_usd == 10.0
    # The blended RATE is unchanged — both legs pay 6% — and the book still
    # earns $0.60/yr more. Only the dollar figure sees it.
    assert est.apy_delta_pct == 0.0
    assert est.annual_gain_usd == pytest.approx(0.6)
    assert est.payback_days is not None
    assert est.verdict == "ok"


def test_min_trade_usd_skips_are_reported_not_hidden() -> None:
    moves = [
        costs.MoveInput("big", 8453, current_usd=50.0, target_usd=60.0, apy_pct=8.0),
        costs.MoveInput("dust", 8453, current_usd=50.0, target_usd=49.5, apy_pct=4.0),
    ]
    est = costs.estimate_rebalance(moves, min_trade_usd=1.0)
    assert est is not None
    assert est.moved_leg_count == 1
    assert est.skipped_leg_count == 1
    assert est.skipped_usd == 0.5


def test_target_yield_is_the_post_threshold_book() -> None:
    """A plan whose whole improvement is sub-threshold improves nothing."""
    moves = [
        costs.MoveInput("a", 8453, current_usd=50.0, target_usd=49.5, apy_pct=4.0),
        costs.MoveInput("b", 8453, current_usd=50.0, target_usd=50.5, apy_pct=8.0),
    ]
    unthrottled = costs.estimate_rebalance(moves)
    throttled = costs.estimate_rebalance(moves, min_trade_usd=1.0)
    assert unthrottled is not None and throttled is not None
    assert unthrottled.annual_gain_usd > 0
    # The executor will decline both trades, so the book does not move and
    # neither does its yield. Reporting the target's gain here would be a
    # number for a book nobody will hold.
    assert throttled.moved_leg_count == 0
    assert throttled.annual_gain_usd == 0.0
    assert throttled.verdict == "nothing_to_do"
    assert throttled.warning() is None


def test_a_chain_that_cannot_fund_its_own_buys_bridges() -> None:
    moves = [
        costs.MoveInput("base", 8453, current_usd=50.0, target_usd=40.0, apy_pct=4.0),
        costs.MoveInput("monad", 143, current_usd=50.0, target_usd=60.0, apy_pct=8.0),
    ]
    est = costs.estimate_rebalance(moves)
    assert est is not None
    assert est.net_flow_by_chain == {143: 10.0, 8453: -10.0}
    # Monad needs $10 it does not have; Base frees exactly $10. That crossing is
    # a bridge whether or not the plan called it one.
    assert est.bridged_usd == 10.0
    assert est.unfundable_usd == 0.0
    assert est.bridge_fee_usd > 0


def test_idle_on_the_chain_removes_the_bridge() -> None:
    moves = [
        costs.MoveInput("monad", 143, current_usd=50.0, target_usd=60.0, apy_pct=8.0),
    ]
    est = costs.estimate_rebalance(moves, idle_usd_by_chain={143: 25.0})
    assert est is not None
    assert est.bridged_usd == 0.0
    assert est.bridge_fee_usd == 0.0


def test_unfundable_is_a_different_failure_from_an_expensive_one() -> None:
    """🔴 No chain can cover the shortfall: the plan cannot run as written."""
    moves = [
        costs.MoveInput("monad", 143, current_usd=50.0, target_usd=90.0, apy_pct=8.0),
    ]
    est = costs.estimate_rebalance(moves, idle_usd_by_chain={143: 1.0})
    assert est is not None
    assert est.unfundable_usd == pytest.approx(39.0)
    assert est.verdict == "unfundable"
    assert est.warning() == "rebalance:unfundable"


def test_payback_thresholds_agree_with_the_drift_gate_horizon() -> None:
    from open_allocator.core import drift

    assert costs.CostParams().uneconomic_payback_days == drift._PAYBACK_HORIZON_DAYS


def test_rebalance_from_holdings_treats_an_exit_as_a_move() -> None:
    est = costs.estimate_rebalance_from_holdings(
        {"gone": 30.0, "stay": 70.0},
        {"stay": 100.0},
        chain_by_instrument={"gone": 8453, "stay": 8453},
        apy_by_instrument={"gone": 3.0, "stay": 7.0},
    )
    assert est is not None
    assert est.moved_leg_count == 2
    assert est.sell_usd == 30.0
    assert est.buy_usd == 30.0
    assert est.target_blended_apy_pct == pytest.approx(7.0)


def test_rebalance_with_unknown_base_rate_does_not_publish_mixed_payback() -> None:
    est = costs.estimate_rebalance_from_holdings(
        {"known": 50.0, "unknown": 50.0},
        {"known": 60.0, "unknown": 40.0},
        chain_by_instrument={"known": 8453, "unknown": 8453},
        apy_by_instrument={"known": 5.0, "unknown": 9.0},
        base_apy_by_instrument={"known": 4.0, "unknown": None},
    )

    assert est is not None
    assert est.advertised_current_blended_apy_pct == 7.0
    assert est.current_base_apy_coverage_bps == 5000
    assert est.target_base_apy_coverage_bps == 6000
    assert est.current_blended_apy_pct is None
    assert est.target_blended_apy_pct is None
    assert est.annual_gain_usd is None
    assert est.payback_days is None
    assert est.apy_basis == "mixed_unknown"
    assert est.verdict == "apy_unavailable"


def test_rebalance_returns_none_on_an_empty_book() -> None:
    assert costs.estimate_rebalance([]) is None


# ─────────────────────────────────────────────────────────────────────────────
# Spread is charged, and a leg's tx count is the leg's own


def test_spread_is_charged_not_merely_reported() -> None:
    """🔑 A tolerance and an expectation, and only one of them is a cost.

    ``max_slippage_usd`` stays out of the net figure; ``spread_cost_usd`` is
    inside the total, so no cost, verdict or breakeven is gas alone.
    """
    est = costs.estimate(_legs())
    assert est is not None
    assert est.spread_cost_usd == round(
        100.0 * costs.DEFAULT_EXPECTED_SPREAD_BPS / 10_000, 4
    )
    assert est.total_expected_cost_usd == round(
        est.gas_cost_usd + est.bridge_fee_usd + est.spread_cost_usd, 4
    )
    # The tolerance is still published, still larger, and still not charged.
    assert est.max_slippage_usd > est.spread_cost_usd
    assert est.max_slippage_usd not in (
        est.total_expected_cost_usd,
        est.total_expected_cost_usd - est.gas_cost_usd,
    )
    assert est.as_metadata()["spread_cost_usd"] == est.spread_cost_usd


def test_charging_spread_lengthens_every_breakeven() -> None:
    """Spread only ever makes a trade take longer to repay."""
    priced = costs.estimate(_legs())
    gas_only = costs.estimate(_legs(), params=costs.CostParams(expected_spread_bps=0.0))
    assert priced is not None and gas_only is not None
    assert priced.total_expected_cost_usd > gas_only.total_expected_cost_usd
    assert priced.breakeven_days > gas_only.breakeven_days
    assert priced.net_apy_pct_year1 < gas_only.net_apy_pct_year1


def test_a_leg_that_needs_no_swap_pays_gas_but_not_spread() -> None:
    no_swap = [costs.LegInput("usdc-vault", 8453, 100.0, 4.0, swaps=False)]
    est = costs.estimate(no_swap)
    assert est is not None
    assert est.spread_cost_usd == 0.0
    assert est.gas_cost_usd > 0.0


def test_a_batched_leg_signs_once_not_twice() -> None:
    """A loop, a multicall or a 4337 bundle is ONE user-op, not 2n.

    Charging ``txs_per_leg`` per inner call prices a transaction shape this
    executor does not submit, and the error grows with the number of turns.
    """
    plain = costs.estimate([costs.LegInput("a", 8453, 100.0, 4.0)])
    bundled = costs.estimate([costs.LegInput("a", 8453, 100.0, 4.0, txs=1)])
    assert plain is not None and bundled is not None
    assert plain.gas_cost_usd == round(2 * costs.DEFAULT_L2_GAS_USD_PER_TX, 4)
    assert bundled.gas_cost_usd == round(costs.DEFAULT_L2_GAS_USD_PER_TX, 4)
    # ...and the swap it still performs is still charged.
    assert bundled.spread_cost_usd == plain.spread_cost_usd


def test_a_declared_zero_tx_leg_is_honoured_and_a_negative_one_is_not() -> None:
    params = costs.CostParams()
    assert params.txs_for(0, default=2) == 0
    assert params.txs_for(None, default=2) == 2
    # A negative count would credit the book with gas it never earned.
    assert params.txs_for(-5, default=2) == 0


def test_per_instrument_tx_counts_travel_through_the_allocation_helper() -> None:
    est = costs.estimate_from_allocation_legs(
        [
            {"instrument_id": "bundled", "usd": 50.0},
            {"instrument_id": "plain", "usd": 50.0},
        ],
        chain_by_instrument={"bundled": 8453, "plain": 8453},
        apy_by_instrument={"bundled": 4.0, "plain": 4.0},
        txs_by_instrument={"bundled": 1},
    )
    assert est is not None
    assert est.gas_cost_usd == round(3 * costs.DEFAULT_L2_GAS_USD_PER_TX, 4)


def test_rebalance_charges_spread_on_turnover() -> None:
    est = costs.estimate_rebalance(_book())
    assert est is not None
    # $10 sold and $10 bought: both crossings pay.
    assert est.spread_cost_usd == round(
        20.0 * costs.DEFAULT_EXPECTED_SPREAD_BPS / 10_000, 4
    )
    assert est.total_expected_cost_usd == round(
        est.gas_cost_usd + est.bridge_fee_usd + est.spread_cost_usd, 4
    )
    gas_only = costs.estimate_rebalance(
        _book(), params=costs.CostParams(expected_spread_bps=0.0)
    )
    assert gas_only is not None
    assert est.payback_days > gas_only.payback_days


def test_rebalance_honours_a_per_move_tx_count() -> None:
    moves = [
        costs.MoveInput("keep", 8453, 50.0, 40.0, 4.0, txs=0),
        costs.MoveInput("grow", 8453, 50.0, 60.0, 8.0, txs=1),
    ]
    est = costs.estimate_rebalance(moves)
    assert est is not None
    # One batched operation covering both legs, rather than 2 + 1.
    assert est.tx_count == 1


def test_drift_and_rebalance_price_the_same_switch_the_same_way() -> None:
    """The two modules must not disagree about what a round trip costs.

    ``core.drift`` pins its gas convention to ``CostParams``; spread travels the
    same way, or the gate fires switches this module then calls uneconomic.
    """
    from open_allocator.core import drift

    params = costs.CostParams()
    position_usd = 1_000.0
    est = costs.estimate_rebalance(
        [
            costs.MoveInput("out", 8453, position_usd, 0.0, 5.0),
            costs.MoveInput("in", 8453, 0.0, position_usd, 9.0),
        ],
        params=params,
    )
    assert est is not None
    assert est.spread_cost_usd == round(
        params.spread_usd(drift._SWITCH_CROSSINGS * position_usd), 4
    )
