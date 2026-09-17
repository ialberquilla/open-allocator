from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from open_allocator.core.types import TxBundle
from open_allocator.exec import calldata, funding
from open_allocator.exec.client import InstrumentCalldataResponse
from open_allocator.exec.erc20 import BalanceReadError
from open_allocator.exec.funding import LedgerOperation, conservative_output, key_for

FIXTURES = Path(__file__).parent / "fixtures"
SAFE = "0x1111111111111111111111111111111111111111"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
VAULT_SHARE = "0x3333333333333333333333333333333333333333"
BASE = 8453
ARBITRUM = 42161
ARB_USDC = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"


def bundle(
    fixture: str,
    *,
    leg_index: int = 0,
    chain_id: int = BASE,
    **overrides: Any,
) -> TxBundle:
    payload = json.loads((FIXTURES / fixture).read_text(encoding="utf-8"))
    payload.update(chainId=chain_id, expiresAt=None, **overrides)
    for call in payload["calls"]:
        call["chainId"] = chain_id
    _steps, planned = calldata.plan_bundle(
        InstrumentCalldataResponse.model_validate(payload),
        leg_index=leg_index,
        first_step_index=0,
    )
    return planned


def deposit(usdc_raw: int, *, leg_index: int = 0, **overrides: Any) -> TxBundle:
    return bundle(
        "calldata-instrument-deposit-swap.json",
        leg_index=leg_index,
        instrumentId=f"0x{leg_index + 1:064x}",
        amountIn=str(usdc_raw),
        requires=[{"token": USDC, "amount": str(usdc_raw)}],
        leftovers=[{"token": USDC, "maxAmount": str(usdc_raw)}],
        **overrides,
    )


def withdraw(
    shares_raw: int,
    *,
    expected_usdc: int,
    min_usdc: int | None = None,
    leg_index: int = 0,
) -> TxBundle:
    return bundle(
        "calldata-instrument-withdraw-max.json",
        leg_index=leg_index,
        instrumentId=f"0x{leg_index + 1:064x}",
        requires=[{"token": VAULT_SHARE, "amount": str(shares_raw)}],
        expectedOut=str(expected_usdc),
        minOut=None if min_usdc is None else str(min_usdc),
    )


def balances(**by_token: int) -> dict[funding.BalanceKey, int | None]:
    tokens = {"usdc": USDC, "shares": VAULT_SHARE}
    return {
        key_for(BASE, SAFE, tokens[name]): amount for name, amount in by_token.items()
    }


def run(
    operations: list[LedgerOperation],
    held: dict[funding.BalanceKey, int | None],
    *,
    slippage_bps: int = 0,
) -> dict[str, funding.FundingRequirement]:
    requirements = funding.check(operations, held, slippage_bps=slippage_bps)
    return {item.token.casefold(): item for item in requirements}


# --- aggregation ------------------------------------------------------------


def test_bundles_that_each_fit_are_refused_when_together_they_do_not() -> None:
    first, second = deposit(60_000_000), deposit(50_000_000, leg_index=1)
    operation = LedgerOperation(BASE, (first, second))

    usdc = run([operation], balances(usdc=100_000_000))[USDC.casefold()]

    assert not usdc.ok
    assert usdc.required_raw == "110000000"
    assert usdc.available_raw == "100000000"
    assert usdc.shortfall_raw == "10000000"
    assert usdc.bundle_ids == (first.bundle_id, second.bundle_id)


def test_requirements_aggregate_across_operations_on_the_same_chain() -> None:
    first = LedgerOperation(BASE, (deposit(60_000_000),))
    later = LedgerOperation(BASE, (deposit(50_000_000, leg_index=1),))

    usdc = run([first, later], balances(usdc=100_000_000))[USDC.casefold()]

    assert usdc.required_raw == "110000000"
    assert not usdc.ok


def test_a_funded_plan_passes_with_the_exact_balance() -> None:
    operation = LedgerOperation(
        BASE, (deposit(60_000_000), deposit(40_000_000, leg_index=1))
    )

    usdc = run([operation], balances(usdc=100_000_000))[USDC.casefold()]

    assert usdc.ok
    assert usdc.shortfall_raw == "0"


