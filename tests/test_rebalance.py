from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from open_allocator.core.checkpoint import (
    AllocationLogEntry,
    Checkpoint,
    read_allocation_log,
)
from open_allocator.core.positions import IdleBalance, PositionHolding, Positions
from open_allocator.core.rebalance import RebalancePolicyError, plan_rebalance
from open_allocator.core.schema import validate
from open_allocator.core.state import CheckpointExists, CheckpointNotFound
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
from open_allocator.exec.bundle_execution import UnderfundedPlanError
from open_allocator.exec.calldata import CalldataUnsupportedError
from open_allocator.exec.client import (
    InstrumentCalldataQuery,
    InstrumentCalldataResponse,
)
from open_allocator.exec.execute import (
    ExecutionBroadcastError,
    FundingLedger,
    GasCheck,
)
from open_allocator.exec.paymaster_types import (
    PaymasterTokenQuote,
    PreparedUserOperation,
    UserOperationGas,
)
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
    _rpc_overrides: dict[int, str] = field(
        default_factory=lambda: {8453: "rpc://base", 42161: "rpc://arb", 10: "rpc://op"}
    )
    settle_waiter: object = lambda: None  # never sleep in tests
    # Off by default so the funding tests measure routing, not the reserve.
    # test_the_paymaster_reserve_is_held_back covers the reserve on its own.
    paymaster_reserve_usd: float = 0.0


class MemoryStateBackend:
    def __init__(self) -> None:
        self.checkpoints: dict[str, Checkpoint] = {}
        self.log: list[AllocationLogEntry] = []
        self.completed: dict[tuple[str, str], Any] = {}

    def write_checkpoint(self, checkpoint: Checkpoint) -> None:
        if checkpoint.id in self.checkpoints:
            raise CheckpointExists(checkpoint.id)
        self.checkpoints[checkpoint.id] = checkpoint

    def read_checkpoint(self, checkpoint_id: str) -> Checkpoint:
        try:
            return self.checkpoints[checkpoint_id]
        except KeyError as error:
            raise CheckpointNotFound(checkpoint_id) from error

    def append_allocation_log_entry(self, entry: AllocationLogEntry) -> None:
        self.log.append(entry)

    def read_allocation_log(self) -> tuple[AllocationLogEntry, ...]:
        return tuple(self.log)

    def is_completed(self, scope: str, key: str) -> bool:
        return (scope, key) in self.completed

    def mark_completed(self, scope: str, key: str, value: Any = None) -> None:
        self.completed[(scope, key)] = value

    def completed_value(self, scope: str, key: str) -> Any:
        return self.completed.get((scope, key))


@dataclass(frozen=True)
class StateConfig(Config):
    state_backend: object = field(default_factory=MemoryStateBackend)
    position_settlement_attempts: int = 1


@dataclass
class PositionAwareClient(MockRebalanceClient):
    observed_shares: list[str | None] = field(default_factory=list)

    def positions(self, _body: dict[str, object]) -> dict[str, Any]:
        value = self.observed_shares.pop(0)
        if value is None:
            return {"positions": []}
        return {
            "positions": [
                {
                    "instrumentId": "vault-b",
                    "protocol": "aave",
                    "symbol": "USDC",
                    "balance": value,
                    "balanceRaw": value.replace(".", ""),
                    "decimals": 6,
                    "usdValue": value,
                    "shareBalance": value,
                    "shareBalanceRaw": value.replace(".", ""),
                    "shareDecimals": 6,
                    "yieldTokenSymbol": "aUSDC",
                    "yieldTokenAddress": "0x0000000000000000000000000000000000000002",
                }
            ]
        }


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


def test_confirmed_buy_persists_the_settled_share_boundary() -> None:
    backend = MemoryStateBackend()
    config = StateConfig(state_backend=backend)
    client = PositionAwareClient([], [response("0xbuy")], observed_shares=["9.5"])

    report = execute_rebalance(
        client,
        MockSigner(),
        positions_snapshot(holding("vault-a", "90"), idle_usdc="10"),
        allocation(("vault-a", 0.9), ("vault-b", 0.1)),
        policy(),
        confirm=True,
        known_instruments=known("vault-a", "vault-b"),
        config=config,
        idempotency_store={},
    )

    entries = read_allocation_log(backend=backend)
    assert report.status == "success"
    assert len(entries) == 1
    assert entries[0].usd == 10
    assert entries[0].shares == "9.5"
    assert entries[0].share_price == "1.05263157894736842"
    assert entries[0].basis == "derived"
    assert entries[0].tx_hash == f"0x{1:064x}"


