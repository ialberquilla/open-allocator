"""CCTP V2 messages: finding a Safe's burn, checking Circle's attestation of it,
and composing the redemption that funds the destination deposit.

Nothing Circle returns is used on its word. The source transaction's
``MessageSent`` log is the burn as it actually happened; an attested message is
accepted only when it is that log's message with Circle's fields filled in, and
every field the Safe's funds depend on — domains, messengers, caller, token,
recipient, sender, amount, fee, and finality — matches what was burned.

Layouts follow Circle's ``MessageV2`` and ``BurnMessageV2``. The source chain
emits a message with nonce, executed finality, executed fee, and expiration
block zeroed; Circle's attested copy fills exactly those.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode
from eth_utils import keccak
from pydantic import Field
from web3 import HTTPProvider, Web3

from open_allocator.core.types import FrozenModel, TxStep
from open_allocator.exec import chains
from open_allocator.exec.circle import CircleMessage

MESSAGE_SENT_TOPIC = "0x" + keccak(text="MessageSent(bytes)").hex()
_DEPOSIT_FOR_BURN_SELECTOR = keccak(
    text="depositForBurn(uint256,uint32,bytes32,address,bytes32,uint256,uint32)"
)[:4]
_APPROVE_SELECTOR = keccak(text="approve(address,uint256)")[:4]
_RECEIVE_MESSAGE_SELECTOR = keccak(text="receiveMessage(bytes,bytes)")[:4]
_USED_NONCES_SELECTOR = keccak(text="usedNonces(bytes32)")[:4]

MESSAGE_VERSION = 1
BURN_MESSAGE_VERSION = 1

# MessageV2 header offsets.
_NONCE = slice(12, 44)
_FINALITY_EXECUTED = slice(144, 148)
_BODY = 148
# BurnMessageV2 body offsets, relative to the body.
_FEE_EXECUTED = slice(164, 196)
_EXPIRATION_BLOCK = slice(196, 228)
_BURN_BODY_LENGTH = 228


class CctpValidationError(ValueError):
    """A CCTP message or log is not the burn it must be; never redeem it."""


@dataclass(frozen=True)
class BurnMessage:
    version: int
    burn_token: str
    mint_recipient: str
    amount: int
    message_sender: str
    max_fee: int
    fee_executed: int
    expiration_block: int
    hook_data: bytes


@dataclass(frozen=True)
class CctpMessage:
    raw: bytes
    version: int
    source_domain: int
    destination_domain: int
    nonce: str
    # The source and destination TokenMessengers.
    sender: str
    recipient: str
    destination_caller: str
    min_finality_threshold: int
    finality_threshold_executed: int
    body: BurnMessage


class BurnExpectation(FrozenModel):
    """What a Safe's burn committed to, from the bridge bundle it submitted."""

    source_domain: int = Field(ge=0)
    destination_domain: int = Field(ge=0)
    source_token_messenger: str
    destination_token_messenger: str
    account: str
    burn_token: str
    amount_raw: str = Field(pattern=r"^[1-9]\d*$")
    max_fee_raw: str = Field(pattern=r"^\d+$")
    min_finality_threshold: int


class BurnCall(FrozenModel):
    """A decoded ``TokenMessengerV2.depositForBurn`` call."""

    token_messenger: str
    amount_raw: str
    destination_domain: int
    mint_recipient: str
    burn_token: str
    destination_caller: str
    max_fee_raw: str
    min_finality_threshold: int


def decode_burn_steps(steps: Sequence[TxStep]) -> BurnCall:
    """The burn a bridge bundle's calls make, with the approval that funds it.

    Refuses anything but an ERC-20 approval of the burned token to the
    TokenMessenger for at least the amount, followed by ``depositForBurn``
    without a hook.
    """
    if len(steps) != 2 or [step.kind for step in steps] != ["approve", "bridge_burn"]:
        raise CctpValidationError(
            "a bridge bundle must be exactly an approval and a burn, got "
            f"{[step.kind for step in steps]}"
        )
    approve, burn = steps
    if approve.value or burn.value:
        raise CctpValidationError("a bridge bundle must not send native value")
    call = _calldata(burn.data, _DEPOSIT_FOR_BURN_SELECTOR, "depositForBurn")
    try:
        amount, domain, recipient, token, caller, max_fee, threshold = abi_decode(
            ["uint256", "uint32", "bytes32", "address", "bytes32", "uint256", "uint32"],
            call,
        )
        spender, allowance = abi_decode(
            ["address", "uint256"],
            _calldata(approve.data, _APPROVE_SELECTOR, "approve"),
        )
    except CctpValidationError:
        raise
    except Exception as error:  # noqa: BLE001 - malformed calldata is refused
        raise CctpValidationError("bridge calldata does not decode") from error
    decoded = BurnCall(
        token_messenger=Web3.to_checksum_address(burn.to),
        amount_raw=str(amount),
        destination_domain=int(domain),
        mint_recipient=_address(bytes(recipient), "mintRecipient"),
        burn_token=Web3.to_checksum_address(token),
        destination_caller=_address(bytes(caller), "destinationCaller"),
        max_fee_raw=str(max_fee),
        min_finality_threshold=int(threshold),
    )
    if approve.to.casefold() != decoded.burn_token.casefold():
        raise CctpValidationError("the bridge approval is not on the burned token")
    if str(spender).casefold() != burn.to.casefold():
        raise CctpValidationError("the bridge approval is not to the TokenMessenger")
    if int(allowance) < int(amount):
        raise CctpValidationError("the bridge approval does not cover the burn")
    return decoded