def test_chains_do_not_pool_their_balances() -> None:
    base = LedgerOperation(BASE, (deposit(80_000_000),))
    arbitrum_deposit = bundle(
        "calldata-instrument-deposit-swap.json",
        chain_id=ARBITRUM,
        leg_index=1,
        instrumentId="0x" + "0" * 63 + "9",
        tokenIn={"address": ARB_USDC, "symbol": "USDC", "decimals": 6},
        amountIn="80000000",
        requires=[{"token": ARB_USDC, "amount": "80000000"}],
    )
    arbitrum = LedgerOperation(ARBITRUM, (arbitrum_deposit,))
    held = {
        key_for(BASE, SAFE, USDC): 100_000_000,
        key_for(ARBITRUM, SAFE, ARB_USDC): 10_000_000,
    }

    result = run([base, arbitrum], held)

    assert result[USDC.casefold()].ok
    assert not result[ARB_USDC.casefold()].ok
    assert result[ARB_USDC.casefold()].chain_id == ARBITRUM


# --- withdrawals and credits --------------------------------------------------


def test_a_withdrawal_is_verified_against_the_shares_actually_held() -> None:
    operation = LedgerOperation(
        BASE, (withdraw(49 * 10**18, expected_usdc=50_000_000),)
    )

    shares = run([operation], balances(shares=48 * 10**18))[VAULT_SHARE.casefold()]

    assert not shares.ok
    assert shares.shortfall_raw == str(10**18)


def test_earlier_withdrawal_proceeds_fund_a_later_deposit_at_min_out() -> None:
    operation = LedgerOperation(
        BASE,
        (
            withdraw(49 * 10**18, expected_usdc=50_000_000, min_usdc=49_000_000),
            deposit(59_000_000, leg_index=1),
        ),
    )

    usdc = run([operation], balances(usdc=10_000_000, shares=49 * 10**18))[
        USDC.casefold()
    ]

    assert usdc.required_raw == "10000000"
    assert usdc.ok


def test_without_min_out_expected_output_is_credited_less_slippage() -> None:
    operation = LedgerOperation(
        BASE,
        (
            withdraw(49 * 10**18, expected_usdc=50_000_000),
            deposit(50_000_000, leg_index=1),
        ),
    )

    usdc = run([operation], balances(usdc=0, shares=49 * 10**18), slippage_bps=50)[
        USDC.casefold()
    ]

    # 50 USDC expected, credited as 49.75: the deposit is 0.25 short.
    assert usdc.required_raw == "250000"
    assert not usdc.ok


def test_a_deposit_before_a_withdrawal_cannot_spend_its_proceeds() -> None:
    operation = LedgerOperation(
        BASE,
        (
            deposit(50_000_000),
            withdraw(49 * 10**18, expected_usdc=50_000_000, leg_index=1),
        ),
    )

    usdc = run([operation], balances(usdc=0, shares=49 * 10**18))[USDC.casefold()]

    assert usdc.required_raw == "50000000"


def test_leftovers_are_never_credited() -> None:
    # The deposit may leave all of its USDC behind; counting on that would let
    # a second deposit spend money the first one probably used.
    operation = LedgerOperation(
        BASE, (deposit(60_000_000), deposit(60_000_000, leg_index=1))
    )

    usdc = run([operation], balances(usdc=60_000_000))[USDC.casefold()]

    assert usdc.required_raw == "120000000"


@pytest.mark.parametrize(
    ("expected_out", "min_out", "slippage_bps", "credited"),
    [
        ("1000", "900", 50, 900),
        ("1000", None, 50, 995),
        ("1000", None, 0, 1000),
        (None, None, 50, 0),
        ("1000", None, 20_000, 0),
    ],
)
def test_conservative_output(
    expected_out: str | None,
    min_out: str | None,
    slippage_bps: int,
    credited: int,
) -> None:
    item = withdraw(1, expected_usdc=1).model_copy(
        update={"expected_out": expected_out, "min_out": min_out}
    )
    assert conservative_output(item, slippage_bps) == credited


# --- the paymaster's charge ---------------------------------------------------


