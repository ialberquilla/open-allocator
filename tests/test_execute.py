from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import pytest

from open_allocator.core.types import (
    Allocation,
    AllocationLeg,
    Policy,
    PolicyAllowed,
    PolicyCaps,
    PolicyGates,
    PolicyWallet,
    TxPlan,
    TxStep,
    Vault,
)
from open_allocator.exec.erc4337_paymaster import (
    Erc4337PaymasterSigner,
    PaymasterRejected,
    PaymasterUnsupportedChain,
    PaymasterUserOperationRequest,
    PaymasterUserOperationSubmission,
)
from open_allocator.exec.execute import (
    ExecutionBroadcastError,
    GasCheck,
    GasPreflightError,
    PolicyCheckFailed,
    execute_allocation,
)
from open_allocator.exec.signer import Receipt


@dataclass
class MockOneTxClient:
    responses: list[dict[str, Any]]
    bodies: list[dict[str, object]] = field(default_factory=list)

    def build_buy(self, body: dict[str, object]) -> dict[str, Any]:
        self.bodies.append(body)
        return self.responses.pop(0)


@dataclass
class MockSigner:
    fail_at: int | None = None
    sent: list[tuple[TxStep, str]] = field(default_factory=list)

    def address(self) -> str:
        return "0x0000000000000000000000000000000000000001"

    def send(self, tx: TxStep, rpc_url: str) -> Receipt:
        if self.fail_at is not None and len(self.sent) == self.fail_at:
            raise RuntimeError("boom")
        self.sent.append((tx, rpc_url))
        index = len(self.sent)
        return Receipt(
            transaction_hash=f"0x{index:064x}",
            block_number=index,
            gas_used=21_000,
            status=1,
            from_address=self.address(),
            to_address=tx.to,
        )


@dataclass(frozen=True)
class Config:
    gas_checker: object = lambda _address, chain_id, _rpc_url, _config: GasCheck(
        chain_id=chain_id,
        ok=True,
        balance_wei=1,
        required_wei=1,
        message=f"native gas available on chain {chain_id}",
    )
    _rpc_overrides: dict[int, str] = field(default_factory=lambda: {8453: "rpc://base"})


@dataclass(frozen=True)
class PaymasterConfig:
    signer_mode: str = "erc4337-paymaster"
    paymaster_bundler_url: str = "https://bundler.example"
    paymaster_url: str = "https://paymaster.example"
    paymaster_account_address: str = "0x0000000000000000000000000000000000000aaa"
    paymaster_entry_point: str = "0x0000000000000000000000000000000000004337"
    paymaster_usdc_address: str = "0x0000000000000000000000000000000000000c0c"
    paymaster_supported_chain_ids: tuple[int, ...] | None = (8453,)
    gas_checker: object = lambda *_args: (_ for _ in ()).throw(
        AssertionError("native gas checker must not run in paymaster mode")
    )


def allocation(*instrument_ids: str) -> Allocation:
    return Allocation(
        legs=tuple(
            AllocationLeg(
                instrument_id=instrument_id,
                weight=1 / len(instrument_ids),
                usd=100,
            )
            for instrument_id in instrument_ids
        ),
        total_usd=100 * len(instrument_ids),
        metadata={},
    )


def permissive_policy() -> Policy:
    return Policy(
        wallet=PolicyWallet(mode="self-custody", signer="local-eoa"),
        allowed=PolicyAllowed(
            protocols=None,
            chains=None,
            assets=("USDC",),
            curators=None,
        ),
        caps=PolicyCaps(
            max_weight_per_instrument=1,
            max_weight_per_protocol=1,
            max_weight_per_curator=1,
            max_weight_per_chain=1,
            min_instrument_tvl_usd=1,
            max_reward_dependence=1,
        ),
        gates=PolicyGates(
            new_instrument_needs_approval=True,
            autonomous_rebalance=False,
            max_deploy_per_cycle_usd=1_000_000,
        ),
    )


def vault(instrument_id: str, chain_id: int = 8453) -> Vault:
    return Vault(
        instrument_id=instrument_id,
        protocol="morpho",
        chain_id=chain_id,
        asset="USDC",
        apy=0.04,
        tvl_usd=1_000_000,
        curator="curator-a",
        reward_dependence=0.1,
    )


def buy_response(*transactions: dict[str, object], **extra: object) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "operationId": "op-1",
        "transactions": list(transactions),
    }
    payload.update(extra)
    return payload


def tx(
    to_suffix: int,
    data: str,
    *,
    value: int | str = 0,
    chain_id: int = 8453,
    type_: str | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "to": f"0x{to_suffix:040x}",
        "data": data,
        "value": value,
        "chainId": chain_id,
    }
    if type_ is not None:
        payload["type"] = type_
    return payload


