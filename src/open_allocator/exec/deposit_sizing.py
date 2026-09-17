"""Calldata deposits sized to the USDC their chain will actually hold.

Shared by calldata deposits and rebalances. A chain's deposits are funded by the
Safe's USDC there plus the conservative proceeds of the withdrawals that precede
them on that chain, and are sized down only for proceeds rounding and the
paymaster's maximum gas charge. A chain that needs more than that is left at
full size, so the funding check reports the shortfall instead of a quietly
smaller deposit.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from open_allocator.core import amounts
from open_allocator.core.types import TxPlan
from open_allocator.exec import bundle_execution, calldata, chains, funding

# Attempts to fit a chain's deposits around the paymaster charge before the
# shortfall is reported as a blocker.
MAX_PREPARATIONS = 3


@dataclass(frozen=True)
class DepositRequest:
    """One deposit the plan wants, before it is sized to its chain's funds."""

    # The allocation leg or rebalance trade index the bundle is keyed by.
    index: int
    instrument_id: str
    chain_id: int
    token: calldata.DepositToken
    wanted_raw: int


@dataclass(frozen=True)
class FittedPlan:
    plan: TxPlan
    preparation: bundle_execution.PlanPreparation
    # One note per deposit sized down or skipped.
    messages: tuple[str, ...]
    # What each planned deposit actually spends, by index.
    deposit_usd: dict[int, float]


