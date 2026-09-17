from __future__ import annotations

import json
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path
from typing import Any

import httpx
import pytest
from eth_abi import encode as abi_encode
from eth_account import Account
from eth_utils import keccak
from web3 import Web3

from open_allocator.core.policy import PolicyResult
from open_allocator.core.types import TxPlan
from open_allocator.exec import (
    bundle_execution,
    calldata,
    paymaster_registry,
    safe_4337_signature,
    safe_deployment,
)
from open_allocator.exec.client import InstrumentCalldataResponse
from open_allocator.exec.erc4337_paymaster import (
    Erc4337PaymasterSigner,
    PaymasterConfigurationError,
    PaymasterError,
    PaymasterUnsupportedChain,
    PaymasterUserOperationRequest,
    UserOperationCall,
    _adapter_from_config,
)
from open_allocator.exec.pimlico import PimlicoError
from open_allocator.exec.pimlico_adapter import (
    PimlicoUserOperationAdapter,
    pimlico_adapter_from_config,
)
from open_allocator.exec.safe_deployment import SafeSeed
from open_allocator.exec.user_operation import MAX_UINT256, MULTISEND_CALL_ONLY

BASE = 8453
FANTOM = 250  # deliberately not in PAYMASTER_CHAINS
USDC = Web3.to_checksum_address("0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913")
PAYMASTER = Web3.to_checksum_address("0x777777777777AeC03fd955926DbF81597e66834C")
VAULT = Web3.to_checksum_address("0x" + "cc" * 20)
API_KEY = "pim_secret_key"
SAFE = Web3.to_checksum_address("0x" + "5a" * 20)
USER_OP_HASH = "0x" + "ab" * 32
TX_HASH = "0x" + "cd" * 32

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
            # All three tiers, as the real endpoint quotes them: the tier is a
            # multiplier on what the paymaster charges in USDC, so which one is
            # picked has to be visible to a test.
            "pimlico_getUserOperationGasPrice": {
                "slow": {
                    "maxFeePerGas": "0x1dcd6500",
                    "maxPriorityFeePerGas": "0xf4240",
                },
                "standard": {
                    "maxFeePerGas": "0x3b9aca00",
                    "maxPriorityFeePerGas": "0xf4240",
                },
                "fast": {
                    "maxFeePerGas": "0x59682f00",
                    "maxPriorityFeePerGas": "0xf4240",
                },
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
            "eth_getUserOperationReceipt": {
                "userOpHash": USER_OP_HASH,
                "success": True,
                "actualGasUsed": "0x1234",
                "receipt": {
                    "transactionHash": TX_HASH,
                    "blockNumber": "0x64",
                    "gasUsed": "0x1234",
                },
            },
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
_ALLOWANCE = keccak(text="allowance(address,address)")[:4]

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

    def __init__(
        self,
        *,
        nonce: int = 0,
        deployed: bool = True,
        allowance: int = 0,
    ) -> None:
        self.eth = self
        self._nonce = nonce
        self._deployed = deployed
        # A fresh Safe has approved nothing, so the default here is the state
        # the first operation actually finds.
        self._allowance = allowance
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
        if selector == _ALLOWANCE:
            return self._allowance.to_bytes(32, "big")
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
    inclusion_timeout_s: float = 120.0,
    allowance: int = 0,
    fee_tier: str = "standard",
) -> PimlicoUserOperationAdapter:
    w3 = FakeWeb3(nonce=nonce, deployed=deployed, allowance=allowance)
    return PimlicoUserOperationAdapter(
        api_key=API_KEY,
        owner_keys=[OWNER_KEY],
        seed=seed,
        account_address=account_address,
        rpc_urls={BASE: "https://base.invalid"},
        fee_tier=fee_tier,
        http_client=endpoint.client(),
        web3_factory=lambda url: w3,
        poll_interval_s=0,
        sleep=lambda _seconds: None,
        inclusion_timeout_s=inclusion_timeout_s,
    )


def _request(chain_id: int = BASE) -> PaymasterUserOperationRequest:
    return PaymasterUserOperationRequest(
        sender=SAFE,
        chain_id=chain_id,
        entry_point=safe_4337_signature.paymaster_registry.ENTRY_POINT_V07,
        calls=(UserOperationCall(to=VAULT, data="0xdeadbeef", value=0),),
        gas_token_address=USDC,
        account_type="safe",
    )