def test_confirmed_top_up_logs_only_the_new_shares() -> None:
    backend = MemoryStateBackend()
    client = PositionAwareClient([], [response("0xbuy")], observed_shares=["49.5"])
    execute_rebalance(
        client,
        MockSigner(),
        positions_snapshot(
            holding("vault-a", "50"), holding("vault-b", "40"), idle_usdc="10"
        ),
        allocation(("vault-a", 0.5), ("vault-b", 0.5)),
        policy(),
        confirm=True,
        known_instruments=known("vault-a", "vault-b"),
        config=StateConfig(state_backend=backend),
        idempotency_store={},
    )

    assert read_allocation_log(backend=backend)[0].shares == "9.5"


def test_confirmed_buy_with_stale_positions_remains_unattributed() -> None:
    backend = MemoryStateBackend()
    client = PositionAwareClient([], [response("0xbuy")], observed_shares=[None])
    report = execute_rebalance(
        client,
        MockSigner(),
        positions_snapshot(holding("vault-a", "90"), idle_usdc="10"),
        allocation(("vault-a", 0.9), ("vault-b", 0.1)),
        policy(),
        confirm=True,
        known_instruments=known("vault-a", "vault-b"),
        config=StateConfig(state_backend=backend),
        idempotency_store={},
    )

    assert report.status == "in_progress"
    assert read_allocation_log(backend=backend) == ()
    assert any("cost basis" in message for message in report.messages)


def test_retry_attributes_a_confirmed_buy_without_rebroadcasting() -> None:
    backend = MemoryStateBackend()
    config = StateConfig(state_backend=backend)
    store: dict[str, object] = {}
    current = positions_snapshot(holding("vault-a", "90"), idle_usdc="10")
    target = allocation(("vault-a", 0.9), ("vault-b", 0.1))

    first_signer = MockSigner()
    first = execute_rebalance(
        PositionAwareClient([], [response("0xbuy")], observed_shares=[None]),
        first_signer,
        current,
        target,
        policy(),
        confirm=True,
        known_instruments=known("vault-a", "vault-b"),
        config=config,
        idempotency_store=store,
    )
    second_signer = MockSigner()
    second = execute_rebalance(
        PositionAwareClient([], [response("0xbuy-retry")], observed_shares=["9.5"]),
        second_signer,
        current,
        target,
        policy(),
        confirm=True,
        known_instruments=known("vault-a", "vault-b"),
        config=config,
        idempotency_store=store,
    )

    assert first.status == "in_progress"
    assert second.status == "success"
    assert len(first_signer.sent) == 1
    assert second_signer.sent == []
    assert len(read_allocation_log(backend=backend)) == 1


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


def test_plan_rebalance_partial_sell_carries_raw_underlying_calldata_amount() -> None:
    current = positions_snapshot(holding("vault-a", "80"), holding("vault-b", "20"))

    plan = plan_rebalance(
        current,
        allocation(("vault-a", 0.5), ("vault-b", 0.5)),
        policy(),
        known_instruments=known("vault-a", "vault-b"),
    )

    sell, buy = plan.trades
    assert (sell.action, sell.instrument_id, sell.usd) == ("sell", "vault-a", 30)
    # floor(80000000 * 30 / 80): raw underlying units, not the share estimate.
    assert sell.calldata_amount == "30000000"
    assert sell.yield_token_amount == "30"
    assert buy.calldata_amount is None


def test_plan_rebalance_full_exit_sends_max() -> None:
    current = positions_snapshot(holding("vault-a", "60"), holding("vault-b", "40"))

    plan = plan_rebalance(
        current,
        allocation(("vault-b", 1.0)),
        policy(),
        known_instruments=known("vault-a", "vault-b"),
    )

    sells = [trade for trade in plan.trades if trade.action == "sell"]
    assert [(trade.instrument_id, trade.calldata_amount) for trade in sells] == [
        ("vault-a", "max")
    ]


def test_plan_rebalance_partial_sell_without_raw_balance_has_no_calldata_amount() -> (
    None
):
    unreadable = holding("vault-a", "80").model_copy(update={"balance_raw": None})
    current = positions_snapshot(unreadable, holding("vault-b", "20"))

    plan = plan_rebalance(
        current,
        allocation(("vault-a", 0.5), ("vault-b", 0.5)),
        policy(),
        known_instruments=known("vault-a", "vault-b"),
    )

    sell = plan.trades[0]
    assert sell.action == "sell"
    assert sell.yield_token_amount == "30"
    assert sell.calldata_amount is None


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