def test_happy_path_preserves_order_and_collects_receipts() -> None:
    client = MockOneTxClient(
        [
            buy_response(
                tx(2, "0xapprove", type_="approve"),
                tx(3, "0xbuy", value="5", type_="deposit"),
            )
        ]
    )
    signer = MockSigner()

    report = execute_allocation(
        client,
        signer,
        allocation("base-morpho-usdc"),
        permissive_policy(),
        confirm=True,
        known_instruments=[vault("base-morpho-usdc")],
        config=Config(),
    )

    assert report.status == "success"
    assert [step.kind for step in report.plan.steps] == ["approve", "buy"]
    assert [sent[0].data for sent in signer.sent] == ["0xapprove", "0xbuy"]
    assert [sent[1] for sent in signer.sent] == ["rpc://base", "rpc://base"]
    assert [receipt.transaction_hash for receipt in report.receipts] == [
        f"0x{1:064x}",
        f"0x{2:064x}",
    ]
    assert client.bodies == [
        {
            "userAddress": signer.address(),
            "instrumentId": "base-morpho-usdc",
            "amountUsdc": "100",
        }
    ]


def test_source_chain_id_metadata_override_is_sent() -> None:
    client = MockOneTxClient(
        [
            buy_response(
                tx(2, "0xapprove", type_="approve"),
                tx(3, "0xbuy", value="5", type_="deposit"),
            )
        ]
    )
    signer = MockSigner()

    alloc = allocation("base-morpho-usdc")
    alloc = alloc.model_copy(update={"metadata": {"source_chain_id": 42161}})

    execute_allocation(
        client,
        signer,
        alloc,
        permissive_policy(),
        confirm=True,
        known_instruments=[vault("base-morpho-usdc")],
        config=Config(),
    )

    # Explicit override still pins the source chain; default omits it so 1Tx
    # can auto-route from whichever chain actually holds USDC.
    assert client.bodies == [
        {
            "userAddress": signer.address(),
            "instrumentId": "base-morpho-usdc",
            "amountUsdc": "100",
            "sourceChainId": 42161,
        }
    ]


def test_hex_numeric_transaction_fields_are_accepted() -> None:
    client = MockOneTxClient(
        [buy_response(tx(2, "0xbuy", value="0x5", chain_id="0x2105"))]
    )
    signer = MockSigner()

    report = execute_allocation(
        client,
        signer,
        allocation("base-morpho-usdc"),
        permissive_policy(),
        confirm=True,
        known_instruments=[vault("base-morpho-usdc")],
        config=Config(),
    )

    assert report.plan.steps[0].value == 5
    assert report.plan.steps[0].chain_id == 8453
    assert signer.sent[0][0].value == 5


def test_policy_violation_aborts_before_client_or_signer() -> None:
    client = MockOneTxClient([buy_response(tx(2, "0x"))])
    signer = MockSigner()

    with pytest.raises(PolicyCheckFailed):
        execute_allocation(
            client,
            signer,
            allocation("new-vault"),
            permissive_policy(),
            confirm=True,
            known_instruments=[],
            config=Config(),
        )

    assert client.bodies == []
    assert signer.sent == []


def test_without_confirm_returns_plan_and_does_not_send() -> None:
    client = MockOneTxClient([buy_response(tx(2, "0xapprove"), tx(3, "0xbuy"))])
    signer = MockSigner()

    plan = execute_allocation(
        client,
        signer,
        allocation("base-morpho-usdc"),
        permissive_policy(),
        known_instruments=[vault("base-morpho-usdc")],
        config=Config(),
    )

    assert isinstance(plan, TxPlan)
    assert [step.data for step in plan.steps] == ["0xapprove", "0xbuy"]
    assert signer.sent == []


def test_gas_preflight_failure_aborts_before_sends() -> None:
    config = Config(
        gas_checker=lambda _address, chain_id, _rpc_url, _config: GasCheck(
            chain_id=chain_id,
            ok=False,
            balance_wei=0,
            required_wei=1,
            message=f"insufficient native gas on chain {chain_id}",
        )
    )
    client = MockOneTxClient([buy_response(tx(2, "0x"))])
    signer = MockSigner()

    with pytest.raises(GasPreflightError) as error:
        execute_allocation(
            client,
            signer,
            allocation("base-morpho-usdc"),
            permissive_policy(),
            confirm=True,
            known_instruments=[vault("base-morpho-usdc")],
            config=config,
        )

    assert error.value.checks[0].ok is False
    assert signer.sent == []


def test_missing_rpc_fails_preflight_before_sends() -> None:
    client = MockOneTxClient([buy_response(tx(2, "0x", chain_id=999999))])
    signer = MockSigner()

    with pytest.raises(GasPreflightError, match="missing RPC"):
        execute_allocation(
            client,
            signer,
            allocation("unknown-chain-vault"),
            permissive_policy(),
            confirm=True,
            known_instruments=[vault("unknown-chain-vault", chain_id=999999)],
            config=Config(_rpc_overrides={}),
        )

    assert signer.sent == []


