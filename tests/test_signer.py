from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from eth_account import Account
from pydantic import SecretStr
from web3 import EthereumTesterProvider, Web3

from open_allocator.core.types import (
    Allocation,
    AllocationLeg,
    Policy,
    PolicyAllowed,
    PolicyCaps,
    PolicyGates,
    PolicyWallet,
    TxStep,
    Vault,
)
from open_allocator.exec.erc4337_paymaster import (
    Erc4337PaymasterSigner,
    PaymasterUserOperationRequest,
    PaymasterUserOperationSubmission,
)
from open_allocator.exec.execute import GasCheck, execute_allocation
from open_allocator.exec.remote_signer import (
    GenericHttpRemoteSignerAdapter,
    RemoteSignerPolicyRejected,
)
from open_allocator.exec.safe_signer import (
    SafeGuardPolicy,
    SafeGuardRejected,
    SafeProposal,
    SafeSignerThresholdError,
    SafeTransaction,
)
from open_allocator.exec.signer import (
    LocalEoaSigner,
    Receipt,
    RemoteSigner,
    SafeSigner,
    Signer,
    TransactionReverted,
    signer_from_config,
)

TEST_PRIVATE_KEY = "0x" + "11" * 32
RPC_URL = "eth-tester://local"
REMOTE_ADDRESS = "0x00000000000000000000000000000000000000aa"
SAFE_ADDRESS = "0x0000000000000000000000000000000000000afe"


@dataclass(frozen=True)
class SignerConfig:
    private_key: SecretStr | None
    signer_mode: str = "local-eoa"
    remote_signer_provider: str | None = None
    remote_signer_url: str | None = None
    remote_signer_credential: SecretStr | None = None
    remote_signer_key_id: str | None = None
    safe_address: str | None = None
    safe_transaction_service_url: str | None = None
    safe_chain_id: int | None = None
    safe_proposer_address: str | None = None
    safe_proposer_credential: SecretStr | None = None
    paymaster_provider: str | None = None
    paymaster_bundler_url: str | None = None
    paymaster_bundler_credential: SecretStr | None = None
    paymaster_url: str | None = None
    paymaster_credential: SecretStr | None = None
    paymaster_account_address: str | None = None
    paymaster_account_type: str = "smart-account"
    paymaster_entry_point: str | None = None
    paymaster_usdc_address: str | None = None
    paymaster_supported_chain_ids: tuple[int, ...] | None = None


@pytest.fixture
def tester_web3() -> Web3:
    return Web3(EthereumTesterProvider())


@pytest.fixture
def signer_config() -> SignerConfig:
    return SignerConfig(private_key=SecretStr(TEST_PRIVATE_KEY))


def make_signer(w3: Web3, config: SignerConfig) -> LocalEoaSigner:
    return LocalEoaSigner(config, web3_factory=lambda _rpc_url: w3)


def fund_signer(w3: Web3, signer: LocalEoaSigner) -> None:
    tx_hash = w3.eth.send_transaction(
        {
            "from": w3.eth.accounts[0],
            "to": signer.address(),
            "value": w3.to_wei(1, "ether"),
        }
    )
    w3.eth.wait_for_transaction_receipt(tx_hash)


def test_address_matches_account_derived_from_test_key(
    tester_web3: Web3,
    signer_config: SignerConfig,
) -> None:
    signer = make_signer(tester_web3, signer_config)

    assert signer.address() == Account.from_key(TEST_PRIVATE_KEY).address


