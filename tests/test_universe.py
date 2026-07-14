from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from open_allocator.core import universe
from open_allocator.core.types import (
    Policy,
    PolicyAllowed,
    PolicyCaps,
    PolicyGates,
    PolicyWallet,
    Unknown,
)
from open_allocator.exec.chains import rpc_url
from open_allocator.exec.client import Instrument


class StubClient:
    def __init__(self, instruments: list[object]) -> None:
        self.instruments = instruments
        self.calls = 0

    def list_instruments(self, **_filters: object) -> object:
        self.calls += 1
        return SimpleNamespace(data=tuple(self.instruments))


class PaginatedClient:
    def __init__(self, pages: list[object]) -> None:
        self.pages = pages
        self.calls: list[dict[str, object]] = []

    def list_instruments(self, **filters: object) -> object:
        self.calls.append(filters)
        return self.pages.pop(0)


def instrument(**overrides: Any) -> dict[str, Any]:
    data = {
        "instrumentId": "instrument-1",
        "protocol": "protocol-1",
        "chainId": 1001,
        "tokenSymbol": "USDC",
        "currentApy": 4.2,
        "tvl": 1_000_000,
    }
    data.update(overrides)
    return data


def policy(
    *,
    protocols: tuple[str, ...] | None = None,
    chains: tuple[int, ...] | None = None,
    assets: tuple[str, ...] | None = None,
    min_tvl: float = 0,
) -> Policy:
    return Policy(
        wallet=PolicyWallet(mode="self-custody", signer="local-eoa"),
        allowed=PolicyAllowed(protocols=protocols, chains=chains, assets=assets),
        caps=PolicyCaps(
            max_weight_per_instrument=1,
            max_weight_per_protocol=1,
            max_weight_per_curator=1,
            max_weight_per_chain=1,
            min_instrument_tvl_usd=min_tvl,
            max_reward_dependence=1,
        ),
        gates=PolicyGates(
            new_instrument_needs_approval=True,
            autonomous_rebalance=False,
            max_deploy_per_cycle_usd=1_000,
        ),
    )


def test_discovers_novel_protocol_and_chain_from_pydantic_model() -> None:
    novel_protocol = "new-protocol-under-test"
    novel_chain = 987_654_321
    pydantic_instrument = Instrument.model_validate(
        instrument(
            instrumentId="novel-instrument",
            protocol=novel_protocol,
            chainId=novel_chain,
            isActive=True,
            isStablecoin=True,
        )
    )
    client = StubClient([pydantic_instrument])

    assert rpc_url(novel_chain, {}) is None

    vaults = universe.discover(client)

    assert client.calls == 1
    assert len(vaults) == 1
    assert vaults[0].instrument_id == "novel-instrument"
    assert vaults[0].protocol == novel_protocol
    assert vaults[0].chain_id == novel_chain
    assert universe.seen_protocols(vaults) == (novel_protocol,)
    assert universe.seen_chains(vaults) == (novel_chain,)


def test_discovers_all_paginated_instrument_pages() -> None:
    client = PaginatedClient(
        [
            SimpleNamespace(
                data=(instrument(instrumentId="page-1"),),
                pagination=SimpleNamespace(limit=1, offset=0, has_more=True),
            ),
            SimpleNamespace(
                data=(instrument(instrumentId="page-2"),),
                pagination=SimpleNamespace(limit=1, offset=1, has_more=False),
            ),
        ]
    )

    vaults = universe.discover(client)

    assert [vault.instrument_id for vault in vaults] == ["page-1", "page-2"]
    assert client.calls == [{}, {"limit": 1, "offset": 1}]


def test_policy_filters_narrow_and_none_allowlists_pass_through() -> None:
    instruments = [
        instrument(instrumentId="wrong-protocol", protocol="protocol-a", chainId=2002),
        instrument(instrumentId="wrong-chain", protocol="protocol-b", chainId=2002),
        instrument(
            instrumentId="wrong-asset",
            protocol="protocol-b",
            chainId=3003,
            tokenSymbol="DAI",
        ),
        instrument(
            instrumentId="keep",
            protocol="protocol-b",
            chainId=3003,
            tokenSymbol="USDT",
        ),
    ]

    filtered = universe.discover(
        StubClient(instruments),
        policy=policy(protocols=("protocol-b",), chains=(3003,), assets=("USDT",)),
    )
    unfiltered = universe.discover(StubClient(instruments), policy=policy())

    assert [vault.instrument_id for vault in filtered] == ["keep"]
    assert [vault.instrument_id for vault in unfiltered] == [
        "wrong-protocol",
        "wrong-chain",
        "wrong-asset",
        "keep",
    ]


def test_min_instrument_tvl_usd_excludes_thin_pools() -> None:
    vaults = universe.discover(
        StubClient(
            [
                instrument(instrumentId="thin", tvl=999_999),
                instrument(instrumentId="large", tvl=1_000_000),
            ]
        ),
        policy=policy(min_tvl=1_000_000),
    )

    assert [vault.instrument_id for vault in vaults] == ["large"]


def test_missing_risk_fields_are_unknown() -> None:
    vault = universe.discover(StubClient([instrument()]))[0]

    assert vault.curator == Unknown
    assert vault.reward_dependence == Unknown
    assert vault.oracle == Unknown
    assert vault.fee == Unknown
    assert vault.apy_stability == Unknown
    assert vault.market_concentration == Unknown
    assert vault.liquidity == Unknown
    assert vault.collateral_mix == Unknown


def test_universe_module_has_no_hardcoded_protocol_or_chain_literals() -> None:
    source = Path(universe.__file__).read_text()

    forbidden_literals = (
        '"morpho"',
        '"aave"',
        '"compound"',
        '"base"',
        '"arbitrum"',
        '"ethereum"',
        "8453",
        "42161",
        "137",
    )
    for literal in forbidden_literals:
        assert literal not in source
    assert "open_allocator.exec.chains" not in source
