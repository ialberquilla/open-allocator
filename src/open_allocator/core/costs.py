"""Deterministic execution-cost and economic-viability estimate.

The 1Tx portfolio simulation reports *gross* yield: it has no gas, bridge, or
slippage model, so at small deploy sizes it can advertise an attractive APY that
fixed execution costs quietly erase. This module estimates those costs from an
allocation's legs and turns them into a net-of-cost view plus a blunt verdict
(``ok`` / ``marginal`` / ``uneconomic``) that callers surface before anyone
signs.

The model is intentionally simple and conservative, not a gas oracle:

- **Gas** is charged per signed transaction on the *source* chain, because with
  a self-custody EOA every deposit (approve + buy) signs on the chain the USDC
  is sourced from (see ``docs/funding-and-bridging.md``). Priced from **live**
  chain gas when a :class:`GasPricing` is supplied (see
  ``open_allocator.exec.gas``), and from a static per-chain-class fallback only
  when it is not.

  The fallback exists to keep this module pure and offline-runnable; it is not a
  substitute for live pricing. A *constant* gas price cannot be calibrated
  correct — measured 2026-07-30, a flat ``$0.03``/tx L2 figure was 8.4x too high
  on Base and 5.4x too high on Arbitrum, while a flat ``$0.20``/tx L1 figure was
  2.7x too high at a 0.109 gwei base fee and would be ~50x too *low* at a
  historically ordinary 15 gwei. Whenever the number matters, pass live pricing.
- **Bridge fee** applies only to legs whose destination chain differs from the
  source chain: 1Tx routes those over CCTP fast-transfer, whose fee is a few
  basis points of the bridged notional.
- **Slippage** is the swap tolerance (``slippageBps``); it is a *max adverse*
  bound, not an expected cost, so it is reported separately and kept out of the
  net-APY figure.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

# Static per-signed-tx gas cost in USD. FALLBACK ONLY — used when no live
# GasPricing is supplied. Re-calibrated 2026-07-30 from real receipts rather
# than operator recollection: a Base ERC-4626 op measured $0.0036 and an
# Arbitrum one $0.0056, against a previous constant of $0.03. See the module
# docstring for why any constant here is wrong in one direction or the other.
DEFAULT_L2_GAS_USD_PER_TX = 0.006
DEFAULT_L1_GAS_USD_PER_TX = 0.08
# Chains priced at the L1 rate. Ethereum mainnet today; extend as needed.
DEFAULT_L1_CHAIN_IDS = frozenset({1})

# Gas units for one signed deposit-side tx. Measured 2026-07-30 on a real Base
# ERC-4626 call (186,751 gas, tx 0x4640d9b1...a245) and rounded up. Deliberately
# the *heavier* of the two txs in a leg — an ERC-20 approve is roughly a quarter
# of this — so a leg's gas is over- rather than under-stated.
DEFAULT_GAS_UNITS_PER_TX = 190_000

# Every EVM native token is 18 decimals, ETH or not, so one divisor serves all.
_WEI_PER_NATIVE_TOKEN = 10**18


@dataclass(frozen=True)
class GasPricing:
    """Live gas pricing: per-chain gas prices in wei and per-chain token prices.

    Both maps are keyed by chain id, and **that is the load-bearing part**. Not
    every chain pays gas in the same token, so a single price quote cannot serve
    all of them: a gas price is a number in whatever the chain's native token
    is, and converting it at another token's price is wrong by the ratio between
    the two — orders of magnitude, silently. Keying the price by chain makes
    that unrepresentable rather than merely discouraged, and a chain whose token
    has no quote simply is not in the map.

    A chain absent from either map falls back to the static constants, which is
    the honest behaviour when a read failed or no price source exists —
    silently substituting another chain's number would be worse than admitting
    the gap.
    """

    gas_price_wei: Mapping[int, int]
    # USD per whole native token, per chain — ETH for most, but never assumed.
    native_usd: Mapping[int, float]
    gas_units_per_tx: int = DEFAULT_GAS_UNITS_PER_TX

    def usd_per_tx(self, chain_id: int) -> float | None:
        """USD for one signed tx on ``chain_id``, or None if unpriced."""
        price = self.gas_price_wei.get(chain_id)
        token_usd = self.native_usd.get(chain_id)
        if price is None or token_usd is None or token_usd <= 0:
            return None
        return self.gas_units_per_tx * price / _WEI_PER_NATIVE_TOKEN * token_usd


@dataclass(frozen=True)
class CostParams:
    l2_gas_usd_per_tx: float = DEFAULT_L2_GAS_USD_PER_TX
    l1_gas_usd_per_tx: float = DEFAULT_L1_GAS_USD_PER_TX
    l1_chain_ids: frozenset[int] = DEFAULT_L1_CHAIN_IDS
    # Live pricing when available; None falls back to the constants above.
    gas: GasPricing | None = None
    # A deposit is approve + buy; two signed txs per leg on the source chain.
    txs_per_leg: int = 2
    # Circle CCTP v2 fast-transfer fee on bridged notional (always fast mode).
    cctp_fast_fee_bps: float = 1.0
    # Max adverse swap slippage tolerance; reported, not counted as expected cost.
    slippage_bps: float = 50.0
    # One-time cost as a share of deploy: above marginal -> "marginal";
    # above uneconomic -> "uneconomic".
    marginal_cost_pct: float = 1.0
    uneconomic_cost_pct: float = 3.0

    def gas_usd_per_tx(self, chain_id: int) -> float:
        if self.gas is not None:
            live = self.gas.usd_per_tx(chain_id)
            if live is not None:
                return live
        return (
            self.l1_gas_usd_per_tx
            if chain_id in self.l1_chain_ids
            else self.l2_gas_usd_per_tx
        )

    def gas_priced_live(self, chain_id: int) -> bool:
        """Whether ``chain_id``'s gas came from a live read, not the fallback."""
        return self.gas is not None and self.gas.usd_per_tx(chain_id) is not None