def test_retry_after_mid_book_failure_skips_completed_leg() -> None:
    store: dict[str, object] = {}
    first_client = MockOneTxClient(
        [
            buy_response(tx(2, "0xleg0")),
            buy_response(tx(3, "0xleg1")),
        ]
    )
    first_signer = MockSigner(fail_at=1)

    with pytest.raises(ExecutionBroadcastError):
        execute_allocation(
            first_client,
            first_signer,
            allocation("vault-a", "vault-b"),
            permissive_policy(),
            confirm=True,
            known_instruments=[vault("vault-a"), vault("vault-b")],
            config=Config(),
            idempotency_store=store,
        )

    assert "leg:0:vault-a" in store
    retry_client = MockOneTxClient([buy_response(tx(4, "0xleg1-retry"))])
    retry_signer = MockSigner()

    report = execute_allocation(
        retry_client,
        retry_signer,
        allocation("vault-a", "vault-b"),
        permissive_policy(),
        confirm=True,
        known_instruments=[vault("vault-a"), vault("vault-b")],
        config=Config(),
        idempotency_store=store,
    )

    assert report.status == "success"
    assert [body["instrumentId"] for body in retry_client.bodies] == ["vault-b"]
    assert [sent[0].data for sent in retry_signer.sent] == ["0xleg1-retry"]


def test_retry_skips_completed_step_within_incomplete_leg() -> None:
    store: dict[str, object] = {"leg:0:vault-a:step:0": True}
    client = MockOneTxClient([buy_response(tx(2, "0xapprove"), tx(3, "0xbuy"))])
    signer = MockSigner()

    report = execute_allocation(
        client,
        signer,
        allocation("vault-a"),
        permissive_policy(),
        confirm=True,
        known_instruments=[vault("vault-a")],
        config=Config(),
        idempotency_store=store,
    )

    assert [step.status for step in report.steps] == ["skipped", "sent"]
    assert [sent[0].data for sent in signer.sent] == ["0xbuy"]
    assert "leg:0:vault-a" in store


def test_cross_chain_pending_reports_in_progress_not_error() -> None:
    client = MockOneTxClient(
        [
            buy_response(
                tx(2, "0xsource-router"),
                status="confirming_source",
                isCrossChain=True,
            )
        ]
    )
    signer = MockSigner()

    report = execute_allocation(
        client,
        signer,
        allocation("base-to-arb-vault"),
        permissive_policy(),
        confirm=True,
        known_instruments=[vault("base-to-arb-vault")],
        config=Config(),
    )

    assert report.status == "in_progress"
    assert report.in_progress is True
    assert report.receipts
    assert report.messages == ("cross-chain buy is in progress",)


def test_paymaster_mode_builds_user_operation_without_native_gas_check() -> None:
    adapter = MockPaymasterAdapter()
    signer = Erc4337PaymasterSigner(
        adapter=adapter,
        entry_point="0x0000000000000000000000000000000000004337",
        usdc_address="0x0000000000000000000000000000000000000c0c",
    )
    client = MockOneTxClient([buy_response(tx(2, "0xbuy", value=5))])

    report = execute_allocation(
        client,
        signer,
        allocation("base-morpho-usdc"),
        permissive_policy(),
        confirm=True,
        known_instruments=[vault("base-morpho-usdc")],
        config=PaymasterConfig(),
    )

    assert report.gas_checks[0].required_wei == 0
    # No static surcharge is quoted: Pimlico's fee lives inside the exchangeRate
    # from pimlico_getTokenQuotes, so preflight names where the cost comes from
    # rather than inventing a percentage. (The old message claimed "+10%" on
    # Base and nothing elsewhere, which understated 14 of 16 chains.)
    assert report.gas_checks[0].message == (
        "gas paid in USDC via pimlico on chain 8453 "
        "(rate quoted at submission, provider fee included)"
    )
    assert len(adapter.requests) == 1
    request = adapter.requests[0]
    assert request.sender == signer.address()
    assert request.chain_id == 8453
    assert request.call_data.to == "0x0000000000000000000000000000000000000002"
    assert request.call_data.data == "0xbuy"
    assert request.call_data.value == 5
    assert request.gas_token == "USDC"
    assert request.gas_token_address == "0x0000000000000000000000000000000000000c0c"
    assert report.receipts[0].execution_status == "user_operation_submitted"


