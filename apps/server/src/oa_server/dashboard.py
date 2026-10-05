"""What the dashboard pages read: the live book, the shelf, the NAV history, job
runs.

Every number a page shows is computed here or in the library; the web app only
formats. The book is read live through the service layer (cached briefly, it
takes seconds). The shelf is discovery with its metric history, which takes
about a minute, so it is read on start and hourly and served from memory. The
NAV history comes from the tables the backfill writes.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from oa_server import nav_job
from oa_server.db.models import ChainCloseRow, JobRunRow, NavAccountRow, NavDayRow
from oa_server.schemas import (
    BookPosition,
    BookResponse,
    BookSlice,
    IdleBalance,
    JobRun,
    NavChain,
    NavPoint,
    NavResponse,
    NavSummary,
    ShelfResponse,
    ShelfVault,
)
from open_allocator.core.nav import ONE, ONE_USD
from open_allocator.core.types import Unknown, Vault
from open_allocator.exec import chains
from open_allocator.service import nav as nav_service
from open_allocator.service import universe
from open_allocator.service.positions import positions as read_positions

JsonObject = dict[str, Any]

BOOK_TTL_SECONDS = 60
SHELF_TTL_SECONDS = 3600
# How often the server backfills, and reads the shelf, on its own once started.
BACKFILL_INTERVAL_SECONDS = 3600
SHELF_INTERVAL_SECONDS = 3600

ShelfRead = tuple[list[Vault], list[JsonObject]]


def read_shelf() -> ShelfRead:
    """The scored universe with its metric history, and discovery's warnings."""
    warnings: list[JsonObject] = []
    vaults = universe.discover_vaults(enrich=True, on_warning=warnings.append)
    return vaults, warnings


def book_view(payload: JsonObject, *, read_at: datetime) -> BookResponse:
    """The `positions` payload, with the aggregates the pages show."""
    holdings = payload.get("holdings") or []
    deployed = sum(float(h.get("usd_value") or 0) for h in holdings)
    idle = [
        IdleBalance(
            chain_id=int(b["chain_id"]),
            chain=chains.chain_name(int(b["chain_id"])),
            usd=float(b.get("usd_value") or 0),
        )
        for b in payload.get("idle_balances") or []
    ]
    idle_usd = sum(b.usd for b in idle)
    positions = []
    for h in holdings:
        usd = float(h.get("usd_value") or 0)
        levered = h.get("levered") or None
        positions.append(
            BookPosition(
                instrument_id=h["instrument_id"],
                protocol=h["protocol"],
                chain_id=int(h["chain_id"]),
                chain=chains.chain_name(int(h["chain_id"])),
                symbol=h.get("symbol") or "?",
                name=h.get("description") or h.get("yield_token_symbol") or h["symbol"],
                yield_token_symbol=h.get("yield_token_symbol"),
                usd=usd,
                weight=usd / deployed if deployed > 0 else 0.0,
                apy=h.get("current_apy"),
                leverage=(levered or {}).get("leverage"),
            )
        )
    positions.sort(key=lambda p: p.usd, reverse=True)
    priced = [p for p in positions if p.apy is not None]
    priced_usd = sum(p.usd for p in priced)
    blended = (
        sum(p.usd * float(p.apy or 0) for p in priced) / priced_usd
        if priced_usd > 0
        else None
    )
    weights = [p.weight for p in positions if p.weight > 0]
    return BookResponse(
        account=payload.get("address") or "",
        read_at=read_at,
        total_usd=deployed + idle_usd,
        deployed_usd=deployed,
        idle_usd=idle_usd,
        blended_apy=blended,
        income_per_year_usd=None if blended is None else priced_usd * blended / 100,
        effective_positions=1 / sum(w * w for w in weights) if weights else None,
        positions=positions,
        idle=idle,
        by_protocol=_slices(positions, lambda p: p.protocol, deployed),
        by_chain=_slices(positions, lambda p: p.chain, deployed),
        warnings=[str(w) for w in payload.get("warnings") or []],
    )