def test_send_signs_broadcasts_and_handles_nonce_across_sequential_sends(
    tester_web3: Web3,
    signer_config: SignerConfig,
) -> None:
    signer = make_signer(tester_web3, signer_config)
    fund_signer(tester_web3, signer)
    recipient = tester_web3.eth.accounts[1]

    first_receipt = signer.send(
        TxStep(
            to=recipient,
            data="0x",
            value=123,
            chain_id=tester_web3.eth.chain_id,
            kind="buy",
        ),
        RPC_URL,
    )
    second_receipt = signer.send(
        TxStep(
            to=recipient,
            data="0x",
            value=456,
            chain_id=tester_web3.eth.chain_id,
            kind="sell",
        ),
        RPC_URL,
    )

    assert first_receipt.status == 1
    assert second_receipt.status == 1
    assert first_receipt.from_address == signer.address()
    assert second_receipt.from_address == signer.address()
    assert tester_web3.eth.get_transaction_count(signer.address()) == 2
    assert tester_web3.eth.get_transaction(first_receipt.transaction_hash)["nonce"] == 0
    second_transaction = tester_web3.eth.get_transaction(
        second_receipt.transaction_hash
    )
    assert second_transaction["nonce"] == 1


def test_reverted_tx_surfaces_typed_error(
    tester_web3: Web3,
    signer_config: SignerConfig,
) -> None:
    signer = make_signer(tester_web3, signer_config)
    fund_signer(tester_web3, signer)
    reverting_contract = deploy_reverting_contract(tester_web3)

    with pytest.raises(TransactionReverted) as error:
        signer.send(
            TxStep(
                to=reverting_contract,
                data="0x",
                value=0,
                chain_id=tester_web3.eth.chain_id,
                kind="approve",
            ),
            RPC_URL,
        )

    assert "reverted" in str(error.value)


def test_signer_interface_conformance(
    tester_web3: Web3,
    signer_config: SignerConfig,
) -> None:
    assert isinstance(make_signer(tester_web3, signer_config), Signer)
    assert isinstance(RemoteSigner(), Signer)
    assert isinstance(SafeSigner(), Signer)
    assert isinstance(Erc4337PaymasterSigner(), Signer)


def test_remote_signer_requests_remote_signature_without_raw_key_material() -> None:
    fake_web3 = FakeWeb3(chain_id=8453)
    adapter = MockRemoteAdapter(address_value=REMOTE_ADDRESS)
    signer = RemoteSigner(adapter=adapter, web3_factory=lambda _rpc_url: fake_web3)

    receipt = signer.send(
        TxStep(
            to="0x00000000000000000000000000000000000000bb",
            data="0x1234",
            value=42,
            chain_id=8453,
            kind="buy",
        ),
        "rpc://base",
    )

    assert adapter.address_calls == 1
    assert len(adapter.transactions) == 1
    assert adapter.transactions[0] == {
        "to": Web3.to_checksum_address(
            "0x00000000000000000000000000000000000000bb"
        ),
        "data": "0x1234",
        "value": 42,
        "chainId": 8453,
        "nonce": 7,
        "gasPrice": 125,
        "gas": 25_200,
    }
    assert fake_web3.eth.sent_raw_transactions == ["0xsigned"]
    assert receipt.status == 1
    assert receipt.from_address == REMOTE_ADDRESS
    assert "private_key" not in vars(signer)
    assert "_account" not in vars(signer)
    assert not any(
        isinstance(value, bytes | bytearray) for value in vars(signer).values()
    )
    assert TEST_PRIVATE_KEY not in repr(signer)


def test_remote_signer_policy_rejection_surfaces_typed_error() -> None:
    fake_web3 = FakeWeb3(chain_id=8453)
    adapter = MockRemoteAdapter(address_value=REMOTE_ADDRESS, reject=True)
    signer = RemoteSigner(adapter=adapter, web3_factory=lambda _rpc_url: fake_web3)

    with pytest.raises(RemoteSignerPolicyRejected, match="provider policy rejected"):
        signer.send(
            TxStep(
                to="0x00000000000000000000000000000000000000bb",
                data="0x1234",
                value=42,
                chain_id=8453,
                kind="approve",
            ),
            "rpc://base",
        )

    assert fake_web3.eth.sent_raw_transactions == []


