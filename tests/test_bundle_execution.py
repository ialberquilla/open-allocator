from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from open_allocator.core.checkpoint import read_allocation_log
from open_allocator.core.policy import PolicyResult
from open_allocator.core.types import TxBundle, TxPlan, TxStep
from open_allocator.exec import calldata
from open_allocator.exec.bundle_execution import (
    BundleLog,
    PlannedBundle,
    SubmissionModeError,
    assemble_plan,
    execute_plan,
    operations,
    planned_bundles,
    prepare_plan,
)
from open_allocator.exec.calldata import CalldataValidationError
from open_allocator.exec.client import (
    InstrumentCalldataQuery,
    InstrumentCalldataResponse,
)
from open_allocator.exec.execute import (
    ExecutionBroadcastError,
    GasCheck,
    TransactionPlanError,
)
from open_allocator.exec.paymaster_types import (
    PaymasterPreparationUnavailable,
    PaymasterRejected,
    PaymasterTokenQuote,
    PreparedUserOperation,
    UserOperationGas,
)
from open_allocator.exec.signer import Receipt

ACCOUNT = "0x0000000000000000000000000000000000000001"
BASE = 8453
ARBITRUM = 42161
NOW = 1_789_650_000
FIXTURES = Path(__file__).parent / "fixtures"
OK = PolicyResult(ok=True, violations=())


def response(
    instrument_id: str,
    *,
    chain_id: int = BASE,
    expires_at: int | None = None,
    quote_block: int = 35_123_456,
    account: str = ACCOUNT,
) -> InstrumentCalldataResponse:
    payload = json.loads(
        (FIXTURES / "calldata-instrument-deposit-swap.json").read_text(encoding="utf-8")
    )
    payload.update(
        instrumentId=instrument_id,
        account=account,
        chainId=chain_id,
        expiresAt=expires_at,
        quoteBlock=quote_block,
    )
    payload["simulation"]["quoteBlock"] = quote_block
    for call in payload["calls"]:
        call["chainId"] = chain_id
    return InstrumentCalldataResponse.model_validate(payload)


def plan(*responses: InstrumentCalldataResponse) -> TxPlan:
    items: list[PlannedBundle] = []
    for leg_index, item in enumerate(responses):
        steps, bundle = calldata.plan_bundle(
            item, leg_index=leg_index, first_step_index=0
        )
        items.append(PlannedBundle(bundle=bundle, steps=steps))
    return assemble_plan(items, "test plan")


def leg_key(bundle: TxBundle) -> str:
    return f"leg:{bundle.leg_index}:{bundle.instrument_id}"


def buy_log(bundle: TxBundle) -> BundleLog:
    return BundleLog(action_type="buy", usd=100.25)


@dataclass(frozen=True)
class Config:
    gas_checker: object = lambda _address, chain_id, _rpc, _config: GasCheck(
        chain_id=chain_id,
        ok=True,
        message=f"native gas available on chain {chain_id}",
    )
    _rpc_overrides: dict[int, str] = field(
        default_factory=lambda: {BASE: "rpc://base", ARBITRUM: "rpc://arbitrum"}
    )
    min_calldata_ttl_seconds: int = 20
    slippage_bps: int = 30
    checkpoint_dir: Path | None = None
    allocation_log_path: Path | None = None


def receipt(index: int, *, pending: bool = False) -> Receipt:
    return Receipt(
        transaction_hash=f"0x{index:064x}",
        block_number=0 if pending else index,
        gas_used=0 if pending else 21_000,
        status=0 if pending else 1,
        from_address=ACCOUNT,
        pending=pending,
        execution_status="safe_proposed" if pending else "mined",
    )


@dataclass
class BatchingSigner:
    batches: list[tuple[TxStep, ...]] = field(default_factory=list)
    error: Exception | None = None
    pending: bool = False

    def address(self) -> str:
        return ACCOUNT

    def send(self, tx: TxStep, rpc_url: str) -> Receipt:
        raise AssertionError("a batching signer must receive whole operations")

    def send_batch(self, steps: tuple[TxStep, ...], rpc_url: str) -> Receipt:
        if self.error is not None:
            raise self.error
        self.batches.append(tuple(steps))
        return receipt(len(self.batches), pending=self.pending)


@dataclass
class SequentialSigner:
    sent: list[TxStep] = field(default_factory=list)
    fail_at: int | None = None

    def address(self) -> str:
        return ACCOUNT

    def send(self, tx: TxStep, rpc_url: str) -> Receipt:
        if self.fail_at is not None and len(self.sent) == self.fail_at:
            raise RuntimeError("boom")
        self.sent.append(tx)
        return receipt(len(self.sent))