# ── funding: a sell pays for a buy inside the same batch ──────────────────────
#
# The bug (agent-showcase A7, 2026-08-20): every buy was planned against the
# balances the wallet held BEFORE the batch, so a sell-funded buy was rejected by
# 1Tx with "No chain has sufficient USDC balance" even though the batch orders
# sells first and the money is there by the time the buy runs.


def test_ledger_prefers_the_vaults_own_chain_so_no_bridge_is_needed() -> None:
    ledger = FundingLedger({8453: 50.0, 42161: 90.0})

    assert ledger.plan_sources(8453, 30.0) == ((8453, 30.0),)
    assert ledger.available[8453] == pytest.approx(20.0), "debited, not just read"
    assert ledger.available[42161] == pytest.approx(90.0), "untouched"


def test_ledger_falls_back_to_the_best_funded_chain_then_splits() -> None:
    ledger = FundingLedger({8453: 10.0, 42161: 30.0, 143: 5.0})

    # Own chain cannot cover it alone, so draw from it first, then the largest.
    sources = ledger.plan_sources(8453, 25.0)

    assert sources == ((8453, 10.0), (42161, 15.0))
    assert sum(usd for _, usd in sources) == pytest.approx(25.0)
    assert ledger.available[8453] == pytest.approx(0.0)
    assert ledger.available[42161] == pytest.approx(15.0)
    assert ledger.available[143] == pytest.approx(5.0), "never needed"


def test_ledger_credits_a_sell_and_then_the_buy_it_pays_for_fits() -> None:
    ledger = FundingLedger({8453: 1.0})

    assert ledger.plan_sources(8453, 12.56) == (), "before the sell: nothing fits"
    ledger.credit(8453, 12.0)
    assert ledger.plan_sources(8453, 12.56) == ((8453, 12.56),), "after: it does"


def test_ledger_refuses_to_split_what_it_cannot_cover_in_aggregate() -> None:
    """Better one op 1Tx rejects than three that cannot all settle."""
    ledger = FundingLedger({8453: 5.0, 42161: 5.0})

    assert ledger.plan_sources(8453, 25.0) == ()
    assert ledger.available == {8453: 5.0, 42161: 5.0}, "nothing debited"


def test_ledger_ignores_dust_chains_and_an_unknown_amount() -> None:
    ledger = FundingLedger({8453: 0.004, 42161: 20.0})

    assert ledger.plan_sources(42161, None) == (), "unknown amount: let 1Tx pick"
    assert ledger.plan_sources(8453, 15.0) == ((42161, 15.0),), "dust is not a source"


@dataclass
class BalanceAwareClient(MockRebalanceClient):
    """A client that reports idle USDC per chain and settles sells into it.

    The settlement half matters: the staged executor re-reads balances between
    the sells and the buys, so a mock that never credits a sell would test the
    executor against a wallet that behaves nothing like the real one.
    """

    idle: dict[int, float] = field(default_factory=dict)
    sell_credits: dict[str, tuple[int, float]] = field(default_factory=dict)

    def balances(self, _address: str) -> dict[str, Any]:
        return {
            "balances": [
                {"chainId": chain, "usdcBalance": usdc}
                for chain, usdc in self.idle.items()
            ]
        }

    def build_sell(self, body: dict[str, object]) -> dict[str, Any]:
        credit = self.sell_credits.get(str(body["instrumentId"]))
        if credit is not None:
            chain, usd = credit
            self.idle[chain] = self.idle.get(chain, 0.0) + usd
        return super().build_sell(body)

    def build_buy(self, body: dict[str, object]) -> dict[str, Any]:
        chain = body.get("sourceChainId")
        if isinstance(chain, int):
            spent = float(str(body["amountUsdc"]))
            self.idle[chain] = max(0.0, self.idle.get(chain, 0.0) - spent)
        return super().build_buy(body)


def chain_holding(instrument_id: str, balance: str, chain_id: int) -> PositionHolding:
    return holding(instrument_id, balance).model_copy(update={"chain_id": chain_id})


def chain_vault(instrument_id: str, chain_id: int) -> Vault:
    return vault(instrument_id).model_copy(update={"chain_id": chain_id})


def bare_positions(*holdings: PositionHolding) -> Positions:
    total = sum(item.usd_value for item in holdings)
    return Positions(
        address=ADDRESS,
        holdings=tuple(holdings),
        idle_balances=(),
        total_position_usd=total,
        total_idle_usdc=0.0,
        total_usd=total,
        total_usdc_usd="0",
    )


