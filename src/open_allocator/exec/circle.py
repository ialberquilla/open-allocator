"""Circle's CCTP V2 attestation service, as a bridged leg's readiness source.

A burn is only redeemable once Circle attests its message. This client asks, a
bounded number of HTTP attempts per call, and never waits for the attestation
itself: an unready message is an ordinary answer, and the caller reports the
leg in progress for a rerun to pick up.

Responses are typed but tolerant of new fields. Nothing here is trusted: the
raw message is decoded and checked against the burn before it is used
(``exec.cctp``), so Circle's ``decodedMessage`` is not read at all.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

DEFAULT_IRIS_API_URL = "https://iris-api.circle.com"


class CircleError(RuntimeError):
    pass


class CircleHTTPError(CircleError):
    def __init__(self, method: str, path: str, status_code: int, text: str) -> None:
        self.status_code = status_code
        super().__init__(f"Circle {method} {path} failed ({status_code}): {text[:300]}")


class CircleModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)


class CircleMessage(CircleModel):
    # "0x" until the message is attested.
    message: str = "0x"
    # "PENDING" (or absent) until the message is attested.
    attestation: str | None = None
    event_nonce: str | None = Field(default=None, alias="eventNonce")
    cctp_version: int | None = Field(default=None, alias="cctpVersion")
    status: str
    delay_reason: str | None = Field(default=None, alias="delayReason")

    @property
    def complete(self) -> bool:
        return (
            self.status == "complete"
            and _is_hex(self.message)
            and self.attestation is not None
            and _is_hex(self.attestation)
        )


class CircleMessages(CircleModel):
    messages: tuple[CircleMessage, ...] = ()


class CircleClient:
    def __init__(
        self,
        base_url: str = DEFAULT_IRIS_API_URL,
        *,
        timeout: float = 10.0,
        max_retries: int = 2,
        backoff_factor: float = 0.5,
        backoff_cap: float = 5.0,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._max_retries = max_retries
        self._backoff_factor = backoff_factor
        self._backoff_cap = backoff_cap
        self._sleep = sleep
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Accept": "application/json"},
            timeout=timeout,
            transport=transport,
        )

    @classmethod
    def from_config(cls, config: object | None) -> CircleClient:
        return cls(
            str(getattr(config, "circle_iris_api_url", None) or DEFAULT_IRIS_API_URL),
            timeout=float(getattr(config, "circle_http_timeout_seconds", 10.0)),
            max_retries=int(getattr(config, "circle_http_max_retries", 2)),
        )

    def close(self) -> None:
        self._http.close()

    def messages(self, source_domain: int, transaction_hash: str) -> CircleMessages:
        """Every CCTP message Circle has seen in a source transaction.

        A 404 means Circle has not indexed the transaction yet, which is the
        same answer as an empty list: not ready.
        """
        path = f"/v2/messages/{int(source_domain)}"
        response = self._request(
            "GET",
            path,
            params={"transactionHash": transaction_hash},
            not_found_ok=True,
        )
        if response is None:
            return CircleMessages()
        try:
            return CircleMessages.model_validate(response)
        except ValidationError as error:
            raise CircleError(
                f"Circle GET {path} returned an unreadable response: {error}"
            ) from error

    def reattest(self, nonce: str) -> None:
        """Ask Circle to re-attest an expired message; poll ``messages`` after."""
        self._request("POST", f"/v2/reattest/{nonce}")

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        not_found_ok: bool = False,
    ) -> object | None:
        for attempt in range(self._max_retries + 1):
            try:
                response = self._http.request(method, path, params=params)
            except httpx.HTTPError as error:
                if attempt < self._max_retries:
                    self._sleep(self._backoff(attempt))
                    continue
                raise CircleError(
                    f"Circle {method} {path} failed: {type(error).__name__}"
                ) from error
            retryable = response.status_code == 429 or response.status_code >= 500
            if retryable and attempt < self._max_retries:
                self._sleep(self._backoff(attempt))
                continue
            if response.status_code == 404 and not_found_ok:
                return None
            if response.is_error:
                raise CircleHTTPError(method, path, response.status_code, response.text)
            if not response.content:
                return None
            try:
                return response.json()
            except ValueError as error:
                raise CircleError(
                    f"Circle {method} {path} returned non-JSON"
                ) from error
        raise AssertionError("unreachable retry loop exit")

    def _backoff(self, attempt: int) -> float:
        return min(self._backoff_factor * (2**attempt), self._backoff_cap)


def _is_hex(value: str) -> bool:
    if not value.startswith("0x") or len(value) <= 2 or len(value) % 2:
        return False
    try:
        bytes.fromhex(value[2:])
    except ValueError:
        return False
    return True


__all__ = [
    "DEFAULT_IRIS_API_URL",
    "CircleClient",
    "CircleError",
    "CircleHTTPError",
    "CircleMessage",
    "CircleMessages",
]
