"""The NAV backfill against Postgres, with a scripted chain."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import Engine, delete, select
from sqlalchemy.orm import Session

from oa_server.dashboard import Dashboard, book_view
from oa_server.db.models import ChainCloseRow, JobRunRow, NavDayRow, PositionCloseRow
from oa_server.nav_job import backfill
from open_allocator.exec.chain_book import (
    ArchiveUnavailable,
    ChainClose,
    ChainReadError,
    HeldInstrument,
    InstrumentClose,
)

ACCOUNT = "0x" + "5a" * 20
START = date(2026, 9, 1)
TODAY = date(2026, 9, 6)  # closes 09-01 .. 09-05
VAULT = "0x" + "11" * 32
PT = "0x" + "22" * 32

INSTRUMENTS = [
    HeldInstrument(VAULT, 8453, "Morpho", "USDC", "0x" + "11" * 20, None, 6),
    HeldInstrument(PT, 42161, "Pendle", "sUSDai", "0x" + "22" * 20, None, 18),
]


def position(
    instrument_id: str, chain_id: int, shares: int, usd: int
) -> InstrumentClose:
    return InstrumentClose(
        instrument_id=instrument_id,
        chain_id=chain_id,
        protocol="Morpho",
        symbol="USDC",
        shares_raw=shares,
        base_shares_raw=shares,
        underlying_raw=usd,
        decimals=6,
        price_usd=Decimal(1),
        price_source="numeraire",
        usd_micro=usd,
    )


class ScriptedChain:
    """A Base vault that grows 0.01 a day, and an Arbitrum chain holding
    nothing; `refuse` makes a (chain, day) raise instead."""

    def __init__(self) -> None:
        self.reads: list[tuple[int, date]] = []
        self.refuse: dict[tuple[int, date], Exception] = {}

    def __call__(self, chain_id: int, day: date, **_: Any) -> ChainClose:
        self.reads.append((chain_id, day))
        if (chain_id, day) in self.refuse:
            raise self.refuse[(chain_id, day)]
        n = (day - START).days
        positions: tuple[InstrumentClose, ...] = ()
        if chain_id == 8453:
            positions = (position(VAULT, 8453, 1_000_000, 100_000_000 + n * 10_000),)
        return ChainClose(chain_id, 1_000 + n, 1_788_000_000 + n * 86_400, positions)


def run(engine: Engine, chain: ScriptedChain, **kwargs: Any):
    return backfill(
        engine,
        today=TODAY,
        account=ACCOUNT,
        instruments=INSTRUMENTS,
        read_chain_day=chain,
        first_day=lambda *_: (START, []),
        **kwargs,
    )


def nav_rows(engine: Engine) -> list[tuple[Any, ...]]:
    with Session(engine) as session:
        return [
            (
                r.day,
                r.nav_micro,
                r.flow_micro,
                r.units,
                r.unit_price,
                r.yield_micro,
                r.status,
            )
            for r in session.scalars(select(NavDayRow).order_by(NavDayRow.day))
        ]


def test_backfill_reads_every_closed_day_once_and_derives_nav(engine: Engine) -> None:
    chain = ScriptedChain()
    result = run(engine, chain)
    assert result.read == 10  # five days, two chains
    assert result.errors == {}
    rows = nav_rows(engine)
    assert [r[0] for r in rows] == [START + timedelta(days=n) for n in range(5)]
    assert [r[-1] for r in rows] == ["opened", "ok", "ok", "ok", "ok"]
    assert rows[1][4] == Decimal(100_010_000_000_000)  # 100.01 at 1e-12
    assert rows[1][5] == 10_000

    # Again: nothing new to read, and the same rows.
    chain.reads.clear()
    again = run(engine, chain)
    assert again.read == 0 and chain.reads == []
    assert nav_rows(engine) == rows
    with Session(engine) as session:
        runs = session.scalars(select(JobRunRow.status)).all()
    assert runs == ["ok", "ok"]


def test_a_deleted_day_is_read_again_and_the_history_is_unchanged(
    engine: Engine,
) -> None:
    chain = ScriptedChain()
    run(engine, chain)
    before = nav_rows(engine)
    with Session(engine) as session, session.begin():
        for model in (PositionCloseRow, ChainCloseRow):
            session.execute(delete(model).where(model.day == date(2026, 9, 3)))
    chain.reads.clear()
    run(engine, chain)
    assert sorted(chain.reads) == [(8453, date(2026, 9, 3)), (42161, date(2026, 9, 3))]
    assert nav_rows(engine) == before


def test_an_unread_chain_is_a_gap_and_the_next_run_fills_it(engine: Engine) -> None:
    chain = ScriptedChain()
    chain.refuse[(42161, date(2026, 9, 3))] = ChainReadError("timeout")
    result = run(engine, chain)
    assert result.errors == {42161: "timeout"}
    rows = nav_rows(engine)
    assert rows[2][1] is None and rows[2][-1] == "unknown"
    # The day after the gap is read against the last day read.
    assert rows[3][-1] == "ok"

    chain.refuse.clear()
    run(engine, chain)
    assert [r[-1] for r in nav_rows(engine)] == ["opened", "ok", "ok", "ok", "ok"]


def test_no_archive_state_stops_asking_for_older_days(engine: Engine) -> None:
    chain = ScriptedChain()
    chain.refuse[(42161, date(2026, 9, 3))] = ArchiveUnavailable("pruned")
    result = run(engine, chain)
    arbitrum = sorted(day for chain_id, day in chain.reads if chain_id == 42161)
    assert arbitrum == [date(2026, 9, 3), date(2026, 9, 4), date(2026, 9, 5)]
    assert "RPC_URL_42161" in result.errors[42161]
    statuses = [r[-1] for r in nav_rows(engine)]
    assert statuses == ["unknown", "unknown", "unknown", "opened", "ok"]


def test_an_unvalued_position_is_stored_and_read_again_next_run(engine: Engine) -> None:
    chain = ScriptedChain()
    original = chain.__call__

    def unpriced(chain_id: int, day: date, **kwargs: Any) -> ChainClose:
        close = original(chain_id, day, **kwargs)
        if chain_id == 8453 and day == date(2026, 9, 2):
            return ChainClose(
                close.chain_id, close.block, close.block_timestamp, (), "no price"
            )
        return close

    run(engine, unpriced)  # type: ignore[arg-type]
    assert nav_rows(engine)[1][-1] == "unknown"
    chain.reads.clear()
    run(engine, chain)
    assert chain.reads == [(8453, date(2026, 9, 2))]
    assert nav_rows(engine)[1][-1] == "ok"


def test_the_start_day_is_kept_unless_the_operator_moves_it(engine: Engine) -> None:
    chain = ScriptedChain()
    asked: list[int] = []

    def first_day(*_: Any) -> tuple[date, list[str]]:
        asked.append(1)
        return START, []

    for _ in range(2):
        backfill(
            engine,
            today=TODAY,
            account=ACCOUNT,
            instruments=INSTRUMENTS,
            read_chain_day=chain,
            first_day=first_day,
        )
    assert asked == [1]
    run(engine, chain, since=date(2026, 9, 4))
    assert [r[0] for r in nav_rows(engine)] == [date(2026, 9, 4), date(2026, 9, 5)]


def test_the_dashboard_reports_the_series_and_coverage(engine: Engine) -> None:
    run(engine, ScriptedChain())
    board = Dashboard(engine, read_book=dict, account=lambda: ACCOUNT)
    nav = board.nav()
    assert nav.start_day == START
    assert [c.chain_id for c in nav.chains] == [8453, 42161]
    assert nav.chains[0].days_read == 5
    assert nav.summary.unit_price == pytest.approx(100.04)
    assert nav.summary.yield_usd == pytest.approx(0.04)
    assert nav.summary.since == START
    assert nav.last_run is not None and nav.last_run.status == "ok"


def test_book_view_aggregates_the_positions_payload() -> None:
    from datetime import UTC, datetime

    view = book_view(
        {
            "address": ACCOUNT,
            "holdings": [
                {
                    "instrument_id": "a",
                    "protocol": "Morpho",
                    "chain_id": 8453,
                    "symbol": "USDC",
                    "usd_value": 75.0,
                    "current_apy": 8.0,
                },
                {
                    "instrument_id": "b",
                    "protocol": "Fluid",
                    "chain_id": 42161,
                    "symbol": "USDC",
                    "usd_value": 25.0,
                    "current_apy": None,
                },
            ],
            "idle_balances": [{"chain_id": 8453, "usd_value": 1.5}],
            "warnings": [],
        },
        read_at=datetime(2026, 9, 6, tzinfo=UTC),
    )
    assert (view.total_usd, view.deployed_usd, view.idle_usd) == (101.5, 100.0, 1.5)
    assert view.blended_apy == 8.0  # only positions reporting an APY
    assert view.effective_positions == pytest.approx(1 / (0.75**2 + 0.25**2))
    assert [s.label for s in view.by_chain] == ["Base", "Arbitrum One"]


def test_each_positions_share_of_the_return_is_rebuilt_with_nav(
    engine: Engine,
) -> None:
    run(engine, ScriptedChain())
    nav = Dashboard(engine, read_book=dict, account=lambda: ACCOUNT).nav()
    (share,) = nav.by_position
    assert (share.instrument_id, share.chain, share.unknown_days) == (
        VAULT,
        "Base",
        0,
    )
    assert share.yield_usd == pytest.approx(nav.summary.yield_usd)
    assert [(p.protocol, p.yield_usd) for p in nav.by_protocol] == [
        ("Morpho", pytest.approx(0.04))
    ]


class LoopChain(ScriptedChain):
    """The Base vault, and a loop on Arbitrum whose equity grows 0.02 a day and
    whose debt is resized on 09-03."""

    def __call__(self, chain_id: int, day: date, **kwargs: Any) -> ChainClose:
        close = super().__call__(chain_id, day, **kwargs)
        if chain_id != 42161:
            return close
        n = (day - START).days
        loop = InstrumentClose(
            instrument_id=PT,
            chain_id=42161,
            protocol="Aave",
            symbol="USDC",
            shares_raw=300_000_000,
            base_shares_raw=300_000_000,
            underlying_raw=50_000_000 + n * 20_000,
            decimals=6,
            price_usd=Decimal(1),
            price_source="numeraire",
            usd_micro=50_000_000 + n * 20_000,
            debt_shares_raw=250_000_000 if day < date(2026, 9, 3) else 260_000_000,
        )
        return ChainClose(chain_id, close.block, close.block_timestamp, (loop,))


def test_a_resized_loop_leaves_that_day_unknown_and_stores_its_debt(
    engine: Engine,
) -> None:
    run(engine, LoopChain())
    with Session(engine) as session:
        debts = session.scalars(
            select(PositionCloseRow.debt_shares_raw)
            .where(PositionCloseRow.chain_id == 42161)
            .order_by(PositionCloseRow.day)
        ).all()
    assert debts[:3] == [250_000_000, 250_000_000, 260_000_000]
    statuses = {row[0]: row[6] for row in nav_rows(engine)}
    assert statuses[date(2026, 9, 3)] == "unknown"
    assert statuses[date(2026, 9, 4)] == "ok"
    nav = Dashboard(engine, read_book=dict, account=lambda: ACCOUNT).nav()
    loop = next(p for p in nav.by_position if p.instrument_id == PT)
    # 09-01→02 and 09-03→04→05 are split; the resize day is not.
    assert loop.unknown_days == 1
    assert loop.yield_usd == pytest.approx(0.06)