def test_a_sell_funds_a_buy_on_the_same_chain_with_no_idle_at_all() -> None:
    """A7 end to end: the wallet holds zero USDC, and the buy still gets a source."""
    current = bare_positions(
        chain_holding("vault-a", "20", 8453),
        chain_holding("vault-b", "80", 8453),
    )
    # vault-a 20 -> 10 (sell 10), vault-b unchanged, vault-c 0 -> 10 (buy 10)
    target = allocation(("vault-a", 0.1), ("vault-b", 0.8), ("vault-c", 0.1))
    client = BalanceAwareClient(
        [response("0xsell")],
        [response("0xbuy")],
        idle={},
        sell_credits={"vault-a": (8453, 10.0)},
    )

    execute_rebalance(
        client,
        MockSigner(),
        current,
        target,
        policy(autonomous_rebalance=True),
        known_instruments=known("vault-a", "vault-b", "vault-c"),
        config=Config(),
        confirm=True,
    )

    assert len(client.buy_bodies) == 1, "one chain covers it, so one op"
    body = client.buy_bodies[0]
    assert body["sourceChainId"] == 8453, "the chain the sell proceeds landed on"
    assert float(body["amountUsdc"]) == pytest.approx(10.0)


def test_a_buy_splits_into_several_ops_when_no_single_chain_covers_it() -> None:
    current = bare_positions(
        chain_holding("vault-a", "50", 8453),
        chain_holding("vault-b", "50", 42161),
    )
    # both sell 10; vault-c buys 20 — more than either chain frees on its own
    target = allocation(("vault-a", 0.4), ("vault-b", 0.4), ("vault-c", 0.2))
    client = BalanceAwareClient(
        [response("0xsell-a"), response("0xsell-b")],
        [response("0xbuy-1"), response("0xbuy-2")],
        idle={},
        sell_credits={"vault-a": (8453, 10.0), "vault-b": (42161, 10.0)},
    )

    execute_rebalance(
        client,
        MockSigner(),
        current,
        target,
        policy(autonomous_rebalance=True),
        known_instruments=[
            chain_vault("vault-a", 8453),
            chain_vault("vault-b", 42161),
            chain_vault("vault-c", 10),
        ],
        config=Config(),
        confirm=True,
    )

    assert len(client.buy_bodies) == 2, "two funding chains, two ops"
    assert {b["sourceChainId"] for b in client.buy_bodies} == {8453, 42161}
    assert sum(float(b["amountUsdc"]) for b in client.buy_bodies) == pytest.approx(20.0)


def test_split_buy_ops_get_distinct_idempotency_keys() -> None:
    """Two ops for one leg must not collapse onto one key, or a retry drops one."""
    store: dict[str, object] = {}
    current = bare_positions(
        chain_holding("vault-a", "50", 8453),
        chain_holding("vault-b", "50", 42161),
    )
    target = allocation(("vault-a", 0.4), ("vault-b", 0.4), ("vault-c", 0.2))
    client = BalanceAwareClient(
        [response("0xsell-a"), response("0xsell-b")],
        [response("0xbuy-1"), response("0xbuy-2")],
        idle={},
        sell_credits={"vault-a": (8453, 10.0), "vault-b": (42161, 10.0)},
    )

    execute_rebalance(
        client,
        MockSigner(),
        current,
        target,
        policy(autonomous_rebalance=True),
        known_instruments=[
            chain_vault("vault-a", 8453),
            chain_vault("vault-b", 42161),
            chain_vault("vault-c", 10),
        ],
        config=Config(),
        idempotency_store=store,
        confirm=True,
    )

    buy_keys = [key for key in store if "src" in str(key)]
    assert len(buy_keys) == len(set(buy_keys)) == 2


def test_split_buy_ops_log_their_own_share_deltas_and_usd() -> None:
    class SettlingSplitClient(BalanceAwareClient):
        settled = iter(("4", "10"))

        def positions(self, body: dict[str, object]) -> dict[str, Any]:
            assert body["chainId"] == 10, "read the destination, not funding chain"
            shares = next(self.settled)
            return {"positions": [{"instrumentId": "vault-c", "shareBalance": shares}]}

    backend = MemoryStateBackend()
    current = bare_positions(
        chain_holding("vault-a", "50", 8453),
        chain_holding("vault-b", "50", 42161),
    )
    client = SettlingSplitClient(
        [response("0xsell-a"), response("0xsell-b")],
        [response("0xbuy-1"), response("0xbuy-2")],
        idle={},
        sell_credits={"vault-a": (8453, 10.0), "vault-b": (42161, 10.0)},
    )

    execute_rebalance(
        client,
        MockSigner(),
        current,
        allocation(("vault-a", 0.4), ("vault-b", 0.4), ("vault-c", 0.2)),
        policy(autonomous_rebalance=True),
        known_instruments=[
            chain_vault("vault-a", 8453),
            chain_vault("vault-b", 42161),
            chain_vault("vault-c", 10),
        ],
        config=StateConfig(state_backend=backend),
        idempotency_store={},
        confirm=True,
    )

    buys = [entry for entry in backend.log if entry.action_type == "buy"]
    assert [(entry.usd, entry.shares) for entry in buys] == [(10.0, "4"), (10.0, "6")]
    assert all(entry.basis == "derived" for entry in buys)


