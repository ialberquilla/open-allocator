"""The plan/apply split: an approved plan is the plan that runs, at most once."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from test_cli import (
    DEPOSIT_KINDS,
    ExecutionOneTxClient,
    execution_policy,
    install_execution_surface_mocks,
    write_execution_files,
)

from open_allocator.exec.execute import TransactionPlanError
from open_allocator.service import ServiceError
from open_allocator.service import execution as execution_service
from open_allocator.service.plan_store import InMemoryPlanStore, plan_hash


def read_allocation(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def planned(tmp_path: Path) -> dict[str, Any]:
    allocation_path, policy_path = write_execution_files(tmp_path)
    proposal = execution_service.plan_execute(
        read_allocation(allocation_path), policy=policy_path
    )
    # Plans cross process and storage boundaries as JSON.
    return json.loads(json.dumps(proposal))


def test_applying_runs_the_stored_plan_without_planning_again(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    signer = install_execution_surface_mocks(monkeypatch)
    proposal = planned(tmp_path)
    assert signer.sent == []
    ExecutionOneTxClient.calldata_requests = []

    report = execution_service.apply_execute(
        proposal["plan"], expected_hash=proposal["plan_hash"]
    )

    assert report["status"] == "success"
    assert ExecutionOneTxClient.calldata_requests == []
    assert report["plan"] == proposal["plan"]["plan"]
    assert [step["kind"] for step in report["plan"]["steps"]] == DEPOSIT_KINDS
    assert [sent[0].data for sent in signer.sent] == [
        step["data"] for step in proposal["plan"]["plan"]["steps"]
    ]


def test_dry_run_report_is_the_plan_without_broadcast(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    signer = install_execution_surface_mocks(monkeypatch)
    proposal = planned(tmp_path)

    assert proposal["report"]["status"] == "planned"
    assert proposal["report"]["plan"] == proposal["plan"]["plan"]
    assert proposal["plan_hash"] == plan_hash("execute", proposal["plan"])
    assert signer.sent == []


def test_a_plan_that_does_not_match_its_hash_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    signer = install_execution_surface_mocks(monkeypatch)
    proposal = planned(tmp_path)
    tampered = json.loads(json.dumps(proposal["plan"]))
    tampered["plan"]["steps"][-1]["data"] = "0xdeadbeef"

    with pytest.raises(ServiceError) as raised:
        execution_service.apply_execute(tampered, expected_hash=proposal["plan_hash"])

    assert raised.value.code == "plan_mismatch"
    assert signer.sent == []


def test_a_plan_for_another_account_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    signer = install_execution_surface_mocks(monkeypatch)
    proposal = planned(tmp_path)
    other = {**proposal["plan"], "account": "0x" + "22" * 20}

    with pytest.raises(TransactionPlanError, match="built for"):
        execution_service.apply_execute(other)

    assert signer.sent == []


def test_a_plan_the_wallet_moved_past_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    signer = install_execution_surface_mocks(
        monkeypatch, idempotency_store_path=tmp_path / "idempotency.json"
    )
    proposal = planned(tmp_path)
    execution_service.apply_execute(proposal["plan"])
    sent = len(signer.sent)

    with pytest.raises(TransactionPlanError, match="stale"):
        execution_service.apply_execute(proposal["plan"])

    assert len(signer.sent) == sent


def test_an_approved_plan_runs_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    signer = install_execution_surface_mocks(monkeypatch)
    store = InMemoryPlanStore()
    response = execution_service.propose(store, planned(tmp_path))
    assert response["plan_required"] is True
    assert signer.sent == []

    report = execution_service.apply_approved(store, response["plan_hash"])
    with pytest.raises(ServiceError) as raised:
        execution_service.apply_approved(store, response["plan_hash"])

    assert report["status"] == "success"
    assert raised.value.code == "plan_used"
    assert len(signer.sent) == len(DEPOSIT_KINDS)


def test_an_expired_or_unknown_plan_is_not_applied(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    signer = install_execution_surface_mocks(monkeypatch)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    store = InMemoryPlanStore(ttl=timedelta(minutes=15), clock=lambda: now)
    response = execution_service.propose(store, planned(tmp_path))
    now += timedelta(minutes=15)

    with pytest.raises(ServiceError) as expired:
        execution_service.apply_approved(store, response["plan_hash"])
    with pytest.raises(ServiceError) as unknown:
        execution_service.apply_approved(store, "0" * 64)

    assert expired.value.code == "plan_expired"
    assert unknown.value.code == "plan_not_found"
    assert signer.sent == []


def test_approval_rechecks_the_plan_against_the_operator_policy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    signer = install_execution_surface_mocks(monkeypatch)
    proposal = planned(tmp_path)
    loose = tmp_path / "loose.yaml"
    loose.write_text(json.dumps(execution_policy()), encoding="utf-8")
    strict = tmp_path / "strict.yaml"
    strict_policy = execution_policy()
    strict_policy["gates"]["max_deploy_per_cycle_usd"] = 10
    strict.write_text(json.dumps(strict_policy), encoding="utf-8")

    refusing = InMemoryPlanStore()
    refused = execution_service.propose(refusing, proposal)
    with pytest.raises(ServiceError) as raised:
        execution_service.apply_approved(refusing, refused["plan_hash"], policy=strict)
    assert raised.value.code == "policy_violation"
    assert "max_deploy_per_cycle_usd" in raised.value.detail
    assert signer.sent == []
    # A refused plan stays used: approving it again does not retry it.
    with pytest.raises(ServiceError) as again:
        execution_service.apply_approved(refusing, refused["plan_hash"], policy=loose)
    assert again.value.code == "plan_used"

    allowing = InMemoryPlanStore()
    allowed = execution_service.propose(allowing, proposal)
    report = execution_service.apply_approved(
        allowing, allowed["plan_hash"], policy=loose
    )
    assert report["status"] == "success"


def test_store_hash_is_canonical_and_a_used_plan_stays_used() -> None:
    store = InMemoryPlanStore()
    first = store.put("execute", {"b": 1, "a": [1, 2]})
    assert first.plan_hash == plan_hash("execute", {"a": [1, 2], "b": 1})
    assert first.plan_hash != plan_hash("rebalance", {"a": [1, 2], "b": 1})

    store.take(first.plan_hash)
    again = store.put("execute", {"a": [1, 2], "b": 1})

    assert again.used_at is not None
    with pytest.raises(ServiceError):
        store.take(first.plan_hash)


def test_review_describes_the_stored_plan_in_token_units(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    install_execution_surface_mocks(monkeypatch)
    proposal = planned(tmp_path)

    review = execution_service.review_plan("execute", proposal["plan"])

    assert review["kind"] == "execute"
    assert review["account"] == proposal["plan"]["account"]
    assert [leg["instrument_id"] for leg in review["legs"]] == [
        leg["instrument_id"] for leg in proposal["plan"]["allocation"]["legs"]
    ]
    (bundle,) = review["bundles"]
    raw = proposal["plan"]["plan"]["bundles"][0]
    decimals = raw["token_in"]["decimals"]
    assert bundle["amount_in"]["raw"] == raw["amount"]
    assert bundle["amount_in"]["amount"] == str(int(raw["amount"]) // 10**decimals)
    assert bundle["amount_in"]["symbol"] == raw["token_in"]["symbol"]
    assert bundle["steps"] == DEPOSIT_KINDS
    assert review["transactions"] == len(DEPOSIT_KINDS)
    assert review["funding"][0]["required"]["symbol"] == raw["token_in"]["symbol"]
    assert review["policy"] == {"ok": True, "violations": []}
    assert review["blockers"] == []


def test_review_refuses_an_unknown_kind() -> None:
    with pytest.raises(ServiceError) as refused:
        execution_service.review_plan("teleport", {})

    assert refused.value.code == "invalid_input"


def test_a_rejected_plan_cannot_be_approved() -> None:
    store = InMemoryPlanStore()
    stored = store.put("execute", {"a": 1})

    execution_service.reject(store, stored.plan_hash)
    with pytest.raises(ServiceError) as approved:
        execution_service.apply_approved(store, stored.plan_hash)

    assert approved.value.code == "plan_used"
