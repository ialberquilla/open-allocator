from __future__ import annotations

from collections.abc import Callable
from typing import Literal, Protocol, runtime_checkable

from eth_account import Account
from pydantic import Field
from web3 import HTTPProvider, Web3
from web3.exceptions import ContractLogicError, Web3Exception

from open_allocator.core.types import FrozenModel, TxStep


class Receipt(FrozenModel):
    transaction_hash: str
    block_number: int
    gas_used: int = Field(ge=0)
    status: int = Field(ge=0)
    from_address: str
    to_address: str | None = None
    contract_address: str | None = None
    effective_gas_price: int | None = Field(default=None, ge=0)
    pending: bool = False
    execution_status: Literal[
        "mined",
        "safe_proposed",
        "safe_executed",
        "user_operation_submitted",
    ] = "mined"
    safe_tx_hash: str | None = None
    proposal_id: str | None = None
    confirmations: int | None = Field(default=None, ge=0)
    threshold: int | None = Field(default=None, ge=1)
    message: str | None = None


class SignerError(RuntimeError):
    pass


class TransactionBuildError(SignerError):
    pass


class TransactionBroadcastError(SignerError):
    pass


class TransactionReverted(SignerError):
    def __init__(
        self,
        message: str,
        *,
        tx_hash: str | None = None,
        receipt: Receipt | None = None,
    ) -> None:
        self.tx_hash = tx_hash
        self.receipt = receipt
        super().__init__(message)


@runtime_checkable
class Signer(Protocol):
    def address(self) -> str: ...

    def send(self, tx: TxStep, rpc_url: str) -> Receipt: ...


Web3Factory = Callable[[str], Web3]


class LocalEoaSigner:
    def __init__(
        self,
        config: object,
        *,
        web3_factory: Web3Factory | None = None,
        receipt_timeout: float = 120,
    ) -> None:
        private_key = _required_secret(config, "private_key")
        self._account = Account.from_key(private_key)
        self._web3_factory = web3_factory or _http_web3
        self._receipt_timeout = receipt_timeout

    def __repr__(self) -> str:
        return (
            f"LocalEoaSigner(address={self.address()!r}, "
            "private_key=<redacted>)"
        )

    def address(self) -> str:
        return self._account.address

    def send(self, tx: TxStep, rpc_url: str) -> Receipt:
        w3 = self._web3_factory(rpc_url)
        tx_hash: str | None = None

        try:
            transaction = self._transaction_dict(w3, tx)
            signed = self._account.sign_transaction(transaction)
            raw_transaction = signed.raw_transaction
            tx_hash_bytes = w3.eth.send_raw_transaction(raw_transaction)
            tx_hash = tx_hash_bytes.to_0x_hex()
            raw_receipt = w3.eth.wait_for_transaction_receipt(
                tx_hash_bytes,
                timeout=self._receipt_timeout,
            )
        except SignerError:
            raise
        except Web3Exception as error:
            raise TransactionBroadcastError("transaction broadcast failed") from error
        except Exception as error:
            if _is_revert_error(error):
                raise TransactionReverted(
                    "transaction reverted before broadcast"
                ) from error
            raise TransactionBroadcastError("transaction broadcast failed") from error

        receipt = _typed_receipt(raw_receipt)
        if receipt.status != 1:
            raise TransactionReverted(
                f"transaction reverted: {receipt.transaction_hash}",
                tx_hash=tx_hash,
                receipt=receipt,
            )

        return receipt

    def _transaction_dict(self, w3: Web3, tx: TxStep) -> dict[str, object]:
        return _build_transaction_dict(w3, tx, self.address())