def test_a_buy_too_big_for_one_round_comes_back_for_the_remainder() -> None:
    """Sell, buy what fits, discover it is still short, do another op.

    The venue only credits half the sell before the first buy round, so the leg
    can only be part-filled; the second round picks up what landed since.
    """
    current = bare_positions(
        chain_holding("vault-a", "20", 8453),
        chain_holding("vault-b", "80", 8453),
    )
    target = allocation(("vault-a", 0.1), ("vault-b", 0.8), ("vault-c", 0.1))

    class DripClient(BalanceAwareClient):
        """Half the proceeds land immediately, the rest on the next look."""

        def balances(self, address: str) -> dict[str, Any]:
            seen = super().balances(address)
            self.idle[8453] = self.idle.get(8453, 0.0) + 4.0
            return seen

    client = DripClient(
        [response("0xsell")],
        [response("0xbuy-1"), response("0xbuy-2"), response("0xbuy-3")],
        idle={},
        sell_credits={"vault-a": (8453, 6.0)},
    )

    report = execute_rebalance(
        client,
        MockSigner(),
        current,
        target,
        policy(autonomous_rebalance=True),
        known_instruments=known("vault-a", "vault-b", "vault-c"),
        config=Config(),
        confirm=True,
    )

    assert len(client.buy_bodies) >= 2, "one round could not fill it"
    total = sum(float(body["amountUsdc"]) for body in client.buy_bodies)
    assert total == pytest.approx(10.0), "the rounds add up to the whole leg"
    assert report.status == "success"


def test_a_rebalance_that_cannot_be_funded_reports_what_is_outstanding() -> None:
    """No progress ends the loop; the shortfall is stated, not spun on."""
    current = bare_positions(
        chain_holding("vault-a", "20", 8453),
        chain_holding("vault-b", "80", 8453),
    )
    target = allocation(("vault-a", 0.1), ("vault-b", 0.8), ("vault-c", 0.1))
    client = BalanceAwareClient(
        [response("0xsell")],
        [response("0xbuy")],
        idle={8453: 2.0},
        sell_credits={},  # the sell never settles
    )

    report = execute_rebalance(
        client,
        MockSigner(),
        current,
        target,
        policy(autonomous_rebalance=True),
        known_instruments=known("vault-a", "vault-b", "vault-c"),
        config=Config(),
        confirm=True,
    )

    assert report.in_progress is True
    assert any("outstanding" in message for message in report.messages)
    assert len(client.buy_bodies) == 1, "one partial op, then it stops"
    assert float(client.buy_bodies[0]["amountUsdc"]) == pytest.approx(2.0)


def test_the_paymaster_reserve_is_held_back_from_every_chain() -> None:
    """Gas is paid in USDC from this balance, so it cannot all be deployed."""
    current = bare_positions(
        chain_holding("vault-a", "20", 8453),
        chain_holding("vault-b", "80", 8453),
    )
    target = allocation(("vault-a", 0.1), ("vault-b", 0.8), ("vault-c", 0.1))
    client = BalanceAwareClient(
        [response("0xsell")],
        [response("0xbuy")],
        idle={},
        sell_credits={"vault-a": (8453, 10.0)},
    )

    @dataclass(frozen=True)
    class ReservedConfig(Config):
        paymaster_reserve_usd: float = 0.75

    execute_rebalance(
        client,
        MockSigner(),
        current,
        target,
        policy(autonomous_rebalance=True),
        known_instruments=known("vault-a", "vault-b", "vault-c"),
        config=ReservedConfig(),
        confirm=True,
    )

    spent = float(client.buy_bodies[0]["amountUsdc"])
    assert spent == pytest.approx(9.25), "$10 freed, $0.75 kept back for gas"


def test_the_reserve_is_a_floor_on_the_wallet_not_a_toll_on_each_sell() -> None:
    ledger = FundingLedger({8453: 5.0}, reserve_usd=0.75)

    assert ledger.available[8453] == pytest.approx(4.25)
    ledger.credit(8453, 10.0)
    assert ledger.available[8453] == pytest.approx(14.25), "charged once, not twice"


# --- calldata API (ONE_TX_TRANSACTION_API=calldata) --------------------------

