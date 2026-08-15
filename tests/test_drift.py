"""The daily gate, and the one answer it is not allowed to get wrong.

`drifted: false` tells the caller to skip looking. So the tests that matter most
are not the ones proving drift fires when the book moved -- they are the ones
proving it does *not* stay quiet when a check could not be run at all. A wrong
`true` costs one agent run; a wrong `false` is a book that stops being governed
by the mandate it claims to follow, silently, for as long as nobody checks.
"""

from __future__ import annotations

import json
import random
from datetime import date, timedelta
from pathlib import Path

import pytest
import yaml

from open_allocator.core import costs
from open_allocator.core import drift as drift_core
from open_allocator.core.mandate import Mandate
from open_allocator.core.positions import IdleBalance, PositionHolding, Positions
from open_allocator.core.types import (
    Allocation,
    AllocationLeg,
    Policy,
    PolicyAllowed,
    PolicyCaps,
    PolicyGates,
    PolicyWallet,
    Vault,
)

ADDRESS = "0x0000000000000000000000000000000000000001"


def mandate(
    *,
    weight_drift_bps: int = 100,
    sleeve_drift_bps: int = 500,
    # Defaulted rather than left absent: an absent band makes the opportunity
    # check unevaluated, which makes every other test in this file drift for a
    # reason it is not about.
    min_uplift_bps: int | None = 50,
    tiers: list[dict[str, object]] | None = None,
) -> Mandate:
    return Mandate.model_validate(
        {
            "version": 1,
            "text": "different buckets, good yield, enough diversification",
            "derived_at": "2026-08-14T00:00:00Z",
            "derived_by": "test",
            "policy_path": "policy-derived.yaml",
            "policy_hash": "sha256:" + "0" * 64,
            "strategy": "sleeves",
            "strategy_params": {
                # One tier by default so sleeve_band is satisfied trivially and
                # every other test isolates the check it is actually about. The
                # ladder is exercised deliberately in the sleeve test below.
                "tiers": tiers
                if tiers is not None
                else [
                    {
                        "name": "all",
                        "min_score": 0.00,
                        "max_score": 1.01,
                        "weight": 1.0,
                    },
                ]
            },
            "bands": {
                "weight_drift_bps": weight_drift_bps,
                "sleeve_drift_bps": sleeve_drift_bps,
                **(
                    {} if min_uplift_bps is None else {"min_uplift_bps": min_uplift_bps}
                ),
            },
            "rationale": [
                {
                    "knob": "strategy",
                    "value": "sleeves",
                    "because": "the ask names buckets",
                }
            ],
        }
    )


def policy(*, min_effective_positions: float | None = None) -> Policy:
    return Policy(
        wallet=PolicyWallet(mode="self-custody", signer="local-eoa"),
        allowed=PolicyAllowed(protocols=None, chains=None, assets=None, curators=None),
        caps=PolicyCaps(
            max_weight_per_instrument=1,
            max_weight_per_protocol=1,
            max_weight_per_curator=1,
            max_weight_per_chain=1,
            min_instrument_tvl_usd=1,
            max_reward_dependence=1,
            min_effective_positions=min_effective_positions,
        ),
        gates=PolicyGates(
            new_instrument_needs_approval=False,
            autonomous_rebalance=True,
            max_deploy_per_cycle_usd=1_000_000,
        ),
    )


def series(seed: int, count: int = 120) -> tuple[tuple[date, float], ...]:
    """A dated random walk long enough to clear `diversify.MIN_OVERLAP` (60).

    Below that every pair is unmeasured, unmeasured fails closed to one bet, and
    the independence floor becomes impossible rather than merely strict -- so a
    short series would test the failure path while looking like the happy one.
    """
    rng = random.Random(seed)
    level = 5.0
    values = []
    for _ in range(count):
        level += rng.random() - 0.5
        values.append(level)
    return tuple(
        (date(2026, 1, 1) + timedelta(days=index), value)
        for index, value in enumerate(values)
    )


def vault(
    instrument_id: str,
    *,
    apy: float = 0.04,
    history: bool = False,
    seed: int = 1,
    chain_id: int = 8453,
) -> Vault:
    payload: dict[str, object] = {
        "instrument_id": instrument_id,
        "protocol": "aave",
        "chain_id": chain_id,
        "asset": "USDC",
        "apy": apy,
        "tvl_usd": 1_000_000,
        "curator": "curator-a",
        "reward_dependence": 0.1,
    }
    if history:
        payload["apy_daily"] = series(seed)
    return Vault.model_validate(payload)