class SourceLog(FrozenModel):
    """One ``MessageSent`` event: which transmitter emitted it, where, and what."""

    log_index: int = Field(ge=0)
    emitter: str
    message: str


def decode_message(message: str | bytes) -> CctpMessage:
    raw = _bytes(message)
    if len(raw) < _BODY + _BURN_BODY_LENGTH:
        raise CctpValidationError(
            f"CCTP message is {len(raw)} bytes, shorter than a burn message"
        )
    body = raw[_BODY:]
    return CctpMessage(
        raw=raw,
        version=_uint(raw[0:4]),
        source_domain=_uint(raw[4:8]),
        destination_domain=_uint(raw[8:12]),
        nonce="0x" + raw[_NONCE].hex(),
        sender=_address(raw[44:76], "sender"),
        recipient=_address(raw[76:108], "recipient"),
        destination_caller=_address(raw[108:140], "destinationCaller"),
        min_finality_threshold=_uint(raw[140:144]),
        finality_threshold_executed=_uint(raw[_FINALITY_EXECUTED]),
        body=BurnMessage(
            version=_uint(body[0:4]),
            burn_token=_address(body[4:36], "burnToken"),
            mint_recipient=_address(body[36:68], "mintRecipient"),
            amount=_uint(body[68:100]),
            message_sender=_address(body[100:132], "messageSender"),
            max_fee=_uint(body[132:164]),
            fee_executed=_uint(body[_FEE_EXECUTED]),
            expiration_block=_uint(body[_EXPIRATION_BLOCK]),
            hook_data=bytes(body[_BURN_BODY_LENGTH:]),
        ),
    )


def unattested(message: str | bytes) -> bytes:
    """The message with the fields Circle fills at attestation zeroed.

    Equal for a source ``MessageSent`` message and Circle's attested copy of it.
    """
    raw = bytearray(_bytes(message))
    if len(raw) < _BODY + _BURN_BODY_LENGTH:
        raise CctpValidationError("CCTP message is shorter than a burn message")
    for field in (
        _NONCE,
        _FINALITY_EXECUTED,
        _shift(_FEE_EXECUTED),
        _shift(_EXPIRATION_BLOCK),
    ):
        raw[field] = bytes(field.stop - field.start)
    return bytes(raw)


def message_hash(message: str | bytes) -> str:
    return "0x" + keccak(_bytes(message)).hex()


def message_sent_logs(logs: Iterable[Mapping[str, object]]) -> tuple[SourceLog, ...]:
    """Every ``MessageSent`` event in a receipt's logs, in log order."""
    found: list[SourceLog] = []
    for log in logs:
        topics = [_hex_text(topic) for topic in _sequence(log.get("topics"))]
        if not topics or topics[0].casefold() != MESSAGE_SENT_TOPIC:
            continue
        try:
            (message,) = abi_decode(["bytes"], _bytes(_hex_text(log.get("data"))))
        except Exception as error:  # noqa: BLE001 - an undecodable log is refused
            raise CctpValidationError("a MessageSent log does not decode") from error
        found.append(
            SourceLog(
                log_index=_int(log.get("logIndex")),
                emitter=str(log.get("address")),
                message="0x" + bytes(message).hex(),
            )
        )
    return tuple(sorted(found, key=lambda item: item.log_index))


def select_source_log(
    logs: Sequence[SourceLog],
    expected: BurnExpectation,
    *,
    message_transmitter: str,
    burn_index: int,
    burns_in_operation: int,
) -> SourceLog:
    """The log of this burn among a transaction's CCTP messages.

    A bundler transaction can carry other accounts' burns, and one Safe
    operation can carry several of its own. The Safe's burns are taken in log
    order, which is call order; there must be exactly as many as the operation
    submitted, and the one at this burn's position must be this burn.
    """
    own = [
        item
        for item in logs
        if item.emitter.casefold() == message_transmitter.casefold()
        and _is_own_burn(item.message, expected.account)
    ]
    if len(own) != burns_in_operation:
        raise CctpValidationError(
            f"the source transaction carries {len(own)} CCTP burns from "
            f"{expected.account} through {message_transmitter}, but its "
            f"operation submitted {burns_in_operation}; the burn cannot be "
            "identified"
        )
    selected = own[burn_index]
    check_burn(decode_message(selected.message), expected, attested=False)
    return selected