# --- the whole flow, in order ----------------------------------------------


def test_submit_sends_a_signed_user_operation() -> None:
    endpoint = FakeEndpoint()
    submission = _adapter(endpoint).submit_user_operation(_request())

    assert submission.user_op_hash == USER_OP_HASH
    assert submission.status == "included"
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


# --- preparation: built and estimated, never signed or sent ------------------

_SUBMISSION_METHODS = ("pm_getPaymasterData", "eth_sendUserOperation")


def test_prepare_estimates_the_operation_without_signing_or_sending() -> None:
    endpoint = FakeEndpoint()
    prepared = _adapter(endpoint).prepare_user_operation(_request())

    methods = endpoint.methods()
    assert "eth_estimateUserOperationGas" in methods
    assert not any(method in methods for method in _SUBMISSION_METHODS)
    assert prepared.sender == SAFE
    assert prepared.chain_id == BASE
    assert prepared.entry_point == paymaster_registry.ENTRY_POINT_V07
    # Still the stub: nothing was signed.
    stub = safe_4337_signature.dummy_signature(1)
    assert prepared.user_operation["signature"] == stub
    assert prepared.gas.call_gas_limit == 0x186A0
    assert prepared.gas.verification_gas_limit == 0x30D40
    assert prepared.gas.pre_verification_gas == 0xC350
    assert prepared.gas.max_fee_per_gas == 0x3B9ACA00
    assert prepared.gas.max_priority_fee_per_gas == 0xF4240
    assert prepared.paymaster.paymaster == PAYMASTER
    assert prepared.paymaster.token == USDC
    assert prepared.paymaster.exchange_rate == 0x1BC16D674EC80000
    assert prepared.paymaster.post_op_gas == 0x1388


def _erc20_stub(token: str = USDC, *, post_op_gas: int = 0x1388) -> dict[str, Any]:
    body = bytes([0x03, 0x00]) + bytes(12) + bytes.fromhex(token[2:])
    body += post_op_gas.to_bytes(16, "big") + (10**18).to_bytes(32, "big")
    body += (1).to_bytes(16, "big") + bytes(20) + b"\x01" * 65
    return {
        "paymaster": PAYMASTER,
        "paymasterPostOpGasLimit": "0x4e20",
        "paymasterData": "0x" + body.hex(),
    }


_ESTIMATE_WITH_PAYMASTER_LIMITS = {
    "callGasLimit": "0x186a0",
    "verificationGasLimit": "0x30d40",
    "preVerificationGas": "0xc350",
    "paymasterVerificationGasLimit": "0x7530",
    "paymasterPostOpGasLimit": "0x3a98",
}


def test_prepare_bounds_the_gas_token_charge_from_the_stub_config() -> None:
    endpoint = FakeEndpoint(
        pm_getPaymasterStubData=_erc20_stub(),
        eth_estimateUserOperationGas=_ESTIMATE_WITH_PAYMASTER_LIMITS,
    )
    prepared = _adapter(endpoint).prepare_user_operation(_request())

    # Where the stub and the estimate or quote disagree, the larger value is
    # bounded: the stub's postOp limit (0x4e20) over the estimate's (0x3a98),
    # and the quote's rate (2e18) over the stub's (1e18).
    limits = 0x30D40 + 0x186A0 + 0xC350 + 0x7530 + 0x4E20
    penalty = (0x186A0 + 0x4E20) // 10
    expected = (limits + penalty + 0x1388) * 0x3B9ACA00 * 2 * 10**18 // 10**18
    assert prepared.max_gas_token_charge_raw == str(expected)


def test_prepare_does_not_invent_a_charge_from_an_unparseable_stub() -> None:
    """A stub without the ERC-20 config could be hiding a constant fee."""
    endpoint = FakeEndpoint(
        eth_estimateUserOperationGas=_ESTIMATE_WITH_PAYMASTER_LIMITS
    )
    prepared = _adapter(endpoint).prepare_user_operation(_request())
    assert prepared.max_gas_token_charge_raw is None


