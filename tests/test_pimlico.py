from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from open_allocator.exec import paymaster_registry, safe_deployment
from open_allocator.exec.pimlico import (
    PimlicoClient,
    PimlicoError,
    PimlicoPaymasterAdapter,
    PimlicoRpcError,
    TokenQuote,
)

BASE = 8453
MONAD = 143
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
PAYMASTER_V07 = "0x777777777777AeC03fd955926DbF81597e66834C"
API_KEY = "pim_secret_key"

ENTRY_POINT_V07 = paymaster_registry.ENTRY_POINT_V07


class FakeEndpoint:
    """A Pimlico endpoint that records calls and replies from a script."""

    def __init__(self, replies: dict[str, Any]) -> None:
        self.replies = replies
        self.calls: list[dict[str, Any]] = []
        self.urls: list[str] = []

    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            self.calls.append(body)
            self.urls.append(str(request.url))
            method = body["method"]
            if method not in self.replies:
                return httpx.Response(
                    200,
                    json={
                        "jsonrpc": "2.0",
                        "id": body["id"],
                        "error": {"code": -32601, "message": f"unknown {method}"},
                    },
                )
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": self.replies[method],
                },
            )

        return httpx.MockTransport(handle)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=self.transport())

    def params(self, method: str) -> list[Any]:
        for call in self.calls:
            if call["method"] == method:
                return call["params"]
        raise AssertionError(f"{method} was never called")


QUOTE_REPLY = {
    "quotes": [
        {
            "paymaster": PAYMASTER_V07,
            "token": USDC,
            "postOpGas": "0xa7f8",
            "exchangeRate": "0xe9d61943a68eaf17e8",
            "exchangeRateNativeToUsd": "0xe9e52828",
            "balanceSlot": "0x2",
            "allowanceSlot": "0x3",
        }
    ]
}


def adapter_for(
    endpoint: FakeEndpoint, chain_id: int = BASE
) -> PimlicoPaymasterAdapter:
    return PimlicoPaymasterAdapter(
        PimlicoClient(chain_id=chain_id, api_key=API_KEY, client=endpoint.client())
    )


# --- endpoint shape --------------------------------------------------------


def test_the_endpoint_embeds_the_chain_id_and_the_api_key() -> None:
    endpoint = FakeEndpoint({"pimlico_getUserOperationGasPrice": {"fast": {}}})
    PimlicoClient(chain_id=MONAD, api_key=API_KEY, client=endpoint.client()).call(
        "pimlico_getUserOperationGasPrice", []
    )

    # One configured URL cannot serve a multi-chain deployment, which is why the
    # API key rather than a URL is the unit of config.
    assert endpoint.urls[0] == f"https://api.pimlico.io/v2/{MONAD}/rpc?apikey={API_KEY}"


def test_the_api_key_never_reaches_a_repr_or_an_error() -> None:
    endpoint = FakeEndpoint({})
    client = PimlicoClient(chain_id=BASE, api_key=API_KEY, client=endpoint.client())

    assert API_KEY not in repr(client)
    # The key is in the URL, so an error that quoted the URL would leak it.
    with pytest.raises(PimlicoRpcError) as error:
        client.call("eth_sendUserOperation", [])
    assert API_KEY not in str(error.value)


def test_a_json_rpc_error_becomes_a_typed_error() -> None:
    endpoint = FakeEndpoint({})

    with pytest.raises(PimlicoRpcError) as error:
        adapter_for(endpoint).send({})

    assert error.value.code == -32601
    assert "eth_sendUserOperation" in str(error.value)


# --- the live quote is the only honest cost --------------------------------


def test_token_quote_reads_the_live_rate() -> None:
    endpoint = FakeEndpoint({"pimlico_getTokenQuotes": QUOTE_REPLY})

    quote = adapter_for(endpoint).token_quote(USDC)

    assert quote == TokenQuote(
        paymaster=PAYMASTER_V07,
        token=USDC,
        post_op_gas=0xA7F8,
        exchange_rate=0xE9D61943A68EAF17E8,
        exchange_rate_native_to_usd=0xE9E52828,
    )


def test_token_quote_asks_the_entry_point_the_safe_can_actually_use() -> None:
    endpoint = FakeEndpoint({"pimlico_getTokenQuotes": QUOTE_REPLY})

    adapter_for(endpoint).token_quote(USDC)

    params = endpoint.params("pimlico_getTokenQuotes")
    assert params[0] == {"tokens": [USDC]}
    # v0.7: Safe4337Module pins this immutably and has no v0.8 release.
    assert params[1] == ENTRY_POINT_V07
    assert params[2] == hex(BASE)


def test_the_quoted_cost_includes_the_paymasters_own_postop_gas() -> None:
    quote = TokenQuote(
        paymaster=PAYMASTER_V07, token=USDC, post_op_gas=1_000, exchange_rate=10**18
    )

    # At a 1:1 rate the token cost is (gas + postOpGas) * fee — the paymaster
    # charges for collecting payment too, so ignoring postOpGas under-quotes.
    assert quote.token_cost(gas_limit=100_000, max_fee_per_gas=2) == 202_000


