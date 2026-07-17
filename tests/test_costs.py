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
    # 3 legs * 2 txs * $0.03 L2 gas.
    assert est.gas_cost_usd == round(3 * 2 * 0.03, 4)
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
    est = costs.estimate(tiny)
    assert est is not None
    # Fixed gas ($0.12) dwarfs a $2 deploy -> ~6% drag, uneconomic, with a warning.
    assert est.verdict == "uneconomic"
    assert est.warning() is not None
    assert est.warning().startswith("viability:uneconomic")


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
