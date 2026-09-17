import json
from pathlib import Path
from typing import Any

import pytest

from open_allocator.exec.calldata import (
    CalldataExpiredError,
    CalldataValidationError,
    ensure_calldata_lifetime,
    validate_bridge_calldata,
    validate_instrument_calldata,
)
from open_allocator.exec.client import (
    BridgeCalldataResponse,
    InstrumentCalldataResponse,
)

FIXTURES = Path(__file__).parent / "fixtures"
INSTRUMENT_ID = "0x" + "ab" * 32
SAFE = "0x1111111111111111111111111111111111111111"
EXPIRES_AT = 1789650060


def deposit_response(**overrides: Any) -> InstrumentCalldataResponse:
    payload = json.loads(
        (FIXTURES / "calldata-instrument-deposit-swap.json").read_text(encoding="utf-8")
    )
    payload.update(overrides)
    return InstrumentCalldataResponse.model_validate(payload)


def bridge_response() -> BridgeCalldataResponse:
    return BridgeCalldataResponse.model_validate_json(
        (FIXTURES / "calldata-bridge-fast.json").read_text(encoding="utf-8")
    )


def validate_deposit(
    response: InstrumentCalldataResponse,
    **overrides: Any,
) -> InstrumentCalldataResponse:
    expected: dict[str, Any] = {
        "instrument_id": INSTRUMENT_ID,
        "account": SAFE,
        "action": "deposit",
        "chain_id": 8453,
        "amount": "100250000",
        "min_ttl_seconds": 20,
        "now": EXPIRES_AT - 45,
    }
    expected.update(overrides)
    return validate_instrument_calldata(response, **expected)


def test_valid_instrument_bundle_passes() -> None:
    response = deposit_response()

    assert validate_deposit(response) is response


def test_identity_comparison_ignores_address_and_hex_case() -> None:
    validate_deposit(
        deposit_response(),
        instrument_id=INSTRUMENT_ID.upper().replace("0X", "0x"),
        account=SAFE.upper().replace("0X", "0x"),
    )


@pytest.mark.parametrize(
    ("override", "field"),
    [
        ({"instrument_id": "0x" + "cd" * 32}, "instrumentId"),
        ({"account": "0x2222222222222222222222222222222222222222"}, "account"),
        ({"action": "withdraw"}, "action"),
        ({"chain_id": 42161}, "chainId"),
        ({"amount": "100000000"}, "amountIn"),
    ],
)
def test_mismatched_instrument_bundle_is_rejected(
    override: dict[str, Any],
    field: str,
) -> None:
    with pytest.raises(CalldataValidationError, match=field):
        validate_deposit(deposit_response(), **override)


@pytest.mark.parametrize("now", [EXPIRES_AT, EXPIRES_AT + 1])
def test_expired_bundle_is_rejected(now: int) -> None:
    with pytest.raises(CalldataExpiredError, match="expired"):
        validate_deposit(deposit_response(), now=now)


def test_bundle_below_minimum_lifetime_must_be_rebuilt() -> None:
    with pytest.raises(CalldataExpiredError, match="below the 20s minimum"):
        validate_deposit(deposit_response(), now=EXPIRES_AT - 19)


def test_bundle_at_minimum_lifetime_passes() -> None:
    ensure_calldata_lifetime(
        deposit_response(),
        min_ttl_seconds=20,
        now=EXPIRES_AT - 20,
    )


def test_bundle_without_quote_expiry_has_no_lifetime_limit() -> None:
    ensure_calldata_lifetime(
        deposit_response(expiresAt=None),
        min_ttl_seconds=3600,
        now=EXPIRES_AT + 10_000,
    )


def test_expiry_is_an_execution_contract_rejection() -> None:
    with pytest.raises(CalldataValidationError):
        validate_deposit(deposit_response(), now=EXPIRES_AT)


def test_valid_bridge_bundle_passes() -> None:
    response = bridge_response()

    assert (
        validate_bridge_calldata(
            response,
            from_chain_id=8453,
            to_chain_id=42161,
            account=SAFE,
            amount="100000000",
            fast=True,
        )
        is response
    )


@pytest.mark.parametrize(
    ("override", "field"),
    [
        ({"from_chain_id": 10}, "fromChainId"),
        ({"to_chain_id": 10}, "toChainId"),
        ({"account": "0x2222222222222222222222222222222222222222"}, "account"),
        ({"amount": "1"}, "amount"),
        ({"fast": False}, "fast"),
    ],
)
def test_mismatched_bridge_bundle_is_rejected(
    override: dict[str, Any],
    field: str,
) -> None:
    expected: dict[str, Any] = {
        "from_chain_id": 8453,
        "to_chain_id": 42161,
        "account": SAFE,
        "amount": "100000000",
        "fast": True,
    }
    expected.update(override)

    with pytest.raises(CalldataValidationError, match=field):
        validate_bridge_calldata(bridge_response(), **expected)
