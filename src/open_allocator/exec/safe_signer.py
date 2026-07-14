from __future__ import annotations

from collections.abc import Sequence
from typing import Literal, Protocol, runtime_checkable

import httpx
from pydantic import Field

from open_allocator.core.types import FrozenModel, Policy, TxStep
from open_allocator.exec.signer import Receipt, SignerError


class SafeSignerError(SignerError):
    pass


class SafeSignerThresholdError(SafeSignerError):
    pass


class SafeGuardRejected(SafeSignerError):
    pass


class SafeTransaction(FrozenModel):
    safe_address: str
    to: str
    data: str
    value: int = Field(ge=0)
    chain_id: int
    operation: int = Field(default=0, ge=0)
    safe_tx_gas: int = Field(default=0, ge=0)
    base_gas: int = Field(default=0, ge=0)
    gas_price: int = Field(default=0, ge=0)
    gas_token: str | None = None
    refund_receiver: str | None = None
    nonce: int | None = Field(default=None, ge=0)


class SafeProposer(FrozenModel):
    address: str | None = None
    credential: str | None = None


class SafeProposal(FrozenModel):
    safe_address: str
    safe_tx_hash: str
    transaction: SafeTransaction
    status: Literal["proposed", "pending", "executable", "executed"] = "proposed"
    confirmations: int = Field(default=0, ge=0)
    threshold: int | None = Field(default=None, ge=1)
    proposal_id: str | None = None
    service_url: str | None = None

    @property
    def executable(self) -> bool:
        if self.status == "executable":
            return True
        return self.threshold is not None and self.confirmations >= self.threshold


@runtime_checkable
class SafeTransactionServiceAdapter(Protocol):
    def address(self) -> str: ...

    def chain_id(self) -> int: ...

    def propose_transaction(
        self,
        transaction: SafeTransaction,
        *,
        proposer: SafeProposer | None = None,
        rpc_url: str,
    ) -> SafeProposal: ...

    def get_transaction(self, safe_tx_hash: str) -> SafeProposal: ...

    def submit_confirmation(
        self,
        safe_tx_hash: str,
        *,
        signer_address: str,
        signature: str | None = None,
    ) -> SafeProposal: ...

    def execute_transaction(self, safe_tx_hash: str, *, rpc_url: str) -> Receipt: ...


class SafeGuardPolicy:
    def __init__(
        self,
        *,
        policy: Policy | None = None,
        allowed_targets: Sequence[str] | None = None,
    ) -> None:
        self._policy = policy
        self._allowed_targets = (
            None
            if allowed_targets is None
            else frozenset(target.casefold() for target in allowed_targets)
        )

    def validate(self, tx: TxStep) -> None:
        allowed_chains = None
        if self._policy is not None:
            allowed_chains = self._policy.allowed.chains
        if allowed_chains is not None and tx.chain_id not in allowed_chains:
            raise SafeGuardRejected(
                f"safe guard rejected tx: chain {tx.chain_id} is outside policy"
            )
        if (
            self._allowed_targets is not None
            and tx.to.casefold() not in self._allowed_targets
        ):
            raise SafeGuardRejected(
                f"safe guard rejected tx: target {tx.to} is outside policy"
            )


