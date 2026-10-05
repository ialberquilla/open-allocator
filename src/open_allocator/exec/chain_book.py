"""What a Safe held at a past block, read from chain alone.

The NAV history is rebuilt from these reads, so it does not depend on who sent
the transactions or whether anything was watching at the time. 1Tx has no
wallet-history endpoint and free RPC tiers serve no useful log ranges, so the
only inputs are state reads at a block: an archive RPC per chain answers them.

Valuation follows 1Tx's own ``/positions`` (darex ``positions.service``):

- ERC-4626 vaults: ``convertToAssets(shares)``; the shares are the base count.
- Aave and its forks: ``balanceOf`` is the underlying; ``scaledBalanceOf`` is
  the base count, because the balance itself grows with yield. A position
  with debt against it is a loop, and is not valued here.
- Compound V3: ``balanceOf``; the principal from ``userBasic`` is the base.
- Moonwell: ``balanceOfUnderlying``; the mToken balance is the base.
- Pendle PTs: ``getPtToAssetRate(market)`` on the router-static; the PT
  balance is the base.

A position that cannot be valued is reported with its reason, never as zero:
a zero reads as "worth nothing" and would take its value out of NAV without an
error. The chain's USDC is the numeraire and is 1 by definition; any other
underlying is priced from DeFiLlama's historical prices at the close.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from typing import Any, Protocol

import httpx
from dotenv import dotenv_values
from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode
from eth_utils import keccak
from web3 import Web3

from open_allocator.exec import chains
from open_allocator.exec.config import env_file_path

MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"

# Pendle's router-static per chain, as darex configures it.
PENDLE_ROUTER_STATIC: Mapping[int, str] = {
    42161: "0xAdB09F65bd90d19e3148D9ccb693F3161C6DB3E8",
    143: "0x6813d43782395A1F2AAb42f39aeEDE03ac655e09",
}

# DeFiLlama's chain slugs for its coins API.
LLAMA_CHAINS: Mapping[int, str] = {
    1: "ethereum",
    10: "optimism",
    56: "bsc",
    130: "unichain",
    137: "polygon",
    143: "monad",
    146: "sonic",
    480: "wc",
    8453: "base",
    42161: "arbitrum",
    43114: "avax",
    59144: "linea",
}
LLAMA_URL = "https://coins.llama.fi"

# How a protocol's positions are valued, by the protocol name 1Tx lists.
VALUATION: Mapping[str, str] = {
    "morpho": "erc4626",
    "euler": "erc4626",
    "fluid": "erc4626",
    "avantis": "erc4626",
    "tokemak": "erc4626",
    "aave": "aave",
    "neverland": "aave",
    "compound": "comet",
    "moonwell": "mtoken",
    "pendle": "pendle_pt",
}

_CHUNK = 200
_USD_QUANTUM = Decimal(1)


def _selector(signature: str) -> bytes:
    return keccak(text=signature)[:4]


_AGGREGATE3 = _selector("aggregate3((address,bool,bytes)[])")
_BALANCE_OF = _selector("balanceOf(address)")
_SCALED_BALANCE_OF = _selector("scaledBalanceOf(address)")
_CONVERT_TO_ASSETS = _selector("convertToAssets(uint256)")
_USER_BASIC = _selector("userBasic(address)")
_BALANCE_OF_UNDERLYING = _selector("balanceOfUnderlying(address)")
_PT_TO_ASSET_RATE = _selector("getPtToAssetRate(address)")
_POOL = _selector("POOL()")
_USER_ACCOUNT_DATA = _selector("getUserAccountData(address)")
_READ_TOKENS = _selector("readTokens()")
_ASSET_INFO = _selector("assetInfo()")


class ChainReadError(RuntimeError):
    """A read failed; nothing it would have returned is to be assumed."""


class ArchiveUnavailable(ChainReadError):
    """The RPC does not keep state that far back."""


_ARCHIVE_HINTS = (
    "historical state",
    "missing trie node",
    "header not found",
    "state is not available",
    "pruned",
    "not available",
    "block not found",
    "unknown block",
)


class Rpc(Protocol):
    def __call__(self, method: str, params: list[Any]) -> Any: ...


class HttpRpc:
    """JSON-RPC over HTTP, with a short backoff on rate limits."""

    def __init__(
        self,
        url: str,
        *,
        client: httpx.Client | None = None,
        retries: int = 4,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._url = url
        self._client = client or httpx.Client(timeout=30)
        self._retries = retries
        self._sleep = sleep

    def __call__(self, method: str, params: list[Any]) -> Any:
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        for attempt in range(self._retries + 1):
            try:
                response = self._client.post(self._url, json=body)
            except httpx.HTTPError as error:
                if attempt == self._retries:
                    raise ChainReadError(f"{method}: {type(error).__name__}") from None
                self._sleep(0.5 * 2**attempt)
                continue
            if response.status_code == 429 and attempt < self._retries:
                self._sleep(0.5 * 2**attempt)
                continue
            try:
                payload = response.json()
            except ValueError:
                raise ChainReadError(
                    f"{method}: HTTP {response.status_code}, not JSON"
                ) from None
            error = payload.get("error") if isinstance(payload, dict) else None
            if error:
                message = str(error.get("message", error))
                if "rate" in message.lower() and attempt < self._retries:
                    self._sleep(0.5 * 2**attempt)
                    continue
                if any(hint in message.lower() for hint in _ARCHIVE_HINTS):
                    raise ArchiveUnavailable(f"{method}: {message[:160]}")
                raise ChainReadError(f"{method}: {message[:160]}")
            return payload.get("result")
        raise ChainReadError(f"{method}: rate limited")


def rpc_url_for(chain_id: int) -> str | None:
    """``RPC_URL_<chain>`` from the environment or the .env file, else the
    public RPC. History needs an archive node, which a public RPC rarely is."""
    env = {
        **{k: v for k, v in dotenv_values(env_file_path()).items() if v},
        **os.environ,
    }
    return chains.rpc_url(chain_id, env)


def rpc_for(chain_id: int) -> Rpc | None:
    url = rpc_url_for(chain_id)
    return HttpRpc(url) if url else None


# Blocks and time.


@dataclass(frozen=True)
class Block:
    number: int
    timestamp: int


def get_block(rpc: Rpc, number: int | str) -> Block:
    tag = hex(number) if isinstance(number, int) else number
    block = rpc("eth_getBlockByNumber", [tag, False])
    if not block:
        raise ChainReadError(f"block {number} not found")
    return Block(int(block["number"], 16), int(block["timestamp"], 16))


def block_at(rpc: Rpc, timestamp: int, *, latest: Block | None = None) -> Block:
    """The last block at or before ``timestamp``.

    Interpolation on the block times, falling back to halving when it stalls,
    so it takes a handful of reads rather than one per bit of block height.
    """
    hi = latest or get_block(rpc, "latest")
    if hi.timestamp <= timestamp:
        return hi
    lo = get_block(rpc, 0)
    if lo.timestamp > timestamp:
        raise ChainReadError(f"chain did not exist at {timestamp}")
    halve = False
    while hi.number - lo.number > 1:
        if halve or hi.timestamp == lo.timestamp:
            guess = (lo.number + hi.number) // 2
        else:
            span = hi.number - lo.number
            guess = lo.number + (timestamp - lo.timestamp) * span // (
                hi.timestamp - lo.timestamp
            )
        guess = min(max(guess, lo.number + 1), hi.number - 1)
        block = get_block(rpc, guess)
        before = hi.number - lo.number
        if block.timestamp <= timestamp:
            lo = block
        else:
            hi = block
        # Interpolation that barely narrowed the range is followed by a halving.
        halve = (hi.number - lo.number) * 4 > before * 3
    return lo


def day_close_timestamp(day: date) -> int:
    """The last second of a UTC day."""
    end = datetime(day.year, day.month, day.day, tzinfo=UTC) + timedelta(days=1)
    return int(end.timestamp()) - 1


def deployment_block(
    rpc: Rpc, address: str, *, latest: Block | None = None
) -> int | None:
    """The first block at which ``address`` has code, or None if it has none.

    Needs archive state: on a pruned RPC the search fails rather than guessing.
    """
    hi = (latest or get_block(rpc, "latest")).number
    if _code(rpc, address, hi) == "0x":
        return None
    lo = 0
    while hi - lo > 1:
        middle = (lo + hi) // 2
        if _code(rpc, address, middle) == "0x":
            lo = middle
        else:
            hi = middle
    return hi


def _code(rpc: Rpc, address: str, block: int) -> str:
    return str(rpc("eth_getCode", [address, hex(block)]) or "0x")


# Multicall.


@dataclass(frozen=True)
class Call:
    target: str
    data: bytes


def multicall(rpc: Rpc, calls: Sequence[Call], block: int) -> list[bytes | None]:
    """Every call at ``block``; None where a call reverted."""
    results: list[bytes | None] = []
    for start in range(0, len(calls), _CHUNK):
        chunk = calls[start : start + _CHUNK]
        payload = _AGGREGATE3 + abi_encode(
            ["(address,bool,bytes)[]"],
            [[(Web3.to_checksum_address(c.target), True, c.data) for c in chunk]],
        )
        raw = rpc(
            "eth_call",
            [{"to": MULTICALL3, "data": "0x" + payload.hex()}, hex(block)],
        )
        if not isinstance(raw, str) or raw == "0x":
            raise ChainReadError(f"multicall returned nothing at block {block}")
        (decoded,) = abi_decode(["(bool,bytes)[]"], bytes.fromhex(raw[2:]))
        results.extend(data if ok else None for ok, data in decoded)
    return results


def _address_arg(address: str) -> bytes:
    return abi_encode(["address"], [Web3.to_checksum_address(address)])


def _uint(data: bytes | None) -> int | None:
    if data is None or len(data) < 32:
        return None
    return int.from_bytes(data[:32], "big")


# Instruments and closes.


@dataclass(frozen=True)
class HeldInstrument:
    """What a close needs to know about one instrument."""

    instrument_id: str
    chain_id: int
    protocol: str
    symbol: str
    yield_token: str
    underlying_token: str | None
    underlying_decimals: int | None
    protocol_address: str | None = None


@dataclass(frozen=True)
class InstrumentClose:
    instrument_id: str
    chain_id: int
    protocol: str
    symbol: str
    shares_raw: int
    base_shares_raw: int | None
    underlying_raw: int | None
    decimals: int | None
    price_usd: Decimal | None
    price_source: str | None
    usd_micro: int | None
    # Why the position could not be valued; None when it was.
    reason: str | None = None


@dataclass(frozen=True)
class ChainClose:
    chain_id: int
    block: int
    block_timestamp: int
    positions: tuple[InstrumentClose, ...] = ()
    # Why the close is not complete; None when every position was valued.
    reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.reason is None


class Prices(Protocol):
    def __call__(
        self, chain_id: int, tokens: Sequence[str], timestamp: int
    ) -> Mapping[str, Decimal]: ...


@dataclass
class LlamaPrices:
    """DeFiLlama's historical prices; a token it cannot price is left out."""

    client: httpx.Client = field(default_factory=lambda: httpx.Client(timeout=30))

    def __call__(
        self, chain_id: int, tokens: Sequence[str], timestamp: int
    ) -> Mapping[str, Decimal]:
        slug = LLAMA_CHAINS.get(chain_id)
        if slug is None or not tokens:
            return {}
        coins = ",".join(f"{slug}:{token.lower()}" for token in tokens)
        try:
            response = self.client.get(
                f"{LLAMA_URL}/prices/historical/{timestamp}/{coins}",
                params={"searchWidth": "6h"},
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            return {}
        prices: dict[str, Decimal] = {}
        for key, coin in (payload.get("coins") or {}).items():
            price = coin.get("price")
            if isinstance(price, int | float) and price > 0:
                prices[key.split(":", 1)[1].lower()] = Decimal(str(price))
        return prices


def valuation_of(protocol: str) -> str | None:
    lowered = protocol.lower()
    for prefix, method in VALUATION.items():
        if lowered.startswith(prefix):
            return method
    return None


def read_chain_close(
    rpc: Rpc,
    *,
    chain_id: int,
    block: Block,
    account: str,
    instruments: Iterable[HeldInstrument],
    prices: Prices,
) -> ChainClose:
    """The account's positions on one chain at ``block``, each valued in USD.

    Three multicalls: every listed yield token's balance; then, for the held
    ones, what they are worth in their asset; then the facts that depend on
    those answers (an Aave pool's debt, a Pendle SY's accounting asset).
    """
    listed = [i for i in instruments if i.chain_id == chain_id]
    owner = _address_arg(account)
    balances = multicall(
        rpc, [Call(i.yield_token, _BALANCE_OF + owner) for i in listed], block.number
    )
    held = [
        _Held(instrument, balance, valuation_of(instrument.protocol))
        for instrument, balance in zip(listed, map(_uint, balances), strict=True)
        if balance
    ]

    second = _Batch()
    for item in held:
        token = item.instrument.yield_token
        if item.method == "erc4626":
            item.ask(second, "assets", token, _CONVERT_TO_ASSETS + _u256(item.balance))
        elif item.method == "aave":
            item.ask(second, "base", token, _SCALED_BALANCE_OF + owner)
            item.ask(second, "pool", token, _POOL)
        elif item.method == "comet":
            item.ask(second, "basic", token, _USER_BASIC + owner)
        elif item.method == "mtoken":
            item.ask(second, "assets", token, _BALANCE_OF_UNDERLYING + owner)
        elif item.method == "pendle_pt":
            router = PENDLE_ROUTER_STATIC.get(chain_id)
            market = item.instrument.protocol_address
            if router and market:
                item.ask(
                    second, "rate", router, _PT_TO_ASSET_RATE + _address_arg(market)
                )
                item.ask(second, "tokens", market, _READ_TOKENS)
            else:
                item.method = None
    second.run(rpc, block.number)

    third = _Batch()
    for item in held:
        if item.method == "aave" and (pool := _address(item.answer(second, "pool"))):
            item.ask(third, "account", pool, _USER_ACCOUNT_DATA + owner)
        elif item.method == "pendle_pt" and (
            sy := _address(item.answer(second, "tokens"))
        ):
            item.ask(third, "asset", sy, _ASSET_INFO)
    third.run(rpc, block.number)

    for item in held:
        _value(item, second, third)

    usdc = (chains.usdc_address(chain_id) or "").lower()
    to_price = sorted({i.asset for i in held if i.asset and i.asset != usdc})
    quoted = prices(chain_id, to_price, block.timestamp) if to_price else {}

    positions = tuple(_close(item, chain_id, usdc, quoted) for item in held)
    failed = [p for p in positions if p.reason]
    return ChainClose(
        chain_id=chain_id,
        block=block.number,
        block_timestamp=block.timestamp,
        positions=positions,
        reason="; ".join(f"{p.symbol}: {p.reason}" for p in failed) or None,
    )


class _Batch:
    """Calls gathered for one multicall, answered by index."""

    def __init__(self) -> None:
        self.calls: list[Call] = []
        self.answers: list[bytes | None] = []

    def add(self, target: str, data: bytes) -> int:
        self.calls.append(Call(target, data))
        return len(self.calls) - 1

    def run(self, rpc: Rpc, block: int) -> None:
        self.answers = multicall(rpc, self.calls, block) if self.calls else []


@dataclass
class _Held:
    instrument: HeldInstrument
    balance: int
    method: str | None
    asks: dict[tuple[int, str], int] = field(default_factory=dict)
    underlying: int | None = None
    base: int | None = None
    asset: str | None = None
    decimals: int | None = None
    reason: str | None = None

    def ask(self, batch: _Batch, name: str, target: str, data: bytes) -> None:
        self.asks[(id(batch), name)] = batch.add(target, data)

    def answer(self, batch: _Batch, name: str) -> bytes | None:
        index = self.asks.get((id(batch), name))
        return None if index is None else batch.answers[index]


def _value(item: _Held, second: _Batch, third: _Batch) -> None:
    """The position in its asset's raw units, its base count, and that asset."""
    instrument = item.instrument
    item.asset = (instrument.underlying_token or "").lower() or None
    item.decimals = instrument.underlying_decimals
    method = item.method
    if method is None:
        item.reason = f"no historical valuation for {instrument.protocol}"
    elif method == "erc4626":
        item.underlying = _uint(item.answer(second, "assets"))
        item.base = item.balance
    elif method == "aave":
        if _word(item.answer(third, "account"), 1):
            item.reason = "levered: the debt against it is not valued historically"
        item.underlying = item.balance
        item.base = _uint(item.answer(second, "base"))
    elif method == "comet":
        principal = _int_word(item.answer(second, "basic"), 0)
        item.underlying = item.balance
        item.base = principal if principal and principal > 0 else None
    elif method == "mtoken":
        item.underlying = _uint(item.answer(second, "assets"))
        item.base = item.balance
    elif method == "pendle_pt":
        # A PT redeems to its SY's accounting asset, so that is what is priced.
        rate = _uint(item.answer(second, "rate"))
        info = item.answer(third, "asset")
        item.underlying = item.balance * rate // 10**18 if rate is not None else None
        item.base = item.balance
        item.asset = _address(info[32:64] if info else None)
        item.decimals = _word(info, 2)
    if item.reason is None and item.underlying is None:
        item.reason = f"{instrument.protocol} valuation call reverted"
    if item.reason is None and (item.asset is None or item.decimals is None):
        item.reason = f"{instrument.symbol}: asset or decimals unknown"


def _close(
    item: _Held, chain_id: int, usdc: str, quoted: Mapping[str, Decimal]
) -> InstrumentClose:
    price: Decimal | None = None
    source: str | None = None
    if item.asset and item.asset == usdc:
        price, source = Decimal(1), "numeraire"
    elif item.asset and item.asset in quoted:
        price, source = quoted[item.asset], "defillama"
    reason = item.reason
    if reason is None and price is None:
        reason = f"no price for {item.instrument.symbol} at the close"
    usd: int | None = None
    if reason is None and price is not None:
        assert item.underlying is not None and item.decimals is not None
        value = Decimal(item.underlying) * price * 10**6 / Decimal(10) ** item.decimals
        usd = int(value.quantize(_USD_QUANTUM, rounding=ROUND_DOWN))
    return InstrumentClose(
        instrument_id=item.instrument.instrument_id,
        chain_id=chain_id,
        protocol=item.instrument.protocol,
        symbol=item.instrument.symbol,
        shares_raw=item.balance,
        base_shares_raw=item.base,
        underlying_raw=item.underlying,
        decimals=item.decimals,
        price_usd=price,
        price_source=source,
        usd_micro=usd,
        reason=reason,
    )


def _u256(value: int) -> bytes:
    return abi_encode(["uint256"], [value])


def _word(data: bytes | None, index: int) -> int | None:
    if data is None or len(data) < 32 * (index + 1):
        return None
    return int.from_bytes(data[32 * index : 32 * (index + 1)], "big")


def _int_word(data: bytes | None, index: int) -> int | None:
    if data is None or len(data) < 32 * (index + 1):
        return None
    return int.from_bytes(data[32 * index : 32 * (index + 1)], "big", signed=True)


def _address(data: bytes | None) -> str | None:
    word = _word(data, 0)
    if word is None or word == 0:
        return None
    return "0x" + word.to_bytes(32, "big")[12:].hex()
