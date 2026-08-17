from __future__ import annotations

from eth_abi import encode as abi_encode
from eth_utils import keccak
from web3 import Web3

# The one ERC-20 read a gasless userOp needs. Deliberately not a web3 Contract:
# one eth_call with a hand-encoded selector avoids vendoring an ABI file for a
# single method, matching entry_point.py and safe_deployment.py.

_ALLOWANCE_SELECTOR = keccak(text="allowance(address,address)")[:4]


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


__all__ = ["allowance"]
