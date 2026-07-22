from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Literal, Protocol, runtime_checkable

import httpx
from pydantic import Field

from open_allocator.core.types import FrozenModel, TxStep
from open_allocator.exec import chains, paymaster_registry
from open_allocator.exec.composition import composition_from_config
from open_allocator.exec.signer import Receipt, SignerError


class PaymasterError(SignerError):
    pass


class PaymasterConfigurationError(PaymasterError):
    pass


class PaymasterRejected(PaymasterError):
    pass


class PaymasterUnsupportedChain(PaymasterError):
    def __init__(self, chain_id: int) -> None:
        self.chain_id = chain_id
        super().__init__(
            f"ERC-4337 USDC paymaster is not configured for chain {chain_id}"
        )


class UserOperationCall(FrozenModel):
    to: str
    data: str
    value: int = Field(ge=0)


class PaymasterUserOperationRequest(FrozenModel):
    sender: str
    chain_id: int
    entry_point: str
    # A sequence because a smart account can batch: the calls of one plan ride in
    # a single operation, so the gas the paymaster pulls in postOp can be paid
    # out of USDC the same operation just produced.
    calls: tuple[UserOperationCall, ...] = Field(min_length=1)
    gas_token: Literal["USDC"] = "USDC"
    gas_token_address: str
    account_type: Literal["smart-account", "safe"] = "smart-account"

    @property
    def call_data(self) -> UserOperationCall:
        """The first call — the whole operation when it is not a batch."""
        return self.calls[0]


class PaymasterUserOperationSubmission(FrozenModel):
    user_op_hash: str
    transaction_hash: str | None = None
    status: Literal["submitted", "included"] = "submitted"
    block_number: int = Field(default=0, ge=0)
    gas_used: int = Field(default=0, ge=0)
    message: str | None = None


@runtime_checkable
class PaymasterUserOperationAdapter(Protocol):
    def address(self) -> str: ...

    def submit_user_operation(
        self,
        request: PaymasterUserOperationRequest,
    ) -> PaymasterUserOperationSubmission: ...


class Erc4337PaymasterSigner:
    def __init__(
        self,
        config: object | None = None,
        *,
        adapter: PaymasterUserOperationAdapter | None = None,
        account: object | None = None,
        account_type: Literal["smart-account", "safe"] | None = None,
        entry_point: str | None = None,
        usdc_address: str | None = None,
    ) -> None:
        self._adapter = adapter or (
            _adapter_from_config(config) if config is not None else None
        )
        self._config = config
        self._account = account
        self._account_type = account_type or _account_type_from_config(config)
        self._entry_point = entry_point or _optional_config_value(
            config,
            "paymaster_entry_point",
        )
        # Resolved per transaction, not here: the gas token depends on the chain.
        # An explicit argument still pins one token for every chain.
        self._usdc_address = usdc_address

    def __repr__(self) -> str:
        if self._adapter is None:
            return "Erc4337PaymasterSigner(status=<unconfigured>)"
        return "Erc4337PaymasterSigner(status=configured)"

    def address(self) -> str:
        account_address = _account_address(self._account)
        if account_address is not None:
            return account_address
        return self._require_adapter().address()

    def send(self, tx: TxStep, rpc_url: str) -> Receipt:
        return self.send_batch((tx,), rpc_url)

    def send_batch(self, steps: Sequence[TxStep], rpc_url: str) -> Receipt:
        """Every step in one user operation, atomically.

        This is what makes a gasless exit self-funding: the paymaster charges in
        postOp, after execution, so a batch that redeems into the account can pay
        its own gas out of the proceeds. Sent one at a time, the first step is an
        approval that produces nothing and the charge has to come from a balance
        the account does not have.
        """
        _ = rpc_url
        if not steps:
            raise PaymasterConfigurationError("a user operation needs a step")
        chain_ids = {step.chain_id for step in steps}
        if len(chain_ids) != 1:
            raise PaymasterConfigurationError(
                f"one user operation cannot span chains {sorted(chain_ids)}"
            )
        tx = steps[0]
        request = PaymasterUserOperationRequest(
            sender=self.address(),
            chain_id=tx.chain_id,
            entry_point=self._required_entry_point(),
            calls=tuple(
                UserOperationCall(to=step.to, data=step.data, value=step.value)
                for step in steps
            ),
            gas_token_address=self._required_usdc_address(tx.chain_id),
            account_type=self._account_type,
        )
        submission = self._require_adapter().submit_user_operation(request)
        return Receipt(
            transaction_hash=submission.transaction_hash or submission.user_op_hash,
            block_number=submission.block_number,
            gas_used=submission.gas_used,
            status=1 if submission.status == "included" else 0,
            from_address=request.sender,
            # The last step is the action the batch exists for; the ones before
            # it are approvals clearing its path.
            to_address=steps[-1].to,
            pending=submission.status == "submitted",
            execution_status="user_operation_submitted",
            safe_tx_hash=(
                submission.user_op_hash if request.account_type == "safe" else None
            ),
            message=submission.message
            or "ERC-4337 user operation submitted via USDC paymaster",
        )

    def _require_adapter(self) -> PaymasterUserOperationAdapter:
        if self._adapter is None:
            raise PaymasterConfigurationError(
                "Erc4337PaymasterSigner requires paymaster adapter configuration"
            )
        return self._adapter

    def _required_entry_point(self) -> str:
        if self._entry_point is None:
            # The EntryPoint is a protocol singleton at the same address on every
            # chain, and the registry knows it — so this is derivable, not a
            # question for the user. generic-http still requires it in config,
            # where a self-hosted bundler may be pinned to a different one.
            return paymaster_registry.ENTRY_POINTS[
                paymaster_registry.DEFAULT_ENTRY_POINT_VERSION
            ]
        return self._entry_point

    def _required_usdc_address(self, chain_id: int) -> str:
        if self._usdc_address is not None:
            return self._usdc_address
        return require_usdc_address(self._config, chain_id)