def fit(
    client: object,
    signer: object,
    address: str,
    *,
    deposits: Sequence[DepositRequest],
    withdrawals: Mapping[int, Sequence[bundle_execution.PlannedBundle]] | None = None,
    withdrawal_usd: Mapping[int, float] | None = None,
    summary: Callable[[Sequence[bundle_execution.PlannedBundle]], str],
    config: object | None,
    idempotency_store: object | None,
) -> FittedPlan:
    """Plan each chain's withdrawals, then its deposits sized to fit, and prepare.

    ``withdrawals`` are already-built bundles by chain; ``withdrawal_usd`` is
    each one's dollar value by index. Deposits that need another chain's
    withdrawal proceeds are refused as cross-chain.
    """
    sells = {chain_id: list(items) for chain_id, items in (withdrawals or {}).items()}
    sell_usd = withdrawal_usd or {}
    chain_order: dict[int, None] = dict.fromkeys(sells)
    buys: dict[int, list[DepositRequest]] = {}
    for request in deposits:
        chain_order[request.chain_id] = None
        buys.setdefault(request.chain_id, []).append(request)

    idle_keys = {
        chain_id: funding.key_for(chain_id, address, chain_buys[0].token.address)
        for chain_id, chain_buys in buys.items()
    }
    read, _unread = funding.read_balances(idle_keys.values(), config)
    # None when unreadable: such a chain is not sized, and the funding check
    # reports the balance it could not read.
    idle = {chain_id: read.get(key) for chain_id, key in idle_keys.items()}
    slippage = funding.slippage_bps(config)
    planned_proceeds: dict[int, int] = {}
    safe_proceeds: dict[int, int] = {}
    for chain_id, chain_sells in sells.items():
        usdc = chains.usdc_address(chain_id, config)
        for item in chain_sells:
            bundle = item.bundle
            if usdc is None or bundle.token_out.address.casefold() != usdc.casefold():
                # Pays out in something a deposit cannot spend.
                continue
            planned_proceeds[chain_id] = planned_proceeds.get(
                chain_id, 0
            ) + amounts.to_raw_units(
                sell_usd[bundle.leg_index],
                bundle.token_out.decimals,
                name="sell amount",
            )
            safe_proceeds[chain_id] = safe_proceeds.get(
                chain_id, 0
            ) + funding.conservative_output(bundle, slippage)

    def slack(chain_id: int) -> int:
        # Each dollar-to-raw conversion rounds down by at most one unit.
        return len(sells.get(chain_id, ())) + len(buys.get(chain_id, ()))

    short: dict[int, int] = {}
    surplus: dict[int, int] = {}
    for chain_id in chain_order:
        proceeds = planned_proceeds.get(chain_id, 0)
        if chain_id not in buys:
            surplus[chain_id] = proceeds
            continue
        held = idle[chain_id]
        if held is None:
            continue
        wanted = sum(buy.wanted_raw for buy in buys[chain_id])
        gap = wanted - held - proceeds
        if gap > slack(chain_id):
            short[chain_id] = gap
        else:
            surplus[chain_id] = min(proceeds, proceeds - gap)
    for chain_id, gap in short.items():
        elsewhere = [
            other
            for other, amount in surplus.items()
            if other != chain_id and amount > slack(other)
        ]
        if elsewhere:
            raise calldata.CalldataUnsupportedError(
                f"cross-chain rebalance: buys on {chain_label(chain_id)} need "
                f"{gap} raw units of USDC more than the Safe holds there plus "
                "the proceeds of its sells there, while sells on "
                f"{', '.join(chain_label(other) for other in elsewhere)} pay "
                "out on another chain; bridging is not supported by the calldata "
                "API path yet"
            )

    def sized(reserve: Mapping[int, int]) -> tuple[dict[int, int], list[str]]:
        sizes: dict[int, int] = {}
        notes: list[str] = []
        for chain_id, chain_buys in buys.items():
            held = idle[chain_id]
            if held is None or chain_id in short:
                sizes.update((buy.index, buy.wanted_raw) for buy in chain_buys)
                continue
            budget = held + safe_proceeds.get(chain_id, 0) - reserve.get(chain_id, 0)
            for buy in chain_buys:
                size = max(0, min(buy.wanted_raw, budget))
                budget -= size
                sizes[buy.index] = size
                if size < buy.wanted_raw:
                    notes.append(
                        _sized_note(
                            buy,
                            size,
                            with_sells=chain_id in sells,
                            reserved=chain_id in reserve,
                        )
                    )
        return sizes, notes

    built: dict[tuple[int, int], bundle_execution.PlannedBundle] = {}

    def deposit(buy: DepositRequest, size: int) -> bundle_execution.PlannedBundle:
        # A size seen before reuses its bundle, so fitting the paymaster charge
        # re-requests only the deposits it actually changed.
        if (buy.index, size) not in built:
            steps, bundle = calldata.request_bundle(
                client,
                instrument_id=buy.instrument_id,
                action="deposit",
                account=address,
                chain_id=buy.chain_id,
                amount=str(size),
                leg_index=buy.index,
                first_step_index=0,
                config=config,
                token=buy.token,
            )
            built[(buy.index, size)] = bundle_execution.PlannedBundle(
                bundle=bundle, steps=steps
            )
        return built[(buy.index, size)]

    reserve: dict[int, int] = {}
    for _attempt in range(MAX_PREPARATIONS):
        sizes, notes = sized(reserve)
        ordered: list[bundle_execution.PlannedBundle] = []
        for chain_id in chain_order:
            # Sells before buys, chains contiguous: the signer merges consecutive
            # same-chain bundles into one operation.
            ordered.extend(sells.get(chain_id, ()))
            ordered.extend(
                deposit(buy, sizes[buy.index])
                for buy in buys.get(chain_id, ())
                if sizes[buy.index] > 0
            )
        tx_plan = bundle_execution.assemble_plan(ordered, summary(ordered))
        preparation = bundle_execution.prepare_plan(
            signer, tx_plan, config, idempotency_store
        )
        grown = dict(reserve)
        for item in preparation.funding:
            chain_buys = buys.get(item.chain_id, ())
            if (
                item.ok
                or not item.includes_gas_charge
                or item.available_raw is None
                or not chain_buys
                or item.chain_id in short
                or idle[item.chain_id] is None
                or item.token.casefold() != chain_buys[0].token.address.casefold()
                or not any(sizes[buy.index] > 0 for buy in chain_buys)
            ):
                continue
            grown[item.chain_id] = grown.get(item.chain_id, 0) + int(item.shortfall_raw)
        if grown == reserve:
            break
        reserve = grown

    return FittedPlan(
        plan=tx_plan,
        preparation=preparation,
        messages=tuple(notes),
        deposit_usd={
            buy.index: float(
                amounts.from_raw_units(sizes[buy.index], buy.token.decimals)
            )
            for chain_buys in buys.values()
            for buy in chain_buys
            if sizes[buy.index] > 0
        },
    )


def chain_label(chain_id: int) -> str:
    return f"{chains.chain_name(chain_id)} (chain {chain_id})"


def _sized_note(
    buy: DepositRequest,
    size: int,
    *,
    with_sells: bool,
    reserved: bool,
) -> str:
    wanted = amounts.from_raw_units(buy.wanted_raw, buy.token.decimals)
    fits = (
        f"the USDC {chain_label(buy.chain_id)} will hold — its balance"
        + (" plus its sells' minimum proceeds" if with_sells else "")
        + (", less the paymaster's maximum gas charge" if reserved else "")
    )
    if size == 0:
        return f"buy {buy.instrument_id} of {wanted} USDC skipped: none fits {fits}"
    return (
        f"buy {buy.instrument_id} sized to "
        f"{amounts.from_raw_units(size, buy.token.decimals)} of {wanted} USDC to fit "
        f"{fits}"
    )


__all__ = ["MAX_PREPARATIONS", "DepositRequest", "FittedPlan", "chain_label", "fit"]
