"""The NAV backfill: read every closed day the database lacks, then rebuild NAV.

Idempotent by construction. A day's close is read at that day's last block,
which never changes, so a stored close is never read again; the NAV series is
derived from the stored closes alone and rewritten whole, so the same closes
always give the same rows. Running it twice, on any machine, after any gap,
converges on the same history.

Only a definitive answer is stored. A read the RPC could not serve (no archive
state, a timeout) leaves the day missing, to be tried on the next run; it shows
as a gap, never as a zero.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Engine, delete, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from oa_server.db.models import (
    ChainCloseRow,
    JobRunRow,
    NavAccountRow,
    NavDayRow,
    PositionCloseRow,
)
from open_allocator.core.nav import PositionClose
from open_allocator.exec.chain_book import (
    ArchiveUnavailable,
    ChainClose,
    ChainReadError,
    HeldInstrument,
)
from open_allocator.service import nav as nav_service

JOB = "nav"
# One backfill at a time, across processes.
_LOCK_KEY = 0x0A_4E_41_56

ReadChainDay = Callable[..., ChainClose]


@dataclass
class BackfillResult:
    account: str
    start_day: date | None
    end_day: date
    chain_ids: list[int] = field(default_factory=list)
    read: int = 0
    stored: dict[int, int] = field(default_factory=dict)
    errors: dict[int, str] = field(default_factory=dict)
    nav_days: int = 0
    notes: list[str] = field(default_factory=list)
    skipped: bool = False

    def payload(self) -> dict[str, Any]:
        return {
            "account": self.account,
            "start_day": self.start_day.isoformat() if self.start_day else None,
            "end_day": self.end_day.isoformat(),
            "chain_ids": self.chain_ids,
            "read": self.read,
            "stored": {str(k): v for k, v in self.stored.items()},
            "errors": {str(k): v for k, v in self.errors.items()},
            "nav_days": self.nav_days,
            "notes": self.notes,
            "skipped": self.skipped,
        }


def backfill(
    engine: Engine,
    *,
    since: date | None = None,
    today: date | None = None,
    account: str | None = None,
    instruments: Sequence[HeldInstrument] | None = None,
    read_chain_day: ReadChainDay = nav_service.read_chain_day,
    first_day: Callable[..., tuple[date | None, list[str]]] = nav_service.first_day,
) -> BackfillResult:
    """Fill every missing (day, chain) close up to yesterday, then rebuild NAV."""
    today = today or datetime.now(UTC).date()
    end = today - timedelta(days=1)
    account = (account or nav_service.book_account()).lower()
    result = BackfillResult(account=account, start_day=None, end_day=end)

    with engine.connect() as lock:
        if not lock.execute(
            text("select pg_try_advisory_lock(:k)"), {"k": _LOCK_KEY}
        ).scalar():
            result.skipped = True
            result.notes.append("another backfill is running")
            return result
        try:
            run_id = _start_run(engine)
            try:
                _backfill(
                    engine,
                    result,
                    since=since,
                    instruments=instruments,
                    read_chain_day=read_chain_day,
                    first_day=first_day,
                )
            except Exception as error:
                _finish_run(
                    engine, run_id, "failed", {**result.payload(), "error": str(error)}
                )
                raise
            status = "ok" if not result.errors else "partial"
            _finish_run(engine, run_id, status, result.payload())
        finally:
            lock.execute(text("select pg_advisory_unlock(:k)"), {"k": _LOCK_KEY})
    return result


def _backfill(
    engine: Engine,
    result: BackfillResult,
    *,
    since: date | None,
    instruments: Sequence[HeldInstrument] | None,
    read_chain_day: ReadChainDay,
    first_day: Callable[..., tuple[date | None, list[str]]],
) -> None:
    account = result.account
    with Session(engine) as session:
        held_ids = session.scalars(
            select(PositionCloseRow.instrument_id)
            .where(PositionCloseRow.account == account)
            .distinct()
        ).all()
        stored_chains = session.scalars(
            select(ChainCloseRow.chain_id)
            .where(ChainCloseRow.account == account)
            .distinct()
        ).all()
    if instruments is None:
        instruments = nav_service.listed_instruments(held_ids)
    chain_ids = sorted(set(nav_service.chains_of(instruments)) | set(stored_chains))
    result.chain_ids = chain_ids

    start = _start_day(engine, account, since, chain_ids, first_day, result)
    result.start_day = start
    if start is None or start > result.end_day:
        _rebuild_nav(engine, result)
        return

    days = [start + timedelta(days=n) for n in range((result.end_day - start).days + 1)]
    for chain_id in chain_ids:
        with Session(engine) as session:
            done = set(
                session.scalars(
                    select(ChainCloseRow.day).where(
                        ChainCloseRow.account == account,
                        ChainCloseRow.chain_id == chain_id,
                        ChainCloseRow.status == "ok",
                    )
                ).all()
            )
        # Newest first: an RPC without archive state answers recent days and
        # then stops, and nothing older than that first refusal is asked.
        for day in sorted((d for d in days if d not in done), reverse=True):
            try:
                close = read_chain_day(
                    chain_id, day, account=account, instruments=instruments
                )
            except ArchiveUnavailable:
                result.errors[chain_id] = (
                    f"no state before {day + timedelta(days=1)} on this RPC; "
                    f"set RPC_URL_{chain_id} to an archive node"
                )
                break
            except ChainReadError as error:
                result.errors[chain_id] = str(error)
                continue
            result.read += 1
            _store_close(engine, account, day, close)
            result.stored[chain_id] = result.stored.get(chain_id, 0) + 1
    _rebuild_nav(engine, result)


def _start_day(
    engine: Engine,
    account: str,
    since: date | None,
    chain_ids: Sequence[int],
    first_day: Callable[..., tuple[date | None, list[str]]],
    result: BackfillResult,
) -> date | None:
    with Session(engine) as session, session.begin():
        row = session.get(NavAccountRow, account)
        if since is not None:
            if row is None:
                session.add(
                    NavAccountRow(
                        account=account, start_day=since, notes=["set by operator"]
                    )
                )
            else:
                row.start_day, row.notes = since, ["set by operator"]
            return since
        if row is not None:
            result.notes.extend(row.notes or [])
            return row.start_day
    found, notes = first_day(account, chain_ids)
    result.notes.extend(notes)
    if found is None:
        return None
    with Session(engine) as session, session.begin():
        session.add(NavAccountRow(account=account, start_day=found, notes=notes))
    return found


def _store_close(engine: Engine, account: str, day: date, close: ChainClose) -> None:
    """One chain's close and its positions, replaced together."""
    with Session(engine) as session, session.begin():
        key = (
            PositionCloseRow.account == account,
            PositionCloseRow.day == day,
            PositionCloseRow.chain_id == close.chain_id,
        )
        session.execute(delete(PositionCloseRow).where(*key))
        values = {
            "account": account,
            "day": day,
            "chain_id": close.chain_id,
            "block": close.block,
            "block_at": datetime.fromtimestamp(close.block_timestamp, UTC),
            "status": "ok" if close.ok else "unknown",
            "reason": close.reason,
        }
        session.execute(
            insert(ChainCloseRow)
            .values(**values)
            .on_conflict_do_update(
                index_elements=["account", "day", "chain_id"],
                set_={
                    k: v
                    for k, v in values.items()
                    if k not in {"account", "day", "chain_id"}
                }
                | {"read_at": text("now()")},
            )
        )
        session.add_all(
            PositionCloseRow(
                account=account,
                day=day,
                chain_id=close.chain_id,
                instrument_id=p.instrument_id.lower(),
                protocol=p.protocol,
                symbol=p.symbol,
                shares_raw=Decimal(p.shares_raw),
                base_shares_raw=_decimal(p.base_shares_raw),
                underlying_raw=_decimal(p.underlying_raw),
                decimals=p.decimals,
                price_usd=p.price_usd,
                price_source=p.price_source,
                usd_micro=p.usd_micro,
                reason=p.reason,
            )
            for p in close.positions
        )


