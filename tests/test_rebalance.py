from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from open_allocator.core.checkpoint import (
    AllocationLogEntry,
    Checkpoint,
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


def test_plan_rebalance_survives_a_zero_raw_balance_against_a_live_value() -> None:
    # A venue that reports balance_raw as "0" against a live usd_value must not
    # fail the plan; the sell simply carries no calldata amount.
    zero_raw = holding("vault-a", "80").model_copy(update={"balance_raw": "0"})
    current = positions_snapshot(zero_raw, holding("vault-b", "20"))

    plan = plan_rebalance(
        current,
        allocation(("vault-a", 0.5), ("vault-b", 0.5)),
        policy(),
        known_instruments=known("vault-a", "vault-b"),
    )

    sell = plan.trades[0]
    assert (sell.action, sell.instrument_id) == ("sell", "vault-a")
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
    client = CalldataRebalanceClient(chains={"vault-a": 8453, "vault-b": 8453})
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
            config=CalldataConfig(),
        )

    assert signer.address_calls == 0
    assert client.requests == []


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


# --- calldata fixtures -------------------------------------------------------

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
