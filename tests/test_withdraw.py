from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from open_allocator.core.checkpoint import read_allocation_log
from open_allocator.core.positions import IdleBalance, PositionHolding, Positions
from open_allocator.core.schema import validate
from open_allocator.core.types import (
    Policy,
    PolicyAllowed,
    PolicyCaps,
    PolicyGates,
    PolicyWallet,
    TxStep,
)
from open_allocator.core.withdraw import calldata_withdraw_amount, plan_withdraw
from open_allocator.exec.bundle_execution import UnderfundedPlanError
from open_allocator.exec.calldata import (
    CalldataUnsupportedError,
    CalldataValidationError,
)
from open_allocator.exec.client import (
    InstrumentCalldataQuery,
    InstrumentCalldataResponse,
)
from open_allocator.exec.execute import GasCheck
from open_allocator.exec.signer import Receipt
from open_allocator.exec.withdraw import withdraw

ADDRESS = "0x0000000000000000000000000000000000000001"


@dataclass
class MockWithdrawClient:
    responses: list[dict[str, Any]]
    sell_bodies: list[dict[str, object]] = field(default_factory=list)

    def build_sell(self, body: dict[str, object]) -> dict[str, Any]:
        self.sell_bodies.append(body)
        return self.responses.pop(0)


@dataclass
class MockSigner:
    sent: list[tuple[TxStep, str]] = field(default_factory=list)

    def address(self) -> str:
        return ADDRESS

    def send(self, tx: TxStep, rpc_url: str) -> Receipt:
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
    _allocation_log_path: Path | None = None

    @property
    def allocation_log_path(self) -> Path | None:
        return self._allocation_log_path


def holding(
    *,
    instrument_id: str = "base-aave-usdc",
    usd_value: str = "100",
    share_balance: str = "74.999123",
    share_decimals: int = 6,
) -> PositionHolding:
    return PositionHolding(
        instrument_id=instrument_id,
        protocol="aave",
        chain_id=8453,
        symbol="USDC",
        balance=usd_value,
        balance_raw=str(int(float(usd_value) * 1_000_000)),
        decimals=6,
        usd_value=float(usd_value),
        share_balance=share_balance,
        share_balance_raw=str(int(float(share_balance) * (10**share_decimals))),
        share_decimals=share_decimals,
        yield_token_symbol="aUSDC",
        yield_token_address="0x0000000000000000000000000000000000000002",
    )


def positions_snapshot(position: PositionHolding) -> Positions:
    return Positions(
        address=ADDRESS,
        holdings=(position,),
        idle_balances=(
            IdleBalance(
                chain_id=8453,
                chain_name="Base",
                usdc_balance="0.000000",
                usdc_balance_raw="0",
                usd_value=0,
            ),
        ),
        total_position_usd=position.usd_value,
        total_idle_usdc=0,
        total_usd=position.usd_value,
        total_usdc_usd="0.000000",
    )


def permissive_policy() -> Policy:
    return Policy(
        wallet=PolicyWallet(mode="self-custody", signer="local-eoa"),
        allowed=PolicyAllowed(
            protocols=None,
            chains=None,
            assets=("USDC",),
            curators=None,
        ),
        caps=PolicyCaps(
            max_weight_per_instrument=1,
            max_weight_per_protocol=1,
            max_weight_per_curator=1,
            max_weight_per_chain=1,
            min_instrument_tvl_usd=1,
            max_reward_dependence=1,
        ),
        gates=PolicyGates(
            new_instrument_needs_approval=True,
            autonomous_rebalance=False,
            max_deploy_per_cycle_usd=1_000_000,
        ),
    )


def sell_response(**extra: object) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "transactions": [
            {
                "to": "0x0000000000000000000000000000000000000002",
                "data": "0xsell",
                "value": 0,
                "chainId": 8453,
            }
        ]
    }
    payload.update(extra)
    return payload


