from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from open_allocator.exec import paymaster_registry

# Pimlico's concrete adapter. One per-chain endpoint serves both the bundler
# (eth_*) and the paymaster (pm_*/pimlico_*) as plain JSON-RPC, so this is one
# client rather than the two base URLs the generic HTTP adapter assumes.
#
# Method shapes checked against Pimlico's docs on 2026-07-15.

JSON_RPC_ID = 4337

# pimlico_getUserOperationGasPrice quotes three tiers. The EntryPoint charges
# min(maxFeePerGas, baseFee + priority) and the ERC-20 paymaster converts that
# into the gas token, so the tier is a multiplier on the bill — but a modest
# one: measured 2026-08-16, fast/standard was 1.048x on both Base and Monad,
# and slow/standard 0.95x. `standard` is the right default for a scheduled
# rebalancer that is not competing for inclusion; `fast` is there for when
# operations sit unincluded.
FeeTier = Literal["slow", "standard", "fast"]
FEE_TIERS: tuple[FeeTier, ...] = ("slow", "standard", "fast")
DEFAULT_FEE_TIER: FeeTier = "standard"


class PimlicoError(RuntimeError):
    pass


class PimlicoRpcError(PimlicoError):
    """The endpoint answered, and the answer was an error."""

    def __init__(self, method: str, code: int, message: str) -> None:
        self.method = method
        self.code = code
        super().__init__(f"{method} failed ({code}): {message}")


@dataclass(frozen=True)
class TokenQuote:
    """A live gas quote, denominated in an ERC-20.

    Pimlico bakes its fee into `exchange_rate`, so this — not a static per-chain
    table — is the only honest source for what gas will cost in USDC.

    🔑 **This carries the quote's fields and deliberately does no arithmetic on
    them.** It used to offer a ``token_cost(gas_limit, max_fee_per_gas)`` helper
    that scaled ``exchange_rate`` by 1e18. Nothing ever called it, so the
    scaling was never checked against a real charge — and a plausible-looking
    cost function that no receipt has ever agreed with is worse than none at
    all, because the first caller has no reason to doubt it. Anything added
    back here should be derived against a real charge first.
    """

    paymaster: str
    token: str
    post_op_gas: int
    exchange_rate: int
    exchange_rate_native_to_usd: int | None = None


class PimlicoClient:
    """Thin JSON-RPC client for one chain's Pimlico endpoint."""

    def __init__(
        self,
        *,
        chain_id: int,
        api_key: str,
        client: httpx.Client | None = None,
        timeout: float = 30,
        url: str | None = None,
    ) -> None:
        self._chain_id = chain_id
        # The key lives in the URL, so this string is a secret: never log it,
        # never put it in an exception. Errors carry the method, not the URL.
        self._url = url or paymaster_registry.pimlico_rpc_url(chain_id, api_key)
        self._client = client or httpx.Client()
        self._timeout = timeout

    def __repr__(self) -> str:
        return f"PimlicoClient(chain_id={self._chain_id}, url=<redacted>)"

    @property
    def chain_id(self) -> int:
        return self._chain_id

    def call(self, method: str, params: Sequence[Any]) -> Any:
        try:
            response = self._client.post(
                self._url,
                json={
                    "jsonrpc": "2.0",
                    "id": JSON_RPC_ID,
                    "method": method,
                    "params": list(params),
                },
                timeout=self._timeout,
            )
        except httpx.HTTPError as error:
            raise PimlicoError(f"{method} request failed") from error

        try:
            payload = response.json()
        except ValueError as error:
            raise PimlicoError(f"{method} returned invalid JSON") from error
        if not isinstance(payload, Mapping):
            raise PimlicoError(f"{method} returned non-object JSON")

        error_payload = payload.get("error")
        if isinstance(error_payload, Mapping):
            raise PimlicoRpcError(
                method,
                _as_int(error_payload.get("code"), default=0),
                str(error_payload.get("message", "unknown error")),
            )
        if response.is_error:
            raise PimlicoError(f"{method} failed with HTTP {response.status_code}")
        if "result" not in payload:
            raise PimlicoError(f"{method} response has no result")
        return payload["result"]


