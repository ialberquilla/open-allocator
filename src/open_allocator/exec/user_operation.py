from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from eth_abi import encode as abi_encode
from eth_utils import keccak
from web3 import Web3

from open_allocator.exec import safe_deployment
from open_allocator.exec.safe_deployment import SafeSeed

# Building the callData a Safe4337Module userOp carries.
#
# A Safe cannot execute a raw call from a userOp: the EntryPoint calls
# executeUserOp() on the module, which then drives the Safe. Batching (the
# paymaster approval riding along with the actual transfer) goes through
# MultiSendCallOnly, delegatecalled from the Safe.

# Safe 1.4.1 canonical deployments. MultiSendCallOnly rather than MultiSend:
# both are delegatecalled, but this one can only emit plain CALLs, so a batch
# cannot be tricked into delegatecalling out of the Safe.
MULTISEND_CALL_ONLY = "0x9641d764fc13c8B624c04430C7356C1C7C8102e2"

CALL = 0
DELEGATECALL = 1

_EXECUTE_USER_OP_SELECTOR = keccak(
    text="executeUserOp(address,uint256,bytes,uint8)"
)[:4]
_MULTISEND_SELECTOR = keccak(text="multiSend(bytes)")[:4]
_APPROVE_SELECTOR = keccak(text="approve(address,uint256)")[:4]

MAX_UINT256 = 2**256 - 1


@dataclass(frozen=True)
class Call:
    to: str
    data: str = "0x"
    value: int = 0
    operation: int = CALL


def approve_calldata(spender: str, amount: int) -> str:
    """ERC-20 approve(spender, amount)."""
    data = _APPROVE_SELECTOR + abi_encode(
        ["address", "uint256"],
        [Web3.to_checksum_address(spender), amount],
    )
    return "0x" + data.hex()


def approval_call(token: str, spender: str, amount: int) -> Call:
    return Call(
        to=Web3.to_checksum_address(token),
        data=approve_calldata(spender, amount),
    )


def multisend_calldata(calls: Sequence[Call]) -> str:
    """multiSend(bytes) over tightly-packed transactions.

    Each entry is packed, not ABI-encoded: operation (1 byte) ‖ to (20) ‖
    value (32) ‖ data length (32) ‖ data. The whole blob is then ABI-encoded as
    a single `bytes` argument.
    """
    packed = b""
    for call in calls:
        data = _to_bytes(call.data)
        packed += (
            call.operation.to_bytes(1, "big")
            + bytes.fromhex(Web3.to_checksum_address(call.to)[2:])
            + call.value.to_bytes(32, "big")
            + len(data).to_bytes(32, "big")
            + data
        )
    encoded = _MULTISEND_SELECTOR + abi_encode(["bytes"], [packed])
    return "0x" + encoded.hex()


def execute_user_op_calldata(call: Call) -> str:
    """Wrap a single call as Safe4337Module.executeUserOp()."""
    data = _EXECUTE_USER_OP_SELECTOR + abi_encode(
        ["address", "uint256", "bytes", "uint8"],
        [
            Web3.to_checksum_address(call.to),
            call.value,
            _to_bytes(call.data),
            call.operation,
        ],
    )
    return "0x" + data.hex()


def batched_user_op_calldata(calls: Sequence[Call]) -> str:
    """Wrap one or more calls as a single userOp's callData.

    One call executes directly; several are delegatecalled through
    MultiSendCallOnly so the batch is atomic — the paymaster approval and the
    transfer it pays for either both land or neither does.
    """
    if not calls:
        raise ValueError("a user operation needs at least one call")
    if len(calls) == 1:
        return execute_user_op_calldata(calls[0])
    return execute_user_op_calldata(
        Call(
            to=MULTISEND_CALL_ONLY,
            data=multisend_calldata(calls),
            value=0,
            operation=DELEGATECALL,
        )
    )


def paymaster_calls(
    actions: Call | Sequence[Call],
    *,
    token: str,
    paymaster: str,
    approval_amount: int = MAX_UINT256,
) -> tuple[Call, ...]:
    """The actions, with the paymaster's USDC approval riding in front of them.

    Pimlico's ERC-20 paymaster pulls the token from the smart account in postOp
    rather than being prefunded, so without an allowance the op reverts. Sending
    the approval as a separate transaction first would need gas — the thing we
    have no native token for — so it has to be inside the same op.

    Defaults to an unlimited approval: the paymaster is a fixed, audited
    contract, and re-approving on every op costs gas the user pays in USDC. Pass
    an exact amount to scope it per-op instead.

    Several actions ride together so the charge in postOp can be paid out of
    whatever the batch itself produced.
    """
    batch = (actions,) if isinstance(actions, Call) else tuple(actions)
    if not batch:
        raise ValueError("a user operation needs at least one action")
    return (approval_call(token, paymaster, approval_amount), *batch)


def build_user_operation(
    *,
    sender: str,
    nonce: int,
    calls: Sequence[Call],
    seed: SafeSeed | None = None,
    deployed: bool = True,
    signature: str = "0x",
) -> dict[str, Any]:
    """An unpacked (v0.7+) userOp, deploying the Safe if it is not yet on chain.

    EntryPoint v0.7 replaced v0.6's single concatenated `initCode` with the
    `factory`/`factoryData` pair, so the deployment rides in two fields.
    Including them for an *already-deployed* Safe reverts the op, so the caller
    must tell us which it is — a Safe redeploy fails quietly rather than
    raising, so this is not something to discover by trying.
    """
    user_operation: dict[str, Any] = {
        "sender": Web3.to_checksum_address(sender),
        "nonce": hex(nonce),
        "callData": batched_user_op_calldata(calls),
        "signature": signature,
    }
    if not deployed:
        if seed is None:
            raise ValueError(
                "deploying a Safe in the first user operation needs its seed"
            )
        factory, factory_data = safe_deployment.deploy_factory_data(seed)
        user_operation["factory"] = factory
        user_operation["factoryData"] = factory_data
    return user_operation


def _to_bytes(data: str | bytes) -> bytes:
    if isinstance(data, bytes):
        return data
    return bytes.fromhex(data[2:] if data.startswith("0x") else data)


__all__ = [
    "CALL",
    "DELEGATECALL",
    "MAX_UINT256",
    "MULTISEND_CALL_ONLY",
    "Call",
    "approval_call",
    "approve_calldata",
    "batched_user_op_calldata",
    "build_user_operation",
    "execute_user_op_calldata",
    "multisend_calldata",
    "paymaster_calls",
]