def holding(instrument_id: str, usd: float) -> PositionHolding:
    return PositionHolding(
        instrument_id=instrument_id,
        protocol="aave",
        chain_id=8453,
        symbol="USDC",
        balance=str(usd),
        balance_raw=str(int(usd * 1_000_000)),
        decimals=6,
        usd_value=usd,
        share_balance=str(usd),
        share_balance_raw=str(int(usd * 1_000_000)),
        share_decimals=6,
        yield_token_symbol="aUSDC",
        yield_token_address="0x0000000000000000000000000000000000000002",
    )


def book(*holdings: PositionHolding) -> Positions:
    total = sum(item.usd_value for item in holdings)
    return Positions(
        address=ADDRESS,
        holdings=tuple(holdings),
        idle_balances=(
            IdleBalance(
                chain_id=8453,
                chain_name="Base",
                usdc_balance="0",
                usdc_balance_raw="0",
                usd_value=0,
            ),
        ),
        total_position_usd=total,
        total_idle_usdc=0,
        total_usd=total,
        total_usdc_usd="0",
    )


def target(*legs: tuple[str, float]) -> Allocation:
    total = 1_000.0
    return Allocation(
        legs=tuple(
            AllocationLeg(instrument_id=key, weight=weight, usd=total * weight)
            for key, weight in legs
        ),
        total_usd=total,
        metadata={},
    )


def reasons_of(report: drift_core.DriftReport, kind: str) -> list[object]:
    return [reason for reason in report.reasons if reason.type == kind]


# --- the answer that must never be wrong ----------------------------------


def test_a_check_that_cannot_be_run_drifts_rather_than_passing() -> None:
    """No target allocation means no per-instrument target, and a mandate
    carries tier weights rather than per-instrument ones. Reporting `false`
    here would tell the caller the bands are fine."""
    report = drift_core.evaluate(
        mandate(),
        book(holding("vault-a", 500), holding("vault-b", 500)),
        policy(),
        known_instruments=[vault("vault-a"), vault("vault-b")],
    )

    assert report.drifted is True
    unevaluated = reasons_of(report, "unevaluated")
    assert [item.check for item in unevaluated] == ["weight_band"]
    assert "no target allocation" in unevaluated[0].because


def test_a_held_instrument_missing_from_the_shelf_does_not_shrink_a_sleeve() -> None:
    """Dropping it would make every sleeve total read low for a reason that has
    nothing to do with drift; bucketing it would guess which sleeve it was in."""
    report = drift_core.evaluate(
        mandate(),
        book(holding("vault-a", 500), holding("delisted", 500)),
        policy(),
        target=target(("vault-a", 0.5), ("delisted", 0.5)),
        known_instruments=[vault("vault-a")],
    )

    unevaluated = reasons_of(report, "unevaluated")
    # Two checks, independently unevaluable for the same root cause. Reported
    # separately rather than deduplicated: a caller filtering on `check` should
    # not have to know that sleeve_band failing implies opportunity failing,
    # and collapsing them would make the report depend on evaluation order.
    assert sorted(item.check for item in unevaluated) == ["opportunity", "sleeve_band"]
    assert all("delisted" in item.because for item in unevaluated)
    assert report.drifted is True


def test_an_unmeasurable_independence_count_is_none_not_zero() -> None:
    """Zero would read as the most concentrated book possible, which is
    alarming in the wrong direction and, worse, actionable."""
    report = drift_core.evaluate(
        mandate(),
        book(holding("vault-a", 1_000)),
        policy(),
        target=target(("vault-a", 1.0)),
        known_instruments=[vault("vault-a")],
    )

    assert report.effective_positions is None


# --- a book that has not moved --------------------------------------------


def test_a_book_matching_its_target_within_the_bands_does_not_drift() -> None:
    report = drift_core.evaluate(
        mandate(),
        book(holding("vault-a", 600), holding("vault-b", 400)),
        policy(),
        target=target(("vault-a", 0.60), ("vault-b", 0.40)),
        known_instruments=[
            vault("vault-a", apy=0.09, history=True, seed=1),
            vault("vault-b", apy=0.01, history=True, seed=2),
        ],
    )

    assert report.reasons == ()
    assert report.drifted is False
    assert report.total_usd == 1_000


# --- the four reason types ------------------------------------------------


