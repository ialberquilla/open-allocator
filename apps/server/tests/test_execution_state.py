"""Execution state in Postgres: the library's state port, backed by the server's
database, and the server's services running on it."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session
from test_service_execution import Transfers, planned_bridge
from test_state import MockSigner, fs_config, run_execution

from oa_server.db.models import IdempotencyKeyRow
from oa_server.state import PostgresStateBackend
from open_allocator.core.checkpoint import write_checkpoint
from open_allocator.core.state import (
    CheckpointExists,
    CheckpointNotFound,
    StateBackend,
)
from open_allocator.service import execution as execution_service
from open_allocator.service import use_state_backend
from open_allocator.service.plan_store import InMemoryPlanStore


@pytest.fixture
def backend(engine: Engine) -> PostgresStateBackend:
    return PostgresStateBackend(engine)


@pytest.fixture
def database_state(backend: PostgresStateBackend) -> Iterator[PostgresStateBackend]:
    """The server's state for every service call, as the launcher sets it."""
    use_state_backend(backend)
    try:
        yield backend
    finally:
        use_state_backend(None)


def test_it_satisfies_the_state_port(backend: PostgresStateBackend) -> None:
    assert isinstance(backend, StateBackend)


def test_checkpoints_round_trip_once(
    backend: PostgresStateBackend,
) -> None:
    checkpoint = write_checkpoint("execute", "in_progress", {"a": 1}, backend=backend)

    assert backend.read_checkpoint(checkpoint.id) == checkpoint
    with pytest.raises(CheckpointExists):
        backend.write_checkpoint(checkpoint)
    with pytest.raises(CheckpointNotFound):
        backend.read_checkpoint("nothing-was-written-here")


def test_completed_keys_are_scoped_and_keep_the_latest_value(
    backend: PostgresStateBackend,
) -> None:
    assert not backend.is_completed("a", "leg")
    assert backend.completed_value("a", "leg") is None

    backend.mark_completed("a", "leg")
    backend.mark_completed("a", "bridge", {"state": "source_submitted"})
    backend.mark_completed("a", "bridge", {"state": "awaiting_attestation"})

    assert backend.is_completed("a", "leg")
    assert backend.completed_value("a", "leg") is None
    assert backend.completed_value("a", "bridge") == {"state": "awaiting_attestation"}
    assert not backend.is_completed("b", "leg")


def test_a_retry_on_a_wiped_filesystem_sends_nothing_again(
    backend: PostgresStateBackend,
    tmp_path: Path,
) -> None:
    """The failure the port exists for, on the server's database."""
    state_dir = tmp_path / "state"
    signer = MockSigner()

    run_execution(fs_config(state_dir, state_backend=backend), signer)
    first_run = len(signer.sent)
    shutil.rmtree(state_dir, ignore_errors=True)
    report = run_execution(fs_config(state_dir, state_backend=backend), signer)

    assert first_run > 0
    assert len(signer.sent) == first_run
    assert report.status == "success"
    assert not state_dir.exists()
    assert len(backend.read_allocation_log()) == 1


def test_a_transfer_advances_through_the_database_not_the_files(
    database_state: PostgresStateBackend,
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Planning reads the transfer under way and approval writes it, both from
    the database, while the files the config points at stay untouched."""
    transfers = Transfers()
    transfers.install(monkeypatch, tmp_path)
    store = InMemoryPlanStore()

    burn = execution_service.propose(store, planned_bridge())
    execution_service.apply_approved(store, burn["plan_hash"])
    transfers.circle.ready = True
    advance = planned_bridge()
    response = execution_service.propose(store, advance)
    settled = execution_service.apply_approved(store, response["plan_hash"])

    assert advance["plan"]["existing"]["state"] == "awaiting_attestation"
    assert settled["status"] == "success"
    assert transfers.kinds() == [["approve", "bridge_burn"], ["cctp_receive"]]
    assert not (tmp_path / "idempotency.json").exists()
    with Session(engine) as session:
        keys = session.execute(select(func.count()).select_from(IdempotencyKeyRow))
        assert keys.scalar_one() > 0