def shelf_view(read: ShelfRead, *, read_at: datetime) -> ShelfResponse:
    """The shelf as `list-vaults` describes it, best score first."""
    vaults, warnings = read
    scores = universe.score_by_instrument(vaults)
    rows = []
    for vault in vaults:
        summary = universe.vault_summary(vault, scores[vault.instrument_id])
        metrics = summary["risk_metrics"]
        rows.append(
            ShelfVault(
                instrument_id=vault.instrument_id,
                protocol=vault.protocol,
                chain_id=vault.chain_id,
                chain=chains.chain_name(vault.chain_id),
                asset=vault.asset,
                name=vault.description or vault.asset,
                yield_token_symbol=vault.yield_token_symbol,
                curator=_known(vault.curator),
                sector=vault.sector,
                apy=summary["apy"],
                base_apy=summary["base_apy"],
                reward_apy=summary["reward_apy"],
                priced_reward_apy=_known(summary["priced_reward_apy"]),
                reward_dependence=_known(summary["reward_dependence"]),
                tvl_usd=summary["tvl_usd"],
                levered=summary["levered"],
                max_leverage=summary["max_leverage"],
                maturity=vault.maturity,
                days_to_maturity=summary["days_to_maturity"],
                score=summary["score"],
                history_days=_known(metrics.get("history_days")),
                sharpe=_known(metrics.get("sharpe")),
                max_drawdown=_known(metrics.get("max_drawdown")),
                volatility=_known(metrics.get("volatility")),
                realized_apy=_known(metrics.get("realized_apy")),
                delivery_gap=_known(metrics.get("delivery_gap")),
            )
        )
    rows.sort(key=lambda row: row.score, reverse=True)
    return ShelfResponse(
        read_at=read_at, vaults=rows, warnings=[_warning(w) for w in warnings]
    )


def _known(value: Any) -> Any:
    return None if value == Unknown else value


def _warning(warning: JsonObject) -> str:
    skipped = warning.get("instruments")
    if warning.get("warning") == "skipped_instruments" and isinstance(skipped, list):
        names = "; ".join(f"{s['instrument_id']}: {s['reason']}" for s in skipped)
        return f"{len(skipped)} instruments 1Tx lists could not be read ({names})"
    return json.dumps(warning, sort_keys=True, default=str)


def _slices(
    positions: list[BookPosition], key: Callable[[BookPosition], str], total: float
) -> list[BookSlice]:
    sums: dict[str, float] = {}
    for p in positions:
        sums[key(p)] = sums.get(key(p), 0.0) + p.usd
    return [
        BookSlice(label=label, usd=usd, weight=usd / total if total > 0 else 0.0)
        for label, usd in sorted(sums.items(), key=lambda item: item[1], reverse=True)
    ]