def test_a_leg_outside_its_band_reports_the_target_it_missed() -> None:
    report = drift_core.evaluate(
        mandate(weight_drift_bps=100),
        book(holding("vault-a", 700), holding("vault-b", 300)),
        policy(),
        target=target(("vault-a", 0.60), ("vault-b", 0.40)),
        known_instruments=[
            vault("vault-a", apy=0.09, history=True, seed=1),
            vault("vault-b", apy=0.01, history=True, seed=2),
        ],
    )

    weight = reasons_of(report, "weight_band")
    assert {item.instrument_id for item in weight} == {"vault-a", "vault-b"}
    first = next(item for item in weight if item.instrument_id == "vault-a")
    assert (first.target_bps, first.actual_bps, first.band_bps) == (6_000, 7_000, 100)


def test_a_position_held_outside_the_target_is_the_most_drifted_leg_there_is() -> None:
    """Iterating the target's own legs would miss exactly this case."""
    report = drift_core.evaluate(
        mandate(),
        book(holding("vault-a", 500), holding("stray", 500)),
        policy(),
        target=target(("vault-a", 1.0)),
        known_instruments=[vault("vault-a"), vault("stray")],
    )

    stray = next(
        item
        for item in reasons_of(report, "weight_band")
        if item.instrument_id == "stray"
    )
    assert (stray.target_bps, stray.actual_bps) == (0, 5_000)


def test_a_sleeve_away_from_its_declared_share_reports_the_gap() -> None:
    """The fixtures all score 0.7194, so the ladder is cut just above that: the
    whole book lands in `frontier`, which the mandate wanted at 40%."""
    report = drift_core.evaluate(
        mandate(
            sleeve_drift_bps=500,
            tiers=[
                {"name": "core", "min_score": 0.72, "max_score": 1.01, "weight": 0.60},
                {
                    "name": "frontier",
                    "min_score": 0.00,
                    "max_score": 0.72,
                    "weight": 0.40,
                },
            ],
        ),
        book(holding("vault-a", 600), holding("vault-b", 400)),
        policy(),
        target=target(("vault-a", 0.60), ("vault-b", 0.40)),
        known_instruments=[
            vault("vault-a", history=True, seed=1),
            vault("vault-b", history=True, seed=2),
        ],
    )

    sleeves = {item.sleeve: item for item in reasons_of(report, "sleeve_band")}

    assert sleeves["frontier"].target_bps == 4_000
    assert sleeves["frontier"].actual_bps == 10_000
    assert sleeves["core"].actual_bps == 0
    assert reasons_of(report, "weight_band") == []


def test_a_policy_floor_the_book_no_longer_clears_is_a_reason() -> None:
    report = drift_core.evaluate(
        mandate(),
        book(holding("vault-a", 500), holding("vault-b", 500)),
        policy(min_effective_positions=9.0),
        target=target(("vault-a", 0.5), ("vault-b", 0.5)),
        known_instruments=[
            vault("vault-a", apy=0.04, history=True, seed=1),
            vault("vault-b", apy=0.05, history=True, seed=2),
        ],
    )

    rules = [item.rule for item in reasons_of(report, "policy_violation")]
    assert any(rule.startswith("min_effective_positions") for rule in rules)
    assert report.drifted is True


def test_shelf_change_counts_both_directions() -> None:
    report = drift_core.evaluate(
        mandate(),
        book(holding("vault-a", 1_000)),
        policy(),
        target=target(("vault-a", 1.0)),
        known_instruments=[vault("vault-a"), vault("new-one")],
        previous_shelf={"instrument_ids": ["vault-a", "gone"]},
    )

    (change,) = reasons_of(report, "shelf_change")
    assert (change.new_instruments, change.delisted) == (1, 1)


def test_an_unchanged_shelf_is_not_a_reason() -> None:
    report = drift_core.evaluate(
        mandate(),
        book(holding("vault-a", 1_000)),
        policy(),
        target=target(("vault-a", 1.0)),
        known_instruments=[vault("vault-a")],
        previous_shelf={"instrument_ids": ["vault-a"]},
    )

    assert reasons_of(report, "shelf_change") == []


# --- the shelf snapshot ---------------------------------------------------


def test_yesterdays_list_vaults_output_is_a_valid_previous_shelf() -> None:
    """No conversion step, so the check works before any caller builds storage."""
    listed = [{"instrument_id": "vault-a"}, {"instrument_id": "vault-b"}]

    assert drift_core.ShelfSnapshot.parse(listed).instrument_ids == (
        "vault-a",
        "vault-b",
    )


