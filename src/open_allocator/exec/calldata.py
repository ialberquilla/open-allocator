"""Validation of 1Tx calldata bundles before they may enter a transaction plan.

The client parses responses strictly, which fixes their shape. This module binds
a parsed bundle to the request and execution context it is about to be used in:
the instrument, Safe, action, chain, amount, and remaining quote lifetime. A
bundle that fails any check must never reach a signer.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping

from open_allocator.core import amounts
from open_allocator.core.types import (
    BundleLeftover,
    BundleRequirement,
    BundleToken,
    FrozenModel,
    TxBundle,
    TxStep,
    Vault,
    bundle_digest,
)
from open_allocator.exec import chains
from open_allocator.exec.client import (
    BridgeCalldataResponse,
    CalldataAction,
    CalldataTokenInfo,
    InstrumentCalldataQuery,
    InstrumentCalldataResponse,
    OneTxDecodeError,
)

INSTRUMENT_CALLDATA_ENDPOINT = "GET /instruments/:instrumentId/calldata"
# Mirrors AllocatorConfig's default for callers that pass a partial config.
DEFAULT_MIN_CALLDATA_TTL_SECONDS = 20


class CalldataValidationError(OneTxDecodeError):
    pass


class CalldataExpiredError(CalldataValidationError):
    pass


class CalldataAmountError(ValueError):
    """A calldata request amount could not be derived exactly; do not guess."""


class CalldataUnsupportedError(ValueError):
    """A configured or requested feature the calldata API path cannot honor.

    Raised instead of ignoring the setting, so referral fees are never dropped
    and a cross-chain leg never becomes a same-chain one.
    """


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

    Requests omit ``tokenIn``, so 1Tx spends the chain's USDC. Decimals come
    from discovery, never assumed: USDC is not 6 decimals on every chain.
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
    response: InstrumentCalldataResponse | TxBundle,
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


def ensure_deposit_token(
    response: InstrumentCalldataResponse,
    token: DepositToken,
) -> None:
    """Require the bundle to spend the token its raw amount was computed in.

    A ``tokenIn`` with other decimals would make the raw amount a different sum
    of money than the one selected.
    """
    _require_equal(
        "tokenIn.address", response.token_in.address, token.address, fold=True
    )
    _require_equal("tokenIn.decimals", response.token_in.decimals, token.decimals)


def bundle_steps(response: InstrumentCalldataResponse) -> tuple[TxStep, ...]:
    """The bundle's calls as plan steps, in exactly the order returned.

    Each call keeps its backend type. Approvals, swaps, fees, and the protocol
    call are one atomic sequence; nothing here may reorder, drop, or relabel one.
    """
    return tuple(
        TxStep(
            to=call.to,
            data=call.data,
            value=int(call.value),
            chain_id=call.chain_id,
            kind=call.type,
        )
        for call in response.calls
    )


def plan_bundle(
    response: InstrumentCalldataResponse,
    *,
    leg_index: int,
    first_step_index: int,
) -> tuple[tuple[TxStep, ...], TxBundle]:
    """Plan steps for a validated bundle, plus the metadata that binds them.

    ``first_step_index`` is where the steps will sit in the plan. Callers must
    have run :func:`validate_instrument_calldata` on ``response`` first.
    """
    steps = bundle_steps(response)
    digest = bundle_digest(
        instrument_id=response.instrument_id,
        action=response.action,
        account=response.account,
        chain_id=response.chain_id,
        amount=response.amount_in,
        steps=steps,
        quote_block=response.quote_block,
        expires_at=response.expires_at,
    )
    bundle = TxBundle(
        bundle_id=bundle_id(leg_index, response.instrument_id, response.action),
        digest=digest,
        leg_index=leg_index,
        instrument_id=response.instrument_id,
        action=response.action,
        account=response.account,
        chain_id=response.chain_id,
        step_indexes=tuple(range(first_step_index, first_step_index + len(steps))),
        endpoint=INSTRUMENT_CALLDATA_ENDPOINT,
        amount=response.amount_in,
        token_in=_bundle_token(response.token_in),
        token_out=_bundle_token(response.token_out),
        quote_block=response.quote_block,
        expires_at=response.expires_at,
        requires=tuple(
            BundleRequirement(token=item.token, amount=item.amount)
            for item in response.requires
        ),
        leftovers=tuple(
            BundleLeftover(token=item.token, max_amount=item.max_amount)
            for item in response.leftovers
        ),
        expected_out=response.expected_out,
        min_out=response.min_out,
        protocol_gas=response.simulation.gas_used,
        simulated_out=response.simulation.token_out_delta,
        simulation_scope=response.simulation.scope,
        simulation_engine=response.simulation.engine,
    )
    return steps, bundle


def request_bundle(
    client: object,
    *,
    instrument_id: str,
    action: CalldataAction,
    account: str,
    chain_id: int,
    amount: str,
    leg_index: int,
    first_step_index: int,
    config: object | None = None,
    token: DepositToken | None = None,
    now: float | None = None,
) -> tuple[tuple[TxStep, ...], TxBundle]:
    """Fetch, validate, and plan one instrument bundle; nothing unvalidated escapes.

    ``token`` is the deposit token the raw ``amount`` was computed in, and is
    checked against the response's ``tokenIn``.
    """
    fetch = getattr(client, "instrument_calldata", None)
    if not callable(fetch):
        raise TypeError("client does not implement instrument_calldata")
    query = InstrumentCalldataQuery(
        action=action,
        account=account,
        amount=amount,
        slippage_bps=_int_config(config, "slippage_bps"),
    )
    response = fetch(instrument_id, query)
    validate_instrument_calldata(
        response,
        instrument_id=instrument_id,
        account=account,
        action=action,
        chain_id=chain_id,
        amount=amount,
        min_ttl_seconds=min_ttl_seconds(config),
        now=now,
    )
    if token is not None:
        ensure_deposit_token(response, token)
    return plan_bundle(
        response,
        leg_index=leg_index,
        first_step_index=first_step_index,
    )


def refresh_bundle(
    client: object,
    bundle: TxBundle,
    *,
    config: object | None = None,
    now: float | None = None,
) -> tuple[tuple[TxStep, ...], TxBundle]:
    """Fresh calldata for the same logical leg, validated like the original.

    Keeps the bundle ID with a new digest, so completion recorded for the old
    calls never applies. Step indexes start at zero.
    """
    token = (
        DepositToken(
            chain_id=bundle.chain_id,
            address=bundle.token_in.address,
            decimals=bundle.token_in.decimals,
        )
        if bundle.action == "deposit"
        else None
    )
    return request_bundle(
        client,
        instrument_id=bundle.instrument_id,
        action=bundle.action,
        account=bundle.account,
        chain_id=bundle.chain_id,
        amount=bundle.amount,
        leg_index=bundle.leg_index,
        first_step_index=0,
        config=config,
        token=token,
        now=now,
    )


def needs_refresh(
    bundle: TxBundle,
    *,
    min_ttl_seconds: int,
    now: float | None = None,
) -> bool:
    """Whether a planned bundle is too close to expiry to be signed as it is."""
    try:
        ensure_calldata_lifetime(bundle, min_ttl_seconds=min_ttl_seconds, now=now)
    except CalldataExpiredError:
        return True
    return False


def min_ttl_seconds(config: object | None) -> int:
    configured = _int_config(config, "min_calldata_ttl_seconds")
    return DEFAULT_MIN_CALLDATA_TTL_SECONDS if configured is None else configured


def bundle_id(leg_index: int, instrument_id: str, action: CalldataAction) -> str:
    """The logical leg a bundle serves; stable when its calldata is rebuilt."""
    return f"leg:{leg_index}:{instrument_id}:{action}"


def ensure_calldata_supported(config: object | None) -> None:
    """Reject settings the calldata API has no equivalent for."""
    fee_bps = _config_value(config, "referral_fee_bps")
    wallet = _config_value(config, "referral_wallet")
    if (fee_bps is not None and int(fee_bps) > 0) or wallet is not None:
        raise CalldataUnsupportedError(
            "referral fees are not supported by the 1Tx calldata API; unset "
            "ONE_TX_REFERRAL_FEE_BPS and ONE_TX_REFERRAL_WALLET or use "
            "ONE_TX_TRANSACTION_API=legacy"
        )


def uses_calldata_api(config: object | None) -> bool:
    return _config_value(config, "transaction_api") == "calldata"


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


def _bundle_token(token: CalldataTokenInfo) -> BundleToken:
    return BundleToken(
        address=token.address,
        symbol=token.symbol,
        decimals=token.decimals,
    )


def _config_value(config: object | None, attr: str) -> object | None:
    if config is None:
        return None
    if isinstance(config, Mapping):
        return config.get(attr)
    return getattr(config, attr, None)


def _int_config(config: object | None, attr: str) -> int | None:
    value = _config_value(config, attr)
    return None if value is None else int(value)  # type: ignore[call-overload]


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
