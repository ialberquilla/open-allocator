from __future__ import annotations

from pathlib import Path

import pytest
from eth_account import Account
from eth_utils import keccak
from web3 import EthereumTesterProvider, Web3

from open_allocator.exec import paymaster_registry
from open_allocator.exec.safe_4337_signature import (
    DOMAIN_SEPARATOR_TYPEHASH,
    SAFE_OP_TYPEHASH,
    domain_separator,
    dummy_signature,
    encode_signature,
    init_code,
    operation_hash,
    paymaster_and_data,
    sign_operation_hash,
    sign_user_operation,
)
from open_allocator.exec.safe_deployment import (
    Safe4337Wiring,
    SafeSeed,
    predict_address,
    setup_calldata,
)
from open_allocator.exec.user_operation import (
    Call,
    build_user_operation,
    paymaster_calls,
)

USDC = Web3.to_checksum_address("0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913")
PAYMASTER = Web3.to_checksum_address("0x777777777777AeC03fd955926DbF81597e66834C")
MODULE = Web3.to_checksum_address("0x75cf11467937ce3F2f357CE24ffc3DBF8fD5c226")
ENTRY_POINT_V07 = paymaster_registry.ENTRY_POINT_V07

BASE = 8453
MONAD = 143


# --- the constants are the module's, not ours -------------------------------
#
# Both are pasted 32-byte literals in Safe4337Module.sol, so a transcription
# typo would be invisible. Deriving them from the documented type strings makes
# the paste self-checking.


def test_safe_op_typehash_matches_its_type_string() -> None:
    assert SAFE_OP_TYPEHASH == keccak(
        text=(
            "SafeOp(address safe,uint256 nonce,bytes initCode,bytes callData,"
            "uint128 verificationGasLimit,uint128 callGasLimit,"
            "uint256 preVerificationGas,uint128 maxPriorityFeePerGas,"
            "uint128 maxFeePerGas,bytes paymasterAndData,uint48 validAfter,"
            "uint48 validUntil,address entryPoint)"
        )
    )


def test_domain_separator_typehash_matches_its_type_string() -> None:
    assert DOMAIN_SEPARATOR_TYPEHASH == keccak(
        text="EIP712Domain(uint256 chainId,address verifyingContract)"
    )


def test_domain_separator_verifying_contract_is_the_module_not_the_safe() -> None:
    """The Safe is a struct field; the module is the EIP-712 verifying contract.

    Getting these the wrong way round yields a plausible hash that no Safe will
    ever accept.
    """
    safe = Web3.to_checksum_address("0x" + "ab" * 20)
    assert domain_separator(chain_id=BASE, module=MODULE) != domain_separator(
        chain_id=BASE, module=safe
    )


# --- vectors verified against the deployed module ---------------------------


def _seed() -> SafeSeed:
    return SafeSeed(
        owners=(
            Web3.to_checksum_address("0x" + "11" * 20),
            Web3.to_checksum_address("0x" + "22" * 20),
        ),
        threshold=1,
        salt_nonce=7,
    )


def _sponsored_user_op(sender: str) -> dict[str, object]:
    calls = paymaster_calls(
        Call(to=USDC, data="0xdeadbeef", value=0),
        token=USDC,
        paymaster=PAYMASTER,
    )
    user_op = build_user_operation(
        sender=sender, nonce=5, calls=calls, seed=_seed(), deployed=False
    )
    user_op.update(
        {
            "callGasLimit": 111111,
            "verificationGasLimit": 222222,
            "preVerificationGas": 333333,
            "maxFeePerGas": 444444,
            "maxPriorityFeePerGas": 55555,
            "paymaster": PAYMASTER,
            "paymasterVerificationGasLimit": 66666,
            "paymasterPostOpGasLimit": 77777,
            "paymasterData": "0x" + USDC[2:].lower(),
        }
    )
    return user_op


def _bare_user_op(sender: str) -> dict[str, object]:
    user_op = build_user_operation(
        sender=sender,
        nonce=9,
        calls=(Call(to=USDC, data="0x", value=0),),
        deployed=True,
    )
    user_op.update(
        {
            "callGasLimit": 1,
            "verificationGasLimit": 2,
            "preVerificationGas": 3,
            "maxFeePerGas": 4,
            "maxPriorityFeePerGas": 5,
        }
    )
    return user_op


_SAFE_ADDRESS = Web3.to_checksum_address("0xd14A562B79e00db8FaA73faC934298D8A8C194e8")
LIVE_VECTORS = {
    (
        BASE,
        "sponsored",
    ): "bc27b623f6698cca2b6fa286ed33438c4ef1a8843d8e2ab29858a16ce0af05a7",
    (BASE, "bare"): "7b0d33b75fdf36a5c13613a4e04579a78f5318c82fd0a8d401481179fd3fc39f",
    (
        MONAD,
        "sponsored",
    ): "16e71c1cb8130779337423947d23d1d0c25ca54a73da277ce9072ff6d405c4ba",
    (MONAD, "bare"): "a30de3aafdfe3d803eee444a70b1e4a7a3cc5ea7a803426efeec44d5473c4895",
}


