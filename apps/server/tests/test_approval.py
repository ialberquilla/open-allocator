"""The Postgres plan store and approval: the approved plan runs, once."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, update
from sqlalchemy.orm import Session
from test_cli import (
    DEPOSIT_KINDS,
    execution_policy,
    install_execution_surface_mocks,
    write_execution_files,
)

from oa_server.approval import PostgresPlanStore, approve
from oa_server.db.models import PlanRow
from open_allocator.service import ServiceError
from open_allocator.service import execution as execution_service
from open_allocator.service.plan_store import plan_hash


def proposed(tmp_path: Path) -> tuple[dict[str, Any], Path]:
    """A real `execute` proposal against the fakes, and a policy that allows it."""
    allocation_path, policy_path = write_execution_files(tmp_path)
    proposal = execution_service.plan_execute(
        json.loads(allocation_path.read_text(encoding="utf-8")), policy=policy_path
    )
    return json.loads(json.dumps(proposal)), policy_path


def test_store_round_trips_and_hashes_canonically(engine: Engine) -> None:
    store = PostgresPlanStore(engine)
    stored = store.put("execute", {"b": 1, "a": [1, 2]})

    assert stored.plan_hash == plan_hash("execute", {"a": [1, 2], "b": 1})
    fetched = store.get(stored.plan_hash)
    assert fetched is not None
    assert fetched.plan == {"a": [1, 2], "b": 1}
    assert fetched.used_at is None
    assert store.get("0" * 64) is None


def test_a_plan_is_taken_once_and_stays_used(engine: Engine) -> None:
    store = PostgresPlanStore(engine)
    stored = store.put("execute", {"a": 1})

    taken = store.take(stored.plan_hash)
    with pytest.raises(ServiceError) as again:
        store.take(stored.plan_hash)
    reproposed = store.put("execute", {"a": 1})

    assert taken.used_at is not None
    assert again.value.code == "plan_used"
    assert reproposed.used_at is not None


def test_unknown_and_expired_plans_are_refused(engine: Engine) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    store = PostgresPlanStore(engine, ttl=timedelta(minutes=15), clock=lambda: now)
    stored = store.put("execute", {"a": 1})
    now += timedelta(minutes=15)

    with pytest.raises(ServiceError) as expired:
        store.take(stored.plan_hash)
    with pytest.raises(ServiceError) as unknown:
        store.take("0" * 64)

    assert expired.value.code == "plan_expired"
    assert unknown.value.code == "plan_not_found"


def test_concurrent_approvals_take_a_plan_once(engine: Engine) -> None:
    store = PostgresPlanStore(engine)
    stored = store.put("execute", {"a": 1})

    def attempt(_: int) -> str:
        try:
            store.take(stored.plan_hash)
        except ServiceError as error:
            return error.code
        return "taken"

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(attempt, range(16)))

    assert outcomes.count("taken") == 1
    assert set(outcomes) == {"taken", "plan_used"}


def test_approve_applies_the_stored_plan_once_and_records_it(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    signer = install_execution_surface_mocks(monkeypatch)
    store = PostgresPlanStore(engine)
    proposal, policy_path = proposed(tmp_path)
    response = execution_service.propose(store, proposal)
    assert signer.sent == []

    result = approve(store, response["plan_hash"], policy=policy_path)
    with pytest.raises(ServiceError) as again:
        approve(store, response["plan_hash"], policy=policy_path)

    assert result["status"] == "success"
    assert again.value.code == "plan_used"
    assert len(signer.sent) == len(DEPOSIT_KINDS)
    with Session(engine) as session:
        row = session.get(PlanRow, response["plan_hash"])
        assert row is not None
        assert row.result is not None and row.result["status"] == "success"
        # The repeated approval did not overwrite the outcome.
        assert row.error is None


def test_approve_refuses_a_plan_the_policy_no_longer_allows(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    signer = install_execution_surface_mocks(monkeypatch)
    store = PostgresPlanStore(engine)
    proposal, _ = proposed(tmp_path)
    response = execution_service.propose(store, proposal)
    strict = tmp_path / "strict.yaml"
    strict_policy = execution_policy()
    strict_policy["gates"]["max_deploy_per_cycle_usd"] = 10
    strict.write_text(json.dumps(strict_policy), encoding="utf-8")

    with pytest.raises(ServiceError) as refused:
        approve(store, response["plan_hash"], policy=strict)

    assert refused.value.code == "policy_violation"
    assert signer.sent == []
    with Session(engine) as session:
        row = session.get(PlanRow, response["plan_hash"])
        assert row is not None and row.used_at is not None
        assert row.error is not None and row.error.startswith("policy_violation")


def test_approve_refuses_a_stored_plan_that_changed_since_it_was_proposed(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    signer = install_execution_surface_mocks(monkeypatch)
    store = PostgresPlanStore(engine)
    proposal, policy_path = proposed(tmp_path)
    response = execution_service.propose(store, proposal)
    tampered = json.loads(json.dumps(proposal["plan"]))
    tampered["plan"]["steps"][-1]["data"] = "0xdeadbeef"
    with Session(engine) as session, session.begin():
        session.execute(
            update(PlanRow)
            .where(PlanRow.hash == response["plan_hash"])
            .values(plan=tampered)
        )

    with pytest.raises(ServiceError) as refused:
        approve(store, response["plan_hash"], policy=policy_path)

    assert refused.value.code == "plan_mismatch"
    assert signer.sent == []