def signer_from_config(
    config: object,
    *,
    web3_factory: Web3Factory | None = None,
) -> Signer:
    mode = getattr(config, "signer_mode", None)
    if mode == "local-eoa":
        return LocalEoaSigner(config, web3_factory=web3_factory)
    if mode == "remote":
        from open_allocator.exec.remote_signer import RemoteSigner

        return RemoteSigner(config, web3_factory=web3_factory)
    if mode == "safe":
        from open_allocator.exec.safe_signer import SafeSigner

        return SafeSigner(config)
    if mode == "erc4337-paymaster":
        from open_allocator.exec.erc4337_paymaster import Erc4337PaymasterSigner

        return Erc4337PaymasterSigner(config)
    raise ValueError(f"unknown signer mode: {mode!r}")


def _http_web3(rpc_url: str) -> Web3:
    return Web3(HTTPProvider(rpc_url))


def _required_secret(config: object, name: str) -> str:
    value = getattr(config, name, None)
    if value is None:
        raise ValueError(f"{name} is required for LocalEoaSigner")

    get_secret_value = getattr(value, "get_secret_value", None)
    if callable(get_secret_value):
        return str(get_secret_value())
    return str(value)


def _buffered_gas_price(w3: Web3) -> int:
    gas_price = int(w3.eth.gas_price)
    return gas_price + max(gas_price // 4, 1)


def _build_transaction_dict(w3: Web3, tx: TxStep, address: str) -> dict[str, object]:
    try:
        chain_id = w3.eth.chain_id
        if chain_id != tx.chain_id:
            raise TransactionBuildError(
                f"tx chain_id {tx.chain_id} does not match RPC chain_id {chain_id}"
            )

        transaction: dict[str, object] = {
            "to": w3.to_checksum_address(tx.to),
            "data": tx.data,
            "value": tx.value,
            "chainId": tx.chain_id,
            "nonce": w3.eth.get_transaction_count(address, "pending"),
            "gasPrice": _buffered_gas_price(w3),
        }
        estimate = w3.eth.estimate_gas({"from": address, **transaction})
        transaction["gas"] = estimate + max(estimate // 5, 1)
        return transaction
    except TransactionBuildError:
        raise
    except ContractLogicError as error:
        raise TransactionReverted("transaction reverted during gas estimate") from error
    except Exception as error:
        if _is_revert_error(error):
            raise TransactionReverted(
                "transaction reverted during gas estimate"
            ) from error
        raise TransactionBuildError("transaction preparation failed") from error


def _typed_receipt(raw_receipt: object) -> Receipt:
    return Receipt(
        transaction_hash=_hex_value(_receipt_value(raw_receipt, "transactionHash")),
        block_number=int(_receipt_value(raw_receipt, "blockNumber")),
        gas_used=int(_receipt_value(raw_receipt, "gasUsed")),
        status=int(_receipt_value(raw_receipt, "status")),
        from_address=str(_receipt_value(raw_receipt, "from")),
        to_address=_optional_str(_receipt_value(raw_receipt, "to")),
        contract_address=_optional_str(_receipt_value(raw_receipt, "contractAddress")),
        effective_gas_price=_optional_int(
            _receipt_value(raw_receipt, "effectiveGasPrice")
        ),
    )


def _receipt_value(raw_receipt: object, key: str) -> object:
    if isinstance(raw_receipt, dict):
        return raw_receipt.get(key)
    return getattr(raw_receipt, key)


def _hex_value(value: object) -> str:
    to_0x_hex = getattr(value, "to_0x_hex", None)
    if callable(to_0x_hex):
        return str(to_0x_hex())
    hex_value = getattr(value, "hex", None)
    if callable(hex_value):
        result = hex_value()
        return result if str(result).startswith("0x") else f"0x{result}"
    return str(value)


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    return int(value)


def _is_revert_error(error: Exception) -> bool:
    if isinstance(error, ContractLogicError):
        return True
    error_name = type(error).__name__
    if error_name in {"TransactionFailed", "ContractPanicError", "OffchainLookup"}:
        return True
    message = str(error).lower()
    return "execution reverted" in message or "transaction reverted" in message


from open_allocator.exec.remote_signer import RemoteSigner  # noqa: E402,F401
from open_allocator.exec.safe_signer import SafeSigner  # noqa: E402,F401