@dataclass
class PreparingSigner(BatchingSigner):
    deployed: bool = True
    sender: str = ACCOUNT
    prepared: list[tuple[TxStep, ...]] = field(default_factory=list)
    unavailable: bool = False

    def prepare_batch(
        self, steps: tuple[TxStep, ...], rpc_url: str
    ) -> PreparedUserOperation:
        if self.unavailable:
            raise PaymasterPreparationUnavailable("GenericHttp cannot estimate")
        self.prepared.append(tuple(steps))
        return PreparedUserOperation(
            sender=self.sender,
            chain_id=steps[0].chain_id,
            entry_point="0x0000000071727De22E5E9d8BAf0edAc6f37da032",
            user_operation={"sender": self.sender, "signature": "0xstub"},
            deployed=self.deployed,
            factory=None
            if self.deployed
            else "0x000000000000000000000000000000000000fac7",
            factory_data=None if self.deployed else "0x1234",
            gas=UserOperationGas(
                call_gas_limit=900_000,
                verification_gas_limit=450_000,
                pre_verification_gas=60_000,
                paymaster_verification_gas_limit=30_000,
                paymaster_post_op_gas_limit=15_000,
                max_fee_per_gas=1_000_000,
                max_priority_fee_per_gas=1_000,
            ),
            paymaster=PaymasterTokenQuote(
                paymaster="0x777777777777AeC03fd955926DbF81597e66834C",
                token="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                exchange_rate=10**18,
                post_op_gas=5_000,
                approval_included=True,
            ),
        )


@dataclass
class ProposalSafeSigner(BatchingSigner):
    proposes_asynchronously = True
    deployed: bool = True
    checked: list[tuple[int, str]] = field(default_factory=list)

    def is_deployed(self, chain_id: int, rpc_url: str) -> bool:
        self.checked.append((chain_id, rpc_url))
        return self.deployed


@dataclass
class RefreshingClient:
    account: str = ACCOUNT
    requests: list[tuple[str, InstrumentCalldataQuery]] = field(default_factory=list)

    def instrument_calldata(
        self,
        instrument_id: str,
        query: InstrumentCalldataQuery,
    ) -> InstrumentCalldataResponse:
        self.requests.append((instrument_id, query))
        return response(
            instrument_id,
            expires_at=NOW + 600,
            quote_block=35_123_999,
            account=self.account,
        )


def run(
    signer: object,
    tx_plan: TxPlan,
    *,
    client: object | None = None,
    config: Config | None = None,
    store: dict[str, object] | None = None,
) -> Any:
    return execute_plan(
        client or RefreshingClient(),
        signer,
        tx_plan,
        stage="execute",
        policy_result=OK,
        completion_key=leg_key,
        log=buy_log,
        config=config or Config(),
        idempotency_store=store,
        clock=lambda: NOW,
    )


# --- grouping ---------------------------------------------------------------


def test_consecutive_same_chain_bundles_share_one_operation() -> None:
    bundles = planned_bundles(
        plan(response("a"), response("b"), response("c", chain_id=ARBITRUM))
    )

    grouped = operations(bundles, batching=True)

    assert [op.bundle_ids for op in grouped] == [
        ("leg:0:a:deposit", "leg:1:b:deposit"),
        ("leg:2:c:deposit",),
    ]


def test_same_chain_bundles_apart_in_plan_order_are_not_merged() -> None:
    bundles = planned_bundles(
        plan(response("a"), response("b", chain_id=ARBITRUM), response("c"))
    )

    assert [op.chain_id for op in operations(bundles, batching=True)] == [
        BASE,
        ARBITRUM,
        BASE,
    ]


def test_a_signer_that_cannot_batch_gets_one_operation_per_bundle() -> None:
    bundles = planned_bundles(plan(response("a"), response("b")))

    assert len(operations(bundles, batching=False)) == 2


def test_a_step_outside_every_bundle_is_refused() -> None:
    tx_plan = plan(response("a"))
    loose = TxPlan(
        steps=(*tx_plan.steps, tx_plan.steps[0]),
        summary="loose step",
        bundles=tx_plan.bundles,
    )

    with pytest.raises(TransactionPlanError, match="belong to exactly one bundle"):
        planned_bundles(loose)


# --- submission -------------------------------------------------------------


