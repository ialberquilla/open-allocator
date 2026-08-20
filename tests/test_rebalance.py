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
from open_allocator.exec.execute import (
    ExecutionBroadcastError,
    FundingLedger,
    GasCheck,
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
