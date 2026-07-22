from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

RPC_ENV_PREFIX = "RPC_URL_"
USDC_ENV_PREFIX = "PAYMASTER_USDC_ADDRESS_"


@dataclass(frozen=True)
class ChainInfo:
    name: str
    rpc_url: str | None
    # Whether the Safe Singleton Factory reaches this chain, so a Safe derived
    # from one seed lands on the same address here as everywhere else. False for
    # non-standard-CREATE chains (zkSync-Era-type), where the guarantee breaks.
    deterministic_safe: bool = True


# Static chain data is display/default-RPC configuration only. It must never be
# used to decide which chains are discoverable or scorable from 1Tx responses.
DEFAULT_CHAINS: Mapping[int, ChainInfo] = {
    1: ChainInfo("Ethereum", "https://ethereum-rpc.publicnode.com"),
    10: ChainInfo("OP Mainnet", "https://optimism-rpc.publicnode.com"),
    56: ChainInfo("BNB Smart Chain", "https://bsc-rpc.publicnode.com"),
    100: ChainInfo("Gnosis", "https://gnosis-rpc.publicnode.com"),
    130: ChainInfo("Unichain", "https://unichain-rpc.publicnode.com"),
    137: ChainInfo("Polygon", "https://polygon-bor-rpc.publicnode.com"),
    143: ChainInfo("Monad", "https://rpc.monad.xyz"),
    146: ChainInfo("Sonic", "https://sonic-rpc.publicnode.com"),
    250: ChainInfo("Fantom", "https://fantom-rpc.publicnode.com"),
    # zkSync Era derives contract addresses differently, so a Safe from the same
    # seed lands elsewhere here. Scorable and depositable, just not same-address.
    324: ChainInfo(
        "zkSync Era",
        "https://zksync-era-rpc.publicnode.com",
        deterministic_safe=False,
    ),
    480: ChainInfo("World Chain", "https://worldchain.drpc.org"),
    1101: ChainInfo("Polygon zkEVM", "https://polygon-zkevm-rpc.publicnode.com"),
    1868: ChainInfo("Soneium", "https://soneium.drpc.org"),
    5000: ChainInfo("Mantle", "https://mantle-rpc.publicnode.com"),
    8453: ChainInfo("Base", "https://mainnet.base.org"),
    34443: ChainInfo("Mode", "https://mode-rpc.publicnode.com"),
    42161: ChainInfo("Arbitrum One", "https://arbitrum-one-rpc.publicnode.com"),
    42220: ChainInfo("Celo", "https://celo-rpc.publicnode.com"),
    43114: ChainInfo("Avalanche C-Chain", "https://avalanche-c-chain-rpc.publicnode.com"),
    57073: ChainInfo("Ink", "https://ink.drpc.org"),
    59144: ChainInfo("Linea", "https://linea-rpc.publicnode.com"),
    80094: ChainInfo("Berachain", "https://berachain-rpc.publicnode.com"),
    81457: ChainInfo("Blast", "https://blast-rpc.publicnode.com"),
    534352: ChainInfo("Scroll", "https://scroll-rpc.publicnode.com"),
}

DEFAULT_RPC_URLS: Mapping[int, str] = {
    chain_id: info.rpc_url
    for chain_id, info in DEFAULT_CHAINS.items()
    if info.rpc_url is not None
}

# USDC is a different contract on every chain, so the paymaster's gas token is a
# registry, not one setting. Every row was read off-chain via symbol()/decimals().
# Two are not the plain 6-decimal Circle token: 56 is Binance-Peg USDC with 18
# decimals, 100 is the bridged USDC.e. Blast has no canonical USDC, so gas there
# needs an explicit PAYMASTER_USDC_ADDRESS_81457.
USDC_ADDRESSES: Mapping[int, str] = {
    1: "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
    10: "0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85",
    56: "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d",
    100: "0x2a22f9c3b484c3629090FeED35F17Ff8F88f76F0",
    130: "0x078D782b760474a361dDA0AF3839290b0EF57AD6",
    137: "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",
    143: "0x754704Bc059F8C67012fEd69BC8A327a5aafb603",
    146: "0x29219dd400f2Bf60E5a23d13Be72B486D4038894",
    480: "0x79A02482A880bCE3F13e09Da970dC34db4CD24d1",
    5000: "0x09Bc4E0D864854c6aFB6eB9A9cdF58aC190D0dF9",
    8453: "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    42161: "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
    43114: "0xB97EF9Ef8734C71904D8002F8b6Bc66Dd9c48a6E",
    59144: "0x176211869cA2b568f2A7D4EE941E073a821EE1ff",
    534352: "0x06eFdBFf2a14a7c8E15944D1F4A48F9F95F663A4",
}


