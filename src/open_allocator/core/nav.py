"""The unit ledger behind the NAV history, over the Safe's positions only.

Ported from darex's ``agent-nav.ts`` so the two read a book the same way.

NAV is the value held in instruments; idle USDC is not in it. Money moving
between idle and a position is a flow, whoever moved it, so no record of
deposits or withdrawals is needed: a flow is read off the position's own
non-rebasing share count (`close_flows`). That is what lets a history be
rebuilt from chain reads alone, for days nobody was watching.

The day's return accrues to the units already outstanding; flows then mint
(or burn) units at the resulting price. Gas is paid from idle USDC, outside
NAV, so it is booked as money spent from outside: it lowers the price and
mints the units that keep NAV whole.

Integers throughout: USD in micro (1e-6), units and unit price at 1e-12,
rounded half to even.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

USD_SCALE = 6
LEDGER_SCALE = 12

ONE_USD = 10**USD_SCALE
ONE = 10**LEDGER_SCALE

# A new ledger opens here, so a reader sees the growth of $100.
UNIT_PRICE_BASE = 100 * ONE


@dataclass(frozen=True)
class PositionClose:
    """One position at a close.

    ``base_shares`` is a count that does not grow with yield: ERC-4626 or
    mToken shares, Aave's scaled balance, Comet's principal, a PT balance.
    None when it could not be read.

    ``debt_shares`` is a loop's debt in the same terms (the debt token's
    scaled balance); None for a position with no debt. ``usd_micro`` is then
    the loop's equity.
    """

    chain_id: int
    instrument_id: str
    base_shares: int | None
    usd_micro: int
    debt_shares: int | None = None

    @property
    def key(self) -> str:
        return position_key(self.chain_id, self.instrument_id)


@dataclass(frozen=True)
class CloseFlows:
    # Value held in positions, micro-USD.
    nav_micro: int
    # Money that moved into positions (negative: out of them), micro-USD.
    flow_micro: int
    # A position whose flow could not be split from its return. Its whole
    # change is counted as flow, so the day's price is not known.
    unknown: bool


@dataclass(frozen=True)
class LedgerState:
    # NAV at the previous close, micro-USD.
    nav_micro: int
    # Units outstanding after it, 1e-12. Zero when the ledger is closed.
    units: int


@dataclass(frozen=True)
class LedgerStep:
    units: int
    # None when the day's return is unknown, or nothing is held.
    unit_price: int | None
    # NAV change less flows and gas, micro-USD; None when the return is unknown.
    yield_micro: int | None


def position_key(chain_id: int, instrument_id: str) -> str:
    return f"{chain_id}:{instrument_id.lower()}"


def close_flows(
    previous: Sequence[PositionClose], current: Sequence[PositionClose]
) -> CloseFlows:
    """NAV and flows between two closes.

    - A position held at both closes: the change in its share count, valued at
      today's price per share. Unchanged shares are no flow, whatever the value
      did: that is the return.
    - A position that appears: its whole value flowed in. One that is gone: its
      last value flowed out (the part of a day's yield it earned before leaving
      is not seen).
    - A loop whose collateral and debt shares are both unchanged had no flow.
      One resized (either changed) cannot be split: two legs moved at
      different prices, and one share count does not say how much equity
      came or went.
    """
    before = {leg.key: leg for leg in previous}
    after = {leg.key: leg for leg in current}
    nav = 0
    flow = 0
    unknown = False

    for key, leg in after.items():
        nav += leg.usd_micro
        prior = before.get(key)
        if prior is None:
            flow += leg.usd_micro
            continue
        moved = held_flow(prior, leg)
        if moved is None:
            flow += leg.usd_micro - prior.usd_micro
            unknown = True
        else:
            flow += moved
    for key, prior in before.items():
        if key not in after:
            flow -= prior.usd_micro
    return CloseFlows(nav_micro=nav, flow_micro=flow, unknown=unknown)


def held_flow(prior: PositionClose, leg: PositionClose) -> int | None:
    """The flow into a position held at both closes, micro-USD; None when it
    cannot be split from the return."""
    if leg.base_shares is None or prior.base_shares is None or leg.base_shares <= 0:
        return None
    if leg.debt_shares is not None or prior.debt_shares is not None:
        same = (
            leg.debt_shares == prior.debt_shares
            and leg.base_shares == prior.base_shares
        )
        return 0 if same else None
    if leg.base_shares == prior.base_shares:
        return 0
    return div_round(
        (leg.base_shares - prior.base_shares) * leg.usd_micro, leg.base_shares
    )


@dataclass(frozen=True)
class PositionYield:
    # Return earned while held, micro-USD, over the days it could be split.
    yield_micro: int
    # Days held at both closes whose flow could not be split from the return.
    unknown_days: int


def attribute_yield(
    closes: Iterable[Sequence[PositionClose] | None],
) -> dict[str, PositionYield]:
    """Each position's share of the return, by `position_key`, over closes in
    day order.

    A position's return is its value change less its flow, on each day it is
    held at both ends; ``None`` is an unread day, and the next close is diffed
    against the last one read, as the ledger does. The part of a day earned
    by a position entering or leaving is not seen here either, and gas is not
    charged to any position.
    """
    earned: dict[str, int] = {}
    unknown: dict[str, int] = {}
    previous: dict[str, PositionClose] | None = None
    for legs in closes:
        if legs is None:
            continue
        current = {leg.key: leg for leg in legs}
        for key, leg in current.items():
            prior = (previous or {}).get(key)
            if prior is None:
                continue
            moved = held_flow(prior, leg)
            if moved is None:
                unknown[key] = unknown.get(key, 0) + 1
            else:
                earned[key] = (
                    earned.get(key, 0) + leg.usd_micro - prior.usd_micro - moved
                )
        previous = current
    return {
        key: PositionYield(
            yield_micro=earned.get(key, 0), unknown_days=unknown.get(key, 0)
        )
        for key in sorted(earned.keys() | unknown.keys())
    }


def unit_step(
    previous: LedgerState | None,
    *,
    nav_micro: int,
    flow_micro: int,
    gas_micro: int | None = 0,
    flow_unknown: bool = False,
    open_price: int = UNIT_PRICE_BASE,
) -> LedgerStep:
    """Advance the ledger by one close.

    ``gas_micro`` None means the gas in the window is not known: the step is
    taken as if it were 0, and the price and yield are withheld. So is a close
    whose flow is unknown.

    With no open ledger (the first close, or everything withdrawn), a close
    holding something opens one at ``open_price``: 100 for a new book, the last
    published price for one coming back, so the series does not jump.
    """
    if previous is None or previous.units <= 0 or previous.nav_micro <= 0:
        if nav_micro <= 0:
            return LedgerStep(units=0, unit_price=None, yield_micro=None)
        return LedgerStep(
            units=div_round(nav_micro * ONE * (ONE // ONE_USD), open_price),
            unit_price=open_price,
            yield_micro=None,
        )

    gas = gas_micro or 0
    priced = nav_micro - flow_micro - gas
    if priced <= 0:
        # The return and the gas took everything the units held: nothing to price.
        return LedgerStep(units=0, unit_price=None, yield_micro=None)

    minted = div_round((flow_micro + gas) * previous.units, priced)
    known = gas_micro is not None and not flow_unknown
    return LedgerStep(
        units=previous.units + minted,
        unit_price=div_round(priced * ONE * (ONE // ONE_USD), previous.units)
        if known
        else None,
        yield_micro=priced - previous.nav_micro if known else None,
    )


@dataclass(frozen=True)
class NavDay:
    day: str
    # None on a day whose close could not be read: a gap, never a zero.
    nav_micro: int | None
    # On the day a ledger opens, the whole NAV is a flow in.
    flow_micro: int | None
    units: int | None
    unit_price: int | None
    yield_micro: int | None
    status: str  # "ok" | "opened" | "unknown" | "empty"
    reason: str | None = None


def derive_nav(
    closes: Iterable[tuple[str, Sequence[PositionClose] | None, str | None]],
    *,
    gas_micro: Mapping[str, int | None] | None = None,
) -> list[NavDay]:
    """The NAV series from closes in day order.

    Each close is ``(day, legs, reason)``; ``legs`` None is a day that could not
    be read, and ``reason`` says why. It is a gap: the next readable day is
    diffed against the last one read, so its return covers the gap. The ledger
    reopens at the last published price, never at 100 again.
    """
    gas = gas_micro or {}
    rows: list[NavDay] = []
    previous_legs: Sequence[PositionClose] = ()
    state: LedgerState | None = None
    last_price = UNIT_PRICE_BASE

    for day, legs, reason in closes:
        if legs is None:
            rows.append(
                NavDay(day, None, None, None, None, None, "unknown", reason or "unread")
            )
            continue
        flows = close_flows(previous_legs, legs)
        opening = state is None or state.units <= 0 or state.nav_micro <= 0
        step = unit_step(
            state,
            nav_micro=flows.nav_micro,
            flow_micro=flows.flow_micro,
            gas_micro=gas.get(day, 0),
            flow_unknown=flows.unknown,
            open_price=last_price,
        )
        if step.unit_price is not None:
            last_price = step.unit_price
        if flows.nav_micro <= 0 and opening:
            status = "empty"
        elif opening:
            status = "opened"
        elif step.unit_price is None:
            status = "unknown"
        else:
            status = "ok"
        rows.append(
            NavDay(
                day=day,
                nav_micro=flows.nav_micro,
                flow_micro=flows.flow_micro,
                units=step.units,
                unit_price=step.unit_price,
                yield_micro=step.yield_micro,
                status=status,
                reason="flow not separable from return"
                if flows.unknown and not opening
                else reason,
            )
        )
        previous_legs = legs
        state = LedgerState(nav_micro=flows.nav_micro, units=step.units)
    return rows


def div_round(n: int, d: int) -> int:
    """``n / d`` rounded half to even; ``d`` must be positive."""
    q, r = divmod(abs(n), d)
    twice = 2 * r
    if twice > d or (twice == d and q % 2 == 1):
        q += 1
    return q if n >= 0 else -q
