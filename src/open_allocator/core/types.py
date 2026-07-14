from __future__ import annotations

import math
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


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Vault(FrozenModel):
    instrument_id: str
    protocol: str
    chain_id: int
    asset: str
    asset_category: str | None = None
    is_stablecoin: bool | None = None
    apy: float
    tvl_usd: float = Field(ge=0)
    apy_series: tuple[float, ...] = ()
    tvl_usd_series: tuple[float, ...] = ()
    curator: TextRiskValue = Unknown
    reward_dependence: NumericRiskValue = Unknown
    oracle: TextRiskValue = Unknown
    fee: NumericRiskValue = Unknown
    apy_stability: NumericRiskValue = Unknown
    market_concentration: NumericRiskValue = Unknown
    liquidity: NumericRiskValue = Unknown
    collateral_mix: JsonRiskValue = Unknown


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

        reconstructed = sum(
            factor.normalized_value * factor.weight
            for factor in known_factors
            if factor.normalized_value is not None
        ) / total_weight
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


class TxStep(FrozenModel):
    to: str
    data: str
    value: int = Field(ge=0)
    chain_id: int
    kind: Literal["approve", "buy", "sell"]


class TxPlan(FrozenModel):
    steps: tuple[TxStep, ...]
    summary: str


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