class PimlicoPaymasterAdapter:
    """Bundler + ERC-20 paymaster for one chain."""

    def __init__(
        self,
        client: PimlicoClient,
        *,
        entry_point: str | None = None,
        entry_point_version: str | None = None,
    ) -> None:
        self._client = client
        row = paymaster_registry.paymaster_chain(client.chain_id)
        version = entry_point_version or (
            row.entry_point_version
            if row
            else paymaster_registry.DEFAULT_ENTRY_POINT_VERSION
        )
        self._entry_point_version = version
        self._entry_point = entry_point or paymaster_registry.ENTRY_POINTS[version]

    def __repr__(self) -> str:
        return (
            f"PimlicoPaymasterAdapter(chain_id={self._client.chain_id}, "
            f"entry_point_version={self._entry_point_version})"
        )

    @property
    def entry_point(self) -> str:
        return self._entry_point

    @property
    def chain_id(self) -> int:
        return self._client.chain_id

    def token_quote(self, token: str) -> TokenQuote:
        """pimlico_getTokenQuotes — the live, fee-inclusive rate for `token`."""
        result = self._client.call(
            "pimlico_getTokenQuotes",
            [
                {"tokens": [token]},
                self._entry_point,
                _hex(self._client.chain_id),
            ],
        )
        quotes = _field(result, "quotes")
        if not isinstance(quotes, Sequence) or not quotes:
            raise PimlicoError(f"no token quote returned for {token}")
        quote = quotes[0]
        if not isinstance(quote, Mapping):
            raise PimlicoError("malformed token quote")
        return TokenQuote(
            paymaster=str(_field(quote, "paymaster")),
            token=str(quote.get("token", token)),
            post_op_gas=_as_int(_field(quote, "postOpGas")),
            exchange_rate=_as_int(_field(quote, "exchangeRate")),
            exchange_rate_native_to_usd=_optional_int(
                quote.get("exchangeRateNativeToUsd")
            ),
        )

    def estimate_gas(
        self,
        user_operation: Mapping[str, Any],
    ) -> dict[str, int]:
        """eth_estimateUserOperationGas."""
        result = self._client.call(
            "eth_estimateUserOperationGas",
            [dict(user_operation), self._entry_point],
        )
        if not isinstance(result, Mapping):
            raise PimlicoError("malformed gas estimate")
        return {
            key: _as_int(value)
            for key, value in result.items()
            if isinstance(value, str | int)
        }

    def sponsor(
        self,
        user_operation: Mapping[str, Any],
        *,
        token: str,
    ) -> dict[str, Any]:
        """pm_getPaymasterData — ERC-7677, with {token} for ERC-20 mode.

        Returns the userOp with the paymaster fields filled in.
        """
        result = self._client.call(
            "pm_getPaymasterData",
            [
                dict(user_operation),
                self._entry_point,
                _hex(self._client.chain_id),
                {"token": token},
            ],
        )
        if not isinstance(result, Mapping):
            raise PimlicoError("malformed paymaster data")
        sponsored = dict(user_operation)
        sponsored.update(result)
        return sponsored

    def stub_data(
        self,
        user_operation: Mapping[str, Any],
        *,
        token: str,
    ) -> dict[str, Any]:
        """pm_getPaymasterStubData — placeholder fields for gas estimation."""
        result = self._client.call(
            "pm_getPaymasterStubData",
            [
                dict(user_operation),
                self._entry_point,
                _hex(self._client.chain_id),
                {"token": token},
            ],
        )
        if not isinstance(result, Mapping):
            raise PimlicoError("malformed paymaster stub data")
        stubbed = dict(user_operation)
        stubbed.update(result)
        return stubbed

    def gas_price(self, tier: FeeTier = DEFAULT_FEE_TIER) -> dict[str, int]:
        """pimlico_getUserOperationGasPrice — the bundler's fee suggestion.

        ``tier`` picks how much to overbid; see FEE_TIERS. An endpoint that
        answers with the fee fields directly, rather than per tier, is used
        as-is — that shape has no tiers to choose between.
        """
        result = self._client.call("pimlico_getUserOperationGasPrice", [])
        if not isinstance(result, Mapping):
            raise PimlicoError("malformed gas price")
        quoted = result.get(tier, result)
        if not isinstance(quoted, Mapping):
            raise PimlicoError("malformed gas price")
        if "maxFeePerGas" not in quoted:
            # Naming what was on offer: silently falling through to another
            # tier would quietly charge a price nobody asked for.
            offered = ", ".join(sorted(str(key) for key in result)) or "nothing"
            raise PimlicoError(
                f"gas price tier {tier!r} was not quoted (offered: {offered})"
            )
        return {
            "maxFeePerGas": _as_int(_field(quoted, "maxFeePerGas")),
            "maxPriorityFeePerGas": _as_int(_field(quoted, "maxPriorityFeePerGas")),
        }

    def send(self, user_operation: Mapping[str, Any]) -> str:
        """eth_sendUserOperation — returns the userOp hash."""
        result = self._client.call(
            "eth_sendUserOperation",
            [dict(user_operation), self._entry_point],
        )
        return str(result)

    def receipt(self, user_op_hash: str) -> dict[str, Any] | None:
        """eth_getUserOperationReceipt — None while still pending."""
        result = self._client.call("eth_getUserOperationReceipt", [user_op_hash])
        if result is None:
            return None
        if not isinstance(result, Mapping):
            raise PimlicoError("malformed user operation receipt")
        return dict(result)


def _field(payload: Mapping[str, Any], key: str) -> Any:
    if key not in payload:
        raise PimlicoError(f"response missing {key}")
    return payload[key]


def _hex(value: int) -> str:
    return hex(value)


def _as_int(value: Any, *, default: int | None = None) -> int:
    if value is None:
        if default is None:
            raise PimlicoError("expected a number, got null")
        return default
    if isinstance(value, bool):
        raise PimlicoError("expected a number, got a bool")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value, 16) if value.startswith("0x") else int(value)
        except ValueError as error:
            raise PimlicoError(f"expected a number, got {value!r}") from error
    raise PimlicoError(f"expected a number, got {type(value).__name__}")


def _optional_int(value: Any) -> int | None:
    return None if value is None else _as_int(value)


__all__ = [
    "DEFAULT_FEE_TIER",
    "FEE_TIERS",
    "FeeTier",
    "PimlicoClient",
    "PimlicoError",
    "PimlicoPaymasterAdapter",
    "PimlicoRpcError",
    "TokenQuote",
]
