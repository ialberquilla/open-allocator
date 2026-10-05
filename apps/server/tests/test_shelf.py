"""The shelf view and its cache, against scripted discovery."""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from typing import Any

from oa_server.dashboard import Dashboard, ShelfRead, shelf_view
from open_allocator.core.types import Unknown, Vault

READ_AT = datetime(2026, 10, 5, tzinfo=UTC)


def vault(instrument_id: str, **updates: Any) -> Vault:
    return Vault(
        instrument_id=instrument_id,
        protocol="Morpho",
        chain_id=8453,
        asset="USDC",
        is_stablecoin=True,
        apy=5.0,
        tvl_usd=10_000_000,
        apy_series=tuple(5.0 + (i % 3) * 0.1 for i in range(90)),
    ).model_copy(update=updates)


def test_the_shelf_is_scored_best_first_and_unknown_stays_unknown() -> None:
    thin = vault("thin", apy=9.0, tvl_usd=50_000, apy_series=())
    deep = vault("deep", curator="Steakhouse", reward_dependence=0.2)

    view = shelf_view(([thin, deep], []), read_at=READ_AT)

    assert [row.instrument_id for row in view.vaults] == ["deep", "thin"]
    assert view.vaults[0].score >= view.vaults[1].score
    best, worst = view.vaults
    assert best.chain == "Base"
    assert best.curator == "Steakhouse" and best.reward_dependence == 0.2
    assert best.history_days == 90 and best.sharpe is not None
    assert best.max_drawdown is not None and best.max_drawdown <= 0
    assert best.delivery_gap is not None and best.delivery_gap <= 0
    assert worst.curator is None and worst.reward_dependence is None
    assert worst.sharpe is None and worst.realized_apy is None


def test_an_emission_priced_reward_is_not_counted() -> None:
    rewarded = vault(
        "rewarded",
        apy_base=4.0,
        apy_reward=3.0,
        reward_price_basis="emission",
        curator=Unknown,
    )

    (row,) = shelf_view(([rewarded], []), read_at=READ_AT).vaults

    assert row.reward_apy == 3.0
    assert row.priced_reward_apy is None


def test_skipped_instruments_are_reported() -> None:
    warning = {
        "warning": "skipped_instruments",
        "instruments": [{"instrument_id": "0xdead", "reason": "no asset"}],
    }

    view = shelf_view(([vault("a")], [warning]), read_at=READ_AT)

    assert view.warnings == [
        "1 instruments 1Tx lists could not be read (0xdead: no asset)"
    ]


class Discovery:
    def __init__(self) -> None:
        self.reads = 0
        self.release = threading.Event()
        self.release.set()

    def __call__(self) -> ShelfRead:
        self.reads += 1
        self.release.wait(5)
        return [vault("a")], []


def board(discovery: Discovery) -> Dashboard:
    return Dashboard(
        None,  # type: ignore[arg-type]
        read_shelf=discovery,
        clock=lambda: READ_AT,
    )


def test_the_shelf_is_read_once_and_served_from_memory() -> None:
    discovery = Discovery()
    dashboard = board(discovery)

    first = dashboard.shelf()
    second = dashboard.shelf()
    dashboard.shelf(refresh=True)

    assert first is second
    assert discovery.reads == 2


def test_a_request_during_a_read_waits_for_it_instead_of_reading_again() -> None:
    discovery = Discovery()
    discovery.release.clear()
    dashboard = board(discovery)
    reading = threading.Thread(target=lambda: dashboard.shelf(refresh=True))
    reading.start()
    while discovery.reads == 0:
        threading.Event().wait(0.01)

    waiting = threading.Thread(target=lambda: dashboard.shelf(refresh=True))
    waiting.start()
    threading.Event().wait(0.1)
    discovery.release.set()
    reading.join()
    waiting.join()

    assert discovery.reads == 1