def test_each_operation_is_submitted_once_with_its_calls_in_order() -> None:
    tx_plan = plan(response("a"), response("b"), response("c", chain_id=ARBITRUM))
    signer = BatchingSigner()
    store: dict[str, object] = {}

    result = run(signer, tx_plan, store=store)

    assert [len(batch) for batch in signer.batches] == [10, 5]
    assert signer.batches[0] == tx_plan.steps[:10]
    assert signer.batches[1] == tx_plan.steps[10:]
    assert result.status == "success"
    assert result.plan == tx_plan
    assert [step.step_index for step in result.steps] == list(range(15))
    assert {step.status for step in result.steps} == {"sent"}


def test_a_batched_operation_is_marked_complete_as_a_whole() -> None:
    """Inner calls share one receipt; none is complete on its own."""
    tx_plan = plan(response("a"))
    store: dict[str, object] = {}

    result = run(BatchingSigner(), tx_plan, store=store)

    bundle = tx_plan.bundles[0]
    bundle_key = f"{bundle.bundle_id}:digest:{bundle.digest}"
    assert set(store) == {bundle_key, "leg:0:a"}
    assert {step.idempotency_key for step in result.steps} == {bundle_key}
    assert set(result.completed_keys) == {bundle_key, "leg:0:a"}


def test_a_signer_that_cannot_batch_sends_and_marks_each_call() -> None:
    tx_plan = plan(response("a"))
    signer = SequentialSigner()
    store: dict[str, object] = {}

    run(signer, tx_plan, store=store)

    assert tuple(signer.sent) == tx_plan.steps
    bundle = tx_plan.bundles[0]
    call_keys = {
        f"{bundle.bundle_id}:digest:{bundle.digest}:call:{index}" for index in range(5)
    }
    assert call_keys < set(store)
    assert "leg:0:a" in store


def test_a_failed_call_leaves_its_leg_incomplete() -> None:
    tx_plan = plan(response("a"))
    store: dict[str, object] = {}

    with pytest.raises(ExecutionBroadcastError) as raised:
        run(SequentialSigner(fail_at=2), tx_plan, store=store)

    assert "leg:0:a" not in store
    assert sum(":call:" in key for key in store) == 2
    assert raised.value.partial_report.status == "failed"
    assert raised.value.partial_report.plan == tx_plan


def test_a_paymaster_failure_surfaces_as_itself_after_checkpointing(
    tmp_path: Path,
) -> None:
    signer = BatchingSigner(error=PaymasterRejected("sponsorship denied"))

    with pytest.raises(PaymasterRejected, match="sponsorship denied"):
        run(signer, plan(response("a")), config=Config(checkpoint_dir=tmp_path))

    payloads = [json.loads(path.read_text()) for path in tmp_path.glob("*.json")]
    assert [payload["status"] for payload in payloads] == ["failed"]


def test_a_pending_operation_reports_in_progress() -> None:
    result = run(BatchingSigner(pending=True), plan(response("a")))

    assert result.status == "in_progress"
    assert any("awaiting threshold" in message for message in result.messages)


def test_a_bundle_is_logged_once_not_once_per_call(tmp_path: Path) -> None:
    log_path = tmp_path / "allocation-log.jsonl"

    run(
        BatchingSigner(),
        plan(response("a"), response("b")),
        config=Config(allocation_log_path=log_path),
    )

    entries = read_allocation_log(log_path=log_path)
    assert [(entry.instrument_id, entry.action_type) for entry in entries] == [
        ("a", "buy"),
        ("b", "buy"),
    ]


# --- expiry -----------------------------------------------------------------


def test_an_expiring_bundle_is_rebuilt_immediately_before_signing() -> None:
    tx_plan = plan(response("a", expires_at=NOW + 10))
    client = RefreshingClient()
    signer = BatchingSigner()
    store: dict[str, object] = {}

    result = run(signer, tx_plan, client=client, store=store)

    original = tx_plan.bundles[0]
    [(instrument_id, query)] = client.requests
    assert instrument_id == "a"
    assert query.model_dump(by_alias=True, exclude_none=True) == {
        "action": "deposit",
        "account": ACCOUNT,
        "amount": original.amount,
        "slippageBps": 30,
    }
    rebuilt = result.plan.bundles[0]
    assert rebuilt.bundle_id == original.bundle_id
    assert rebuilt.digest != original.digest
    assert rebuilt.expires_at == NOW + 600
    # Completion is recorded against the calls actually sent, not the stale ones.
    assert f"{original.bundle_id}:digest:{original.digest}" not in store
    assert f"{rebuilt.bundle_id}:digest:{rebuilt.digest}" in store
    assert any("rebuilt before signing" in message for message in result.messages)


