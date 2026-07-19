from __future__ import annotations

from pathlib import Path

import pytest
from eth_account import Account
from web3 import EthereumTesterProvider, Web3

from open_allocator.exec import chains
from open_allocator.exec.safe_deployment import (
    Safe4337Wiring,
    SafeDeploymentError,
    SafeFactoryMissing,
    SafeSeed,
    deploy_transaction,
    deployment_status,
    is_deployed,
    predict_address,
    setup_calldata,
)

# The real SafeModuleSetup v0.3.0 runtime bytecode, read off Base and Monad at
# the canonical 0x2dd68b00…5b47 (byte-identical on both, keccak af2d170bb766d277).
# Vendored rather than fetched so these stay offline, and used rather than a stub
# because Safe.setup() delegatecalls it — a stand-in would prove nothing about
# whether a real 4337 Safe deploys.
_MODULE_SETUP_RUNTIME = bytes.fromhex(
    (Path(__file__).parent / "fixtures" / "safe_module_setup_v0.3.0.hex")
    .read_text()
    .strip()
)

OWNER_A = Web3.to_checksum_address("0x" + "11" * 20)
OWNER_B = Web3.to_checksum_address("0x" + "22" * 20)
OWNER_C = Web3.to_checksum_address("0x" + "33" * 20)

BASE = 8453
ZKSYNC_ERA = 324


# --- a local EVM with the real Safe contracts on it ------------------------


@pytest.fixture(scope="module")
def safe_chain() -> dict[str, object]:
    """eth-tester with the real Safe singleton + proxy factory deployed."""
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
    wiring = Safe4337Wiring(
        # Any address with code satisfies the fallback-handler slot; setup()
        # only checks it is a contract. The module's own behaviour is exercised
        # by the EntryPoint, which eth-tester has no bundler for.
        module=singleton,
        module_setup=_deploy_runtime(w3, deployer, _MODULE_SETUP_RUNTIME),
    )
    return {
        "w3": w3,
        "singleton": singleton,
        "factory": factory,
        "deployer": deployer,
        "wiring": wiring,
    }


def _deploy_runtime(w3: Web3, deployer: object, runtime: bytes) -> str:
    """Put fixed runtime bytecode on the chain at a fresh address.

    eth-tester has no way to write code to a chosen address, so the canonical
    0x2dd68b00… cannot be reproduced locally. The address does not matter here:
    it is delegatecalled, and both prediction and deploy read it from the same
    Safe4337Wiring, so they stay consistent.
    """
    init_code = (
        b"\x61"
        + len(runtime).to_bytes(2, "big")  # PUSH2 <len>
        + b"\x80"  # DUP1
        + b"\x60\x0c"  # PUSH1 12 (runtime offset in this init code)
        + b"\x60\x00"  # PUSH1 0
        + b"\x39"  # CODECOPY
        + b"\x60\x00"  # PUSH1 0
        + b"\xf3"  # RETURN
        + runtime
    )
    signed = deployer.sign_transaction(
        {
            "from": deployer.address,
            "data": "0x" + init_code.hex(),
            "nonce": w3.eth.get_transaction_count(deployer.address),
            "gas": 3_000_000,
            "gasPrice": w3.eth.gas_price,
            "chainId": w3.eth.chain_id,
        }
    )
    receipt = w3.eth.wait_for_transaction_receipt(
        w3.eth.send_raw_transaction(signed.raw_transaction)
    )
    address = receipt["contractAddress"]
    assert w3.eth.get_code(address) == runtime
    return address


def predict(safe_chain: dict[str, object], seed: SafeSeed, chain_id: int = BASE) -> str:
    return predict_address(
        safe_chain["w3"],
        seed,
        chain_id=chain_id,
        singleton=safe_chain["singleton"],
        proxy_factory=safe_chain["factory"],
        wiring=safe_chain["wiring"],
    )


# --- the address is real: prediction matches an actual deploy --------------


