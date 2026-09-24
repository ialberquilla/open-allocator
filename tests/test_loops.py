"""Levered loops through the execution path.

Fixtures are ``GET /loops`` and ``GET /loops/:loopId/calldata`` responses: a
cross-asset USDC/AUSD open at L=4 and a same-asset USDC/USDC open at L=3.
"""

from __future__ import annotations

import copy
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from open_allocator.core import levered
from open_allocator.core import policy as policy_core
from open_allocator.core.checkpoint import allocation_log_totals, read_allocation_log
from open_allocator.core.positions import (
    IdleBalance,
    LeveredPositionError,
    PoolAccount,
    PositionHolding,
    Positions,
    decompose_levered,
)
from open_allocator.core.schema import validate
from open_allocator.core.types import (
    Allocation,
    AllocationLeg,
    Policy,
    PolicyAllowed,
    PolicyCaps,
    PolicyGates,
    PolicyWallet,
    TxPlan,
    TxStep,
    Vault,
)
from open_allocator.exec import loop_close, loops
from open_allocator.exec.bundle_execution import SubmissionModeError
from open_allocator.exec.client import (
    LoopCalldataQuery,
    LoopCalldataResponse,
    LoopsListResponse,
    OneTxDecodeError,
)
from open_allocator.exec.execute import (
    GasCheck,
    PolicyCheckFailed,
    TransactionPlanError,
    execute_allocation,
    plan_calldata_allocation,
)
from open_allocator.exec.rebalance import execute_rebalance
from open_allocator.exec.signer import Receipt
from open_allocator.exec.withdraw import withdraw

FIXTURES = Path(__file__).parent / "fixtures"
ACCOUNT = "0x0000000000000000000000000000000000000001"
POOL = "0x80F00661b13CC5F6ccd3885bE7b4C9c67545D585"
MONAD = 143
MONAD_USDC = "0x754704Bc059F8C67012fEd69BC8A327a5aafb603"
AUSD = "0x00000000eFE302BEAA2b3e6e1b18d08D69a9012a"
N_USDC = "0x0000008f4ebefd380701541f3c3b8714bd828824fa2842de58ba96eea9758a3f"
N_AUSD = "0x0000008f8b183cc36cd7ab674e79e56a05bc89cd6a733a92f3ca89ac84bc0266"
CROSS = "0x0000008feb2e28c4aea6972122672ddb25db4d3df4af7e83e7ffcf3ad8fb5adb"
SAME = "0x0000008f8cd11c51638c07c1edaadb662438c0d22453d5b5e8a646b9e404ec92"


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def screen() -> LoopsListResponse:
    return LoopsListResponse.model_validate(fixture("loops-screen.json"))


def neverland(instrument_id: str, asset: str, token: str) -> Vault:
    return Vault(
        instrument_id=instrument_id,
        protocol="Neverland",
        chain_id=MONAD,
        asset=asset,
        token_address=token,
        token_decimals=6,
        yield_token_address="0x38648958836eA88b368b4ac23b86Ad44B0fe7508",
        yield_token_decimals=6,
        apy=10.2,
        apy_base=2.1,
        apy_reward=8.1,
        tvl_usd=1_000_000,
        curator="Neverland",
        reward_dependence=0.5,
        protocol_address=POOL,
    )


def instruments() -> list[Vault]:
    return [
        neverland(N_USDC, "USDC", MONAD_USDC),
        neverland(N_AUSD, "AUSD", AUSD),
    ]


def universe() -> list[Vault]:
    base = instruments()
    rows, skipped = loops.loop_vaults(screen().data, base)
    assert skipped == ()
    return [*base, *rows]


def loop_policy(**caps: object) -> Policy:
    return Policy(
        wallet=PolicyWallet(mode="self-custody", signer="safe"),
        allowed=PolicyAllowed(
            protocols=None, chains=None, assets=("USDC",), curators=None
        ),
        caps=PolicyCaps(
            max_weight_per_instrument=1,
            max_weight_per_protocol=1,
            max_weight_per_curator=1,
            max_weight_per_chain=1,
            min_instrument_tvl_usd=1,
            max_reward_dependence=1,
            **{
                "max_weight_levered": 1.0,
                "max_gross_leverage": 4.0,
                "max_book_gross_exposure": 4.0,
                "min_health_factor": 1.10,
                "min_depeg_buffer_bps": 1500,
                **caps,
            },
        ),
        gates=PolicyGates(
            new_instrument_needs_approval=True,
            autonomous_rebalance=False,
            max_deploy_per_cycle_usd=1_000_000,
        ),
    )