def test_a_bundle_with_enough_lifetime_is_signed_as_planned() -> None:
    tx_plan = plan(response("a", expires_at=NOW + 600))
    client = RefreshingClient()

    result = run(BatchingSigner(), tx_plan, client=client)

    assert client.requests == []
    assert result.plan == tx_plan


def test_a_rebuilt_bundle_is_validated_before_it_can_be_signed() -> None:
    signer = BatchingSigner()

    with pytest.raises(CalldataValidationError, match="account"):
        run(
            signer,
            plan(response("a", expires_at=NOW + 10)),
            client=RefreshingClient(account="0x" + "99" * 20),
        )

    assert signer.batches == []


# --- signer capability ------------------------------------------------------


def test_a_proposal_only_safe_never_queues_expiring_calldata() -> None:
    signer = ProposalSafeSigner()
    tx_plan = plan(response("a"), response("b", expires_at=NOW + 600))

    assert "leg:1:b:deposit" in prepare_plan(signer, tx_plan, Config()).blockers[0]
    with pytest.raises(SubmissionModeError, match="erc4337-paymaster"):
        run(signer, tx_plan)

    assert signer.batches == []


def test_a_proposal_only_safe_may_propose_calldata_that_does_not_expire() -> None:
    signer = ProposalSafeSigner(pending=True)

    result = run(signer, plan(response("a")))

    assert len(signer.batches) == 1
    assert result.status == "in_progress"


def test_safe_over_rpc_reports_deployment_required_for_an_undeployed_safe() -> None:
    signer = ProposalSafeSigner(deployed=False)
    tx_plan = plan(response("a"), response("b", chain_id=ARBITRUM))

    preparation = prepare_plan(signer, tx_plan, Config())

    assert signer.checked == [(BASE, "rpc://base"), (ARBITRUM, "rpc://arbitrum")]
    assert len(preparation.blockers) == 2
    assert all("not deployed" in blocker for blocker in preparation.blockers)
    with pytest.raises(SubmissionModeError, match="not deployed"):
        run(signer, tx_plan)
    assert signer.batches == []


# --- wallet-aware preparation -----------------------------------------------


def test_a_dry_run_prepares_each_operation_without_submitting() -> None:
    signer = PreparingSigner(deployed=False)
    tx_plan = plan(response("a"), response("b"), response("c", chain_id=ARBITRUM))

    preparation = prepare_plan(signer, tx_plan, Config())

    assert signer.batches == []
    assert [len(steps) for steps in signer.prepared] == [10, 5]
    first = preparation.preparations[0]
    assert first.bundle_ids == ("leg:0:a:deposit", "leg:1:b:deposit")
    assert first.includes_deployment is True
    assert first.call_gas_limit == 900_000
    assert first.paymaster_verification_gas_limit == 30_000
    assert first.paymaster_approval_included is True
    assert first.max_gas_token_charge_raw is None
    assert preparation.blockers == ()


def test_wallet_gas_and_protocol_gas_stay_separate_measurements() -> None:
    tx_plan = plan(response("a"))

    [prepared] = prepare_plan(PreparingSigner(), tx_plan, Config()).preparations

    assert tx_plan.bundles[0].protocol_gas == "412345"
    assert prepared.call_gas_limit == 900_000
    assert "protocol_gas" not in prepared.model_dump()


def test_an_operation_sent_from_another_account_is_refused() -> None:
    signer = PreparingSigner(sender="0x" + "99" * 20)

    with pytest.raises(TransactionPlanError, match="was built for"):
        prepare_plan(signer, plan(response("a")), Config())


def test_an_adapter_that_cannot_estimate_is_noted_not_guessed() -> None:
    preparation = prepare_plan(
        PreparingSigner(unavailable=True), plan(response("a")), Config()
    )

    assert preparation.preparations == ()
    assert preparation.blockers == ()
    assert "not estimated before submission" in preparation.messages[0]


def test_execution_carries_the_preparation_into_the_result() -> None:
    signer = PreparingSigner()

    result = run(signer, plan(response("a")))

    assert len(result.preparations) == 1
    assert len(signer.batches) == 1


def test_an_empty_plan_submits_nothing() -> None:
    signer = BatchingSigner()

    result = run(signer, TxPlan(steps=(), summary="nothing to do"))

    assert signer.batches == []
    assert result.status == "success"
