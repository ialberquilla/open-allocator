from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
from datetime import date
from enum import StrEnum
from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, model_validator


class UnknownValue(StrEnum):
    UNKNOWN = "Unknown"


Unknown = UnknownValue.UNKNOWN

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list[JsonScalar] | dict[str, JsonScalar]
TextRiskValue: TypeAlias = UnknownValue | str | None
NumericRiskValue: TypeAlias = UnknownValue | float | None
JsonRiskValue: TypeAlias = UnknownValue | JsonValue


def curator_bucket(instrument_id: str, curator: TextRiskValue) -> str:
    """Cap bucket key for a vault's curator.

    An undisclosed curator is not evidence that instruments share one, so each
    unknown gets a unique bucket and never collectively trips the curator cap.
    Shared by the allocator (when clamping) and the policy checker (when
    validating) so the two can never disagree about what "same curator" means.
    """
    if curator is None or curator == Unknown:
        return f"__unknown_curator__:{instrument_id}"
    return str(curator)


UNKNOWN_SECTOR = "__unknown_sector__"


def sector_bucket(sector: str | None) -> str:
    """Cap bucket key for a vault's sector (yield source).

    Deliberately the mirror image of :func:`curator_bucket`. An undisclosed
    curator is not evidence that two instruments share one, so each unknown
    gets its own bucket. An *unclassified sector* is the opposite: it is an
    unmeasured concentration, and a diversification cap must not read silence
    as diversity. So every unknown lands in one shared bucket and they trip the
    sector cap collectively — the dimension fails closed, loudly, instead of
    quietly becoming a no-op when the upstream field is missing.
    """
    if sector is None or sector == Unknown or sector == "":
        return UNKNOWN_SECTOR
    return str(sector)


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Vault(FrozenModel):
    instrument_id: str
    protocol: str
    chain_id: int
    asset: str
    # Underlying (deposit/withdraw) token and yield token, as discovered from
    # 1Tx. Execution converts amounts to raw units with these decimals, so they
    # are never defaulted: None = upstream did not say, and a raw-amount request
    # that needs one must fail closed.
    token_address: str | None = None
    token_decimals: int | None = Field(default=None, ge=0)
    yield_token_address: str | None = None
    yield_token_decimals: int | None = Field(default=None, ge=0)
    asset_category: str | None = None
    # Yield source ("how this pays"), sourced from 1Tx discovery — never
    # hardcoded here, per the Dynamic Universe Rule. None = upstream has not
    # classified it; see sector_bucket for what that costs.
    sector: str | None = None
    is_stablecoin: bool | None = None
    # Advertised/headline APY. Keep this meaning until APY-basis migration is
    # explicit so additive split data cannot silently change allocations.
    apy: float
    tvl_usd: float = Field(ge=0)
    apy_base: float | None = None
    apy_reward: float | None = None
    reward_tokens: tuple[str, ...] = ()
    apy_series: tuple[float, ...] = ()
    tvl_usd_series: tuple[float, ...] = ()
    # The same APY history, resampled to one observation per UTC date and
    # keeping the date. ``apy_series`` is raw feed observations at whatever
    # cadence upstream wrote them, and that cadence differs between instruments
    # — so position k of two series is not the same moment in time. Anything
    # comparing instruments to each other (see
    # :mod:`open_allocator.core.diversify`) must align on dates and use this;
    # single-instrument path metrics may keep using ``apy_series``.
    apy_daily: tuple[tuple[date, float], ...] = ()
    curator: TextRiskValue = Unknown
    reward_dependence: NumericRiskValue = Unknown
    oracle: TextRiskValue = Unknown
    fee: NumericRiskValue = Unknown
    apy_stability: NumericRiskValue = Unknown
    market_concentration: NumericRiskValue = Unknown
    liquidity: NumericRiskValue = Unknown
    collateral_mix: JsonRiskValue = Unknown

    @property
    def accruing_apy(self) -> float | None:
        """Yield known to accrue into the yield-token share price."""
        return self.apy_base


class FactorScore(FrozenModel):
    raw_input: JsonRiskValue
    normalized_value: float | None = Field(ge=0, le=1)
    weight: float = Field(ge=0)
    unknown: bool = False

    @model_validator(mode="after")
    def _known_factors_have_normalized_values(self) -> "FactorScore":
        if not self.unknown and self.normalized_value is None:
            raise ValueError("known factors require normalized_value")
        return self


