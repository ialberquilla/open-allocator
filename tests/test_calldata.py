import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from open_allocator.core.schema import SchemaValidationError, validate
from open_allocator.core.types import (
    BundleLeftover,
    BundleRequirement,
    BundleToken,
    TxBundle,
    TxPlan,
    TxStep,
    Vault,
)
from open_allocator.exec.calldata import (
    CalldataAmountError,
    CalldataExpiredError,
    CalldataUnsupportedError,
    CalldataValidationError,
    DepositToken,
    bundle_steps,
    deposit_amount_raw,
    deposit_token,
    ensure_calldata_lifetime,
    ensure_calldata_supported,
    ensure_deposit_token,
    plan_bundle,
    request_bundle,
    validate_bridge_calldata,
    validate_instrument_calldata,
)
from open_allocator.exec.client import (
    BridgeCalldataResponse,
    InstrumentCalldataQuery,
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


def withdraw_response(**overrides: Any) -> InstrumentCalldataResponse:
    payload = json.loads(
        (FIXTURES / "calldata-instrument-withdraw-max.json").read_text(encoding="utf-8")
    )
    payload.update(overrides)
    return InstrumentCalldataResponse.model_validate(payload)


def planned(
    response: InstrumentCalldataResponse,
    *,
    leg_index: int = 0,
    first_step_index: int = 0,
) -> tuple[tuple[TxStep, ...], TxBundle]:
    return plan_bundle(
        response,
        leg_index=leg_index,
        first_step_index=first_step_index,
    )


def test_bundle_steps_keep_every_call_type_in_returned_order() -> None:
    response = deposit_response()

    steps = bundle_steps(response)

    assert [step.kind for step in steps] == [
        "approve",
        "swap",
        "approve",
        "approve",
        "deposit",
    ]
    assert [(step.to, step.data) for step in steps] == [
        (call.to, call.data) for call in response.calls
    ]
    assert all(step.chain_id == 8453 and step.value == 0 for step in steps)


def test_bundle_steps_carry_call_value_as_an_integer() -> None:
    payload = deposit_response().model_dump(by_alias=True, mode="json")
    payload["calls"][1]["value"] = "1000000000000000000"

    steps = bundle_steps(InstrumentCalldataResponse.model_validate(payload))

    assert steps[1].value == 10**18


def test_plan_bundle_records_the_quote_it_was_built_from() -> None:
    response = deposit_response()

    steps, bundle = planned(response, leg_index=2, first_step_index=3)

    assert bundle.bundle_id == f"leg:2:{INSTRUMENT_ID}:deposit"
    assert bundle.leg_index == 2
    assert bundle.step_indexes == (3, 4, 5, 6, 7)
    assert bundle.instrument_id == INSTRUMENT_ID
    assert bundle.action == "deposit"
    assert bundle.account == SAFE
    assert bundle.chain_id == 8453
    assert bundle.source == "1tx-calldata"
    assert bundle.endpoint == "GET /instruments/:instrumentId/calldata"
    assert bundle.amount == "100250000"
    assert bundle.token_in == BundleToken(address=BASE_USDC, symbol="USDC", decimals=6)
    assert bundle.token_out.decimals == 18
    assert bundle.quote_block == 35123456
    assert bundle.expires_at == EXPIRES_AT
    assert bundle.requires == (BundleRequirement(token=BASE_USDC, amount="100250000"),)
    assert bundle.leftovers == (
        BundleLeftover(token=BASE_USDC, max_amount="100250000"),
    )
    assert bundle.expected_out == "98765432100000000000"
    assert bundle.min_out is None
    assert bundle.protocol_gas == "412345"
    assert bundle.simulated_out == "98765432100000000000"
    assert bundle.simulation_scope == "protocol_bundle"
    assert bundle.simulation_engine == "wallet_neutral_atomic"
    assert len(steps) == 5


def test_a_planned_bundle_is_a_schema_valid_plan() -> None:
    steps, bundle = planned(deposit_response())
    plan = TxPlan(steps=steps, summary="deposit", bundles=(bundle,))

    payload = plan.model_dump(mode="json")

    assert validate(payload, "tx-plan") == payload
    assert TxPlan.model_validate(payload) == plan


def test_a_withdraw_bundle_plans_max_and_its_expected_output() -> None:
    steps, bundle = planned(withdraw_response())
    plan = TxPlan(steps=steps, summary="withdraw", bundles=(bundle,))

    assert [step.kind for step in steps] == ["withdraw"]
    assert bundle.amount == "max"
    assert bundle.expires_at is None
    assert bundle.expected_out == "50001234"
    assert bundle.leftovers == ()
    validate(plan.model_dump(mode="json"), "tx-plan")


def test_legacy_plans_without_bundles_stay_readable() -> None:
    payload = {
        "steps": [
            {
                "to": "0x0000000000000000000000000000000000000002",
                "data": "0xabcd",
                "value": 0,
                "chain_id": 8453,
                "kind": "buy",
            }
        ],
        "summary": "legacy",
    }

    assert validate(payload, "tx-plan") == payload
    assert TxPlan.model_validate(payload).bundles == ()


def test_digest_is_stable_for_the_same_bundle() -> None:
    _, first = planned(deposit_response())
    _, again = planned(deposit_response(), leg_index=4, first_step_index=9)

    assert first.digest == again.digest


def test_digest_ignores_address_and_hex_case() -> None:
    payload = deposit_response().model_dump(by_alias=True, mode="json")
    payload["account"] = SAFE.upper().replace("0X", "0x")
    for call in payload["calls"]:
        call["to"] = call["to"].lower()
        call["data"] = call["data"].upper().replace("0X", "0x")

    _, recased = planned(InstrumentCalldataResponse.model_validate(payload))
    _, original = planned(deposit_response())

    assert recased.digest == original.digest


def _mutated_deposit(mutate: Any) -> InstrumentCalldataResponse:
    payload = deposit_response().model_dump(by_alias=True, mode="json")
    mutate(payload)
    return InstrumentCalldataResponse.model_validate(payload)


def _set_quote_block(payload: dict[str, Any], block: int) -> None:
    payload["quoteBlock"] = block
    payload["simulation"]["quoteBlock"] = block


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda p: p["calls"].reverse(), id="call-order"),
        pytest.param(
            lambda p: p["calls"][4].__setitem__("data", "0x6e553f65"),
            id="call-data",
        ),
        pytest.param(
            lambda p: p["calls"][1].__setitem__("value", "1"),
            id="call-value",
        ),
        pytest.param(
            lambda p: p["calls"][1].__setitem__("to", "0x" + "55" * 20),
            id="call-target",
        ),
        pytest.param(
            lambda p: p["calls"][1].__setitem__("type", "fee"),
            id="call-type",
        ),
        pytest.param(lambda p: p["calls"].pop(2), id="dropped-call"),
        pytest.param(lambda p: p.__setitem__("amountIn", "100250001"), id="amount"),
        pytest.param(lambda p: _set_quote_block(p, 35123457), id="quote-block"),
        pytest.param(
            lambda p: p.__setitem__("expiresAt", EXPIRES_AT + 1),
            id="expiry",
        ),
        pytest.param(
            lambda p: p.__setitem__("account", "0x" + "22" * 20),
            id="account",
        ),
        pytest.param(
            lambda p: p.__setitem__("instrumentId", "0x" + "cd" * 32),
            id="instrument",
        ),
    ],
)
def test_digest_changes_with_what_the_bundle_commits_to(mutate: Any) -> None:
    _, original = planned(deposit_response())
    _, changed = planned(_mutated_deposit(mutate))

    assert changed.digest != original.digest


