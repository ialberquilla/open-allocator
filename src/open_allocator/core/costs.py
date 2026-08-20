"""Deterministic execution-cost and economic-viability estimate.

The 1Tx portfolio simulation reports *gross* yield: it has no gas, bridge, or
slippage model, so at small deploy sizes it can advertise an attractive APY that
fixed execution costs quietly erase. This module estimates those costs from an
allocation's legs and turns them into a net-of-cost view plus a blunt verdict
(``ok`` / ``marginal`` / ``uneconomic``) that callers surface before anyone
signs.

🔑 **TWO QUESTIONS, TWO FUNCTIONS, AND THE SECOND IS NOT THE FIRST.**
:func:`estimate` prices *deploying cash into a book* and judges the cost against
the yield that book will earn — correct when the alternative is holding USDC.
:func:`estimate_rebalance` prices *moving an existing book to a different one*
and judges the cost against the **difference** between them, because the book is
already earning and only the change is new. Asking :func:`estimate` about a
rebalance returns a breakeven computed against yield that was never at stake,
which flatters every trade and flatters compliance-only trades infinitely: they
improve yield by nothing and would still report a short payback.

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
    # An exit is one redeem. Kept separate from ``txs_per_leg`` because a
    # rebalance pays both and they are not the same number: charging a sell two
    # txs overstates every trim, and the trims are where a small book lives.
    # Matches ``core.drift``'s ``_EXIT_TXS_PER_SWITCH`` so the gate's payback and
    # this module's payback are the same arithmetic on the same trade.
    txs_per_exit: int = 1
    # Payback thresholds for a REBALANCE verdict, in days. The uneconomic line
    # is ``core.drift``'s ``_PAYBACK_HORIZON_DAYS`` restated: a switch that does
    # not repay within a year is not a switch, and the two modules disagreeing
    # about that would let the gate fire something this module then blesses.
    marginal_payback_days: float = 90.0
    uneconomic_payback_days: float = 365.0
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


# ─────────────────────────────────────────────────────────────────────────────
# Rebalancing an existing book, which is a different question from deploying one


@dataclass(frozen=True)
class MoveInput:
    """One instrument's before and after. ``current_usd == target_usd`` is fine.

    An unchanged leg costs nothing and still belongs in the list: it is part of
    the book whose blended yield is the denominator on both sides of the
    comparison, and leaving it out would price a six-leg book's improvement as
    if it were a two-leg book's.
    """

    instrument_id: str
    chain_id: int
    current_usd: float
    target_usd: float
    apy_pct: float

    @property
    def delta_usd(self) -> float:
        return self.target_usd - self.current_usd


@dataclass(frozen=True)
class RebalanceEstimate:
    """What moving from the current book to a target one costs and earns.

    🔑 THE FIELD THAT MATTERS IS ``annual_gain_usd``, AND IT CAN BE ZERO.
    :func:`estimate` prices a fresh deployment and judges it against the book's
    *gross* yield, which is the right question when the alternative is holding
    cash and the wrong one here: a rebalance is already invested, so its payback
    comes only from the *difference* between the two books. A move that changes
    the weights without improving the yield — a compliance fix, most obviously —
    has no yield to repay out of, and asking ``estimate`` about it returns a
    flattering breakeven computed against yield the book was already earning.
    """

    # What actually moves
    moved_leg_count: int
    skipped_leg_count: int
    skipped_usd: float
    buy_usd: float
    sell_usd: float
    turnover_usd: float
    tx_count: int

    # What it costs
    gas_cost_usd: float
    bridge_fee_usd: float
    total_expected_cost_usd: float
    max_slippage_usd: float

    # Whether each chain can pay for its own buys
    bridged_usd: float
    unfundable_usd: float
    net_flow_by_chain: Mapping[int, float]

    # What it earns
    current_blended_apy_pct: float
    target_blended_apy_pct: float
    apy_delta_pct: float
    annual_gain_usd: float
    payback_days: float | None

    verdict: str
    gas_priced_live: bool
    unpriced_chain_ids: tuple[int, ...]

    def as_metadata(self) -> dict[str, float | int | str | bool]:
        """Flat, schema-safe scalar dict. ``net_flow_by_chain`` is dropped."""
        data: dict[str, float | int | str | bool] = {
            "moved_leg_count": self.moved_leg_count,
            "skipped_leg_count": self.skipped_leg_count,
            "skipped_usd": self.skipped_usd,
            "buy_usd": self.buy_usd,
            "sell_usd": self.sell_usd,
            "turnover_usd": self.turnover_usd,
            "tx_count": self.tx_count,
            "gas_cost_usd": self.gas_cost_usd,
            "bridge_fee_usd": self.bridge_fee_usd,
            "total_expected_cost_usd": self.total_expected_cost_usd,
            "max_slippage_usd": self.max_slippage_usd,
            "bridged_usd": self.bridged_usd,
            "unfundable_usd": self.unfundable_usd,
            "current_blended_apy_pct": self.current_blended_apy_pct,
            "target_blended_apy_pct": self.target_blended_apy_pct,
            "apy_delta_pct": self.apy_delta_pct,
            "annual_gain_usd": self.annual_gain_usd,
            "verdict": self.verdict,
            "gas_priced_live": self.gas_priced_live,
        }
        # None means "never repays", which is not the same as a large number and
        # must not be flattened into one.
        if self.payback_days is not None:
            data["payback_days"] = self.payback_days
        return data

    def warning(self) -> str | None:
        if self.verdict in {"ok", "nothing_to_do"}:
            return None
        return f"rebalance:{self.verdict}"


def estimate_rebalance(
    moves: Sequence[MoveInput],
    *,
    params: CostParams | None = None,
    min_trade_usd: float = 0.0,
    idle_usd_by_chain: Mapping[int, float] | None = None,
) -> RebalanceEstimate | None:
    """Cost, funding and payback of moving a book from current to target.

    ``min_trade_usd`` is the executor's own threshold
    (``core.rebalance.plan_rebalance``, default $1.00). It is an input here
    rather than an afterthought because **a move below it does not happen**: the
    caller is otherwise told the cost of a plan the executor will silently
    decline to run, and the book that actually results is neither the current
    one nor the target. Skipped legs are reported, not hidden.

    ``idle_usd_by_chain`` is what each chain can already pay with. Omit it and
    every net inflow is assumed to need bridging, which overstates cost rather
    than understating it — the safe direction, and the honest one when the
    caller does not know.

    Returns ``None`` when the move list is empty.
    """
    params = params or CostParams()
    if not moves:
        return None

    threshold = max(min_trade_usd, 0.0)
    moved = [m for m in moves if abs(m.delta_usd) > threshold and m.delta_usd != 0.0]
    skipped = [
        m
        for m in moves
        if m.delta_usd != 0.0 and abs(m.delta_usd) <= threshold
    ]

    buy_usd = sum(m.delta_usd for m in moved if m.delta_usd > 0)
    sell_usd = sum(-m.delta_usd for m in moved if m.delta_usd < 0)

    # Gas is charged on the leg's OWN chain, for both directions: you redeem
    # where the vault is and you deposit where the vault is. This is the same
    # convention ``core.drift`` prices a switch with, and it deliberately
    # differs from :func:`estimate`, which charges a first deployment on the
    # single chain the USDC is funded from.
    gas_cost = 0.0
    tx_count = 0
    for move in moved:
        txs = params.txs_per_leg if move.delta_usd > 0 else params.txs_per_exit
        tx_count += txs
        gas_cost += txs * params.gas_usd_per_tx(move.chain_id)

    # Can each chain pay for its own buys? Net flow plus whatever idle already
    # sits there; a shortfall has to arrive from somewhere else, and that is a
    # bridge whether or not anyone planned one.
    idle = dict(idle_usd_by_chain or {})
    net_flow: dict[int, float] = {}
    for move in moved:
        net_flow[move.chain_id] = net_flow.get(move.chain_id, 0.0) + move.delta_usd

    shortfall = 0.0
    surplus = 0.0
    for chain_id, net in net_flow.items():
        available = idle.get(chain_id, 0.0) - net
        if available < 0:
            shortfall += -available
        else:
            surplus += available
    # Idle on a chain with no movement is still capital that can be bridged out.
    for chain_id, amount in idle.items():
        if chain_id not in net_flow:
            surplus += amount

    bridged_usd = shortfall
    # 🔴 What no chain can cover. A positive number here means the plan cannot
    # be executed as written, which is a different failure from an expensive
    # one and must not be reported as a cost.
    unfundable_usd = max(shortfall - surplus, 0.0)
    bridge_fee = bridged_usd * params.cctp_fast_fee_bps / 10_000

    turnover = buy_usd + sell_usd
    total_cost = gas_cost + bridge_fee
    max_slippage = turnover * params.slippage_bps / 10_000

    # Blended yield on both sides, over the WHOLE book each time. The target
    # side uses the post-threshold book — what will actually be held — so a plan
    # whose improvement lives entirely in skipped legs shows no improvement.
    effective = {m.instrument_id: m.current_usd for m in moves}
    for move in moved:
        effective[move.instrument_id] = move.target_usd
    apy_by_id = {m.instrument_id: m.apy_pct for m in moves}

    current_total = sum(m.current_usd for m in moves)
    target_total = sum(effective.values())
    current_blended = (
        sum(m.current_usd * m.apy_pct for m in moves) / current_total
        if current_total > 0
        else 0.0
    )
    target_blended = (
        sum(usd * apy_by_id[i] for i, usd in effective.items()) / target_total
        if target_total > 0
        else 0.0
    )

    # Dollars per year, not percentage points: deploying idle capital raises the
    # book's earnings while barely moving its blended rate, and a rebalance that
    # trades yield for compliance lowers the rate while still being correct.
    # Only the dollar figure answers "does this repay its own gas".
    annual_gain_usd = (
        target_total * target_blended / 100 - current_total * current_blended / 100
    )

    payback_days = (
        total_cost / (annual_gain_usd / 365.0) if annual_gain_usd > 0 else None
    )

    unpriced = tuple(
        sorted(
            {
                move.chain_id
                for move in moved
                if not params.gas_priced_live(move.chain_id)
            }
        )
    )

    if not moved:
        verdict = "nothing_to_do"
    elif unfundable_usd > 0:
        verdict = "unfundable"
    elif payback_days is None:
        # No yield to repay out of. Not automatically wrong — a compliance fix
        # buys compliance — but the caller has to justify it on something other
        # than money, and saying so is this module's whole job.
        verdict = "no_yield_gain"
    elif payback_days > params.uneconomic_payback_days:
        verdict = "uneconomic"
    elif payback_days > params.marginal_payback_days:
        verdict = "marginal"
    else:
        verdict = "ok"

    return RebalanceEstimate(
        moved_leg_count=len(moved),
        skipped_leg_count=len(skipped),
        skipped_usd=round(sum(abs(m.delta_usd) for m in skipped), 4),
        buy_usd=round(buy_usd, 4),
        sell_usd=round(sell_usd, 4),
        turnover_usd=round(turnover, 4),
        tx_count=tx_count,
        gas_cost_usd=round(gas_cost, 4),
        bridge_fee_usd=round(bridge_fee, 4),
        total_expected_cost_usd=round(total_cost, 4),
        max_slippage_usd=round(max_slippage, 4),
        bridged_usd=round(bridged_usd, 4),
        unfundable_usd=round(unfundable_usd, 4),
        net_flow_by_chain={k: round(v, 4) for k, v in sorted(net_flow.items())},
        current_blended_apy_pct=round(current_blended, 4),
        target_blended_apy_pct=round(target_blended, 4),
        apy_delta_pct=round(target_blended - current_blended, 4),
        annual_gain_usd=round(annual_gain_usd, 4),
        payback_days=round(payback_days, 2) if payback_days is not None else None,
        verdict=verdict,
        gas_priced_live=not unpriced,
        unpriced_chain_ids=unpriced,
    )


def estimate_rebalance_from_holdings(
    current_usd_by_instrument: Mapping[str, float],
    target_usd_by_instrument: Mapping[str, float],
    *,
    chain_by_instrument: Mapping[str, int],
    apy_by_instrument: Mapping[str, float],
    params: CostParams | None = None,
    min_trade_usd: float = 0.0,
    idle_usd_by_chain: Mapping[int, float] | None = None,
) -> RebalanceEstimate | None:
    """Build :func:`estimate_rebalance` inputs from two plain USD maps.

    The union of both maps is priced, so an instrument being exited entirely
    (present in current, absent from target) and one being opened (the reverse)
    are both moves rather than omissions. An instrument with no known chain is
    skipped — it cannot be priced — and a missing APY is treated as 0 so the leg
    still carries its execution cost.
    """
    moves: list[MoveInput] = []
    for instrument_id in sorted(
        set(current_usd_by_instrument) | set(target_usd_by_instrument)
    ):
        chain_id = chain_by_instrument.get(instrument_id)
        if chain_id is None:
            continue
        moves.append(
            MoveInput(
                instrument_id=instrument_id,
                chain_id=chain_id,
                current_usd=float(current_usd_by_instrument.get(instrument_id, 0.0)),
                target_usd=float(target_usd_by_instrument.get(instrument_id, 0.0)),
                apy_pct=float(apy_by_instrument.get(instrument_id, 0.0)),
            )
        )
    return estimate_rebalance(
        moves,
        params=params,
        min_trade_usd=min_trade_usd,
        idle_usd_by_chain=idle_usd_by_chain,
    )


__all__ = [
    "DEFAULT_GAS_UNITS_PER_TX",
    "CostEstimate",
    "CostParams",
    "GasPricing",
    "LegInput",
    "MoveInput",
    "RebalanceEstimate",
    "default_source_chain_id",
    "estimate",
    "estimate_from_allocation_legs",
    "estimate_rebalance",
    "estimate_rebalance_from_holdings",
    "min_economic_leg_usd",
]
