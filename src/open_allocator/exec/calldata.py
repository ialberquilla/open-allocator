"""Validation of 1Tx calldata bundles before they may enter a transaction plan.

The client parses responses strictly, which fixes their shape. This module binds
a parsed bundle to the request and execution context it is about to be used in:
the instrument, Safe, action, chain, amount, and remaining quote lifetime. A
bundle that fails any check must never reach a signer.
"""

from __future__ import annotations

import time
from collections.abc import Iterable

from open_allocator.core import amounts
from open_allocator.core.types import FrozenModel, Vault
from open_allocator.exec import chains
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


class CalldataAmountError(ValueError):
    """A calldata request amount could not be derived exactly; do not guess."""


class DepositToken(FrozenModel):
    chain_id: int
    address: str
    decimals: int


def deposit_token(
    chain_id: int,
    vaults: Iterable[Vault],
    config: object | None = None,
) -> DepositToken:
    """The token a deposit request's raw ``amount`` is denominated in.

    Requests omit ``tokenIn``, so 1Tx spends the chain's USDC whatever the
    vault's underlying is. The address comes from the chain registry the
    paymaster and balances already use; its decimals come from discovery — an
    instrument on that chain whose underlying is that token — and are never
    assumed, because USDC is not 6 decimals on every chain.
    """
    address = chains.usdc_address(chain_id, config)
    if address is None:
        raise CalldataAmountError(f"no USDC address is known for chain {chain_id}")
    decimals = {
        vault.token_decimals
        for vault in vaults
        if vault.chain_id == chain_id
        and vault.token_address is not None
        and vault.token_address.casefold() == address.casefold()
        and vault.token_decimals is not None
    }
    if not decimals:
        raise CalldataAmountError(
            f"no discovered instrument on chain {chain_id} reports decimals for "
            f"USDC {address}"
        )
    if len(decimals) > 1:
        raise CalldataAmountError(
            f"discovered instruments disagree on decimals for USDC {address} on "
            f"chain {chain_id}: {sorted(decimals)}"
        )
    return DepositToken(chain_id=chain_id, address=address, decimals=decimals.pop())


def deposit_amount_raw(amount_usdc: object, token: DepositToken) -> str:
    """A human USDC amount as the raw ``amount`` of a deposit request.

    Rounds down, so the request never spends more than was selected.
    """
    raw = amounts.to_raw_units(amount_usdc, token.decimals, name="deposit amount")
    if raw <= 0:
        raise CalldataAmountError(
            f"deposit amount {amount_usdc} rounds down to zero raw units"
        )
    return str(raw)


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
