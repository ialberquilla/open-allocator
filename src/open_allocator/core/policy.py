from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from typing import TypeAlias

from pydantic import Field, model_validator

from open_allocator.core import eligibility
from open_allocator.core.types import (
    Allocation,
    FrozenModel,
    Policy,
    Vault,
    curator_bucket,
)

PolicyScalar: TypeAlias = str | int | float | bool | None
PolicyValue: TypeAlias = PolicyScalar | tuple[PolicyScalar, ...]

_EPSILON = 1e-9


class PolicyViolation(FrozenModel):
    rule: str
    entity: str
    limit: PolicyValue
    actual: PolicyValue

    @property
    def offending_entity(self) -> str:
        return self.entity


class PolicyResult(FrozenModel):
    ok: bool
    violations: tuple[PolicyViolation, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def _ok_matches_violations(self) -> "PolicyResult":
        if self.ok == bool(self.violations):
            raise ValueError("ok must be true only when there are no violations")
        return self


def check(
    allocation: Allocation | Mapping[str, object],
    policy: Policy | Mapping[str, object],
    known_instruments: Iterable[Vault | Mapping[str, object]],
) -> PolicyResult:
    allocation_model = _allocation(allocation)
    policy_model = _policy(policy)
    vault_by_id = _vault_by_id(known_instruments)
    violations: list[PolicyViolation] = []

    missing_instruments = tuple(
        sorted(
            leg.instrument_id
            for leg in allocation_model.legs
            if leg.instrument_id not in vault_by_id
        )
    )
    if policy_model.gates.new_instrument_needs_approval:
        for instrument_id in missing_instruments:
            violations.append(
                _violation(
                    "new_instrument_needs_approval",
                    instrument_id,
                    "approved instrument",
                    "unseen instrument",
                )
            )

    allocated_vaults = {
        leg.instrument_id: vault_by_id[leg.instrument_id]
        for leg in allocation_model.legs
        if leg.instrument_id in vault_by_id
    }
    _check_allowlists(allocation_model, policy_model, allocated_vaults, violations)
    _check_caps(allocation_model, policy_model, allocated_vaults, violations)
    _check_quality_caps(allocated_vaults, policy_model, violations)
    _check_gates(allocation_model, policy_model, violations)

    return PolicyResult(ok=not violations, violations=tuple(violations))


def _allocation(allocation: Allocation | Mapping[str, object]) -> Allocation:
    if isinstance(allocation, Allocation):
        return allocation
    return Allocation.model_validate(allocation)


def _policy(policy: Policy | Mapping[str, object]) -> Policy:
    if isinstance(policy, Policy):
        return policy
    return Policy.model_validate(policy)


def _vault_by_id(
    known_instruments: Iterable[Vault | Mapping[str, object]],
) -> dict[str, Vault]:
    vaults: dict[str, Vault] = {}
    for instrument in known_instruments:
        vault = (
            instrument
            if isinstance(instrument, Vault)
            else Vault.model_validate(instrument)
        )
        vaults[vault.instrument_id] = vault
    return vaults


def _check_allowlists(
    allocation: Allocation,
    policy: Policy,
    vault_by_id: Mapping[str, Vault],
    violations: list[PolicyViolation],
) -> None:
    for leg in allocation.legs:
        vault = vault_by_id.get(leg.instrument_id)
        if vault is None:
            continue
        for finding in eligibility.allowlist_findings(vault, policy.allowed):
            violations.append(
                _violation(
                    finding.rule, leg.instrument_id, finding.limit, finding.actual
                )
            )


def _check_caps(
    allocation: Allocation,
    policy: Policy,
    vault_by_id: Mapping[str, Vault],
    violations: list[PolicyViolation],
) -> None:
    instrument_weights: defaultdict[str, float] = defaultdict(float)
    protocol_weights: defaultdict[str, float] = defaultdict(float)
    curator_weights: defaultdict[str, float] = defaultdict(float)
    chain_weights: defaultdict[int, float] = defaultdict(float)

    for leg in allocation.legs:
        instrument_weights[leg.instrument_id] += leg.weight
        vault = vault_by_id.get(leg.instrument_id)
        if vault is None:
            continue
        protocol_weights[vault.protocol] += leg.weight
        curator_weights[curator_bucket(vault.instrument_id, vault.curator)] += (
            leg.weight
        )
        chain_weights[vault.chain_id] += leg.weight

    caps = policy.caps
    _check_weight_cap(
        "max_weight_per_instrument",
        caps.max_weight_per_instrument,
        instrument_weights,
        violations,
    )
    _check_weight_cap(
        "max_weight_per_protocol",
        caps.max_weight_per_protocol,
        protocol_weights,
        violations,
    )
    _check_weight_cap(
        "max_weight_per_curator",
        caps.max_weight_per_curator,
        curator_weights,
        violations,
    )
    _check_weight_cap(
        "max_weight_per_chain",
        caps.max_weight_per_chain,
        chain_weights,
        violations,
    )


def _check_weight_cap(
    rule: str,
    limit: float,
    weights: Mapping[object, float],
    violations: list[PolicyViolation],
) -> None:
    for entity, actual in sorted(weights.items(), key=lambda item: str(item[0])):
        if actual > limit + _EPSILON:
            violations.append(_violation(rule, str(entity), limit, actual))


def _check_quality_caps(
    vault_by_id: Mapping[str, Vault],
    policy: Policy,
    violations: list[PolicyViolation],
) -> None:
    for instrument_id, vault in sorted(vault_by_id.items()):
        for finding in eligibility.quality_findings(vault, policy.caps):
            violations.append(
                _violation(finding.rule, instrument_id, finding.limit, finding.actual)
            )


def _check_gates(
    allocation: Allocation,
    policy: Policy,
    violations: list[PolicyViolation],
) -> None:
    if allocation.total_usd > policy.gates.max_deploy_per_cycle_usd + _EPSILON:
        violations.append(
            _violation(
                "max_deploy_per_cycle_usd",
                "allocation",
                policy.gates.max_deploy_per_cycle_usd,
                allocation.total_usd,
            )
        )

    mode = _autonomous_mode(allocation.metadata)
    if mode is not None and not policy.gates.autonomous_rebalance:
        violations.append(
            _violation(
                "autonomous_rebalance",
                "allocation",
                policy.gates.autonomous_rebalance,
                mode,
            )
        )


def _autonomous_mode(metadata: Mapping[str, object]) -> str | bool | None:
    for key in ("autonomous", "unattended"):
        if metadata.get(key) is True:
            return True

    for key in ("execution_mode", "mode"):
        value = metadata.get(key)
        if isinstance(value, str) and value.casefold() in {"autonomous", "unattended"}:
            return value

    return None


def _violation(
    rule: str,
    entity: str,
    limit: PolicyValue,
    actual: PolicyValue,
) -> PolicyViolation:
    return PolicyViolation(rule=rule, entity=entity, limit=limit, actual=actual)


__all__ = ["PolicyResult", "PolicyViolation", "check"]