@pytest.mark.parametrize("chain_id", [BASE, MONAD])
def test_sponsored_operation_hash_matches_the_deployed_module(chain_id: int) -> None:
    digest = operation_hash(
        _sponsored_user_op(_SAFE_ADDRESS),
        chain_id=chain_id,
        module=MODULE,
        entry_point=ENTRY_POINT_V07,
    )
    assert digest.hex() == LIVE_VECTORS[(chain_id, "sponsored")]


@pytest.mark.parametrize("chain_id", [BASE, MONAD])
def test_bare_operation_hash_matches_the_deployed_module(chain_id: int) -> None:
    """No initCode, no paymaster, with a validity window."""
    digest = operation_hash(
        _bare_user_op(_SAFE_ADDRESS),
        chain_id=chain_id,
        module=MODULE,
        entry_point=ENTRY_POINT_V07,
        valid_after=1700000000,
        valid_until=1800000000,
    )
    assert digest.hex() == LIVE_VECTORS[(chain_id, "bare")]


def test_operation_hash_is_chain_bound() -> None:
    """The chain id is in the domain separator, so an op cannot be replayed."""
    user_op = _sponsored_user_op(_SAFE_ADDRESS)
    assert operation_hash(user_op, chain_id=BASE) != operation_hash(
        user_op, chain_id=MONAD
    )


def test_operation_hash_covers_the_validity_window() -> None:
    user_op = _bare_user_op(_SAFE_ADDRESS)
    assert operation_hash(user_op, chain_id=BASE) != operation_hash(
        user_op, chain_id=BASE, valid_until=1800000000
    )


def test_operation_hash_covers_the_paymaster_fields() -> None:
    """Otherwise a bundler could swap the paymaster after signing."""
    user_op = _sponsored_user_op(_SAFE_ADDRESS)
    tampered = dict(user_op) | {"paymaster": Web3.to_checksum_address("0x" + "99" * 20)}
    assert operation_hash(user_op, chain_id=BASE) != operation_hash(
        tampered, chain_id=BASE
    )


# --- packing the unpacked form ---------------------------------------------


def test_init_code_concatenates_factory_and_factory_data() -> None:
    """v0.7 unpacked the RPC form, but the hashed struct still packs it."""
    user_op = {"factory": "0x" + "ab" * 20, "factoryData": "0xc0ffee"}
    assert init_code(user_op) == bytes.fromhex("ab" * 20 + "c0ffee")


def test_init_code_is_empty_for_a_deployed_safe() -> None:
    assert init_code({"sender": _SAFE_ADDRESS}) == b""


def test_paymaster_and_data_packs_the_gas_limits_as_16_byte_fields() -> None:
    packed = paymaster_and_data(
        {
            "paymaster": PAYMASTER,
            "paymasterVerificationGasLimit": 1,
            "paymasterPostOpGasLimit": 2,
            "paymasterData": "0xbeef",
        }
    )
    assert packed[:20] == bytes.fromhex(PAYMASTER[2:])
    assert packed[20:36] == (1).to_bytes(16, "big")
    assert packed[36:52] == (2).to_bytes(16, "big")
    assert packed[52:] == bytes.fromhex("beef")


def test_paymaster_and_data_is_empty_when_unsponsored() -> None:
    assert paymaster_and_data({"sender": _SAFE_ADDRESS}) == b""


# --- the signature envelope -------------------------------------------------


def test_encode_signature_prefixes_the_validity_window() -> None:
    raw = bytes.fromhex(
        encode_signature([b"\x01" * 65], valid_after=1, valid_until=2)[2:]
    )
    assert raw[:6] == (1).to_bytes(6, "big")
    assert raw[6:12] == (2).to_bytes(6, "big")
    assert raw[12:] == b"\x01" * 65


def test_dummy_signature_is_the_right_length_for_estimation() -> None:
    """Length is what the bundler prices; content must never recover an owner."""
    raw = bytes.fromhex(dummy_signature(2)[2:])
    assert len(raw) == 12 + 2 * 65


def test_dummy_signature_cannot_be_mistaken_for_a_real_one() -> None:
    """v > 30 sends checkSignatures down the eth_sign branch, not ecrecover."""
    raw = bytes.fromhex(dummy_signature(1)[2:])
    assert raw[12 + 64] == 0x1F


# --- owner ordering: the trap ----------------------------------------------