class VaultScore(FrozenModel):
    instrument_id: str
    score: float = Field(ge=0, le=1)
    factors: dict[str, FactorScore]

    @model_validator(mode="after")
    def _score_is_reconstructable_from_known_factors(self) -> "VaultScore":
        known_factors = [
            factor for factor in self.factors.values() if not factor.unknown
        ]
        total_weight = sum(factor.weight for factor in known_factors)
        if total_weight == 0:
            if self.score == 0:
                return self
            raise ValueError("all-unknown factors require a zero score")

        reconstructed = (
            sum(
                factor.normalized_value * factor.weight
                for factor in known_factors
                if factor.normalized_value is not None
            )
            / total_weight
        )
        if not math.isclose(self.score, reconstructed, rel_tol=1e-9, abs_tol=1e-9):
            raise ValueError("score is not reconstructable from known factors")
        return self


class AllocationLeg(FrozenModel):
    instrument_id: str
    weight: float = Field(ge=0, le=1)
    usd: float = Field(ge=0)


class Allocation(FrozenModel):
    legs: tuple[AllocationLeg, ...]
    total_usd: float = Field(ge=0)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


# The backend's calldata call types are kept as returned, never collapsed:
# `approve`, `swap`, `deposit`, `withdraw`, `bridge_burn`, `fee`, plus the
# client-composed `cctp_receive`. `buy`/`sell` exist only for plans built through
# the legacy /transactions endpoints while ONE_TX_TRANSACTION_API allows them.
TxStepKind: TypeAlias = Literal[
    "approve",
    "swap",
    "deposit",
    "withdraw",
    "bridge_burn",
    "cctp_receive",
    "fee",
    "buy",
    "sell",
]
LEGACY_STEP_KINDS: frozenset[str] = frozenset({"buy", "sell"})


class TxStep(FrozenModel):
    to: str
    data: str
    value: int = Field(ge=0)
    chain_id: int
    kind: TxStepKind


class BundleToken(FrozenModel):
    address: str
    symbol: str | None = None
    decimals: int = Field(ge=0)


class BundleRequirement(FrozenModel):
    """A raw token balance the bundle's simulation assumed the account holds."""

    token: str
    amount: str = Field(pattern=r"^\d+$")


class BundleLeftover(FrozenModel):
    """The most of a token the bundle may leave behind in the account."""

    token: str
    max_amount: str = Field(pattern=r"^\d+$")


class TxBundle(FrozenModel):
    """One atomic calldata bundle as it was quoted, bound to its plan steps.

    The steps it names are the bundle's calls, in order, and must be submitted
    together. ``protocol_gas`` is 1Tx's wallet-neutral simulation of those bare
    calls; it is not the Safe/UserOperation gas, which is estimated separately
    and never reported as the same measurement.
    """

    # Stable across rebuilds of the same logical leg; ``digest`` changes with
    # the calls, so completion recorded for one digest never carries over.
    bundle_id: str = Field(min_length=1)
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    leg_index: int = Field(ge=0)
    instrument_id: str = Field(min_length=1)
    action: Literal["deposit", "withdraw"]
    account: str
    chain_id: int = Field(ge=1)
    step_indexes: tuple[int, ...] = Field(min_length=1)
    source: Literal["1tx-calldata"] = "1tx-calldata"
    endpoint: str = Field(min_length=1)
    # Raw units of ``token_in``, or ``max`` for a full withdrawal.
    amount: str = Field(pattern=r"^(?:max|[1-9]\d*)$")
    token_in: BundleToken
    token_out: BundleToken
    quote_block: int = Field(ge=0)
    # Unix seconds; None when the bundle embeds no expiring quote.
    expires_at: int | None = Field(default=None, ge=0)
    requires: tuple[BundleRequirement, ...] = ()
    leftovers: tuple[BundleLeftover, ...] = ()
    # Raw units of ``token_out``.
    expected_out: str | None = Field(default=None, pattern=r"^\d+$")
    min_out: str | None = Field(default=None, pattern=r"^\d+$")
    protocol_gas: str = Field(pattern=r"^\d+$")
    simulation_scope: Literal["protocol_bundle"] = "protocol_bundle"
    simulation_engine: Literal["wallet_neutral_atomic"] = "wallet_neutral_atomic"