FIXTURES = Path(__file__).parent / "fixtures"
USDC_BY_CHAIN = {
    8453: "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    42161: "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
}
WITHDRAW_KINDS = ["withdraw"]
DEPOSIT_KINDS = ["approve", "swap", "approve", "approve", "deposit"]


def calldata_vault(instrument_id: str, chain_id: int = 8453) -> Vault:
    return chain_vault(instrument_id, chain_id).model_copy(
        update={"token_address": USDC_BY_CHAIN[chain_id], "token_decimals": 6}
    )


@dataclass
class CalldataRebalanceClient:
    """1Tx calldata for rebalance trades, echoing each request's amount.

    A withdrawal pays out ``proceeds[instrument]`` raw USDC as its ``minOut``
    (or ``expectedOut`` when ``quoted_min`` is false); a deposit requires
    exactly what it was asked to spend.
    """

    chains: dict[str, int]
    proceeds: dict[str, int] = field(default_factory=dict)
    quoted_min: bool = True
    shares: dict[str, str] = field(default_factory=dict)
    requests: list[tuple[str, InstrumentCalldataQuery]] = field(default_factory=list)

    def instrument_calldata(
        self,
        instrument_id: str,
        query: InstrumentCalldataQuery,
    ) -> InstrumentCalldataResponse:
        self.requests.append((instrument_id, query))
        chain_id = self.chains[instrument_id]
        usdc = USDC_BY_CHAIN[chain_id]
        if query.action == "withdraw":
            payload = fixture("calldata-instrument-withdraw-max.json")
            payload["tokenOut"]["address"] = usdc
            out = str(self.proceeds[instrument_id])
            payload["expectedOut"] = out
            payload["minOut"] = out if self.quoted_min else None
        else:
            payload = fixture("calldata-instrument-deposit-swap.json")
            payload["tokenIn"]["address"] = usdc
            payload["requires"] = [{"token": usdc, "amount": query.amount}]
            payload["leftovers"] = []
        payload.update(
            instrumentId=instrument_id,
            account=query.account,
            chainId=chain_id,
            amountIn=query.amount,
            expiresAt=None,
        )
        for call in payload["calls"]:
            call["chainId"] = chain_id
        return InstrumentCalldataResponse.model_validate(payload)

    def positions(self, _body: dict[str, object]) -> dict[str, Any]:
        return {
            "positions": [
                {"instrumentId": instrument_id, "shareBalance": balance}
                for instrument_id, balance in self.shares.items()
            ]
        }

    def build_sell(self, body: dict[str, object]) -> dict[str, Any]:
        raise AssertionError("the calldata path must not call the legacy builder")

    def build_buy(self, body: dict[str, object]) -> dict[str, Any]:
        raise AssertionError("the calldata path must not call the legacy builder")

    def deposits(self) -> list[tuple[str, str]]:
        return [
            (instrument_id, query.amount)
            for instrument_id, query in self.requests
            if query.action == "deposit"
        ]


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def usdc_reader(idle: dict[int, int]) -> object:
    def read(chain_id: int, _rpc: str, token: str, _account: str) -> int:
        if token.casefold() == USDC_BY_CHAIN[chain_id].casefold():
            return idle.get(chain_id, 0)
        return 10**30  # the position tokens being withdrawn

    return read


@dataclass(frozen=True)
class CalldataConfig(Config):
    transaction_api: str = "calldata"
    slippage_bps: int = 30
    token_balance_reader: object = field(default_factory=lambda: usdc_reader({}))


@dataclass(frozen=True)
class CalldataStateConfig(CalldataConfig):
    state_backend: object = field(default_factory=MemoryStateBackend)
    position_settlement_attempts: int = 1


@dataclass
class BatchingSigner(MockSigner):
    batches: list[tuple[TxStep, ...]] = field(default_factory=list)
    pending: bool = False

    def send_batch(self, steps: tuple[TxStep, ...], rpc_url: str) -> Receipt:
        self.batches.append(tuple(steps))
        index = len(self.batches)
        return Receipt(
            transaction_hash=f"0x{index:064x}",
            block_number=0 if self.pending else index,
            gas_used=0 if self.pending else 21_000,
            status=0 if self.pending else 1,
            from_address=ADDRESS,
            pending=self.pending,
            execution_status="safe_proposed" if self.pending else "mined",
        )