class MissingRPCError(RuntimeError):
    def __init__(self, chain_id: int) -> None:
        self.chain_id = chain_id
        super().__init__(f"no RPC for chain {chain_id}")


def rpc_url(chain_id: int, config: object | None = None) -> str | None:
    override = _rpc_override(chain_id, config)
    if override is not None:
        return override
    return DEFAULT_RPC_URLS.get(chain_id)


def require_rpc_url(chain_id: int, config: object | None = None) -> str:
    url = rpc_url(chain_id, config)
    if url is None:
        raise MissingRPCError(chain_id)
    return url


def usdc_address(chain_id: int, config: object | None = None) -> str | None:
    """The USDC to pay gas in on this chain, or None if we do not know one.

    Never guessed: a wrong gas token approves the wrong contract.
    """
    override = _usdc_override(chain_id, config)
    if override is not None:
        return override
    return USDC_ADDRESSES.get(chain_id)


def chain_name(chain_id: int) -> str:
    info = DEFAULT_CHAINS.get(chain_id)
    if info is not None:
        return info.name
    return f"chain {chain_id}"


def supports_deterministic_safe(chain_id: int) -> bool:
    """Whether a same-address Safe can be derived for this chain.

    Unknown chains are given the benefit of the doubt: this is a capability
    hint, never a universe gate, and the authoritative check is whether the
    factory actually has code on the chain (see safe_deployment).
    """
    info = DEFAULT_CHAINS.get(chain_id)
    if info is None:
        return True
    return info.deterministic_safe


def rpc_overrides_from_env(env: Mapping[str, str]) -> dict[int, str]:
    return _chain_keyed_env(env, RPC_ENV_PREFIX)


def usdc_overrides_from_env(env: Mapping[str, str]) -> dict[int, str]:
    return _chain_keyed_env(env, USDC_ENV_PREFIX)


def _chain_keyed_env(env: Mapping[str, str], prefix: str) -> dict[int, str]:
    overrides: dict[int, str] = {}
    for name, value in env.items():
        if not name.startswith(prefix):
            continue

        raw_chain_id = name.removeprefix(prefix)
        if not raw_chain_id.isdecimal():
            continue

        stripped = value.strip()
        if stripped:
            overrides[int(raw_chain_id)] = stripped

    return overrides


def _rpc_override(chain_id: int, config: object | None) -> str | None:
    return _chain_override(
        chain_id,
        config,
        prefix=RPC_ENV_PREFIX,
        attribute="_rpc_overrides",
        # Legacy: a bare chain id is only accepted for RPCs. Accepting it for
        # both would let one dict answer either lookup with the same value.
        mapping_keys=(chain_id, str(chain_id)),
    )


def _usdc_override(chain_id: int, config: object | None) -> str | None:
    return _chain_override(
        chain_id,
        config,
        prefix=USDC_ENV_PREFIX,
        attribute="_usdc_overrides",
    )


def _chain_override(
    chain_id: int,
    config: object | None,
    *,
    prefix: str,
    attribute: str,
    mapping_keys: tuple[object, ...] = (),
) -> str | None:
    if config is None:
        return _chain_keyed_env(os.environ, prefix).get(chain_id)

    if isinstance(config, Mapping):
        for key in (f"{prefix}{chain_id}", *mapping_keys):
            value = config.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    overrides = getattr(config, attribute, None)
    if isinstance(overrides, Mapping):
        value = overrides.get(chain_id)
        if isinstance(value, str) and value.strip():
            return value.strip()

    return None
