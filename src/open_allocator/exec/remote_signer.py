from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol, runtime_checkable

import httpx
from web3.exceptions import Web3Exception

from open_allocator.core.types import TxStep
from open_allocator.exec.signer import (
    Receipt,
    SignerError,
    TransactionBroadcastError,
    TransactionReverted,
    Web3Factory,
    _build_transaction_dict,
    _http_web3,
    _typed_receipt,
)


class RemoteSignerProviderError(SignerError):
    pass


class RemoteSignerPolicyRejected(RemoteSignerProviderError):
    pass


RawSignedTransaction = bytes | bytearray | str


@runtime_checkable
class RemoteSignerAdapter(Protocol):
    def address(self) -> str: ...

    def sign_transaction(
        self,
        transaction: Mapping[str, object],
    ) -> RawSignedTransaction: ...


class RemoteSigner:
    def __init__(
        self,
        config: object | None = None,
        *,
        adapter: RemoteSignerAdapter | None = None,
        web3_factory: Web3Factory | None = None,
        receipt_timeout: float = 120,
    ) -> None:
        if adapter is not None:
            self._adapter: RemoteSignerAdapter | None = adapter
        elif config is not None:
            self._adapter = _adapter_from_config(config)
        else:
            self._adapter = None
        self._web3_factory = web3_factory or _http_web3
        self._receipt_timeout = receipt_timeout

    def __repr__(self) -> str:
        if self._adapter is None:
            return "RemoteSigner(status=<unconfigured>)"
        return "RemoteSigner(status=configured)"

    def address(self) -> str:
        return self._require_adapter().address()

    def send(self, tx: TxStep, rpc_url: str) -> Receipt:
        adapter = self._require_adapter()
        w3 = self._web3_factory(rpc_url)
        try:
            transaction = _build_transaction_dict(w3, tx, adapter.address())
            raw_transaction = adapter.sign_transaction(transaction)
            tx_hash_bytes = w3.eth.send_raw_transaction(
                _raw_signed_transaction(raw_transaction)
            )
            raw_receipt = w3.eth.wait_for_transaction_receipt(
                tx_hash_bytes,
                timeout=self._receipt_timeout,
            )
        except SignerError:
            raise
        except Web3Exception as error:
            raise TransactionBroadcastError("transaction broadcast failed") from error
        except Exception as error:
            raise TransactionBroadcastError("transaction broadcast failed") from error

        receipt = _typed_receipt(raw_receipt)
        if receipt.status != 1:
            raise TransactionReverted(
                f"transaction reverted: {receipt.transaction_hash}",
                tx_hash=receipt.transaction_hash,
                receipt=receipt,
            )

        return receipt

    def _require_adapter(self) -> RemoteSignerAdapter:
        if self._adapter is None:
            raise ValueError("RemoteSigner requires remote signer configuration")
        return self._adapter


class GenericHttpRemoteSignerAdapter:
    def __init__(
        self,
        *,
        provider_url: str,
        credential: object,
        key_id: str,
        expected_address: str | None = None,
        client: httpx.Client | None = None,
        timeout: float = 30,
    ) -> None:
        self._provider_url = provider_url.rstrip("/")
        self._credential = credential
        self._key_id = key_id
        self._expected_address = expected_address
        self._client = client or httpx.Client()
        self._timeout = timeout

    def __repr__(self) -> str:
        return "GenericHttpRemoteSignerAdapter(credential=<redacted>)"

    def address(self) -> str:
        payload = self._post("address", {"keyId": self._key_id})
        address = _required_payload_string(payload, "address")
        if self._expected_address is not None and not _same_address(
            address,
            self._expected_address,
        ):
            raise RemoteSignerProviderError("remote signer address mismatch")
        return address

    def sign_transaction(
        self,
        transaction: Mapping[str, object],
    ) -> RawSignedTransaction:
        payload = self._post(
            "sign-transaction",
            {"keyId": self._key_id, "transaction": dict(transaction)},
        )
        if _is_policy_rejection_payload(payload):
            raise RemoteSignerPolicyRejected(_policy_rejection_message(payload))
        raw_transaction = _first_payload_string(
            payload,
            ("rawTransaction", "raw_transaction", "signedTransaction"),
        )
        if raw_transaction is None:
            raise RemoteSignerProviderError(
                "remote signer response missing rawTransaction",
            )
        return raw_transaction

    def _post(self, endpoint: str, body: Mapping[str, object]) -> Mapping[str, object]:
        try:
            response = self._client.post(
                f"{self._provider_url}/{endpoint}",
                headers={"Authorization": f"Bearer {_secret_value(self._credential)}"},
                json=body,
                timeout=self._timeout,
            )
        except httpx.HTTPError as error:
            raise RemoteSignerProviderError("remote signer request failed") from error

        payload = _json_payload(response)
        if _is_policy_rejection_response(response, payload):
            raise RemoteSignerPolicyRejected(_policy_rejection_message(payload))
        if response.is_error:
            raise RemoteSignerProviderError(
                f"remote signer request failed with HTTP {response.status_code}",
            )
        return payload