def check_burn(
    message: CctpMessage,
    expected: BurnExpectation,
    *,
    attested: bool,
) -> None:
    """Refuse a message that is not exactly the expected burn."""
    body = message.body
    mismatches = [
        name
        for name, actual, wanted in (
            ("version", message.version, MESSAGE_VERSION),
            ("burn message version", body.version, BURN_MESSAGE_VERSION),
            ("source domain", message.source_domain, expected.source_domain),
            (
                "destination domain",
                message.destination_domain,
                expected.destination_domain,
            ),
            (
                "source TokenMessenger",
                message.sender.casefold(),
                expected.source_token_messenger.casefold(),
            ),
            (
                "destination TokenMessenger",
                message.recipient.casefold(),
                expected.destination_token_messenger.casefold(),
            ),
            (
                "destinationCaller",
                message.destination_caller.casefold(),
                expected.account.casefold(),
            ),
            (
                "burn token",
                body.burn_token.casefold(),
                expected.burn_token.casefold(),
            ),
            (
                "mintRecipient",
                body.mint_recipient.casefold(),
                expected.account.casefold(),
            ),
            (
                "message sender",
                body.message_sender.casefold(),
                expected.account.casefold(),
            ),
            ("amount", body.amount, int(expected.amount_raw)),
            ("maxFee", body.max_fee, int(expected.max_fee_raw)),
            (
                "minFinalityThreshold",
                message.min_finality_threshold,
                expected.min_finality_threshold,
            ),
            ("hook data", body.hook_data, b""),
        )
        if actual != wanted
    ]
    if attested:
        if message.nonce == "0x" + "00" * 32:
            mismatches.append("nonce (unassigned)")
        if message.finality_threshold_executed < message.min_finality_threshold:
            mismatches.append("executed finality (below the minimum)")
        if body.fee_executed > body.max_fee:
            mismatches.append("feeExecuted (above maxFee)")
    if mismatches:
        raise CctpValidationError(
            f"{'attested' if attested else 'source'} CCTP message does not match "
            f"the burn: {', '.join(mismatches)}"
        )


@dataclass(frozen=True)
class Attestation:
    message: CctpMessage
    attestation: str

    @property
    def net_mint(self) -> int:
        """What the destination mints: the burn less Circle's executed fee."""
        return self.message.body.amount - self.message.body.fee_executed


def select_attestation(
    messages: Sequence[CircleMessage],
    source_message: str,
    expected: BurnExpectation,
    *,
    burn_index: int,
) -> Attestation | None:
    """Circle's attestation of this source message, or None while not ready.

    Candidates are Circle's attested copies of the source message. Identical
    burns in one operation have identical source messages; they are told apart
    by position, as in :func:`select_source_log`. Any failed check raises.
    """
    wanted = unattested(source_message)
    candidates: list[CircleMessage] = []
    for item in messages:
        if not item.complete:
            continue
        if unattested(item.message) == wanted:
            candidates.append(item)
    if len(candidates) <= burn_index:
        return None
    chosen = candidates[burn_index]
    if chosen.cctp_version is not None and chosen.cctp_version != 2:
        raise CctpValidationError(
            f"Circle attested the message as CCTP version {chosen.cctp_version}, not 2"
        )
    decoded = decode_message(chosen.message)
    check_burn(decoded, expected, attested=True)
    if chosen.event_nonce is not None and _bytes(chosen.event_nonce).rjust(
        32, b"\0"
    ) != _bytes(decoded.nonce):
        raise CctpValidationError(
            "Circle's eventNonce does not match the attested message's nonce"
        )
    assert chosen.attestation is not None  # complete
    return Attestation(message=decoded, attestation=chosen.attestation)


def delay_reasons(messages: Sequence[CircleMessage]) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            item.delay_reason for item in messages if item.delay_reason is not None
        )
    )


def receive_message_step(
    chain_id: int,
    message_transmitter: str,
    message: str | bytes,
    attestation: str,
) -> TxStep:
    """``MessageTransmitterV2.receiveMessage(message, attestation)`` as a step."""
    data = _RECEIVE_MESSAGE_SELECTOR + abi_encode(
        ["bytes", "bytes"], [_bytes(message), _bytes(attestation)]
    )
    return TxStep(
        to=Web3.to_checksum_address(message_transmitter),
        data="0x" + data.hex(),
        value=0,
        chain_id=chain_id,
        kind="cctp_receive",
    )