class GenericHttpPaymasterUserOperationAdapter:
    def __init__(
        self,
        *,
        account_address: str,
        bundler_url: str,
        paymaster_url: str,
        bundler_credential: object | None = None,
        paymaster_credential: object | None = None,
        supported_chain_ids: Sequence[int] | None = None,
        client: httpx.Client | None = None,
        timeout: float = 30,
    ) -> None:
        self._account_address = account_address
        self._bundler_url = bundler_url.rstrip("/")
        self._paymaster_url = paymaster_url.rstrip("/")
        self._bundler_credential = bundler_credential
        self._paymaster_credential = paymaster_credential
        self._supported_chain_ids = (
            None if supported_chain_ids is None else frozenset(supported_chain_ids)
        )
        self._client = client or httpx.Client()
        self._timeout = timeout

    def __repr__(self) -> str:
        return "GenericHttpPaymasterUserOperationAdapter(credential=<redacted>)"

    def address(self) -> str:
        return self._account_address

    def submit_user_operation(
        self,
        request: PaymasterUserOperationRequest,
    ) -> PaymasterUserOperationSubmission:
        if (
            self._supported_chain_ids is not None
            and request.chain_id not in self._supported_chain_ids
        ):
            raise PaymasterUnsupportedChain(request.chain_id)

        sponsored_payload = self._post(
            self._paymaster_url,
            "sponsor-user-operation",
            request.model_dump(mode="json"),
            credential=self._paymaster_credential,
        )
        user_operation = _payload_mapping(
            sponsored_payload,
            "userOperation",
            default=request.model_dump(mode="json"),
        )
        send_payload = self._post(
            self._bundler_url,
            "send-user-operation",
            {
                "chainId": request.chain_id,
                "entryPoint": request.entry_point,
                "userOperation": dict(user_operation),
            },
            credential=self._bundler_credential,
        )
        user_op_hash = _first_payload_string(
            send_payload,
            ("userOpHash", "user_op_hash", "hash"),
        )
        if user_op_hash is None:
            raise PaymasterError("bundler response missing userOpHash")
        return PaymasterUserOperationSubmission(
            user_op_hash=user_op_hash,
            transaction_hash=_first_payload_string(
                send_payload,
                ("transactionHash", "transaction_hash", "txHash"),
            ),
            status=_submission_status(send_payload.get("status")),
            block_number=_optional_int(send_payload.get("blockNumber"), default=0),
            gas_used=_optional_int(send_payload.get("gasUsed"), default=0),
            message=_first_payload_string(send_payload, ("message",)),
        )

    def _post(
        self,
        base_url: str,
        endpoint: str,
        body: Mapping[str, object],
        *,
        credential: object | None,
    ) -> Mapping[str, object]:
        try:
            response = self._client.post(
                f"{base_url}/{endpoint}",
                headers=_auth_headers(credential),
                json=body,
                timeout=self._timeout,
            )
        except httpx.HTTPError as error:
            raise PaymasterError("paymaster/bundler request failed") from error

        payload = _json_payload(response)
        if _is_unsupported_chain_response(response, payload):
            chain_id = _first_payload_int(payload, ("chainId", "chain_id"))
            raise PaymasterUnsupportedChain(
                chain_id if chain_id is not None else int(body.get("chainId", 0))
            )
        if _is_rejection_response(response, payload):
            raise PaymasterRejected(_rejection_message(payload))
        if response.is_error:
            raise PaymasterError(
                f"paymaster/bundler request failed with HTTP {response.status_code}"
            )
        return payload