def test_safe_signer_wraps_identical_onetx_step_calldata() -> None:
    adapter = MockSafeTransactionServiceAdapter(threshold=2)
    signer = SafeSigner(adapter=adapter)
    tx = TxStep(
        to="0x00000000000000000000000000000000000000bb",
        data="0x1234abcd",
        value=42,
        chain_id=8453,
        kind="buy",
    )

    receipt = signer.send(tx, "rpc://base")

    assert len(adapter.proposed) == 1
    safe_tx = adapter.proposed[0]
    assert safe_tx.to == tx.to
    assert safe_tx.data == tx.data
    assert safe_tx.value == tx.value
    assert safe_tx.chain_id == tx.chain_id
    assert receipt.transaction_hash == receipt.safe_tx_hash
    assert receipt.pending is True
    assert receipt.execution_status == "safe_proposed"
    assert receipt.status == 0
    assert receipt.block_number == 0
    assert "pending threshold" in str(receipt.message)


def test_safe_signer_collects_signatures_then_executes_after_threshold() -> None:
    adapter = MockSafeTransactionServiceAdapter(threshold=2)
    signer = SafeSigner(adapter=adapter)
    proposal_receipt = signer.send(
        TxStep(
            to="0x00000000000000000000000000000000000000bb",
            data="0x1234",
            value=42,
            chain_id=8453,
            kind="buy",
        ),
        "rpc://base",
    )
    assert proposal_receipt.safe_tx_hash is not None

    signer.collect_signature(
        proposal_receipt.safe_tx_hash,
        signer_address="0x0000000000000000000000000000000000000001",
    )
    with pytest.raises(SafeSignerThresholdError, match="1/2"):
        signer.execute(proposal_receipt.safe_tx_hash, "rpc://base")

    signer.collect_signature(
        proposal_receipt.safe_tx_hash,
        signer_address="0x0000000000000000000000000000000000000002",
    )
    receipt = signer.execute(proposal_receipt.safe_tx_hash, "rpc://base")

    assert receipt.status == 1
    assert receipt.pending is False
    assert receipt.execution_status == "safe_executed"
    assert adapter.executed == [proposal_receipt.safe_tx_hash]


def test_safe_guard_rejects_tx_outside_policy() -> None:
    guard = SafeGuardPolicy(
        policy=safe_policy(allowed_chains=(8453,)),
        allowed_targets=("0x00000000000000000000000000000000000000bb",),
    )
    allowed_tx = TxStep(
        to="0x00000000000000000000000000000000000000bb",
        data="0x",
        value=0,
        chain_id=8453,
        kind="approve",
    )

    guard.validate(allowed_tx)
    with pytest.raises(SafeGuardRejected, match="chain 1"):
        guard.validate(allowed_tx.model_copy(update={"chain_id": 1}))
    with pytest.raises(SafeGuardRejected, match="target"):
        guard.validate(
            allowed_tx.model_copy(
                update={"to": "0x00000000000000000000000000000000000000cc"}
            )
        )


def test_executor_can_call_safe_signer_without_executor_changes() -> None:
    adapter = MockSafeTransactionServiceAdapter(threshold=2)
    signer = SafeSigner(adapter=adapter)
    client = MockOneTxClient(
        response={
            "transactions": [
                {
                    "to": "0x00000000000000000000000000000000000000bb",
                    "data": "0x1234",
                    "value": 42,
                    "chainId": 8453,
                }
            ]
        }
    )

    report = execute_allocation(
        client,
        signer,
        Allocation(
            legs=(AllocationLeg(instrument_id="vault-1", weight=1, usd=100),),
            total_usd=100,
            metadata={},
        ),
        safe_policy(),
        confirm=True,
        known_instruments=(safe_vault("vault-1"),),
        config=SafeExecutorConfig(),
    )

    assert report.receipts[0].pending is True
    assert report.receipts[0].execution_status == "safe_proposed"
    assert adapter.proposed[0].to == "0x00000000000000000000000000000000000000bb"


