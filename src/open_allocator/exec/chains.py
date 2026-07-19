from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

RPC_ENV_PREFIX = "RPC_URL_"


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
    overrides: dict[int, str] = {}
    for name, value in env.items():
        if not name.startswith(RPC_ENV_PREFIX):
            continue

        raw_chain_id = name.removeprefix(RPC_ENV_PREFIX)
        if not raw_chain_id.isdecimal():
            continue

        url = value.strip()
        if url:
            overrides[int(raw_chain_id)] = url

    return overrides


def _rpc_override(chain_id: int, config: object | None) -> str | None:
    if config is None:
        return rpc_overrides_from_env(os.environ).get(chain_id)

    if isinstance(config, Mapping):
        return _rpc_override_from_mapping(chain_id, config)

    overrides = getattr(config, "_rpc_overrides", None)
    if isinstance(overrides, Mapping):
        value = overrides.get(chain_id)
        if isinstance(value, str) and value.strip():
            return value.strip()

    return None


def _rpc_override_from_mapping(
    chain_id: int,
    config: Mapping[object, object],
) -> str | None:
    for key in (f"{RPC_ENV_PREFIX}{chain_id}", chain_id, str(chain_id)):
        value = config.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None