class SafeSigner:
    def __init__(
        self,
        config: object | None = None,
        *,
        adapter: SafeTransactionServiceAdapter | None = None,
        guard: SafeGuardPolicy | None = None,
    ) -> None:
        self._adapter = adapter or (
            _adapter_from_config(config) if config is not None else None
        )
        self._guard = guard
        self._proposer = _proposer_from_config(config)

    def __repr__(self) -> str:
        if self._adapter is None:
            return "SafeSigner(status=<unconfigured>)"
        return "SafeSigner(status=configured)"

    def address(self) -> str:
        return self._require_adapter().address()

    def send(self, tx: TxStep, rpc_url: str) -> Receipt:
        adapter = self._require_adapter()
        if tx.chain_id != adapter.chain_id():
            raise SafeSignerError(
                f"tx chain_id {tx.chain_id} does not match Safe chain_id "
                f"{adapter.chain_id()}"
            )
        if self._guard is not None:
            self._guard.validate(tx)

        safe_tx = SafeTransaction(
            safe_address=adapter.address(),
            to=tx.to,
            data=tx.data,
            value=tx.value,
            chain_id=tx.chain_id,
        )
        proposal = adapter.propose_transaction(
            safe_tx,
            proposer=self._proposer,
            rpc_url=rpc_url,
        )
        return _pending_receipt(proposal)

    def collect_signature(
        self,
        safe_tx_hash: str,
        *,
        signer_address: str,
        signature: str | None = None,
    ) -> SafeProposal:
        return self._require_adapter().submit_confirmation(
            safe_tx_hash,
            signer_address=signer_address,
            signature=signature,
        )

    def execute(self, safe_tx_hash: str, rpc_url: str) -> Receipt:
        adapter = self._require_adapter()
        proposal = adapter.get_transaction(safe_tx_hash)
        if not proposal.executable:
            threshold = (
                "unknown" if proposal.threshold is None else str(proposal.threshold)
            )
            raise SafeSignerThresholdError(
                "safe transaction is pending threshold signatures "
                f"({proposal.confirmations}/{threshold})"
            )
        return adapter.execute_transaction(safe_tx_hash, rpc_url=rpc_url)

    def _require_adapter(self) -> SafeTransactionServiceAdapter:
        if self._adapter is None:
            raise ValueError(
                "SafeSigner requires Safe transaction service configuration"
            )
        return self._adapter


class SafeEthPyTransactionServiceAdapter:
    def __init__(
        self,
        *,
        safe_address: str,
        chain_id: int,
        transaction_service_url: str,
        api_key: str | None = None,
        request_timeout: int = 10,
    ) -> None:
        self._safe_address = safe_address
        self._chain_id = chain_id
        self._transaction_service_url = transaction_service_url.rstrip("/")
        self._api_key = api_key
        self._request_timeout = request_timeout

    def address(self) -> str:
        return self._safe_address

    def chain_id(self) -> int:
        return self._chain_id

    def propose_transaction(
        self,
        transaction: SafeTransaction,
        *,
        proposer: SafeProposer | None = None,
        rpc_url: str,
    ) -> SafeProposal:
        try:
            from safe_eth.eth.ethereum_client import EthereumClient
            from safe_eth.eth.ethereum_network import EthereumNetwork
            from safe_eth.safe.api.transaction_service_api import TransactionServiceApi
            from safe_eth.safe.safe_tx import SafeTx
        except ImportError as error:
            raise SafeSignerError(
                "safe-eth-py is required for Safe service proposals"
            ) from error

        ethereum_client = EthereumClient(rpc_url)
        service = TransactionServiceApi(
            EthereumNetwork(self._chain_id),
            ethereum_client,
            base_url=self._transaction_service_url,
            api_key=self._api_key,
            request_timeout=self._request_timeout,
        )
        safe_tx = SafeTx(
            ethereum_client,
            transaction.safe_address,
            transaction.to,
            transaction.value,
            _hex_bytes(transaction.data),
            transaction.operation,
            transaction.safe_tx_gas,
            transaction.base_gas,
            transaction.gas_price,
            transaction.gas_token,
            transaction.refund_receiver,
            safe_nonce=transaction.nonce,
            chain_id=transaction.chain_id,
        )
        service.post_transaction(safe_tx)
        safe_tx_hash = _to_0x_hex(safe_tx.safe_tx_hash)
        return SafeProposal(
            safe_address=transaction.safe_address,
            safe_tx_hash=safe_tx_hash,
            transaction=transaction,
            status="proposed",
            confirmations=0,
            threshold=None,
            proposal_id=safe_tx_hash,
            service_url=self._transaction_service_url,
        )

    def get_transaction(self, safe_tx_hash: str) -> SafeProposal:
        raise SafeSignerError(
            "SafeEthPyTransactionServiceAdapter execution polling is not configured"
        )

    def submit_confirmation(
        self,
        safe_tx_hash: str,
        *,
        signer_address: str,
        signature: str | None = None,
    ) -> SafeProposal:
        raise SafeSignerError(
            "SafeSigner does not hold co-signer keys; collect signatures in Safe UI "
            "or provide an execution-capable adapter"
        )

    def execute_transaction(self, safe_tx_hash: str, *, rpc_url: str) -> Receipt:
        raise SafeSignerError(
            "SafeSigner will not execute Safe transactions without an "
            "execution-capable adapter"
        )