def test_paymaster_signer_can_wrap_safe_account_at_signer_seam() -> None:
    safe_adapter = MockSafeTransactionServiceAdapter()
    safe_signer = SafeSigner(adapter=safe_adapter)
    paymaster_adapter = MockPaymasterAdapter()
    signer = Erc4337PaymasterSigner(
        adapter=paymaster_adapter,
        account=safe_signer,
        account_type="safe",
        entry_point="0x0000000000000000000000000000000000004337",
        usdc_address="0x0000000000000000000000000000000000000c0c",
    )
    client = MockOneTxClient(
        response={
            "transactions": [
                {
                    "to": "0x00000000000000000000000000000000000000bb",
                    "data": "0x1234",
                    "value": 42,
                    "chainId": 8453,
                }
            ]
        }
    )

    report = execute_allocation(
        client,
        signer,
        Allocation(
            legs=(AllocationLeg(instrument_id="vault-1", weight=1, usd=100),),
            total_usd=100,
            metadata={},
        ),
        safe_policy(),
        confirm=True,
        known_instruments=(safe_vault("vault-1"),),
        config=PaymasterExecutorConfig(),
    )

    assert client.response["transactions"][0]["data"] == "0x1234"
    assert paymaster_adapter.requests[0].sender == SAFE_ADDRESS
    assert paymaster_adapter.requests[0].account_type == "safe"
    assert paymaster_adapter.requests[0].call_data.data == "0x1234"
    assert report.receipts[0].execution_status == "user_operation_submitted"


