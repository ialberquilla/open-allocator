"""The book at a past day's close, read from chain, and the NAV it gives.

The server stores these closes and derives its NAV history from them; nothing
here keeps state. A close is a fact about a finished day, so reading it again
gives the same answer: that is what makes a backfill safe to repeat.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime

from open_allocator.core.nav import NavDay, PositionClose, derive_nav
from open_allocator.exec import chain_book
from open_allocator.exec.chain_book import (
    ArchiveUnavailable,
    Block,
    ChainClose,
    ChainReadError,
    HeldInstrument,
    Prices,
    Rpc,
)
from open_allocator.exec.client import OneTxClient
from open_allocator.exec.config import AllocatorConfig, ReadOnlyOneTxConfig
from open_allocator.service._common import signer_address


def book_account() -> str:
    """The account whose book is tracked: the configured signer (the Safe)."""
    return signer_address(AllocatorConfig())


def listed_instruments(
    held_ids: Iterable[str] = (), *, client: object | None = None
) -> list[HeldInstrument]:
    """Every instrument 1Tx lists, plus any of ``held_ids`` it no longer lists.

    A matured PT leaves the listing while it is still held until redeemed, so
    instruments seen in earlier closes are read back by id.
    """
    if client is None:
        with OneTxClient(ReadOnlyOneTxConfig()) as owned:
            return listed_instruments(held_ids, client=owned)
    rows: list[object] = []
    response = client.list_instruments()  # type: ignore[attr-defined]
    rows.extend(response.data)
    while response.pagination is not None and response.pagination.has_more:
        page = response.pagination
        response = client.list_instruments(  # type: ignore[attr-defined]
            limit=page.limit, offset=page.offset + page.limit
        )
        rows.extend(response.data)
    seen = {str(getattr(row, "instrument_id", "")).lower() for row in rows}
    for instrument_id in sorted({i.lower() for i in held_ids} - seen):
        try:
            rows.append(client.get_instrument(instrument_id))  # type: ignore[attr-defined]
        except Exception:
            continue
    instruments = []
    for row in rows:
        held = _held(row)
        if held is not None:
            instruments.append(held)
    return instruments


def _held(row: object) -> HeldInstrument | None:
    yield_token = getattr(row, "yield_token_address", None)
    if not yield_token:
        return None
    return HeldInstrument(
        instrument_id=str(row.instrument_id),  # type: ignore[attr-defined]
        chain_id=int(row.chain_id),  # type: ignore[attr-defined]
        protocol=str(row.protocol),  # type: ignore[attr-defined]
        symbol=str(getattr(row, "token_symbol", None) or "?"),
        yield_token=str(yield_token),
        underlying_token=getattr(row, "token_address", None),
        underlying_decimals=getattr(row, "token_decimals", None),
        protocol_address=getattr(row, "protocol_address", None),
    )


def chains_of(instruments: Iterable[HeldInstrument]) -> list[int]:
    return sorted({instrument.chain_id for instrument in instruments})


def read_chain_day(
    chain_id: int,
    day: date,
    *,
    account: str,
    instruments: Sequence[HeldInstrument],
    rpc: Rpc | None = None,
    prices: Prices | None = None,
    latest: Block | None = None,
) -> ChainClose:
    """The account's positions on ``chain_id`` at ``day``'s last block.

    Raises `ArchiveUnavailable` when the RPC keeps no state that old, and
    `ChainReadError` for any other failed read: the day is then not known.
    """
    rpc = rpc or chain_book.rpc_for(chain_id)
    if rpc is None:
        raise ChainReadError(f"no RPC for chain {chain_id}; set RPC_URL_{chain_id}")
    latest = latest or chain_book.get_block(rpc, "latest")
    close_at = chain_book.day_close_timestamp(day)
    if latest.timestamp <= close_at:
        raise ChainReadError(f"{day} has not closed on chain {chain_id}")
    block = chain_book.block_at(rpc, close_at, latest=latest)
    return chain_book.read_chain_close(
        rpc,
        chain_id=chain_id,
        block=block,
        account=account,
        instruments=instruments,
        prices=prices or chain_book.LlamaPrices(),
    )


def first_day(account: str, chain_ids: Sequence[int]) -> tuple[date | None, list[str]]:
    """The UTC day the account's code first appears on any of ``chain_ids``.

    Returns the day (None when it is deployed nowhere that could be searched)
    and a note per chain that could not be searched.
    """
    days: list[date] = []
    notes: list[str] = []
    for chain_id in chain_ids:
        rpc = chain_book.rpc_for(chain_id)
        if rpc is None:
            notes.append(f"chain {chain_id}: no RPC")
            continue
        try:
            latest = chain_book.get_block(rpc, "latest")
            number = chain_book.deployment_block(rpc, account, latest=latest)
            if number is not None:
                stamp = chain_book.get_block(rpc, number).timestamp
                days.append(datetime.fromtimestamp(stamp, UTC).date())
        except ArchiveUnavailable:
            notes.append(
                f"chain {chain_id}: the RPC keeps no old state; "
                f"set RPC_URL_{chain_id} to an archive node"
            )
        except ChainReadError as error:
            notes.append(f"chain {chain_id}: {error}")
    return (min(days) if days else None), notes


def nav_series(
    days: Iterable[
        tuple[str, Mapping[int, Sequence[PositionClose]] | None, str | None]
    ],
) -> list[NavDay]:
    """The NAV series from per-day closes, every chain of a day merged.

    A day whose close is None (some chain unread or unvalued) is a gap.
    """
    return derive_nav(
        (
            day,
            None
            if by_chain is None
            else [leg for legs in by_chain.values() for leg in legs],
            reason,
        )
        for day, by_chain, reason in days
    )