def test_paymaster_preflight_unsupported_chain_is_typed_error() -> None:
    signer = Erc4337PaymasterSigner(
        adapter=MockPaymasterAdapter(),
        entry_point="0x0000000000000000000000000000000000004337",
        usdc_address="0x0000000000000000000000000000000000000c0c",
    )
    client = MockOneTxClient([buy_response(tx(2, "0xbuy", chain_id=1))])

    with pytest.raises(PaymasterUnsupportedChain, match="chain 1"):
        execute_allocation(
            client,
            signer,
            allocation("eth-morpho-usdc"),
            permissive_policy(),
            confirm=True,
            known_instruments=[vault("eth-morpho-usdc", chain_id=1)],
            config=PaymasterConfig(paymaster_supported_chain_ids=(8453,)),
        )


def test_paymaster_rejection_surfaces_typed_actionable_error() -> None:
    adapter = MockPaymasterAdapter(reject=True)
    signer = Erc4337PaymasterSigner(
        adapter=adapter,
        entry_point="0x0000000000000000000000000000000000004337",
        usdc_address="0x0000000000000000000000000000000000000c0c",
    )
    client = MockOneTxClient([buy_response(tx(2, "0xbuy"))])

    with pytest.raises(PaymasterRejected, match="USDC allowance"):
        execute_allocation(
            client,
            signer,
            allocation("base-morpho-usdc"),
            permissive_policy(),
            confirm=True,
            known_instruments=[vault("base-morpho-usdc")],
            config=PaymasterConfig(),
        )


def test_policy_violation_blocks_paymaster_submission() -> None:
    adapter = MockPaymasterAdapter()
    signer = Erc4337PaymasterSigner(
        adapter=adapter,
        entry_point="0x0000000000000000000000000000000000004337",
        usdc_address="0x0000000000000000000000000000000000000c0c",
    )
    client = MockOneTxClient([buy_response(tx(2, "0xbuy"))])

    with pytest.raises(PolicyCheckFailed):
        execute_allocation(
            client,
            signer,
            allocation("new-vault"),
            permissive_policy(),
            confirm=True,
            known_instruments=[],
            config=PaymasterConfig(),
        )

    assert client.bodies == []
    assert adapter.requests == []


@dataclass
class MockPaymasterAdapter:
    address_value: str = "0x0000000000000000000000000000000000000aaa"
    reject: bool = False
    requests: list[PaymasterUserOperationRequest] = field(default_factory=list)

    def address(self) -> str:
        return self.address_value

    def submit_user_operation(
        self,
        request: PaymasterUserOperationRequest,
    ) -> PaymasterUserOperationSubmission:
        if self.reject:
            raise PaymasterRejected("USDC allowance is below paymaster gas budget")
        self.requests.append(request)
        return PaymasterUserOperationSubmission(
            user_op_hash=f"0xuserop{len(self.requests):057d}",
            status="submitted",
        )


@pytest.mark.integration
def test_live_tiny_base_deposit_skips_without_creds_or_explicit_gate() -> None:
    required = [
        "ONE_TX_API_URL",
        "ONE_TX_API_KEY",
        "ONE_TX_PRIVATE_KEY",
        "RPC_URL_8453",
        "OPEN_ALLOCATOR_LIVE_TINY_DEPOSIT",
        "OPEN_ALLOCATOR_LIVE_TINY_DEPOSIT_INSTRUMENT_ID",
        "OPEN_ALLOCATOR_LIVE_TINY_DEPOSIT_USD",
    ]
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        pytest.skip(f"live tiny deposit requires: {', '.join(missing)}")

    if os.environ["OPEN_ALLOCATOR_LIVE_TINY_DEPOSIT"] != "1":
        pytest.skip("set OPEN_ALLOCATOR_LIVE_TINY_DEPOSIT=1 to opt in")

    amount_usd = float(os.environ["OPEN_ALLOCATOR_LIVE_TINY_DEPOSIT_USD"])
    if amount_usd <= 0 or amount_usd > 1:
        raise ValueError("live tiny deposit amount must be > 0 and <= 1 USD")

    from open_allocator.exec.client import OneTxClient
    from open_allocator.exec.config import AllocatorConfig
    from open_allocator.exec.signer import LocalEoaSigner

    instrument_id = os.environ["OPEN_ALLOCATOR_LIVE_TINY_DEPOSIT_INSTRUMENT_ID"]
    live_allocation = Allocation(
        legs=(
            AllocationLeg(
                instrument_id=instrument_id,
                weight=1,
                usd=amount_usd,
            ),
        ),
        total_usd=amount_usd,
        metadata={"source_chain_id": 8453},
    )
    config = AllocatorConfig()
    signer = LocalEoaSigner(config)
    with OneTxClient(config) as client:
        report = execute_allocation(
            client,
            signer,
            live_allocation,
            permissive_policy(),
            confirm=True,
            known_instruments=[vault(instrument_id)],
            config=config,
            idempotency_store={},
        )

    assert report.status in {"success", "in_progress"}