def usdc_address_for_chain(config: object | None, chain_id: int) -> str | None:
    """The gas token for one chain: registry first, legacy setting as fallback.

    `PAYMASTER_USDC_ADDRESS` assumed a single-chain deployment, so it must not
    win over a known row — on a multi-chain run it names a token that is
    something else at that address elsewhere. `PAYMASTER_USDC_ADDRESS_<chain id>`
    wins over both.
    """
    address = chains.usdc_address(chain_id, config)
    if address is not None:
        return address
    return _optional_config_value(config, "paymaster_usdc_address")


def require_usdc_address(config: object | None, chain_id: int) -> str:
    address = usdc_address_for_chain(config, chain_id)
    if address is None:
        raise PaymasterConfigurationError(
            f"no USDC address is known for chain {chain_id} "
            f"({chains.chain_name(chain_id)}), so gas cannot be paid there; set "
            f"PAYMASTER_USDC_ADDRESS_{chain_id} to the USDC the paymaster accepts"
        )
    return address


def submits_via_paymaster(config: object | None) -> bool:
    if config is None:
        return False
    return composition_from_config(config).submission == "erc4337-paymaster"


def validate_paymaster_preflight(
    config: object | None,
    chain_ids: Sequence[int],
) -> dict[int, str]:
    if config is None:
        raise PaymasterConfigurationError(
            "paymaster configuration is required for ERC-4337 paymaster mode"
        )

    # Pimlico configures none of these: its endpoint embeds the chain id and is
    # derived from the API key, the Safe from the seed, the EntryPoint from the
    # registry. Demanding them here made PAYMASTER_PROVIDER=pimlico unreachable
    # from execute, even though config validation had stopped requiring them.
    bundler_urls = _preflight_endpoints(config, chain_ids)

    # An explicit allowlist still wins, for pinning a deployment to known chains.
    supported_chain_ids = _supported_chain_ids(config)
    provider = paymaster_provider_from_config(config)

    for chain_id in chain_ids:
        if supported_chain_ids is not None:
            if chain_id not in supported_chain_ids:
                raise PaymasterUnsupportedChain(chain_id)
        elif not paymaster_registry.is_gas_payable(chain_id, provider=provider):
            raise PaymasterUnsupportedChain(chain_id)
        # Here rather than at submission: a plan that cannot name the gas token
        # on its third chain must fail before the first two are broadcast.
        require_usdc_address(config, chain_id)

    return bundler_urls


def _preflight_endpoints(
    config: object,
    chain_ids: Sequence[int],
) -> dict[int, str]:
    if getattr(config, "paymaster_provider", None) == "pimlico":
        _required_config_value(config, "pimlico_api_key")
        # Key-free: the real URL carries the API key in its query string, and
        # this value travels into execution where it could be logged.
        return {
            chain_id: paymaster_registry.PIMLICO_RPC_TEMPLATE.format(chain_id=chain_id)
            for chain_id in chain_ids
        }

    bundler_url = _required_config_value(config, "paymaster_bundler_url")
    _required_config_value(config, "paymaster_url")
    _required_config_value(config, "paymaster_account_address")
    _required_config_value(config, "paymaster_entry_point")
    return {chain_id: bundler_url for chain_id in chain_ids}


def paymaster_provider_from_config(
    config: object | None,
) -> paymaster_registry.PaymasterProviderName:
    value = getattr(config, "paymaster_provider", None)
    if value in ("pimlico", "circle"):
        return value
    # generic-http and unset both mean "no opinion" — take the default, which
    # is the provider with the widest chain coverage.
    return paymaster_registry.DEFAULT_PROVIDER


TokenQuoter = Callable[[int], "object | None"]


