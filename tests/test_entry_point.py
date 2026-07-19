from __future__ import annotations

from typing import Any

import pytest
from eth_abi import decode as abi_decode
from eth_utils import keccak
from web3 import Web3

from open_allocator.exec import paymaster_registry
from open_allocator.exec.entry_point import EntryPointError, get_nonce

SENDER = Web3.to_checksum_address("0x" + "5a" * 20)
ENTRY_POINT = paymaster_registry.ENTRY_POINT_V07

_GET_NONCE = keccak(text="getNonce(address,uint192)")[:4]


class FakeWeb3:
    def __init__(self, *, result: bytes) -> None:
        self.eth = self
        self._result = result
        self.calls: list[dict[str, Any]] = []

    def call(self, transaction: dict[str, Any]) -> bytes:
        self.calls.append(transaction)
        return self._result


def test_get_nonce_decodes_the_entry_points_answer() -> None:
    w3 = FakeWeb3(result=(42).to_bytes(32, "big"))
    assert get_nonce(w3, SENDER) == 42


def test_get_nonce_asks_the_entry_point() -> None:
    w3 = FakeWeb3(result=(0).to_bytes(32, "big"))
    get_nonce(w3, SENDER)

    call = w3.calls[0]
    assert call["to"] == ENTRY_POINT
    data = bytes.fromhex(call["data"][2:])
    assert data[:4] == _GET_NONCE
    sender, key = abi_decode(["address", "uint192"], data[4:])
    assert Web3.to_checksum_address(sender) == SENDER
    assert key == 0


def test_a_nonce_key_selects_an_independent_sequence() -> None:
    """4337 splits the nonce into a 192-bit key and a 64-bit sequence."""
    w3 = FakeWeb3(result=(0).to_bytes(32, "big"))
    get_nonce(w3, SENDER, key=9)

    _, key = abi_decode(
        ["address", "uint192"], bytes.fromhex(w3.calls[0]["data"][2:])[4:]
    )
    assert key == 9


def test_a_fresh_safe_has_nonce_zero() -> None:
    """An undeployed Safe is a mapping miss, not an error: 0 is a real answer."""
    w3 = FakeWeb3(result=(0).to_bytes(32, "big"))
    assert get_nonce(w3, SENDER) == 0


def test_an_empty_answer_means_no_entry_point_here() -> None:
    """eth_call to an address with no code returns empty, not zero."""
    w3 = FakeWeb3(result=b"")
    with pytest.raises(EntryPointError, match="not deployed"):
        get_nonce(w3, SENDER)


def test_the_entry_point_is_overridable() -> None:
    other = Web3.to_checksum_address("0x" + "ee" * 20)
    w3 = FakeWeb3(result=(1).to_bytes(32, "big"))
    get_nonce(w3, SENDER, entry_point=other)
    assert w3.calls[0]["to"] == other
