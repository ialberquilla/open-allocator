"""Validation of 1Tx calldata bundles before they may enter a transaction plan.

The client parses responses strictly, which fixes their shape. This module binds
a parsed bundle to the request and execution context it is about to be used in:
the instrument, Safe, action, chain, amount, and remaining quote lifetime. A
bundle that fails any check must never reach a signer.
"""

from __future__ import annotations

import time

from open_allocator.exec.client import (
    BridgeCalldataResponse,
    CalldataAction,
    InstrumentCalldataResponse,
    OneTxDecodeError,
)


class CalldataValidationError(OneTxDecodeError):
    pass


class CalldataExpiredError(CalldataValidationError):
    pass


def validate_instrument_calldata(
    response: InstrumentCalldataResponse,
    *,
    instrument_id: str,
    account: str,
    action: CalldataAction,
    chain_id: int,
    amount: str,
    min_ttl_seconds: int,
    now: float | None = None,
) -> InstrumentCalldataResponse:
    _require_equal("instrumentId", response.instrument_id, instrument_id, fold=True)
    _require_equal("account", response.account, account, fold=True)
    _require_equal("action", response.action, action)
    _require_equal("chainId", response.chain_id, chain_id)
    _require_equal("amountIn", response.amount_in, amount)
    ensure_calldata_lifetime(response, min_ttl_seconds=min_ttl_seconds, now=now)
    return response


def ensure_calldata_lifetime(
    response: InstrumentCalldataResponse,
    *,
    min_ttl_seconds: int,
    now: float | None = None,
) -> None:
    """Require the bundle to outlive the configured minimum.

    Checked when a bundle is planned and again immediately before one-shot
    signing; a bundle below the threshold must be rebuilt, not signed.
    """
    if response.expires_at is None:
        return
    current = time.time() if now is None else now
    remaining = response.expires_at - current
    if remaining <= 0:
        raise CalldataExpiredError(
            f"calldata for {response.instrument_id} expired at {response.expires_at}"
        )
    if remaining < min_ttl_seconds:
        raise CalldataExpiredError(
            f"calldata for {response.instrument_id} expires in {remaining:.0f}s, "
            f"below the {min_ttl_seconds}s minimum; rebuild it"
        )


def validate_bridge_calldata(
    response: BridgeCalldataResponse,
    *,
    from_chain_id: int,
    to_chain_id: int,
    account: str,
    amount: str,
    fast: bool,
) -> BridgeCalldataResponse:
    _require_equal("fromChainId", response.from_chain_id, from_chain_id)
    _require_equal("toChainId", response.to_chain_id, to_chain_id)
    _require_equal("account", response.account, account, fold=True)
    _require_equal("amount", response.amount, amount)
    _require_equal("fast", response.fast, fast)
    return response


def _require_equal(
    field: str,
    actual: object,
    expected: object,
    *,
    fold: bool = False,
) -> None:
    if fold and isinstance(actual, str) and isinstance(expected, str):
        matches = actual.casefold() == expected.casefold()
    else:
        matches = actual == expected
    if not matches:
        raise CalldataValidationError(
            f"calldata {field} {actual!r} does not match expected {expected!r}"
        )