def bundle_digest(
    *,
    instrument_id: str,
    action: str,
    account: str,
    chain_id: int,
    amount: str,
    steps: Sequence[TxStep],
    quote_block: int,
    expires_at: int | None,
) -> str:
    """SHA-256 over what a bundle commits the account to, in call order.

    Covers the instrument, action, account, chain, amount, every call's target,
    data, value, and type, and the quote block and expiry. Addresses and hex are
    case-folded so a re-encoded but identical bundle keeps its digest.
    """
    payload = {
        "instrument_id": instrument_id.casefold(),
        "action": action,
        "account": account.casefold(),
        "chain_id": chain_id,
        "amount": amount,
        "calls": [
            {
                "to": step.to.casefold(),
                "data": step.data.casefold(),
                "value": str(step.value),
                "type": step.kind,
            }
            for step in steps
        ],
        "quote_block": quote_block,
        "expires_at": expires_at,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class TxPlan(FrozenModel):
    steps: tuple[TxStep, ...]
    summary: str
    # Empty for legacy plans, so stored ones stay readable.
    bundles: tuple[TxBundle, ...] = ()

    @model_validator(mode="after")
    def _bundles_bind_their_steps(self) -> "TxPlan":
        claimed: set[int] = set()
        for bundle in self.bundles:
            indexes = bundle.step_indexes
            first = indexes[0]
            # Contiguous and ascending: a bundle is one ordered atomic batch, so
            # nothing may be interleaved with or reordered inside it.
            if indexes != tuple(range(first, first + len(indexes))):
                raise ValueError(
                    f"bundle {bundle.bundle_id} step indexes must be contiguous "
                    f"and ascending, got {list(indexes)}"
                )
            if first < 0 or indexes[-1] >= len(self.steps):
                raise ValueError(
                    f"bundle {bundle.bundle_id} references steps outside the plan"
                )
            if claimed.intersection(indexes):
                raise ValueError(
                    f"bundle {bundle.bundle_id} shares steps with another bundle"
                )
            claimed.update(indexes)

            steps = [self.steps[index] for index in indexes]
            for index, step in zip(indexes, steps, strict=True):
                if step.chain_id != bundle.chain_id:
                    raise ValueError(
                        f"step {index} is on chain {step.chain_id}, bundle "
                        f"{bundle.bundle_id} is on chain {bundle.chain_id}"
                    )
                if step.kind in LEGACY_STEP_KINDS:
                    raise ValueError(
                        f"step {index} of bundle {bundle.bundle_id} has legacy "
                        f"kind {step.kind!r}"
                    )
            digest = bundle_digest(
                instrument_id=bundle.instrument_id,
                action=bundle.action,
                account=bundle.account,
                chain_id=bundle.chain_id,
                amount=bundle.amount,
                steps=steps,
                quote_block=bundle.quote_block,
                expires_at=bundle.expires_at,
            )
            if digest != bundle.digest:
                raise ValueError(
                    f"bundle {bundle.bundle_id} digest does not match its steps"
                )
        return self


class PolicyWallet(FrozenModel):
    mode: str
    signer: Literal["local-eoa", "remote", "safe", "erc4337-paymaster"]


class PolicyAllowed(FrozenModel):
    protocols: tuple[str, ...] | None = None
    chains: tuple[int, ...] | None = None
    asset_categories: tuple[str, ...] | None = None
    stablecoin_only: bool | None = None
    assets: tuple[str, ...] | None = None
    curators: tuple[str, ...] | None = None


class PolicyCaps(FrozenModel):
    max_weight_per_instrument: float = Field(ge=0, le=1)
    max_weight_per_protocol: float = Field(ge=0, le=1)
    max_weight_per_curator: float = Field(ge=0, le=1)
    max_weight_per_chain: float = Field(ge=0, le=1)
    # Optional so existing policy files keep loading. Absent = the sector
    # dimension is not enforced (the allocator treats it as 1.0); it is still
    # always *reported*, so an unset cap cannot hide a monoculture.
    max_weight_per_sector: float | None = Field(default=None, ge=0, le=1)
    # Floor, not a ceiling — the only cap here shaped that way, because
    # diversification is a property of the whole allocation rather than of any
    # one bucket. Measured from the instruments' own APY history (see
    # `core.diversify`), so unlike the weight caps above it cannot be satisfied
    # by spreading capital across labels that move together. Optional; absent =
    # not enforced, and it is reported either way.
    min_effective_positions: float | None = Field(default=None, ge=0)
    min_instrument_tvl_usd: float = Field(ge=0)
    max_reward_dependence: float = Field(ge=0, le=1)


class PolicyGates(FrozenModel):
    new_instrument_needs_approval: bool
    autonomous_rebalance: bool
    max_deploy_per_cycle_usd: float = Field(ge=0)


class Policy(FrozenModel):
    version: int = 1
    wallet: PolicyWallet
    allowed: PolicyAllowed
    caps: PolicyCaps
    gates: PolicyGates
