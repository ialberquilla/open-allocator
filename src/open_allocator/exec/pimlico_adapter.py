from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import httpx
from web3 import Web3

from open_allocator.exec import (
    chains,
    paymaster_registry,
    safe_4337_signature,
    safe_deployment,
)
from open_allocator.exec import (
    entry_point as entry_point_reads,
)
from open_allocator.exec.erc4337_paymaster import (
    PaymasterConfigurationError,
    PaymasterError,
    PaymasterUnsupportedChain,
    PaymasterUserOperationRequest,
    PaymasterUserOperationSubmission,
)
from open_allocator.exec.pimlico import PimlicoClient, PimlicoPaymasterAdapter
from open_allocator.exec.safe_deployment import SafeSeed
from open_allocator.exec.user_operation import (
    Call,
    build_user_operation,
    paymaster_calls,
)

# Drives one userOp end to end against Pimlico: derive the Safe, read its nonce,
# quote the token, build, estimate, sponsor, sign, send.
#
# This is the piece that makes PAYMASTER_PROVIDER=pimlico reachable from the CLI.
# The lower-level modules (pimlico, user_operation, safe_4337_signature) were each
# usable on their own; nothing connected them to signer_from_config.

_GAS_LIMIT_PLACEHOLDERS = {
    "callGasLimit": "0x0",
    "verificationGasLimit": "0x0",
    "preVerificationGas": "0x0",
}