def test_a_confirmed_exit_logs_the_price_it_sold_at(tmp_path: Path) -> None:
    """The cost-basis wiring, end to end.

    A withdraw is the one action whose share price is known exactly at write
    time -- `WithdrawPlan.share_price_usd` is quoted from the position itself
    -- so the log must carry it rather than recording bare share counts and
    leaving the dollars to be guessed later. There is no backfill for this.
    """
    position = holding(usd_value="100", share_balance="80")
    log_path = tmp_path / "allocation-log.jsonl"

    report = withdraw(
        MockWithdrawClient([sell_response(expectedUsdc="99.50")]),
        MockSigner(),
        position,
        permissive_policy(),
        confirm=True,
        config=Config(_allocation_log_path=log_path),
    )

    entry = read_allocation_log(log_path=log_path)[0]

    assert report.status == "success"
    assert entry.action_type == "withdraw"
    assert entry.shares == "80"
    assert entry.share_price == "1.25"
    assert entry.basis == "quoted"
    # And the dollars follow from the two, so an exit no longer lands in the
    # log without a value and get skipped by reconciliation.
    assert entry.usd == pytest.approx(100.0)


def test_full_exit_sends_exact_share_balance_as_yield_token_amount() -> None:
    position = holding(share_balance="74.999123")
    client = MockWithdrawClient([sell_response(expectedUsdc="99.50")])
    signer = MockSigner()

    report = withdraw(
        client,
        signer,
        position,
        permissive_policy(),
        confirm=True,
        config=Config(),
    )

    assert report.status == "success"
    assert report.withdraw_plan.full_exit is True
    assert report.withdraw_plan.yield_token_amount == "74.999123"
    assert client.sell_bodies == [
        {
            "userAddress": ADDRESS,
            "instrumentId": "base-aave-usdc",
            "yieldTokenAmount": "74.999123",
        }
    ]
    assert report.sell.expected_usdc == "99.50"
    assert [sent[0].kind for sent in signer.sent] == ["sell"]


def test_partial_exit_converts_usd_to_rounded_down_shares_without_oversell() -> None:
    position = holding(usd_value="10", share_balance="3.000000")

    plan = plan_withdraw(position, permissive_policy(), amount=9.999999)

    assert plan.full_exit is False
    assert plan.yield_token_amount == "2.999999"

    client = MockWithdrawClient([sell_response()])
    withdraw(
        client,
        MockSigner(),
        position,
        permissive_policy(),
        amount=9.999999,
        confirm=True,
        config=Config(),
    )

    assert client.sell_bodies[0]["yieldTokenAmount"] == "2.999999"
    assert client.sell_bodies[0]["yieldTokenAmount"] != position.share_balance


def test_sell_payload_has_no_usd_denominated_amount_regression() -> None:
    client = MockWithdrawClient([sell_response()])

    withdraw(
        client,
        MockSigner(),
        holding(),
        permissive_policy(),
        amount=25,
        confirm=True,
        config=Config(),
    )

    body = client.sell_bodies[0]
    assert set(body) == {"userAddress", "instrumentId", "yieldTokenAmount"}
    assert not {
        "amount",
        "amountUsd",
        "amountUsdc",
        "sellAmount",
        "usd",
        "usdc",
    } & set(body)


def test_without_confirm_returns_plan_and_does_not_send() -> None:
    client = MockWithdrawClient([sell_response()])
    signer = MockSigner()

    report = withdraw(
        client,
        signer,
        holding(),
        permissive_policy(),
        amount=25,
        confirm=False,
        config=Config(),
    )

    assert report.status == "planned"
    assert report.messages == ("dry-run only; no transactions broadcast",)
    assert client.sell_bodies[0]["yieldTokenAmount"] == "18.74978"
    assert signer.sent == []


def test_withdraw_composes_with_position_holding_output_shape() -> None:
    position = holding(instrument_id="vault-from-positions")
    payload = positions_snapshot(position).model_dump(mode="json")
    selected = payload["holdings"][0]
    client = MockWithdrawClient([sell_response()])

    report = withdraw(
        client,
        MockSigner(),
        selected,
        permissive_policy().model_dump(mode="json"),
        confirm=True,
        config=Config(),
    )

    assert report.withdraw_plan.instrument_id == "vault-from-positions"
    assert client.sell_bodies[0]["yieldTokenAmount"] == position.share_balance