def test_a_returned_snapshot_round_trips_as_the_next_days_input() -> None:
    report = drift_core.evaluate(
        mandate(),
        book(holding("vault-a", 1_000)),
        policy(),
        target=target(("vault-a", 1.0)),
        known_instruments=[vault("vault-a"), vault("vault-b")],
    )
    assert report.shelf is not None

    stored = json.loads(json.dumps(report.shelf.model_dump(mode="json")))

    assert drift_core.ShelfSnapshot.parse(stored) == report.shelf


def test_the_snapshot_hash_ignores_ordering() -> None:
    """It answers "did the set change", so a reordered discovery is not a change."""
    assert (
        drift_core.ShelfSnapshot.of(["b", "a"]).hash
        == drift_core.ShelfSnapshot.of(["a", "b"]).hash
    )


# --- weights ---------------------------------------------------------------


def test_idle_usdc_is_not_a_position_and_does_not_dilute_the_weights() -> None:
    """Folding it in would make every leg read as under-weight by whatever was
    waiting to be deployed, which is a funding state, not drift."""
    positions = book(holding("vault-a", 600), holding("vault-b", 400))
    funded = positions.model_copy(
        update={
            "idle_balances": (
                IdleBalance(
                    chain_id=8453,
                    chain_name="Base",
                    usdc_balance="1000",
                    usdc_balance_raw="1000000000",
                    usd_value=1_000,
                ),
            ),
            "total_idle_usdc": 1_000,
            "total_usd": 2_000,
        }
    )

    report = drift_core.evaluate(
        mandate(),
        funded,
        policy(),
        target=target(("vault-a", 0.60), ("vault-b", 0.40)),
        known_instruments=[
            vault("vault-a", apy=0.09, history=True, seed=1),
            vault("vault-b", apy=0.01, history=True, seed=2),
        ],
    )

    assert reasons_of(report, "weight_band") == []
    assert report.total_usd == 1_000


def test_an_empty_book_reports_no_weights_rather_than_failing() -> None:
    report = drift_core.evaluate(
        mandate(),
        book(),
        policy(),
        target=target(("vault-a", 1.0)),
        known_instruments=[vault("vault-a")],
    )

    assert report.total_usd == 0
    assert report.effective_positions is None
    weight = reasons_of(report, "weight_band")
    assert [item.instrument_id for item in weight] == ["vault-a"]
    assert weight[0].actual_bps == 0


# --- the CLI surface ------------------------------------------------------


def test_the_command_reads_the_derived_policy_relative_to_the_mandate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mandate and its policy travel together, so the pair must resolve the
    same way from any working directory -- which is the case a container hits."""
    from open_allocator import cli

    nested = tmp_path / "config"
    nested.mkdir()
    policy_file = nested / "policy-derived.yaml"
    policy_file.write_text(yaml.safe_dump(policy().model_dump(mode="json")))
    mandate_file = nested / "mandate.yaml"
    mandate_file.write_text(
        yaml.safe_dump(
            mandate().model_dump(mode="json") | {"policy_path": "policy-derived.yaml"}
        )
    )
    positions_file = tmp_path / "positions.json"
    positions_file.write_text(
        json.dumps(book(holding("vault-a", 1_000)).model_dump(mode="json"))
    )

    monkeypatch.setattr(cli, "_discover_vaults", lambda **_: [vault("vault-a")])
    monkeypatch.chdir(tmp_path)

    payload = _run_drift(cli, mandate_file, positions_file)

    assert payload["total_usd"] == 1_000
    assert payload["drifted"] is True


def _run_drift(
    cli: object, mandate_file: Path, positions_file: Path
) -> dict[str, object]:
    from typer.testing import CliRunner

    result = CliRunner().invoke(
        cli.app,
        ["drift", "--mandate", str(mandate_file), "--positions", str(positions_file)],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


# --- opportunity: the shelf moving under a book that did not ---------------
#
# The other four reason types all ask whether the book left the mandate. These
# ask the question none of them can: the book is exactly where it should be, and
# a better-paying instrument appeared next to it. Every check above answers "no"
# to that, which is how a satellite sits in a 5% pool while a 9% pool is listed.


def test_opportunity_fires_when_a_better_same_sleeve_candidate_appears() -> None:
    report = drift_core.evaluate(
        mandate(min_uplift_bps=50),
        book(holding("vault-a", 1_000)),
        policy(),
        target=target(("vault-a", 1.0)),
        known_instruments=[
            vault("vault-a", apy=5.0),
            vault("vault-b", apy=9.0),
        ],
    )

    found = reasons_of(report, "opportunity")
    assert len(found) == 1
    assert found[0].held_instrument_id == "vault-a"
    assert found[0].candidate_instrument_id == "vault-b"
    assert found[0].uplift_bps == 400
    assert report.drifted is True


def test_opportunity_stays_quiet_below_the_mandate_band() -> None:
    """The band is the mandate's churn tolerance and it is the first gate."""
    report = drift_core.evaluate(
        mandate(min_uplift_bps=500),
        book(holding("vault-a", 1_000)),
        policy(),
        target=target(("vault-a", 1.0)),
        known_instruments=[
            vault("vault-a", apy=5.0),
            # 400 bps better, but the mandate asked for 500.
            vault("vault-b", apy=9.0),
        ],
    )

    assert reasons_of(report, "opportunity") == []


