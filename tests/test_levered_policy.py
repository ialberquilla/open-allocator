"""Policy over levered rows: routing, gross exposure, and the health factor.

The fixture is a cross-asset stablecoin loop whose reward APY is
emission-priced and paid in one thinly traded token. It is judged against the
repo's own ``policy.yaml``, so a policy change that would let such a loop
through fails here.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import pytest
from pydantic import ValidationError

from open_allocator.core import eligibility
from open_allocator.core.policy import (
    PolicyResult,
    check,
    leg_health_factor,
    resulting_book,
)
from open_allocator.core.policy_loader import load_policy
from open_allocator.core.schema import SchemaValidationError, validate
from open_allocator.core.types import (
    Allocation,
    AllocationLeg,
    Policy,
    PolicyAllowed,
    PolicyCaps,
    PolicyGates,
    PolicyWallet,
    Vault,
    levered_ltv_floor,
)

REPO_POLICY = Path(__file__).resolve().parents[1] / "policy.yaml"

# ltv 0.85 under the reserve's own parameters is a 6.67x ceiling; an
# efficiency-mode category (ltv 0.93 / liqThr 0.94) is 14.29x.
RESERVE_MAX_LEVERAGE = 1 / (1 - 0.85)
EMODE_MAX_LEVERAGE = 1 / (1 - 0.93)
THIN_REWARD_VOLUME_USD = 635.0


def loop(**updates: object) -> Vault:
    """A cross-asset USDC/AUSD loop as a synthetic instrument."""
    base = Vault(
        instrument_id="loop:lender:USDC/AUSD",
        protocol="lender",
        chain_id=143,
        asset="USDC",
        is_stablecoin=True,
        apy=26.70,
        apy_base=2.055,
        apy_reward=24.645,
        reward_price_basis="emission",
        tvl_usd=2_796_631,
        curator="lender",
        # Leverage multiplies the reward share far past any unlevered row's.
        reward_dependence=0.92,
        oracle="pool-oracle",
        fee=0.0,
        is_levered=True,
        max_leverage=EMODE_MAX_LEVERAGE,
        liquidation_threshold=0.94,
        debt_asset="AUSD",
        reward_liquidity_usd=THIN_REWARD_VOLUME_USD,
    )
    return base.model_copy(update=updates)


def plain(instrument_id: str = "plain", **updates: object) -> Vault:
    base = Vault(
        instrument_id=instrument_id,
        protocol="morpho",
        chain_id=8453,
        asset="USDC",
        is_stablecoin=True,
        apy=4.0,
        tvl_usd=10_000_000,
        curator="curator-a",
        reward_dependence=0.1,
        oracle="chainlink",
        fee=0.05,
    )
    return base.model_copy(update=updates)


def caps(**updates: object) -> PolicyCaps:
    base = PolicyCaps(
        max_weight_per_instrument=1,
        max_weight_per_protocol=1,
        max_weight_per_curator=1,
        max_weight_per_chain=1,
        min_instrument_tvl_usd=0,
        max_reward_dependence=1,
    )
    return base.model_copy(update=updates)


def policy(**cap_updates: object) -> Policy:
    return Policy(
        wallet=PolicyWallet(mode="self-custody", signer="local-eoa"),
        allowed=PolicyAllowed(),
        caps=caps(**cap_updates),
        gates=PolicyGates(
            new_instrument_needs_approval=False,
            autonomous_rebalance=True,
            max_deploy_per_cycle_usd=1_000_000,
        ),
    )


def book(
    legs: Iterable[tuple[str, float, float | None]],
    *,
    total_usd: float = 1_000,
) -> Allocation:
    return Allocation(
        legs=tuple(
            AllocationLeg(
                instrument_id=instrument_id,
                weight=weight,
                usd=weight * total_usd,
                leverage=leverage,
            )
            for instrument_id, weight, leverage in legs
        ),
        total_usd=total_usd,
    )


def rules(result: PolicyResult) -> set[str]:
    return {violation.rule for violation in result.violations}


def only(result: PolicyResult, rule: str) -> object:
    matches = [v for v in result.violations if v.rule == rule]
    assert len(matches) == 1, result.violations
    return matches[0]


LOOP_ID = loop().instrument_id


# --- 3.2: routing ----------------------------------------------------------


def test_a_thin_reward_loop_skips_reward_dependence_and_fails_liquidity() -> None:
    findings = eligibility.candidate_findings(loop(), load_policy(REPO_POLICY))
    found = {finding.rule: finding for finding in findings}

    assert "max_reward_dependence" not in found
    assert found["min_reward_liquidity_usd"].actual == THIN_REWARD_VOLUME_USD
    assert found["min_reward_liquidity_usd"].limit == 50_000


def test_routing_is_not_removal_the_same_row_unlevered_still_fails_dependence() -> None:
    unlevered = plain(reward_dependence=0.686)

    findings = eligibility.candidate_findings(unlevered, load_policy(REPO_POLICY))

    assert "max_reward_dependence" in {finding.rule for finding in findings}


def test_unmeasured_reward_liquidity_fails_rather_than_passes() -> None:
    """A ``null`` volume must never read as a pass."""
    finding = next(
        f
        for f in eligibility.candidate_findings(
            loop(reward_liquidity_usd=None), policy(min_reward_liquidity_usd=1)
        )
        if f.rule == "min_reward_liquidity_usd"
    )

    assert finding.actual == "Unknown"


def test_a_loop_that_pays_no_reward_has_nothing_to_sell() -> None:
    no_reward = loop(
        apy_reward=0.0, reward_price_basis="none", reward_liquidity_usd=None
    )

    findings = eligibility.candidate_findings(
        no_reward, policy(min_reward_liquidity_usd=50_000)
    )

    assert "min_reward_liquidity_usd" not in {f.rule for f in findings}


def test_a_deep_reward_market_clears_the_liquidity_floor() -> None:
    findings = eligibility.candidate_findings(
        loop(reward_liquidity_usd=5_000_000), policy(min_reward_liquidity_usd=50_000)
    )

    assert findings == []


def test_a_zero_levered_ceiling_narrows_levered_rows_out_before_construction() -> None:
    result = eligibility.candidate_exclusion(loop(), policy(max_weight_levered=0.0))

    assert result == "max_weight_levered"
    assert (
        eligibility.candidate_exclusion(plain(), policy(max_weight_levered=0.0)) is None
    )


def test_levered_caps_do_not_touch_an_unlevered_book() -> None:
    """Every existing book is unchanged by the new knobs, set to anything."""
    strict = policy(
        max_gross_leverage=1.0,
        max_book_gross_exposure=1.0,
        max_weight_levered=0.0,
        min_health_factor=5.0,
        min_depeg_buffer_bps=10_000,
        min_reward_liquidity_usd=1e12,
    )
    vaults = (plain("a", reward_liquidity_usd=None), plain("b"))

    result = check(book((("a", 0.5, None), ("b", 0.5, None))), strict, vaults)

    assert result.ok, result.violations


# --- 3.3: gross exposure ---------------------------------------------------


def test_thirty_percent_at_eight_x_fails_a_two_x_book() -> None:
    """0.3 x 8 + 0.7 x 1 = 3.1 gross."""
    result = check(
        book(((LOOP_ID, 0.3, 8.0), ("plain", 0.7, None))),
        policy(max_book_gross_exposure=2.0),
        (loop(), plain()),
    )

    violation = only(result, "max_book_gross_exposure")
    assert violation.entity == "allocation"  # type: ignore[attr-defined]
    assert violation.actual == pytest.approx(3.1)  # type: ignore[attr-defined]


def test_the_same_book_passes_every_weight_cap_that_cannot_see_leverage() -> None:
    """Why the gross cap exists: 30% clears a 30% instrument cap at any L."""
    result = check(
        book(((LOOP_ID, 0.3, 8.0), ("plain", 0.7, None))),
        policy(max_weight_per_instrument=0.7),
        (loop(), plain()),
    )

    assert result.ok, result.violations


def test_an_unchosen_leverage_is_charged_at_the_declared_ceiling() -> None:
    """Not at 1: an L nobody picked is whatever the venue allows."""
    result = check(
        book(((LOOP_ID, 0.3, None), ("plain", 0.7, None))),
        policy(max_book_gross_exposure=2.0, max_gross_leverage=4.0),
        (loop(), plain()),
    )

    assert only(result, "max_gross_leverage").actual == pytest.approx(  # type: ignore[attr-defined]
        EMODE_MAX_LEVERAGE
    )
    assert only(result, "max_book_gross_exposure").actual == pytest.approx(  # type: ignore[attr-defined]
        0.3 * EMODE_MAX_LEVERAGE + 0.7
    )


def test_a_leg_above_its_rows_ceiling_is_unreachable_not_just_large() -> None:
    result = check(
        book(((LOOP_ID, 1.0, 20.0),)),
        policy(),
        (loop(),),
    )

    assert rules(result) == {"max_leverage_declared"}


def test_leverage_on_an_unlevered_instrument_is_refused() -> None:
    result = check(book((("plain", 1.0, 3.0),)), policy(), (plain(),))

    assert rules(result) == {"leverage_on_unlevered_instrument"}


def test_levered_weight_is_summed_across_the_book() -> None:
    second = loop(instrument_id="loop:b", reward_liquidity_usd=None)
    result = check(
        book(((LOOP_ID, 0.08, 2.0), ("loop:b", 0.07, 2.0), ("plain", 0.85, None))),
        policy(max_weight_levered=0.10),
        (loop(), second, plain()),
    )

    assert only(result, "max_weight_levered").actual == pytest.approx(0.15)  # type: ignore[attr-defined]


# --- 3.3: health factor and depeg budget -----------------------------------


def test_eight_x_under_emode_is_below_a_one_ten_health_floor() -> None:
    """L=8 at liqThr 0.94 is HF 8 x 0.94 / 7 = 1.074."""
    result = check(
        book(((LOOP_ID, 1.0, 8.0),)), policy(min_health_factor=1.10), (loop(),)
    )

    assert only(result, "min_health_factor").actual == pytest.approx(  # type: ignore[attr-defined]
        1.074286, abs=1e-6
    )


def test_without_a_published_threshold_the_health_factor_is_a_lower_bound() -> None:
    """The LTV the ceiling implies stands in, and it only ever errs low."""
    unpublished = loop(liquidation_threshold=None, max_leverage=RESERVE_MAX_LEVERAGE)

    bound = leg_health_factor(unpublished, 4.0)
    published = leg_health_factor(
        unpublished.model_copy(update={"liquidation_threshold": 0.90}), 4.0
    )

    assert levered_ltv_floor(unpublished) == pytest.approx(0.85)
    assert bound == pytest.approx(4 * 0.85 / 3)
    assert published is not None and bound is not None and bound < published


def test_a_cross_asset_loop_is_held_to_the_depeg_budget() -> None:
    """At L=8 the buffer is 743 bps."""
    result = check(
        book(((LOOP_ID, 1.0, 8.0),)), policy(min_depeg_buffer_bps=1200), (loop(),)
    )

    assert only(result, "min_depeg_buffer_bps").actual == 743  # type: ignore[attr-defined]


def test_a_same_asset_loop_has_no_price_ratio_to_budget_for() -> None:
    same = loop(debt_asset="usdc")

    result = check(
        book(((LOOP_ID, 1.0, 8.0),)), policy(min_depeg_buffer_bps=1200), (same,)
    )

    assert result.ok, result.violations


def test_an_unreported_debt_asset_is_budgeted_as_cross_asset() -> None:
    unknown_debt = loop(debt_asset=None)

    result = check(
        book(((LOOP_ID, 1.0, 8.0),)), policy(min_depeg_buffer_bps=1200), (unknown_debt,)
    )

    assert "min_depeg_buffer_bps" in rules(result)


def test_an_unlevered_leg_of_a_levered_row_has_no_debt_and_no_health_factor() -> None:
    result = check(
        book(((LOOP_ID, 1.0, 1.0),)),
        policy(min_health_factor=1.5, min_depeg_buffer_bps=5_000),
        (loop(),),
    )

    assert result.ok, result.violations


# --- the phase gate ---------------------------------------------------------


def test_a_thin_reward_loop_is_rejected_by_the_shipped_policy() -> None:
    shipped = load_policy(REPO_POLICY)

    result = check(
        book(((LOOP_ID, 0.10, 8.0), ("plain", 0.90, None))),
        shipped.model_copy(
            update={
                "caps": shipped.caps.model_copy(
                    update={"min_effective_positions": None}
                )
            }
        ),
        (loop(), plain()),
    )

    assert not result.ok
    assert "max_reward_dependence" not in rules(result)
    assert {
        "min_reward_liquidity_usd",
        "max_gross_leverage",
        "min_health_factor",
        "min_depeg_buffer_bps",
    } <= rules(result)


# --- the book a top-up produces --------------------------------------------


def test_a_new_levered_leg_keeps_its_leverage_in_the_resulting_book() -> None:
    merged = resulting_book(book(((LOOP_ID, 1.0, 3.0),), total_usd=100), {"plain": 900})

    by_id = {leg.instrument_id: leg for leg in merged.legs}
    assert by_id[LOOP_ID].leverage == 3.0
    assert by_id["plain"].leverage is None


def test_a_top_up_of_a_held_levered_leg_falls_back_to_the_ceiling() -> None:
    """The held L is not recorded here, and a blend of two unknowns is unknown."""
    merged = resulting_book(book(((LOOP_ID, 1.0, 3.0),), total_usd=100), {LOOP_ID: 100})

    assert merged.legs[0].leverage is None


# --- the types and schemas that carry it ------------------------------------


def test_pair_parameters_belong_to_levered_rows_only() -> None:
    with pytest.raises(ValidationError, match="debt_asset belongs to levered rows"):
        Vault.model_validate(plain().model_dump() | {"debt_asset": "AUSD"})
    with pytest.raises(ValidationError, match="liquidation_threshold belongs"):
        Vault.model_validate(plain().model_dump() | {"liquidation_threshold": 0.9})


def test_a_threshold_below_the_implied_ltv_would_liquidate_at_open() -> None:
    with pytest.raises(ValidationError, match="liquidate at open"):
        Vault.model_validate(
            loop().model_dump()
            | {"max_leverage": RESERVE_MAX_LEVERAGE, "liquidation_threshold": 0.80}
        )


@pytest.mark.parametrize("floor", [1.0, 0.95])
def test_a_health_floor_at_or_below_one_binds_nothing_and_is_refused(
    floor: float,
) -> None:
    with pytest.raises(ValidationError):
        PolicyCaps.model_validate(caps().model_dump() | {"min_health_factor": floor})
    raw = load_policy(REPO_POLICY).model_dump(mode="json")
    raw["caps"]["min_health_factor"] = floor
    with pytest.raises(SchemaValidationError):
        validate(raw, "policy")


def test_the_shipped_policy_carries_every_levered_cap() -> None:
    shipped = load_policy(REPO_POLICY).caps

    assert shipped.max_weight_levered == 0.10
    assert shipped.max_gross_leverage == 4.0
    assert shipped.max_book_gross_exposure == 1.5
    assert shipped.min_health_factor == 1.10
    assert shipped.min_depeg_buffer_bps == 1500
    assert shipped.min_reward_liquidity_usd == 50_000


def test_a_levered_leg_round_trips_through_the_allocation_schema() -> None:
    payload = book(((LOOP_ID, 0.3, 8.0), ("plain", 0.7, None))).model_dump(mode="json")

    assert validate(payload, "allocation") == payload
    assert Allocation.model_validate(payload).legs[0].leverage == 8.0
