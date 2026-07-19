from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from eth_abi import encode as abi_encode
from eth_account import Account
from eth_utils import keccak
from web3 import Web3

from open_allocator.exec import paymaster_registry, safe_deployment

# Signing a Safe4337Module userOp.
#
# The module defines its *own* EIP-712 hash and does not use the EntryPoint's
# getUserOpHash. Two consequences that are easy to get wrong:
#
# - The EIP-712 verifyingContract is the **module**, not the Safe. The Safe is a
#   field inside the struct instead.
# - The struct hashes the *packed* (on-chain) userOp — initCode, accountGasLimits,
#   gasFees, paymasterAndData — while the JSON-RPC form the bundler takes is the
#   *unpacked* v0.7 one (factory/factoryData, callGasLimit, ...). We build the
#   unpacked form and pack it here purely to hash it; the two must agree or the
#   op reverts in validateUserOp with a signature error rather than saying why.
#
# Transcribed from Safe4337Module.sol at tag 4337/v0.3.0 — the deployed release.
# `main` has since diverged; do not read the hash off main.

# keccak256("EIP712Domain(uint256 chainId,address verifyingContract)")
DOMAIN_SEPARATOR_TYPEHASH = bytes.fromhex(
    "47e79534a245952e8b16893a336b85a3d9ea9fa8c573f3d803afb92a79469218"
)

# keccak256(
#   "SafeOp(address safe,uint256 nonce,bytes initCode,bytes callData,"
#   "uint128 verificationGasLimit,uint128 callGasLimit,uint256 preVerificationGas,"
#   "uint128 maxPriorityFeePerGas,uint128 maxFeePerGas,bytes paymasterAndData,"
#   "uint48 validAfter,uint48 validUntil,address entryPoint)"
# )
SAFE_OP_TYPEHASH = bytes.fromhex(
    "c03dfc11d8b10bf9cf703d558958c8c42777f785d998c62060d85a4f0ef6ea7f"
)

# The module packs validAfter ‖ validUntil in front of the owner signatures, so a
# signature is never just 65 bytes. 0/0 means "valid forever": the EntryPoint
# treats validUntil == 0 as no expiry.
VALIDITY_PREFIX_BYTES = 12
OWNER_SIGNATURE_BYTES = 65

# A gas estimate needs a signature of the right *shape* before a real one can
# exist — the estimate feeds the gas fields, which the real signature commits to.
# v=0x1f (31) is deliberate: >30 sends checkSignatures down the eth_sign branch,
# so a stub can never accidentally recover a real owner.
_DUMMY_OWNER_SIGNATURE = bytes(64) + bytes([0x1F])


def domain_separator(*, chain_id: int, module: str) -> bytes:
    """EIP-712 domain separator — verifyingContract is the module, not the Safe."""
    return keccak(
        abi_encode(
            ["bytes32", "uint256", "address"],
            [DOMAIN_SEPARATOR_TYPEHASH, chain_id, Web3.to_checksum_address(module)],
        )
    )


def init_code(user_operation: Mapping[str, Any]) -> bytes:
    """The packed struct's `initCode`: factory ‖ factoryData, or empty.

    v0.7 unpacked the RPC form into factory/factoryData, but the on-chain struct
    the module hashes still carries the v0.6-style concatenation.
    """
    factory = user_operation.get("factory")
    if not factory:
        return b""
    return _to_bytes(factory) + _to_bytes(user_operation.get("factoryData", "0x"))


def paymaster_and_data(user_operation: Mapping[str, Any]) -> bytes:
    """The packed struct's `paymasterAndData`, or empty when unsponsored.

    paymaster (20) ‖ paymasterVerificationGasLimit (16) ‖ paymasterPostOpGasLimit
    (16) ‖ paymasterData.
    """
    paymaster = user_operation.get("paymaster")
    if not paymaster:
        return b""
    return (
        _to_bytes(paymaster)
        + _as_int(user_operation.get("paymasterVerificationGasLimit", 0)).to_bytes(
            16, "big"
        )
        + _as_int(user_operation.get("paymasterPostOpGasLimit", 0)).to_bytes(16, "big")
        + _to_bytes(user_operation.get("paymasterData", "0x"))
    )


