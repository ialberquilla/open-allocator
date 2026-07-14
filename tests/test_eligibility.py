from __future__ import annotations

import pytest

from open_allocator.core import eligibility
from open_allocator.core.policy import check
from open_allocator.core.types import (
    Allocation,
    AllocationLeg,
    Policy,
    PolicyAllowed,
    PolicyCaps,
    PolicyGates,
    PolicyWallet,
    Unknown,
    Vault,
)


def vault(**updates: object) -> Vault:
    base = Vault(
        instrument_id="vault-a",
        protocol="aave",
        chain_id=8453,
        asset="USDC",
        asset_category="USD",
        is_stablecoin=True,
        apy=0.05,
        tvl_usd=10_000_000,
        curator="curator-a",
        reward_dependence=0.1,
    )
    return base.model_copy(update=updates)


def policy(
    *,
    allowed: PolicyAllowed | None = None,
    caps: PolicyCaps | None = None,
) -> Policy:
    return Policy(
        wallet=PolicyWallet(mode="self-custody", signer="local-eoa"),
        allowed=allowed or PolicyAllowed(),
        caps=caps
        or PolicyCaps(
            max_weight_per_instrument=1,
            max_weight_per_protocol=1,
            max_weight_per_curator=1,
            max_weight_per_chain=1,
            min_instrument_tvl_usd=0,
            max_reward_dependence=1,
        ),
        gates=PolicyGates(
            new_instrument_needs_approval=False,
            autonomous_rebalance=True,
            max_deploy_per_cycle_usd=1_000_000_000,
        ),
    )


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


# --- scalar helpers --------------------------------------------------------


def test_policy_value_normalizes_unknown() -> None:
    assert eligibility.policy_value(Unknown) == "Unknown"
    assert eligibility.policy_value(0.4) == 0.4
    assert eligibility.policy_number(Unknown) is None
    assert eligibility.policy_number(True) is None
    assert eligibility.policy_number(0.4) == 0.4


# --- per-axis findings -----------------------------------------------------


def test_allowlists_flag_out_of_set_values() -> None:
    result = eligibility.candidate_findings(
        vault(protocol="curve", asset="WBTC"),
        policy(allowed=PolicyAllowed(protocols=("aave",), assets=("USDC",))),
    )
    rules = {finding.rule for finding in result}
    assert rules == {"allowed_protocols", "allowed_assets"}


def test_stablecoin_only_flags_non_stable() -> None:
    result = eligibility.candidate_findings(
        vault(is_stablecoin=False),
        policy(allowed=PolicyAllowed(stablecoin_only=True)),
    )
    assert [finding.rule for finding in result] == ["allowed_stablecoin_only"]


def test_unknown_reward_dependence_is_a_finding() -> None:
    result = eligibility.quality_findings(
        vault(reward_dependence=Unknown), caps(max_reward_dependence=0.5)
    )
    finding = next(f for f in result if f.rule == "max_reward_dependence")
    assert finding.actual == "Unknown"


def test_candidate_findings_order_is_allowlists_then_quality() -> None:
    result = eligibility.candidate_findings(
        vault(protocol="curve", tvl_usd=1),
        policy(
            allowed=PolicyAllowed(protocols=("aave",)),
            caps=caps(min_instrument_tvl_usd=1_000),
        ),
    )
    assert [finding.rule for finding in result] == [
        "allowed_protocols",
        "min_instrument_tvl_usd",
    ]


# --- discovery scope is coarser than candidate scope ----------------------


def test_discovery_ignores_curator_and_reward() -> None:
    p = policy(
        allowed=PolicyAllowed(curators=("only-this",)),
        caps=caps(max_reward_dependence=0.0),
    )
    v = vault(protocol="morpho", curator="other", reward_dependence=0.9)
    # Candidate scope rejects it; discovery scope (coarse) still admits it.
    assert eligibility.candidate_exclusion(v, p) is not None
    assert eligibility.discovery_eligible(v, p) is True


def test_discovery_still_applies_allowlists_and_tvl_floor() -> None:
    p = policy(
        allowed=PolicyAllowed(protocols=("aave",)),
        caps=caps(min_instrument_tvl_usd=1_000),
    )
    assert eligibility.discovery_eligible(vault(protocol="curve"), p) is False
    assert eligibility.discovery_eligible(vault(tvl_usd=1), p) is False
    assert eligibility.discovery_eligible(vault(), p) is True


# --- the invariant: candidate narrowing agrees with policy.check ----------


@pytest.mark.parametrize(
    "bad_vault,bad_policy,expected_rule",
    [
        (
            vault(protocol="curve"),
            policy(allowed=PolicyAllowed(protocols=("aave",))),
            "allowed_protocols",
        ),
        (
            vault(chain_id=1),
            policy(allowed=PolicyAllowed(chains=(8453,))),
            "allowed_chains",
        ),
        (
            vault(is_stablecoin=False),
            policy(allowed=PolicyAllowed(stablecoin_only=True)),
            "allowed_stablecoin_only",
        ),
        (
            vault(curator="other"),
            policy(allowed=PolicyAllowed(curators=("curator-a",))),
            "allowed_curators",
        ),
        (
            vault(tvl_usd=1),
            policy(caps=caps(min_instrument_tvl_usd=1_000)),
            "min_instrument_tvl_usd",
        ),
        (
            vault(reward_dependence=0.9),
            policy(caps=caps(max_reward_dependence=0.1)),
            "max_reward_dependence",
        ),
    ],
)
def test_candidate_exclusion_matches_policy_check(
    bad_vault: Vault,
    bad_policy: Policy,
    expected_rule: str,
) -> None:
    # CLI candidate narrowing uses candidate_exclusion; policy.check is the
    # authoritative gate. They must never disagree about a single vault.
    assert eligibility.candidate_exclusion(bad_vault, bad_policy) == expected_rule

    leg = AllocationLeg(instrument_id=bad_vault.instrument_id, weight=1.0, usd=100.0)
    allocation = Allocation(legs=(leg,), total_usd=100.0)
    result = check(allocation, bad_policy, (bad_vault,))
    assert expected_rule in {violation.rule for violation in result.violations}
