from __future__ import annotations

import json

import httpx
import pytest
from cctp_messages import (
    ARBITRUM,
    BASE,
    BASE_DOMAIN,
    BASE_USDC,
    MESSAGE_TRANSMITTER,
    OTHER,
    SAFE,
    TOKEN_MESSENGER,
    attested,
    bridge_response,
    burn_calls,
    burn_message,
    cctp_config_payload,
    message_sent_log,
)
from eth_abi import decode as abi_decode

from open_allocator.core.types import TxPlan, TxStep
from open_allocator.exec import calldata, cctp
from open_allocator.exec.calldata import CalldataValidationError, DepositToken
from open_allocator.exec.circle import CircleClient, CircleError, CircleMessage
from open_allocator.exec.client import BridgeCalldataResponse, CctpConfigResponse

EXPECTED = cctp.BurnExpectation(
    source_domain=6,
    destination_domain=3,
    source_token_messenger=TOKEN_MESSENGER,
    destination_token_messenger=TOKEN_MESSENGER,
    account=SAFE,
    burn_token=BASE_USDC,
    amount_raw="100000000",
    max_fee_raw="12000",
    min_finality_threshold=1000,
)
SOURCE = burn_message(amount=100_000_000)


def complete(message: bytes, *, nonce: int = 7) -> CircleMessage:
    return CircleMessage.model_validate(
        {
            "message": "0x" + message.hex(),
            "attestation": "0x" + "ab" * 65,
            "eventNonce": "0x" + nonce.to_bytes(32, "big").hex(),
            "cctpVersion": 2,
            "status": "complete",
        }
    )


# --- messages -----------------------------------------------------------------


def test_a_burn_message_decodes_field_by_field() -> None:
    raw = attested(SOURCE, nonce=7, fee_executed=9_000, expiration=500)

    message = cctp.decode_message(raw)

    assert message.source_domain == 6
    assert message.destination_domain == 3
    assert message.nonce == "0x" + (7).to_bytes(32, "big").hex()
    assert message.sender == TOKEN_MESSENGER
    assert message.destination_caller.casefold() == SAFE
    assert message.body.amount == 100_000_000
    assert message.body.fee_executed == 9_000
    assert message.body.expiration_block == 500
    assert message.body.message_sender.casefold() == SAFE


def test_an_attested_message_equals_its_source_once_circles_fields_are_zeroed() -> None:
    raw = attested(SOURCE, nonce=7, fee_executed=9_000, executed=2000, expiration=9)

    assert cctp.unattested(raw) == cctp.unattested(SOURCE) == SOURCE
    assert cctp.unattested(burn_message(amount=1)) != SOURCE


def test_message_sent_logs_are_parsed_in_log_order() -> None:
    logs = [
        {"address": OTHER, "topics": ["0x" + "00" * 32], "data": "0x", "logIndex": 1},
        message_sent_log(SOURCE, log_index=9),
        message_sent_log(burn_message(amount=5, sender=OTHER), log_index=4),
    ]

    found = cctp.message_sent_logs(logs)

    assert [item.log_index for item in found] == [4, 9]
    assert found[1].message == "0x" + SOURCE.hex()


def test_the_safes_burn_is_picked_out_of_other_accounts_burns() -> None:
    logs = cctp.message_sent_logs(
        [
            message_sent_log(
                burn_message(amount=100_000_000, sender=OTHER), log_index=1
            ),
            message_sent_log(SOURCE, log_index=2),
        ]
    )

    selected = cctp.select_source_log(
        logs,
        EXPECTED,
        message_transmitter=MESSAGE_TRANSMITTER,
        burn_index=0,
        burns_in_operation=1,
    )

    assert selected.log_index == 2


def test_a_burn_count_that_does_not_match_the_operation_is_refused() -> None:
    logs = cctp.message_sent_logs(
        [message_sent_log(SOURCE, log_index=1), message_sent_log(SOURCE, log_index=2)]
    )

    with pytest.raises(cctp.CctpValidationError, match="cannot be identified"):
        cctp.select_source_log(
            logs,
            EXPECTED,
            message_transmitter=MESSAGE_TRANSMITTER,
            burn_index=0,
            burns_in_operation=1,
        )