def operation_hash(
    user_operation: Mapping[str, Any],
    *,
    chain_id: int,
    module: str = safe_deployment.SAFE_4337_MODULE,
    entry_point: str = paymaster_registry.ENTRY_POINT_V07,
    valid_after: int = 0,
    valid_until: int = 0,
) -> bytes:
    """Safe4337Module.getOperationHash() for an unpacked v0.7 userOp.

    `entry_point` must be the module's immutable SUPPORTED_ENTRYPOINT, which for
    every deployed Safe4337Module release is v0.7. It is a parameter only so a
    test can point at a locally deployed module.
    """
    struct_hash = keccak(
        abi_encode(
            [
                "bytes32",
                "address",
                "uint256",
                "bytes32",
                "bytes32",
                "uint128",
                "uint128",
                "uint256",
                "uint128",
                "uint128",
                "bytes32",
                "uint48",
                "uint48",
                "address",
            ],
            [
                SAFE_OP_TYPEHASH,
                Web3.to_checksum_address(_field(user_operation, "sender")),
                _as_int(_field(user_operation, "nonce")),
                keccak(init_code(user_operation)),
                keccak(_to_bytes(_field(user_operation, "callData"))),
                _as_int(user_operation.get("verificationGasLimit", 0)),
                _as_int(user_operation.get("callGasLimit", 0)),
                _as_int(user_operation.get("preVerificationGas", 0)),
                _as_int(user_operation.get("maxPriorityFeePerGas", 0)),
                _as_int(user_operation.get("maxFeePerGas", 0)),
                keccak(paymaster_and_data(user_operation)),
                valid_after,
                valid_until,
                Web3.to_checksum_address(entry_point),
            ],
        )
    )
    return keccak(
        b"\x19\x01" + domain_separator(chain_id=chain_id, module=module) + struct_hash
    )


def encode_signature(
    owner_signatures: Sequence[bytes],
    *,
    valid_after: int = 0,
    valid_until: int = 0,
) -> str:
    """validAfter ‖ validUntil ‖ ownerSignatures, as the module expects."""
    return (
        "0x"
        + (
            valid_after.to_bytes(6, "big")
            + valid_until.to_bytes(6, "big")
            + b"".join(owner_signatures)
        ).hex()
    )


def dummy_signature(owner_count: int, **kwargs: int) -> str:
    """A correctly-shaped unusable signature, for gas estimation only.

    Bundlers estimate against the signature's length (it is hashed and paid for),
    so estimating with "0x" underprices verification and the real op then fails
    AA23. This never recovers to an owner.
    """
    return encode_signature([_DUMMY_OWNER_SIGNATURE] * owner_count, **kwargs)


def sign_operation_hash(digest: bytes, private_keys: Sequence[str]) -> list[bytes]:
    """Owner ECDSA signatures over the operation hash, ordered as Safe requires.

    Safe.checkSignatures walks the signatures and enforces
    `currentOwner > lastOwner`, so they must be sorted by **signer address
    ascending** — which is not the Safe's owner-list order (that one is fixed at
    setup and feeds the address). Sorting the *owners* anywhere upstream moves
    the Safe; sorting these signatures is mandatory. The two are unrelated and
    both are load-bearing.
    """
    signed = []
    for key in private_keys:
        account = Account.from_key(key)
        # "unsafe" names the risk of signing an opaque digest — which is the
        # whole job here. The digest is one we computed from the userOp above,
        # not something a counterparty handed us.
        signature = Account.unsafe_sign_hash(digest, key)
        signed.append((Web3.to_checksum_address(account.address), signature))
    signed.sort(key=lambda pair: int(pair[0], 16))
    return [
        # v stays 27/28: checkSignatures reads v <= 30 as a plain ecrecover over
        # the data hash, which is exactly what the module asks it to verify.
        signature.r.to_bytes(32, "big")
        + signature.s.to_bytes(32, "big")
        + bytes([signature.v])
        for _, signature in signed
    ]


def sign_user_operation(
    user_operation: Mapping[str, Any],
    *,
    private_keys: Sequence[str],
    chain_id: int,
    module: str = safe_deployment.SAFE_4337_MODULE,
    entry_point: str = paymaster_registry.ENTRY_POINT_V07,
    valid_after: int = 0,
    valid_until: int = 0,
) -> dict[str, Any]:
    """The userOp with a real owner signature in place.

    Sign last: the hash commits to every field except `signature` itself, so any
    later edit — a gas re-estimate, a fresh paymaster quote — silently invalidates
    it.
    """
    if not private_keys:
        raise ValueError("signing a user operation needs at least one owner key")
    digest = operation_hash(
        user_operation,
        chain_id=chain_id,
        module=module,
        entry_point=entry_point,
        valid_after=valid_after,
        valid_until=valid_until,
    )
    signed = dict(user_operation)
    signed["signature"] = encode_signature(
        sign_operation_hash(digest, private_keys),
        valid_after=valid_after,
        valid_until=valid_until,
    )
    return signed


def _field(payload: Mapping[str, Any], key: str) -> Any:
    if key not in payload:
        raise ValueError(f"user operation is missing {key}")
    return payload[key]


def _as_int(value: Any) -> int:
    if isinstance(value, bool):
        raise TypeError("expected a number, got a bool")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value, 16) if value.startswith("0x") else int(value)
    raise TypeError(f"expected a number, got {type(value).__name__}")


def _to_bytes(data: str | bytes) -> bytes:
    if isinstance(data, bytes):
        return data
    return bytes.fromhex(data[2:] if data.startswith("0x") else data)


__all__ = [
    "DOMAIN_SEPARATOR_TYPEHASH",
    "OWNER_SIGNATURE_BYTES",
    "SAFE_OP_TYPEHASH",
    "VALIDITY_PREFIX_BYTES",
    "domain_separator",
    "dummy_signature",
    "encode_signature",
    "init_code",
    "operation_hash",
    "paymaster_and_data",
    "sign_operation_hash",
    "sign_user_operation",
]