def test_prepare_does_not_bound_a_charge_in_another_token() -> None:
    endpoint = FakeEndpoint(
        pm_getPaymasterStubData=_erc20_stub("0x" + "99" * 20),
        eth_estimateUserOperationGas=_ESTIMATE_WITH_PAYMASTER_LIMITS,
    )
    prepared = _adapter(endpoint).prepare_user_operation(_request())
    assert prepared.max_gas_token_charge_raw is None


def test_prepare_carries_paymaster_gas_limits_when_estimated() -> None:
    endpoint = FakeEndpoint(
        eth_estimateUserOperationGas={
            "callGasLimit": "0x186a0",
            "verificationGasLimit": "0x30d40",
            "preVerificationGas": "0xc350",
            "paymasterVerificationGasLimit": "0x7530",
            "paymasterPostOpGasLimit": "0x3a98",
        }
    )
    prepared = _adapter(endpoint).prepare_user_operation(_request())

    assert prepared.gas.paymaster_verification_gas_limit == 0x7530
    assert prepared.gas.paymaster_post_op_gas_limit == 0x3A98


def test_prepare_fails_closed_on_an_incomplete_estimate() -> None:
    endpoint = FakeEndpoint(eth_estimateUserOperationGas={"callGasLimit": "0x1"})
    with pytest.raises(PaymasterError, match="verificationGasLimit"):
        _adapter(endpoint).prepare_user_operation(_request())


def test_prepare_includes_deployment_for_a_counterfactual_safe() -> None:
    endpoint = FakeEndpoint()
    prepared = _adapter(
        endpoint,
        deployed=False,
        seed=SafeSeed(owners=(OWNER,), threshold=1),
        account_address=None,
    ).prepare_user_operation(_request())

    assert prepared.deployed is False
    assert prepared.includes_deployment is True
    assert prepared.factory is not None and prepared.factory_data is not None
    # The bundler estimated the operation that deploys the Safe, not a bare call.
    estimated = next(
        call["params"][0]
        for call in endpoint.calls
        if call["method"] == "eth_estimateUserOperationGas"
    )
    assert estimated["factory"] == prepared.factory
    assert estimated["factoryData"] == prepared.factory_data
    assert not any(method in endpoint.methods() for method in _SUBMISSION_METHODS)


def test_prepare_omits_deployment_for_a_deployed_safe() -> None:
    prepared = _adapter(
        FakeEndpoint(),
        deployed=True,
        seed=SafeSeed(owners=(OWNER,), threshold=1),
        account_address=None,
    ).prepare_user_operation(_request())

    assert prepared.includes_deployment is False
    assert prepared.factory is None and prepared.factory_data is None
    assert "factory" not in prepared.user_operation


def test_prepare_estimates_the_paymaster_approval_with_the_calls() -> None:
    endpoint = FakeEndpoint()
    prepared = _adapter(endpoint).prepare_user_operation(_request())

    estimated = next(
        call["params"][0]
        for call in endpoint.calls
        if call["method"] == "eth_estimateUserOperationGas"
    )
    call_data = estimated["callData"].lower()
    assert PAYMASTER[2:].lower() in call_data
    assert VAULT[2:].lower() in call_data
    assert prepared.paymaster.approval_included is True


def test_prepare_reports_a_standing_approval_as_not_included() -> None:
    prepared = _adapter(FakeEndpoint(), allowance=MAX_UINT256).prepare_user_operation(
        _request()
    )
    assert prepared.paymaster.approval_included is False


def test_submit_prepares_afresh_instead_of_reusing_a_dry_run() -> None:
    """Nonce, fees, and paymaster data from a dry run are stale by submission."""
    endpoint = FakeEndpoint()
    w3 = FakeWeb3(nonce=3)
    adapter = PimlicoUserOperationAdapter(
        api_key=API_KEY,
        owner_keys=[OWNER_KEY],
        account_address=SAFE,
        rpc_urls={BASE: "https://base.invalid"},
        http_client=endpoint.client(),
        web3_factory=lambda _url: w3,
        poll_interval_s=0,
        sleep=lambda _seconds: None,
    )
    prepared = adapter.prepare_user_operation(_request())
    w3._nonce = 4

    adapter.submit_user_operation(_request())

    assert int(prepared.user_operation["nonce"], 16) == 3
    assert int(endpoint.sent_user_op()["nonce"], 16) == 4
    assert endpoint.methods().count("eth_estimateUserOperationGas") == 2