def test_zero_rounding_partial_withdraw_is_rejected() -> None:
    with pytest.raises(ValueError, match="zero yield-token shares"):
        plan_withdraw(
            holding(usd_value="100", share_balance="1", share_decimals=0),
            permissive_policy(),
            amount="0.99",
        )


def recipe_holding(
    *,
    balance: str,
    balance_raw: str | None,
    decimals: int | None,
    share_balance: str,
    share_balance_raw: str,
    share_decimals: int,
    symbol: str = "USDC",
) -> PositionHolding:
    return PositionHolding(
        instrument_id="recipe-vault",
        protocol="protocol",
        chain_id=8453,
        symbol=symbol,
        balance=balance,
        balance_raw=balance_raw,
        decimals=decimals,
        usd_value=float(balance),
        share_balance=share_balance,
        share_balance_raw=share_balance_raw,
        share_decimals=share_decimals,
    )


# Positions whose shares and underlying differ in price and decimals.
RECIPES = {
    "erc4626-18-decimal-shares": recipe_holding(
        balance="16.8213",
        balance_raw="16821300",
        decimals=6,
        share_balance="16.356116850925590939",
        share_balance_raw="16356116850925590939",
        share_decimals=18,
    ),
    "erc4626-appreciated-shares": recipe_holding(
        balance="13.391423",
        balance_raw="13391423",
        decimals=6,
        share_balance="9.816138",
        share_balance_raw="9816138",
        share_decimals=6,
    ),
    "aave-atoken": recipe_holding(
        balance="40.123456",
        balance_raw="40123456",
        decimals=6,
        share_balance="40.123456",
        share_balance_raw="40123456",
        share_decimals=6,
    ),
    "comet": recipe_holding(
        balance="25.000001",
        balance_raw="25000001",
        decimals=6,
        share_balance="25.000001",
        share_balance_raw="25000001",
        share_decimals=6,
    ),
}


@pytest.mark.parametrize("recipe", sorted(RECIPES))
def test_full_exit_sends_max_for_every_recipe(recipe: str) -> None:
    position = RECIPES[recipe]

    for plan in (
        plan_withdraw(position, permissive_policy()),
        plan_withdraw(position, permissive_policy(), amount=position.balance),
        plan_withdraw(position, permissive_policy(), amount="1000000"),
    ):
        assert plan.full_exit is True
        assert plan.calldata_amount == "max"
        assert calldata_withdraw_amount(plan) == "max"


@pytest.mark.parametrize(
    ("recipe", "amount", "raw_assets"),
    [
        # floor(16821300 * 5 / 16.8213)
        ("erc4626-18-decimal-shares", "5", "5000000"),
        # floor(13391423 * 10 / 13.391423)
        ("erc4626-appreciated-shares", "10", "10000000"),
        # floor(40123456 * 0.333333 / 40.123456)
        ("aave-atoken", "0.333333", "333333"),
        # floor(25000001 * 12.5 / 25.000001) — rounds down, never up
        ("comet", "12.5", "12500000"),
    ],
)
def test_partial_exit_sends_raw_underlying_units_not_shares(
    recipe: str,
    amount: str,
    raw_assets: str,
) -> None:
    position = RECIPES[recipe]

    plan = plan_withdraw(position, permissive_policy(), amount=amount)

    assert plan.full_exit is False
    assert plan.calldata_amount == raw_assets
    assert calldata_withdraw_amount(plan) == raw_assets
    assert plan.underlying_decimals == position.decimals
    # The share estimate is kept for accounting, but is not the calldata amount.
    assert plan.yield_token_amount != raw_assets
    assert plan.calldata_amount != position.share_balance_raw


def test_partial_exit_of_an_18_decimal_underlying_is_exact() -> None:
    position = recipe_holding(
        balance="1.5",
        balance_raw="1500000000000000000",
        decimals=18,
        share_balance="1.4",
        share_balance_raw="1400000000000000000",
        share_decimals=18,
        symbol="WETH",
    )

    plan = plan_withdraw(position, permissive_policy(), amount="0.1")

    assert plan.calldata_amount == "100000000000000000"