@dataclass
class PaymasterSigner(BatchingSigner):
    charge: int = 12_345
    prepared: list[tuple[TxStep, ...]] = field(default_factory=list)

    def prepare_batch(
        self, steps: tuple[TxStep, ...], rpc_url: str
    ) -> PreparedUserOperation:
        self.prepared.append(tuple(steps))
        return PreparedUserOperation(
            sender=ADDRESS,
            chain_id=steps[0].chain_id,
            entry_point="0x0000000071727De22E5E9d8BAf0edAc6f37da032",
            user_operation={"sender": ADDRESS, "signature": "0xstub"},
            deployed=True,
            gas=UserOperationGas(
                call_gas_limit=900_000,
                verification_gas_limit=450_000,
                pre_verification_gas=60_000,
                max_fee_per_gas=1_000_000,
                max_priority_fee_per_gas=1_000,
            ),
            paymaster=PaymasterTokenQuote(
                paymaster="0x777777777777AeC03fd955926DbF81597e66834C",
                token=USDC_BY_CHAIN[steps[0].chain_id],
                exchange_rate=10**18,
                post_op_gas=5_000,
                approval_included=True,
            ),
            max_gas_token_charge_raw=str(self.charge),
        )


def rotation() -> tuple[Positions, Allocation, CalldataRebalanceClient]:
    """Base only: sell $30 of vault-a, buy $30 of vault-b, no idle USDC."""
    return (
        bare_positions(holding("vault-a", "60"), holding("vault-b", "40")),
        allocation(("vault-a", 0.3), ("vault-b", 0.7)),
        CalldataRebalanceClient(
            chains={"vault-a": 8453, "vault-b": 8453},
            proceeds={"vault-a": 30_000_000},
        ),
    )


def run_calldata(
    client: CalldataRebalanceClient,
    signer: object,
    current: Positions,
    target: Allocation,
    *,
    confirm: bool = False,
    config: CalldataConfig | None = None,
    store: object | None = None,
) -> Any:
    return execute_rebalance(
        client,
        signer,  # type: ignore[arg-type]
        current,
        target,
        policy(),
        confirm=confirm,
        known_instruments=[
            calldata_vault(instrument_id, chain_id)
            for instrument_id, chain_id in client.chains.items()
        ],
        config=config or CalldataConfig(),
        idempotency_store=store,
    )


def test_calldata_rebalance_sell_proceeds_fund_a_buy_on_the_same_chain() -> None:
    current, target, client = rotation()

    report = run_calldata(client, BatchingSigner(), current, target)

    assert report.status == "planned"
    assert [(item, query.action, query.amount) for item, query in client.requests] == [
        ("vault-a", "withdraw", "30000000"),
        ("vault-b", "deposit", "30000000"),
    ]
    assert [bundle.bundle_id for bundle in report.plan.bundles] == [
        "leg:0:vault-a:withdraw",
        "leg:1:vault-b:deposit",
    ]
    [usdc] = [item for item in report.funding if item.token == USDC_BY_CHAIN[8453]]
    assert usdc.ok, "the withdrawal's minimum proceeds cover the deposit"
    assert usdc.available_raw == "0"
    assert not any("short by" in message for message in report.messages)
    payload = report.plan.model_dump(mode="json")
    assert validate(payload, "tx-plan") == payload


def test_calldata_rebalance_is_one_sell_before_buy_operation_per_chain() -> None:
    current, target, client = rotation()
    signer = BatchingSigner()
    store: dict[str, object] = {}

    report = run_calldata(client, signer, current, target, confirm=True, store=store)

    assert report.status == "success"
    [batch] = signer.batches
    assert [step.kind for step in batch] == WITHDRAW_KINDS + DEPOSIT_KINDS
    assert batch == report.plan.steps
    assert signer.sent == []
    assert {"leg:0:vault-a", "leg:1:vault-b"} <= set(store)

    rerun = run_calldata(client, signer, current, target, confirm=True, store=store)

    assert len(client.requests) == 2, "a submitted trade is never rebuilt"
    assert len(signer.batches) == 1
    assert rerun.plan.steps == ()


def test_calldata_rebalance_on_two_chains_keeps_each_chain_to_one_operation() -> None:
    current = bare_positions(
        holding("vault-a", "30"),
        chain_holding("vault-c", "30", 42161),
        holding("vault-e", "40"),
    )
    # Base: vault-a 30 -> 10, vault-b 0 -> 20. Arbitrum: vault-c 30 -> 10,
    # vault-d 0 -> 20. vault-e is unchanged.
    target = allocation(
        ("vault-a", 0.1),
        ("vault-b", 0.2),
        ("vault-c", 0.1),
        ("vault-d", 0.2),
        ("vault-e", 0.4),
    )
    client = CalldataRebalanceClient(
        chains={
            "vault-a": 8453,
            "vault-b": 8453,
            "vault-c": 42161,
            "vault-d": 42161,
            "vault-e": 8453,
        },
        proceeds={"vault-a": 20_000_000, "vault-c": 20_000_000},
    )
    signer = BatchingSigner()

    report = run_calldata(client, signer, current, target, confirm=True)

    assert report.status == "success"
    assert [
        ({step.chain_id for step in batch}, [step.kind for step in batch])
        for batch in signer.batches
    ] == [
        ({8453}, WITHDRAW_KINDS + DEPOSIT_KINDS),
        ({42161}, WITHDRAW_KINDS + DEPOSIT_KINDS),
    ]


