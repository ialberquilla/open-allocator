import json
from pathlib import Path
from typing import Any

import pytest

from open_allocator.core.types import Vault
from open_allocator.exec.calldata import (
    CalldataAmountError,
    CalldataExpiredError,
    CalldataValidationError,
    DepositToken,
    deposit_amount_raw,
    deposit_token,
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


BASE_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
BSC_USDC = "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d"


def discovered(
    instrument_id: str,
    *,
    chain_id: int = 8453,
    token_address: str | None = BASE_USDC,
    token_decimals: int | None = 6,
    asset: str = "USDC",
) -> Vault:
    return Vault(
        instrument_id=instrument_id,
        protocol="protocol",
        chain_id=chain_id,
        asset=asset,
        apy=1.0,
        tvl_usd=1.0,
        token_address=token_address,
        token_decimals=token_decimals,
    )


@pytest.mark.parametrize(
    ("amount", "raw"),
    [(100.25, "100250000"), ("0.000001", "1"), ("1.0000019", "1000001")],
)
def test_deposit_amount_converts_usdc_to_raw_units(amount: object, raw: str) -> None:
    token = DepositToken(chain_id=8453, address=BASE_USDC, decimals=6)

    assert deposit_amount_raw(amount, token) == raw


def test_deposit_amount_uses_the_discovered_decimals_not_six() -> None:
    token = DepositToken(chain_id=56, address=BSC_USDC, decimals=18)

    assert deposit_amount_raw("100.25", token) == "100250000000000000000"


def test_deposit_amount_rejects_an_amount_that_rounds_to_zero() -> None:
    token = DepositToken(chain_id=8453, address=BASE_USDC, decimals=6)

    with pytest.raises(CalldataAmountError, match="zero raw units"):
        deposit_amount_raw("0.0000009", token)


def test_deposit_token_is_the_chain_usdc_even_for_a_non_usdc_vault() -> None:
    vaults = (
        discovered(
            "gho-vault",
            token_address="0x6Bb7a212910682DCFdbd5BCBb3e28FB4E8da10Ee",
            token_decimals=18,
            asset="GHO",
        ),
        discovered("usdc-vault"),
        discovered("other-chain", chain_id=42161, token_decimals=18),
    )

    token = deposit_token(8453, vaults, config={})

    assert token == DepositToken(chain_id=8453, address=BASE_USDC, decimals=6)


def test_deposit_token_matches_the_address_case_insensitively() -> None:
    vaults = (discovered("usdc-vault", token_address=BASE_USDC.lower()),)

    assert deposit_token(8453, vaults, config={}).decimals == 6


def test_deposit_token_honors_the_usdc_address_override() -> None:
    override = "0x00000000000000000000000000000000000000cc"
    vaults = (
        discovered("canonical", chain_id=56, token_address=BSC_USDC, token_decimals=18),
        discovered("override", chain_id=56, token_address=override, token_decimals=6),
    )

    token = deposit_token(56, vaults, config={"PAYMASTER_USDC_ADDRESS_56": override})

    assert token == DepositToken(chain_id=56, address=override, decimals=6)


def test_deposit_token_fails_closed_without_a_discovered_decimal() -> None:
    vaults = (
        discovered("no-decimals", token_decimals=None),
        discovered("other-token", token_address="0x" + "11" * 20),
    )

    with pytest.raises(CalldataAmountError, match="reports decimals"):
        deposit_token(8453, vaults, config={})


def test_deposit_token_fails_closed_on_disagreeing_decimals() -> None:
    vaults = (
        discovered("six", token_decimals=6),
        discovered("eighteen", token_decimals=18),
    )

    with pytest.raises(CalldataAmountError, match="disagree"):
        deposit_token(8453, vaults, config={})


def test_deposit_token_fails_closed_for_a_chain_without_known_usdc() -> None:
    with pytest.raises(CalldataAmountError, match="no USDC address"):
        deposit_token(999_999, (discovered("x", chain_id=999_999),), config={})