def test_signatures_are_sorted_by_signer_address_not_key_order() -> None:
    """Safe.checkSignatures enforces `currentOwner > lastOwner` (GS026).

    This is *not* the Safe's owner-list order — that one is fixed at setup() and
    feeds the address. Passing keys in owner-list order and signing them in that
    order reverts whenever the list is not already ascending.
    """
    keys = [Account.create().key.hex() for _ in range(3)]
    addresses = [Account.from_key(key).address for key in keys]
    digest = keccak(text="anything")

    signatures = sign_operation_hash(digest, keys)
    recovered = [Account._recover_hash(digest, signature=sig) for sig in signatures]
    assert recovered == sorted(addresses, key=lambda a: int(a, 16))
    assert len(set(recovered)) == 3


def test_signatures_recover_to_the_owners_that_signed() -> None:
    keys = [Account.create().key.hex() for _ in range(2)]
    digest = keccak(text="anything")
    signatures = sign_operation_hash(digest, keys)
    recovered = {Account._recover_hash(digest, signature=sig) for sig in signatures}
    assert recovered == {Account.from_key(key).address for key in keys}


def test_signature_v_stays_in_the_ecrecover_branch() -> None:
    """v <= 30 tells checkSignatures to ecrecover the data hash directly, which
    is what the module asks it to verify. Bumping v would mean eth_sign."""
    signatures = sign_operation_hash(keccak(text="x"), [Account.create().key.hex()])
    assert signatures[0][64] in (27, 28)


# --- signing a whole operation ---------------------------------------------


def test_sign_user_operation_fills_in_the_signature_and_changes_nothing_else() -> None:
    user_op = _sponsored_user_op(_SAFE_ADDRESS)
    key = Account.create().key.hex()
    signed = sign_user_operation(user_op, private_keys=[key], chain_id=BASE)

    assert signed["signature"] != user_op.get("signature", "0x")
    assert {k: v for k, v in signed.items() if k != "signature"} == {
        k: v for k, v in user_op.items() if k != "signature"
    }


def test_sign_user_operation_does_not_mutate_its_input() -> None:
    user_op = _sponsored_user_op(_SAFE_ADDRESS)
    before = dict(user_op)
    sign_user_operation(
        user_op, private_keys=[Account.create().key.hex()], chain_id=BASE
    )
    assert user_op == before


def test_sign_user_operation_signs_the_operation_hash() -> None:
    user_op = _bare_user_op(_SAFE_ADDRESS)
    key = Account.create().key.hex()
    signed = sign_user_operation(
        user_op, private_keys=[key], chain_id=BASE, valid_after=1, valid_until=2
    )

    raw = bytes.fromhex(signed["signature"][2:])
    digest = operation_hash(user_op, chain_id=BASE, valid_after=1, valid_until=2)
    assert (
        Account._recover_hash(digest, signature=raw[12:])
        == Account.from_key(key).address
    )


def test_sign_user_operation_needs_a_key() -> None:
    with pytest.raises(ValueError, match="at least one owner key"):
        sign_user_operation(
            _bare_user_op(_SAFE_ADDRESS), private_keys=[], chain_id=BASE
        )


# --- a real Safe accepts the signature -------------------------------------
#
# The ordering rule and the v convention are only claims until the contract that
# enforces them agrees. eth-tester has no bundler, so the module cannot run
# validateUserOp here — but checkSignatures is the call it makes, and this is a
# real Safe 1.4.1 checking a real signature over our real operation hash.

_MODULE_SETUP_RUNTIME = bytes.fromhex(
    (Path(__file__).parent / "fixtures" / "safe_module_setup_v0.3.0.hex")
    .read_text()
    .strip()
)

_CHECK_SIGNATURES = keccak(text="checkSignatures(bytes32,bytes,bytes)")[:4]