def paymaster_cost_notes(
    config: object | None,
    chain_ids: Sequence[int],
    *,
    quoter: TokenQuoter | None = None,
) -> tuple[dict[str, object], ...]:
    """Per-chain gas facts for preflight: provider, entrypoint, live rate.

    The rate is fetched rather than tabulated because Pimlico bakes its fee into
    the exchangeRate — there is no static number that is both stable and true.
    A quote that cannot be fetched degrades to "quoted at submission" rather
    than failing: not knowing the rate yet is not a reason to block a plan.
    """
    provider = paymaster_provider_from_config(config)
    if quoter is None:
        quoter = _live_token_quoter(config, provider)
    notes: list[dict[str, object]] = []
    for chain_id in chain_ids:
        row = paymaster_registry.paymaster_chain(chain_id, provider=provider)
        if row is None:
            notes.append(
                {
                    "chain_id": chain_id,
                    "gas_payable_in_usdc": False,
                    "provider": None,
                }
            )
            continue
        note: dict[str, object] = {
            "chain_id": chain_id,
            "gas_payable_in_usdc": True,
            "provider": row.provider,
            "entry_point_version": row.entry_point_version,
        }
        gas_token = usdc_address_for_chain(config, chain_id)
        if gas_token is not None:
            note["gas_token_address"] = gas_token
        quote = quoter(chain_id) if quoter is not None else None
        if quote is not None:
            note["exchange_rate"] = getattr(quote, "exchange_rate", None)
            note["paymaster"] = getattr(quote, "paymaster", None)
        notes.append(note)
    return tuple(notes)


def _live_token_quoter(
    config: object | None,
    provider: paymaster_registry.PaymasterProviderName,
) -> TokenQuoter | None:
    """A quoter backed by the real Pimlico endpoint, if we can build one."""
    if config is None or provider != "pimlico":
        return None
    api_key = _optional_secret_config_value(config, "pimlico_api_key")
    if api_key is None:
        return None

    from open_allocator.exec.pimlico import (
        PimlicoClient,
        PimlicoError,
        PimlicoPaymasterAdapter,
    )

    def quote(chain_id: int) -> object | None:
        # Per chain: quoting one chain's USDC against another's paymaster prices
        # a token that chain will not be charged in.
        usdc = usdc_address_for_chain(config, chain_id)
        if usdc is None:
            return None
        try:
            adapter = PimlicoPaymasterAdapter(
                PimlicoClient(chain_id=chain_id, api_key=api_key)
            )
            return adapter.token_quote(usdc)
        except PimlicoError:
            # Preflight is advisory; a missing quote must not block the plan.
            return None

    return quote


def _adapter_from_config(config: object) -> PaymasterUserOperationAdapter:
    provider = getattr(config, "paymaster_provider", None)
    if provider == "pimlico":
        # Imported here rather than at module scope: pimlico_adapter imports the
        # request/submission types from this module, so a top-level import would
        # be a cycle.
        from open_allocator.exec.pimlico_adapter import pimlico_adapter_from_config

        return pimlico_adapter_from_config(config)
    if provider == "circle":
        raise PaymasterConfigurationError(
            "PAYMASTER_PROVIDER=circle is a registry row, not an adapter: no "
            "Circle client exists yet. Use pimlico (which also covers more "
            "chains, including Monad) or generic-http."
        )
    if provider == "generic-http":
        return GenericHttpPaymasterUserOperationAdapter(
            account_address=_required_config_value(config, "paymaster_account_address"),
            bundler_url=_required_config_value(config, "paymaster_bundler_url"),
            paymaster_url=_required_config_value(config, "paymaster_url"),
            bundler_credential=_optional_secret_config_value(
                config,
                "paymaster_bundler_credential",
            ),
            paymaster_credential=_optional_secret_config_value(
                config,
                "paymaster_credential",
            ),
            supported_chain_ids=_supported_chain_ids(config),
        )
    raise PaymasterConfigurationError(f"unknown paymaster provider: {provider!r}")


def _account_address(account: object | None) -> str | None:
    if account is None:
        return None
    address = getattr(account, "address", None)
    if callable(address):
        return str(address())
    return None


def _account_type_from_config(
    config: object | None,
) -> Literal["smart-account", "safe"]:
    value = _optional_config_value(config, "paymaster_account_type")
    if value == "safe":
        return "safe"
    return "smart-account"


