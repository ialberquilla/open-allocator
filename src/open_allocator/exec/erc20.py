from __future__ import annotations

from eth_abi import encode as abi_encode
from eth_utils import keccak
from web3 import Web3

# The ERC-20 reads execution needs. Deliberately not a web3 Contract: one
# eth_call with a hand-encoded selector avoids vendoring an ABI file for two
# methods, matching entry_point.py and safe_deployment.py.

_ALLOWANCE_SELECTOR = keccak(text="allowance(address,address)")[:4]
_BALANCE_OF_SELECTOR = keccak(text="balanceOf(address)")[:4]


class BalanceReadError(RuntimeError):
    """A balance could not be read; never to be taken as zero or as enough."""


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

    The opposite asymmetry to :func:`allowance`. A funding check that read an
    unanswerable balance as zero would refuse a funded plan, and one that read
    it as anything else would pass an unfunded one — so there is no default,
    and the caller decides what an unreadable balance means.
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


__all__ = ["BalanceReadError", "allowance", "balance_of"]