@dataclass(frozen=True)
class LegInput:
    instrument_id: str
    chain_id: int
    usd: float
    apy_pct: float


@dataclass(frozen=True)
class CostEstimate:
    source_chain_id: int
    deploy_usd: float
    gas_cost_usd: float
    bridge_fee_usd: float
    total_expected_cost_usd: float
    max_slippage_usd: float
    cost_pct_of_deploy: float
    gross_blended_apy_pct: float
    net_apy_pct_year1: float
    breakeven_days: float | None
    bridged_usd: float
    bridged_leg_count: int
    leg_count: int
    verdict: str
    # False when the source chain's gas came from the static fallback rather
    # than a live read. Surfaced because a fallback-priced gas number should not
    # be read with the same confidence as a measured one.
    gas_priced_live: bool = True

    def as_metadata(self) -> dict[str, float | int | str | bool]:
        """Flat, schema-safe scalar dict for allocation ``metadata``."""
        data: dict[str, float | int | str | bool] = {
            "source_chain_id": self.source_chain_id,
            "deploy_usd": self.deploy_usd,
            "gas_cost_usd": self.gas_cost_usd,
            "gas_priced_live": self.gas_priced_live,
            "bridge_fee_usd": self.bridge_fee_usd,
            "total_expected_cost_usd": self.total_expected_cost_usd,
            "max_slippage_usd": self.max_slippage_usd,
            "cost_pct_of_deploy": self.cost_pct_of_deploy,
            "gross_blended_apy_pct": self.gross_blended_apy_pct,
            "net_apy_pct_year1": self.net_apy_pct_year1,
            "bridged_usd": self.bridged_usd,
            "bridged_leg_count": self.bridged_leg_count,
            "leg_count": self.leg_count,
            "verdict": self.verdict,
        }
        # breakeven_days is None when gross yield is non-positive (never repays).
        if self.breakeven_days is not None:
            data["breakeven_days"] = self.breakeven_days
        return data

    def warning(self) -> str | None:
        if self.verdict == "ok":
            return None
        return f"viability:{self.verdict}:cost_pct={self.cost_pct_of_deploy:.2f}"


def min_economic_leg_usd(
    chain_id: int,
    *,
    params: CostParams | None = None,
    max_gas_pct_of_leg: float = 0.10,
) -> float:
    """Smallest leg size on ``chain_id`` whose gas stays under a share of it.

    A gas-aware dust floor. ``min_position_usd`` is a flat number the caller
    picks, which is fine while every chain costs the same fraction of a cent and
    wrong the moment an Ethereum leg is priced at a normal base fee: the same
    $250 leg is negligible-cost at 0.109 gwei and ~4% gas at 15 gwei.

    Expressed as a *share of the leg* rather than an absolute so it scales with
    gas instead of needing recalibration. The default 0.10% is deliberately
    loose — this is a floor to stop absurd legs, not an optimiser.
    """
    params = params or CostParams()
    leg_gas = params.txs_per_leg * params.gas_usd_per_tx(chain_id)
    if max_gas_pct_of_leg <= 0:
        raise ValueError("max_gas_pct_of_leg must be positive")
    return leg_gas / (max_gas_pct_of_leg / 100)


