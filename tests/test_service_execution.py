"""The plan/apply split: an approved plan is the plan that runs, at most once."""

import json
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cctp_messages import ARBITRUM, ARBITRUM_USDC, BASE, BASE_USDC
from test_bridge import FEE, Circle
from test_bridge import Client as BridgeClient
from test_bridge import Config as BridgeConfig
from test_bridge import Signer as BridgeSigner
from test_bridge import World as BridgeWorld
from test_bridge import vaults as bridge_vaults
from test_cli import (
    DEPOSIT_KINDS,
    ExecutionOneTxClient,
    RebalanceOneTxClient,
    WithdrawOneTxClient,
    execution_policy,
    install_execution_surface_mocks,
    install_rebalance_surface_mocks,
    install_withdraw_surface_mocks,
    patch_surface,
    write_execution_files,
    write_rebalance_files,
)
from test_loops import CROSS, SAME, BatchingSigner, LoopClient, loop_policy
from test_loops import Config as LoopConfig
from test_loops import _close_client as close_client
from test_loops import _morpho as loop_morpho
from test_loops import _open_client as open_client
from test_loops import _open_policy as open_policy
from test_loops import instruments as loop_instruments

from open_allocator.core.positions import Positions
from open_allocator.core.state import LocalFsBackend, with_state_backend
from open_allocator.exec import loops as loops_exec
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


def planned_withdrawal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    amount: float | None = None,
) -> dict[str, Any]:
    positions_path, _target, policy_path = write_rebalance_files(tmp_path)
    book = Positions.model_validate_json(positions_path.read_text(encoding="utf-8"))
    monkeypatch.setattr(
        loops_exec, "read_book", lambda _client, _address, _config=None: (book, [])
    )
    proposal = execution_service.plan_withdraw(
        "vault-a", amount=amount, policy=policy_path
    )
    return json.loads(json.dumps(proposal))


def test_a_withdrawal_plan_is_the_dry_run_and_sends_nothing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    signer = install_withdraw_surface_mocks(monkeypatch)

    proposal = planned_withdrawal(monkeypatch, tmp_path)

    assert proposal["kind"] == "withdraw"
    assert proposal["plan_hash"] == plan_hash("withdraw", proposal["plan"])
    assert proposal["report"]["status"] == "planned"
    assert proposal["report"]["plan"] == proposal["plan"]["plan"]
    assert proposal["plan"]["withdraw_plan"]["instrument_id"] == "vault-a"
    assert signer.sent == []