class Dashboard:
    def __init__(
        self,
        engine: Engine,
        *,
        read_book: Callable[[], JsonObject] = read_positions,
        read_shelf: Callable[[], ShelfRead] = read_shelf,
        account: Callable[[], str] = nav_service.book_account,
        run_backfill: Callable[[], object] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._engine = engine
        self._read_book = read_book
        self._read_shelf = read_shelf
        self._account = account
        self._run_backfill = run_backfill or (lambda: nav_job.backfill(engine))
        self._clock = clock
        self._book: tuple[float, BookResponse] | None = None
        self._book_lock = threading.Lock()
        self._shelf: tuple[float, ShelfResponse] | None = None
        self._shelf_lock = threading.Lock()
        self._backfill_lock = threading.Lock()

    def book(self, *, refresh: bool = False) -> BookResponse:
        with self._book_lock:
            cached = self._book
            if (
                cached
                and not refresh
                and time.monotonic() - cached[0] < BOOK_TTL_SECONDS
            ):
                return cached[1]
            view = book_view(self._read_book(), read_at=self._clock())
            self._book = (time.monotonic(), view)
            return view

    def shelf(self, *, refresh: bool = False) -> ShelfResponse:
        """The cached shelf, read first if it is missing, stale or asked for.

        A read already under way is waited for rather than repeated.
        """
        asked = time.monotonic()
        with self._shelf_lock:
            cached = self._shelf
            if cached and (
                cached[0] >= asked
                or (not refresh and asked - cached[0] < SHELF_TTL_SECONDS)
            ):
                return cached[1]
            view = shelf_view(self._read_shelf(), read_at=self._clock())
            self._shelf = (time.monotonic(), view)
            return view

    def backfill(self) -> bool:
        """Run one backfill unless one is already running here; False if it was."""
        if not self._backfill_lock.acquire(blocking=False):
            return False
        try:
            self._run_backfill()
        finally:
            self._backfill_lock.release()
        return True

    @property
    def backfilling(self) -> bool:
        return self._backfill_lock.locked()

    def nav(self) -> NavResponse:
        account = self._account().lower()
        with Session(self._engine) as session:
            days = session.scalars(
                select(NavDayRow)
                .where(NavDayRow.account == account)
                .order_by(NavDayRow.day)
            ).all()
            start = session.get(NavAccountRow, account)
            closes = session.scalars(
                select(ChainCloseRow).where(ChainCloseRow.account == account)
            ).all()
            last = session.scalars(
                select(JobRunRow)
                .where(JobRunRow.job == nav_job.JOB)
                .order_by(JobRunRow.id.desc())
                .limit(1)
            ).first()
        points = [_point(row) for row in days]
        detail = (last.detail or {}) if last else {}
        errors = detail.get("errors") or {}
        chain_ids = sorted(
            {c.chain_id for c in closes}
            | {int(c) for c in detail.get("chain_ids") or []}
        )
        coverage = []
        for chain_id in chain_ids:
            mine = [c for c in closes if c.chain_id == chain_id]
            ok = [c.day for c in mine if c.status == "ok"]
            coverage.append(
                NavChain(
                    chain_id=chain_id,
                    chain=chains.chain_name(chain_id),
                    days_read=len(ok),
                    days_unknown=len(mine) - len(ok),
                    first_day=min(ok) if ok else None,
                    last_day=max(ok) if ok else None,
                    error=errors.get(str(chain_id)),
                )
            )
        return NavResponse(
            account=account,
            start_day=start.start_day if start else None,
            start_notes=list(start.notes or []) if start else [],
            days=points,
            chains=coverage,
            summary=_summary(points),
            last_run=_job(last) if last else None,
            backfilling=self.backfilling,
        )

    def jobs(self, limit: int = 20) -> list[JobRun]:
        with Session(self._engine) as session:
            rows = session.scalars(
                select(JobRunRow).order_by(JobRunRow.id.desc()).limit(limit)
            ).all()
        return [_job(row) for row in rows]


def _usd(micro: int | None) -> float | None:
    return None if micro is None else micro / ONE_USD


def _price(value: Decimal | None) -> float | None:
    return None if value is None else float(value / ONE)


def _point(row: NavDayRow) -> NavPoint:
    return NavPoint(
        day=row.day,
        nav_usd=_usd(row.nav_micro),
        flow_usd=_usd(row.flow_micro),
        unit_price=_price(row.unit_price),
        yield_usd=_usd(row.yield_micro),
        status=row.status,  # type: ignore[arg-type]
        reason=row.reason,
    )


def _summary(points: list[NavPoint]) -> NavSummary:
    priced = [p for p in points if p.unit_price is not None]
    # The current ledger: an earlier one that was emptied (a test deposit,
    # a full exit) is history, not the start of this record.
    first = next((p for p in reversed(points) if p.status == "opened"), None)
    last = priced[-1] if priced else None
    since: date | None = first.day if first else None
    current = [p for p in points if since is None or p.day >= since]
    total_return = None
    annualized = None
    if first and last and first.unit_price and last.unit_price:
        total_return = last.unit_price / first.unit_price - 1
        elapsed = (last.day - first.day).days
        if elapsed >= 7:
            annualized = (1 + total_return) ** (365 / elapsed) - 1
    return NavSummary(
        since=since,
        last_day=last.day if last else None,
        unit_price=last.unit_price if last else None,
        nav_usd=next(
            (p.nav_usd for p in reversed(points) if p.nav_usd is not None), None
        ),
        total_return=total_return,
        annualized_return=annualized,
        yield_usd=sum(p.yield_usd for p in current if p.yield_usd is not None),
        net_flow_usd=sum(p.flow_usd for p in current if p.flow_usd is not None),
        days=len(current),
        unknown_days=sum(1 for p in current if p.status == "unknown"),
    )


def _job(row: JobRunRow) -> JobRun:
    return JobRun(
        id=row.id,
        job=row.job,
        started_at=row.started_at,
        finished_at=row.finished_at,
        status=row.status,
        detail=row.detail,
    )