class PimlicoUserOperationAdapter:
    """A Safe's userOp submitted via Pimlico, paying gas in USDC.

    Safe-only by construction: the signature this builds is a Safe4337Module
    SafeOp, so a non-Safe smart account would produce a valid-looking op that
    fails validation on chain.
    """

    def __init__(
        self,
        *,
        api_key: str,
        owner_keys: Sequence[str],
        seed: SafeSeed | None = None,
        account_address: str | None = None,
        config: object | None = None,
        rpc_urls: Mapping[int, str] | None = None,
        module: str = safe_deployment.SAFE_4337_MODULE,
        http_client: httpx.Client | None = None,
        web3_factory: Callable[[str], Web3] | None = None,
        inclusion_timeout_s: float = 120.0,
        poll_interval_s: float = 2.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_key:
            raise PaymasterConfigurationError(
                "PIMLICO_API_KEY is required for PAYMASTER_PROVIDER=pimlico"
            )
        if not owner_keys:
            raise PaymasterConfigurationError(
                "ONE_TX_PRIVATE_KEY is required to sign user operations"
            )
        if seed is None and account_address is None:
            raise PaymasterConfigurationError(
                "SAFE_OWNERS (with SAFE_THRESHOLD) or PAYMASTER_ACCOUNT_ADDRESS "
                "is required to know which Safe is sending"
            )
        if seed is not None and len(owner_keys) < seed.threshold:
            # A userOp is signed in one shot: there is no propose→co-sign→execute
            # round trip for the co-owners to join (that is `safe + rpc`). Without
            # threshold-many keys here the op can only fail validation on chain,
            # so refuse now rather than pay to find out.
            raise PaymasterConfigurationError(
                f"SAFE_THRESHOLD is {seed.threshold} but only {len(owner_keys)} "
                f"owner key(s) are available to sign; a user operation is signed "
                f"in one shot, so an N-of-M Safe needs N keys here or the "
                f"`safe + rpc` submission path, which can collect signatures"
            )
        self._api_key = api_key
        self._owner_keys = tuple(owner_keys)
        self._seed = seed
        self._account_address = account_address
        self._config = config
        self._rpc_urls = dict(rpc_urls or {})
        self._module = module
        self._http_client = http_client
        self._web3_factory = web3_factory or _default_web3
        self._cached_address: str | None = account_address
        self._inclusion_timeout_s = inclusion_timeout_s
        self._poll_interval_s = poll_interval_s
        self._sleep = sleep

    def __repr__(self) -> str:
        return "PimlicoUserOperationAdapter(provider=pimlico, key=<redacted>)"

    def address(self) -> str:
        """The Safe's address — the same on every chain, so any chain answers.

        Derived rather than configured: one seed means one address,
        and asking the user to also paste the address invites the two to disagree.
        """
        if self._cached_address is not None:
            return self._cached_address
        seed = self._require_seed()
        for chain_id in self._candidate_chain_ids():
            url = self._rpc_url(chain_id)
            if url is None:
                continue
            address = safe_deployment.predict_address(
                self._web3(url),
                seed,
                chain_id=chain_id,
            )
            self._cached_address = address
            return address
        raise PaymasterConfigurationError(
            "no RPC URL is configured for any chain, so the Safe address cannot "
            "be derived; set RPC_URL_<chain id> for at least one chain"
        )

    def submit_user_operation(
        self,
        request: PaymasterUserOperationRequest,
    ) -> PaymasterUserOperationSubmission:
        chain_id = request.chain_id
        if not paymaster_registry.is_gas_payable(chain_id, provider="pimlico"):
            raise PaymasterUnsupportedChain(chain_id)

        pimlico = self._paymaster(chain_id)
        w3 = self._web3(self._require_rpc_url(chain_id))
        sender, deployed = self._sender(w3, chain_id)

        # Read the nonce rather than assume 0: the Safe may have sent ops before,
        # and a stale nonce is an AA25 rejection.
        nonce = entry_point_reads.get_nonce(
            w3,
            sender,
            entry_point=pimlico.entry_point,
        )
        token = request.gas_token_address

        # The paymaster address comes from the live quote, not the registry: it
        # is authoritative at submission time and the constant is only a fallback.
        quote = pimlico.token_quote(token)
        calls = paymaster_calls(
            [
                Call(to=call.to, data=call.data, value=call.value)
                for call in request.calls
            ],
            token=token,
            paymaster=quote.paymaster,
        )

        user_op = build_user_operation(
            sender=sender,
            nonce=nonce,
            calls=calls,
            seed=self._seed,
            deployed=deployed,
            signature=safe_4337_signature.dummy_signature(len(self._owner_keys)),
        )
        user_op.update(_hex_values(pimlico.gas_price()))
        # pm_getPaymasterStubData rejects a userOp whose gas limits are absent,
        # but the limits come from an estimate that needs the stub first. Seed
        # zeros to break the cycle — the estimate below overwrites all three.
        user_op.update(_GAS_LIMIT_PLACEHOLDERS)
        user_op.update(
            _hex_values(pimlico.estimate_gas(pimlico.stub_data(user_op, token=token)))
        )

        # Sponsor before signing: paymasterAndData is inside the SafeOp hash, so
        # signing first would produce a signature for a different operation.
        sponsored = pimlico.sponsor(user_op, token=token)
        signed = safe_4337_signature.sign_user_operation(
            sponsored,
            private_keys=self._owner_keys,
            chain_id=chain_id,
            module=self._module,
            entry_point=pimlico.entry_point,
        )

        user_op_hash = pimlico.send(signed)
        message = (
            f"user operation submitted via Pimlico on "
            f"{chains.chain_name(chain_id)}, gas paid in USDC"
            + ("" if deployed else "; deploys the Safe in this operation")
        )

        # Wait for it: the next operation from this Safe reads the nonce and the
        # deployment status from the chain, and both are wrong until this one is
        # mined — the second op re-sends the factory and the EntryPoint rejects
        # it with AA10. Ops from one sender are sequential whether we like it or
        # not.
        included = self._await_inclusion(pimlico, user_op_hash)
        if included is None:
            return PaymasterUserOperationSubmission(
                user_op_hash=user_op_hash,
                status="submitted",
                message=f"{message}; still pending after "
                f"{self._inclusion_timeout_s:.0f}s",
            )
        return _submission_from_receipt(user_op_hash, included, message)

    def _await_inclusion(
        self,
        pimlico: PimlicoPaymasterAdapter,
        user_op_hash: str,
    ) -> Mapping[str, Any] | None:
        deadline = time.monotonic() + self._inclusion_timeout_s
        while True:
            receipt = pimlico.receipt(user_op_hash)
            if receipt is not None:
                return receipt
            if time.monotonic() >= deadline:
                return None
            self._sleep(self._poll_interval_s)

    def _sender(self, w3: Web3, chain_id: int) -> tuple[str, bool]:
        """The Safe and whether it already exists.

        Checked, never guessed: a redeploy does not raise, it reverts with
        status 0 and burns the gas, so build_user_operation makes this explicit.
        """
        if self._seed is not None:
            status = safe_deployment.deployment_status(
                w3, self._seed, chain_id=chain_id
            )
            self._cached_address = status.address
            return status.address, status.deployed
        address = Web3.to_checksum_address(str(self._account_address))
        if not safe_deployment.is_deployed(w3, address):
            raise PaymasterConfigurationError(
                f"the Safe at {address} is not deployed on "
                f"{chains.chain_name(chain_id)} and PAYMASTER_ACCOUNT_ADDRESS "
                f"alone cannot deploy it; set SAFE_OWNERS and SAFE_THRESHOLD so "
                f"the first operation can deploy it"
            )
        return address, True

    def _paymaster(self, chain_id: int) -> PimlicoPaymasterAdapter:
        return PimlicoPaymasterAdapter(
            PimlicoClient(
                chain_id=chain_id,
                api_key=self._api_key,
                client=self._http_client,
            )
        )

    def _require_seed(self) -> SafeSeed:
        if self._seed is None:
            raise PaymasterConfigurationError(
                "SAFE_OWNERS and SAFE_THRESHOLD are required to derive the Safe"
            )
        return self._seed

    def _candidate_chain_ids(self) -> tuple[int, ...]:
        configured = tuple(self._rpc_urls)
        return configured + tuple(
            chain_id
            for chain_id in paymaster_registry.PAYMASTER_CHAINS
            if chain_id not in self._rpc_urls
        )

    def _rpc_url(self, chain_id: int) -> str | None:
        if chain_id in self._rpc_urls:
            return self._rpc_urls[chain_id]
        return chains.rpc_url(chain_id, self._config)

    def _require_rpc_url(self, chain_id: int) -> str:
        url = self._rpc_url(chain_id)
        if url is None:
            raise PaymasterConfigurationError(
                f"no RPC URL for chain {chain_id}; the bundler cannot tell us the "
                f"Safe's nonce or whether it is deployed, so set RPC_URL_{chain_id}"
            )
        return url

    def _web3(self, rpc_url: str) -> Web3:
        return self._web3_factory(rpc_url)


def _submission_from_receipt(
    user_op_hash: str,
    receipt: Mapping[str, Any],
    message: str,
) -> PaymasterUserOperationSubmission:
    inner = receipt.get("receipt")
    inner = inner if isinstance(inner, Mapping) else {}
    if receipt.get("success") is False:
        # Included and reverted still costs the user gas, so it must not read as
        # a success anywhere downstream.
        reason = receipt.get("reason") or "no reason given"
        raise PaymasterError(
            f"user operation {user_op_hash} reverted on chain: {reason}"
        )
    return PaymasterUserOperationSubmission(
        user_op_hash=user_op_hash,
        transaction_hash=_optional_str(inner.get("transactionHash")),
        status="included",
        block_number=_optional_hex_int(inner.get("blockNumber")),
        gas_used=_optional_hex_int(
            inner.get("gasUsed", receipt.get("actualGasUsed")),
        ),
        message=message,
    )


def _optional_str(value: object) -> str | None:
    return str(value) if isinstance(value, str) and value else None


def _optional_hex_int(value: object) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value:
        return int(value, 16 if value.startswith("0x") else 10)
    return 0


def _default_web3(rpc_url: str) -> Web3:
    return Web3(Web3.HTTPProvider(rpc_url))


def pimlico_adapter_from_config(config: object) -> PimlicoUserOperationAdapter:
    """Build the adapter from config, failing with the env var a user can act on."""
    account_type = getattr(config, "paymaster_account_type", None)
    owners = getattr(config, "safe_owners", None)
    if account_type != "safe" and owners is None:
        raise PaymasterConfigurationError(
            "PAYMASTER_PROVIDER=pimlico signs a Safe4337Module operation, so it "
            "needs a Safe: set PAYMASTER_ACCOUNT_TYPE=safe with SAFE_OWNERS, or "
            "use PAYMASTER_PROVIDER=generic-http for a non-Safe smart account"
        )

    seed = None
    if owners is not None:
        threshold = getattr(config, "safe_threshold", None)
        if threshold is None:
            raise PaymasterConfigurationError(
                "SAFE_THRESHOLD is required with SAFE_OWNERS"
            )
        seed = SafeSeed(
            # Order is preserved deliberately: it feeds setup() and therefore the
            # Safe's address. Sorting here would silently move the Safe.
            owners=tuple(owners),
            threshold=int(threshold),
            salt_nonce=int(getattr(config, "safe_salt_nonce", 0) or 0),
        )

    return PimlicoUserOperationAdapter(
        api_key=_secret(getattr(config, "pimlico_api_key", None)) or "",
        owner_keys=_owner_keys(config),
        seed=seed,
        account_address=getattr(config, "paymaster_account_address", None),
        config=config,
    )


def _owner_keys(config: object) -> tuple[str, ...]:
    key = _secret(getattr(config, "private_key", None))
    return (key,) if key else ()


def _secret(value: object) -> str | None:
    if value is None:
        return None
    getter = getattr(value, "get_secret_value", None)
    return str(getter()) if callable(getter) else str(value)


def _hex_values(values: Mapping[str, Any]) -> dict[str, str]:
    """Gas fields go over JSON-RPC as hex quantities, not decimals."""
    return {
        key: value if isinstance(value, str) else hex(int(value))
        for key, value in values.items()
    }


__all__ = ["PimlicoUserOperationAdapter", "pimlico_adapter_from_config"]