def loop_allocation(loop_id: str = CROSS, leverage: float = 4.0) -> Allocation:
    return Allocation(
        legs=(
            AllocationLeg(instrument_id=loop_id, weight=1.0, usd=20, leverage=leverage),
        ),
        total_usd=20,
        metadata={},
    )


@dataclass
class LoopClient:
    """Serves the fixtures, bound to the request that asked."""

    edit: Any = None
    requests: list[tuple[str, LoopCalldataQuery]] = field(default_factory=list)
    holdings: list[dict[str, Any]] = field(default_factory=list)
    positions_fail: bool = False

    def loops(self) -> LoopsListResponse:
        return screen()

    def loop_calldata(
        self, loop_id: str, query: LoopCalldataQuery
    ) -> LoopCalldataResponse:
        self.requests.append((loop_id, query))
        if query.action == "close":
            name = "loop-calldata-close-cross.json"
        elif loop_id == CROSS:
            name = "loop-calldata-open-cross.json"
        else:
            name = "loop-calldata-open-same.json"
        payload = fixture(name)
        payload["account"] = query.account
        payload["amountIn"] = query.amount
        if payload["expiresAt"] is not None:
            payload["expiresAt"] = int(time.time()) + 3600
        if self.edit is not None:
            self.edit(payload)
        return LoopCalldataResponse.model_validate(payload)

    def balances(self, address: str) -> dict[str, Any]:
        if self.positions_fail:
            raise RuntimeError("positions unavailable")
        return {
            "address": address,
            "balances": [{"chainId": MONAD, "usdcBalance": "50.000000"}],
        }

    def positions(self, body: dict[str, object]) -> dict[str, Any]:
        return {"chainId": MONAD, "positions": self.holdings}

    def instrument_calldata(self, *_args: object) -> Any:
        raise AssertionError("a loop leg must never be built as a plain deposit")


@dataclass
class BatchingSigner:
    batches: list[tuple[TxStep, ...]] = field(default_factory=list)
    sent: list[TxStep] = field(default_factory=list)

    def address(self) -> str:
        return ACCOUNT

    def send(self, tx: TxStep, rpc_url: str) -> Receipt:
        self.sent.append(tx)
        return _receipt(len(self.sent), tx.to)

    def send_batch(self, steps: tuple[TxStep, ...], rpc_url: str) -> Receipt:
        self.batches.append(tuple(steps))
        return _receipt(len(self.batches), steps[-1].to)


@dataclass
class SequentialSigner:
    sent: list[TxStep] = field(default_factory=list)

    def address(self) -> str:
        return ACCOUNT

    def send(self, tx: TxStep, rpc_url: str) -> Receipt:
        self.sent.append(tx)
        return _receipt(len(self.sent), tx.to)


def _receipt(index: int, to: str) -> Receipt:
    return Receipt(
        transaction_hash=f"0x{index:064x}",
        block_number=index,
        gas_used=2_019_131,
        status=1,
        from_address=ACCOUNT,
        to_address=to,
    )


@dataclass(frozen=True)
class Config:
    transaction_api: str = "calldata"
    slippage_bps: int = 10
    token_balance_reader: object = lambda _chain, _rpc, _token, _account: 10**30
    referral_fee_bps: int = 0
    referral_wallet: str | None = None
    source_chain_id: int | None = None
    gas_checker: object = lambda _address, chain_id, _rpc, _config: GasCheck(
        chain_id=chain_id, ok=True, balance_wei=1, required_wei=1, message="ok"
    )
    _rpc_overrides: dict[int, str] = field(
        default_factory=lambda: {MONAD: "rpc://monad"}
    )
    allocation_log_path: Path | None = None


# --- the contract -----------------------------------------------------------


def test_both_production_bundles_parse_under_the_strict_contract() -> None:
    cross = LoopCalldataResponse.model_validate(
        fixture("loop-calldata-open-cross.json")
    )
    same = LoopCalldataResponse.model_validate(fixture("loop-calldata-open-same.json"))

    assert [call.type for call in cross.calls][:6] == [
        "set_account_config",
        "approve",
        "deposit",
        "approve",
        "approve",
        "borrow",
    ]
    assert cross.leverage.params_basis == "emode:2"
    assert cross.simulated.health_factor == 1.253333
    assert cross.simulated.depeg_buffer_bps == 2533
    assert same.swap is None and same.expires_at is None
    assert same.simulated.depeg_buffer_bps is None


def test_an_unknown_field_is_a_contract_change() -> None:
    payload = fixture("loop-calldata-open-cross.json")
    payload["recommendedLeverage"] = 8

    with pytest.raises(ValueError, match="recommendedLeverage"):
        LoopCalldataResponse.model_validate(payload)


