from __future__ import annotations

from dataclasses import dataclass

from eth_abi import encode as abi_encode
from eth_utils import keccak
from web3 import Web3

from open_allocator.exec import chains

# A Safe is deployed counterfactually: its address is derived from the seed
# (owners + threshold + salt) and is identical on every chain where the Safe
# Singleton Factory put the factory and singleton at the canonical addresses.
# Devs never open the Safe UI, and the address is known before the first tx.

SAFE_VERSION = "1.4.1"

# Canonical v1.4.1 deployments (same address on every chain reached by the Safe
# Singleton Factory). Overridable per chain because a wrong constant must be
# correctable without a release — and verify_deployment() checks that code
# actually exists at these addresses before any address is returned, so a bad
# constant fails loudly rather than yielding a plausible-looking dead address.
SAFE_SINGLETON_L2 = "0x29fcB43b46531BcA003ddC8FCB67FFE91900C762"
SAFE_PROXY_FACTORY = "0x4e1DCf7AD4e460CfD30791CCC4F9c8a4f820ec67"
SAFE_FALLBACK_HANDLER = "0xfd0732Dc9E303f09fCEf3a7388Ad10A83459Ec99"

# Safe4337Module v0.3.0 — canonical on 37 chains including Monad (143).
# It is *both* the fallback handler and an enabled module: the EntryPoint calls
# validateUserOp on the proxy, which falls back to here.
#
# Every Safe this module deploys is 4337-enabled, whether or not it ever submits
# a userOp, because enablement is baked into the address (see setup_calldata).
# The module subclasses CompatibilityFallbackHandler, so using it as the handler
# costs nothing: EIP-1271 isValidSignature and the ERC-721/1155 receiver hooks
# still work. An unused module is inert.
SAFE_4337_MODULE = "0x75cf11467937ce3F2f357CE24ffc3DBF8fD5c226"

# SafeModuleSetup v0.3.0. Enabling a module is a call the Safe must make to
# itself, but the Safe's address is not known until setup() has run — so setup()
# delegatecalls this library to do it from inside the constructor.
SAFE_MODULE_SETUP = "0x2dd68b007B46fBe91B9A7c3EDa5A7a1063cB5b47"

# Safe4337Module pins its EntryPoint in an immutable constructor arg, so the
# module *is* the EntryPoint choice. Verified on-chain (Base + Monad, 2026-07-15):
# SUPPORTED_ENTRYPOINT() == 0x0000000071727De22E5E9d8BAf0edAc6f37da032 (v0.7).
# There is no v0.8-compatible Safe4337Module release — v0.3.0 (2024-03) is the
# latest. A Safe cannot submit v0.8 userOps; do not "upgrade" this to v0.8.
SAFE_4337_ENTRY_POINT_VERSION = "v0.7"

NULL_ADDRESS = "0x" + "00" * 20

_SETUP_SELECTOR_ABI = (
    "setup(address[],uint256,address,bytes,address,address,uint256,address)"
)
_PROXY_CREATION_CODE_SELECTOR = keccak(text="proxyCreationCode()")[:4]
_ENABLE_MODULES_SELECTOR = keccak(text="enableModules(address[])")[:4]


class SafeDeploymentError(RuntimeError):
    pass


class SafeFactoryMissing(SafeDeploymentError):
    """The chain has no canonical Safe factory/singleton at the expected address.

    The same-address guarantee does not hold here (e.g. zkSync-Era-type chains,
    whose non-standard CREATE2 derivation produces different addresses).
    """

    def __init__(self, chain_id: int, address: str, what: str) -> None:
        self.chain_id = chain_id
        self.address = address
        super().__init__(
            f"no {what} at {address} on {chains.chain_name(chain_id)} "
            f"(chain {chain_id}); the deterministic same-address guarantee does "
            f"not hold there, so no Safe address can be predicted"
        )


@dataclass(frozen=True)
class Safe4337Wiring:
    """The Safe4337Module wiring that every Safe's address is derived from.

    One address in two roles: `module` is both the Safe's fallback handler (so
    the EntryPoint's validateUserOp call reaches it) and an enabled module (so
    it can drive the Safe). They are a pair rather than two knobs because a Safe
    where they disagree is not a working 4337 account.

    Overridable for the same reason the singleton and factory are: a wrong
    constant must be fixable without a release, and tests need a local chain.
    """

    module: str = SAFE_4337_MODULE
    module_setup: str = SAFE_MODULE_SETUP


