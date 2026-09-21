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


# How a reward APY was priced by whoever reported it.
#
# ``traded`` means the reward token was valued at a quote something would
# actually fill. ``emission`` means it was valued at the emission schedule,
# which is what yield aggregators publish and which overstates a thin reward
# token by whatever the market discounts it. ``none`` means the row pays no
# reward at all, so there is nothing to price.
#
# ``None`` (the field absent) is *not* a fourth basis: it means upstream did not
# say, and it is treated exactly like ``emission`` — not traded. Reward pricing
# fails closed, because the optimistic direction is the one that floats
# reward-heavy rows to the top of a ranking.
RewardPriceBasis: TypeAlias = Literal["traded", "emission", "none", "unknown"]


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
    # Never defaulted: None means 1Tx did not report it, and raw-amount requests
    # that need it fail closed.
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
    # How ``apy_reward`` was priced upstream; see RewardPriceBasis. Never
    # defaulted to a basis, because absent and "we priced this at a traded
    # quote" are different claims.
    reward_price_basis: RewardPriceBasis | None = None
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
    # A levered (looped) row: ONE synthetic instrument whose net value is the
    # equity in it, while its gross exposure is up to ``max_leverage`` times
    # that. Everything upstream — weights, caps, drift, delivered yield — keeps
    # reading one instrument with one APY, which is what makes the design
    # affordable; the cost is that leverage is invisible to
    # ``max_weight_per_instrument``, so the row is required to *declare* its
    # ceiling for anything measuring gross exposure to read.
    is_levered: bool = False
    max_leverage: float | None = Field(default=None, ge=1)
    # EFFECTIVE liquidation threshold for the pair, when upstream published
    # one. Optional because discovery does not always carry it; absent, the
    # health factor is bounded from below by the
    # LTV that ``max_leverage`` implies, since a threshold is never below the
    # borrow ceiling. See :func:`levered_ltv_floor`.
    liquidation_threshold: float | None = Field(default=None, gt=0, le=1)
    # The borrowed asset. A loop whose debt is a different asset from its
    # collateral carries a price-ratio axis, and its health factor is then the
    # depeg budget. ``None`` = not reported, which is read as cross-asset.
    debt_asset: str | None = None
    # 24h traded volume of the thinnest reward stream the row pays. Capacity
    # on a reward-driven position binds on the reward token's exit liquidity.
    # ``None`` = unmeasured, which is never zero and never a pass.
    reward_liquidity_usd: float | None = Field(default=None, ge=0)
    # The contract a protocol call targets (an Aave pool, a vault). Discovered,
    # never assumed; it is how positions sharing one pool's account-wide state
    # are recognised.
    protocol_address: str | None = None
    # A levered row's two legs, as 1Tx instrument ids. A loop row's own
    # ``instrument_id`` is its loop id; these name the pair it was built from.
    collateral_instrument_id: str | None = None
    debt_instrument_id: str | None = None

    @model_validator(mode="after")
    def _levered_rows_declare_their_ceiling(self) -> "Vault":
        if self.is_levered and self.max_leverage is None:
            raise ValueError(
                f"{self.instrument_id}: a levered row must declare max_leverage"
            )
        for name in (
            "max_leverage",
            "liquidation_threshold",
            "debt_asset",
            "collateral_instrument_id",
            "debt_instrument_id",
        ):
            if not self.is_levered and getattr(self, name) is not None:
                raise ValueError(
                    f"{self.instrument_id}: {name} belongs to levered rows only"
                )
        floor = levered_ltv_floor(self)
        if (
            self.liquidation_threshold is not None
            and floor is not None
            and self.liquidation_threshold < floor - 1e-9
        ):
            raise ValueError(
                f"{self.instrument_id}: liquidation threshold "
                f"{self.liquidation_threshold} is below the ltv {floor:.6f} its "
                "max_leverage implies, which would liquidate at open"
            )
        return self

    @property
    def cross_asset(self) -> bool:
        """Whether a levered row borrows a different asset than it supplies.

        An unreported debt asset reads as cross-asset: the depeg budget is the
        axis that fails expensively, so it is assumed present until shown
        absent.
        """
        if not self.is_levered:
            return False
        if self.debt_asset is None:
            return True
        return self.debt_asset.casefold() != self.asset.casefold()

    @property
    def accruing_apy(self) -> float | None:
        """Yield known to accrue into the yield-token share price."""
        return self.apy_base


def levered_ltv_floor(vault: Vault) -> float | None:
    """The LTV a levered row's declared ``max_leverage`` implies.

    ``max_leverage = 1 / (1 - ltv)``, so ``ltv = 1 - 1 / max_leverage``. A
    liquidation threshold is never below the LTV, so this is a lower bound on
    the threshold and a health factor computed from it is a lower bound on
    the real one — the conservative stand-in when no threshold was published.
    ``None`` for an unlevered row, or one whose ceiling is 1 (it cannot borrow).
    """
    if not vault.is_levered or vault.max_leverage is None or vault.max_leverage <= 1:
        return None
    return 1.0 - 1.0 / vault.max_leverage


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
    # The leverage a levered leg is held at. ``weight`` and ``usd`` stay the
    # equity, so every existing sum still reads net value; this is what the
    # gross-exposure caps multiply it by. ``None`` on a levered leg is charged
    # at the row's declared ``max_leverage`` — an unchosen L is whatever the
    # venue allows, not 1.
    leverage: float | None = Field(default=None, ge=1)