# --- a calldata plan through the real signer and adapter ----------------------


def _deposit_plan(account: str) -> TxPlan:
    payload = json.loads(
        (
            Path(__file__).parent / "fixtures" / "calldata-instrument-deposit-swap.json"
        ).read_text(encoding="utf-8")
    )
    payload.update(account=account, expiresAt=None)
    steps, bundle = calldata.plan_bundle(
        InstrumentCalldataResponse.model_validate(payload),
        leg_index=0,
        first_step_index=0,
    )
    return TxPlan(steps=steps, summary="deposit", bundles=(bundle,))


@pytest.mark.parametrize("deployed", [False, True], ids=["counterfactual", "deployed"])
def test_a_calldata_plan_is_prepared_without_sending_then_submitted_afresh(
    deployed: bool,
) -> None:
    endpoint = FakeEndpoint()
    seed = SafeSeed(owners=(OWNER,), threshold=1)
    adapter = _adapter(endpoint, deployed=deployed, seed=seed, account_address=None)
    signer = Erc4337PaymasterSigner(adapter=adapter, usdc_address=USDC)
    tx_plan = _deposit_plan(adapter.address())

    preparation = bundle_execution.prepare_plan(signer, tx_plan, _PlanConfig())

    assert not any(method in endpoint.methods() for method in _SUBMISSION_METHODS)
    [prepared] = preparation.preparations
    assert prepared.bundle_ids == (tx_plan.bundles[0].bundle_id,)
    assert prepared.includes_deployment is (not deployed)
    assert prepared.call_gas_limit == 0x186A0

    result = bundle_execution.execute_plan(
        object(),
        signer,
        tx_plan,
        stage="execute",
        policy_result=PolicyResult(ok=True, violations=()),
        completion_key=lambda bundle: f"leg:{bundle.leg_index}",
        log=lambda _bundle: bundle_execution.BundleLog(action_type="buy"),
        config=_PlanConfig(),
    )

    assert result.status == "success"
    # The dry run, the confirmed run's check before anything is sent, and once
    # more immediately before signing — never a stale estimate reused.
    assert endpoint.methods().count("eth_estimateUserOperationGas") == 3
    assert len(result.preparations) == 1
    assert endpoint.methods().count("eth_sendUserOperation") == 1
    sent = endpoint.sent_user_op()
    assert ("factory" in sent) is (not deployed)
    # One operation carrying every bundle call, in the order Darex returned them.
    call_data = sent["callData"].lower()
    cursor = 0
    for step in tx_plan.steps:
        cursor = call_data.index(step.to[2:].lower(), cursor)
        cursor = call_data.index(step.data[2:].lower(), cursor) + len(step.data) - 2


@dataclass(frozen=True)
class _PlanConfig:
    signer_mode: str = "erc4337-paymaster"
    paymaster_provider: str = "pimlico"
    pimlico_api_key: str = API_KEY
    _rpc_overrides: dict[int, str] = dataclass_field(
        default_factory=lambda: {BASE: "https://base.invalid"}
    )
    token_balance_reader: object = lambda _chain, _rpc, _token, _account: 10**30


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


# --- waiting for inclusion --------------------------------------------------


def test_submission_reports_the_transaction_not_just_the_user_op_hash() -> None:
    submission = _adapter(FakeEndpoint()).submit_user_operation(_request())

    assert submission.status == "included"
    assert submission.transaction_hash == TX_HASH
    assert submission.block_number == 0x64
    assert submission.gas_used == 0x1234


def test_a_reverted_operation_is_not_reported_as_a_success() -> None:
    endpoint = FakeEndpoint(
        eth_getUserOperationReceipt={
            "userOpHash": USER_OP_HASH,
            "success": False,
            "reason": "AA33 reverted",
            "receipt": {"transactionHash": TX_HASH},
        }
    )

    with pytest.raises(PaymasterError, match="reverted on chain: AA33 reverted"):
        _adapter(endpoint).submit_user_operation(_request())


def test_a_pending_operation_degrades_to_submitted_rather_than_hanging() -> None:
    """The next op needs this one mined, but a slow bundler is not a failure."""
    endpoint = FakeEndpoint(eth_getUserOperationReceipt=None)

    submission = _adapter(endpoint, inclusion_timeout_s=0).submit_user_operation(
        _request()
    )

    assert submission.status == "submitted"
    assert submission.transaction_hash is None
    assert "still pending" in (submission.message or "")


