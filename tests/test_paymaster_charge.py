from __future__ import annotations

import pytest

from open_allocator.exec.paymaster_charge import (
    max_token_charge,
    parse_erc20_paymaster_data,
)
from open_allocator.exec.paymaster_types import UserOperationGas

USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
TREASURY = "0x" + "7e" * 20


def paymaster_data(
    *,
    mode_byte: int = 0x03,
    flags: int = 0,
    token: str = USDC,
    post_op_gas: int = 18_990,
    exchange_rate: int = 2_682_448_618,
    prefund: int | None = None,
    constant_fee: int | None = None,
) -> str:
    """ERC-20-mode paymasterData laid out as SingletonPaymasterV7 parses it."""
    body = bytes([mode_byte, flags])
    body += (0).to_bytes(6, "big") + (0).to_bytes(6, "big")
    body += bytes.fromhex(token[2:])
    body += post_op_gas.to_bytes(16, "big")
    body += exchange_rate.to_bytes(32, "big")
    body += (1).to_bytes(16, "big")
    body += bytes.fromhex(TREASURY[2:])
    if prefund is not None:
        body += prefund.to_bytes(16, "big")
    if constant_fee is not None:
        body += constant_fee.to_bytes(16, "big")
    body += b"\x01" * 65
    return "0x" + body.hex()


def gas(**overrides: int | None) -> UserOperationGas:
    values: dict[str, int | None] = {
        "call_gas_limit": 356_332,
        "verification_gas_limit": 91_249,
        "pre_verification_gas": 62_161,
        "paymaster_verification_gas_limit": 46_456,
        "paymaster_post_op_gas_limit": 68_990,
        "max_fee_per_gas": 8_855_000,
        "max_priority_fee_per_gas": 1_000_000,
    }
    values.update(overrides)
    return UserOperationGas.model_validate(values)


def test_a_stub_shaped_like_pimlicos_parses_as_erc20_mode() -> None:
    # The layout of a Base stub: 183 bytes,
    # mode byte 0x03 (ERC-20, any bundler), no optional fields.
    data = paymaster_data()
    assert len(bytes.fromhex(data[2:])) == 183

    config = parse_erc20_paymaster_data(data)

    assert config is not None
    assert config.token == USDC
    assert config.post_op_gas == 18_990
    assert config.exchange_rate == 2_682_448_618
    assert config.constant_fee == 0


def test_a_constant_fee_is_read_past_a_prefund() -> None:
    config = parse_erc20_paymaster_data(
        paymaster_data(flags=0x05, prefund=777, constant_fee=1_234)
    )
    assert config is not None
    assert config.constant_fee == 1_234


@pytest.mark.parametrize(
    "data",
    [
        None,
        "0x00",
        "not hex",
        "0xzz",
        paymaster_data(mode_byte=0x01),  # verifying mode, not ERC-20
        paymaster_data(flags=0x01)[: -2 * 65],  # constant fee flagged, absent
    ],
    ids=["none", "stub-placeholder", "text", "bad-hex", "verifying-mode", "truncated"],
)
def test_anything_else_is_no_config_rather_than_a_guess(data: object) -> None:
    assert parse_erc20_paymaster_data(data) is None


def test_the_bound_is_every_limit_plus_penalty_and_post_op_at_the_max_fee() -> None:
    charge = max_token_charge(
        gas(), post_op_gas=18_990, exchange_rate=2_702_526_992, constant_fee=5
    )

    limits = 91_249 + 356_332 + 62_161 + 46_456 + 68_990
    penalty = (356_332 + 68_990) // 10
    expected = (limits + penalty + 18_990) * 8_855_000 * 2_702_526_992 // 10**18 + 5
    assert charge == expected


def test_the_bound_holds_a_charge_the_paymaster_actually_took_on_base() -> None:
    """Base tx 0x8e82d9bd…4967f, block 51431027: UserOperationSponsored paid 8598.

    Limits, fee, postOpGas, and rate are decoded from that handleOps input.
    """
    charge = max_token_charge(gas(), post_op_gas=18_990, exchange_rate=2_702_526_992)

    assert charge is not None
    assert 8_598 <= charge
    # The same rate applied to the EntryPoint's actualGasCost reproduces the
    # charge, which is what pins the 1e18 scaling.
    assert 3_192_111_475_000 * 2_702_526_992 // 10**18 == pytest.approx(8_598, rel=0.01)


def test_a_second_real_charge_is_bounded_too() -> None:
    """Base tx 0x47150c24…64684, block 51431033: paid 4249."""
    charge = max_token_charge(
        gas(
            call_gas_limit=45_619,
            verification_gas_limit=59_304,
            pre_verification_gas=106_627,
            paymaster_verification_gas_limit=40_262,
            paymaster_post_op_gas_limit=68_990,
            max_fee_per_gas=9_100_000,
        ),
        post_op_gas=18_990,
        exchange_rate=2_702_526_992,
    )
    assert charge is not None
    assert 4_249 <= charge


@pytest.mark.parametrize(
    "missing", ["paymaster_verification_gas_limit", "paymaster_post_op_gas_limit"]
)
def test_no_bound_without_the_paymaster_gas_limits(missing: str) -> None:
    assert (
        max_token_charge(gas(**{missing: None}), post_op_gas=1, exchange_rate=1) is None
    )