def test_partial_exit_rounds_underlying_units_down_without_overdraw() -> None:
    position = recipe_holding(
        balance="3",
        balance_raw="3000001",
        decimals=6,
        share_balance="3",
        share_balance_raw="3000000",
        share_decimals=6,
    )

    plan = plan_withdraw(position, permissive_policy(), amount="1")

    # 3000001 / 3 = 1000000.33…
    assert plan.calldata_amount == "1000000"


@pytest.mark.parametrize(
    ("balance_raw", "decimals"),
    [(None, 6), ("3000000", None)],
)
def test_partial_exit_without_raw_underlying_fails_closed(
    balance_raw: str | None,
    decimals: int | None,
) -> None:
    position = recipe_holding(
        balance="3",
        balance_raw=balance_raw,
        decimals=decimals,
        share_balance="3",
        share_balance_raw="3000000",
        share_decimals=6,
    )

    plan = plan_withdraw(position, permissive_policy(), amount="1")

    assert plan.calldata_amount is None
    with pytest.raises(ValueError, match="share amount must never be sent"):
        calldata_withdraw_amount(plan)
    # A full exit needs no raw balance: ``max`` withdraws whatever is held.
    assert calldata_withdraw_amount(plan_withdraw(position, permissive_policy())) == (
        "max"
    )


def test_partial_exit_plans_against_a_zero_raw_balance() -> None:
    # Planning runs before either transaction API is chosen: a venue reporting
    # balance_raw as "0" against a live usd_value must still produce a legacy
    # plan, which redeems shares and never reads calldata_amount.
    position = recipe_holding(
        balance="3",
        balance_raw="0",
        decimals=6,
        share_balance="3",
        share_balance_raw="3000000",
        share_decimals=6,
    )

    plan = plan_withdraw(position, permissive_policy(), amount="1")

    assert plan.yield_token_amount == "1"
    assert plan.calldata_amount is None
    with pytest.raises(ValueError, match="share amount must never be sent"):
        calldata_withdraw_amount(plan)


@dataclass
class PendingWithdrawSigner:
    """A Safe below its threshold: the exit is proposed, never broadcast."""

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


def test_a_proposed_exit_is_not_reported_as_a_completed_withdrawal() -> None:
    """Nothing has been redeemed until the co-signers execute it."""
    report = withdraw(
        MockWithdrawClient([sell_response(expectedUsdc="99.50")]),
        PendingWithdrawSigner(),
        holding(share_balance="74.999123"),
        permissive_policy(),
        confirm=True,
        config=Config(),
    )

    assert report.status == "in_progress"
    assert report.in_progress is True
    assert any("awaiting threshold" in message for message in report.messages)


# --- calldata API (ONE_TX_TRANSACTION_API=calldata) --------------------------

BASE_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
CALLDATA_FIXTURES = Path(__file__).parent / "fixtures"


def swapped_withdraw_calls() -> list[dict[str, Any]]:
    """A non-USDC vault: withdraw the asset, approve it, swap it to USDC."""
    return [
        {
            "to": "0x3333333333333333333333333333333333333333",
            "data": "0xb460af94",
            "value": "0",
            "chainId": 8453,
            "type": "withdraw",
        },
        {
            "to": "0x5555555555555555555555555555555555555555",
            "data": "0x095ea7b3",
            "value": "0",
            "chainId": 8453,
            "type": "approve",
        },
        {
            "to": "0x4444444444444444444444444444444444444444",
            "data": "0x04e45aaf",
            "value": "0",
            "chainId": 8453,
            "type": "swap",
        },
    ]


@dataclass
class MockCalldataWithdrawClient:
    overrides: dict[str, Any] = field(default_factory=dict)
    requests: list[tuple[str, InstrumentCalldataQuery]] = field(default_factory=list)

    def instrument_calldata(
        self,
        instrument_id: str,
        query: InstrumentCalldataQuery,
    ) -> InstrumentCalldataResponse:
        self.requests.append((instrument_id, query))
        payload = json.loads(
            (CALLDATA_FIXTURES / "calldata-instrument-withdraw-max.json").read_text(
                encoding="utf-8"
            )
        )
        payload.update(
            instrumentId=instrument_id,
            account=query.account,
            amountIn=query.amount,
        )
        payload.update(self.overrides)
        return InstrumentCalldataResponse.model_validate(payload)

    def build_sell(self, body: dict[str, object]) -> dict[str, Any]:
        raise AssertionError("the calldata path must not call the legacy builder")