class CctpChainReader(Protocol):
    """The chain reads a bridged leg needs, injectable for tests."""

    def transaction_logs(
        self, chain_id: int, transaction_hash: str
    ) -> tuple[Mapping[str, object], ...] | None:
        """Receipt logs of an included transaction; None while it is not.

        Raises when the transaction reverted.
        """
        ...

    def nonce_used(
        self, chain_id: int, message_transmitter: str, nonce: str
    ) -> bool: ...

    def block_number(self, chain_id: int) -> int: ...


class RpcCctpReader:
    def __init__(self, config: object | None) -> None:
        self._config = config

    def transaction_logs(
        self, chain_id: int, transaction_hash: str
    ) -> tuple[Mapping[str, object], ...] | None:
        w3 = self._web3(chain_id)
        try:
            receipt = w3.eth.get_transaction_receipt(transaction_hash)  # type: ignore[arg-type]
        except Exception as error:  # noqa: BLE001
            if type(error).__name__ == "TransactionNotFound":
                return None
            raise CctpValidationError(
                f"could not read the receipt of {transaction_hash} on "
                f"{chains.chain_name(chain_id)} ({type(error).__name__})"
            ) from None
        if int(receipt["status"]) != 1:
            raise CctpValidationError(
                f"source transaction {transaction_hash} reverted on "
                f"{chains.chain_name(chain_id)}"
            )
        return tuple(
            {
                "address": str(log["address"]),
                "topics": [_hex_text(topic) for topic in log["topics"]],
                "data": _hex_text(log["data"]),
                "logIndex": int(log["logIndex"]),
            }
            for log in receipt["logs"]
        )

    def nonce_used(self, chain_id: int, message_transmitter: str, nonce: str) -> bool:
        w3 = self._web3(chain_id)
        data = _USED_NONCES_SELECTOR + _bytes(nonce).rjust(32, b"\0")
        raw = w3.eth.call(
            {
                "to": Web3.to_checksum_address(message_transmitter),
                "data": "0x" + data.hex(),
            }
        )
        if len(raw) < 32:
            raise CctpValidationError(
                f"usedNonces returned {len(raw)} bytes on {chains.chain_name(chain_id)}"
            )
        return int.from_bytes(raw[:32], "big") != 0

    def block_number(self, chain_id: int) -> int:
        return int(self._web3(chain_id).eth.block_number)

    def _web3(self, chain_id: int) -> Web3:
        return Web3(HTTPProvider(chains.require_rpc_url(chain_id, self._config)))


def _calldata(data: str, selector: bytes, name: str) -> bytes:
    raw = _bytes(data)
    if raw[:4] != selector:
        raise CctpValidationError(f"bridge calldata is not a {name} call")
    return raw[4:]


def _is_own_burn(message: str, account: str) -> bool:
    try:
        decoded = decode_message(message)
    except CctpValidationError:
        return False
    return decoded.body.message_sender.casefold() == account.casefold()


def _shift(field: slice) -> slice:
    return slice(_BODY + field.start, _BODY + field.stop)


def _uint(raw: bytes) -> int:
    return int.from_bytes(raw, "big")


def _address(raw: bytes, name: str) -> str:
    if any(raw[:12]):
        raise CctpValidationError(f"CCTP message {name} is not an EVM address")
    return Web3.to_checksum_address("0x" + raw[12:].hex())


def _bytes(value: str | bytes) -> bytes:
    if isinstance(value, bytes | bytearray):
        return bytes(value)
    text = value[2:] if value.startswith(("0x", "0X")) else value
    try:
        return bytes.fromhex(text)
    except ValueError as error:
        raise CctpValidationError("CCTP bytes are not hex") from error


def _hex_text(value: object) -> str:
    to_0x_hex = getattr(value, "to_0x_hex", None)
    if callable(to_0x_hex):
        return str(to_0x_hex())
    if isinstance(value, bytes | bytearray):
        return "0x" + bytes(value).hex()
    text = str(value)
    return text if text.startswith("0x") else "0x" + text


def _sequence(value: object) -> Sequence[object]:
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return value
    return ()


def _int(value: object) -> int:
    if isinstance(value, str):
        return int(value, 0)
    return int(value)  # type: ignore[call-overload]


__all__ = [
    "MESSAGE_SENT_TOPIC",
    "Attestation",
    "BurnExpectation",
    "BurnCall",
    "BurnMessage",
    "CctpChainReader",
    "CctpMessage",
    "CctpValidationError",
    "RpcCctpReader",
    "SourceLog",
    "check_burn",
    "decode_burn_steps",
    "decode_message",
    "delay_reasons",
    "message_hash",
    "message_sent_logs",
    "receive_message_step",
    "select_attestation",
    "select_source_log",
    "unattested",
]
