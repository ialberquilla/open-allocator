from __future__ import annotations

from collections.abc import Iterable

import pytest

from open_allocator.core.policy import PolicyResult, check
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


def allocation(
    legs: Iterable[tuple[str, float, float]] = (("vault-a", 1.0, 100.0),),
    *,
    total_usd: float = 100,
    metadata: dict[str, object] | None = None,
) -> Allocation:
    return Allocation(
        legs=tuple(
            AllocationLeg(instrument_id=instrument_id, weight=weight, usd=usd)
            for instrument_id, weight, usd in legs
        ),
        total_usd=total_usd,
        metadata=metadata or {},
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


def gates(**updates: object) -> PolicyGates:
    base = PolicyGates(
        new_instrument_needs_approval=True,
        autonomous_rebalance=True,
        max_deploy_per_cycle_usd=1_000_000,
    )
    return base.model_copy(update=updates)


def policy(
    *,
    allowed: PolicyAllowed | None = None,
    policy_caps: PolicyCaps | None = None,
    policy_gates: PolicyGates | None = None,
) -> Policy:
    return Policy(
        wallet=PolicyWallet(mode="self-custody", signer="local-eoa"),
        allowed=allowed or PolicyAllowed(),
        caps=policy_caps or caps(),
        gates=policy_gates or gates(),
    )


def violation(result: PolicyResult, rule: str) -> object:
    matches = [item for item in result.violations if item.rule == rule]
    assert matches, result.violations
    return matches[0]


PASSING_CASES = [
    pytest.param(
        allocation(),
        policy(allowed=PolicyAllowed(protocols=("morpho",))),
        (vault(),),
        id="allowed_protocols",
    ),
    pytest.param(
        allocation(),
        policy(allowed=PolicyAllowed(chains=(8453,))),
        (vault(),),
        id="allowed_chains",
    ),
    pytest.param(
        allocation(),
        policy(allowed=PolicyAllowed(assets=("USDC",))),
        (vault(),),
        id="allowed_assets",
    ),
    pytest.param(
        allocation(),
        policy(allowed=PolicyAllowed(curators=("curator-a",))),
        (vault(),),
        id="allowed_curators",
    ),
    pytest.param(
        allocation((("vault-a", 0.5, 50),), total_usd=50),
        policy(policy_caps=caps(max_weight_per_instrument=0.5)),
        (vault(),),
        id="max_weight_per_instrument",
    ),
    pytest.param(
        allocation((("vault-a", 0.2, 20), ("vault-b", 0.3, 30)), total_usd=50),
        policy(policy_caps=caps(max_weight_per_protocol=0.5)),
        (vault(), vault(instrument_id="vault-b")),
        id="max_weight_per_protocol",
    ),
    pytest.param(
        allocation((("vault-a", 0.2, 20), ("vault-b", 0.3, 30)), total_usd=50),
        policy(policy_caps=caps(max_weight_per_curator=0.5)),
        (vault(), vault(instrument_id="vault-b")),
        id="max_weight_per_curator",
    ),
    pytest.param(
        allocation((("vault-a", 0.2, 20), ("vault-b", 0.3, 30)), total_usd=50),
        policy(policy_caps=caps(max_weight_per_chain=0.5)),
        (vault(), vault(instrument_id="vault-b")),
        id="max_weight_per_chain",
    ),
    pytest.param(
        allocation(),
        policy(policy_caps=caps(min_instrument_tvl_usd=10_000_000)),
        (vault(),),
        id="min_instrument_tvl_usd",
    ),
    pytest.param(
        allocation(),
        policy(policy_caps=caps(max_reward_dependence=0.1)),
        (vault(),),
        id="max_reward_dependence",
    ),
    pytest.param(
        allocation(),
        policy(policy_gates=gates(new_instrument_needs_approval=True)),
        (vault(),),
        id="new_instrument_needs_approval",
    ),
    pytest.param(
        allocation(total_usd=100),
        policy(policy_gates=gates(max_deploy_per_cycle_usd=100)),
        (vault(),),
        id="max_deploy_per_cycle_usd",
    ),
    pytest.param(
        allocation(metadata={"execution_mode": "autonomous"}),
        policy(policy_gates=gates(autonomous_rebalance=True)),
        (vault(),),
        id="autonomous_rebalance",
    ),
]


@pytest.mark.parametrize(
    ("target", "target_policy", "known_instruments"),
    PASSING_CASES,
)
def test_each_policy_rule_has_a_passing_fixture(
    target: Allocation,
    target_policy: Policy,
    known_instruments: tuple[Vault, ...],
) -> None:
    result = check(target, target_policy, known_instruments)

    assert result.ok is True
    assert result.violations == ()


FAILING_CASES = [
    pytest.param(
        allocation(),
        policy(allowed=PolicyAllowed(protocols=("aave",))),
        (vault(),),
        "allowed_protocols",
        "vault-a",
        ("aave",),
        "morpho",
        id="allowed_protocols",
    ),
    pytest.param(
        allocation(),
        policy(allowed=PolicyAllowed(chains=(1,))),
        (vault(),),
        "allowed_chains",
        "vault-a",
        (1,),
        8453,
        id="allowed_chains",
    ),
    pytest.param(
        allocation(),
        policy(allowed=PolicyAllowed(assets=("DAI",))),
        (vault(),),
        "allowed_assets",
        "vault-a",
        ("DAI",),
        "USDC",
        id="allowed_assets",
    ),
    pytest.param(
        allocation(),
        policy(allowed=PolicyAllowed(curators=("curator-b",))),
        (vault(),),
        "allowed_curators",
        "vault-a",
        ("curator-b",),
        "curator-a",
        id="allowed_curators",
    ),
    pytest.param(
        allocation((("vault-a", 0.6, 60),), total_usd=60),
        policy(policy_caps=caps(max_weight_per_instrument=0.5)),
        (vault(),),
        "max_weight_per_instrument",
        "vault-a",
        0.5,
        0.6,
        id="max_weight_per_instrument",
    ),
    pytest.param(
        allocation((("vault-a", 0.3, 30), ("vault-b", 0.3, 30)), total_usd=60),
        policy(policy_caps=caps(max_weight_per_protocol=0.5)),
        (vault(), vault(instrument_id="vault-b")),
        "max_weight_per_protocol",
        "morpho",
        0.5,
        0.6,
        id="max_weight_per_protocol",
    ),
    pytest.param(
        allocation((("vault-a", 0.3, 30), ("vault-b", 0.3, 30)), total_usd=60),
        policy(policy_caps=caps(max_weight_per_curator=0.5)),
        (vault(), vault(instrument_id="vault-b")),
        "max_weight_per_curator",
        "curator-a",
        0.5,
        0.6,
        id="max_weight_per_curator",
    ),
    pytest.param(
        allocation((("vault-a", 0.3, 30), ("vault-b", 0.3, 30)), total_usd=60),
        policy(policy_caps=caps(max_weight_per_chain=0.5)),
        (vault(), vault(instrument_id="vault-b")),
        "max_weight_per_chain",
        "8453",
        0.5,
        0.6,
        id="max_weight_per_chain",
    ),
    pytest.param(
        allocation(),
        policy(policy_caps=caps(min_instrument_tvl_usd=10_000_001)),
        (vault(),),
        "min_instrument_tvl_usd",
        "vault-a",
        10_000_001,
        10_000_000,
        id="min_instrument_tvl_usd",
    ),
    pytest.param(
        allocation(),
        policy(policy_caps=caps(max_reward_dependence=0.05)),
        (vault(),),
        "max_reward_dependence",
        "vault-a",
        0.05,
        0.1,
        id="max_reward_dependence",
    ),
    pytest.param(
        allocation((("new-vault", 1, 100),)),
        policy(policy_gates=gates(new_instrument_needs_approval=True)),
        (),
        "new_instrument_needs_approval",
        "new-vault",
        "approved instrument",
        "unseen instrument",
        id="new_instrument_needs_approval",
    ),
    pytest.param(
        allocation(total_usd=101),
        policy(policy_gates=gates(max_deploy_per_cycle_usd=100)),
        (vault(),),
        "max_deploy_per_cycle_usd",
        "allocation",
        100,
        101,
        id="max_deploy_per_cycle_usd",
    ),
    pytest.param(
        allocation(metadata={"execution_mode": "unattended"}),
        policy(policy_gates=gates(autonomous_rebalance=False)),
        (vault(),),
        "autonomous_rebalance",
        "allocation",
        False,
        "unattended",
        id="autonomous_rebalance",
    ),
]


@pytest.mark.parametrize(
    (
        "target",
        "target_policy",
        "known_instruments",
        "rule",
        "entity",
        "limit",
        "actual",
    ),
    FAILING_CASES,
)
def test_each_policy_rule_has_an_actionable_failing_fixture(
    target: Allocation,
    target_policy: Policy,
    known_instruments: tuple[Vault, ...],
    rule: str,
    entity: str,
    limit: object,
    actual: object,
) -> None:
    result = check(target, target_policy, known_instruments)
    item = violation(result, rule)

    assert result.ok is False
    assert item.entity == entity
    assert item.limit == limit
    assert item.actual == actual


def test_null_allowlists_mean_all_and_pass_through() -> None:
    result = check(
        allocation(),
        policy(
            allowed=PolicyAllowed(
                protocols=None,
                chains=None,
                assets=None,
                curators=None,
            )
        ),
        (vault(protocol="new-protocol", chain_id=999_999, asset="NEW", curator="new"),),
    )

    assert result.ok is True


def test_explicit_allowlists_block_values_outside_them() -> None:
    result = check(
        allocation(),
        policy(allowed=PolicyAllowed(protocols=("aave",), assets=("DAI",))),
        (vault(),),
    )

    assert result.ok is False
    assert {item.rule for item in result.violations} == {
        "allowed_assets",
        "allowed_protocols",
    }


def test_unseen_instrument_is_flagged_for_approval() -> None:
    result = check(
        allocation((("not-approved-yet", 1, 100),)),
        policy(policy_gates=gates(new_instrument_needs_approval=True)),
        (vault(),),
    )
    item = violation(result, "new_instrument_needs_approval")

    assert result.ok is False
    assert item.entity == "not-approved-yet"
    assert item.limit == "approved instrument"
    assert item.actual == "unseen instrument"


def test_deploy_cycle_cap_blocks_oversized_allocation() -> None:
    result = check(
        allocation(total_usd=25_000.01),
        policy(policy_gates=gates(max_deploy_per_cycle_usd=25_000)),
        (vault(),),
    )
    item = violation(result, "max_deploy_per_cycle_usd")

    assert result.ok is False
    assert item.limit == 25_000
    assert item.actual == 25_000.01


@pytest.mark.parametrize(
    "metadata",
    [
        {"execution_mode": "autonomous"},
        {"mode": "unattended"},
        {"autonomous": True},
        {"unattended": True},
    ],
)
def test_autonomous_rebalance_false_blocks_unattended_or_autonomous_mode(
    metadata: dict[str, object],
) -> None:
    result = check(
        allocation(metadata=metadata),
        policy(policy_gates=gates(autonomous_rebalance=False)),
        (vault(),),
    )

    assert result.ok is False
    assert violation(result, "autonomous_rebalance").entity == "allocation"


@pytest.mark.parametrize(
    ("cap_name", "target", "known_instruments"),
    [
        pytest.param(
            "max_weight_per_instrument",
            allocation((("vault-a", 0.51, 51),), total_usd=51),
            (vault(),),
            id="instrument",
        ),
        pytest.param(
            "max_weight_per_protocol",
            allocation((("vault-a", 0.26, 26), ("vault-b", 0.25, 25)), total_usd=51),
            (vault(), vault(instrument_id="vault-b")),
            id="protocol",
        ),
        pytest.param(
            "max_weight_per_curator",
            allocation((("vault-a", 0.26, 26), ("vault-b", 0.25, 25)), total_usd=51),
            (vault(), vault(instrument_id="vault-b")),
            id="curator",
        ),
        pytest.param(
            "max_weight_per_chain",
            allocation((("vault-a", 0.26, 26), ("vault-b", 0.25, 25)), total_usd=51),
            (vault(), vault(instrument_id="vault-b")),
            id="chain",
        ),
    ],
)
def test_no_allocation_exceeding_any_weight_cap_returns_ok_true(
    cap_name: str,
    target: Allocation,
    known_instruments: tuple[Vault, ...],
) -> None:
    result = check(
        target,
        policy(policy_caps=caps(**{cap_name: 0.5})),
        known_instruments,
    )

    assert result.ok is False
    assert violation(result, cap_name).limit == 0.5


def test_execution_can_abort_on_policy_result_without_override_api() -> None:
    result = check(
        allocation((("vault-a", 0.6, 60),), total_usd=60),
        policy(policy_caps=caps(max_weight_per_instrument=0.5)),
        (vault(),),
    )

    assert result.ok is False
    assert result.violations
    assert result.model_dump(mode="json") == {
        "ok": False,
        "violations": [
            {
                "rule": "max_weight_per_instrument",
                "entity": "vault-a",
                "limit": 0.5,
                "actual": 0.6,
            }
        ],
    }


def test_policy_result_rejects_ok_true_with_violations() -> None:
    failing = check(
        allocation((("vault-a", 0.6, 60),), total_usd=60),
        policy(policy_caps=caps(max_weight_per_instrument=0.5)),
        (vault(),),
    )

    with pytest.raises(ValueError, match="ok must be true only"):
        PolicyResult(ok=True, violations=failing.violations)