def test_an_unknown_position_is_not_planned(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    install_withdraw_surface_mocks(monkeypatch)
    positions_path, _target, policy_path = write_rebalance_files(tmp_path)
    book = Positions.model_validate_json(positions_path.read_text(encoding="utf-8"))
    monkeypatch.setattr(
        loops_exec, "read_book", lambda _client, _address, _config=None: (book, [])
    )

    with pytest.raises(ServiceError) as raised:
        execution_service.plan_withdraw("vault-z", policy=policy_path)

    assert raised.value.code == "not_found"
    assert WithdrawOneTxClient.calls == []


def test_an_approved_withdrawal_runs_the_stored_plan_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    signer = install_withdraw_surface_mocks(
        monkeypatch, idempotency_store_path=tmp_path / "idempotency.json"
    )
    store = InMemoryPlanStore()
    response = execution_service.propose(
        store, planned_withdrawal(monkeypatch, tmp_path)
    )
    WithdrawOneTxClient.calls = []

    # A withdrawal has no policy re-check: an exit is never refused for it.
    report = execution_service.apply_approved(
        store, response["plan_hash"], policy=tmp_path / "missing.yaml"
    )
    with pytest.raises(ServiceError) as again:
        execution_service.apply_approved(store, response["plan_hash"])

    assert report["status"] == "success"
    assert [getattr(sent[0], "kind") for sent in signer.sent] == ["withdraw"]
    assert WithdrawOneTxClient.calls == []
    assert again.value.code == "plan_used"


def test_a_withdrawal_sent_since_it_was_planned_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    signer = install_withdraw_surface_mocks(
        monkeypatch, idempotency_store_path=tmp_path / "idempotency.json"
    )
    proposal = planned_withdrawal(monkeypatch, tmp_path)
    execution_service.apply_withdraw(proposal["plan"])
    sent = len(signer.sent)

    with pytest.raises(TransactionPlanError, match="stale"):
        execution_service.apply_withdraw(
            proposal["plan"], expected_hash=proposal["plan_hash"]
        )

    assert len(signer.sent) == sent


def test_a_tampered_withdrawal_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    signer = install_withdraw_surface_mocks(monkeypatch)
    proposal = planned_withdrawal(monkeypatch, tmp_path)
    tampered = json.loads(json.dumps(proposal["plan"]))
    tampered["account"] = "0x" + "22" * 20

    with pytest.raises(ServiceError) as raised:
        execution_service.apply_withdraw(tampered, expected_hash=proposal["plan_hash"])
    with pytest.raises(TransactionPlanError, match="built for"):
        execution_service.apply_withdraw(tampered)

    assert raised.value.code == "plan_mismatch"
    assert signer.sent == []


def test_a_withdrawal_review_describes_the_exit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    install_withdraw_surface_mocks(monkeypatch)
    proposal = planned_withdrawal(monkeypatch, tmp_path, amount=30)

    review = execution_service.review_plan("withdraw", proposal["plan"])

    assert review["kind"] == "withdraw"
    assert review["account"] == proposal["plan"]["account"]
    assert (review["instrument_id"], review["chain_id"]) == ("vault-a", 8453)
    assert review["full_exit"] is False
    assert (review["requested_usd"], review["current_usd"]) == (30.0, 80.0)
    assert review["shares"] == "30"
    assert review["share_symbol"] == "aUSDC"
    (bundle,) = review["bundles"]
    assert bundle["action"] == "withdraw"
    assert bundle["steps"] == ["withdraw"]
    # A partial exit's calldata amount is in the underlying, not the yield token.
    assert bundle["amount_in"] == {
        "raw": "30000000",
        "amount": "30",
        "symbol": "USDC",
        "token": None,
    }
    assert review["transactions"] == 1


def planned_rebalance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> dict[str, Any]:
    positions_path, target_path, policy_path = write_rebalance_files(tmp_path)
    book = Positions.model_validate_json(positions_path.read_text(encoding="utf-8"))
    monkeypatch.setattr(
        loops_exec,
        "read_book",
        lambda _client, _address, _config=None: (book, ["loop unreadable"]),
    )
    warnings: list[dict[str, Any]] = []
    proposal = execution_service.plan_rebalance(
        read_allocation(target_path), policy=policy_path, on_warning=warnings.append
    )
    assert {"warning": "unread_position", "message": "loop unreadable"} in warnings
    return json.loads(json.dumps(proposal))


def test_a_rebalance_plan_is_the_dry_run_and_sends_nothing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    signer = install_rebalance_surface_mocks(monkeypatch)

    proposal = planned_rebalance(monkeypatch, tmp_path)

    assert proposal["kind"] == "rebalance"
    assert proposal["plan_hash"] == plan_hash("rebalance", proposal["plan"])
    assert proposal["report"]["status"] == "planned"
    assert proposal["report"]["plan"] == proposal["plan"]["plan"]
    trades = proposal["plan"]["rebalance_plan"]["trades"]
    assert [(trade["action"], trade["instrument_id"]) for trade in trades] == [
        ("sell", "vault-a"),
        ("buy", "vault-b"),
    ]
    assert signer.sent == []


def test_a_rebalance_is_planned_from_a_given_book(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    install_rebalance_surface_mocks(monkeypatch)
    positions_path, target_path, policy_path = write_rebalance_files(tmp_path)

    def unread(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the live book was read")

    monkeypatch.setattr(loops_exec, "read_book", unread)

    proposal = execution_service.plan_rebalance(
        read_allocation(target_path),
        positions=json.loads(positions_path.read_text(encoding="utf-8")),
        policy=policy_path,
    )

    assert proposal["plan"]["positions"]["total_usd"] == 100.0


def test_an_approved_rebalance_runs_the_stored_plan_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    signer = install_rebalance_surface_mocks(
        monkeypatch, idempotency_store_path=tmp_path / "idempotency.json"
    )
    store = InMemoryPlanStore()
    response = execution_service.propose(
        store, planned_rebalance(monkeypatch, tmp_path)
    )
    RebalanceOneTxClient.calls = []

    report = execution_service.apply_approved(store, response["plan_hash"])
    with pytest.raises(ServiceError) as again:
        execution_service.apply_approved(store, response["plan_hash"])

    assert report["status"] == "success"
    assert [getattr(sent[0], "kind") for sent in signer.sent] == [
        "withdraw",
        *DEPOSIT_KINDS,
    ]
    assert RebalanceOneTxClient.calls == []
    assert again.value.code == "plan_used"


def test_a_rebalance_sent_since_it_was_planned_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    signer = install_rebalance_surface_mocks(
        monkeypatch, idempotency_store_path=tmp_path / "idempotency.json"
    )
    proposal = planned_rebalance(monkeypatch, tmp_path)
    execution_service.apply_rebalance(proposal["plan"])
    sent = len(signer.sent)

    with pytest.raises(TransactionPlanError, match="stale"):
        execution_service.apply_rebalance(
            proposal["plan"], expected_hash=proposal["plan_hash"]
        )

    assert len(signer.sent) == sent


def test_a_tampered_rebalance_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    signer = install_rebalance_surface_mocks(monkeypatch)
    proposal = planned_rebalance(monkeypatch, tmp_path)
    tampered = json.loads(json.dumps(proposal["plan"]))
    tampered["account"] = "0x" + "22" * 20

    with pytest.raises(ServiceError) as raised:
        execution_service.apply_rebalance(tampered, expected_hash=proposal["plan_hash"])
    with pytest.raises(TransactionPlanError, match="built for"):
        execution_service.apply_rebalance(tampered)

    assert raised.value.code == "plan_mismatch"
    assert signer.sent == []


def test_approval_rechecks_a_rebalance_target_against_the_operator_policy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    signer = install_rebalance_surface_mocks(monkeypatch)
    proposal = planned_rebalance(monkeypatch, tmp_path)
    strict = tmp_path / "strict.yaml"
    strict_policy = execution_policy()
    strict_policy["caps"]["max_weight_per_instrument"] = 0.4
    strict.write_text(json.dumps(strict_policy), encoding="utf-8")
    store = InMemoryPlanStore()
    response = execution_service.propose(store, proposal)

    with pytest.raises(ServiceError) as raised:
        execution_service.apply_approved(store, response["plan_hash"], policy=strict)

    assert raised.value.code == "policy_violation"
    assert "max_weight_per_instrument" in raised.value.detail
    assert signer.sent == []


def test_a_rebalance_target_outside_the_policy_is_not_planned(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    install_rebalance_surface_mocks(monkeypatch)
    positions_path, target_path, _policy_path = write_rebalance_files(tmp_path)
    strict = tmp_path / "strict.yaml"
    strict_policy = execution_policy()
    strict_policy["caps"]["max_weight_per_instrument"] = 0.4
    strict.write_text(json.dumps(strict_policy), encoding="utf-8")

    with pytest.raises(ServiceError) as raised:
        execution_service.plan_rebalance(
            read_allocation(target_path),
            positions=json.loads(positions_path.read_text(encoding="utf-8")),
            policy=strict,
        )

    assert raised.value.code == "policy_violation"
    assert RebalanceOneTxClient.calls == []


def test_a_rebalance_review_describes_the_trades(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    install_rebalance_surface_mocks(monkeypatch)
    proposal = planned_rebalance(monkeypatch, tmp_path)

    review = execution_service.review_plan("rebalance", proposal["plan"])

    assert review["kind"] == "rebalance"
    assert review["account"] == proposal["plan"]["account"]
    assert (review["book_usd"], review["target_usd"]) == (100.0, 100.0)
    assert (review["total_sell_usd"], review["total_buy_usd"]) == (30.0, 30.0)
    assert [
        (trade["action"], trade["instrument_id"], trade["usd"])
        for trade in review["trades"]
    ] == [("sell", "vault-a", 30.0), ("buy", "vault-b", 30.0)]
    assert review["trades"][0]["deposit_usd"] is None
    assert review["trades"][1]["deposit_usd"] == 30.0
    withdraw, deposit = review["bundles"]
    assert withdraw["steps"] == ["withdraw"]
    # A partial sell's calldata amount is in the underlying, not the yield token.
    assert withdraw["amount_in"] == {
        "raw": "30000000",
        "amount": "30",
        "symbol": "USDC",
        "token": None,
    }
    assert deposit["steps"] == DEPOSIT_KINDS
    assert review["policy"] == {"ok": True, "violations": []}
    assert review["transactions"] == 1 + len(DEPOSIT_KINDS)


# --- loops and transfers --------------------------------------------------------


def state_config(config: object, tmp_path: Path) -> object:
    """`config` with its state under `tmp_path`, as the server's."""
    return with_state_backend(
        config,
        LocalFsBackend(
            checkpoint_dir=tmp_path / "checkpoints",
            log_path=tmp_path / "allocation-log.jsonl",
            idempotency_store_path=tmp_path / "idempotency.json",
        ),
    )


def install_loop_surface(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, client: LoopClient
) -> BatchingSigner:
    signer = BatchingSigner()
    config = state_config(LoopConfig(), tmp_path)
    patch_surface(monkeypatch, "AllocatorConfig", lambda: config)
    patch_surface(monkeypatch, "OneTxClient", lambda _config: nullcontext(client))
    patch_surface(monkeypatch, "signer_from_config", lambda _config: signer)
    patch_surface(
        monkeypatch,
        "discover_vaults_from_client",
        lambda _client, **_options: [*loop_instruments(), loop_morpho()],
    )
    return signer


def planned_open(**options: Any) -> dict[str, Any]:
    proposal = execution_service.plan_loop_open(
        SAME, equity_usd=20.0, leverage=3.0, policy=open_policy(), **options
    )
    return json.loads(json.dumps(proposal))


def test_a_loop_open_plan_is_the_dry_run_and_sends_nothing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    signer = install_loop_surface(monkeypatch, tmp_path, open_client())

    proposal = planned_open()

    assert proposal["kind"] == "loop-open"
    assert proposal["plan_hash"] == plan_hash("loop-open", proposal["plan"])
    assert proposal["report"]["status"] == "planned"
    assert proposal["report"]["plan"] == proposal["plan"]["plan"]
    assert proposal["plan"]["policy_result"]["ok"] is True
    assert signer.batches == []


def test_a_loop_open_outside_the_policy_is_not_planned(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """With nothing else held the loop is the whole book, over the 70% cap."""
    install_loop_surface(monkeypatch, tmp_path, open_client(held_usd=None))

    with pytest.raises(ServiceError) as raised:
        planned_open()

    assert raised.value.code == "policy_violation"


def test_an_approved_loop_open_runs_the_stored_plan_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client = open_client()
    signer = install_loop_surface(monkeypatch, tmp_path, client)
    store = InMemoryPlanStore()
    response = execution_service.propose(store, planned_open())
    client.requests.clear()

    report = execution_service.apply_approved(
        store, response["plan_hash"], policy=open_policy()
    )
    with pytest.raises(ServiceError) as again:
        execution_service.apply_approved(store, response["plan_hash"])

    assert report["status"] == "success"
    assert len(signer.batches) == 1
    assert client.requests == []
    assert again.value.code == "plan_used"


def test_approval_rechecks_a_loop_open_against_the_operator_policy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """20 next to a held 30 is 40% of the book, over a 30% instrument cap."""
    signer = install_loop_surface(monkeypatch, tmp_path, open_client())
    store = InMemoryPlanStore()
    response = execution_service.propose(store, planned_open())
    policy = open_policy()
    tighter = policy.model_copy(
        update={
            "caps": policy.caps.model_copy(update={"max_weight_per_instrument": 0.3})
        }
    )

    with pytest.raises(ServiceError) as refused:
        execution_service.apply_approved(store, response["plan_hash"], policy=tighter)

    assert refused.value.code == "policy_violation"
    assert signer.batches == []


def test_a_loop_open_sent_since_it_was_planned_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    signer = install_loop_surface(monkeypatch, tmp_path, open_client())
    proposal = planned_open()
    execution_service.apply_loop_open(proposal["plan"])

    with pytest.raises(TransactionPlanError, match="stale"):
        execution_service.apply_loop_open(
            proposal["plan"], expected_hash=proposal["plan_hash"]
        )

    assert len(signer.batches) == 1


def test_a_tampered_loop_open_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    signer = install_loop_surface(monkeypatch, tmp_path, open_client())
    proposal = planned_open()
    tampered = json.loads(json.dumps(proposal["plan"]))
    tampered["leverage"] = 8.0

    with pytest.raises(ServiceError) as raised:
        execution_service.apply_loop_open(tampered, expected_hash=proposal["plan_hash"])

    assert raised.value.code == "plan_mismatch"
    assert signer.batches == []


def test_a_loop_open_review_describes_the_open(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    install_loop_surface(monkeypatch, tmp_path, open_client())
    proposal = planned_open()

    review = execution_service.review_plan("loop-open", proposal["plan"])

    assert review["kind"] == "loop-open"
    assert (review["loop_id"], review["equity_usd"], review["leverage"]) == (
        SAME,
        20.0,
        3.0,
    )
    assert review["loop"]["account_config"] == "aave e-mode 0 -> 2"
    assert review["policy"]["ok"] is True
    (bundle,) = review["bundles"]
    assert bundle["action"] == "loop_open"
    assert review["transactions"] == 9


def test_an_approved_loop_close_runs_the_stored_plan_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    signer = install_loop_surface(monkeypatch, tmp_path, close_client())
    store = InMemoryPlanStore()
    proposal = json.loads(
        json.dumps(execution_service.plan_loop_close(CROSS, policy=loop_policy()))
    )
    assert proposal["report"]["status"] == "planned"
    response = execution_service.propose(store, proposal)

    # A close has no policy re-check: an exit is never refused for it.
    report = execution_service.apply_approved(
        store, response["plan_hash"], policy=tmp_path / "missing.yaml"
    )
    with pytest.raises(ServiceError) as again:
        execution_service.apply_approved(store, response["plan_hash"])

    assert report["status"] == "success"
    assert len(signer.batches) == 1
    assert again.value.code == "plan_used"


def test_a_loop_close_review_describes_the_unwind(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    install_loop_surface(monkeypatch, tmp_path, close_client())
    proposal = execution_service.plan_loop_close(CROSS, policy=loop_policy())

    review = execution_service.review_plan("loop-close", proposal["plan"])

    assert (review["kind"], review["loop_id"]) == ("loop-close", CROSS)
    assert review["loop"]["account_config"] == "aave e-mode 2 -> 0"
    assert [bundle["action"] for bundle in review["bundles"]] == ["loop_close"]
    assert review["transactions"] == 25


@dataclass
class Transfers:
    world: BridgeWorld = field(default_factory=BridgeWorld)
    client: BridgeClient = field(default_factory=BridgeClient)
    signer: BridgeSigner = field(init=False)
    circle: Circle = field(init=False)

    def __post_init__(self) -> None:
        self.world.credit(BASE, BASE_USDC, 500_000_000)
        self.signer = BridgeSigner(self.world)
        self.circle = Circle(self.world)

    def install(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        config = state_config(
            BridgeConfig(self.world, self.circle, source_chain_id=None), tmp_path
        )
        patch_surface(monkeypatch, "AllocatorConfig", lambda: config)
        patch_surface(
            monkeypatch, "OneTxClient", lambda _config: nullcontext(self.client)
        )
        patch_surface(monkeypatch, "signer_from_config", lambda _config: self.signer)
        patch_surface(
            monkeypatch,
            "discover_vaults_from_client",
            lambda _client, **_options: bridge_vaults(),
        )

    def kinds(self) -> list[list[str]]:
        return [[step.kind for step in batch] for batch in self.signer.batches]


def planned_bridge(ref: str | None = None) -> dict[str, Any]:
    proposal = execution_service.plan_bridge(BASE, ARBITRUM, 100, ref=ref)
    return json.loads(json.dumps(proposal))


def test_a_transfer_is_burned_then_advanced_each_on_its_own_approval(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    transfers = Transfers()
    transfers.install(monkeypatch, tmp_path)
    store = InMemoryPlanStore()

    burn = execution_service.propose(store, planned_bridge())
    assert transfers.signer.batches == []
    burned = execution_service.apply_approved(store, burn["plan_hash"])

    assert burned["status"] == "in_progress"
    assert transfers.kinds() == [["approve", "bridge_burn"]]

    transfers.circle.ready = True
    advance = planned_bridge()
    assert advance["plan"]["existing"]["state"] == "awaiting_attestation"
    assert advance["plan"]["plan"]["bundles"] == []
    review = execution_service.review_plan("bridge", advance["plan"])
    assert review["advances"]["state"] == "awaiting_attestation"
    # Nothing is redeemed until the advance is approved too.
    assert len(transfers.signer.batches) == 1
    response = execution_service.propose(store, advance)
    settled = execution_service.apply_approved(store, response["plan_hash"])

    assert settled["status"] == "success"
    assert transfers.kinds()[1:] == [["cctp_receive"]]
    assert transfers.world.balance(ARBITRUM, ARBITRUM_USDC) == 100_000_000 - FEE
    with pytest.raises(ServiceError, match="--ref") as done:
        planned_bridge()
    assert done.value.code == "invalid_input"
    # Another ref is another transfer.
    assert planned_bridge(ref="again")["plan"]["existing"] is None


def test_a_transfer_started_since_it_was_planned_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    transfers = Transfers()
    transfers.install(monkeypatch, tmp_path)
    first = planned_bridge()
    second = planned_bridge()
    execution_service.apply_bridge(first["plan"], expected_hash=first["plan_hash"])

    with pytest.raises(TransactionPlanError, match="stale"):
        execution_service.apply_bridge(
            second["plan"], expected_hash=second["plan_hash"]
        )

    assert len(transfers.signer.batches) == 1


def test_a_transfer_review_describes_the_burn(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    transfers = Transfers()
    transfers.install(monkeypatch, tmp_path)
    proposal = planned_bridge()

    review = execution_service.review_plan("bridge", proposal["plan"])

    assert review["kind"] == "bridge"
    assert (review["from_chain_id"], review["to_chain_id"]) == (BASE, ARBITRUM)
    assert review["amount_usdc"] == 100
    assert review["advances"] is None
    (bundle,) = review["bundles"]
    assert bundle["action"] == "bridge"
    assert bundle["bridge"]["to_chain_id"] == ARBITRUM
    assert (bundle["amount_in"]["amount"], bundle["amount_in"]["symbol"]) == (
        "100",
        "USDC",
    )
    assert bundle["bridge"]["max_fee"]["symbol"] == "USDC"
    assert [item["required"]["symbol"] for item in review["funding"]] == ["USDC"]
    assert any("CCTP" in note for note in review["notes"])
    assert transfers.signer.batches == []