def _rebuild_nav(engine: Engine, result: BackfillResult) -> None:
    """Derive the whole series from the stored closes and replace it."""
    account = result.account
    with Session(engine) as session, session.begin():
        start = result.start_day
        session.execute(delete(NavDayRow).where(NavDayRow.account == account))
        if start is None:
            return
        closes = (
            session.execute(
                select(ChainCloseRow).where(
                    ChainCloseRow.account == account, ChainCloseRow.day >= start
                )
            )
            .scalars()
            .all()
        )
        positions = (
            session.execute(
                select(PositionCloseRow).where(
                    PositionCloseRow.account == account, PositionCloseRow.day >= start
                )
            )
            .scalars()
            .all()
        )
        # Every chain the book can hold on, read or not: a chain never read is
        # a gap on every day, not a chain with nothing on it.
        chain_ids = sorted(set(result.chain_ids) | {c.chain_id for c in closes})
        by_day: dict[date, dict[int, ChainCloseRow]] = {}
        for close in closes:
            by_day.setdefault(close.day, {})[close.chain_id] = close
        legs: dict[tuple[date, int], list[PositionClose]] = {}
        for p in positions:
            if p.usd_micro is None:
                continue
            legs.setdefault((p.day, p.chain_id), []).append(
                PositionClose(
                    chain_id=p.chain_id,
                    instrument_id=p.instrument_id,
                    base_shares=None
                    if p.base_shares_raw is None
                    else int(p.base_shares_raw),
                    usd_micro=p.usd_micro,
                )
            )

        series = []
        day = start
        while day <= result.end_day:
            read = by_day.get(day, {})
            missing = [c for c in chain_ids if c not in read]
            unknown = [
                read[c] for c in chain_ids if c in read and read[c].status != "ok"
            ]
            if missing or unknown or not chain_ids:
                reasons = [f"chain {c} not read" for c in missing] + [
                    f"chain {c.chain_id}: {c.reason}" for c in unknown
                ]
                series.append((day.isoformat(), None, "; ".join(reasons) or "not read"))
            else:
                series.append(
                    (
                        day.isoformat(),
                        {c: legs.get((day, c), []) for c in chain_ids},
                        None,
                    )
                )
            day += timedelta(days=1)

        rows = nav_service.nav_series(series)
        session.add_all(
            NavDayRow(
                account=account,
                day=date.fromisoformat(row.day),
                nav_micro=row.nav_micro,
                flow_micro=row.flow_micro,
                units=_decimal(row.units),
                unit_price=_decimal(row.unit_price),
                yield_micro=row.yield_micro,
                status=row.status,
                reason=row.reason,
            )
            for row in rows
        )
        result.nav_days = len(rows)


def _start_run(engine: Engine) -> int:
    with Session(engine) as session, session.begin():
        row = JobRunRow(job=JOB, started_at=datetime.now(UTC), status="running")
        session.add(row)
        session.flush()
        return row.id


def _finish_run(
    engine: Engine, run_id: int, status: str, detail: dict[str, Any]
) -> None:
    with Session(engine) as session, session.begin():
        row = session.get(JobRunRow, run_id)
        if row is not None:
            row.status, row.detail, row.finished_at = status, detail, datetime.now(UTC)


def _decimal(value: int | None) -> Decimal | None:
    return None if value is None else Decimal(value)