class Allocation(FrozenModel):
    legs: tuple[AllocationLeg, ...]
    total_usd: float = Field(ge=0)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


# Calldata call types are kept as the backend returns them, plus the
# client-composed `cctp_receive`. `buy`/`sell` are legacy /transactions steps.
TxStepKind: TypeAlias = Literal[
    "approve",
    "swap",
    "deposit",
    "withdraw",
    "bridge_burn",
    "cctp_receive",
    "fee",
    # Levered loops. Supply stays ``deposit``, as 1Tx returns it.
    "borrow",
    "repay",
    "set_account_config",
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


class BundleBridge(FrozenModel):
    """The CCTP transfer a ``bridge`` bundle's burn commits to.

    The burn happens on the bundle's chain; the mint lands on ``to_chain_id``
    only after Circle attests the message, in a separate operation.
    """

    to_chain_id: int = Field(ge=1)
    source_domain: int = Field(ge=0)
    destination_domain: int = Field(ge=0)
    # Raw source USDC Circle may keep from the burned amount.
    max_fee: str = Field(pattern=r"^\d+$")
    # 1000 is CCTP Fast Transfer, 2000 is Standard.
    min_finality_threshold: Literal[1000, 2000]
    fast: bool


class BundleLoop(FrozenModel):
    """What a loop bundle commits the account to, and what 1Tx measured.

    The venue's effective pair parameters and the post-bundle position the
    simulator read back, next to the health factor open-allocator modelled
    for the same leverage. Bound into the bundle digest, so a rebuilt bundle
    that measures differently is a different bundle.
    """

    loop_id: str = Field(min_length=1)
    recipe: str = Field(min_length=1)
    pool: str
    collateral_instrument_id: str = Field(min_length=1)
    debt_instrument_id: str = Field(min_length=1)
    debt_token: BundleToken | None = None
    same_asset: bool
    # None for a close, which names no target.
    requested_leverage: float | None = Field(default=None, gt=1)
    ltv: float = Field(gt=0, lt=1)
    liquidation_threshold: float = Field(gt=0, le=1)
    params_basis: str
    oracle: str
    # Account-wide pool state the bundle changes (Aave's e-mode category).
    requires_account_config: bool
    account_config_current: int = Field(ge=0)
    account_config_target: int = Field(ge=0)
    # Measured by 1Tx after the batch ran, raw units of each leg's token.
    simulated_collateral: str = Field(pattern=r"^\d+$")
    simulated_debt: str = Field(pattern=r"^\d+$")
    simulated_leverage: float | None = None
    simulated_health_factor: float | None = None
    simulated_depeg_buffer_bps: int | None = None
    # Modelled here from ``requested_leverage`` and the effective threshold.
    modelled_health_factor: float | None = None
    modelled_depeg_buffer_bps: int | None = None
    turns: int = Field(ge=0)


BundleAction: TypeAlias = Literal[
    "deposit",
    "withdraw",
    "bridge",
    "cctp_receive",
    "loop_open",
    "loop_adjust",
    "loop_close",
]
LOOP_BUNDLE_ACTIONS: frozenset[str] = frozenset(
    {"loop_open", "loop_adjust", "loop_close"}
)


class TxBundle(FrozenModel):
    """One atomic calldata bundle as it was quoted, bound to its plan steps.

    The steps it names are the bundle's calls, in order, and must be submitted
    together. ``protocol_gas`` is 1Tx's wallet-neutral simulation of those bare
    calls; it is not the Safe/UserOperation gas, which is estimated separately
    and never reported as the same measurement.

    ``bridge`` bundles are 1Tx's CCTP source burn for a leg deposited on another
    chain. ``cctp_receive`` bundles are composed here, not by 1Tx: the attested
    redemption that funds a bridged leg's deposit in the same operation. They
    carry no protocol simulation.
    """

    # Stable across rebuilds of a leg; ``digest`` changes with the calls.
    bundle_id: str = Field(min_length=1)
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    leg_index: int = Field(ge=0)
    # For a bridge or receive bundle, the instrument the bridged leg deposits in.
    instrument_id: str = Field(min_length=1)
    action: BundleAction
    account: str
    chain_id: int = Field(ge=1)
    step_indexes: tuple[int, ...] = Field(min_length=1)
    source: Literal["1tx-calldata", "open-allocator"] = "1tx-calldata"
    endpoint: str = Field(min_length=1)
    # Raw units of ``token_in``, or ``max`` for a full withdrawal. A loop
    # adjust or close acts on the whole position the account holds on its
    # pair, and is ``max``.
    amount: str = Field(pattern=r"^(?:max|[1-9]\d*)$")
    token_in: BundleToken
    token_out: BundleToken
    # Zero for a composed bundle, which nothing quoted.
    quote_block: int = Field(ge=0)
    # Unix seconds; None when the bundle embeds no expiring quote.
    expires_at: int | None = Field(default=None, ge=0)
    requires: tuple[BundleRequirement, ...] = ()
    leftovers: tuple[BundleLeftover, ...] = ()
    # Raw units of ``token_out``.
    expected_out: str | None = Field(default=None, pattern=r"^\d+$")
    min_out: str | None = Field(default=None, pattern=r"^\d+$")
    # None only for a composed bundle, which 1Tx never simulated.
    protocol_gas: str | None = Field(pattern=r"^\d+$")
    # Raw ``token_out`` gained in the simulation, not settled value. None when
    # the stored plan lacks it.
    simulated_out: str | None = Field(default=None, pattern=r"^\d+$")
    simulation_scope: Literal["protocol_bundle"] | None = "protocol_bundle"
    simulation_engine: Literal["wallet_neutral_atomic"] | None = "wallet_neutral_atomic"
    bridge: BundleBridge | None = None
    loop: BundleLoop | None = None

    @model_validator(mode="after")
    def _source_matches_kind(self) -> "TxBundle":
        composed = self.action == "cctp_receive"
        if composed != (self.source == "open-allocator"):
            raise ValueError(
                f"bundle {self.bundle_id}: only cctp_receive bundles are composed "
                "by open-allocator"
            )
        simulated = (
            self.protocol_gas is not None
            and self.simulation_scope is not None
            and self.simulation_engine is not None
        )
        if composed == simulated:
            raise ValueError(
                f"bundle {self.bundle_id}: a 1Tx bundle carries its protocol "
                "simulation and a composed one carries none"
            )
        if (self.action == "bridge") != (self.bridge is not None):
            raise ValueError(
                f"bundle {self.bundle_id}: bridge metadata belongs to bridge "
                "bundles only"
            )
        if (self.action in LOOP_BUNDLE_ACTIONS) != (self.loop is not None):
            raise ValueError(
                f"bundle {self.bundle_id}: loop metadata belongs to loop bundles only"
            )
        return self

    @property
    def is_loop(self) -> bool:
        return self.loop is not None


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
    bridge: BundleBridge | None = None,
    loop: BundleLoop | None = None,
) -> str:
    """SHA-256 over what a bundle commits the account to, in call order.

    Covers the instrument, action, account, chain, amount, every call's target,
    data, value, and type, the quote block and expiry, a bridge bundle's CCTP
    transfer, and a loop bundle's pair parameters and measured position.
    Addresses and hex are case-folded so a re-encoded but identical bundle
    keeps its digest.
    """
    payload: dict[str, object] = {
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
    if bridge is not None:
        # Absent otherwise, so digests of deposit and withdraw bundles are the
        # ones earlier plans recorded.
        payload["bridge"] = bridge.model_dump(mode="json")
    if loop is not None:
        payload["loop"] = loop.model_dump(mode="json")
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
                # A redemption is composed here from an attested message; it
                # must never ride inside, or pass for, a bundle 1Tx built.
                if (step.kind == "cctp_receive") != (bundle.action == "cctp_receive"):
                    raise ValueError(
                        f"step {index} of {bundle.action} bundle "
                        f"{bundle.bundle_id} has kind {step.kind!r}"
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
                bridge=bundle.bridge,
                loop=bundle.loop,
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
    # --- levered rows -------------------------------------------------------
    # A levered row is ONE synthetic instrument whose weight is its equity, so
    # every cap above sees its net value and none sees its gross exposure.
    # These are the caps that do. All optional, and absent = not enforced,
    # like the sector cap: `validate-mandate` compares an absent one as its
    # permissive extreme, so dropping one still reads as a loosening.
    #
    # Largest leverage any one levered leg may be held at.
    max_gross_leverage: float | None = Field(default=None, ge=1)
    # Ceiling on sum(weight x leverage) over the whole book; an unlevered leg
    # counts at 1, so 1.0 admits no leverage at all.
    max_book_gross_exposure: float | None = Field(default=None, ge=1)
    # Ceiling on the summed equity weight of levered legs. 0 admits none.
    max_weight_levered: float | None = Field(default=None, ge=0, le=1)
    # Floor on each levered leg's health factor at its leverage. Strictly
    # above 1: a freshly opened position's HF is never below 1, so a floor at
    # or under it binds nothing and is a floor set wrong.
    min_health_factor: float | None = Field(default=None, gt=1)
    # Floor on the adverse debt/collateral move a CROSS-ASSET levered leg
    # survives, ``(HF - 1) x 10_000``. Same-asset legs have no price ratio.
    min_depeg_buffer_bps: float | None = Field(default=None, ge=0)
    # Floor on a levered row's reward-token 24h volume. Replaces
    # max_reward_dependence for levered rows, which are reward-driven by
    # construction; unmeasured volume fails it.
    min_reward_liquidity_usd: float | None = Field(default=None, ge=0)


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