def test_a_log_from_another_transmitter_is_not_the_burn() -> None:
    logs = cctp.message_sent_logs(
        [message_sent_log(SOURCE, log_index=1, emitter=OTHER)]
    )

    with pytest.raises(cctp.CctpValidationError):
        cctp.select_source_log(
            logs,
            EXPECTED,
            message_transmitter=MESSAGE_TRANSMITTER,
            burn_index=0,
            burns_in_operation=1,
        )


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"destination_domain": 2}, "destination domain"),
        ({"source_domain": 0}, "source domain"),
        ({"caller": OTHER}, "destinationCaller"),
        ({"recipient": OTHER}, "mintRecipient"),
        ({"token": OTHER}, "burn token"),
        ({"amount": 99_000_000}, "amount"),
        ({"max_fee": 13_000}, "maxFee"),
        ({"threshold": 2000}, "minFinalityThreshold"),
        ({"destination_messenger": OTHER}, "destination TokenMessenger"),
        ({"version": 0}, "version"),
    ],
)
def test_an_attested_message_that_is_not_the_burn_is_refused(
    changes: dict[str, object], field: str
) -> None:
    message = attested(
        burn_message(**{"amount": 100_000_000, **changes}),  # type: ignore[arg-type]
        nonce=7,
    )

    with pytest.raises(cctp.CctpValidationError, match=field):
        cctp.check_burn(cctp.decode_message(message), EXPECTED, attested=True)


def test_an_executed_fee_above_the_maximum_is_refused() -> None:
    message = attested(SOURCE, nonce=7, fee_executed=12_001)

    with pytest.raises(cctp.CctpValidationError, match="feeExecuted"):
        cctp.check_burn(cctp.decode_message(message), EXPECTED, attested=True)


def test_an_attestation_below_the_requested_finality_is_refused() -> None:
    message = attested(burn_message(amount=100_000_000, threshold=2000), nonce=7)
    expected = EXPECTED.model_copy(update={"min_finality_threshold": 2000})

    with pytest.raises(cctp.CctpValidationError, match="executed finality"):
        cctp.check_burn(cctp.decode_message(message), expected, attested=True)


# --- attestation selection ----------------------------------------------------


def test_a_pending_message_is_not_ready() -> None:
    pending = CircleMessage.model_validate(
        {
            "message": "0x",
            "attestation": "PENDING",
            "status": "pending_confirmations",
            "delayReason": "insufficient_fee",
        }
    )

    assert (
        cctp.select_attestation([pending], "0x" + SOURCE.hex(), EXPECTED, burn_index=0)
        is None
    )
    assert cctp.delay_reasons([pending]) == ("insufficient_fee",)


def test_the_attestation_of_this_burn_is_chosen_among_a_transactions_messages() -> None:
    others = complete(
        attested(burn_message(amount=100_000_000, sender=OTHER), nonce=3), nonce=3
    )
    ours = complete(attested(SOURCE, nonce=7, fee_executed=10_000), nonce=7)

    chosen = cctp.select_attestation(
        [others, ours], "0x" + SOURCE.hex(), EXPECTED, burn_index=0
    )

    assert chosen is not None
    assert chosen.message.nonce.endswith("07")
    assert chosen.net_mint == 99_990_000


def test_identical_burns_are_told_apart_by_position() -> None:
    first = complete(attested(SOURCE, nonce=7), nonce=7)
    second = complete(attested(SOURCE, nonce=8), nonce=8)

    chosen = cctp.select_attestation(
        [first, second], "0x" + SOURCE.hex(), EXPECTED, burn_index=1
    )

    assert chosen is not None
    assert chosen.message.nonce.endswith("08")


def test_an_event_nonce_that_disagrees_with_the_message_is_refused() -> None:
    wrong = complete(attested(SOURCE, nonce=7), nonce=8)

    with pytest.raises(cctp.CctpValidationError, match="eventNonce"):
        cctp.select_attestation([wrong], "0x" + SOURCE.hex(), EXPECTED, burn_index=0)


# --- calls --------------------------------------------------------------------


def test_receive_message_encodes_the_message_and_attestation() -> None:
    message = attested(SOURCE, nonce=7)

    step = cctp.receive_message_step(ARBITRUM, MESSAGE_TRANSMITTER, message, "0xab")

    assert step.kind == "cctp_receive"
    assert step.to == MESSAGE_TRANSMITTER
    assert step.data.startswith("0x57ecfd28")
    decoded = abi_decode(["bytes", "bytes"], bytes.fromhex(step.data[10:]))
    assert decoded == (message, b"\xab")


def burn_steps(**changes: object) -> tuple[TxStep, ...]:
    return tuple(
        TxStep(
            to=call["to"],
            data=call["data"],
            value=int(call["value"]),
            chain_id=call["chainId"],
            kind=call["type"],
        )
        for call in burn_calls(100_000_000, **changes)  # type: ignore[arg-type]
    )


def test_a_burn_bundle_decodes_to_what_it_burns() -> None:
    burn = cctp.decode_burn_steps(burn_steps())

    assert burn.amount_raw == "100000000"
    assert burn.destination_domain == 3
    assert burn.mint_recipient.casefold() == SAFE
    assert burn.destination_caller.casefold() == SAFE
    assert burn.max_fee_raw == "12000"


