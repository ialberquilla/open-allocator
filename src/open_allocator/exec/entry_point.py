from __future__ import annotations

from eth_abi import encode as abi_encode
from eth_utils import keccak
from web3 import Web3

from open_allocator.exec import paymaster_registry

# The EntryPoint reads a userOp needs. Deliberately not a web3 Contract: one
# eth_call with a hand-encoded selector avoids vendoring an ABI file for two
# methods, matching how safe_deployment reads proxyCreationCode().

_GET_NONCE_SELECTOR = keccak(text="getNonce(address,uint192)")[:4]

# A userOp's nonce is (192-bit key ‖ 64-bit sequence). Key 0 is the ordinary
# sequential nonce; a different key buys an independent sequence, which is how
# 4337 does parallel ops from one account. We have no use for that yet.
DEFAULT_NONCE_KEY = 0


class EntryPointError(RuntimeError):
    pass


def get_nonce(
    w3: Web3,
    sender: str,
    *,
    entry_point: str = paymaster_registry.ENTRY_POINT_V07,
    key: int = DEFAULT_NONCE_KEY,
) -> int:
    """EntryPoint.getNonce(sender, key).

    Read rather than tracked: the account may have been used by something other
    than this process, and a stale nonce is an AA25 rejection at submission.
    Works for an undeployed Safe — the EntryPoint's mapping returns 0, which is
    the right answer for a first op.
    """
    data = _GET_NONCE_SELECTOR + abi_encode(
        ["address", "uint192"],
        [Web3.to_checksum_address(sender), key],
    )
    raw = w3.eth.call(
        {
            "to": Web3.to_checksum_address(entry_point),
            "data": "0x" + data.hex(),
        }
    )
    if len(raw) < 32:
        raise EntryPointError(
            f"EntryPoint at {entry_point} returned no nonce for {sender}; "
            f"it is probably not deployed on this chain"
        )
    return int.from_bytes(raw[:32], "big")


__all__ = ["DEFAULT_NONCE_KEY", "EntryPointError", "get_nonce"]