def test_digest_is_independent_of_non_committing_metadata() -> None:
    _, original = planned(deposit_response())
    _, requoted = planned(_mutated_deposit(lambda p: p.__setitem__("expectedOut", "1")))

    assert requoted.digest == original.digest


def _plan_payload() -> dict[str, Any]:
    steps, bundle = planned(deposit_response())
    return TxPlan(steps=steps, summary="deposit", bundles=(bundle,)).model_dump(
        mode="json"
    )


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        pytest.param(
            lambda p: p["steps"][4].__setitem__("data", "0xdeadbeef"),
            "digest does not match",
            id="tampered-step",
        ),
        pytest.param(
            lambda p: p["steps"].__setitem__(
                slice(1, 3), [p["steps"][2], p["steps"][1]]
            ),
            "digest does not match",
            id="reordered-steps",
        ),
        pytest.param(
            lambda p: p["bundles"][0].__setitem__("step_indexes", [0, 1, 3, 2, 4]),
            "contiguous",
            id="non-contiguous",
        ),
        pytest.param(
            lambda p: p["bundles"][0].__setitem__("step_indexes", [1, 2, 3, 4, 5]),
            "outside the plan",
            id="out-of-range",
        ),
        pytest.param(
            lambda p: p["steps"][0].__setitem__("chain_id", 42161),
            "chain 42161",
            id="chain-mismatch",
        ),
        pytest.param(
            lambda p: p["steps"][4].__setitem__("kind", "buy"),
            "legacy kind",
            id="legacy-kind",
        ),
        pytest.param(
            lambda p: p["bundles"].append(p["bundles"][0]),
            "shares steps",
            id="overlap",
        ),
    ],
)
def test_plan_rejects_bundles_that_do_not_bind_their_steps(
    mutate: Any,
    message: str,
) -> None:
    payload = _plan_payload()
    mutate(payload)

    with pytest.raises(ValidationError, match=message):
        TxPlan.model_validate(payload)


