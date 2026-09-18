from __future__ import annotations

from eth_abi import encode as abi_encode
from eth_utils import keccak
from web3 import Web3

# Hand-encoded eth_calls, so no ABI file is needed for two methods.

_ALLOWANCE_SELECTOR = keccak(text="allowance(address,address)")[:4]
_BALANCE_OF_SELECTOR = keccak(text="balanceOf(address)")[:4]


class BalanceReadError(RuntimeError):
    """A balance could not be read; never to be taken as zero or as enough."""


class BalanceOverrideUnavailable(RuntimeError):
    """No storage slot was found that ``balanceOf`` reads for this holder."""


# Candidate balance-mapping slots (Solidity, Vyper, and OpenZeppelin v5's
# ERC-7201 layouts), each verified against balanceOf before use.
_DECLARATION_SLOTS = 32
_OZ_ERC20_NAMESPACE = int(
    "52c63247e1f47db19d5ce0460030c497f067ca4cebf71ba98eeadabe20bace00", 16
)
# Written to a candidate slot to recognize it; no real balance is this value.
_SLOT_SENTINEL = int.from_bytes(keccak(text="open-allocator:balance-slot"), "big") >> 8


def allowance(w3: Web3, token: str, *, owner: str, spender: str) -> int:
    """ERC-20 allowance(owner, spender), or 0 if it cannot be read.

    🔑 **Never raises, and the asymmetry is the whole point.** The caller uses
    this to decide whether to *skip* an approval it would otherwise send. A
    redundant approval costs a few thousand gas; a missing one makes the
    paymaster's postOp pull fail and the operation revert, having paid for
    everything up to that point. So every failure mode here — an undeployed
    account, an RPC that will not answer, a token that returns nothing — has to
    read as "no allowance", which sends the approval. Guessing wrong in the
    cheap direction is a rounding error; guessing wrong in the other direction
    costs a whole operation.
    """
    data = _ALLOWANCE_SELECTOR + abi_encode(
        ["address", "address"],
        [Web3.to_checksum_address(owner), Web3.to_checksum_address(spender)],
    )
    try:
        raw = w3.eth.call(
            {
                "to": Web3.to_checksum_address(token),
                "data": "0x" + data.hex(),
            }
        )
    except Exception:
        return 0
    if len(raw) < 32:
        return 0
    return int.from_bytes(raw[:32], "big")


def balance_of(w3: Web3, token: str, *, owner: str) -> int:
    """ERC-20 balanceOf(owner). Raises BalanceReadError when it cannot be read.

    No default, unlike :func:`allowance`: the caller decides what an unreadable
    balance means.
    """
    data = _BALANCE_OF_SELECTOR + abi_encode(
        ["address"], [Web3.to_checksum_address(owner)]
    )
    try:
        raw = w3.eth.call(
            {
                "to": Web3.to_checksum_address(token),
                "data": "0x" + data.hex(),
            }
        )
    except Exception as error:
        # The type only: a provider error can quote its URL, and RPC URLs carry
        # API keys.
        raise BalanceReadError(
            f"balanceOf call failed ({type(error).__name__})"
        ) from None
    if len(raw) < 32:
        raise BalanceReadError("balanceOf returned no value")
    return int.from_bytes(raw[:32], "big")


def balance_state_override(
    w3: Web3,
    token: str,
    *,
    owner: str,
    balance: int,
) -> dict[str, dict[str, dict[str, str]]]:
    """An ``eth_call``-style state override that gives ``owner`` ``balance``.

    For estimates only, never for anything submitted. Raises when no candidate
    slot holds the balance, e.g. for rebasing or scaled tokens.
    """
    slot = balance_slot(w3, token, owner=owner)
    return {
        Web3.to_checksum_address(token): {
            "stateDiff": {slot: _word(balance)},
        }
    }


def balance_slot(w3: Web3, token: str, *, owner: str) -> str:
    """The storage slot ``balanceOf(owner)`` reads, verified on chain."""
    holder = Web3.to_checksum_address(owner)
    address = Web3.to_checksum_address(token)
    data = "0x" + (_BALANCE_OF_SELECTOR + abi_encode(["address"], [holder])).hex()
    for slot in _candidate_slots(holder):
        override = {address: {"stateDiff": {slot: _word(_SLOT_SENTINEL)}}}
        try:
            raw = w3.eth.call({"to": address, "data": data}, "latest", override)
        except Exception:
            # A provider without state overrides fails every candidate alike.
            raise BalanceOverrideUnavailable(
                f"the RPC rejected a state override on balanceOf({holder}) of "
                f"{address}; configure an RPC that supports eth_call overrides"
            ) from None
        if len(raw) >= 32 and int.from_bytes(raw[:32], "big") == _SLOT_SENTINEL:
            return slot
    raise BalanceOverrideUnavailable(
        f"no storage slot of {address} holds balanceOf({holder})"
    )


def _candidate_slots(holder: str) -> list[str]:
    key = abi_encode(["address"], [holder])
    bases = [*range(_DECLARATION_SLOTS), _OZ_ERC20_NAMESPACE]
    solidity = [keccak(key + abi_encode(["uint256"], [base])) for base in bases]
    vyper = [
        keccak(abi_encode(["uint256"], [base]) + key)
        for base in range(_DECLARATION_SLOTS)
    ]
    return ["0x" + slot.hex() for slot in (*solidity, *vyper)]


def _word(value: int) -> str:
    return "0x" + value.to_bytes(32, "big").hex()


__all__ = [
    "BalanceOverrideUnavailable",
    "BalanceReadError",
    "allowance",
    "balance_of",
    "balance_slot",
    "balance_state_override",
]