@dataclass(frozen=True)
class CalldataConfig(Config):
    transaction_api: str = "calldata"
    slippage_bps: int = 50
    token_balance_reader: object = lambda _chain, _rpc, _token, _account: 10**30
    referral_fee_bps: int = 0
    referral_wallet: str | None = None


def test_calldata_full_exit_plans_one_max_bundle_with_expected_usdc() -> None:
    client = MockCalldataWithdrawClient()

    report = withdraw(
        client,
        MockSigner(),
        holding(),
        permissive_policy(),
        config=CalldataConfig(),
    )

    [(instrument_id, query)] = client.requests
    assert instrument_id == "base-aave-usdc"
    assert query.action == "withdraw"
    assert query.amount == "max"
    assert query.slippage_bps == 50
    assert report.status == "planned"
    assert [step.kind for step in report.plan.steps] == ["withdraw"]
    [bundle] = report.plan.bundles
    assert bundle.action == "withdraw"
    assert bundle.amount == "max"
    assert bundle.expected_out == "50001234"
    assert bundle.step_indexes == (0,)
    assert report.sell.expected_usdc == "50.001234"
    # The share estimate stays for accounting; it is never the request amount.
    assert report.sell.yield_token_amount == "74.999123"
    validate(report.plan.model_dump(mode="json"), "tx-plan")


def test_calldata_partial_exit_requests_raw_underlying_units() -> None:
    client = MockCalldataWithdrawClient()

    report = withdraw(
        client,
        MockSigner(),
        RECIPES["erc4626-18-decimal-shares"],
        permissive_policy(),
        amount="5",
        config=CalldataConfig(),
    )

    [(_, query)] = client.requests
    assert query.amount == "5000000"
    assert report.plan.bundles[0].amount == "5000000"


def test_calldata_swapped_withdraw_preserves_withdraw_approve_swap_order() -> None:
    leftovers = [
        {"token": "0x5555555555555555555555555555555555555555", "maxAmount": "7"}
    ]
    client = MockCalldataWithdrawClient(
        overrides={
            "calls": swapped_withdraw_calls(),
            "minOut": "49000000",
            "leftovers": leftovers,
        }
    )

    report = withdraw(
        client,
        MockSigner(),
        holding(),
        permissive_policy(),
        config=CalldataConfig(),
    )

    assert [step.kind for step in report.plan.steps] == [
        "withdraw",
        "approve",
        "swap",
    ]
    [bundle] = report.plan.bundles
    assert bundle.step_indexes == (0, 1, 2)
    assert bundle.min_out == "49000000"
    assert [(item.token, item.max_amount) for item in bundle.leftovers] == [
        ("0x5555555555555555555555555555555555555555", "7")
    ]


def test_calldata_withdraw_does_not_call_non_usdc_output_usdc() -> None:
    client = MockCalldataWithdrawClient(
        overrides={
            "tokenOut": {
                "address": "0x5555555555555555555555555555555555555555",
                "symbol": "GHO",
                "decimals": 18,
            }
        }
    )

    report = withdraw(
        client,
        MockSigner(),
        holding(),
        permissive_policy(),
        config=CalldataConfig(),
    )

    assert report.plan.bundles[0].expected_out == "50001234"
    assert report.sell.expected_usdc is None


def test_calldata_partial_exit_without_raw_underlying_fails_closed() -> None:
    client = MockCalldataWithdrawClient()
    position = recipe_holding(
        balance="3",
        balance_raw=None,
        decimals=6,
        share_balance="3",
        share_balance_raw="3000000",
        share_decimals=6,
    )

    with pytest.raises(ValueError, match="share amount must never be sent"):
        withdraw(
            client,
            MockSigner(),
            position,
            permissive_policy(),
            amount="1",
            config=CalldataConfig(),
        )

    assert client.requests == []


def test_calldata_withdraw_rejects_a_bundle_for_another_account() -> None:
    client = MockCalldataWithdrawClient(overrides={"account": "0x" + "22" * 20})

    with pytest.raises(CalldataValidationError, match="account"):
        withdraw(
            client,
            MockSigner(),
            holding(),
            permissive_policy(),
            config=CalldataConfig(),
        )


