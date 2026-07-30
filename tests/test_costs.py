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
    pricing = costs.GasPricing(gas_price_wei={8453: 10**9}, eth_usd=2000.0)
    params = costs.CostParams(gas=pricing)
    est = costs.estimate(_legs(), source_chain_id=8453, params=params)
    assert est is not None
    expected_per_tx = costs.DEFAULT_GAS_UNITS_PER_TX * 1e-9 * 2000.0
    assert est.gas_cost_usd == round(3 * 2 * expected_per_tx, 4)
    assert est.gas_priced_live is True


def test_live_pricing_falls_back_per_chain_when_a_chain_is_missing() -> None:
    # Base priced live, Unichain absent -> Unichain must use the static constant,
    # not silently inherit Base's price.
    pricing = costs.GasPricing(gas_price_wei={8453: 10**9}, eth_usd=2000.0)
    params = costs.CostParams(gas=pricing)
    assert params.gas_priced_live(8453) is True
    assert params.gas_priced_live(130) is False
    assert params.gas_usd_per_tx(130) == costs.DEFAULT_L2_GAS_USD_PER_TX


def test_live_pricing_ignored_when_eth_quote_is_unusable() -> None:
    pricing = costs.GasPricing(gas_price_wei={8453: 10**9}, eth_usd=0.0)
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