def test_calldata_rebalance_sizes_a_buy_to_the_sells_conservative_proceeds() -> None:
    current, target, client = rotation()
    # No quoted minimum: the $30 withdrawal counts for 30 USDC less 30 bps.
    client.quoted_min = False

    report = run_calldata(client, BatchingSigner(), current, target)

    assert client.deposits() == [("vault-b", "29910000")]
    assert any("sized to 29.91 of 30 USDC" in message for message in report.messages)
    assert all(item.ok for item in report.funding)


def test_calldata_rebalance_leaves_the_paymasters_charge_out_of_the_deposit() -> None:
    current, target, client = rotation()
    signer = PaymasterSigner(charge=12_345)

    report = run_calldata(client, signer, current, target)

    assert client.deposits() == [("vault-b", "30000000"), ("vault-b", "29987655")]
    [usdc] = [item for item in report.funding if item.token == USDC_BY_CHAIN[8453]]
    assert usdc.ok
    assert usdc.includes_gas_charge
    assert report.plan.bundles[-1].amount == "29987655"
    assert any("maximum gas charge" in message for message in report.messages)


def test_calldata_rebalance_buys_cannot_exceed_what_the_chain_holds() -> None:
    # vault-b 0 -> 50 deploys the $50 the snapshot shows idle, but the Safe now
    # holds only $10 of it and sells nothing to make up the rest.
    current = positions_snapshot(holding("vault-a", "50"), idle_usdc="50")
    target = allocation(("vault-a", 0.5), ("vault-b", 0.5))
    client = CalldataRebalanceClient(chains={"vault-a": 8453, "vault-b": 8453})
    config = CalldataConfig(token_balance_reader=usdc_reader({8453: 10_000_000}))
    signer = BatchingSigner()

    planned = run_calldata(client, signer, current, target, config=config)

    assert client.deposits() == [("vault-b", "50000000")], "not quietly shrunk"
    [usdc] = planned.funding
    assert usdc.shortfall_raw == "40000000"
    assert any("short by 40000000" in message for message in planned.messages)

    with pytest.raises(UnderfundedPlanError):
        run_calldata(client, signer, current, target, confirm=True, config=config)
    assert signer.batches == []


@pytest.mark.parametrize("confirm", [False, True])
def test_calldata_rebalance_across_chains_is_refused_while_bridging_is_off(
    confirm: bool,
) -> None:
    # Sell $50 on Arbitrum to buy $50 on Base: the proceeds would have to bridge.
    current = bare_positions(chain_holding("vault-a", "100", 42161))
    target = allocation(("vault-a", 0.5), ("vault-b", 0.5))
    client = CalldataRebalanceClient(
        chains={"vault-a": 42161, "vault-b": 8453},
        proceeds={"vault-a": 50_000_000},
    )
    signer = BatchingSigner()

    with pytest.raises(CalldataUnsupportedError, match="cross-chain"):
        run_calldata(client, signer, current, target, confirm=confirm)

    assert client.deposits() == []
    assert signer.batches == []
    assert signer.sent == []


def test_calldata_rebalance_records_the_shares_a_confirmed_buy_received() -> None:
    current, target, client = rotation()
    client.shares = {"vault-b": "70"}
    config = CalldataStateConfig()

    report = run_calldata(
        client, BatchingSigner(), current, target, confirm=True, config=config
    )

    assert report.status == "success"
    sell, buy = config.state_backend.log  # type: ignore[attr-defined]
    assert (sell.action_type, sell.usd, sell.shares) == ("sell", 30.0, "30")
    assert (buy.action_type, buy.usd, buy.shares) == ("buy", 30.0, "30")


def test_calldata_rebalance_does_not_log_shares_for_an_unincluded_buy() -> None:
    current, target, client = rotation()
    client.shares = {"vault-b": "70"}
    config = CalldataStateConfig()

    report = run_calldata(
        client,
        BatchingSigner(pending=True),
        current,
        target,
        confirm=True,
        config=config,
    )

    assert report.status == "in_progress"
    _sell, buy = config.state_backend.log  # type: ignore[attr-defined]
    assert (buy.usd, buy.shares) == (30.0, None)
    assert any("cost basis is not yet observable" in m for m in report.messages)
