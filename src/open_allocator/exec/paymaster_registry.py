from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Literal

PaymasterProviderName = Literal["pimlico", "circle"]

DEFAULT_PROVIDER: PaymasterProviderName = "pimlico"

# EntryPoint is a protocol-level singleton, identical on every chain.
ENTRY_POINT_V07 = "0x0000000071727De22E5E9d8BAf0edAc6f37da032"
ENTRY_POINT_V08 = "0x4337084D9E255Ff0702461CF8895CE9E3b5Ff108"

ENTRY_POINTS: Mapping[str, str] = {
    "v0.7": ENTRY_POINT_V07,
    "v0.8": ENTRY_POINT_V08,
}

# Pimlico's ERC-20 paymaster is a *different contract per EntryPoint version*,
# not one address with a version flag. Same address across chains, per Pimlico's
# contract-addresses reference — though they warn that may not hold forever,
# which is why pimlico_getTokenQuotes (which returns the live paymaster address)
# is authoritative at submission time and these are only a fallback.
ERC20_PAYMASTERS: Mapping[str, str] = {
    "v0.6": "0x6666666666667849c56F2850848ce1c4dA65C68B",
    "v0.7": "0x777777777777AeC03fd955926DbF81597e66834C",
    "v0.8": "0x888888888888Ec68A58AB8094Cc1AD20Ba3D2402",
}

# A Safe cannot submit v0.8 userOps: Safe4337Module pins its EntryPoint in an
# immutable constructor arg, and the latest release (v0.3.0, 2024-03) is pinned
# to v0.7 — verified on-chain on Base and Monad. Since the Safe is the account
# this phase is built around, v0.7 is the default rather than a per-chain
# exception. v0.8 stays reachable for a non-Safe smart account.
DEFAULT_ENTRY_POINT_VERSION = "v0.7"


@dataclass(frozen=True)
class PaymasterChain:
    chain_id: int
    provider: PaymasterProviderName = DEFAULT_PROVIDER
    entry_point_version: str = DEFAULT_ENTRY_POINT_VERSION
    bundler_url: str | None = None

    @property
    def entry_point(self) -> str:
        return ENTRY_POINTS[self.entry_point_version]

    @property
    def paymaster_address(self) -> str | None:
        return ERC20_PAYMASTERS.get(self.entry_point_version)


# Chains where an ERC-20 (USDC) paymaster is available. Seeded from the v3
# "Chain reality" notes; extend by adding a row.
PAYMASTER_CHAINS: Mapping[int, PaymasterChain] = {
    1: PaymasterChain(1),
    10: PaymasterChain(10),
    56: PaymasterChain(56),
    100: PaymasterChain(100),
    130: PaymasterChain(130),
    137: PaymasterChain(137),
    143: PaymasterChain(143),
    146: PaymasterChain(146),
    480: PaymasterChain(480),
    5000: PaymasterChain(5000),
    8453: PaymasterChain(8453),
    42161: PaymasterChain(42161),
    43114: PaymasterChain(43114),
    59144: PaymasterChain(59144),
    81457: PaymasterChain(81457),
    534352: PaymasterChain(534352),
}

# Circle's paymaster covers only these (v0.8). Kept as an option behind the
# provider seam; selecting it elsewhere is an error rather than a silent
# fallback to a chain it cannot serve.
CIRCLE_CHAINS: frozenset[int] = frozenset({1, 10, 130, 137, 8453, 42161, 43114})


# Pimlico serves bundler and paymaster from one per-chain endpoint, so the
# chain id is part of the URL and a single configured URL cannot cover a
# multi-chain deployment. Derive it from the API key instead.
#
PIMLICO_RPC_TEMPLATE = "https://api.pimlico.io/v2/{chain_id}/rpc"


def pimlico_rpc_url(chain_id: int, api_key: str) -> str:
    return f"{PIMLICO_RPC_TEMPLATE.format(chain_id=chain_id)}?apikey={api_key}"


class ChainNotGasPayable(RuntimeError):
    """No USDC paymaster for this chain — it stays scorable, just not gasless."""

    def __init__(self, chain_id: int, provider: str | None = None) -> None:
        self.chain_id = chain_id
        self.provider = provider
        detail = f" via {provider}" if provider else ""
        super().__init__(
            f"chain {chain_id} cannot pay gas in USDC{detail}; it remains "
            f"discoverable and scorable, but needs native gas or another chain"
        )


def paymaster_chain(
    chain_id: int,
    *,
    provider: PaymasterProviderName | None = None,
) -> PaymasterChain | None:
    """The paymaster row for a chain, or None if it is not gas-payable."""
    row = PAYMASTER_CHAINS.get(chain_id)
    if row is None:
        return None
    if provider is None or provider == row.provider:
        return row
    if provider == "circle" and chain_id not in CIRCLE_CHAINS:
        return None
    return replace(row, provider=provider)


def require_paymaster_chain(
    chain_id: int,
    *,
    provider: PaymasterProviderName | None = None,
) -> PaymasterChain:
    row = paymaster_chain(chain_id, provider=provider)
    if row is None:
        raise ChainNotGasPayable(chain_id, provider)
    return row


def is_gas_payable(
    chain_id: int,
    *,
    provider: PaymasterProviderName | None = None,
) -> bool:
    return paymaster_chain(chain_id, provider=provider) is not None


# There is deliberately no surcharge_bps() here. Pimlico bakes its fee into the
# exchangeRate returned by pimlico_getTokenQuotes, so the only honest cost is
# the live quote — a static table cannot be right and drifts silently when their
# pricing changes. This registry answers "can this chain pay gas in USDC", never
# "what does it cost"; see PimlicoPaymasterAdapter.token_quote().
#
# (The table this replaced charged 10% on Arbitrum and Base and 0% everywhere
# else, which understated cost on 14 of 16 chains. Pimlico's published ERC-20
# surcharge is a flat 10%, not an Arb/Base exception.)
