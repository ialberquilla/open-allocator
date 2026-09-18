from __future__ import annotations

from typing import Any

import pytest
from eth_abi import encode as abi_encode
from eth_utils import keccak
from web3 import Web3

from open_allocator.exec import erc20

TOKEN = Web3.to_checksum_address("0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913")
HOLDER = Web3.to_checksum_address("0x" + "12" * 20)
_BALANCE_OF = keccak(text="balanceOf(address)")[:4]


def solidity_slot(holder: str, base: int) -> str:
    key = abi_encode(["address"], [holder]) + abi_encode(["uint256"], [base])
    return "0x" + keccak(key).hex()


def vyper_slot(holder: str, base: int) -> str:
    key = abi_encode(["uint256"], [base]) + abi_encode(["address"], [holder])
    return "0x" + keccak(key).hex()


class StorageToken:
    """A token whose balanceOf reads one storage slot per holder.

    ``scale`` stands in for a token whose balance is derived from storage
    rather than stored — a scaled or rebasing balance — so no slot matches.
    """

    def __init__(
        self,
        slot_of: Any,
        *,
        balance: int = 0,
        scale: int = 1,
        overrides_supported: bool = True,
    ) -> None:
        self.eth = self
        self._slot_of = slot_of
        self._balance = balance
        self._scale = scale
        self._overrides_supported = overrides_supported
        self.calls = 0

    def call(
        self,
        transaction: dict[str, Any],
        block: object = None,
        state_override: dict[str, Any] | None = None,
    ) -> bytes:
        self.calls += 1
        data = bytes.fromhex(transaction["data"][2:])
        assert data[:4] == _BALANCE_OF
        holder = Web3.to_checksum_address("0x" + data[16:36].hex())
        balance = self._balance
        if state_override is not None:
            if not self._overrides_supported:
                raise ValueError("state override not supported")
            diff = state_override.get(Web3.to_checksum_address(transaction["to"]), {})
            written = diff.get("stateDiff", {}).get(self._slot_of(holder))
            if written is not None:
                balance = int(written, 16) * self._scale
        return balance.to_bytes(32, "big")


def test_the_solidity_mapping_slot_is_found_by_what_balance_of_returns() -> None:
    w3 = StorageToken(lambda holder: solidity_slot(holder, 9))

    assert erc20.balance_slot(w3, TOKEN, owner=HOLDER) == solidity_slot(HOLDER, 9)


def test_a_vyper_mapping_slot_is_found() -> None:
    w3 = StorageToken(lambda holder: vyper_slot(holder, 3))

    assert erc20.balance_slot(w3, TOKEN, owner=HOLDER) == vyper_slot(HOLDER, 3)


def test_the_openzeppelin_v5_namespaced_slot_is_found() -> None:
    namespace = int(
        "52c63247e1f47db19d5ce0460030c497f067ca4cebf71ba98eeadabe20bace00", 16
    )
    w3 = StorageToken(lambda holder: solidity_slot(holder, namespace))

    assert erc20.balance_slot(w3, TOKEN, owner=HOLDER) == solidity_slot(
        HOLDER, namespace
    )


def test_the_override_writes_the_balance_to_the_verified_slot() -> None:
    w3 = StorageToken(lambda holder: solidity_slot(holder, 0))

    override = erc20.balance_state_override(w3, TOKEN, owner=HOLDER, balance=300_000)

    assert override == {
        TOKEN: {"stateDiff": {solidity_slot(HOLDER, 0): "0x" + "00" * 29 + "0493e0"}}
    }


def test_a_balance_not_stored_as_a_plain_value_is_not_overridden() -> None:
    w3 = StorageToken(lambda holder: solidity_slot(holder, 0), scale=2)

    with pytest.raises(erc20.BalanceOverrideUnavailable, match="no storage slot"):
        erc20.balance_state_override(w3, TOKEN, owner=HOLDER, balance=1)


def test_an_rpc_without_state_overrides_fails_once_not_per_candidate() -> None:
    w3 = StorageToken(
        lambda holder: solidity_slot(holder, 9), overrides_supported=False
    )

    with pytest.raises(erc20.BalanceOverrideUnavailable, match="state override"):
        erc20.balance_slot(w3, TOKEN, owner=HOLDER)
    assert w3.calls == 1