def _adapter_from_config(config: object) -> RemoteSignerAdapter:
    provider = getattr(config, "remote_signer_provider", None)
    if provider == "generic-http":
        return GenericHttpRemoteSignerAdapter(
            provider_url=_required_config_value(config, "remote_signer_url"),
            credential=_required_config_value(config, "remote_signer_credential"),
            key_id=_required_config_value(config, "remote_signer_key_id"),
            expected_address=getattr(config, "remote_signer_address", None),
        )
    raise ValueError(f"unknown remote signer provider: {provider!r}")


def _required_config_value(config: object, name: str) -> str:
    value = getattr(config, name, None)
    if value is None:
        raise ValueError(f"{name} is required for RemoteSigner")
    return _secret_value(value)


def _secret_value(value: object) -> str:
    get_secret_value = getattr(value, "get_secret_value", None)
    if callable(get_secret_value):
        return str(get_secret_value())
    return str(value)


def _raw_signed_transaction(value: RawSignedTransaction) -> bytes | str:
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, bytes):
        return value
    if isinstance(value, str) and value.startswith("0x"):
        return value
    raise TransactionBroadcastError("remote signer returned invalid raw transaction")


def _json_payload(response: httpx.Response) -> Mapping[str, object]:
    try:
        payload = response.json()
    except ValueError as error:
        raise RemoteSignerProviderError(
            "remote signer returned invalid JSON"
        ) from error
    if not isinstance(payload, Mapping):
        raise RemoteSignerProviderError("remote signer returned non-object JSON")
    return payload


def _is_policy_rejection_response(
    response: httpx.Response,
    payload: Mapping[str, object],
) -> bool:
    return response.status_code in {403, 409} and _is_policy_rejection_payload(payload)


def _is_policy_rejection_payload(payload: Mapping[str, object]) -> bool:
    for value in _walk_values(payload):
        if isinstance(value, str):
            normalized = value.casefold().replace("-", "_")
            has_policy = "policy" in normalized
            has_rejection = any(
                token in normalized
                for token in ("reject", "denied", "deny", "violation")
            )
            if has_policy and has_rejection:
                return True
    return False


def _policy_rejection_message(payload: Mapping[str, object]) -> str:
    message = _first_payload_string(payload, ("message", "reason", "error"))
    if message is None:
        return "remote signer policy rejected transaction"
    return message


def _required_payload_string(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or value == "":
        raise RemoteSignerProviderError(f"remote signer response missing {key}")
    return value


def _first_payload_string(
    payload: Mapping[str, object],
    keys: Sequence[str],
) -> str | None:
    for key, value in _walk_items(payload):
        if key not in keys:
            continue
        if isinstance(value, str) and value != "":
            return value
    return None


def _walk_items(value: object) -> Sequence[tuple[str, object]]:
    items: list[tuple[str, object]] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            items.append((str(key), item))
            items.extend(_walk_items(item))
    elif isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        for item in value:
            items.extend(_walk_items(item))
    return tuple(items)


def _same_address(left: str, right: str) -> bool:
    return left.casefold() == right.casefold()


def _walk_values(value: object) -> Sequence[object]:
    values: list[object] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            values.append(key)
            values.extend(_walk_values(item))
    elif isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        for item in value:
            values.extend(_walk_values(item))
    else:
        values.append(value)
    return tuple(values)


__all__ = [
    "GenericHttpRemoteSignerAdapter",
    "RemoteSigner",
    "RemoteSignerAdapter",
    "RemoteSignerPolicyRejected",
    "RemoteSignerProviderError",
]