@pytest.mark.parametrize(
    ("query", "message"),
    [
        ({"action": "open", "account": ACCOUNT, "leverage": "4"}, "both"),
        (
            {"action": "adjust", "account": ACCOUNT, "leverage": "2", "amount": "1"},
            "no amount",
        ),
        ({"action": "close", "account": ACCOUNT, "leverage": "2"}, "neither"),
        (
            {"action": "open", "account": ACCOUNT, "leverage": "4", "amount": "max"},
            "amount",
        ),
    ],
)
def test_a_loop_query_carries_exactly_what_its_action_needs(
    query: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        LoopCalldataQuery.model_validate(query)


def test_leverage_is_sent_exactly_or_not_at_all() -> None:
    assert loops.leverage_param(4.0) == "4"
    assert loops.leverage_param(3.25) == "3.25"
    with pytest.raises(ValueError, match="four decimal"):
        loops.leverage_param(4.00001)
    with pytest.raises(ValueError, match="above 1"):
        loops.leverage_param(1.0)


def test_screen_rows_become_levered_rows_keyed_by_loop_id() -> None:
    rows, skipped = loops.loop_vaults(screen().data, instruments())

    by_id = {row.instrument_id: row for row in rows}
    cross = by_id[CROSS]
    assert cross.is_levered and cross.cross_asset
    assert cross.max_leverage == 6.6667
    assert cross.liquidation_threshold is None
    assert (cross.collateral_instrument_id, cross.debt_instrument_id) == (
        N_USDC,
        N_AUSD,
    )
    assert cross.protocol_address == POOL
    assert cross.token_address == MONAD_USDC and cross.token_decimals == 6
    assert not by_id[SAME].cross_asset
    assert skipped == ()

    _rows, skipped = loops.loop_vaults(screen().data, instruments()[1:])
    assert {item.loop_id for item in skipped} == {CROSS, SAME}


# --- 5.4: model vs measure --------------------------------------------------


def test_an_l8_emode_measurement_reproduces_the_model() -> None:
    check = levered.check_simulation(
        requested_leverage=8,
        liquidation_threshold=0.94,
        same_asset=False,
        measured_leverage=8,
        measured_health_factor=1.074285,
        measured_depeg_buffer_bps=742,
    )

    assert check.ok
    assert check.modelled_depeg_buffer_bps == 743


def test_a_full_unwind_models_no_debt() -> None:
    check = levered.check_simulation(
        requested_leverage=None,
        liquidation_threshold=0.94,
        same_asset=False,
        measured_leverage=None,
        measured_health_factor=None,
        measured_depeg_buffer_bps=None,
    )
    assert check.ok
    assert not levered.check_simulation(
        requested_leverage=None,
        liquidation_threshold=0.94,
        same_asset=False,
        measured_leverage=None,
        measured_health_factor=1.5,
        measured_depeg_buffer_bps=5000,
    ).ok


@pytest.mark.parametrize(
    ("field_name", "value", "message"),
    [
        ("healthFactor", 1.22, "health factor"),
        ("healthFactor", 1.30, "health factor"),
        ("leverage", 4.2, "leverage"),
        ("depegBufferBps", 1800, "depeg buffer"),
    ],
)
def test_a_seeded_divergence_aborts_before_signing(
    field_name: str, value: float, message: str
) -> None:
    def seed(payload: dict[str, Any]) -> None:
        payload["simulated"][field_name] = value

    signer = BatchingSigner()

    with pytest.raises(loops.LoopDivergenceError, match=message):
        execute_allocation(
            LoopClient(edit=seed),
            signer,
            loop_allocation(),
            loop_policy(),
            confirm=True,
            known_instruments=universe(),
            config=Config(),
            idempotency_store={},
        )
    assert signer.batches == [] and signer.sent == []


def test_a_divergence_is_raised_as_a_stop_with_both_numbers() -> None:
    def seed(payload: dict[str, Any]) -> None:
        payload["simulated"]["healthFactor"] = 1.22

    response = LoopClient(edit=seed).loop_calldata(
        CROSS,
        LoopCalldataQuery(
            action="open", account=ACCOUNT, leverage="4", amount="20000000"
        ),
    )

    with pytest.raises(loops.LoopDivergenceError) as raised:
        loops.check_measurement(response, same_asset=False)
    assert raised.value.check.modelled_health_factor == pytest.approx(1.2533333)
    assert raised.value.check.measured_health_factor == 1.22


# --- policy on the venue's parameters ---------------------------------------


def test_policy_judges_a_loop_on_the_pairs_effective_threshold() -> None:
    # On the screen alone, L=4 at ltv 0.85 bounds the depeg buffer at 1333 bps
    # and fails a 1500 floor; the e-mode threshold of 0.94 gives 2533.
    raw = policy_core.check(loop_allocation(), loop_policy(), universe())
    assert "min_depeg_buffer_bps" in {item.rule for item in raw.violations}

    plan = execute_allocation(
        LoopClient(),
        BatchingSigner(),
        loop_allocation(),
        loop_policy(),
        known_instruments=universe(),
        config=Config(),
    )

    assert isinstance(plan, TxPlan)


def test_a_policy_floor_the_effective_threshold_still_fails_stops_the_plan() -> None:
    client = LoopClient()

    with pytest.raises(PolicyCheckFailed, match="min_depeg_buffer_bps"):
        execute_allocation(
            client,
            BatchingSigner(),
            loop_allocation(),
            loop_policy(min_depeg_buffer_bps=3000),
            known_instruments=universe(),
            config=Config(),
        )
    # Only the parameter read ran; nothing was planned.
    assert len(client.requests) == 1


def test_a_levered_leg_without_a_chosen_leverage_is_refused() -> None:
    allocation = Allocation(
        legs=(AllocationLeg(instrument_id=CROSS, weight=1.0, usd=20),),
        total_usd=20,
        metadata={},
    )

    with pytest.raises(TransactionPlanError, match="names no leverage"):
        execute_allocation(
            LoopClient(),
            BatchingSigner(),
            allocation,
            loop_policy(),
            known_instruments=universe(),
            config=Config(),
        )


# --- 5.1: one bundle, one operation, one idempotency unit -------------------


def test_a_loop_leg_plans_as_one_bound_bundle() -> None:
    client = LoopClient()

    plan = execute_allocation(
        client,
        BatchingSigner(),
        loop_allocation(),
        loop_policy(),
        known_instruments=universe(),
        config=Config(),
    )

    assert isinstance(plan, TxPlan)
    [bundle] = plan.bundles
    assert bundle.action == "loop_open"
    assert bundle.bundle_id == f"leg:0:{CROSS}:loop_open"
    assert bundle.step_indexes == tuple(range(18))
    assert bundle.amount == "20000000"
    assert {"borrow", "set_account_config", "swap", "deposit"} <= {
        step.kind for step in plan.steps
    }
    assert bundle.loop is not None
    assert bundle.loop.requested_leverage == 4
    assert bundle.loop.modelled_health_factor == pytest.approx(1.2533333)
    assert bundle.loop.simulated_health_factor == 1.253333
    assert bundle.loop.requires_account_config
    assert (bundle.loop.account_config_current, bundle.loop.account_config_target) == (
        0,
        2,
    )
    assert [
        query.model_dump(by_alias=True, exclude_none=True)
        for _id, query in client.requests
    ][-1] == {
        "action": "open",
        "account": ACCOUNT,
        "leverage": "4",
        "amount": "20000000",
        "slippageBps": 10,
    }
    payload = plan.model_dump(mode="json")
    assert validate(payload, "tx-plan") == payload


def test_the_loop_digest_binds_the_measured_position() -> None:
    first = execute_allocation(
        LoopClient(),
        BatchingSigner(),
        loop_allocation(),
        loop_policy(),
        known_instruments=universe(),
        config=Config(),
    )

    def nudge(payload: dict[str, Any]) -> None:
        payload["simulated"]["healthFactor"] = 1.253332
        payload["expiresAt"] = first.bundles[0].expires_at  # type: ignore[union-attr]

    second = execute_allocation(
        LoopClient(edit=nudge),
        BatchingSigner(),
        loop_allocation(),
        loop_policy(),
        known_instruments=universe(),
        config=Config(),
    )

    assert isinstance(first, TxPlan) and isinstance(second, TxPlan)
    assert first.steps == second.steps
    assert first.bundles[0].digest != second.bundles[0].digest


def test_inner_calls_are_visible_but_only_the_bundle_is_ever_completed() -> None:
    signer = BatchingSigner()
    store: dict[str, object] = {}

    report = execute_allocation(
        LoopClient(),
        signer,
        loop_allocation(),
        loop_policy(),
        confirm=True,
        known_instruments=universe(),
        config=Config(),
        idempotency_store=store,
    )

    assert report.status == "success"
    [batch] = signer.batches
    assert batch == report.plan.steps and len(batch) == 18
    assert signer.sent == []
    assert len(report.steps) == 18
    [bundle] = report.plan.bundles
    operation_key = f"{bundle.bundle_id}:digest:{bundle.digest}"
    assert {step.idempotency_key for step in report.steps} == {operation_key}
    assert set(store) == {
        operation_key,
        f"leg:0:{CROSS}",
        loops.params_key(0, CROSS),
    }
    assert not any(":call:" in key for key in store)
    assert [loop.loop_id for loop in report.loops] == [CROSS]


def test_a_signer_that_cannot_batch_cannot_carry_a_loop() -> None:
    signer = SequentialSigner()

    with pytest.raises(SubmissionModeError, match="one atomic operation"):
        execute_allocation(
            LoopClient(),
            signer,
            loop_allocation(),
            loop_policy(),
            confirm=True,
            known_instruments=universe(),
            config=Config(),
            idempotency_store={},
        )
    assert signer.sent == []


def test_a_sent_loop_is_not_rebuilt_or_resent() -> None:
    store: dict[str, object] = {}
    execute_allocation(
        LoopClient(),
        BatchingSigner(),
        loop_allocation(),
        loop_policy(),
        confirm=True,
        known_instruments=universe(),
        config=Config(),
        idempotency_store=store,
    )
    client = LoopClient()
    signer = BatchingSigner()

    report = execute_allocation(
        client,
        signer,
        loop_allocation(),
        loop_policy(),
        confirm=True,
        known_instruments=universe(),
        config=Config(),
        idempotency_store=store,
    )

    assert client.requests == [] and signer.batches == []
    assert report.plan.bundles == ()


def test_a_loop_is_never_bridged_or_built_from_another_chain() -> None:
    with pytest.raises(TransactionPlanError, match="same-chain only"):
        execute_allocation(
            LoopClient(),
            BatchingSigner(),
            loop_allocation(),
            loop_policy(),
            known_instruments=universe(),
            config=Config(source_chain_id=8453),
        )


def test_a_loop_whose_collateral_is_not_usdc_is_refused() -> None:
    vaults = [
        vault.model_copy(update={"token_address": AUSD})
        if vault.instrument_id == CROSS
        else vault
        for vault in universe()
    ]

    def ausd_in(payload: dict[str, Any]) -> None:
        payload["tokenIn"]["address"] = AUSD

    with pytest.raises(TransactionPlanError, match="not the chain's USDC"):
        execute_allocation(
            LoopClient(edit=ausd_in),
            BatchingSigner(),
            loop_allocation(),
            loop_policy(),
            known_instruments=vaults,
            config=Config(),
        )


def test_the_legacy_transaction_api_refuses_a_levered_leg() -> None:
    with pytest.raises(TransactionPlanError, match="calldata API"):
        execute_allocation(
            LoopClient(),
            BatchingSigner(),
            loop_allocation(),
            loop_policy(),
            known_instruments=universe(),
            config=Config(transaction_api="legacy"),
        )


def test_a_response_for_another_pair_is_refused() -> None:
    def swap_legs(payload: dict[str, Any]) -> None:
        payload["leverage"]["debtInstrumentId"] = N_USDC

    with pytest.raises(TransactionPlanError, match="debtInstrumentId"):
        execute_allocation(
            LoopClient(edit=swap_legs),
            BatchingSigner(),
            loop_allocation(),
            loop_policy(),
            known_instruments=universe(),
            config=Config(),
        )


def test_an_undeclared_account_change_is_refused() -> None:
    def hide(payload: dict[str, Any]) -> None:
        payload["leverage"]["requiresAccountConfig"] = False

    with pytest.raises(TransactionPlanError, match="requiresAccountConfig"):
        execute_allocation(
            LoopClient(edit=hide),
            BatchingSigner(),
            loop_allocation(),
            loop_policy(),
            known_instruments=universe(),
            config=Config(),
        )


def test_a_rebuilt_loop_keeps_its_leg_and_refuses_moved_parameters() -> None:
    plan = execute_allocation(
        LoopClient(),
        BatchingSigner(),
        loop_allocation(),
        loop_policy(),
        known_instruments=universe(),
        config=Config(),
    )
    assert isinstance(plan, TxPlan)
    [bundle] = plan.bundles

    steps, fresh = loops.refresh_loop_bundle(LoopClient(), bundle, config=Config())
    assert fresh.bundle_id == bundle.bundle_id and len(steps) == 18

    def lower(payload: dict[str, Any]) -> None:
        for block in (payload["leverage"], payload["simulated"]):
            block["liquidationThreshold"] = 0.93
        payload["simulated"]["healthFactor"] = 1.24
        payload["simulated"]["depegBufferBps"] = 2400

    with pytest.raises(OneTxDecodeError, match="liquidation_threshold"):
        loops.refresh_loop_bundle(LoopClient(edit=lower), bundle, config=Config())


# --- 5.3: the announcement --------------------------------------------------


def test_the_announcement_names_the_position_and_its_pool() -> None:
    client = LoopClient(
        holdings=[
            {
                "instrumentId": N_USDC,
                "protocol": "Neverland",
                "symbol": "USDC",
                "balance": "20.000000",
                "shareBalance": "20.000000",
                "shareBalanceRaw": "20000000",
                "shareDecimals": 6,
                "chainId": MONAD,
            }
        ]
    )

    fitted = plan_calldata_allocation(
        client,
        BatchingSigner(),
        loop_allocation(),
        loop_policy(),
        known_instruments=universe(),
        config=Config(),
    )

    [loop] = fitted.loops
    assert (loop.collateral.amount, loop.debt.amount) == ("79.729893", "59.799335")
    assert loop.collateral.symbol == "nUSDC" and loop.debt.symbol == "AUSD"
    assert loop.equity is not None and loop.equity.amount == "20"
    assert (loop.requested_leverage, loop.simulated_leverage) == (4, 4)
    assert loop.modelled_health_factor == pytest.approx(1.2533333)
    assert loop.simulated_depeg_buffer_bps == 2533
    assert loop.account_config == "aave e-mode 0 -> 2"
    assert loop.pool_positions is not None
    assert [item.instrument_id for item in loop.pool_positions] == [N_USDC]
    assert loop.kill_switch is not None
    # The screen's reward APY is priced from DUST only; the other token has no
    # price, which the kill switch has to say rather than hide.
    assert loop.kill_switch.reward_symbol == "DUST"
    assert any("unpriced" in caveat for caveat in loop.kill_switch.caveats)
    assert loop.max_leftovers[0].symbol == "AUSD"
    assert loop.confirmable and fitted.preparation.blockers == ()
    assert fitted.policy_result is not None and fitted.policy_result.ok


def test_no_account_changing_loop_is_confirmed_without_the_pool_shown() -> None:
    signer = BatchingSigner()

    fitted = plan_calldata_allocation(
        LoopClient(positions_fail=True),
        signer,
        loop_allocation(),
        loop_policy(),
        known_instruments=universe(),
        config=Config(),
    )
    [loop] = fitted.loops
    assert loop.pool_positions is None and not loop.confirmable
    assert any("cannot be confirmed" in item for item in fitted.preparation.blockers)

    with pytest.raises(TransactionPlanError, match="cannot be confirmed"):
        execute_allocation(
            LoopClient(positions_fail=True),
            signer,
            loop_allocation(),
            loop_policy(),
            confirm=True,
            known_instruments=universe(),
            config=Config(),
            idempotency_store={},
        )
    assert signer.batches == []


def test_a_loop_that_keeps_the_account_config_does_not_need_the_pool() -> None:
    def keep(payload: dict[str, Any]) -> None:
        payload["leverage"]["accountConfig"]["current"] = 2
        payload["leverage"]["requiresAccountConfig"] = False

    fitted = plan_calldata_allocation(
        LoopClient(edit=keep, positions_fail=True),
        BatchingSigner(),
        loop_allocation(),
        loop_policy(),
        known_instruments=universe(),
        config=Config(),
    )

    [loop] = fitted.loops
    assert loop.confirmable
    assert loop.account_config == "unchanged (aave e-mode 2)"


def test_a_same_asset_loop_announces_no_depeg_budget() -> None:
    fitted = plan_calldata_allocation(
        LoopClient(),
        BatchingSigner(),
        loop_allocation(SAME, leverage=3.0),
        loop_policy(),
        known_instruments=universe(),
        config=Config(),
    )

    [loop] = fitted.loops
    assert loop.same_asset
    assert loop.modelled_depeg_buffer_bps is None
    assert loop.simulated_depeg_buffer_bps is None
    assert fitted.plan.bundles[0].expires_at is None


# --- 5.2: positions ---------------------------------------------------------


def holding(instrument_id: str, usd: float) -> PositionHolding:
    return PositionHolding(
        instrument_id=instrument_id,
        protocol="Neverland",
        chain_id=MONAD,
        symbol="USDC",
        balance=str(usd),
        usd_value=usd,
        share_balance=str(usd),
        share_balance_raw=str(int(usd * 10**6)),
        share_decimals=6,
    )


def book(*holdings: PositionHolding) -> Positions:
    total = sum(item.usd_value for item in holdings)
    return Positions(
        address=ACCOUNT,
        holdings=holdings,
        idle_balances=(IdleBalance(chain_id=MONAD, usdc_balance="5", usd_value=5),),
        total_position_usd=total,
        total_idle_usdc=5,
        total_usd=total + 5,
    )


PAIRS = loops.loop_pairs(screen().data)


def account(
    debt: int, collateral: int = 80, borrowed: tuple[str, ...] = (AUSD,)
) -> PoolAccount:
    return PoolAccount(
        chain_id=MONAD,
        pool=POOL,
        total_collateral_base=collateral * 10**8,
        total_debt_base=debt * 10**8,
        health_factor=1.2533 if debt else None,
        borrowed_assets=borrowed if debt else (),
    )


def test_an_unlevered_book_is_unchanged() -> None:
    original = book(holding(N_USDC, 20), holding("0xbase-vault", 30))

    assert decompose_levered(original, pairs=PAIRS, accounts=[account(0)]) is original
    assert decompose_levered(original, pairs=PAIRS, accounts=[]) is original


def test_a_loop_is_held_at_its_equity_with_its_gross_beside_it() -> None:
    decomposed = decompose_levered(
        book(holding(N_USDC, 80), holding("0xbase-vault", 30)),
        pairs=PAIRS,
        accounts=[account(60)],
    )

    loop = next(item for item in decomposed.holdings if item.levered is not None)
    assert loop.instrument_id == CROSS
    assert loop.usd_value == 20 and loop.balance == "20.000000"
    assert loop.share_balance_raw == "80000000"
    assert loop.levered is not None
    assert (loop.levered.collateral_usd, loop.levered.debt_usd) == (80, 60)
    assert loop.levered.leverage == 4 and loop.levered.health_factor == 1.2533
    assert decomposed.total_position_usd == 50 and decomposed.total_usd == 55


def test_the_borrowed_asset_names_the_loop() -> None:
    decomposed = decompose_levered(
        book(holding(N_USDC, 30)),
        pairs=PAIRS,
        accounts=[account(20, collateral=30, borrowed=(MONAD_USDC,))],
    )

    assert decomposed.holdings[0].instrument_id == SAME


def test_debt_that_cannot_be_attributed_is_refused_not_reported_gross() -> None:
    with pytest.raises(LeveredPositionError, match="2 held positions"):
        decompose_levered(
            book(holding(N_USDC, 80), holding(N_AUSD, 10)),
            pairs=PAIRS,
            accounts=[account(60)],
        )
    with pytest.raises(LeveredPositionError, match="cannot be attributed"):
        decompose_levered(
            book(holding(N_USDC, 80)),
            pairs=PAIRS,
            accounts=[account(60, borrowed=("0x" + "ab" * 20,))],
        )


def test_the_book_is_read_through_the_pool_and_decomposed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = LoopClient(
        holdings=[
            {
                "instrumentId": N_USDC,
                "protocol": "Neverland",
                "symbol": "USDC",
                "balance": "80.000000",
                "shareBalance": "80.000000",
                "shareBalanceRaw": "80000000",
                "shareDecimals": 6,
                "chainId": MONAD,
            }
        ]
    )
    reads: list[tuple[int, str, str]] = []

    def read(_w3: object, *, chain_id: int, pool: str, account: str) -> PoolAccount:
        reads.append((chain_id, pool, account))
        return PoolAccount(
            chain_id=chain_id,
            pool=pool,
            total_collateral_base=80,
            total_debt_base=60,
            borrowed_assets=(AUSD,),
        )

    monkeypatch.setattr(loops, "read_pool_account", read)

    decomposed, warnings = loops.read_book(client, ACCOUNT, Config())

    assert warnings == ()
    assert reads == [(MONAD, POOL, ACCOUNT)]
    assert decomposed.holdings[0].instrument_id == CROSS
    assert decomposed.total_position_usd == 20


def test_an_unreadable_screen_is_a_warning_not_a_guess() -> None:
    class NoScreen(LoopClient):
        def loops(self) -> LoopsListResponse:
            raise RuntimeError("down")

    positions, warnings = loops.read_book(
        NoScreen(
            holdings=[
                {
                    "instrumentId": N_USDC,
                    "protocol": "Neverland",
                    "symbol": "USDC",
                    "balance": "80",
                    "shareBalance": "80",
                    "shareBalanceRaw": "80000000",
                    "shareDecimals": 6,
                }
            ]
        ),
        ACCOUNT,
    )

    assert positions.holdings[0].instrument_id == N_USDC
    assert warnings and "not decomposed" in warnings[0]


def test_withdraw_and_rebalance_leave_a_loop_to_its_close() -> None:
    levered = decompose_levered(
        book(holding(N_USDC, 80)), pairs=PAIRS, accounts=[account(60)]
    )
    position = levered.holdings[0]

    with pytest.raises(TransactionPlanError, match="loop close"):
        withdraw(LoopClient(), BatchingSigner(), position, loop_policy(), confirm=False)

    target = Allocation(
        legs=(AllocationLeg(instrument_id=N_USDC, weight=1.0, usd=25),),
        total_usd=25,
        metadata={},
    )
    with pytest.raises(TransactionPlanError, match="not traded by rebalance"):
        execute_rebalance(
            LoopClient(),
            BatchingSigner(),
            levered,
            target,
            loop_policy(),
            known_instruments=universe(),
            config=Config(),
        )


def test_a_rebalance_that_leaves_the_loop_alone_is_not_blocked() -> None:
    levered = decompose_levered(
        book(holding(N_USDC, 80)), pairs=PAIRS, accounts=[account(60)]
    )
    target = Allocation(
        legs=(AllocationLeg(instrument_id=CROSS, weight=0.8, usd=20, leverage=4),),
        total_usd=25,
        metadata={},
    )

    # Rebalance judges a held loop on the screen's parameters, so the depeg
    # floor is lifted here to isolate the trade guard.
    report = execute_rebalance(
        LoopClient(),
        BatchingSigner(),
        copy.deepcopy(levered),
        target,
        loop_policy(min_depeg_buffer_bps=None),
        known_instruments=universe(),
        config=Config(),
    )

    assert report.plan.bundles == ()


# --- closing a loop on its own ----------------------------------------------


def _close_client() -> LoopClient:
    """A client whose book holds the cross loop, so its pool can be read."""
    client = LoopClient()
    client.holdings = [
        {
            "instrumentId": N_USDC,
            "protocol": "Neverland",
            "symbol": "USDC",
            "balance": "67.461173",
            "shareBalance": "67.461173",
            "shareBalanceRaw": "67461173",
            "shareDecimals": 6,
            "chainId": MONAD,
        }
    ]
    return client


def test_a_close_is_planned_without_a_target_allocation() -> None:
    report = loop_close.close(
        _close_client(),
        BatchingSigner(),
        CROSS,
        policy=loop_policy(),
        known_instruments=instruments(),
        confirm=False,
        config=Config(),
    )

    assert report.status == "planned"
    assert report.loop_id == CROSS
    assert len(report.plan.steps) == 25
    assert report.plan.bundles[0].action == "loop_close"
    # Nothing was sent: a dry run stops at the plan.
    assert report.receipts == ()


def test_a_close_announces_the_emode_it_switches_back() -> None:
    report = loop_close.close(
        _close_client(),
        BatchingSigner(),
        CROSS,
        policy=loop_policy(),
        known_instruments=instruments(),
        confirm=False,
        config=Config(),
    )

    assert report.announcement.requires_account_config
    assert report.announcement.account_config == "aave e-mode 2 -> 0"
    assert report.announcement.confirmable


def test_a_close_that_cannot_read_its_pool_is_refused() -> None:
    """An e-mode switch re-prices every other position held in the pool."""
    client = _close_client()
    client.positions_fail = True

    with pytest.raises(TransactionPlanError, match="could not be read"):
        loop_close.close(
            client,
            BatchingSigner(),
            CROSS,
            policy=loop_policy(),
            known_instruments=instruments(),
            confirm=False,
            config=Config(),
        )


def test_a_confirmed_close_goes_out_as_one_batch() -> None:
    signer = BatchingSigner()
    report = loop_close.close(
        _close_client(),
        signer,
        CROSS,
        policy=loop_policy(),
        known_instruments=instruments(),
        confirm=True,
        config=Config(),
    )

    assert report.status == "success"
    assert len(signer.batches) == 1
    assert len(signer.batches[0]) == 25
    assert signer.sent == []


def test_a_close_refuses_a_signer_that_cannot_batch() -> None:
    """Half an unwind is a levered position with its collateral withdrawn."""
    with pytest.raises(SubmissionModeError, match="one atomic operation"):
        loop_close.close(
            _close_client(),
            SequentialSigner(),
            CROSS,
            policy=loop_policy(),
            known_instruments=instruments(),
            confirm=True,
            config=Config(),
        )


def test_a_loop_outside_the_screen_cannot_be_closed_here() -> None:
    with pytest.raises(loops.calldata.CalldataValidationError, match="loop screen"):
        loop_close.close(
            _close_client(),
            BatchingSigner(),
            "0x" + "ab" * 32,
            policy=loop_policy(),
            known_instruments=instruments(),
            confirm=False,
            config=Config(),
        )


def test_a_sent_close_is_written_to_the_ledger_as_capital_returned(
    tmp_path: Path,
) -> None:
    """A close has no share amount, so it records its expected output.

    The allocation log needs `usd` or `shares`, and the entry is signed
    negative because an unwind returns capital.
    """
    log_path = tmp_path / "allocation-log.jsonl"
    report = loop_close.close(
        _close_client(),
        BatchingSigner(),
        CROSS,
        policy=loop_policy(),
        known_instruments=instruments(),
        confirm=True,
        config=Config(allocation_log_path=log_path),
    )

    assert report.status == "success"
    entries = read_allocation_log(log_path=log_path)
    assert len(entries) == 1
    assert entries[0].action_type == "loop_close"
    assert entries[0].usd == pytest.approx(12.455957)
    assert allocation_log_totals(entries)[CROSS] == pytest.approx(-12.455957)