@pytest.fixture(scope="module")
def deployed_safe() -> dict[str, object]:
    """A real 2-of-2 Safe on eth-tester, owned by keys we hold."""
    from eth_abi import encode as abi_encode
    from safe_eth.eth import EthereumClient
    from safe_eth.safe.proxy_factory import ProxyFactoryV141
    from safe_eth.safe.safe import SafeV141

    w3 = Web3(EthereumTesterProvider())
    client = EthereumClient.__new__(EthereumClient)
    client.w3 = w3
    client.ethereum_node_url = "eth-tester://local"
    client.slow_provider_timeout = 0
    client.provider_timeout = 0
    client.retry_count = 0

    deployer = Account.create()
    w3.eth.send_transaction(
        {
            "from": w3.eth.accounts[0],
            "to": deployer.address,
            "value": w3.to_wei(10, "ether"),
        }
    )
    singleton = SafeV141.deploy_contract(client, deployer).contract_address
    factory = ProxyFactoryV141.deploy_contract(client, deployer).contract_address

    owner_keys = [Account.create().key.hex() for _ in range(2)]
    owners = tuple(Account.from_key(key).address for key in owner_keys)
    wiring = Safe4337Wiring(
        module=singleton,
        module_setup=_deploy_runtime(w3, deployer, _MODULE_SETUP_RUNTIME),
    )
    seed = SafeSeed(owners=owners, threshold=2)

    initializer = setup_calldata(seed, wiring=wiring)
    safe_address = predict_address(
        w3,
        seed,
        chain_id=BASE,
        singleton=singleton,
        proxy_factory=factory,
        wiring=wiring,
    )
    data = keccak(text="createProxyWithNonce(address,bytes,uint256)")[:4] + abi_encode(
        ["address", "bytes", "uint256"],
        [Web3.to_checksum_address(singleton), initializer, seed.salt_nonce],
    )
    signed = deployer.sign_transaction(
        {
            "from": deployer.address,
            "to": Web3.to_checksum_address(factory),
            "data": "0x" + data.hex(),
            "nonce": w3.eth.get_transaction_count(deployer.address),
            "gas": 3_000_000,
            "gasPrice": w3.eth.gas_price,
            "chainId": w3.eth.chain_id,
        }
    )
    receipt = w3.eth.wait_for_transaction_receipt(
        w3.eth.send_raw_transaction(signed.raw_transaction)
    )
    assert receipt["status"] == 1
    # The Safe lands where prediction said it would — asserted rather than read
    # back out of the receipt, so this fixture also re-proves that property.
    assert len(w3.eth.get_code(safe_address)) > 0
    return {"w3": w3, "safe": safe_address, "owner_keys": owner_keys, "owners": owners}


def _deploy_runtime(w3: Web3, deployer: object, runtime: bytes) -> str:
    init_code_bytes = (
        b"\x61"
        + len(runtime).to_bytes(2, "big")
        + b"\x80"
        + b"\x60\x0c"
        + b"\x60\x00"
        + b"\x39"
        + b"\x60\x00"
        + b"\xf3"
        + runtime
    )
    signed = deployer.sign_transaction(
        {
            "from": deployer.address,
            "data": "0x" + init_code_bytes.hex(),
            "nonce": w3.eth.get_transaction_count(deployer.address),
            "gas": 3_000_000,
            "gasPrice": w3.eth.gas_price,
            "chainId": w3.eth.chain_id,
        }
    )
    receipt = w3.eth.wait_for_transaction_receipt(
        w3.eth.send_raw_transaction(signed.raw_transaction)
    )
    return receipt["contractAddress"]


def _check_signatures(
    safe_chain: dict[str, object], digest: bytes, signature_blob: bytes
) -> None:
    """Safe.checkSignatures — a view call that reverts when the sigs are bad."""
    from eth_abi import encode as abi_encode

    w3: Web3 = safe_chain["w3"]
    data = _CHECK_SIGNATURES + abi_encode(
        ["bytes32", "bytes", "bytes"],
        [digest, digest, signature_blob],
    )
    w3.eth.call({"to": safe_chain["safe"], "data": "0x" + data.hex()})


def test_a_real_safe_accepts_our_signature(deployed_safe: dict[str, object]) -> None:
    digest = keccak(text="a safe operation hash")
    signatures = sign_operation_hash(digest, deployed_safe["owner_keys"])
    _check_signatures(deployed_safe, digest, b"".join(signatures))


def test_a_real_safe_rejects_signatures_in_the_wrong_order(
    deployed_safe: dict[str, object],
) -> None:
    """GS026 — proof that sorting is mandatory, not cosmetic.

    This is what "sign in owner order" produces whenever the owner list is not
    already ascending by address.
    """
    digest = keccak(text="a safe operation hash")
    signatures = sign_operation_hash(digest, deployed_safe["owner_keys"])
    with pytest.raises(Exception, match="GS026"):
        _check_signatures(deployed_safe, digest, b"".join(reversed(signatures)))


def test_a_real_safe_rejects_a_short_signature_set(
    deployed_safe: dict[str, object],
) -> None:
    """Threshold is 2; one owner is not enough."""
    digest = keccak(text="a safe operation hash")
    signatures = sign_operation_hash(digest, deployed_safe["owner_keys"][:1])
    with pytest.raises(Exception, match="GS020"):
        _check_signatures(deployed_safe, digest, b"".join(signatures))


def test_a_real_safe_rejects_our_dummy_signature(
    deployed_safe: dict[str, object],
) -> None:
    """The estimation stub must be unusable, not merely unlikely."""
    digest = keccak(text="a safe operation hash")
    raw = bytes.fromhex(dummy_signature(2)[2:])
    with pytest.raises(Exception, match="GS026"):
        _check_signatures(deployed_safe, digest, raw[12:])