SAFE_4337_WIRING = Safe4337Wiring()


@dataclass(frozen=True)
class SafeSeed:
    """Everything the Safe address is derived from."""

    owners: tuple[str, ...]
    threshold: int
    salt_nonce: int = 0

    def __post_init__(self) -> None:
        if not self.owners:
            raise SafeDeploymentError("SAFE_OWNERS must list at least one owner")
        if not 0 < self.threshold <= len(self.owners):
            raise SafeDeploymentError(
                f"SAFE_THRESHOLD must be between 1 and the owner count "
                f"({len(self.owners)}); got {self.threshold}"
            )
        checksummed = tuple(Web3.to_checksum_address(owner) for owner in self.owners)
        if len(set(checksummed)) != len(checksummed):
            raise SafeDeploymentError("SAFE_OWNERS contains duplicates")
        object.__setattr__(self, "owners", checksummed)


@dataclass(frozen=True)
class SafeDeployment:
    address: str
    chain_id: int
    deployed: bool


def enable_modules_calldata(modules: tuple[str, ...]) -> bytes:
    """SafeModuleSetup.enableModules(), delegatecalled from Safe.setup()."""
    return _ENABLE_MODULES_SELECTOR + abi_encode(
        ["address[]"],
        [[Web3.to_checksum_address(module) for module in modules]],
    )


def setup_calldata(
    seed: SafeSeed,
    *,
    wiring: Safe4337Wiring = SAFE_4337_WIRING,
) -> bytes:
    """The Safe.setup() call the proxy is initialised with.

    Every field here feeds the initializer, which is hashed into the CREATE2
    salt — so this calldata *is* the address. Two consequences:

    - Owner order is part of the address; reordering owners silently moves the
      Safe. Callers must not sort.
    - Enabling Safe4337Module moves the address too. Verified on live Base: the
      same seed yields 0xAcd1540B…ac without it and 0x888Db96e…AD with it. We
      enable it unconditionally so that one seed means one address regardless of
      how a transaction is later submitted — making enablement conditional on
      the submission axis would let flipping SIGNER_SUBMISSION move the Safe out
      from under funds already sent to it.
    """
    selector = keccak(text=_SETUP_SELECTOR_ABI)[:4]
    args = abi_encode(
        [
            "address[]",
            "uint256",
            "address",
            "bytes",
            "address",
            "address",
            "uint256",
            "address",
        ],
        [
            list(seed.owners),
            seed.threshold,
            Web3.to_checksum_address(wiring.module_setup),
            enable_modules_calldata((wiring.module,)),
            Web3.to_checksum_address(wiring.module),
            NULL_ADDRESS,
            0,
            NULL_ADDRESS,
        ],
    )
    return selector + args


def predict_address(
    w3: Web3,
    seed: SafeSeed,
    *,
    chain_id: int,
    singleton: str = SAFE_SINGLETON_L2,
    proxy_factory: str = SAFE_PROXY_FACTORY,
    wiring: Safe4337Wiring = SAFE_4337_WIRING,
) -> str:
    """The counterfactual Safe address for this seed on this chain.

    Reads proxyCreationCode() from the chain's factory rather than trusting a
    vendored constant: it is one cacheable read that doubles as the factory
    guard, and it cannot drift from the factory actually deployed there.
    """
    verify_deployment(
        w3,
        chain_id=chain_id,
        singleton=singleton,
        proxy_factory=proxy_factory,
    )

    creation_code = _proxy_creation_code(w3, proxy_factory, chain_id)
    initializer = setup_calldata(seed, wiring=wiring)
    salt = keccak(keccak(initializer) + abi_encode(["uint256"], [seed.salt_nonce]))
    deployment_data = creation_code + abi_encode(
        ["uint256"],
        [int(Web3.to_checksum_address(singleton), 16)],
    )
    return _create2_address(proxy_factory, salt, deployment_data)


