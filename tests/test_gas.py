"""Live-gas reader behaviour, exercised without touching the network.

The point of every test here is that a failed read *degrades* — a gas estimate
that silently invents a price is worse than one that admits it fell back.
"""

from __future__ import annotations

import pytest

from open_allocator.exec import gas


def test_live_pricing_returns_none_without_an_eth_quote(monkeypatch) -> None:
    monkeypatch.setattr(gas, "eth_usd", lambda chain_ids=(): None)
    monkeypatch.setattr(gas, "gas_price_wei", lambda chain_id: 10**9)
    assert gas.live_pricing([8453]) is None


def test_live_pricing_returns_none_when_no_chain_prices(monkeypatch) -> None:
    monkeypatch.setattr(gas, "eth_usd", lambda chain_ids=(): 2000.0)
    monkeypatch.setattr(gas, "gas_price_wei", lambda chain_id: None)
    assert gas.live_pricing([8453, 130]) is None


def test_live_pricing_keeps_the_chains_that_did_price(monkeypatch) -> None:
    monkeypatch.setattr(gas, "eth_usd", lambda chain_ids=(): 2000.0)
    monkeypatch.setattr(
        gas, "gas_price_wei", lambda chain_id: 10**9 if chain_id == 8453 else None
    )
    pricing = gas.live_pricing([8453, 130])
    assert pricing is not None
    # Unichain must be absent, not backfilled from Base.
    assert dict(pricing.gas_price_wei) == {8453: 10**9}
    assert pricing.usd_per_tx(130) is None
    assert pricing.usd_per_tx(8453) is not None


def test_live_pricing_deduplicates_chain_ids(monkeypatch) -> None:
    seen: list[int] = []

    def _price(chain_id: int) -> int:
        seen.append(chain_id)
        return 10**9

    monkeypatch.setattr(gas, "eth_usd", lambda chain_ids=(): 2000.0)
    monkeypatch.setattr(gas, "gas_price_wei", _price)
    gas.live_pricing([8453, 8453, 130, 8453])
    assert seen == [8453, 130]


def test_live_pricing_with_no_chains_is_none() -> None:
    assert gas.live_pricing([]) is None


class _FakeEth:
    def __init__(self, answer: int, decimals: int) -> None:
        self._answer = answer
        self._decimals = decimals

    def call(self, tx: dict) -> bytes:
        data = tx["data"]
        if data == gas._DECIMALS_SELECTOR:
            return self._decimals.to_bytes(32, "big")
        # latestRoundData: 5 words, answer in the second.
        return (
            (0).to_bytes(32, "big")
            + self._answer.to_bytes(32, "big", signed=True)
            + (0).to_bytes(32, "big") * 3
        )


class _FakeWeb3:
    def __init__(self, answer: int, decimals: int) -> None:
        self.eth = _FakeEth(answer, decimals)


@pytest.mark.parametrize(
    ("answer", "decimals", "expected"),
    [
        (191_621_336_196, 8, 1916.21336196),  # the real mainnet feed shape
        (0, 8, None),  # a feed that has never reported
        (-1, 8, None),  # negative price is a broken feed
        (191_621_336_196, 40, None),  # implausible decimals -> refuse to scale
        (1, 8, None),  # 1e-8 USD: scaled wrong, below the plausible floor
        (10**30, 8, None),  # absurdly high: above the plausible ceiling
    ],
)
def test_feed_reads_reject_implausible_prices(
    monkeypatch, answer: int, decimals: int, expected: float | None
) -> None:
    """A misread feed usually means wrong decimals scaling, not a real price."""
    monkeypatch.setattr(gas, "_web3", lambda chain_id: _FakeWeb3(answer, decimals))
    result = gas._read_feed(1, gas.ETH_USD_FEEDS[1])
    if expected is None:
        assert result is None
    else:
        assert result == pytest.approx(expected)


def test_feed_read_survives_an_rpc_exception(monkeypatch) -> None:
    class _Boom:
        @property
        def eth(self):
            raise RuntimeError("rpc down")

    monkeypatch.setattr(gas, "_web3", lambda chain_id: _Boom())
    assert gas._read_feed(1, gas.ETH_USD_FEEDS[1]) is None


def test_eth_usd_prefers_a_chain_already_in_play(monkeypatch) -> None:
    tried: list[int] = []

    def _read(chain_id: int, feed: str) -> float | None:
        tried.append(chain_id)
        return 1900.0 if chain_id == 8453 else None

    monkeypatch.setattr(gas, "_read_feed", _read)
    assert gas.eth_usd([8453]) == 1900.0
    # Base was tried first because it was the chain in play, not because of
    # dict ordering in ETH_USD_FEEDS (where Ethereum comes first).
    assert tried[0] == 8453


def test_eth_usd_falls_through_to_other_feeds(monkeypatch) -> None:
    monkeypatch.setattr(
        gas, "_read_feed", lambda chain_id, feed: 1900.0 if chain_id == 1 else None
    )
    # Nothing in play prices, so a known feed on another chain must be tried.
    assert gas.eth_usd([130]) == 1900.0


def test_eth_usd_is_none_when_every_feed_fails(monkeypatch) -> None:
    monkeypatch.setattr(gas, "_read_feed", lambda chain_id, feed: None)
    assert gas.eth_usd([8453]) is None


def test_gas_price_is_none_without_an_rpc_url(monkeypatch) -> None:
    monkeypatch.setattr(gas.chains, "rpc_url", lambda chain_id: None)
    assert gas.gas_price_wei(8453) is None
