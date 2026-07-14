from __future__ import annotations

from open_allocator.core.screen import ScreenCriteria, screen
from open_allocator.core.types import Unknown, Vault


def vault(instrument_id: str = "vault-a", **updates: object) -> Vault:
    base = Vault(
        instrument_id=instrument_id,
        protocol="protocol-a",
        chain_id=1,
        asset="USDC",
        apy=5.0,
        tvl_usd=10_000_000,
        curator="curator-a",
        reward_dependence=0.1,
    )
    return base.model_copy(update=updates)


def ids(vaults: object) -> set[str]:
    return {v.instrument_id for v in vaults}


def test_inactive_criteria_keeps_everything() -> None:
    vaults = [vault("a"), vault("b")]
    result = screen(vaults, ScreenCriteria())
    assert ids(result.kept) == {"a", "b"}
    assert result.dropped == ()
    assert ScreenCriteria().active is False


def test_min_history_days_drops_thin_series() -> None:
    rich = vault("rich", apy_series=(4.0, 5.0, 4.5, 5.5))
    thin = vault("thin", apy_series=(4.0,))
    result = screen([rich, thin], ScreenCriteria(min_history_days=3))
    assert ids(result.kept) == {"rich"}
    assert result.dropped[0].instrument_id == "thin"
    assert result.dropped[0].rule == "min_history_days"


def test_min_sharpe_drops_unknown_and_low() -> None:
    steady = vault("steady", apy_series=(9.0, 9.1, 8.9, 9.0))
    noisy = vault("noisy", apy_series=(1.0, 20.0, 1.0, 20.0))
    no_history = vault("nohist")
    result = screen(
        [steady, noisy, no_history], ScreenCriteria(min_sharpe=1.0)
    )
    assert "steady" in ids(result.kept)
    assert "no_history" not in ids(result.kept)
    dropped_rules = {d.instrument_id: d.rule for d in result.dropped}
    assert dropped_rules["nohist"] == "min_sharpe"


def test_max_drawdown_drops_deep_dips() -> None:
    calm = vault("calm", apy_series=(5.0, 5.0, 5.0))
    crash = vault("crash", apy_series=(50.0, 50.0, -90.0, -90.0))
    result = screen([calm, crash], ScreenCriteria(max_drawdown=0.001))
    assert ids(result.kept) == {"calm"}
    assert result.dropped[0].rule == "max_drawdown"


def test_max_reward_dependence_drops_reward_reliant_and_unknown() -> None:
    organic = vault("organic", reward_dependence=0.1)
    incentivized = vault("incentivized", reward_dependence=0.8)
    unknown = vault("unknown", reward_dependence=Unknown)
    result = screen(
        [organic, incentivized, unknown],
        ScreenCriteria(max_reward_dependence=0.5),
    )
    assert ids(result.kept) == {"organic"}
    assert {d.instrument_id for d in result.dropped} == {"incentivized", "unknown"}


def test_curator_allowlist_and_min_tvl() -> None:
    a = vault("a", curator="curator-a", tvl_usd=5_000_000)
    b = vault("b", curator="curator-b", tvl_usd=5_000_000)
    small = vault("small", curator="curator-a", tvl_usd=1_000)
    result = screen(
        [a, b, small],
        ScreenCriteria(curators=("curator-a",), min_tvl_usd=1_000_000),
    )
    assert ids(result.kept) == {"a"}
    rules = {d.instrument_id: d.rule for d in result.dropped}
    assert rules["b"] == "curators"
    assert rules["small"] == "min_tvl_usd"


def test_warnings_are_machine_readable() -> None:
    thin = vault("thin", apy_series=())
    result = screen([thin], ScreenCriteria(min_history_days=5))
    assert result.warnings() == ["screen_excluded:thin:min_history_days"]
