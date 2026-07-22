from __future__ import annotations

import pytest

from open_allocator.core.positions import (
    IdleBalance,
    PositionHolding,
    Positions,
    read_positions,
    reconcile,
)
from open_allocator.core.types import Allocation, AllocationLeg

ADDRESS = "0x0000000000000000000000000000000000000001"


class MockPositionsClient:
    def __init__(self) -> None:
        self.position_bodies: list[dict[str, object]] = []

    def balances(self, address: str) -> dict[str, object]:
        assert address == ADDRESS
        return {
            "address": address,
            "balances": [
                {
                    "chainId": 8453,
                    "chainName": "Base",
                    "usdcBalance": "25.000000",
                    "usdcBalanceRaw": "25000000",
                }
            ],
            "totalUsdcUsd": "25.000000",
        }

    def positions(self, body: dict[str, object]) -> dict[str, object]:
        self.position_bodies.append(body)
        return {
            "address": ADDRESS,
            "chainId": 8453,
            "usdcBalance": "25.000000",
            "positions": [
                {
                    "instrumentId": "base-aave-usdc",
                    "protocol": "aave",
                    "symbol": "USDC",
                    "yieldTokenSymbol": "aUSDC",
                    "description": "Aave USDC",
                    "balance": "75.000000",
                    "balanceRaw": "75000000",
                    "decimals": 6,
                    "shareBalance": "74.999123",
                    "shareBalanceRaw": "74999123",
                    "shareDecimals": 6,
                    "currentApy": 0.04,
                    "yieldTokenAddress": (
                        "0x0000000000000000000000000000000000000002"
                    ),
                    "chainId": 8453,
                }
            ],
        }


def holding(instrument_id: str, balance: str) -> PositionHolding:
    return PositionHolding(
        instrument_id=instrument_id,
        protocol="test-protocol",
        chain_id=8453,
        symbol="USDC",
        balance=balance,
        balance_raw=str(int(float(balance) * 1_000_000)),
        decimals=6,
        usd_value=float(balance),
        share_balance=balance,
        share_balance_raw=str(int(float(balance) * 1_000_000)),
        share_decimals=6,
        yield_token_symbol="yUSDC",
        yield_token_address="0x0000000000000000000000000000000000000002",
    )


def positions_snapshot(
    holdings: tuple[PositionHolding, ...],
    *,
    idle_usdc: str = "0",
) -> Positions:
    idle = (
        IdleBalance(
            chain_id=8453,
            chain_name="Base",
            usdc_balance=idle_usdc,
            usdc_balance_raw=str(int(float(idle_usdc) * 1_000_000)),
            usd_value=float(idle_usdc),
        ),
    )
    total_position = sum(item.usd_value for item in holdings)
    total_idle = sum(item.usd_value for item in idle)
    return Positions(
        address=ADDRESS,
        holdings=holdings,
        idle_balances=idle,
        total_position_usd=total_position,
        total_idle_usdc=total_idle,
        total_usd=total_position + total_idle,
        total_usdc_usd=idle_usdc,
    )


def allocation(*legs: tuple[str, float]) -> Allocation:
    return Allocation(
        legs=tuple(
            AllocationLeg(instrument_id=instrument_id, weight=weight, usd=weight * 100)
            for instrument_id, weight in legs
        ),
        total_usd=100,
        metadata={},
    )


def test_read_positions_parses_share_balances_and_idle_usdc() -> None:
    client = MockPositionsClient()

    result = read_positions(client, ADDRESS)

    assert client.position_bodies == [{"address": ADDRESS, "chainId": 8453}]
    assert result.total_position_usd == 75
    assert result.total_idle_usdc == 25
    assert result.total_usd == 100
    assert result.idle_balances[0].usdc_balance == "25.000000"
    assert result.holdings[0].instrument_id == "base-aave-usdc"
    assert result.holdings[0].share_balance == "74.999123"
    assert result.holdings[0].share_balance_raw == "74999123"
    assert result.holdings[0].share_decimals == 6


def test_read_positions_requires_share_balance_fields() -> None:
    class MissingShareClient(MockPositionsClient):
        def positions(self, body: dict[str, object]) -> dict[str, object]:
            payload = super().positions(body)
            positions = payload["positions"]
            assert isinstance(positions, list)
            del positions[0]["shareBalance"]
            return payload

    with pytest.raises(ValueError, match="share_balance"):
        read_positions(MissingShareClient(), ADDRESS)


def test_reconcile_matched_book_has_empty_deltas() -> None:
    current = positions_snapshot(
        (
            holding("vault-a", "60"),
            holding("vault-b", "40"),
        )
    )

    diff = reconcile(current, allocation(("vault-a", 0.6), ("vault-b", 0.4)))

    assert diff.deltas == ()
    assert diff.total_buy_usd == 0
    assert diff.total_sell_usd == 0
    assert diff.deploy_usdc == 0


def test_reconcile_surfaces_idle_usdc_as_deploy_deltas() -> None:
    current = positions_snapshot(
        (
            holding("vault-a", "50"),
            holding("vault-b", "50"),
        ),
        idle_usdc="20",
    )

    diff = reconcile(current, allocation(("vault-a", 0.5), ("vault-b", 0.5)))

    assert diff.idle_usdc == 20
    assert diff.deploy_usdc == 20
    assert diff.total_buy_usd == 20
    assert diff.total_sell_usd == 0
    deploys = [
        (delta.instrument_id, delta.action, delta.buy_usd)
        for delta in diff.deltas
    ]
    assert deploys == [
        ("vault-a", "buy", 10),
        ("vault-b", "buy", 10),
    ]


def test_reconcile_computes_buy_and_sell_deltas() -> None:
    current = positions_snapshot(
        (
            holding("vault-a", "80"),
            holding("vault-b", "20"),
        )
    )

    diff = reconcile(current, allocation(("vault-a", 0.5), ("vault-b", 0.5)))

    assert [
        (delta.instrument_id, delta.action, delta.delta_usd)
        for delta in diff.deltas
    ] == [("vault-a", "sell", -30), ("vault-b", "buy", 30)]
    assert diff.total_buy_usd == 30
    assert diff.total_sell_usd == 30
    assert diff.deploy_usdc == 0


class MockEmptyWalletClient:
    """A funded wallet that has not deposited yet — no holdings at all."""

    def balances(self, address: str) -> dict[str, object]:
        return {
            "address": address,
            "balances": [],
            "totalUsdcUsd": "0",
        }

    def positions(self, body: dict[str, object]) -> dict[str, object]:
        return {
            "address": body.get("address"),
            "chainId": 8453,
            "usdcBalance": "0",
            "positions": [],
        }


def test_read_positions_handles_a_wallet_with_no_holdings() -> None:
    result = read_positions(MockEmptyWalletClient(), ADDRESS)

    assert result.holdings == ()
    assert result.idle_balances == ()
    assert result.total_position_usd == 0
    assert result.total_idle_usdc == 0
    assert result.total_usd == 0