def test_predicted_address_matches_a_real_deploy(
    safe_chain: dict[str, object],
) -> None:
    w3: Web3 = safe_chain["w3"]
    deployer = safe_chain["deployer"]
    seed = SafeSeed(owners=(OWNER_A, OWNER_B), threshold=2, salt_nonce=42)

    predicted = predict(safe_chain, seed)
    assert not is_deployed(w3, predicted)

    tx = deploy_transaction(
        w3,
        seed,
        singleton=safe_chain["singleton"],
        proxy_factory=safe_chain["factory"],
        wiring=safe_chain["wiring"],
    )
    signed = deployer.sign_transaction(
        {
            **tx,
            "from": deployer.address,
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

    # The counterfactual address is where the Safe actually landed.
    assert is_deployed(w3, predicted)


def test_deployment_status_reports_before_and_after(
    safe_chain: dict[str, object],
) -> None:
    w3: Web3 = safe_chain["w3"]
    deployer = safe_chain["deployer"]
    seed = SafeSeed(owners=(OWNER_A,), threshold=1, salt_nonce=7)

    before = deployment_status(
        w3,
        seed,
        chain_id=BASE,
        singleton=safe_chain["singleton"],
        proxy_factory=safe_chain["factory"],
        wiring=safe_chain["wiring"],
    )
    assert before.deployed is False

    tx = deploy_transaction(
        w3,
        seed,
        singleton=safe_chain["singleton"],
        proxy_factory=safe_chain["factory"],
        wiring=safe_chain["wiring"],
    )
    signed = deployer.sign_transaction(
        {
            **tx,
            "from": deployer.address,
            "nonce": w3.eth.get_transaction_count(deployer.address),
            "gas": 3_000_000,
            "gasPrice": w3.eth.gas_price,
            "chainId": w3.eth.chain_id,
        }
    )
    w3.eth.wait_for_transaction_receipt(
        w3.eth.send_raw_transaction(signed.raw_transaction)
    )

    after = deployment_status(
        w3,
        seed,
        chain_id=BASE,
        singleton=safe_chain["singleton"],
        proxy_factory=safe_chain["factory"],
        wiring=safe_chain["wiring"],
    )
    assert after.address == before.address
    assert after.deployed is True


def test_redeploying_the_same_seed_reverts(safe_chain: dict[str, object]) -> None:
    w3: Web3 = safe_chain["w3"]
    deployer = safe_chain["deployer"]
    seed = SafeSeed(owners=(OWNER_B,), threshold=1, salt_nonce=99)

    tx = deploy_transaction(
        w3,
        seed,
        singleton=safe_chain["singleton"],
        proxy_factory=safe_chain["factory"],
        wiring=safe_chain["wiring"],
    )

    def send() -> int:
        signed = deployer.sign_transaction(
            {
                **tx,
                "from": deployer.address,
                "nonce": w3.eth.get_transaction_count(deployer.address),
                "gas": 3_000_000,
                "gasPrice": w3.eth.gas_price,
                "chainId": w3.eth.chain_id,
            }
        )
        receipt = w3.eth.wait_for_transaction_receipt(
            w3.eth.send_raw_transaction(signed.raw_transaction)
        )
        return receipt["status"]

    assert send() == 1
    # CREATE2 collides on the second attempt and the tx reverts (status 0)
    # rather than raising — it silently burns gas. So the caller must check
    # deployment_status first; "just send it again" is not free.
    assert send() == 0


# --- determinism ----------------------------------------------------------


def test_same_seed_yields_the_same_address(safe_chain: dict[str, object]) -> None:
    seed = SafeSeed(owners=(OWNER_A, OWNER_B), threshold=2, salt_nonce=1)

    assert predict(safe_chain, seed) == predict(safe_chain, seed)


def test_same_seed_yields_the_same_address_across_chains(
    safe_chain: dict[str, object],
) -> None:
    seed = SafeSeed(owners=(OWNER_A, OWNER_B), threshold=2, salt_nonce=1)

    # Nothing chain-specific feeds the derivation, so one seed is one address
    # everywhere the canonical factory is deployed.
    assert predict(safe_chain, seed, chain_id=BASE) == predict(
        safe_chain,
        seed,
        chain_id=143,
    )


def test_salt_nonce_changes_the_address(safe_chain: dict[str, object]) -> None:
    a = SafeSeed(owners=(OWNER_A,), threshold=1, salt_nonce=0)
    b = SafeSeed(owners=(OWNER_A,), threshold=1, salt_nonce=1)

    assert predict(safe_chain, a) != predict(safe_chain, b)


def test_threshold_changes_the_address(safe_chain: dict[str, object]) -> None:
    a = SafeSeed(owners=(OWNER_A, OWNER_B), threshold=1)
    b = SafeSeed(owners=(OWNER_A, OWNER_B), threshold=2)

    assert predict(safe_chain, a) != predict(safe_chain, b)


def test_owner_order_changes_the_address(safe_chain: dict[str, object]) -> None:
    a = SafeSeed(owners=(OWNER_A, OWNER_B), threshold=1)
    b = SafeSeed(owners=(OWNER_B, OWNER_A), threshold=1)

    # Owner order feeds the setup calldata, so it feeds the address. This is a
    # trap: silently sorting owners anywhere upstream moves the Safe.
    assert predict(safe_chain, a) != predict(safe_chain, b)
    assert setup_calldata(a) != setup_calldata(b)


# --- chains where the guarantee does not hold -----------------------------


def test_zksync_era_is_rejected_with_no_bogus_address(
    safe_chain: dict[str, object],
) -> None:
    seed = SafeSeed(owners=(OWNER_A,), threshold=1)

    with pytest.raises(SafeFactoryMissing) as error:
        predict(safe_chain, seed, chain_id=ZKSYNC_ERA)

    assert "same-address guarantee does not hold" in str(error.value)


def test_chain_without_a_factory_is_rejected() -> None:
    bare = Web3(EthereumTesterProvider())  # no Safe contracts deployed
    seed = SafeSeed(owners=(OWNER_A,), threshold=1)

    with pytest.raises(SafeFactoryMissing) as error:
        predict_address(bare, seed, chain_id=BASE)

    assert "Safe proxy factory" in str(error.value)


def test_monad_supports_deterministic_safe() -> None:
    assert chains.supports_deterministic_safe(143)
    assert chains.chain_name(143) == "Monad"


def test_zksync_era_does_not_support_deterministic_safe() -> None:
    assert not chains.supports_deterministic_safe(ZKSYNC_ERA)


def test_unknown_chain_is_not_gated_by_the_capability_hint() -> None:
    assert chains.supports_deterministic_safe(999_999)


# --- seed validation ------------------------------------------------------


def test_threshold_above_owner_count_is_rejected() -> None:
    with pytest.raises(SafeDeploymentError):
        SafeSeed(owners=(OWNER_A,), threshold=2)


def test_zero_threshold_is_rejected() -> None:
    with pytest.raises(SafeDeploymentError):
        SafeSeed(owners=(OWNER_A, OWNER_B), threshold=0)


def test_empty_owners_is_rejected() -> None:
    with pytest.raises(SafeDeploymentError):
        SafeSeed(owners=(), threshold=1)


def test_duplicate_owners_are_rejected() -> None:
    with pytest.raises(SafeDeploymentError):
        SafeSeed(owners=(OWNER_A, OWNER_A), threshold=1)


def test_owners_are_checksummed() -> None:
    seed = SafeSeed(owners=(OWNER_C.lower(),), threshold=1)

    assert seed.owners == (OWNER_C,)
