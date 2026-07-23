from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from open_allocator.core.positions import IdleBalance, PositionHolding, Positions
from open_allocator.core.rebalance import RebalancePolicyError, plan_rebalance
from open_allocator.core.types import (
    Allocation,
    AllocationLeg,
    Policy,
    PolicyAllowed,
    PolicyCaps,
    PolicyGates,
    PolicyWallet,
    TxStep,
    Vault,
)
from open_allocator.exec.execute import ExecutionBroadcastError, GasCheck
from open_allocator.exec.rebalance import (
    RebalanceAuthorizationError,
    execute_rebalance,
)
from open_allocator.exec.signer import Receipt

ADDRESS = "0x0000000000000000000000000000000000000001"


def holding(instrument_id: str, balance: str) -> PositionHolding:
    return PositionHolding(
        instrument_id=instrument_id,
        protocol="aave",
        chain_id=8453,
        symbol="USDC",
        balance=balance,
        balance_raw=str(int(float(balance) * 1_000_000)),
        decimals=6,
        usd_value=float(balance),
        share_balance=balance,
        share_balance_raw=str(int(float(balance) * 1_000_000)),
        share_decimals=6,
        yield_token_symbol="aUSDC",
        yield_token_address="0x0000000000000000000000000000000000000002",
    )


