from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from open_allocator.core.positions import IdleBalance, PositionHolding, Positions
from open_allocator.core.types import (
    Policy,
    PolicyAllowed,
    PolicyCaps,
    PolicyGates,
    PolicyWallet,
    TxStep,
)
from open_allocator.core.withdraw import plan_withdraw
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
