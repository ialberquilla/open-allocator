"""Live gas pricing for the cost model.

``core.costs`` is deterministic and does no I/O, so it cannot price gas itself —
it takes a :class:`~open_allocator.core.costs.GasPricing` as an input. This module
is the execution-plane half that reads one.

Two reads, both over the RPC endpoints already configured in
:mod:`open_allocator.exec.chains`:

- **Gas price per chain** — ``eth_gasPrice``. On OP-stack and Arbitrum chains the
  L1 data-availability component is charged separately by the chain, but it is
  negligible in practice (measured 2026-07-30 on a real Base ERC-4626 call: L1
  fee 5.5e-10 ETH against an L2 execution cost of 1.87e-6 ETH, i.e. ~0.03% of
  the total), so the L2 execution price is what the estimate needs.
- **ETH/USD** — a Chainlink aggregator's ``latestRoundData()``. Reading it
  on-chain means no price API and no new dependency.

🔑 **One quote does not serve every chain.** A gas price is denominated in the
chain's own native token, so an ETH/USD quote prices only the chains that pay in
ETH; used on a chain that pays in something else it is wrong by the price ratio
between the two tokens, which can be several orders of magnitude and looks
entirely ordinary in the output. So this module prices **only** the chains whose
gas token it holds a quote for (``chains.pays_gas_in_eth``); every other chain is
left out of the result and falls back to ``core.costs``' static constants with
``gas_priced_live: false``. Adding a price source for another token later is a
new entry in the price map, not a change to anything downstream.

Everything degrades to ``None`` rather than raising. A failed gas read must not
break ``build-allocation`` — the caller falls back to ``core.costs``' static
constants and the estimate reports ``gas_priced_live: false`` so nobody mistakes
a fallback for a measurement.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from eth_utils import keccak
from web3 import Web3

from open_allocator.core.costs import GasPricing
from open_allocator.exec import chains

# Chainlink ETH/USD aggregators, verified live 2026-07-30 (both returned
# $1,916 within a cent of each other and of an independent spot quote).
# Only one is needed per run; the first that answers wins.
ETH_USD_FEEDS: Mapping[int, str] = {
    1: "0x5f4eC3Df9cbd43714FE2740f5E3616155c5b8419",
    8453: "0x71041dddad3595F9CEd3DcCFBe3D1F4b0a16Bb70",
}

_LATEST_ROUND_DATA_SELECTOR = keccak(text="latestRoundData()")[:4]
_DECIMALS_SELECTOR = keccak(text="decimals()")[:4]

# A Chainlink answer this far from plausible is a misread feed, not a price.
_MIN_PLAUSIBLE_ETH_USD = 1.0
_MAX_PLAUSIBLE_ETH_USD = 1_000_000.0


def _web3(chain_id: int) -> Web3 | None:
    url = chains.rpc_url(chain_id)
    if not url:
        return None
    return Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 10}))


def gas_price_wei(chain_id: int) -> int | None:
    """Current gas price on ``chain_id``, or None if it cannot be read."""
    w3 = _web3(chain_id)
    if w3 is None:
        return None
    try:
        price = int(w3.eth.gas_price)
    except Exception:
        return None
    return price if price > 0 else None


def eth_usd(chain_ids: Iterable[int] = ()) -> float | None:
    """ETH/USD from the first Chainlink feed that answers.

    Tries the chains in play first so a run that already talks to Base does not
    need an Ethereum endpoint, then any remaining known feed.
    """
    preferred = [cid for cid in chain_ids if cid in ETH_USD_FEEDS]
    for chain_id in [*preferred, *(c for c in ETH_USD_FEEDS if c not in preferred)]:
        price = _read_feed(chain_id, ETH_USD_FEEDS[chain_id])
        if price is not None:
            return price
    return None


def _read_feed(chain_id: int, feed: str) -> float | None:
    w3 = _web3(chain_id)
    if w3 is None:
        return None
    address = Web3.to_checksum_address(feed)
    try:
        raw = w3.eth.call({"to": address, "data": _LATEST_ROUND_DATA_SELECTOR})
        # latestRoundData returns 5 words; the answer is the second (int256).
        if len(raw) < 64:
            return None
        answer = int.from_bytes(raw[32:64], "big", signed=True)
        raw_decimals = w3.eth.call({"to": address, "data": _DECIMALS_SELECTOR})
        decimals = int.from_bytes(raw_decimals, "big")
    except Exception:
        return None
    if answer <= 0 or decimals > 36:
        return None
    price = answer / 10**decimals
    if not _MIN_PLAUSIBLE_ETH_USD <= price <= _MAX_PLAUSIBLE_ETH_USD:
        return None
    return price


def live_pricing(chain_ids: Iterable[int]) -> GasPricing | None:
    """Read gas prices for the ETH-gas chains in ``chain_ids``, plus one quote.

    Chains that pay gas in their own token are skipped rather than priced with
    the ETH quote — see the module docstring. Returns ``None`` when no ETH/USD
    quote is available or no chain priced: a partial result is still useful
    (``GasPricing`` falls back per chain), but with no price quote at all
    nothing can be converted to USD.
    """
    wanted = list(dict.fromkeys(chain_ids))
    # Reading the feed is only worth doing if some chain here can use it.
    priceable = [chain_id for chain_id in wanted if chains.pays_gas_in_eth(chain_id)]
    if not priceable:
        return None
    price_usd = eth_usd(priceable)
    if price_usd is None:
        return None
    prices: dict[int, int] = {}
    for chain_id in priceable:
        wei = gas_price_wei(chain_id)
        if wei is not None:
            prices[chain_id] = wei
    if not prices:
        return None
    return GasPricing(
        gas_price_wei=prices,
        native_usd={chain_id: price_usd for chain_id in prices},
    )


__all__ = ["ETH_USD_FEEDS", "eth_usd", "gas_price_wei", "live_pricing"]