# --- batching ---------------------------------------------------------------


def _batch_request() -> PaymasterUserOperationRequest:
    return PaymasterUserOperationRequest(
        sender=SAFE,
        chain_id=BASE,
        entry_point=safe_4337_signature.paymaster_registry.ENTRY_POINT_V07,
        calls=(
            UserOperationCall(to=USDC, data="0xaaaaaaaa", value=0),
            UserOperationCall(to=VAULT, data="0xbbbbbbbb", value=0),
        ),
        gas_token_address=USDC,
        account_type="safe",
    )


def test_a_batch_goes_out_as_one_user_operation() -> None:
    """The point of batching: postOp charges after execution, so a batch that
    redeems into the account pays its own gas out of the proceeds."""
    endpoint = FakeEndpoint()

    adapter = _adapter(endpoint)
    adapter.submit_user_operation(_batch_request())

    assert endpoint.methods().count("eth_sendUserOperation") == 1
    call_data = endpoint.sent_user_op()["callData"]
    assert "aaaaaaaa" in call_data
    assert "bbbbbbbb" in call_data


def test_a_batch_still_carries_exactly_one_paymaster_approval() -> None:
    endpoint = FakeEndpoint()

    _adapter(endpoint).submit_user_operation(_batch_request())

    approvals = endpoint.sent_user_op()["callData"].count("095ea7b3")
    assert approvals == 1


def test_a_request_needs_at_least_one_call() -> None:
    with pytest.raises(ValueError, match="calls"):
        PaymasterUserOperationRequest(
            sender=SAFE,
            chain_id=BASE,
            entry_point=safe_4337_signature.paymaster_registry.ENTRY_POINT_V07,
            calls=(),
            gas_token_address=USDC,
        )


# --- what the operation costs in USDC ---------------------------------------
#
# Two things drive the gas bill on this path: the fee tier the bundler is asked
# for, and how much work rides in the op. Both were fixed at their most
# expensive setting — `fast`, and an approval on every single operation.


def test_the_fee_tier_defaults_to_standard_not_fast() -> None:
    """`fast` was hardcoded, which is the wrong default for a job that is not
    racing anyone — worth ~5% of the gas bill at the measured tier spread."""
    endpoint = FakeEndpoint()
    _adapter(endpoint).submit_user_operation(_request())
    assert int(endpoint.sent_user_op()["maxFeePerGas"], 16) == 0x3B9ACA00


def test_the_fee_tier_is_configurable_for_when_ops_sit_unincluded() -> None:
    endpoint = FakeEndpoint()
    _adapter(endpoint, fee_tier="fast").submit_user_operation(_request())
    assert int(endpoint.sent_user_op()["maxFeePerGas"], 16) == 0x59682F00


def test_a_tier_the_bundler_did_not_quote_is_an_error_not_another_tier() -> None:
    """Falling through would charge a price nobody asked for."""
    endpoint = FakeEndpoint(
        pimlico_getUserOperationGasPrice={
            "fast": {"maxFeePerGas": "0x59682f00", "maxPriorityFeePerGas": "0xf4240"}
        }
    )
    with pytest.raises(PimlicoError, match="tier 'standard' was not quoted"):
        _adapter(endpoint).submit_user_operation(_request())


def test_an_unknown_fee_tier_is_refused_at_construction() -> None:
    with pytest.raises(PaymasterConfigurationError, match="PAYMASTER_FEE_TIER"):
        PimlicoUserOperationAdapter(
            api_key=API_KEY,
            owner_keys=[OWNER_KEY],
            account_address=SAFE,
            fee_tier="instant",
        )


def test_from_config_carries_the_fee_tier() -> None:
    config = FakeConfig()
    config.paymaster_fee_tier = "slow"
    assert pimlico_adapter_from_config(config)._fee_tier == "slow"  # noqa: SLF001


def test_from_config_without_a_fee_tier_still_gets_the_default() -> None:
    """A config predating the setting must not construct an adapter with None."""
    assert pimlico_adapter_from_config(FakeConfig())._fee_tier == "standard"  # noqa: SLF001