def positions_snapshot(
    *holdings: PositionHolding,
    idle_usdc: str = "0",
) -> Positions:
    idle = IdleBalance(
        chain_id=8453,
        chain_name="Base",
        usdc_balance=idle_usdc,
        usdc_balance_raw=str(int(float(idle_usdc) * 1_000_000)),
        usd_value=float(idle_usdc),
    )
    total_position = sum(item.usd_value for item in holdings)
    return Positions(
        address=ADDRESS,
        holdings=tuple(holdings),
        idle_balances=(idle,),
        total_position_usd=total_position,
        total_idle_usdc=idle.usd_value,
        total_usd=total_position + idle.usd_value,
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


def policy(
    *,
    autonomous_rebalance: bool = False,
    max_weight_per_instrument: float = 1,
    max_deploy_per_cycle_usd: float = 1_000_000,
) -> Policy:
    return Policy(
        wallet=PolicyWallet(mode="self-custody", signer="local-eoa"),
        allowed=PolicyAllowed(
            protocols=None,
            chains=None,
            assets=("USDC",),
            curators=None,
        ),
        caps=PolicyCaps(
            max_weight_per_instrument=max_weight_per_instrument,
            max_weight_per_protocol=1,
            max_weight_per_curator=1,
            max_weight_per_chain=1,
            min_instrument_tvl_usd=1,
            max_reward_dependence=1,
        ),
        gates=PolicyGates(
            new_instrument_needs_approval=True,
            autonomous_rebalance=autonomous_rebalance,
            max_deploy_per_cycle_usd=max_deploy_per_cycle_usd,
        ),
    )


def vault(instrument_id: str) -> Vault:
    return Vault(
        instrument_id=instrument_id,
        protocol="aave",
        chain_id=8453,
        asset="USDC",
        apy=0.04,
        tvl_usd=1_000_000,
        curator="curator-a",
        reward_dependence=0.1,
    )


def known(*instrument_ids: str) -> list[Vault]:
    return [vault(instrument_id) for instrument_id in instrument_ids]


@dataclass
class MockRebalanceClient:
    sell_responses: list[dict[str, Any]]
    buy_responses: list[dict[str, Any]]
    sell_bodies: list[dict[str, object]] = field(default_factory=list)
    buy_bodies: list[dict[str, object]] = field(default_factory=list)

    def build_sell(self, body: dict[str, object]) -> dict[str, Any]:
        self.sell_bodies.append(body)
        return self.sell_responses.pop(0)

    def build_buy(self, body: dict[str, object]) -> dict[str, Any]:
        self.buy_bodies.append(body)
        return self.buy_responses.pop(0)


@dataclass
class MockSigner:
    fail_at: int | None = None
    sent: list[tuple[TxStep, str]] = field(default_factory=list)
    address_calls: int = 0

    def address(self) -> str:
        self.address_calls += 1
        return ADDRESS

    def send(self, tx: TxStep, rpc_url: str) -> Receipt:
        if self.fail_at is not None and len(self.sent) == self.fail_at:
            raise RuntimeError("boom")
        self.sent.append((tx, rpc_url))
        index = len(self.sent)
        return Receipt(
            transaction_hash=f"0x{index:064x}",
            block_number=index,
            gas_used=21_000,
            status=1,
            from_address=ADDRESS,
            to_address=tx.to,
        )


@dataclass(frozen=True)
class Config:
    gas_checker: object = lambda _address, chain_id, _rpc_url, _config: GasCheck(
        chain_id=chain_id,
        ok=True,
        balance_wei=1,
        required_wei=1,
        message=f"native gas available on chain {chain_id}",
    )
    _rpc_overrides: dict[int, str] = field(default_factory=lambda: {8453: "rpc://base"})


def response(data: str, *, type_: str | None = None) -> dict[str, Any]:
    transaction: dict[str, object] = {
        "to": "0x0000000000000000000000000000000000000002",
        "data": data,
        "value": 0,
        "chainId": 8453,
    }
    if type_ is not None:
        transaction["type"] = type_
    return {"transactions": [transaction]}


def test_plan_rebalance_executes_only_changed_legs_and_skips_dust() -> None:
    current = positions_snapshot(holding("vault-a", "60"), holding("vault-b", "40"))

    plan = plan_rebalance(
        current,
        allocation(("vault-a", 0.5), ("vault-b", 0.4), ("vault-c", 0.1)),
        policy(),
        known_instruments=known("vault-a", "vault-b", "vault-c"),
        min_trade_usd=6,
    )

    planned_trades = [
        (trade.action, trade.instrument_id, trade.usd) for trade in plan.trades
    ]
    assert planned_trades == [("sell", "vault-a", 10), ("buy", "vault-c", 10)]
    assert all(trade.instrument_id != "vault-b" for trade in plan.trades)

    dust_plan = plan_rebalance(
        current,
        allocation(("vault-a", 0.59), ("vault-b", 0.4), ("vault-c", 0.01)),
        policy(),
        known_instruments=known("vault-a", "vault-b", "vault-c"),
        min_trade_usd=2,
    )

    assert dust_plan.trades == ()
    skipped_deltas = [
        (delta.instrument_id, delta.action) for delta in dust_plan.skipped_deltas
    ]
    assert skipped_deltas == [
        ("vault-a", "sell"),
        ("vault-c", "buy"),
    ]


def test_plan_rebalance_orders_sells_before_buys() -> None:
    current = positions_snapshot(holding("vault-a", "80"), holding("vault-b", "20"))

    plan = plan_rebalance(
        current,
        allocation(("vault-a", 0.5), ("vault-b", 0.5)),
        policy(),
        known_instruments=known("vault-a", "vault-b"),
    )

    assert [(trade.action, trade.instrument_id) for trade in plan.trades] == [
        ("sell", "vault-a"),
        ("buy", "vault-b"),
    ]


def test_policy_violation_aborts_before_trade_plan() -> None:
    current = positions_snapshot(holding("vault-a", "50"), holding("vault-b", "50"))

    with pytest.raises(RebalancePolicyError) as error:
        plan_rebalance(
            current,
            allocation(("vault-a", 1.0)),
            policy(max_weight_per_instrument=0.6),
            known_instruments=known("vault-a", "vault-b"),
        )

    assert {violation.rule for violation in error.value.result.violations} == {
        "max_weight_per_instrument",
    }


def test_autonomous_rebalance_false_blocks_unattended_execution() -> None:
    client = MockRebalanceClient([response("0xsell")], [response("0xbuy")])
    signer = MockSigner()
    current = positions_snapshot(holding("vault-a", "80"), holding("vault-b", "20"))

    with pytest.raises(RebalanceAuthorizationError):
        execute_rebalance(
            client,
            signer,
            current,
            allocation(("vault-a", 0.5), ("vault-b", 0.5)),
            policy(autonomous_rebalance=False),
            autonomous=True,
            known_instruments=known("vault-a", "vault-b"),
            config=Config(),
        )

    assert signer.address_calls == 0
    assert client.sell_bodies == []
    assert client.buy_bodies == []


def test_confirmed_rebalance_sells_before_buys_and_retries_from_store() -> None:
    store: dict[str, object] = {}
    current = positions_snapshot(holding("vault-a", "80"), holding("vault-b", "20"))
    target = allocation(("vault-a", 0.5), ("vault-b", 0.5))
    first_client = MockRebalanceClient(
        [response("0xsell")],
        [response("0xbuy")],
    )
    first_signer = MockSigner(fail_at=1)

    with pytest.raises(ExecutionBroadcastError):
        execute_rebalance(
            first_client,
            first_signer,
            current,
            target,
            policy(),
            confirm=True,
            known_instruments=known("vault-a", "vault-b"),
            config=Config(),
            idempotency_store=store,
        )

    assert [sent[0].data for sent in first_signer.sent] == ["0xsell"]
    assert "leg:0:vault-a" in store
    retry_client = MockRebalanceClient([], [response("0xbuy-retry")])
    retry_signer = MockSigner()

    report = execute_rebalance(
        retry_client,
        retry_signer,
        current,
        target,
        policy(),
        confirm=True,
        known_instruments=known("vault-a", "vault-b"),
        config=Config(),
        idempotency_store=store,
    )

    assert report.status == "success"
    assert retry_client.sell_bodies == []
    assert [body["instrumentId"] for body in retry_client.buy_bodies] == ["vault-b"]
    assert [sent[0].data for sent in retry_signer.sent] == ["0xbuy-retry"]


@dataclass
class PendingRebalanceSigner:
    """A Safe below its threshold: every leg is proposed, none broadcast."""

    sent: list[TxStep] = field(default_factory=list)

    def address(self) -> str:
        return ADDRESS

    def send(self, tx: TxStep, rpc_url: str) -> Receipt:
        self.sent.append(tx)
        return Receipt(
            transaction_hash="0xproposal",
            block_number=0,
            gas_used=0,
            status=0,
            from_address=ADDRESS,
            to_address=tx.to,
            pending=True,
            execution_status="safe_proposed",
        )


def test_a_proposed_rebalance_is_not_reported_as_a_completed_rebalance() -> None:
    """The book has not moved until the co-signers execute the proposals."""
    current = positions_snapshot(holding("vault-a", "80"), holding("vault-b", "20"))
    target = allocation(("vault-a", 0.5), ("vault-b", 0.5))

    report = execute_rebalance(
        MockRebalanceClient([response("0xsell")], [response("0xbuy")]),
        PendingRebalanceSigner(),
        current,
        target,
        policy(),
        confirm=True,
        known_instruments=known("vault-a", "vault-b"),
        config=Config(),
    )

    assert report.status == "in_progress"
    assert report.in_progress is True
    assert any("awaiting threshold" in message for message in report.messages)
