"""Reading a book at a past block, against a fake chain."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode
from eth_utils import keccak

from open_allocator.exec import chain_book
from open_allocator.exec.chain_book import (
    ArchiveUnavailable,
    Block,
    ChainReadError,
    HeldInstrument,
    HttpRpc,
)

SAFE = "0x" + "5a" * 20
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
AUSD = "0x" + "a5" * 20
USDAI = "0x" + "d0" * 20


def selector(signature: str) -> bytes:
    return keccak(text=signature)[:4]


def word(*values: int) -> bytes:
    return b"".join(v.to_bytes(32, "big", signed=v < 0) for v in values)


def address_word(address: str) -> bytes:
    return bytes(12) + bytes.fromhex(address[2:])


Handler = Callable[[bytes, int], bytes | None]


class FakeChain:
    """Blocks one second apart from ``genesis``, and contracts answering by
    (address, selector) at a block. A handler returning None reverts."""

    def __init__(
        self, head: int, *, genesis: int = 1_000, archive_from: int = 0
    ) -> None:
        self.head = head
        self.genesis = genesis
        self.archive_from = archive_from
        self.contracts: dict[tuple[str, bytes], Handler] = {}
        self.code_from: dict[str, int] = {}
        self.calls: list[str] = []

    def on(self, address: str, signature: str, handler: Handler) -> None:
        self.contracts[(address.lower(), selector(signature))] = handler

    def __call__(self, method: str, params: list[Any]) -> Any:
        self.calls.append(method)
        if method == "eth_getBlockByNumber":
            tag = params[0]
            number = self.head if tag == "latest" else int(tag, 16)
            if number > self.head:
                return None
            return {"number": hex(number), "timestamp": hex(self.genesis + number)}
        block = int(params[1], 16)
        if block < self.archive_from:
            raise ArchiveUnavailable(f"{method}: historical state not available")
        if method == "eth_getCode":
            deployed = self.code_from.get(params[0].lower())
            return "0x6080" if deployed is not None and block >= deployed else "0x"
        if method == "eth_call":
            data = bytes.fromhex(params[0]["data"][2:])
            assert params[0]["to"] == chain_book.MULTICALL3
            assert data[:4] == selector("aggregate3((address,bool,bytes)[])")
            (calls,) = abi_decode(["(address,bool,bytes)[]"], data[4:])
            results = []
            for target, _allow, calldata in calls:
                handler = self.contracts.get((target.lower(), calldata[:4]))
                answer = handler(calldata[4:], block) if handler else None
                results.append((answer is not None, answer or b""))
            return "0x" + abi_encode(["(bool,bytes)[]"], [results]).hex()
        raise AssertionError(method)


def instrument(
    protocol: str,
    token: str,
    *,
    chain_id: int = 8453,
    underlying: str = USDC_BASE,
    decimals: int = 6,
    market: str | None = None,
    symbol: str = "USDC",
) -> HeldInstrument:
    return HeldInstrument(
        instrument_id="0x" + token[2:].rjust(64, "0"),
        chain_id=chain_id,
        protocol=protocol,
        symbol=symbol,
        yield_token=token,
        underlying_token=underlying,
        underlying_decimals=decimals,
        protocol_address=market,
    )


def no_prices(
    chain_id: int, tokens: Sequence[str], timestamp: int
) -> Mapping[str, Decimal]:
    raise AssertionError(f"nothing to price, asked for {tokens}")


def balance(value: int) -> Handler:
    return lambda _args, _block: word(value)


def read(
    chain: FakeChain, instruments: list[HeldInstrument], prices=no_prices, block=100
):
    return chain_book.read_chain_close(
        chain,
        chain_id=instruments[0].chain_id if instruments else 8453,
        block=Block(block, 1_000 + block),
        account=SAFE,
        instruments=instruments,
        prices=prices,
    )


def test_an_erc4626_vault_is_its_assets_and_its_shares_are_the_base() -> None:
    chain = FakeChain(head=200)
    vault = "0x" + "11" * 20
    chain.on(vault, "balanceOf(address)", balance(9_816_138))
    chain.on(
        vault,
        "convertToAssets(uint256)",
        lambda args, _b: word(abi_decode(["uint256"], args)[0] * 13 // 10),
    )
    close = read(chain, [instrument("Morpho", vault)])
    assert close.ok
    (position,) = close.positions
    assert position.base_shares_raw == 9_816_138
    assert position.underlying_raw == 12_760_979
    assert (position.price_usd, position.price_source) == (Decimal(1), "numeraire")
    assert position.usd_micro == 12_760_979


def test_positions_not_held_are_not_reported() -> None:
    chain = FakeChain(head=200)
    vault = "0x" + "11" * 20
    chain.on(vault, "balanceOf(address)", balance(0))
    assert read(chain, [instrument("Morpho", vault)]).positions == ()


def test_aave_uses_the_scaled_balance_as_base_and_flags_debt_as_a_loop() -> None:
    chain = FakeChain(head=200)
    atoken, pool = "0x" + "22" * 20, "0x" + "99" * 20
    chain.on(atoken, "balanceOf(address)", balance(5_000_000))
    chain.on(atoken, "scaledBalanceOf(address)", balance(4_800_000))
    chain.on(atoken, "POOL()", lambda _a, _b: address_word(pool))
    debt = {"value": 0}
    chain.on(
        pool,
        "getUserAccountData(address)",
        lambda _a, _b: word(10**8, debt["value"], 0, 0, 0, 0),
    )
    close = read(chain, [instrument("Aave", atoken)])
    assert close.ok
    assert close.positions[0].base_shares_raw == 4_800_000
    assert close.positions[0].usd_micro == 5_000_000

    debt["value"] = 1
    close = read(chain, [instrument("Aave", atoken)])
    assert not close.ok
    assert close.positions[0].usd_micro is None
    assert "levered" in (close.positions[0].reason or "")


def test_comet_principal_and_moonwell_underlying() -> None:
    chain = FakeChain(head=200)
    comet, mtoken = "0x" + "33" * 20, "0x" + "44" * 20
    chain.on(comet, "balanceOf(address)", balance(2_000_000))
    chain.on(comet, "userBasic(address)", lambda _a, _b: word(1_900_000, 0, 0, 0, 0))
    chain.on(mtoken, "balanceOf(address)", balance(50_000_000_000))
    chain.on(mtoken, "balanceOfUnderlying(address)", balance(1_010_000))
    close = read(
        chain, [instrument("Compound V3", comet), instrument("Moonwell", mtoken)]
    )
    assert close.ok
    by_protocol = {p.protocol: p for p in close.positions}
    assert by_protocol["Compound V3"].base_shares_raw == 1_900_000
    assert by_protocol["Compound V3"].usd_micro == 2_000_000
    assert by_protocol["Moonwell"].base_shares_raw == 50_000_000_000
    assert by_protocol["Moonwell"].usd_micro == 1_010_000


def test_a_pendle_pt_is_priced_in_its_sy_asset_not_the_listed_token() -> None:
    chain = FakeChain(head=200)
    pt, market, sy = "0x" + "55" * 20, "0x" + "66" * 20, "0x" + "77" * 20
    router = chain_book.PENDLE_ROUTER_STATIC[42161]
    chain.on(pt, "balanceOf(address)", balance(10 * 10**18))
    chain.on(router, "getPtToAssetRate(address)", balance(96 * 10**16))
    chain.on(
        market, "readTokens()", lambda _a, _b: address_word(sy) + address_word(pt) * 2
    )
    chain.on(sy, "assetInfo()", lambda _a, _b: word(0) + address_word(USDAI) + word(18))
    asked: list[str] = []

    def prices(
        chain_id: int, tokens: Sequence[str], timestamp: int
    ) -> Mapping[str, Decimal]:
        asked.extend(tokens)
        return {USDAI: Decimal("0.999")}

    pendle = instrument(
        "Pendle",
        pt,
        chain_id=42161,
        underlying="0x" + "ee" * 20,  # sUSDai: not what a PT redeems to
        decimals=18,
        market=market,
        symbol="sUSDai",
    )
    close = read(chain, [pendle], prices=prices)
    assert close.ok, close.reason
    assert asked == [USDAI]
    (position,) = close.positions
    assert position.base_shares_raw == 10 * 10**18
    assert position.underlying_raw == 96 * 10**17
    assert position.usd_micro == 9_590_400


def test_a_missing_price_is_unknown_never_zero() -> None:
    chain = FakeChain(head=200)
    vault = "0x" + "11" * 20
    chain.on(vault, "balanceOf(address)", balance(1_000_000))
    chain.on(vault, "convertToAssets(uint256)", balance(1_000_000))
    close = read(
        chain,
        [instrument("Euler", vault, underlying=AUSD, symbol="AUSD")],
        prices=lambda *_: {},
    )
    assert not close.ok
    assert close.positions[0].usd_micro is None
    assert "no price for AUSD" in (close.reason or "")


def test_another_tokens_price_is_applied() -> None:
    chain = FakeChain(head=200)
    vault = "0x" + "11" * 20
    chain.on(vault, "balanceOf(address)", balance(1_000_000))
    chain.on(vault, "convertToAssets(uint256)", balance(2_000_000))
    close = read(
        chain,
        [instrument("Euler", vault, underlying=AUSD, symbol="AUSD")],
        prices=lambda _c, _t, _ts: {AUSD: Decimal("0.9995")},
    )
    assert close.positions[0].usd_micro == 1_999_000


def test_an_unknown_protocol_or_a_reverted_call_is_unknown() -> None:
    chain = FakeChain(head=200)
    odd, vault = "0x" + "88" * 20, "0x" + "11" * 20
    chain.on(odd, "balanceOf(address)", balance(1))
    chain.on(vault, "balanceOf(address)", balance(1))
    close = read(chain, [instrument("Newcomer", odd), instrument("Morpho", vault)])
    reasons = {p.protocol: p.reason for p in close.positions}
    assert reasons["Newcomer"] == "no historical valuation for Newcomer"
    assert reasons["Morpho"] == "Morpho valuation call reverted"


def test_block_at_finds_the_last_block_at_or_before_a_time() -> None:
    chain = FakeChain(head=100_000)
    found = chain_book.block_at(chain, 1_000 + 54_321)
    assert found == Block(54_321, 55_321)
    assert len(chain.calls) < 40
    assert chain_book.block_at(chain, 10**12).number == 100_000
    with pytest.raises(ChainReadError):
        chain_book.block_at(chain, 10)


def test_block_at_with_uneven_block_times() -> None:
    class Uneven(FakeChain):
        def __call__(self, method: str, params: list[Any]) -> Any:
            block = super().__call__(method, params)
            number = int(block["number"], 16)
            # Fast blocks early, then one block every ten seconds.
            stamp = number if number < 90_000 else 90_000 + (number - 90_000) * 10
            return {**block, "timestamp": hex(stamp)}

    chain = Uneven(head=100_000)
    assert chain_book.block_at(chain, 95_005).number == 90_500
    assert chain_book.block_at(chain, 45_000).number == 45_000


def test_deployment_block_and_archive_refusal() -> None:
    chain = FakeChain(head=10_000)
    chain.code_from[SAFE] = 4_321
    assert chain_book.deployment_block(chain, SAFE) == 4_321
    assert chain_book.deployment_block(chain, "0x" + "00" * 19 + "01") is None
    pruned = FakeChain(head=10_000, archive_from=9_000)
    pruned.code_from[SAFE] = 4_321
    with pytest.raises(ArchiveUnavailable):
        chain_book.deployment_block(pruned, SAFE)


def test_day_close_timestamp_is_the_last_second_of_the_utc_day() -> None:
    assert chain_book.day_close_timestamp(date(2026, 10, 3)) == 1_791_071_999


def test_http_rpc_maps_pruned_state_to_archive_unavailable() -> None:
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "error": {
                    "code": -32602,
                    "message": "Block requested not found. Request might be "
                    "querying historical state that is not available.",
                },
            },
        )

    rpc = HttpRpc(
        "http://rpc.invalid",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ArchiveUnavailable):
        rpc("eth_call", [{}, "0x1"])


def test_http_rpc_backs_off_on_rate_limits() -> None:
    import httpx

    replies = iter(
        [
            httpx.Response(429),
            httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "0x2a"}),
        ]
    )
    slept: list[float] = []
    rpc = HttpRpc(
        "http://rpc.invalid",
        client=httpx.Client(transport=httpx.MockTransport(lambda _r: next(replies))),
        sleep=slept.append,
    )
    assert rpc("eth_blockNumber", []) == "0x2a"
    assert slept == [0.5]


def test_valuation_matches_protocol_names_by_prefix() -> None:
    assert chain_book.valuation_of("Aave V3") == "aave"
    assert chain_book.valuation_of("Neverland") == "aave"
    assert chain_book.valuation_of("Morpho Blue") == "erc4626"
    assert chain_book.valuation_of("Something") is None