class GenericHttpSafeTransactionServiceAdapter:
    def __init__(
        self,
        *,
        safe_address: str,
        chain_id: int,
        transaction_service_url: str,
        credential: object | None = None,
        client: httpx.Client | None = None,
        timeout: float = 30,
    ) -> None:
        self._safe_address = safe_address
        self._chain_id = chain_id
        self._transaction_service_url = transaction_service_url.rstrip("/")
        self._credential = credential
        self._client = client or httpx.Client()
        self._timeout = timeout

    def address(self) -> str:
        return self._safe_address

    def chain_id(self) -> int:
        return self._chain_id

    def propose_transaction(
        self,
        transaction: SafeTransaction,
        *,
        proposer: SafeProposer | None = None,
        rpc_url: str,
    ) -> SafeProposal:
        payload = {
            "safe": transaction.safe_address,
            "to": transaction.to,
            "data": transaction.data,
            "value": transaction.value,
            "chainId": transaction.chain_id,
            "operation": transaction.operation,
            "safeTxGas": transaction.safe_tx_gas,
            "baseGas": transaction.base_gas,
            "gasPrice": transaction.gas_price,
            "gasToken": transaction.gas_token,
            "refundReceiver": transaction.refund_receiver,
            "nonce": transaction.nonce,
            "proposer": None if proposer is None else proposer.address,
        }
        response = self._client.post(
            f"{self._transaction_service_url}/proposals",
            headers=_auth_headers(self._credential),
            json=payload,
            timeout=self._timeout,
        )
        body = _json_body(response)
        if response.is_error:
            raise SafeSignerError(
                f"Safe transaction service proposal failed with HTTP "
                f"{response.status_code}"
            )
        safe_tx_hash = _required_string(body, "safeTxHash")
        return SafeProposal(
            safe_address=transaction.safe_address,
            safe_tx_hash=safe_tx_hash,
            transaction=transaction,
            status=_proposal_status(body.get("status")),
            confirmations=_optional_int(body.get("confirmations"), default=0),
            threshold=_optional_threshold(body.get("threshold")),
            proposal_id=_optional_string(body.get("proposalId")),
            service_url=self._transaction_service_url,
        )

    def get_transaction(self, safe_tx_hash: str) -> SafeProposal:
        raise SafeSignerError("generic Safe HTTP adapter cannot reconstruct tx locally")

    def submit_confirmation(
        self,
        safe_tx_hash: str,
        *,
        signer_address: str,
        signature: str | None = None,
    ) -> SafeProposal:
        raise SafeSignerError(
            "generic Safe HTTP adapter is proposal-only; collect signatures in Safe UI"
        )

    def execute_transaction(self, safe_tx_hash: str, *, rpc_url: str) -> Receipt:
        raise SafeSignerError("generic Safe HTTP adapter is proposal-only")


def _adapter_from_config(config: object) -> SafeTransactionServiceAdapter:
    return SafeEthPyTransactionServiceAdapter(
        safe_address=_required_config_value(config, "safe_address"),
        chain_id=int(_required_config_value(config, "safe_chain_id")),
        transaction_service_url=_required_config_value(
            config,
            "safe_transaction_service_url",
        ),
        api_key=_optional_secret_config_value(config, "safe_proposer_credential"),
    )