@dataclass
class BatchingWithdrawSigner(MockSigner):
    batches: list[tuple[TxStep, ...]] = field(default_factory=list)

    def send_batch(self, steps: tuple[TxStep, ...], rpc_url: str) -> Receipt:
        self.batches.append(tuple(steps))
        return Receipt(
            transaction_hash=f"0x{len(self.batches):064x}",
            block_number=len(self.batches),
            gas_used=21_000,
            status=1,
            from_address=ADDRESS,
            to_address=steps[-1].to,
        )


def test_calldata_withdraw_executes_the_bundle_as_one_operation(
    tmp_path: Path,
) -> None:
    client = MockCalldataWithdrawClient(overrides={"calls": swapped_withdraw_calls()})
    signer = BatchingWithdrawSigner()
    store: dict[str, object] = {}
    log_path = tmp_path / "allocation-log.jsonl"

    report = withdraw(
        client,
        signer,
        holding(),
        permissive_policy(),
        confirm=True,
        config=CalldataConfig(_allocation_log_path=log_path),
        idempotency_store=store,
    )

    assert report.status == "success"
    [batch] = signer.batches
    assert [step.kind for step in batch] == ["withdraw", "approve", "swap"]
    assert signer.sent == []
    assert report.sell.status == "sent"
    assert report.sell.expected_usdc == "50.001234"
    assert [step.status for step in report.steps] == ["sent"] * 3
    assert "withdraw:0:base-aave-usdc:74.999123" in store
    # Logged once for the bundle, with the share amount it retires.
    [entry] = read_allocation_log(log_path=log_path)
    assert (entry.action_type, entry.shares) == ("withdraw", "74.999123")


def test_calldata_withdraw_rerun_submits_nothing_twice() -> None:
    client = MockCalldataWithdrawClient()
    signer = BatchingWithdrawSigner()
    store: dict[str, object] = {}
    arguments = (client, signer, holding(), permissive_policy())

    withdraw(*arguments, confirm=True, config=CalldataConfig(), idempotency_store=store)
    rerun = withdraw(
        *arguments, confirm=True, config=CalldataConfig(), idempotency_store=store
    )

    assert len(client.requests) == 1
    assert len(signer.batches) == 1
    assert rerun.status == "success"
    assert rerun.plan.steps == ()


def test_calldata_withdraw_rejects_referral_configuration() -> None:
    client = MockCalldataWithdrawClient()

    with pytest.raises(CalldataUnsupportedError, match="referral"):
        withdraw(
            client,
            MockSigner(),
            holding(),
            permissive_policy(),
            config=CalldataConfig(referral_fee_bps=10),
        )

    assert client.requests == []


YIELD_TOKEN = "0x3333333333333333333333333333333333333333"


def test_calldata_withdraw_dry_run_checks_the_shares_actually_held() -> None:
    held = {YIELD_TOKEN.casefold(): 48 * 10**18}
    config = CalldataConfig(
        token_balance_reader=lambda _chain, _rpc, token, _account: held.get(
            token.casefold(), 0
        )
    )

    report = withdraw(
        MockCalldataWithdrawClient(),
        MockSigner(),
        holding(),
        permissive_policy(),
        config=config,
    )

    assert report.status == "planned"
    [shares] = report.funding
    assert shares.token == YIELD_TOKEN
    assert shares.required_raw == str(49 * 10**18)
    assert shares.shortfall_raw == str(10**18)
    assert any("short by" in message for message in report.messages)


def test_calldata_withdraw_of_more_than_is_held_is_refused_unsent() -> None:
    signer = BatchingWithdrawSigner()
    store: dict[str, object] = {}

    with pytest.raises(UnderfundedPlanError):
        withdraw(
            MockCalldataWithdrawClient(),
            signer,
            holding(),
            permissive_policy(),
            confirm=True,
            config=CalldataConfig(
                token_balance_reader=lambda _chain, _rpc, _token, _account: 0
            ),
            idempotency_store=store,
        )

    assert signer.batches == []
    assert store == {}