def verify_deployment(
    w3: Web3,
    *,
    chain_id: int,
    singleton: str = SAFE_SINGLETON_L2,
    proxy_factory: str = SAFE_PROXY_FACTORY,
) -> None:
    """Fail loudly on chains where the same-address guarantee does not hold."""
    if not chains.supports_deterministic_safe(chain_id):
        raise SafeFactoryMissing(chain_id, proxy_factory, "deterministic Safe support")
    if not _has_code(w3, proxy_factory):
        raise SafeFactoryMissing(chain_id, proxy_factory, "Safe proxy factory")
    if not _has_code(w3, singleton):
        raise SafeFactoryMissing(chain_id, singleton, "Safe singleton")


def is_deployed(w3: Web3, address: str) -> bool:
    return _has_code(w3, address)


def deployment_status(
    w3: Web3,
    seed: SafeSeed,
    *,
    chain_id: int,
    singleton: str = SAFE_SINGLETON_L2,
    proxy_factory: str = SAFE_PROXY_FACTORY,
    wiring: Safe4337Wiring = SAFE_4337_WIRING,
) -> SafeDeployment:
    address = predict_address(
        w3,
        seed,
        chain_id=chain_id,
        singleton=singleton,
        proxy_factory=proxy_factory,
        wiring=wiring,
    )
    return SafeDeployment(
        address=address,
        chain_id=chain_id,
        deployed=is_deployed(w3, address),
    )


def deploy_transaction(
    w3: Web3,
    seed: SafeSeed,
    *,
    singleton: str = SAFE_SINGLETON_L2,
    proxy_factory: str = SAFE_PROXY_FACTORY,
    wiring: Safe4337Wiring = SAFE_4337_WIRING,
) -> dict[str, object]:
    """The createProxyWithNonce call that deploys this seed's Safe."""
    _ = w3
    factory, factory_data = deploy_factory_data(
        seed,
        singleton=singleton,
        proxy_factory=proxy_factory,
        wiring=wiring,
    )
    return {"to": factory, "data": factory_data, "value": 0}


def deploy_factory_data(
    seed: SafeSeed,
    *,
    singleton: str = SAFE_SINGLETON_L2,
    proxy_factory: str = SAFE_PROXY_FACTORY,
    wiring: Safe4337Wiring = SAFE_4337_WIRING,
) -> tuple[str, str]:
    """This seed's deployment as a userOp's (factory, factoryData) pair.

    EntryPoint v0.7+ takes the deployment as two unpacked fields rather than
    v0.6's single concatenated `initCode`, so a first userOp can carry the Safe
    deployment and pay for it in USDC. Same createProxyWithNonce call that
    deploy_transaction() sends over plain RPC, only split for the bundler.
    """
    initializer = setup_calldata(seed, wiring=wiring)
    selector = keccak(text="createProxyWithNonce(address,bytes,uint256)")[:4]
    data = selector + abi_encode(
        ["address", "bytes", "uint256"],
        [Web3.to_checksum_address(singleton), initializer, seed.salt_nonce],
    )
    return Web3.to_checksum_address(proxy_factory), "0x" + data.hex()


def _proxy_creation_code(w3: Web3, proxy_factory: str, chain_id: int) -> bytes:
    raw = w3.eth.call(
        {
            "to": Web3.to_checksum_address(proxy_factory),
            "data": "0x" + _PROXY_CREATION_CODE_SELECTOR.hex(),
        }
    )
    if not raw:
        raise SafeFactoryMissing(
            chain_id,
            proxy_factory,
            "readable proxyCreationCode()",
        )
    # Returns `bytes`: head is the offset, then length, then the payload.
    offset = int.from_bytes(raw[:32], "big")
    length = int.from_bytes(raw[offset : offset + 32], "big")
    start = offset + 32
    return bytes(raw[start : start + length])


def _create2_address(deployer: str, salt: bytes, deployment_data: bytes) -> str:
    raw = keccak(
        b"\xff"
        + bytes.fromhex(Web3.to_checksum_address(deployer)[2:])
        + salt
        + keccak(deployment_data)
    )[12:]
    return Web3.to_checksum_address("0x" + raw.hex())


def _has_code(w3: Web3, address: str) -> bool:
    return len(w3.eth.get_code(Web3.to_checksum_address(address))) > 0
