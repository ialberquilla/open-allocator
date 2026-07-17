from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from eth_abi import encode as abi_encode
from eth_account import Account
from eth_utils import keccak
from web3 import Web3

from open_allocator.exec import paymaster_registry, safe_4337_signature, safe_deployment
from open_allocator.exec.erc4337_paymaster import (
    Erc4337PaymasterSigner,
    PaymasterConfigurationError,
    PaymasterUnsupportedChain,
    PaymasterUserOperationRequest,
    UserOperationCall,
    _adapter_from_config,
)
from open_allocator.exec.pimlico_adapter import (
    PimlicoUserOperationAdapter,
    pimlico_adapter_from_config,
)
from open_allocator.exec.safe_deployment import SafeSeed

BASE = 8453
FANTOM = 250  # deliberately not in PAYMASTER_CHAINS
USDC = Web3.to_checksum_address("0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913")
PAYMASTER = Web3.to_checksum_address("0x777777777777AeC03fd955926DbF81597e66834C")
VAULT = Web3.to_checksum_address("0x" + "cc" * 20)
API_KEY = "pim_secret_key"
SAFE = Web3.to_checksum_address("0x" + "5a" * 20)
USER_OP_HASH = "0x" + "ab" * 32

OWNER_KEY = "0x" + "11" * 32
OWNER = Account.from_key(OWNER_KEY).address