def test_a_missing_quote_is_an_error_not_a_silent_zero() -> None:
    endpoint = FakeEndpoint({"pimlico_getTokenQuotes": {"quotes": []}})

    with pytest.raises(PimlicoError, match="no token quote"):
        adapter_for(endpoint).token_quote(USDC)


# --- sponsorship: ERC-20 mode ---------------------------------------------


def test_sponsor_requests_erc20_mode_for_the_token() -> None:
    endpoint = FakeEndpoint(
        {
            "pm_getPaymasterData": {
                "paymaster": PAYMASTER_V07,
                "paymasterData": "0x01000066d1a1a4",
            }
        }
    )
    user_op = {"sender": "0x" + "ab" * 20, "nonce": "0x0", "callData": "0xdead"}

    sponsored = adapter_for(endpoint).sponsor(user_op, token=USDC)

    params = endpoint.params("pm_getPaymasterData")
    # ERC-7677 context: {token} selects ERC-20 mode over sponsorship mode.
    assert params[3] == {"token": USDC}
    assert params[1] == ENTRY_POINT_V07
    # The paymaster fields are merged in; the original call survives.
    assert sponsored["paymaster"] == PAYMASTER_V07
    assert sponsored["paymasterData"] == "0x01000066d1a1a4"
    assert sponsored["callData"] == "0xdead"


def test_send_returns_the_user_op_hash() -> None:
    endpoint = FakeEndpoint({"eth_sendUserOperation": "0x" + "11" * 32})

    assert adapter_for(endpoint).send({"sender": "0x" + "ab" * 20}) == "0x" + "11" * 32
    assert endpoint.params("eth_sendUserOperation")[1] == ENTRY_POINT_V07


def test_a_pending_user_operation_has_no_receipt_yet() -> None:
    endpoint = FakeEndpoint({"eth_getUserOperationReceipt": None})

    assert adapter_for(endpoint).receipt("0x" + "11" * 32) is None


def test_gas_estimates_are_decoded_from_hex() -> None:
    endpoint = FakeEndpoint(
        {
            "eth_estimateUserOperationGas": {
                "callGasLimit": "0x13880",
                "verificationGasLimit": "0x60B01",
                "preVerificationGas": "0xD3E3",
            }
        }
    )

    estimate = adapter_for(endpoint).estimate_gas({"sender": "0x" + "ab" * 20})

    assert estimate["callGasLimit"] == 0x13880
    assert estimate["verificationGasLimit"] == 0x60B01


def test_monad_is_reachable_with_the_same_adapter() -> None:
    endpoint = FakeEndpoint({"pimlico_getTokenQuotes": QUOTE_REPLY})

    quote = adapter_for(endpoint, chain_id=MONAD).token_quote(USDC)

    assert quote.paymaster == PAYMASTER_V07
    assert endpoint.params("pimlico_getTokenQuotes")[2] == hex(MONAD)


# --- the acceptance path: deploy + execute + pay gas, all in one op --------


def test_a_first_user_operation_deploys_the_safe_and_pays_its_gas_in_usdc() -> None:
    from open_allocator.exec.safe_deployment import SafeSeed
    from open_allocator.exec.user_operation import (
        Call,
        build_user_operation,
        paymaster_calls,
    )

    endpoint = FakeEndpoint(
        {
            "pimlico_getTokenQuotes": QUOTE_REPLY,
            "pm_getPaymasterData": {
                "paymaster": PAYMASTER_V07,
                "paymasterData": "0x01000066d1a1a4",
            },
            "eth_sendUserOperation": "0x" + "77" * 32,
        }
    )
    adapter = adapter_for(endpoint, chain_id=MONAD)
    seed = SafeSeed(owners=("0x" + "11" * 20,), threshold=1)
    vault = "0x" + "cc" * 20

    quote = adapter.token_quote(USDC)
    user_op = build_user_operation(
        sender="0x" + "ab" * 20,
        nonce=0,
        calls=paymaster_calls(
            Call(to=vault, data="0xdeadbeef"), token=USDC, paymaster=quote.paymaster
        ),
        seed=seed,
        deployed=False,
        signature="0x" + "ee" * 65,
    )
    user_op_hash = adapter.send(adapter.sponsor(user_op, token=USDC))

    sent = endpoint.params("eth_sendUserOperation")[0]
    # Zero-ETH onboarding on Monad: the op carries its own deployment...
    assert sent["factory"].lower() == (
        safe_deployment.SAFE_PROXY_FACTORY.lower()
    )
    assert sent["factoryData"].startswith("0x")
    # ...pays for it in USDC...
    assert sent["paymaster"] == PAYMASTER_V07
    assert sent["paymasterData"] == "0x01000066d1a1a4"
    # ...and never asks for native gas.
    assert "value" not in sent
    assert user_op_hash == "0x" + "77" * 32