def test_an_approval_that_does_not_cover_the_burn_is_refused() -> None:
    with pytest.raises(cctp.CctpValidationError, match="does not cover"):
        cctp.decode_burn_steps(burn_steps(approval=1))


def request(response: dict[str, object]) -> tuple[tuple[TxStep, ...], object]:
    class Client:
        def bridge_calldata(self, query: object) -> BridgeCalldataResponse:
            return BridgeCalldataResponse.model_validate(response)

    return calldata.request_bridge_bundle(
        Client(),
        instrument_id="arb-vault",
        from_chain_id=BASE,
        to_chain_id=ARBITRUM,
        account=SAFE,
        amount="100000000",
        leg_index=0,
        token=DepositToken(chain_id=BASE, address=BASE_USDC, decimals=6),
        config={"fast_transfer": True},
    )


def test_a_bridge_bundle_carries_its_transfer_and_validates_in_a_plan() -> None:
    steps, bundle = request(bridge_response(100_000_000))

    assert bundle.action == "bridge"  # type: ignore[attr-defined]
    assert bundle.bridge.destination_domain == 3  # type: ignore[attr-defined]
    plan = TxPlan(steps=steps, summary="bridge", bundles=(bundle,))  # type: ignore[arg-type]
    assert plan.bundles[0].bundle_id == "leg:0:arb-vault:bridge"


@pytest.mark.parametrize(
    "calls",
    [
        burn_calls(100_000_000, recipient=OTHER),
        burn_calls(100_000_000, caller=OTHER),
        burn_calls(100_000_000, destination_domain=2),
        burn_calls(99_000_000),
    ],
)
def test_burn_calldata_that_does_not_burn_to_this_safe_is_refused(
    calls: list[dict[str, object]],
) -> None:
    with pytest.raises(CalldataValidationError):
        request(bridge_response(100_000_000, calls=calls))  # type: ignore[arg-type]


def test_a_redemption_cannot_ride_inside_a_1tx_bundle() -> None:
    steps, bundle = request(bridge_response(100_000_000))
    smuggled = (
        steps[0],
        steps[1].model_copy(update={"kind": "cctp_receive"}),
    )

    with pytest.raises(ValueError, match="cctp_receive"):
        TxPlan(steps=smuggled, summary="bridge", bundles=(bundle,))  # type: ignore[arg-type]


def test_the_cctp_config_names_each_chains_contracts() -> None:
    config = CctpConfigResponse.model_validate(cctp_config_payload())

    base = config.chain(BASE)
    assert base is not None
    assert base.cctp_domain == BASE_DOMAIN
    assert base.message_transmitter == MESSAGE_TRANSMITTER
    assert config.chain(1) is None


# --- Circle client ------------------------------------------------------------


def circle(handler: object, sleeps: list[float] | None = None) -> CircleClient:
    return CircleClient(
        "https://iris.test",
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
        sleep=(sleeps if sleeps is not None else []).append,
    )


def test_circle_is_asked_by_source_domain_and_transaction_hash() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "messages": [
                    {
                        "message": "0x" + SOURCE.hex(),
                        "attestation": "0xabcd",
                        "eventNonce": "0x07",
                        "cctpVersion": 2,
                        "status": "complete",
                        "decodedMessage": {"ignored": True},
                    }
                ]
            },
        )

    [message] = circle(handler).messages(6, "0xfeed").messages

    assert seen[0].url.path == "/v2/messages/6"
    assert seen[0].url.params["transactionHash"] == "0xfeed"
    assert message.complete


def test_a_transaction_circle_has_not_seen_is_not_ready_rather_than_an_error() -> None:
    response = circle(lambda _request: httpx.Response(404, json={"error": "x"}))

    assert response.messages(6, "0xfeed").messages == ()


def test_circle_rate_limits_are_retried_boundedly() -> None:
    calls: list[int] = []
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(429)
        return httpx.Response(200, json={"messages": []})

    assert circle(handler, sleeps).messages(6, "0xfeed").messages == ()
    assert len(calls) == 3
    assert len(sleeps) == 2


def test_circle_errors_beyond_the_retries_surface() -> None:
    with pytest.raises(CircleError):
        circle(lambda _request: httpx.Response(503)).messages(6, "0xfeed")


def test_reattestation_is_requested_by_nonce() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=json.dumps({"message": "ok"}))

    circle(handler).reattest("0x07")

    assert seen[0].method == "POST"
    assert seen[0].url.path == "/v2/reattest/0x07"