class FakeEndpoint:
    """A Pimlico endpoint that records calls and replies from a script."""

    def __init__(self, **overrides: Any) -> None:
        self.replies: dict[str, Any] = {
            "pimlico_getTokenQuotes": {
                "quotes": [
                    {
                        "paymaster": PAYMASTER,
                        "token": USDC,
                        "postOpGas": "0x1388",
                        "exchangeRate": "0x1bc16d674ec80000",
                    }
                ]
            },
            "pimlico_getUserOperationGasPrice": {
                "fast": {
                    "maxFeePerGas": "0x59682f00",
                    "maxPriorityFeePerGas": "0xf4240",
                }
            },
            "pm_getPaymasterStubData": {
                "paymaster": PAYMASTER,
                "paymasterData": "0x00",
            },
            "eth_estimateUserOperationGas": {
                "callGasLimit": "0x186a0",
                "verificationGasLimit": "0x30d40",
                "preVerificationGas": "0xc350",
            },
            "pm_getPaymasterData": {
                "paymaster": PAYMASTER,
                "paymasterVerificationGasLimit": "0x7530",
                "paymasterPostOpGasLimit": "0x3a98",
                "paymasterData": "0x" + USDC[2:].lower(),
            },
            "eth_sendUserOperation": USER_OP_HASH,
        }
        self.replies.update(overrides)
        self.calls: list[dict[str, Any]] = []

    def client(self) -> httpx.Client:
        def handle(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            self.calls.append(body)
            method = body["method"]
            if method not in self.replies:
                return httpx.Response(
                    200,
                    json={
                        "jsonrpc": "2.0",
                        "id": body["id"],
                        "error": {"code": -32601, "message": f"no reply for {method}"},
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

        return httpx.Client(transport=httpx.MockTransport(handle))

    def methods(self) -> list[str]:
        return [call["method"] for call in self.calls]

    def sent_user_op(self) -> dict[str, Any]:
        for call in self.calls:
            if call["method"] == "eth_sendUserOperation":
                return call["params"][0]
        raise AssertionError("no user operation was sent")


_PROXY_CREATION_CODE = keccak(text="proxyCreationCode()")[:4]
_GET_NONCE = keccak(text="getNonce(address,uint192)")[:4]

# Any bytes work: prediction only has to be self-consistent here, and a real
# factory's creation code is already exercised against live chains in
# test_safe_deployment.
_FAKE_CREATION_CODE = bytes.fromhex("6080604052")


class FakeWeb3:
    """Enough of a chain to derive a Safe and read its nonce.

    Dispatches on the selector rather than returning one canned word, so a call
    aimed at the wrong contract fails loudly instead of quietly decoding as
    whatever the fake happened to return.
    """

    def __init__(self, *, nonce: int = 0, deployed: bool = True) -> None:
        self.eth = self
        self._nonce = nonce
        self._deployed = deployed
        self.infrastructure = {
            Web3.to_checksum_address(safe_deployment.SAFE_PROXY_FACTORY),
            Web3.to_checksum_address(safe_deployment.SAFE_SINGLETON_L2),
        }

    def call(self, transaction: dict[str, Any]) -> bytes:
        data = bytes.fromhex(transaction["data"][2:])
        selector = data[:4]
        if selector == _PROXY_CREATION_CODE:
            return abi_encode(["bytes"], [_FAKE_CREATION_CODE])
        if selector == _GET_NONCE:
            return self._nonce.to_bytes(32, "big")
        raise AssertionError(f"unexpected eth_call selector {selector.hex()}")

    def get_code(self, address: str) -> bytes:
        # The Safe's own code is what `deployed` controls; the factory and
        # singleton must always be present or prediction refuses to run.
        if Web3.to_checksum_address(address) in self.infrastructure:
            return b"\x60\x60"
        return b"\x60\x60" if self._deployed else b""


def _adapter(
    endpoint: FakeEndpoint,
    *,
    nonce: int = 0,
    deployed: bool = True,
    seed: SafeSeed | None = None,
    account_address: str | None = SAFE,
) -> PimlicoUserOperationAdapter:
    w3 = FakeWeb3(nonce=nonce, deployed=deployed)
    return PimlicoUserOperationAdapter(
        api_key=API_KEY,
        owner_keys=[OWNER_KEY],
        seed=seed,
        account_address=account_address,
        rpc_urls={BASE: "https://base.invalid"},
        http_client=endpoint.client(),
        web3_factory=lambda url: w3,
    )


def _request(chain_id: int = BASE) -> PaymasterUserOperationRequest:
    return PaymasterUserOperationRequest(
        sender=SAFE,
        chain_id=chain_id,
        entry_point=safe_4337_signature.paymaster_registry.ENTRY_POINT_V07,
        call_data=UserOperationCall(to=VAULT, data="0xdeadbeef", value=0),
        gas_token_address=USDC,
        account_type="safe",
    )


# --- the whole flow, in order ----------------------------------------------


def test_submit_sends_a_signed_user_operation() -> None:
    endpoint = FakeEndpoint()
    submission = _adapter(endpoint).submit_user_operation(_request())

    assert submission.user_op_hash == USER_OP_HASH
    assert submission.status == "submitted"
    assert bytes.fromhex(endpoint.sent_user_op()["signature"][2:]) != b""


def test_submit_signs_the_operation_the_bundler_receives() -> None:
    """The signature must recover to the owner over the *sent* op's hash.

    This is the test that would have caught shipping build_user_operation()'s
    signature="0x" default: everything else about the op is well-formed.
    """
    endpoint = FakeEndpoint()
    _adapter(endpoint).submit_user_operation(_request())

    sent = endpoint.sent_user_op()
    raw = bytes.fromhex(sent["signature"][2:])
    digest = safe_4337_signature.operation_hash(sent, chain_id=BASE)
    assert Account._recover_hash(digest, signature=raw[12:]) == OWNER


def test_submit_sponsors_before_it_signs() -> None:
    """paymasterAndData is inside the hash, so signing first signs a different op."""
    endpoint = FakeEndpoint()
    _adapter(endpoint).submit_user_operation(_request())

    methods = endpoint.methods()
    assert methods.index("pm_getPaymasterData") < methods.index("eth_sendUserOperation")


def test_submit_estimates_with_a_stub_signature_then_replaces_it() -> None:
    endpoint = FakeEndpoint()
    _adapter(endpoint).submit_user_operation(_request())

    estimate = next(
        call
        for call in endpoint.calls
        if call["method"] == "eth_estimateUserOperationGas"
    )
    stub = estimate["params"][0]["signature"]
    assert len(bytes.fromhex(stub[2:])) == 12 + 65
    assert endpoint.sent_user_op()["signature"] != stub


def test_submit_reads_the_nonce_from_the_entry_point() -> None:
    endpoint = FakeEndpoint()
    _adapter(endpoint, nonce=7).submit_user_operation(_request())
    assert int(endpoint.sent_user_op()["nonce"], 16) == 7


def test_submit_approves_the_quoted_paymaster_not_the_registry_constant() -> None:
    """The live quote is authoritative; the constant is only a fallback."""
    other = Web3.to_checksum_address("0x" + "99" * 20)
    endpoint = FakeEndpoint(
        pimlico_getTokenQuotes={
            "quotes": [
                {
                    "paymaster": other,
                    "token": USDC,
                    "postOpGas": "0x1388",
                    "exchangeRate": "0x1bc16d674ec80000",
                }
            ]
        }
    )
    _adapter(endpoint).submit_user_operation(_request())
    assert other[2:].lower() in endpoint.sent_user_op()["callData"].lower()


def test_gas_fields_are_hex_quantities() -> None:
    """JSON-RPC takes hex, not decimal; a raw int is silently a different number."""
    endpoint = FakeEndpoint()
    _adapter(endpoint).submit_user_operation(_request())

    sent = endpoint.sent_user_op()
    for field in ("nonce", "callGasLimit", "verificationGasLimit", "maxFeePerGas"):
        assert isinstance(sent[field], str) and sent[field].startswith("0x")


# --- deployment ------------------------------------------------------------


def test_an_undeployed_safe_deploys_in_the_first_operation() -> None:
    endpoint = FakeEndpoint()
    submission = _adapter(
        endpoint,
        deployed=False,
        seed=SafeSeed(owners=(OWNER,), threshold=1),
        account_address=None,
    ).submit_user_operation(_request())

    sent = endpoint.sent_user_op()
    assert "factory" in sent and "factoryData" in sent
    assert "deploys the Safe" in (submission.message or "")


def test_a_deployed_safe_carries_no_factory_fields() -> None:
    """Including them for a live Safe reverts the op."""
    endpoint = FakeEndpoint()
    _adapter(
        endpoint,
        deployed=True,
        seed=SafeSeed(owners=(OWNER,), threshold=1),
        account_address=None,
    ).submit_user_operation(_request())
    assert "factory" not in endpoint.sent_user_op()


def test_the_sender_is_derived_from_the_seed_not_the_configured_address() -> None:
    """One seed means one address; a pasted address that disagrees is a bug."""
    endpoint = FakeEndpoint()
    _adapter(
        endpoint, seed=SafeSeed(owners=(OWNER,), threshold=1), account_address=None
    ).submit_user_operation(_request())
    assert endpoint.sent_user_op()["sender"] != SAFE


def test_an_undeployed_address_without_a_seed_says_what_to_set() -> None:
    """An address cannot be reversed into the seed needed to deploy it."""
    endpoint = FakeEndpoint()
    with pytest.raises(PaymasterConfigurationError, match="SAFE_OWNERS"):
        _adapter(endpoint, deployed=False).submit_user_operation(_request())


# --- guards ----------------------------------------------------------------


def test_a_chain_with_no_paymaster_row_is_rejected() -> None:
    endpoint = FakeEndpoint()
    with pytest.raises(PaymasterUnsupportedChain):
        _adapter(endpoint).submit_user_operation(_request(chain_id=FANTOM))


def test_every_gas_payable_chain_has_an_rpc_without_extra_configuration() -> None:
    """The RPC guard is defensive: chains.DEFAULT_CHAINS already covers every
    paymaster row, so a gas-payable chain never lacks a URL. A new paymaster row
    for a chain with no default RPC would trip this."""
    endpoint = FakeEndpoint()
    adapter = PimlicoUserOperationAdapter(
        api_key=API_KEY,
        owner_keys=[OWNER_KEY],
        account_address=SAFE,
        rpc_urls={},
        http_client=endpoint.client(),
    )
    for chain_id in paymaster_registry.PAYMASTER_CHAINS:
        assert adapter._require_rpc_url(chain_id)  # noqa: SLF001


def test_an_api_key_is_required() -> None:
    with pytest.raises(PaymasterConfigurationError, match="PIMLICO_API_KEY"):
        PimlicoUserOperationAdapter(
            api_key="", owner_keys=[OWNER_KEY], account_address=SAFE
        )


def test_an_owner_key_is_required_to_sign() -> None:
    with pytest.raises(PaymasterConfigurationError, match="ONE_TX_PRIVATE_KEY"):
        PimlicoUserOperationAdapter(
            api_key=API_KEY, owner_keys=[], account_address=SAFE
        )


def test_a_safe_is_required() -> None:
    with pytest.raises(PaymasterConfigurationError, match="SAFE_OWNERS"):
        PimlicoUserOperationAdapter(api_key=API_KEY, owner_keys=[OWNER_KEY])


def test_a_threshold_we_cannot_meet_is_refused_before_paying_for_it() -> None:
    """A userOp is signed in one shot — there is no co-signing round trip, so a
    2-of-2 Safe with one key on disk could only fail validation on chain."""
    other = Account.from_key("0x" + "22" * 32).address
    with pytest.raises(PaymasterConfigurationError, match="SAFE_THRESHOLD is 2"):
        PimlicoUserOperationAdapter(
            api_key=API_KEY,
            owner_keys=[OWNER_KEY],
            seed=SafeSeed(owners=(OWNER, other), threshold=2),
        )


def test_more_keys_than_the_threshold_is_fine() -> None:
    """Signing with every key we hold is allowed; Safe accepts >= threshold."""
    other_key = "0x" + "22" * 32
    adapter = PimlicoUserOperationAdapter(
        api_key=API_KEY,
        owner_keys=[OWNER_KEY, other_key],
        seed=SafeSeed(owners=(OWNER, Account.from_key(other_key).address), threshold=1),
    )
    assert adapter._owner_keys == (OWNER_KEY, other_key)  # noqa: SLF001


def test_repr_never_renders_the_api_key() -> None:
    """The key rides in the URL query string, so nothing may render a URL."""
    adapter = PimlicoUserOperationAdapter(
        api_key=API_KEY, owner_keys=[OWNER_KEY], account_address=SAFE
    )
    assert API_KEY not in repr(adapter)
    assert "api.pimlico.io" not in repr(adapter)


def test_owner_keys_never_render_either() -> None:
    adapter = PimlicoUserOperationAdapter(
        api_key=API_KEY, owner_keys=[OWNER_KEY], account_address=SAFE
    )
    assert OWNER_KEY not in repr(adapter)


# --- building from config --------------------------------------------------


class FakeConfig:
    paymaster_provider = "pimlico"
    paymaster_account_type = "safe"
    safe_owners = (OWNER,)
    safe_threshold = 1
    safe_salt_nonce = 3
    paymaster_account_address = None

    class _Key:
        @staticmethod
        def get_secret_value() -> str:
            return API_KEY

    class _Pk:
        @staticmethod
        def get_secret_value() -> str:
            return OWNER_KEY

    pimlico_api_key = _Key()
    private_key = _Pk()


def test_from_config_preserves_owner_order() -> None:
    """Owner order feeds setup() and therefore the address; sorting moves the Safe."""
    config = FakeConfig()
    config.safe_owners = (
        Web3.to_checksum_address("0x" + "bb" * 20),
        Web3.to_checksum_address("0x" + "aa" * 20),
    )
    config.safe_threshold = 1
    adapter = pimlico_adapter_from_config(config)
    assert adapter._seed.owners == config.safe_owners  # noqa: SLF001


def test_from_config_carries_the_salt_nonce() -> None:
    adapter = pimlico_adapter_from_config(FakeConfig())
    assert adapter._seed.salt_nonce == 3  # noqa: SLF001


def test_from_config_rejects_a_non_safe_account() -> None:
    config = FakeConfig()
    config.paymaster_account_type = "smart-account"
    config.safe_owners = None
    with pytest.raises(PaymasterConfigurationError, match="generic-http"):
        pimlico_adapter_from_config(config)


# --- the seam that was broken ----------------------------------------------
#
# pimlico.py and user_operation.py were complete and tested, but nothing routed
# PAYMASTER_PROVIDER=pimlico to them: _adapter_from_config knew only generic-http
# and raised "unknown paymaster provider" for the registry's own default.


def test_the_signer_builds_a_pimlico_adapter_for_the_pimlico_provider() -> None:
    adapter = _adapter_from_config(FakeConfig())
    assert isinstance(adapter, PimlicoUserOperationAdapter)


def test_circle_says_it_is_a_registry_row_not_an_adapter() -> None:
    """Selecting it used to give the same 'unknown provider' as a typo."""
    config = FakeConfig()
    config.paymaster_provider = "circle"
    with pytest.raises(PaymasterConfigurationError, match="not an adapter"):
        _adapter_from_config(config)


def test_an_unknown_provider_is_still_an_error() -> None:
    config = FakeConfig()
    config.paymaster_provider = "nonesuch"
    with pytest.raises(PaymasterConfigurationError, match="unknown paymaster provider"):
        _adapter_from_config(config)


def test_the_entry_point_defaults_to_the_registrys_v07() -> None:
    """A protocol singleton at one address on every chain is derivable, not a
    question for the user — but a Safe cannot use v0.8, so the default matters."""
    signer = Erc4337PaymasterSigner(adapter=object(), entry_point=None)
    assert signer._required_entry_point() == paymaster_registry.ENTRY_POINT_V07  # noqa: SLF001
