from __future__ import annotations

import pytest
from eth_abi import decode as abi_decode
from eth_utils import keccak
from web3 import Web3

from open_allocator.exec import safe_deployment
from open_allocator.exec.safe_deployment import SafeSeed
from open_allocator.exec.user_operation import (
    CALL,
    DELEGATECALL,
    MAX_UINT256,
    MULTISEND_CALL_ONLY,
    Call,
    approve_calldata,
    batched_user_op_calldata,
    build_user_operation,
    multisend_calldata,
    paymaster_calls,
)

OWNER = Web3.to_checksum_address("0x" + "11" * 20)
USDC = Web3.to_checksum_address("0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913")
PAYMASTER = Web3.to_checksum_address("0x777777777777AeC03fd955926DbF81597e66834C")
VAULT = Web3.to_checksum_address("0x" + "cc" * 20)

SEED = SafeSeed(owners=(OWNER,), threshold=1)

_EXECUTE_USER_OP = keccak(text="executeUserOp(address,uint256,bytes,uint8)")[:4]
_APPROVE = keccak(text="approve(address,uint256)")[:4]


def _decode_execute_user_op(calldata: str) -> tuple[str, int, bytes, int]:
    raw = bytes.fromhex(calldata[2:])
    assert raw[:4] == _EXECUTE_USER_OP
    to, value, data, operation = abi_decode(
        ["address", "uint256", "bytes", "uint8"], raw[4:]
    )
    return Web3.to_checksum_address(to), value, data, operation


# --- the Safe executes through its module, never a raw call ----------------


def test_a_single_call_executes_directly() -> None:
    calldata = batched_user_op_calldata([Call(to=VAULT, data="0xdeadbeef")])

    to, value, data, operation = _decode_execute_user_op(calldata)
    assert to == VAULT
    assert value == 0
    assert data == bytes.fromhex("deadbeef")
    # A plain CALL: nothing to batch, so nothing to delegatecall.
    assert operation == CALL


def test_a_batch_is_delegatecalled_through_multisend_call_only() -> None:
    calldata = batched_user_op_calldata(
        [Call(to=USDC, data="0xaa"), Call(to=VAULT, data="0xbb")]
    )

    to, _, _, operation = _decode_execute_user_op(calldata)
    assert to == MULTISEND_CALL_ONLY
    assert operation == DELEGATECALL


def test_an_empty_user_operation_is_rejected() -> None:
    with pytest.raises(ValueError, match="at least one call"):
        batched_user_op_calldata([])


# --- approval injection: the paymaster pulls USDC from the account ---------


def test_the_approval_rides_in_front_of_the_action() -> None:
    action = Call(to=VAULT, data="0xdeadbeef")

    calls = paymaster_calls(action, token=USDC, paymaster=PAYMASTER)

    # Order matters: the paymaster's postOp pull happens after the op, but the
    # allowance must already exist when it does.
    assert len(calls) == 2
    assert calls[0].to == USDC
    assert calls[1] == action


def test_the_approval_targets_the_paymaster_for_an_unlimited_amount() -> None:
    approval = paymaster_calls(
        Call(to=VAULT), token=USDC, paymaster=PAYMASTER
    )[0]

    raw = bytes.fromhex(approval.data[2:])
    assert raw[:4] == _APPROVE
    spender, amount = abi_decode(["address", "uint256"], raw[4:])
    assert Web3.to_checksum_address(spender) == PAYMASTER
    # Unlimited by default: re-approving every op costs USDC the user pays.
    assert amount == MAX_UINT256


def test_an_exact_approval_can_be_scoped_per_op() -> None:
    approval = paymaster_calls(
        Call(to=VAULT), token=USDC, paymaster=PAYMASTER, approval_amount=1_000_000
    )[0]

    _, amount = abi_decode(["address", "uint256"], bytes.fromhex(approval.data[2:])[4:])
    assert amount == 1_000_000


def test_the_approval_and_the_action_land_atomically() -> None:
    # Both in one userOp: an approval that landed without its action would leave
    # a standing allowance for a transfer that never happened.
    calldata = batched_user_op_calldata(
        paymaster_calls(
            Call(to=VAULT, data="0xdeadbeef"), token=USDC, paymaster=PAYMASTER
        )
    )

    to, _, data, operation = _decode_execute_user_op(calldata)
    assert to == MULTISEND_CALL_ONLY
    assert operation == DELEGATECALL
    # Both legs are inside the one multiSend blob.
    assert bytes.fromhex(USDC[2:]) in data
    assert bytes.fromhex(VAULT[2:]) in data


# --- multiSend packing -----------------------------------------------------


def test_multisend_packs_each_call_without_padding() -> None:
    packed = multisend_calldata([Call(to=VAULT, data="0xabcdef", value=5)])

    raw = bytes.fromhex(packed[2:])
    blob = abi_decode(["bytes"], raw[4:])[0]
    # operation(1) + to(20) + value(32) + len(32) + data(3)
    assert len(blob) == 1 + 20 + 32 + 32 + 3
    assert blob[0] == CALL
    assert blob[1:21] == bytes.fromhex(VAULT[2:])
    assert int.from_bytes(blob[21:53], "big") == 5
    assert int.from_bytes(blob[53:85], "big") == 3
    assert blob[85:] == bytes.fromhex("abcdef")


def test_approve_calldata_is_standard_erc20() -> None:
    assert approve_calldata(PAYMASTER, 1).startswith("0x" + _APPROVE.hex())


# --- deploy-in-first-op ----------------------------------------------------


def test_the_first_user_operation_carries_the_safe_deployment() -> None:
    user_op = build_user_operation(
        sender="0x" + "ab" * 20,
        nonce=0,
        calls=[Call(to=VAULT)],
        seed=SEED,
        deployed=False,
    )

    # v0.7 split the deployment into factory/factoryData; v0.6's packed initCode
    # is gone, and a bundler given initCode here would reject the op.
    factory, factory_data = safe_deployment.deploy_factory_data(SEED)
    assert user_op["factory"] == factory
    assert user_op["factoryData"] == factory_data
    assert "initCode" not in user_op


def test_a_deployed_safe_sends_no_factory_fields() -> None:
    user_op = build_user_operation(
        sender="0x" + "ab" * 20,
        nonce=3,
        calls=[Call(to=VAULT)],
        seed=SEED,
        deployed=True,
    )

    # Re-running the factory for a live Safe reverts the whole op.
    assert "factory" not in user_op
    assert "factoryData" not in user_op
    assert user_op["nonce"] == "0x3"


def test_deploying_without_a_seed_is_refused() -> None:
    with pytest.raises(ValueError, match="seed"):
        build_user_operation(
            sender="0x" + "ab" * 20, nonce=0, calls=[Call(to=VAULT)], deployed=False
        )