def _supported_chain_ids(config: object) -> frozenset[int] | None:
    value = getattr(config, "paymaster_supported_chain_ids", None)
    if value is None:
        return None
    return frozenset(int(chain_id) for chain_id in value)


def _required_config_value(config: object, name: str) -> str:
    value = getattr(config, name, None)
    if value is None:
        raise PaymasterConfigurationError(
            f"{name.upper()} is required for ERC-4337 paymaster mode"
        )
    return _secret_value(value)


def _optional_config_value(config: object | None, name: str) -> str | None:
    if config is None:
        return None
    value = getattr(config, name, None)
    if value is None:
        return None
    return _secret_value(value)


def _optional_secret_config_value(config: object, name: str) -> str | None:
    value = getattr(config, name, None)
    if value is None:
        return None
    return _secret_value(value)


def _secret_value(value: object) -> str:
    get_secret_value = getattr(value, "get_secret_value", None)
    if callable(get_secret_value):
        return str(get_secret_value())
    return str(value)


def _auth_headers(credential: object | None) -> dict[str, str]:
    if credential is None:
        return {}
    return {"Authorization": f"Bearer {_secret_value(credential)}"}


def _json_payload(response: httpx.Response) -> Mapping[str, object]:
    try:
        payload = response.json()
    except ValueError as error:
        raise PaymasterError("paymaster/bundler returned invalid JSON") from error
    if not isinstance(payload, Mapping):
        raise PaymasterError("paymaster/bundler returned non-object JSON")
    return payload


def _payload_mapping(
    payload: Mapping[str, object],
    key: str,
    *,
    default: Mapping[str, object],
) -> Mapping[str, object]:
    value = payload.get(key)
    if value is None:
        return default
    if not isinstance(value, Mapping):
        raise PaymasterError(f"paymaster response {key} must be an object")
    return value


def _is_unsupported_chain_response(
    response: httpx.Response,
    payload: Mapping[str, object],
) -> bool:
    if response.status_code == 404:
        return True
    for value in _walk_values(payload):
        if isinstance(value, str):
            normalized = value.casefold().replace("-", "_")
            if "unsupported_chain" in normalized:
                return True
    return False


def _is_rejection_response(
    response: httpx.Response,
    payload: Mapping[str, object],
) -> bool:
    if response.status_code in {400, 402, 403, 409}:
        return True
    for value in _walk_values(payload):
        if isinstance(value, str):
            normalized = value.casefold().replace("-", "_")
            if "reject" in normalized or "denied" in normalized:
                return True
    return False


def _rejection_message(payload: Mapping[str, object]) -> str:
    message = _first_payload_string(payload, ("message", "reason", "error"))
    if message is None:
        return "USDC paymaster rejected the user operation"
    return message


def _first_payload_string(
    payload: Mapping[str, object],
    keys: Sequence[str],
) -> str | None:
    for key, value in _walk_items(payload):
        if key in keys and isinstance(value, str) and value != "":
            return value
    return None


def _first_payload_int(
    payload: Mapping[str, object],
    keys: Sequence[str],
) -> int | None:
    for key, value in _walk_items(payload):
        if key in keys:
            return int(value)
    return None


def _optional_int(value: object, *, default: int) -> int:
    if value is None:
        return default
    return int(value)


def _submission_status(value: object) -> Literal["submitted", "included"]:
    if value == "included":
        return "included"
    return "submitted"


def _walk_items(value: object) -> Sequence[tuple[str, object]]:
    items: list[tuple[str, object]] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            items.append((str(key), item))
            items.extend(_walk_items(item))
    elif isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        for item in value:
            items.extend(_walk_items(item))
    return tuple(items)


def _walk_values(value: object) -> Sequence[object]:
    values: list[object] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            values.append(key)
            values.extend(_walk_values(item))
    elif isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        for item in value:
            values.extend(_walk_values(item))
    else:
        values.append(value)
    return tuple(values)


__all__ = [
    "Erc4337PaymasterSigner",
    "GenericHttpPaymasterUserOperationAdapter",
    "PaymasterConfigurationError",
    "PaymasterError",
    "PaymasterRejected",
    "PaymasterUnsupportedChain",
    "PaymasterUserOperationAdapter",
    "PaymasterUserOperationRequest",
    "PaymasterUserOperationSubmission",
    "UserOperationCall",
    "require_usdc_address",
    "submits_via_paymaster",
    "usdc_address_for_chain",
    "validate_paymaster_preflight",
]