def test_opportunity_stays_quiet_when_the_switch_never_repays() -> None:
    """The band is a claim about rate; execution cost is a claim about dollars.

    A 60 bps uplift clears a 50 bps band on any book. Whether it is worth taking
    depends entirely on position size against gas, and the mandate band cannot
    see that -- so the payback test is arithmetic the mandate does not get a
    vote on.

    Priced on L1 deliberately. On Base a round trip is fractions of a cent and
    this gate almost never binds; the case where it decides anything is the one
    where gas is real, and testing it on the chain where it is free would prove
    nothing.
    """
    report = drift_core.evaluate(
        mandate(min_uplift_bps=50),
        book(holding("vault-a", 200)),
        policy(),
        target=target(("vault-a", 1.0)),
        known_instruments=[
            vault("vault-a", apy=5.00, chain_id=1),
            vault("vault-b", apy=5.60, chain_id=1),
        ],
        cost_params=costs.CostParams(
            gas=costs.GasPricing(gas_price_wei={1: 15_000_000_000}, eth_usd=3_000.0)
        ),
    )

    # $200 at 60 bps earns $1.20/year against a ~$25.65 round trip: ~21 years.
    assert reasons_of(report, "opportunity") == []


def test_opportunity_does_not_cross_sleeves() -> None:
    """A better-paying frontier name is not an opportunity for a core holding.

    Yield is not the mandate. Crossing the ladder to chase it is the thing the
    sleeve structure exists to prevent, so a cross-sleeve uplift must not read
    as a reason to rebalance.
    """
    ladder = [
        {"name": "core", "min_score": 0.50, "max_score": 1.01, "weight": 1.0},
        {"name": "frontier", "min_score": 0.00, "max_score": 0.50, "weight": 0.0},
    ]
    # vault-b pays far more but scores into the other sleeve: no reward
    # dependence cap headroom and a thin book drop its score below the cut.
    risky = Vault.model_validate(
        {
            "instrument_id": "vault-b",
            "protocol": "aave",
            "chain_id": 8453,
            "asset": "USDC",
            "apy": 20.0,
            "tvl_usd": 1_000,
            "curator": "curator-b",
            "reward_dependence": 0.95,
        }
    )
    report = drift_core.evaluate(
        mandate(min_uplift_bps=50, tiers=ladder),
        book(holding("vault-a", 1_000)),
        policy(),
        target=target(("vault-a", 1.0)),
        known_instruments=[vault("vault-a", apy=5.0), risky],
    )

    crossed = [
        reason
        for reason in reasons_of(report, "opportunity")
        if reason.candidate_instrument_id == "vault-b"
        and reason.sleeve
        != drift_core._tier_for(drift_core.score_vault(risky).score, ladder)["name"]
    ]
    assert crossed == []


def test_opportunity_ignores_candidates_already_held() -> None:
    report = drift_core.evaluate(
        mandate(min_uplift_bps=50),
        book(holding("vault-a", 1_000), holding("vault-b", 1_000)),
        policy(),
        target=target(("vault-a", 0.5), ("vault-b", 0.5)),
        known_instruments=[
            vault("vault-a", apy=5.0),
            vault("vault-b", apy=9.0),
        ],
    )

    assert reasons_of(report, "opportunity") == []


