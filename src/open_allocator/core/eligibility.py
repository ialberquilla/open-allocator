"""Canonical per-vault policy eligibility — the single source of truth.

Historically the same allowlist/cap logic was copy-pasted across four sites
(discovery narrowing, allocation-violation checking, and CLI candidate
narrowing), so adding an allowed-* axis meant editing all of them and it was
easy to miss one. Here each axis is **one predicate**; consumers compose the
subset they need:

- ``discovery_findings`` — coarse pre-filter (allowlists minus curator, plus the
  TVL floor). Used by :func:`open_allocator.core.universe.discover`.
- ``candidate_findings`` — full per-vault gate (all allowlists + quality caps).
  Used by the CLI to narrow candidates and by
  :func:`open_allocator.core.policy.check` to raise per-leg violations.

Adding a new axis = add one predicate and list it in whichever compositions
should apply it. This module owns *only* per-vault rules; allocation-level
weight/concentration caps and gates stay in :mod:`open_allocator.core.policy`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeAlias

from open_allocator.core.types import (
    NumericRiskValue,
    Policy,
    PolicyAllowed,
    PolicyCaps,
    TextRiskValue,
    Unknown,
    Vault,
)

PolicyScalar: TypeAlias = str | int | float | bool | None
PolicyValue: TypeAlias = PolicyScalar | tuple[PolicyScalar, ...]

_EPSILON = 1e-9


@dataclass(frozen=True)
class Finding:
    rule: str
    limit: PolicyValue
    actual: PolicyValue


def policy_value(value: object) -> PolicyScalar:
    """Normalize a risk value to a JSON scalar (``Unknown`` -> ``"Unknown"``)."""
    if value == Unknown:
        return "Unknown"
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return str(value)


def policy_number(value: object) -> float | None:
    """Numeric view of a risk value, or ``None`` when it is missing/non-numeric."""
    scalar = policy_value(value)
    if not isinstance(scalar, int | float) or isinstance(scalar, bool):
        return None
    return float(scalar)


# --- per-axis predicates (the single source of truth per rule) -------------


def _protocol(vault: Vault, allowed: PolicyAllowed) -> Finding | None:
    return _allowlist("allowed_protocols", allowed.protocols, vault.protocol)


def _chain(vault: Vault, allowed: PolicyAllowed) -> Finding | None:
    return _allowlist("allowed_chains", allowed.chains, vault.chain_id)


def _asset(vault: Vault, allowed: PolicyAllowed) -> Finding | None:
    return _allowlist("allowed_assets", allowed.assets, vault.asset)


def _asset_category(vault: Vault, allowed: PolicyAllowed) -> Finding | None:
    return _allowlist(
        "allowed_asset_categories",
        allowed.asset_categories,
        vault.asset_category,
    )


def _stablecoin(vault: Vault, allowed: PolicyAllowed) -> Finding | None:
    if allowed.stablecoin_only and vault.is_stablecoin is not True:
        return Finding("allowed_stablecoin_only", True, vault.is_stablecoin)
    return None


def _curator(vault: Vault, allowed: PolicyAllowed) -> Finding | None:
    return _allowlist(
        "allowed_curators",
        allowed.curators,
        policy_value(vault.curator),
    )


def _min_tvl(vault: Vault, caps: PolicyCaps) -> Finding | None:
    if vault.tvl_usd < caps.min_instrument_tvl_usd:
        return Finding(
            "min_instrument_tvl_usd", caps.min_instrument_tvl_usd, vault.tvl_usd
        )
    return None


def _max_reward_dependence(vault: Vault, caps: PolicyCaps) -> Finding | None:
    return _numeric_max(
        "max_reward_dependence",
        caps.max_reward_dependence,
        vault.reward_dependence,
    )


def _allowlist(
    rule: str,
    allowed: tuple[str, ...] | tuple[int, ...] | None,
    actual: str | int | float | bool | None,
) -> Finding | None:
    if allowed is None:
        return None
    if actual not in allowed:
        return Finding(rule, tuple(allowed), actual)
    return None


def _numeric_max(
    rule: str,
    limit: float,
    raw: NumericRiskValue | TextRiskValue,
) -> Finding | None:
    value = policy_number(raw)
    if value is None:
        return Finding(rule, limit, policy_value(raw))
    if value > limit + _EPSILON:
        return Finding(rule, limit, value)
    return None


_AllowlistPredicate = Callable[[Vault, PolicyAllowed], "Finding | None"]
_CapPredicate = Callable[[Vault, PolicyCaps], "Finding | None"]

# Composition lists — the only thing that differs between scopes.
_ALLOWLIST_AXES: tuple[_AllowlistPredicate, ...] = (
    _protocol,
    _chain,
    _asset,
    _asset_category,
    _stablecoin,
    _curator,
)
_QUALITY_AXES: tuple[_CapPredicate, ...] = (
    _min_tvl,
    _max_reward_dependence,
)
# Discovery is coarse: allowlists minus curator (undisclosed at discovery time),
# plus the TVL floor. No reward narrowing before scoring.
_DISCOVERY_ALLOWLIST_AXES: tuple[_AllowlistPredicate, ...] = (
    _protocol,
    _chain,
    _asset,
    _asset_category,
    _stablecoin,
)


# --- composed entry points -------------------------------------------------


def allowlist_findings(vault: Vault, allowed: PolicyAllowed) -> list[Finding]:
    return [
        finding
        for axis in _ALLOWLIST_AXES
        if (finding := axis(vault, allowed)) is not None
    ]


def quality_findings(vault: Vault, caps: PolicyCaps) -> list[Finding]:
    return [
        finding
        for axis in _QUALITY_AXES
        if (finding := axis(vault, caps)) is not None
    ]


def candidate_findings(vault: Vault, policy: Policy) -> list[Finding]:
    """Every per-vault policy exclusion: allowlists then quality caps."""
    return [
        *allowlist_findings(vault, policy.allowed),
        *quality_findings(vault, policy.caps),
    ]


def candidate_exclusion(vault: Vault, policy: Policy) -> str | None:
    """First per-vault exclusion rule, or ``None`` if the vault is eligible."""
    findings = candidate_findings(vault, policy)
    return findings[0].rule if findings else None


def discovery_findings(vault: Vault, policy: Policy) -> list[Finding]:
    """Coarse discovery-time exclusions (allowlists minus curator + TVL floor)."""
    findings = [
        finding
        for axis in _DISCOVERY_ALLOWLIST_AXES
        if (finding := axis(vault, policy.allowed)) is not None
    ]
    tvl_finding = _min_tvl(vault, policy.caps)
    if tvl_finding is not None:
        findings.append(tvl_finding)
    return findings


def discovery_eligible(vault: Vault, policy: Policy) -> bool:
    return not discovery_findings(vault, policy)


__all__ = [
    "Finding",
    "PolicyScalar",
    "PolicyValue",
    "allowlist_findings",
    "candidate_exclusion",
    "candidate_findings",
    "discovery_eligible",
    "discovery_findings",
    "policy_number",
    "policy_value",
    "quality_findings",
]