def default_source_chain_id(legs: Sequence[LegInput]) -> int:
    """Chain holding the largest share of deploy USD.

    A wallet is most cheaply funded on the chain that needs the most capital, so
    absent an explicit source we assume that chain and treat the rest as bridged.
    """
    if not legs:
        raise ValueError("cannot infer source chain from an empty allocation")
    by_chain: dict[int, float] = {}
    for leg in legs:
        by_chain[leg.chain_id] = by_chain.get(leg.chain_id, 0.0) + leg.usd
    # Deterministic: most USD wins, ties broken by lowest chain id.
    return min(by_chain, key=lambda cid: (-by_chain[cid], cid))


def estimate(
    legs: Sequence[LegInput],
    *,
    source_chain_id: int | None = None,
    params: CostParams | None = None,
) -> CostEstimate | None:
    """Estimate execution cost and viability for an allocation's legs.

    Returns ``None`` when there is nothing to deploy.
    """
    params = params or CostParams()
    priced = [leg for leg in legs if leg.usd > 0]
    if not priced:
        return None

    source = (
        source_chain_id
        if source_chain_id is not None
        else default_source_chain_id(priced)
    )
    deploy_usd = sum(leg.usd for leg in priced)

    # Every deposit signs on the source chain (approve + buy).
    gas_cost = len(priced) * params.txs_per_leg * params.gas_usd_per_tx(source)

    bridged = [leg for leg in priced if leg.chain_id != source]
    bridged_usd = sum(leg.usd for leg in bridged)
    bridge_fee = bridged_usd * params.cctp_fast_fee_bps / 10_000

    total_cost = gas_cost + bridge_fee
    max_slippage = deploy_usd * params.slippage_bps / 10_000

    cost_pct = total_cost / deploy_usd * 100 if deploy_usd > 0 else 0.0
    gross_apy = (
        sum(leg.usd * leg.apy_pct for leg in priced) / deploy_usd
        if deploy_usd > 0
        else 0.0
    )
    net_apy_year1 = gross_apy - cost_pct

    gross_annual_usd = deploy_usd * gross_apy / 100
    breakeven_days = (
        total_cost / (gross_annual_usd / 365) if gross_annual_usd > 0 else None
    )

    if cost_pct > params.uneconomic_cost_pct:
        verdict = "uneconomic"
    elif cost_pct > params.marginal_cost_pct:
        verdict = "marginal"
    else:
        verdict = "ok"

    return CostEstimate(
        source_chain_id=source,
        deploy_usd=round(deploy_usd, 2),
        gas_cost_usd=round(gas_cost, 4),
        bridge_fee_usd=round(bridge_fee, 4),
        total_expected_cost_usd=round(total_cost, 4),
        max_slippage_usd=round(max_slippage, 4),
        cost_pct_of_deploy=round(cost_pct, 3),
        gross_blended_apy_pct=round(gross_apy, 3),
        net_apy_pct_year1=round(net_apy_year1, 3),
        breakeven_days=round(breakeven_days, 1) if breakeven_days is not None else None,
        bridged_usd=round(bridged_usd, 2),
        bridged_leg_count=len(bridged),
        leg_count=len(priced),
        verdict=verdict,
        gas_priced_live=params.gas_priced_live(source),
    )


def estimate_from_allocation_legs(
    legs: Sequence[Mapping[str, object]],
    *,
    chain_by_instrument: Mapping[str, int],
    apy_by_instrument: Mapping[str, float],
    source_chain_id: int | None = None,
    params: CostParams | None = None,
) -> CostEstimate | None:
    """Build :func:`estimate` inputs from allocation legs + universe lookups.

    Legs whose instrument is missing a chain are skipped (cannot be priced);
    a missing APY is treated as 0 so the leg still carries its execution cost.
    """
    inputs: list[LegInput] = []
    for leg in legs:
        instrument_id = str(leg["instrument_id"])
        chain_id = chain_by_instrument.get(instrument_id)
        if chain_id is None:
            continue
        inputs.append(
            LegInput(
                instrument_id=instrument_id,
                chain_id=chain_id,
                usd=float(leg["usd"]),
                apy_pct=float(apy_by_instrument.get(instrument_id, 0.0)),
            )
        )
    return estimate(inputs, source_chain_id=source_chain_id, params=params)


__all__ = [
    "DEFAULT_GAS_UNITS_PER_TX",
    "CostEstimate",
    "CostParams",
    "GasPricing",
    "LegInput",
    "default_source_chain_id",
    "estimate",
    "estimate_from_allocation_legs",
    "min_economic_leg_usd",
]