def test_opportunity_is_unevaluated_when_the_mandate_declares_no_band() -> None:
    """Absent is not zero, and it is not "no opportunity" either.

    A mandate with no churn tolerance has not said switching is never worth it;
    it has said nothing. Inventing a default here would be this module deciding
    policy, and reporting quiet would be the exact bug this check exists to fix.
    """
    report = drift_core.evaluate(
        mandate(min_uplift_bps=None),
        book(holding("vault-a", 1_000)),
        policy(),
        target=target(("vault-a", 1.0)),
        known_instruments=[vault("vault-a", apy=5.0), vault("vault-b", apy=9.0)],
    )

    unevaluated = reasons_of(report, "unevaluated")
    assert any(reason.check == "opportunity" for reason in unevaluated)
    assert report.drifted is True


def test_opportunity_is_unevaluated_when_a_holding_is_off_shelf() -> None:
    report = drift_core.evaluate(
        mandate(min_uplift_bps=50),
        book(holding("vault-a", 1_000), holding("ghost", 1_000)),
        policy(),
        target=target(("vault-a", 0.5), ("ghost", 0.5)),
        known_instruments=[vault("vault-a", apy=5.0), vault("vault-b", apy=9.0)],
    )

    assert any(
        reason.check == "opportunity" and "ghost" in reason.because
        for reason in reasons_of(report, "unevaluated")
    )
    assert report.drifted is True


def test_opportunity_proceeds_on_l2_with_fallback_gas() -> None:
    """An 8x overstatement of fractions of a cent cannot flip this verdict.

    Refusing to evaluate here would make the CLI useless as a gate on the chains
    the shelf actually lives on -- 41 of 58 instruments were Morpho on Base as
    of 2026-08-14.
    """
    report = drift_core.evaluate(
        mandate(min_uplift_bps=50),
        book(holding("vault-a", 1_000)),
        policy(),
        target=target(("vault-a", 1.0)),
        known_instruments=[
            vault("vault-a", apy=5.0, chain_id=8453),
            vault("vault-b", apy=9.0, chain_id=8453),
        ],
        cost_params=costs.CostParams(gas=None),
    )

    assert len(reasons_of(report, "opportunity")) == 1
    assert not [
        reason
        for reason in reasons_of(report, "unevaluated")
        if reason.check == "opportunity"
    ]


def test_opportunity_is_unevaluated_on_l1_with_fallback_gas() -> None:
    """The fallback runs ~50x too low on L1 at an ordinary base fee.

    That error points the wrong way: it makes a switch that never repays look
    like one that repays in a month, so the check must decline to answer rather
    than answer cheaply.
    """
    report = drift_core.evaluate(
        mandate(min_uplift_bps=50),
        book(holding("vault-a", 1_000)),
        policy(),
        target=target(("vault-a", 1.0)),
        known_instruments=[
            vault("vault-a", apy=5.0, chain_id=1),
            vault("vault-b", apy=9.0, chain_id=1),
        ],
        cost_params=costs.CostParams(gas=None),
    )

    assert any(
        reason.check == "opportunity" for reason in reasons_of(report, "unevaluated")
    )
    assert report.drifted is True


def test_opportunity_evaluates_l1_when_gas_is_priced_live() -> None:
    """Live pricing removes the objection: the number is measured, not assumed."""
    report = drift_core.evaluate(
        mandate(min_uplift_bps=50),
        book(holding("vault-a", 100_000)),
        policy(),
        target=target(("vault-a", 1.0)),
        known_instruments=[
            vault("vault-a", apy=5.0, chain_id=1),
            vault("vault-b", apy=9.0, chain_id=1),
        ],
        cost_params=costs.CostParams(
            gas=costs.GasPricing(gas_price_wei={1: 15_000_000_000}, eth_usd=3_000.0)
        ),
    )

    found = reasons_of(report, "opportunity")
    assert len(found) == 1
    # 190k gas * 15 gwei * $3000/ETH = ~$8.55/tx, three txs for a round trip.
    assert found[0].round_trip_cost_usd == pytest.approx(25.65, rel=1e-3)


def test_a_book_with_no_better_candidate_stays_quiet() -> None:
    """The gate's whole economic argument: most days this answers no."""
    report = drift_core.evaluate(
        mandate(min_uplift_bps=50),
        book(holding("vault-a", 1_000)),
        policy(),
        target=target(("vault-a", 1.0)),
        known_instruments=[
            vault("vault-a", apy=9.0),
            vault("vault-b", apy=5.0),
        ],
    )

    assert reasons_of(report, "opportunity") == []
    assert report.drifted is False