def test_the_paymaster_charge_is_required_on_top_of_the_calls() -> None:
    operation = LedgerOperation(
        BASE,
        (deposit(100_000_000),),
        gas_token=USDC,
        gas_charge_raw=40_000,
    )

    usdc = run([operation], balances(usdc=100_000_000))[USDC.casefold()]

    assert usdc.required_raw == "100040000"
    assert usdc.includes_gas_charge
    assert not usdc.ok
    [blocker] = funding.FundingCheck(requirements=(usdc,)).blockers
    assert "maximum gas charge" in blocker
    assert "short by 40000" in blocker


def test_an_exit_pays_its_own_gas_from_its_proceeds() -> None:
    operation = LedgerOperation(
        BASE,
        (withdraw(49 * 10**18, expected_usdc=50_000_000),),
        gas_token=USDC,
        gas_charge_raw=40_000,
    )

    result = run([operation], balances(usdc=0, shares=49 * 10**18))

    assert result[USDC.casefold()].ok
    assert result[USDC.casefold()].required_raw == "0"


def test_dust_cannot_pay_for_its_own_exit() -> None:
    operation = LedgerOperation(
        BASE,
        (withdraw(10**15, expected_usdc=30_000),),
        gas_token=USDC,
        gas_charge_raw=40_000,
    )

    usdc = run([operation], balances(usdc=0, shares=10**15))[USDC.casefold()]

    assert not usdc.ok
    assert usdc.shortfall_raw == "10000"


# --- reading balances -----------------------------------------------------------


@dataclass
class Config:
    token_balance_reader: object
    _rpc_overrides: dict[int, str] = field(
        default_factory=lambda: {BASE: "https://base.example/v2/SECRET-KEY"}
    )


def test_an_unreadable_balance_is_a_shortfall_and_never_quotes_the_rpc() -> None:
    def reader(chain_id: int, rpc_url: str, token: str, account: str) -> int:
        raise ConnectionError(f"400 Client Error for url: {rpc_url}")

    check = funding.check_operations(
        [LedgerOperation(BASE, (deposit(1_000_000),))], Config(reader)
    )

    assert not check.ok
    [usdc] = check.requirements
    assert usdc.available_raw is None
    assert usdc.shortfall_raw == "1000000"
    text = " ".join((*check.messages, *check.blockers))
    assert "SECRET-KEY" not in text
    assert "ConnectionError" in text


def test_a_balance_read_error_keeps_its_own_redacted_reason() -> None:
    def reader(chain_id: int, rpc_url: str, token: str, account: str) -> int:
        raise BalanceReadError("balanceOf returned no value")

    check = funding.check_operations(
        [LedgerOperation(BASE, (deposit(1_000_000),))], Config(reader)
    )

    assert any("balanceOf returned no value" in message for message in check.messages)


def test_a_fresh_read_is_adjusted_for_operations_not_yet_included() -> None:
    reads: list[tuple[int, str, str]] = []

    def reader(chain_id: int, rpc_url: str, token: str, account: str) -> int:
        reads.append((chain_id, token, account))
        return 80_000_000

    check = funding.check_operations(
        [LedgerOperation(BASE, (deposit(50_000_000, leg_index=1),))],
        Config(reader),
        unsettled=[LedgerOperation(BASE, (deposit(40_000_000),))],
    )

    [usdc] = check.requirements
    assert usdc.available_raw == "40000000"
    assert usdc.shortfall_raw == "10000000"
    assert reads, "the balance is still read, not assumed"


def test_projection_debits_requirements_and_credits_conservative_output() -> None:
    operation = LedgerOperation(
        BASE,
        (withdraw(49 * 10**18, expected_usdc=50_000_000, min_usdc=49_000_000),),
        gas_token=USDC,
        gas_charge_raw=40_000,
    )

    projected = funding.project(
        [operation],
        {
            key_for(BASE, SAFE, USDC): 1_000_000,
            key_for(BASE, SAFE, VAULT_SHARE): 49 * 10**18,
        },
        slippage_bps=0,
    )

    assert projected[key_for(BASE, SAFE, USDC)] == 1_000_000 + 49_000_000 - 40_000
    assert projected[key_for(BASE, SAFE, VAULT_SHARE)] == 0