def test_generic_http_adapter_maps_provider_policy_rejection_to_typed_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={
                "error": {
                    "code": "POLICY_REJECTED",
                    "message": "spend exceeds policy",
                }
            },
        )

    adapter = GenericHttpRemoteSignerAdapter(
        provider_url="https://signer.example",
        credential=SecretStr("remote-credential"),
        key_id="key-1",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(RemoteSignerPolicyRejected):
        adapter.sign_transaction({"chainId": 8453})


def test_repr_redacts_private_key(
    tester_web3: Web3,
    signer_config: SignerConfig,
) -> None:
    signer = make_signer(tester_web3, signer_config)

    signer_repr = repr(signer)

    assert TEST_PRIVATE_KEY not in signer_repr
    assert "private_key=<redacted>" in signer_repr
    assert signer.address() in signer_repr


@pytest.mark.parametrize(
    ("mode", "expected_type"),
    [
        ("local-eoa", LocalEoaSigner),
        ("remote", RemoteSigner),
        ("safe", SafeSigner),
        ("erc4337-paymaster", Erc4337PaymasterSigner),
    ],
)
def test_signer_from_config_registration_points(
    tester_web3: Web3,
    mode: str,
    expected_type: type[object],
) -> None:
    config = SignerConfig(private_key=SecretStr(TEST_PRIVATE_KEY), signer_mode=mode)
    if mode == "remote":
        config = SignerConfig(
            private_key=SecretStr(TEST_PRIVATE_KEY),
            signer_mode=mode,
            remote_signer_provider="generic-http",
            remote_signer_url="https://signer.example",
            remote_signer_credential=SecretStr("remote-credential"),
            remote_signer_key_id="key-1",
        )
    if mode == "safe":
        config = SignerConfig(
            private_key=None,
            signer_mode=mode,
            safe_address=SAFE_ADDRESS,
            safe_transaction_service_url="https://safe.example",
            safe_chain_id=8453,
        )
    if mode == "erc4337-paymaster":
        config = SignerConfig(
            private_key=None,
            signer_mode=mode,
            paymaster_provider="generic-http",
            paymaster_bundler_url="https://bundler.example",
            paymaster_url="https://paymaster.example",
            paymaster_account_address="0x0000000000000000000000000000000000000aaa",
            paymaster_entry_point="0x0000000000000000000000000000000000004337",
            paymaster_usdc_address="0x0000000000000000000000000000000000000c0c",
        )

    signer = signer_from_config(config, web3_factory=lambda _rpc_url: tester_web3)

    assert isinstance(signer, expected_type)
    if mode == "safe":
        assert signer.address() == SAFE_ADDRESS
    if mode == "erc4337-paymaster":
        assert signer.address() == "0x0000000000000000000000000000000000000aaa"


def deploy_reverting_contract(w3: Web3) -> str:
    creation_code = "0x6005600c60003960056000f360006000fd"
    tx_hash = w3.eth.send_transaction(
        {"from": w3.eth.accounts[0], "data": creation_code, "gas": 100_000}
    )
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
    assert receipt.status == 1
    assert receipt.contractAddress is not None
    return str(receipt.contractAddress)


def safe_policy(allowed_chains: tuple[int, ...] | None = None) -> Policy:
    return Policy(
        wallet=PolicyWallet(mode="self-custody", signer="safe"),
        allowed=PolicyAllowed(
            protocols=None,
            chains=allowed_chains,
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


def safe_vault(instrument_id: str) -> Vault:
    return Vault(
        instrument_id=instrument_id,
        protocol="morpho",
        chain_id=8453,
        asset="USDC",
        apy=0.04,
        tvl_usd=1_000_000,
        curator="curator-a",
        reward_dependence=0.1,
    )


@dataclass
class MockOneTxClient:
    response: dict[str, Any]

    def build_buy(self, _body: dict[str, object]) -> dict[str, Any]:
        return self.response


@dataclass(frozen=True)
class SafeExecutorConfig:
    gas_checker: object = lambda _address, chain_id, _rpc_url, _config: GasCheck(
        chain_id=chain_id,
        ok=True,
        balance_wei=1,
        required_wei=1,
        message=f"native gas available on chain {chain_id}",
    )
    _rpc_overrides: dict[int, str] = field(default_factory=lambda: {8453: "rpc://base"})


@dataclass(frozen=True)
class PaymasterExecutorConfig:
    signer_mode: str = "erc4337-paymaster"
    paymaster_bundler_url: str = "https://bundler.example"
    paymaster_url: str = "https://paymaster.example"
    paymaster_account_address: str = SAFE_ADDRESS
    paymaster_entry_point: str = "0x0000000000000000000000000000000000004337"
    paymaster_usdc_address: str = "0x0000000000000000000000000000000000000c0c"
    paymaster_supported_chain_ids: tuple[int, ...] = (8453,)


@dataclass
class MockSafeTransactionServiceAdapter:
    safe_address: str = SAFE_ADDRESS
    safe_chain_id: int = 8453
    threshold: int = 2
    proposed: list[SafeTransaction] = field(default_factory=list)
    signatures: dict[str, set[str]] = field(default_factory=dict)
    proposals: dict[str, SafeProposal] = field(default_factory=dict)
    executed: list[str] = field(default_factory=list)

    def address(self) -> str:
        return self.safe_address

    def chain_id(self) -> int:
        return self.safe_chain_id

    def propose_transaction(
        self,
        transaction: SafeTransaction,
        *,
        proposer: object | None = None,
        rpc_url: str,
    ) -> SafeProposal:
        self.proposed.append(transaction)
        safe_tx_hash = f"0xsafe{len(self.proposed):060d}"
        proposal = SafeProposal(
            safe_address=self.safe_address,
            safe_tx_hash=safe_tx_hash,
            transaction=transaction,
            status="pending",
            confirmations=0,
            threshold=self.threshold,
            proposal_id=safe_tx_hash,
            service_url="mock://safe-service",
        )
        self.proposals[safe_tx_hash] = proposal
        self.signatures[safe_tx_hash] = set()
        return proposal

    def get_transaction(self, safe_tx_hash: str) -> SafeProposal:
        return self._proposal(safe_tx_hash)

    def submit_confirmation(
        self,
        safe_tx_hash: str,
        *,
        signer_address: str,
        signature: str | None = None,
    ) -> SafeProposal:
        self.signatures[safe_tx_hash].add(signer_address.casefold())
        proposal = self._proposal(safe_tx_hash)
        self.proposals[safe_tx_hash] = proposal
        return proposal

    def execute_transaction(self, safe_tx_hash: str, *, rpc_url: str) -> Receipt:
        proposal = self._proposal(safe_tx_hash)
        if not proposal.executable:
            raise SafeSignerThresholdError("safe transaction is below threshold")
        self.executed.append(safe_tx_hash)
        return Receipt(
            transaction_hash=f"0x{len(self.executed):064x}",
            block_number=len(self.executed),
            gas_used=21_000,
            status=1,
            from_address=self.safe_address,
            to_address=proposal.transaction.to,
            pending=False,
            execution_status="safe_executed",
            safe_tx_hash=safe_tx_hash,
            confirmations=proposal.confirmations,
            threshold=proposal.threshold,
        )

    def _proposal(self, safe_tx_hash: str) -> SafeProposal:
        proposal = self.proposals[safe_tx_hash]
        confirmations = len(self.signatures[safe_tx_hash])
        status = "executable" if confirmations >= self.threshold else "pending"
        return proposal.model_copy(
            update={"confirmations": confirmations, "status": status}
        )


@dataclass
class MockRemoteAdapter:
    address_value: str
    reject: bool = False
    address_calls: int = 0
    transactions: list[dict[str, object]] = field(default_factory=list)

    def address(self) -> str:
        self.address_calls += 1
        return self.address_value

    def sign_transaction(self, transaction: dict[str, object]) -> str:
        if self.reject:
            raise RemoteSignerPolicyRejected("provider policy rejected transaction")
        self.transactions.append(dict(transaction))
        return "0xsigned"


@dataclass
class MockPaymasterAdapter:
    requests: list[PaymasterUserOperationRequest] = field(default_factory=list)

    def address(self) -> str:
        return "0x0000000000000000000000000000000000000aaa"

    def submit_user_operation(
        self,
        request: PaymasterUserOperationRequest,
    ) -> PaymasterUserOperationSubmission:
        self.requests.append(request)
        return PaymasterUserOperationSubmission(
            user_op_hash=f"0xuserop{len(self.requests):057d}",
            status="submitted",
        )


class FakeHash:
    def __init__(self, value: str) -> None:
        self._value = value

    def to_0x_hex(self) -> str:
        return self._value


class FakeEth:
    def __init__(self, chain_id: int) -> None:
        self.chain_id = chain_id
        self.gas_price = 100
        self.sent_raw_transactions: list[bytes | str] = []
        self.estimated_transactions: list[dict[str, object]] = []

    def get_transaction_count(self, _address: str, block: str) -> int:
        assert block == "pending"
        return 7

    def estimate_gas(self, transaction: dict[str, object]) -> int:
        self.estimated_transactions.append(dict(transaction))
        return 21_000

    def send_raw_transaction(self, raw_transaction: bytes | str) -> FakeHash:
        self.sent_raw_transactions.append(raw_transaction)
        return FakeHash(f"0x{1:064x}")

    def wait_for_transaction_receipt(
        self,
        tx_hash: FakeHash,
        *,
        timeout: float,
    ) -> dict[str, Any]:
        assert timeout == 120
        transaction = self.estimated_transactions[-1]
        return {
            "transactionHash": tx_hash,
            "blockNumber": 1,
            "gasUsed": 21_000,
            "status": 1,
            "from": transaction["from"],
            "to": transaction["to"],
            "contractAddress": None,
            "effectiveGasPrice": 125,
        }


class FakeWeb3:
    def __init__(self, chain_id: int) -> None:
        self.eth = FakeEth(chain_id)

    def to_checksum_address(self, value: str) -> str:
        return Web3.to_checksum_address(value)