@pytest.mark.parametrize(
    ("mutate", "path"),
    [
        (lambda p: p["steps"][0].__setitem__("kind", "router"), "$.steps[0].kind"),
        (lambda p: p["bundles"][0].__setitem__("digest", "abc"), "$.bundles[0].digest"),
        (
            lambda p: p["bundles"][0].__setitem__("amount", "100.25"),
            "$.bundles[0].amount",
        ),
        (
            lambda p: p["bundles"][0].__setitem__("protocol_gas", 412345),
            "$.bundles[0].protocol_gas",
        ),
        (
            lambda p: p["bundles"][0].__setitem__("simulation_engine", "safe"),
            "$.bundles[0].simulation_engine",
        ),
        (lambda p: p["bundles"][0].pop("quote_block"), "$.bundles[0].quote_block"),
    ],
)
def test_schema_rejects_malformed_bundle_metadata(mutate: Any, path: str) -> None:
    payload = _plan_payload()
    mutate(payload)

    with pytest.raises(SchemaValidationError) as error:
        validate(payload, "tx-plan")

    assert path in error.value.paths


def test_deposit_token_mismatch_is_rejected() -> None:
    response = deposit_response()

    ensure_deposit_token(
        response, DepositToken(chain_id=8453, address=BASE_USDC.lower(), decimals=6)
    )
    with pytest.raises(CalldataValidationError, match=r"tokenIn\.address"):
        ensure_deposit_token(
            response, DepositToken(chain_id=8453, address=BSC_USDC, decimals=6)
        )
    with pytest.raises(CalldataValidationError, match=r"tokenIn\.decimals"):
        ensure_deposit_token(
            response, DepositToken(chain_id=8453, address=BASE_USDC, decimals=18)
        )


@pytest.mark.parametrize(
    "config",
    [
        {"referral_fee_bps": 25, "referral_wallet": "0x" + "99" * 20},
        {"referral_fee_bps": 25},
        {"referral_wallet": "0x" + "99" * 20},
    ],
)
def test_referral_configuration_is_explicitly_unsupported(
    config: dict[str, Any],
) -> None:
    with pytest.raises(CalldataUnsupportedError, match="referral"):
        ensure_calldata_supported(config)


def test_no_referral_configuration_is_supported() -> None:
    ensure_calldata_supported({"referral_fee_bps": 0, "referral_wallet": None})
    ensure_calldata_supported(None)


class RecordingCalldataClient:
    def __init__(self, response: InstrumentCalldataResponse) -> None:
        self.response = response
        self.requests: list[tuple[str, InstrumentCalldataQuery]] = []

    def instrument_calldata(
        self,
        instrument_id: str,
        query: InstrumentCalldataQuery,
    ) -> InstrumentCalldataResponse:
        self.requests.append((instrument_id, query))
        return self.response


def test_request_bundle_queries_validates_and_plans() -> None:
    client = RecordingCalldataClient(deposit_response())

    steps, bundle = request_bundle(
        client,
        instrument_id=INSTRUMENT_ID,
        action="deposit",
        account=SAFE,
        chain_id=8453,
        amount="100250000",
        leg_index=1,
        first_step_index=2,
        config={"slippage_bps": 30, "min_calldata_ttl_seconds": 20},
        token=DepositToken(chain_id=8453, address=BASE_USDC, decimals=6),
        now=EXPIRES_AT - 45,
    )

    [(instrument_id, query)] = client.requests
    assert instrument_id == INSTRUMENT_ID
    assert query.model_dump(by_alias=True, exclude_none=True) == {
        "action": "deposit",
        "account": SAFE,
        "amount": "100250000",
        "slippageBps": 30,
    }
    assert bundle.step_indexes == (2, 3, 4, 5, 6)
    assert len(steps) == 5


def test_request_bundle_rejects_a_mismatched_response() -> None:
    client = RecordingCalldataClient(deposit_response())

    with pytest.raises(CalldataValidationError, match="account"):
        request_bundle(
            client,
            instrument_id=INSTRUMENT_ID,
            action="deposit",
            account="0x" + "22" * 20,
            chain_id=8453,
            amount="100250000",
            leg_index=0,
            first_step_index=0,
            now=EXPIRES_AT - 45,
        )


def test_request_bundle_applies_the_configured_minimum_lifetime() -> None:
    client = RecordingCalldataClient(deposit_response())

    with pytest.raises(CalldataExpiredError, match="below the 50s minimum"):
        request_bundle(
            client,
            instrument_id=INSTRUMENT_ID,
            action="deposit",
            account=SAFE,
            chain_id=8453,
            amount="100250000",
            leg_index=0,
            first_step_index=0,
            config={"min_calldata_ttl_seconds": 50},
            now=EXPIRES_AT - 45,
        )