def _proposer_from_config(config: object | None) -> SafeProposer | None:
    if config is None:
        return None
    address = getattr(config, "safe_proposer_address", None)
    credential = _optional_secret_config_value(config, "safe_proposer_credential")
    if address is None and credential is None:
        return None
    return SafeProposer(address=address, credential=credential)


def _pending_receipt(proposal: SafeProposal) -> Receipt:
    return Receipt(
        transaction_hash=proposal.safe_tx_hash,
        block_number=0,
        gas_used=0,
        status=0,
        from_address=proposal.safe_address,
        to_address=proposal.transaction.to,
        pending=True,
        execution_status="safe_proposed",
        safe_tx_hash=proposal.safe_tx_hash,
        proposal_id=proposal.proposal_id,
        confirmations=proposal.confirmations,
        threshold=proposal.threshold,
        message="Safe transaction proposed; pending threshold signatures/execution",
    )


def _required_config_value(config: object, name: str) -> str:
    value = getattr(config, name, None)
    if value is None:
        raise ValueError(f"{name} is required for SafeSigner")
    return _secret_value(value)


def _optional_secret_config_value(config: object, name: str) -> str | None:
    value = getattr(config, name, None)
    if value is None:
        return None
    return _secret_value(value)


def _secret_value(value: object) -> str:
    get_secret_value = getattr(value, "get_secret_value", None)
    if callable(get_secret_value):
        return str(get_secret_value())
    return str(value)


def _hex_bytes(value: str) -> bytes:
    if value == "0x":
        return b""
    if not value.startswith("0x"):
        raise SafeSignerError("Safe transaction data must be 0x-prefixed hex")
    return bytes.fromhex(value[2:])


def _to_0x_hex(value: object) -> str:
    hex_value = getattr(value, "hex", None)
    if callable(hex_value):
        result = str(hex_value())
        return result if result.startswith("0x") else f"0x{result}"
    if isinstance(value, bytes | bytearray):
        return f"0x{bytes(value).hex()}"
    result = str(value)
    return result if result.startswith("0x") else f"0x{result}"


def _auth_headers(credential: object | None) -> dict[str, str]:
    if credential is None:
        return {}
    return {"Authorization": f"Bearer {_secret_value(credential)}"}


def _json_body(response: httpx.Response) -> dict[str, object]:
    try:
        body = response.json()
    except ValueError as error:
        raise SafeSignerError(
            "Safe transaction service returned invalid JSON"
        ) from error
    if not isinstance(body, dict):
        raise SafeSignerError("Safe transaction service returned non-object JSON")
    return body


def _required_string(body: dict[str, object], key: str) -> str:
    value = body.get(key)
    if not isinstance(value, str) or value == "":
        raise SafeSignerError(f"Safe transaction service response missing {key}")
    return value


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _optional_int(value: object, *, default: int) -> int:
    if value is None:
        return default
    return int(value)


def _optional_threshold(value: object) -> int | None:
    if value is None:
        return None
    return int(value)


def _proposal_status(value: object) -> Literal[
    "proposed",
    "pending",
    "executable",
    "executed",
]:
    if isinstance(value, str) and value in {
        "proposed",
        "pending",
        "executable",
        "executed",
    }:
        return value
    return "proposed"


__all__ = [
    "GenericHttpSafeTransactionServiceAdapter",
    "SafeEthPyTransactionServiceAdapter",
    "SafeGuardPolicy",
    "SafeGuardRejected",
    "SafeProposal",
    "SafeProposer",
    "SafeSigner",
    "SafeSignerError",
    "SafeSignerThresholdError",
    "SafeTransaction",
    "SafeTransactionServiceAdapter",
]