# --- the approval that rode on every operation ------------------------------


def test_the_first_operation_approves_the_paymaster() -> None:
    endpoint = FakeEndpoint()
    _adapter(endpoint, allowance=0).submit_user_operation(_request())
    assert endpoint.sent_user_op()["callData"].count("095ea7b3") == 1


def test_a_standing_unlimited_approval_is_not_sent_again() -> None:
    """Once approved, the approval is dead weight in every later operation."""
    endpoint = FakeEndpoint()
    _adapter(endpoint, allowance=2**256 - 1).submit_user_operation(_request())
    assert "095ea7b3" not in endpoint.sent_user_op()["callData"]


def test_an_allowance_drawn_down_by_gas_charges_still_counts() -> None:
    """USDC decrements on transferFrom, so the standing allowance is never
    exactly what was approved — hence a floor rather than an equality check."""
    endpoint = FakeEndpoint()
    spent_on_gas = 5_000_000  # $5 of USDC, far more than this Safe has paid
    _adapter(endpoint, allowance=2**256 - 1 - spent_on_gas).submit_user_operation(
        _request()
    )
    assert "095ea7b3" not in endpoint.sent_user_op()["callData"]


def test_a_small_scoped_allowance_does_not_count_as_unlimited() -> None:
    endpoint = FakeEndpoint()
    _adapter(endpoint, allowance=10**12).submit_user_operation(_request())
    assert endpoint.sent_user_op()["callData"].count("095ea7b3") == 1


def test_dropping_the_approval_takes_a_single_call_out_of_multisend() -> None:
    """The real saving: one action plus one approval is a batch, and a batch is
    a MultiSendCallOnly delegatecall. Alone, the action is a direct call."""
    endpoint = FakeEndpoint()
    _adapter(endpoint, allowance=2**256 - 1).submit_user_operation(_request())

    call_data = endpoint.sent_user_op()["callData"].lower()
    assert MULTISEND_CALL_ONLY[2:].lower() not in call_data
    assert "deadbeef" in call_data


def test_an_undeployed_safe_approves_without_reading_an_allowance() -> None:
    """There is no contract to ask, and asking one that is not there returns 0
    anyway — but the read is skipped rather than relied on."""
    endpoint = FakeEndpoint()
    submission = _adapter(
        endpoint,
        deployed=False,
        seed=SafeSeed(owners=(OWNER,), threshold=1),
        account_address=None,
    ).submit_user_operation(_request())

    assert "factory" in endpoint.sent_user_op()
    assert endpoint.sent_user_op()["callData"].count("095ea7b3") == 1
    assert submission.status == "included"


def test_an_unreadable_allowance_sends_the_approval() -> None:
    """A redundant approval costs gas; a missing one reverts the operation. The
    unknown case has to fall on the cheap side of that."""

    class _Unreadable(FakeWeb3):
        def call(self, transaction: dict[str, Any]) -> bytes:
            data = bytes.fromhex(transaction["data"][2:])
            if data[:4] == _ALLOWANCE:
                raise RuntimeError("rpc down")
            return super().call(transaction)

    endpoint = FakeEndpoint()
    w3 = _Unreadable(allowance=2**256 - 1)
    adapter = PimlicoUserOperationAdapter(
        api_key=API_KEY,
        owner_keys=[OWNER_KEY],
        account_address=SAFE,
        rpc_urls={BASE: "https://base.invalid"},
        http_client=endpoint.client(),
        web3_factory=lambda url: w3,
        poll_interval_s=0,
        sleep=lambda _seconds: None,
    )
    adapter.submit_user_operation(_request())

    assert endpoint.sent_user_op()["callData"].count("095ea7b3") == 1


def test_allowances_observed_on_mainnet_count_as_unlimited() -> None:
    """Real standing allowances: MAX_UINT256 less a few cents of gas charges.

    Pinned because this is the shape the check has to keep recognising as
    charges accumulate over the life of an account.
    """
    endpoint = FakeEndpoint()
    for standing in (
        2**256 - 1 - 31_925,
        2**256 - 1 - 6_319,
    ):
        _adapter(endpoint, allowance=standing).submit_user_operation(_request())
        assert "095ea7b3" not in endpoint.sent_user_op()["callData"]
