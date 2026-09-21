"""Levered loops through the calldata path: discovery, bundles, announcement.

A loop is ONE synthetic instrument keyed by its loop id:
its allocation leg's ``usd`` is the equity, and ``leverage`` is the multiple
the gross position is built to. 1Tx builds the whole loop as one ordered batch
and simulates it; this module binds that bundle to the leg it serves, holds
the venue's measurement against open-allocator's model, and says what the
operation does to the rest of the pool before anything is signed.

Three rules:

- **One bundle, one operation, one idempotency unit.** A loop is never sent
  call by call: a half-built loop is an unhedged levered position. Its inner
  calls stay in the plan for review, but only the bundle is ever completed.
- **Model before, measure after.** The health factor open-allocator models at
  the requested leverage and the pair's EFFECTIVE threshold must match the one
  1Tx simulated, or the bundle is refused before signing (:class:`LoopDivergenceError`).
- **Pool-wide impact is announced.** A bundle that changes account-wide pool
  state (Aave's e-mode) re-prices every other position the account holds in
  that pool; the announcement names them, and when they cannot be read the
  operation cannot be confirmed.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal, cast, get_args

from eth_abi import encode as abi_encode
from eth_utils import keccak
from web3 import HTTPProvider, Web3

from open_allocator.core import amounts, levered
from open_allocator.core import positions as positions_core
from open_allocator.core.types import (
    Allocation,
    BundleLeftover,
    BundleLoop,
    BundleRequirement,
    BundleToken,
    FrozenModel,
    PolicyCaps,
    RewardPriceBasis,
    TxBundle,
    TxStep,
    Vault,
    bundle_digest,
)
from open_allocator.exec import calldata, chains
from open_allocator.exec.client import (
    LoopAction,
    LoopCalldataQuery,
    LoopCalldataResponse,
    LoopRow,
)

LOOP_CALLDATA_ENDPOINT = "GET /loops/:loopId/calldata"
BUNDLE_ACTIONS: Mapping[
    LoopAction, Literal["loop_open", "loop_adjust", "loop_close"]
] = {
    "open": "loop_open",
    "adjust": "loop_adjust",
    "close": "loop_close",
}
_REWARD_PRICE_BASES: frozenset[str] = frozenset(get_args(RewardPriceBasis))
_LEVERAGE_QUANTUM = Decimal("0.0001")


class LoopDivergenceError(calldata.CalldataValidationError):
    """The venue's simulation does not match the model; nothing may be signed."""

    def __init__(self, loop_id: str, check: levered.SimulationCheck) -> None:
        self.check = check
        super().__init__(
            f"loop {loop_id}: model and simulation diverge — "
            + "; ".join(check.divergences)
            + ". If the account holds anything else in this pool, the pool's "
            "account-level health factor includes it"
        )


class LoopPolicyError(calldata.CalldataValidationError):
    """The simulated position breaks a policy floor the model passed."""


class LoopSkipped(FrozenModel):
    loop_id: str
    reason: str


def loop_vaults(
    rows: Iterable[LoopRow],
    instruments: Iterable[Vault],
) -> tuple[list[Vault], tuple[LoopSkipped, ...]]:
    """Each loopable pair as a levered :class:`Vault` keyed by its loop id.

    The row inherits its collateral instrument's discovered fields — token,
    decimals, yield token, sector and risk labels — because the collateral is
    what the account holds. Its APY and history are the collateral's at L=1:
    ``f(L)`` is :func:`open_allocator.core.levered.quote`'s job, not a
    discovered number. ``max_leverage`` is the screen's pool default until
    :func:`refine_levered_vaults` replaces it with the pair's effective one.

    A pair whose collateral instrument was not discovered is skipped, not
    synthesised.
    """
    by_id = {vault.instrument_id.casefold(): vault for vault in instruments}
    vaults: list[Vault] = []
    skipped: list[LoopSkipped] = []
    for row in rows:
        collateral = by_id.get(row.collateral.instrument_id.casefold())
        if collateral is None:
            skipped.append(
                LoopSkipped(
                    loop_id=row.loop_id,
                    reason=(
                        f"collateral instrument {row.collateral.instrument_id} "
                        "is not in the discovered universe"
                    ),
                )
            )
            continue
        ceiling = row.leverage.max_leverage
        if ceiling is None and row.leverage.ltv is not None and row.leverage.ltv < 1:
            ceiling = levered.max_leverage(row.leverage.ltv)
        if ceiling is None:
            skipped.append(
                LoopSkipped(loop_id=row.loop_id, reason="the screen reports no ltv")
            )
            continue
        basis = None if row.reward is None else row.reward.basis
        payload = collateral.model_dump()
        payload.update(
            instrument_id=row.loop_id,
            protocol=row.protocol,
            chain_id=row.chain_id,
            asset=row.collateral.symbol,
            token_address=row.collateral.token_address,
            reward_price_basis=basis if basis in _REWARD_PRICE_BASES else None,
            is_levered=True,
            max_leverage=ceiling,
            liquidation_threshold=None,
            debt_asset=row.debt.symbol,
            reward_liquidity_usd=(
                None if row.reward is None else row.reward.liquidity_usd
            ),
            protocol_address=row.pool,
            collateral_instrument_id=row.collateral.instrument_id,
            debt_instrument_id=row.debt.instrument_id,
        )
        vaults.append(Vault.model_validate(payload))
    return vaults, tuple(skipped)


def discover_loop_vaults(
    client: object,
    instruments: Iterable[Vault],
) -> tuple[list[Vault], tuple[LoopSkipped, ...]]:
    return loop_vaults(loop_rows(client), instruments)


def loop_rows(client: object) -> tuple[LoopRow, ...]:
    fetch = getattr(client, "loops", None)
    if not callable(fetch):
        raise TypeError("client does not implement loops")
    return tuple(fetch().data)


def leverage_param(leverage: float) -> str:
    """``leverage`` as the endpoint's decimal, refusing one it would round.

    The endpoint takes at most four decimal places. A leverage finer than that
    would be built to a different multiple than the one policy checked.
    """
    try:
        value = Decimal(repr(float(leverage)))
    except (InvalidOperation, ValueError) as error:
        raise calldata.CalldataAmountError(
            f"leverage {leverage!r} is not a number"
        ) from error
    quantized = value.quantize(_LEVERAGE_QUANTUM)
    if quantized != value:
        raise calldata.CalldataAmountError(
            f"leverage {leverage} has more than four decimal places; the loop "
            "endpoint would build a different multiple"
        )
    if quantized <= 1:
        raise calldata.CalldataAmountError(f"leverage {leverage} must be above 1")
    return format(quantized.normalize(), "f")


@dataclass(frozen=True)
class LoopTarget:
    """What a loop bundle is expected to be about, checked field by field."""

    loop_id: str
    chain_id: int
    pool: str | None
    collateral_instrument_id: str
    debt_instrument_id: str
    collateral_token: str
    same_asset: bool

    @classmethod
    def from_vault(cls, vault: Vault) -> "LoopTarget":
        if (
            not vault.is_levered
            or vault.collateral_instrument_id is None
            or vault.debt_instrument_id is None
            or vault.token_address is None
        ):
            raise calldata.CalldataValidationError(
                f"{vault.instrument_id} is not a discovered loop pair; its legs "
                "and collateral token are unknown"
            )
        return cls(
            loop_id=vault.instrument_id,
            chain_id=vault.chain_id,
            pool=vault.protocol_address,
            collateral_instrument_id=vault.collateral_instrument_id,
            debt_instrument_id=vault.debt_instrument_id,
            collateral_token=vault.token_address,
            same_asset=not vault.cross_asset,
        )

    @classmethod
    def from_bundle(cls, bundle: TxBundle) -> "LoopTarget":
        loop = bundle.loop
        if loop is None:
            raise calldata.CalldataValidationError(
                f"bundle {bundle.bundle_id} is not a loop bundle"
            )
        return cls(
            loop_id=loop.loop_id,
            chain_id=bundle.chain_id,
            pool=loop.pool,
            collateral_instrument_id=loop.collateral_instrument_id,
            debt_instrument_id=loop.debt_instrument_id,
            collateral_token=bundle.token_in.address,
            same_asset=loop.same_asset,
        )


def validate_loop_calldata(
    response: LoopCalldataResponse,
    *,
    target: LoopTarget,
    account: str,
    action: LoopAction,
    amount: str | None,
    leverage: str | None,
    min_ttl_seconds: int,
    now: float | None = None,
) -> LoopCalldataResponse:
    """Bind a parsed loop bundle to the pair, account and request it answers.

    The strict parse fixed its shape; this fixes its meaning. The pair, pool
    and collateral token must be the discovered ones, the leverage the one
    requested, and the e-mode the simulation ended in the one the bundle
    declares it sets.
    """
    require = calldata._require_equal
    require("loopId", response.loop_id, target.loop_id, fold=True)
    require("account", response.account, account, fold=True)
    require("action", response.action, action)
    require("chainId", response.chain_id, target.chain_id)
    require("amountIn", response.amount_in, amount)
    requested = None if leverage is None else float(leverage)
    require("requestedLeverage", response.leverage.requested_leverage, requested)
    block = response.leverage
    require(
        "collateralInstrumentId",
        block.collateral_instrument_id,
        target.collateral_instrument_id,
        fold=True,
    )
    require(
        "debtInstrumentId",
        block.debt_instrument_id,
        target.debt_instrument_id,
        fold=True,
    )
    if target.pool is not None:
        require("pool", block.pool, target.pool, fold=True)
    require(
        "tokenIn.address", response.token_in.address, target.collateral_token, fold=True
    )
    # The flag must mean exactly "this bundle changes the account's category".
    require(
        "requiresAccountConfig",
        block.requires_account_config,
        block.account_config.current != block.account_config.target,
    )
    require(
        "simulated eModeCategory",
        response.simulated.e_mode_category,
        block.account_config.target,
    )
    if action == "close":
        require("simulated debt", response.simulated.debt, "0")
    else:
        # The effective pair parameters, as the pool computed them for the
        # account after the bundle, are the ones the leverage block publishes.
        require("simulated ltv", response.simulated.ltv, block.ltv)
        require(
            "simulated liquidationThreshold",
            response.simulated.liquidation_threshold,
            block.liquidation_threshold,
        )
    calldata.ensure_calldata_lifetime(
        _Expiring(target.loop_id, response.expires_at),  # type: ignore[arg-type]
        min_ttl_seconds=min_ttl_seconds,
        now=now,
    )
    return response


def check_measurement(
    response: LoopCalldataResponse,
    *,
    same_asset: bool,
    caps: PolicyCaps | None = None,
) -> levered.SimulationCheck:
    """Hold the venue's measurement against the model; raise on any gap.

    Two stops. A divergence between modelled and simulated health factor or
    leverage is :class:`LoopDivergenceError`. A simulated position under a
    policy floor — ``min_health_factor``, and ``min_depeg_buffer_bps`` on a
    cross-asset pair — is :class:`LoopPolicyError`, because the policy check
    ran on the model and this is what the pool says.
    """
    check = levered.check_simulation(
        requested_leverage=response.leverage.requested_leverage,
        liquidation_threshold=response.leverage.liquidation_threshold,
        same_asset=same_asset,
        measured_leverage=response.simulated.leverage,
        measured_health_factor=response.simulated.health_factor,
        measured_depeg_buffer_bps=response.simulated.depeg_buffer_bps,
    )
    if not check.ok:
        raise LoopDivergenceError(response.loop_id, check)
    if caps is None or response.action == "close":
        return check
    hf = response.simulated.health_factor
    if caps.min_health_factor is not None and (
        hf is None or hf < caps.min_health_factor
    ):
        raise LoopPolicyError(
            f"loop {response.loop_id}: simulated health factor {hf} is below the "
            f"policy floor min_health_factor {caps.min_health_factor}"
        )
    buffer = response.simulated.depeg_buffer_bps
    if (
        not same_asset
        and caps.min_depeg_buffer_bps is not None
        and (buffer is None or buffer < caps.min_depeg_buffer_bps)
    ):
        raise LoopPolicyError(
            f"loop {response.loop_id}: simulated depeg buffer {buffer} bps is below "
            f"the policy floor min_depeg_buffer_bps {caps.min_depeg_buffer_bps}"
        )
    return check


def plan_loop_bundle(
    response: LoopCalldataResponse,
    *,
    check: levered.SimulationCheck,
    same_asset: bool,
    leg_index: int,
    first_step_index: int,
) -> tuple[tuple[TxStep, ...], TxBundle]:
    """Plan steps for a validated loop bundle, plus the metadata that binds them."""
    steps = tuple(
        TxStep(
            to=call.to,
            data=call.data,
            value=int(call.value),
            chain_id=call.chain_id,
            kind=call.type,
        )
        for call in response.calls
    )
    block = response.leverage
    action = BUNDLE_ACTIONS[response.action]
    loop = BundleLoop(
        loop_id=response.loop_id,
        recipe=response.recipe,
        pool=block.pool,
        collateral_instrument_id=block.collateral_instrument_id,
        debt_instrument_id=block.debt_instrument_id,
        debt_token=_debt_token(response),
        same_asset=same_asset,
        requested_leverage=block.requested_leverage,
        ltv=block.ltv,
        liquidation_threshold=block.liquidation_threshold,
        params_basis=block.params_basis,
        oracle=block.oracle_source.oracle,
        requires_account_config=block.requires_account_config,
        account_config_current=block.account_config.current,
        account_config_target=block.account_config.target,
        simulated_collateral=response.simulated.collateral,
        simulated_debt=response.simulated.debt,
        simulated_leverage=response.simulated.leverage,
        simulated_health_factor=response.simulated.health_factor,
        simulated_depeg_buffer_bps=response.simulated.depeg_buffer_bps,
        modelled_health_factor=check.modelled_health_factor,
        modelled_depeg_buffer_bps=check.modelled_depeg_buffer_bps,
        turns=len(response.turns),
    )
    # A close or adjust acts on the whole position on the pair.
    amount = response.amount_in or "max"
    digest = bundle_digest(
        instrument_id=response.loop_id,
        action=action,
        account=response.account,
        chain_id=response.chain_id,
        amount=amount,
        steps=steps,
        quote_block=response.quote_block,
        expires_at=response.expires_at,
        loop=loop,
    )
    bundle = TxBundle(
        bundle_id=f"leg:{leg_index}:{response.loop_id}:{action}",
        digest=digest,
        leg_index=leg_index,
        instrument_id=response.loop_id,
        action=action,
        account=response.account,
        chain_id=response.chain_id,
        step_indexes=tuple(range(first_step_index, first_step_index + len(steps))),
        endpoint=LOOP_CALLDATA_ENDPOINT,
        amount=amount,
        token_in=BundleToken(
            address=response.token_in.address,
            symbol=response.token_in.symbol,
            decimals=response.token_in.decimals,
        ),
        token_out=BundleToken(
            address=response.token_out.address,
            symbol=response.token_out.symbol,
            decimals=response.token_out.decimals,
        ),
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
        loop=loop,
    )
    return steps, bundle


def request_loop_bundle(
    client: object,
    *,
    target: LoopTarget,
    action: LoopAction,
    account: str,
    amount: str | None,
    leverage: float | None,
    leg_index: int,
    first_step_index: int = 0,
    config: object | None = None,
    caps: PolicyCaps | None = None,
    now: float | None = None,
) -> tuple[tuple[TxStep, ...], TxBundle]:
    """Fetch, validate, measure-check and plan one loop bundle.

    Nothing that fails a check escapes: a response that does not answer this
    request, or whose simulation the model does not reproduce, raises.
    """
    response = fetch_loop_calldata(
        client,
        target=target,
        action=action,
        account=account,
        amount=amount,
        leverage=leverage,
        config=config,
        now=now,
    )
    check = check_measurement(response, same_asset=target.same_asset, caps=caps)
    return plan_loop_bundle(
        response,
        check=check,
        same_asset=target.same_asset,
        leg_index=leg_index,
        first_step_index=first_step_index,
    )


def fetch_loop_calldata(
    client: object,
    *,
    target: LoopTarget,
    action: LoopAction,
    account: str,
    amount: str | None,
    leverage: float | None,
    config: object | None = None,
    now: float | None = None,
) -> LoopCalldataResponse:
    fetch = getattr(client, "loop_calldata", None)
    if not callable(fetch):
        raise TypeError("client does not implement loop_calldata")
    param = None if leverage is None else leverage_param(leverage)
    query = LoopCalldataQuery(
        action=action,
        account=account,
        leverage=param,
        amount=amount,
        slippage_bps=calldata._int_config(config, "slippage_bps"),
    )
    response = cast(LoopCalldataResponse, fetch(target.loop_id, query))
    return validate_loop_calldata(
        response,
        target=target,
        account=account,
        action=action,
        amount=amount,
        leverage=param,
        min_ttl_seconds=calldata.min_ttl_seconds(config),
        now=now,
    )


def refresh_loop_bundle(
    client: object,
    bundle: TxBundle,
    *,
    config: object | None = None,
    now: float | None = None,
) -> tuple[tuple[TxStep, ...], TxBundle]:
    """Fresh calldata for the same loop leg, checked like the original.

    The pair's effective parameters and the account change must be the ones
    the plan was announced and policy-checked with; if the venue now applies
    others, the plan is rebuilt rather than signed.
    """
    loop = bundle.loop
    if loop is None:
        raise calldata.CalldataValidationError(
            f"bundle {bundle.bundle_id} is not a loop bundle"
        )
    action = cast(
        LoopAction,
        next(key for key, value in BUNDLE_ACTIONS.items() if value == bundle.action),
    )
    steps, fresh = request_loop_bundle(
        client,
        target=LoopTarget.from_bundle(bundle),
        action=action,
        account=bundle.account,
        amount=None if bundle.amount == "max" else bundle.amount,
        leverage=loop.requested_leverage,
        leg_index=bundle.leg_index,
        config=config,
        now=now,
    )
    assert fresh.loop is not None
    for name in (
        "ltv",
        "liquidation_threshold",
        "params_basis",
        "requires_account_config",
        "account_config_target",
    ):
        calldata._require_equal(
            f"rebuilt loop {name}", getattr(fresh.loop, name), getattr(loop, name)
        )
    return steps, fresh


def params_key(leg_index: int, instrument_id: str) -> str:
    return f"loop-params:leg:{leg_index}:{instrument_id}"


def save_params(store: object | None, bundle: TxBundle) -> None:
    """Record the effective parameters a sent loop was built and checked under.

    A rerun does not re-read a leg that already went out, but policy still
    judges the whole allocation, and it must judge that leg on the parameters
    it was opened with rather than on the screen's lower bound.
    """
    from open_allocator.exec.execute import _store_mark_completed

    if bundle.loop is None:
        return
    _store_mark_completed(
        store,
        params_key(bundle.leg_index, bundle.instrument_id),
        {
            "ltv": bundle.loop.ltv,
            "liquidation_threshold": bundle.loop.liquidation_threshold,
        },
    )


def _saved_params(
    store: object | None, leg_index: int, instrument_id: str
) -> tuple[float, float] | None:
    from open_allocator.exec.bridge_state import _stored_value

    value = _stored_value(store, params_key(leg_index, instrument_id))
    if not isinstance(value, Mapping):
        return None
    try:
        return float(value["ltv"]), float(value["liquidation_threshold"])
    except (KeyError, TypeError, ValueError):
        return None


def refine_levered_vaults(
    client: object,
    vaults: Sequence[Vault],
    allocation: Allocation,
    *,
    account: str,
    config: object | None = None,
    skip: Iterable[int] = (),
    store: object | None = None,
) -> list[Vault]:
    """Replace a levered leg's screen parameters with the pair's effective ones.

    The screen publishes the pool default LTV and no liquidation threshold, so
    policy can only bound a loop's health factor from below with it. The
    loop endpoint reads the pair's effective parameters at the quote block —
    an e-mode category, when one admits the pair — and policy has to judge the
    loop on those, before anything is planned. Legs in ``skip`` (already sent)
    are not re-read; they keep the parameters :func:`save_params` recorded when
    they went out.
    """
    by_id = {vault.instrument_id: vault for vault in vaults}
    done = set(skip)
    refined: dict[str, Vault] = {}
    for index, leg in enumerate(allocation.legs):
        vault = by_id.get(leg.instrument_id)
        if vault is None or not vault.is_levered:
            continue
        if index in done:
            saved = _saved_params(store, index, leg.instrument_id)
            if saved is not None:
                ltv, threshold = saved
                refined[vault.instrument_id] = Vault.model_validate(
                    {
                        **vault.model_dump(),
                        "max_leverage": levered.max_leverage(ltv),
                        "liquidation_threshold": threshold,
                    }
                )
            continue
        if leg.leverage is None:
            raise calldata.CalldataValidationError(
                f"leg {index} ({leg.instrument_id}) is levered but names no "
                "leverage; a loop is built to a chosen multiple, never to the "
                "venue's ceiling"
            )
        target = LoopTarget.from_vault(vault)
        response = fetch_loop_calldata(
            client,
            target=target,
            action="open",
            account=account,
            amount=equity_raw(leg.usd, vault),
            leverage=leg.leverage,
            config=config,
        )
        block = response.leverage
        refined[vault.instrument_id] = Vault.model_validate(
            {
                **vault.model_dump(),
                "max_leverage": block.max_leverage,
                "liquidation_threshold": block.liquidation_threshold,
            }
        )
    return [refined.get(vault.instrument_id, vault) for vault in vaults]


def equity_raw(usd: float, vault: Vault) -> str:
    """A leg's equity as raw units of the loop's collateral token, rounded down."""
    if vault.token_decimals is None:
        raise calldata.CalldataAmountError(
            f"{vault.instrument_id} reports no collateral decimals"
        )
    raw = amounts.to_raw_units(usd, vault.token_decimals, name="loop equity")
    if raw <= 0:
        raise calldata.CalldataAmountError(
            f"loop equity {usd} rounds down to zero raw units"
        )
    return str(raw)


# --- Announcement (5.3) ----------------------------------------------------


class PoolPosition(FrozenModel):
    """A position the account holds in the pool a loop bundle reconfigures."""

    instrument_id: str
    symbol: str
    usd_value: float


class LoopLegAmount(FrozenModel):
    instrument_id: str
    symbol: str | None
    raw: str
    amount: str


class KillSwitch(FrozenModel):
    """The reward price at which the loop's gradient turns negative."""

    reward_token: str
    reward_symbol: str | None
    reward_price_usd: float
    trigger_price_usd: float
    price_ratio: float
    caveats: tuple[str, ...] = ()


class LoopAnnouncement(FrozenModel):
    """Everything a levered operation must say before it may be confirmed."""

    bundle_id: str
    loop_id: str
    action: str
    chain_id: int
    protocol: str | None
    pool: str
    recipe: str
    collateral: LoopLegAmount
    debt: LoopLegAmount
    # The open's supplied amount; an adjust or close keeps or returns equity.
    equity: LoopLegAmount | None
    requested_leverage: float | None
    simulated_leverage: float | None
    modelled_health_factor: float | None
    simulated_health_factor: float | None
    modelled_depeg_buffer_bps: int | None
    simulated_depeg_buffer_bps: int | None
    params_basis: str
    ltv: float
    liquidation_threshold: float
    same_asset: bool
    kill_switch: KillSwitch | None
    kill_switch_unavailable: str | None = None
    requires_account_config: bool
    account_config: str
    # Every other position the account holds in the same pool. ``None`` when
    # the book could not be read: then the pool-wide impact is unknown.
    pool_positions: tuple[PoolPosition, ...] | None
    # The most each token may be left in the wallet — 1Tx's conservative cap
    # (every borrow), not the expected remainder, which on a cross-asset loop
    # is the unused swap slippage.
    max_leftovers: tuple[LoopLegAmount, ...] = ()
    confirmable: bool
    blockers: tuple[str, ...] = ()


def announce(
    bundle: TxBundle,
    *,
    vaults: Sequence[Vault],
    row: LoopRow | None,
    pool_positions: Sequence[PoolPosition] | None,
) -> LoopAnnouncement:
    """The levered announcement for one planned loop bundle.

    ``pool_positions`` are the account's holdings in the loop's pool, read by
    the caller; ``None`` means they could not be read. When the bundle changes
    account-wide pool state, an unknown pool is a blocker: the operation cannot
    be confirmed without showing what else it re-prices.
    """
    loop = bundle.loop
    if loop is None:
        raise calldata.CalldataValidationError(
            f"bundle {bundle.bundle_id} is not a loop bundle"
        )
    by_id = {vault.instrument_id.casefold(): vault for vault in vaults}
    collateral_vault = by_id.get(loop.collateral_instrument_id.casefold())
    debt_vault = by_id.get(loop.debt_instrument_id.casefold())
    loop_vault = by_id.get(loop.loop_id.casefold())

    collateral = _leg_amount(
        loop.collateral_instrument_id,
        bundle.token_out.symbol
        or (collateral_vault.asset if collateral_vault else None),
        loop.simulated_collateral,
        bundle.token_out.decimals,
    )
    debt_decimals = (
        loop.debt_token.decimals
        if loop.debt_token is not None
        else (debt_vault.token_decimals if debt_vault else None)
    )
    debt = _leg_amount(
        loop.debt_instrument_id,
        (loop.debt_token.symbol if loop.debt_token else None)
        or (debt_vault.asset if debt_vault else None),
        loop.simulated_debt,
        debt_decimals,
    )
    equity = (
        None
        if bundle.amount == "max"
        else _leg_amount(
            loop.collateral_instrument_id,
            bundle.token_in.symbol,
            bundle.amount,
            bundle.token_in.decimals,
        )
    )
    max_leftovers = tuple(
        _leg_amount(
            item.token,
            _symbol_for(item.token, vaults, loop),
            item.max_amount,
            _decimals_for(item.token, vaults, loop),
        )
        for item in bundle.leftovers
    )
    others = None
    if pool_positions is not None:
        others = tuple(
            position
            for position in pool_positions
            if position.instrument_id.casefold() != loop.loop_id.casefold()
        )
    blockers: list[str] = []
    if loop.requires_account_config and others is None:
        blockers.append(
            f"loop {loop.loop_id} changes the account's configuration in pool "
            f"{loop.pool} (e-mode {loop.account_config_current} -> "
            f"{loop.account_config_target}), which re-prices every other position "
            "held there, and the account's positions could not be read to say "
            "which; it cannot be confirmed until they are"
        )
    kill, kill_reason = _kill_switch(row, loop)
    return LoopAnnouncement(
        bundle_id=bundle.bundle_id,
        loop_id=loop.loop_id,
        action=bundle.action,
        chain_id=bundle.chain_id,
        protocol=loop_vault.protocol if loop_vault else (row.protocol if row else None),
        pool=loop.pool,
        recipe=loop.recipe,
        collateral=collateral,
        debt=debt,
        equity=equity,
        requested_leverage=loop.requested_leverage,
        simulated_leverage=loop.simulated_leverage,
        modelled_health_factor=loop.modelled_health_factor,
        simulated_health_factor=loop.simulated_health_factor,
        modelled_depeg_buffer_bps=loop.modelled_depeg_buffer_bps,
        simulated_depeg_buffer_bps=loop.simulated_depeg_buffer_bps,
        params_basis=loop.params_basis,
        ltv=loop.ltv,
        liquidation_threshold=loop.liquidation_threshold,
        same_asset=loop.same_asset,
        kill_switch=kill,
        kill_switch_unavailable=kill_reason,
        requires_account_config=loop.requires_account_config,
        account_config=(
            f"aave e-mode {loop.account_config_current} -> {loop.account_config_target}"
            if loop.requires_account_config
            else f"unchanged (aave e-mode {loop.account_config_target})"
        ),
        pool_positions=others,
        max_leftovers=max_leftovers,
        confirmable=not blockers,
        blockers=tuple(blockers),
    )


def pool_positions(
    holdings: Iterable[object],
    *,
    pool: str,
    chain_id: int,
    vaults: Sequence[Vault],
) -> tuple[PoolPosition, ...]:
    """The holdings that sit in ``pool`` on ``chain_id``, by discovered address.

    A holding is matched through its instrument's discovered
    ``protocol_address``; nothing about which instruments share a pool is
    assumed.
    """
    in_pool = {
        vault.instrument_id.casefold()
        for vault in vaults
        if vault.chain_id == chain_id
        and vault.protocol_address is not None
        and vault.protocol_address.casefold() == pool.casefold()
    }
    found: list[PoolPosition] = []
    for holding in holdings:
        instrument_id = str(getattr(holding, "instrument_id", ""))
        if (
            getattr(holding, "chain_id", None) == chain_id
            and instrument_id.casefold() in in_pool
        ):
            found.append(
                PoolPosition(
                    instrument_id=instrument_id,
                    symbol=str(getattr(holding, "symbol", "")),
                    usd_value=float(getattr(holding, "usd_value", 0.0)),
                )
            )
    return tuple(found)


def _kill_switch(
    row: LoopRow | None,
    loop: BundleLoop,
) -> tuple[KillSwitch | None, str | None]:
    if row is None:
        return None, "the loop's screen row was not read"
    if row.reward is None or not row.reward.tokens:
        return None, "the pair pays no reward, so no reward price triggers an exit"
    priced = [token for token in row.reward.tokens if token.price_usd]
    if not priced:
        return None, "no reward token has a price to trigger on"
    if len(priced) > 1:
        return None, (
            f"{len(priced)} reward tokens are priced; the reward APY is not "
            "attributable to one price"
        )
    token = priced[0]
    assert token.price_usd is not None
    spec = levered.LeveredSpec(
        loop_id=row.loop_id,
        chain_id=row.chain_id,
        protocol=row.protocol,
        collateral_instrument_id=row.collateral.instrument_id,
        debt_instrument_id=row.debt.instrument_id,
        collateral_asset=row.collateral.symbol,
        debt_asset=row.debt.symbol,
        supply_apy_base_pct=row.collateral.apy_base or 0.0,
        supply_apy_reward_pct=row.collateral.apy_reward or 0.0,
        borrow_apy_base_pct=row.debt.apy_base_borrow or 0.0,
        borrow_apy_reward_pct=row.debt.apy_reward_borrow or 0.0,
        ltv=loop.ltv,
        liquidation_threshold=loop.liquidation_threshold,
        reward_price_usd=token.price_usd,
    )
    ratio = levered.zero_gradient_reward_ratio(spec)
    if ratio is None:
        return None, "the pair pays no reward, so no reward price triggers an exit"
    caveats: list[str] = [
        "screen rates, stale intraday; the gradient is a difference of "
        "differences and utilisation moves it"
    ]
    unpriced = [token.address for token in row.reward.tokens if not token.price_usd]
    if unpriced:
        caveats.append(
            f"the reward APY also includes {len(unpriced)} unpriced token(s) "
            f"({', '.join(unpriced)}), attributed here to "
            f"{token.symbol or token.address}"
        )
    if row.reward.basis != "traded":
        caveats.append(
            f"the reward APY is {row.reward.basis}-priced, not at a traded quote"
        )
    return (
        KillSwitch(
            reward_token=token.address,
            reward_symbol=token.symbol,
            reward_price_usd=token.price_usd,
            trigger_price_usd=ratio * token.price_usd,
            price_ratio=ratio,
            caveats=tuple(caveats),
        ),
        None,
    )


def _leg_amount(
    instrument_id: str,
    symbol: str | None,
    raw: str,
    decimals: int | None,
) -> LoopLegAmount:
    amount = (
        raw if decimals is None else str(amounts.from_raw_units(int(raw), decimals))
    )
    return LoopLegAmount(
        instrument_id=instrument_id, symbol=symbol, raw=raw, amount=amount
    )


def _debt_token(response: LoopCalldataResponse) -> BundleToken | None:
    # The debt leg's token is what a cross-asset loop leaves behind; its
    # decimals are not in the envelope, so the leftover names only the address.
    if response.swap is None:
        return BundleToken(
            address=response.token_in.address,
            symbol=response.token_in.symbol,
            decimals=response.token_in.decimals,
        )
    return None


def _symbol_for(token: str, vaults: Sequence[Vault], loop: BundleLoop) -> str | None:
    vault = _vault_for_token(token, vaults, loop)
    return None if vault is None else vault.asset


def _decimals_for(token: str, vaults: Sequence[Vault], loop: BundleLoop) -> int | None:
    vault = _vault_for_token(token, vaults, loop)
    return None if vault is None else vault.token_decimals


def _vault_for_token(
    token: str, vaults: Sequence[Vault], loop: BundleLoop
) -> Vault | None:
    for vault in vaults:
        if (
            vault.instrument_id.casefold()
            in (
                loop.collateral_instrument_id.casefold(),
                loop.debt_instrument_id.casefold(),
            )
            and vault.token_address is not None
            and vault.token_address.casefold() == token.casefold()
        ):
            return vault
    return None


# --- Positions (5.2) -------------------------------------------------------

# Aave v3 pool reads, hand-encoded: three methods do not need an ABI file.
_GET_USER_ACCOUNT_DATA = keccak(text="getUserAccountData(address)")[:4]
_GET_USER_CONFIGURATION = keccak(text="getUserConfiguration(address)")[:4]
_GET_RESERVE_ADDRESS_BY_ID = keccak(text="getReserveAddressById(uint16)")[:4]
_MAX_RESERVES = 128
_WAD = 10**18
_MAX_UINT256 = 2**256 - 1


class PoolReadError(RuntimeError):
    """A pool account could not be read; never to be taken as "no debt"."""


def loop_pairs(rows: Iterable[LoopRow]) -> tuple[positions_core.LoopPair, ...]:
    return tuple(
        positions_core.LoopPair(
            loop_id=row.loop_id,
            chain_id=row.chain_id,
            pool=row.pool,
            collateral_instrument_id=row.collateral.instrument_id,
            debt_instrument_id=row.debt.instrument_id,
            debt_token=row.debt.token_address,
        )
        for row in rows
    )


def read_pool_account(
    w3: Web3,
    *,
    chain_id: int,
    pool: str,
    account: str,
) -> positions_core.PoolAccount:
    """The account's totals, health factor and reserve flags in one Aave pool.

    Reads the account's own state, the way a wallet reads a balance; which
    pools and pairs exist still comes from discovery.
    """
    holder = abi_encode(["address"], [Web3.to_checksum_address(account)])
    data = _pool_call(w3, pool, _GET_USER_ACCOUNT_DATA + holder, words=6)
    total_collateral, total_debt, *_rest, health = data
    (bitmap,) = _pool_call(w3, pool, _GET_USER_CONFIGURATION + holder, words=1)
    borrowed: list[str] = []
    collateral: list[str] = []
    for reserve_id in range(_MAX_RESERVES):
        flags = (bitmap >> (2 * reserve_id)) & 0b11
        if not flags:
            continue
        (asset,) = _pool_call(
            w3,
            pool,
            _GET_RESERVE_ADDRESS_BY_ID + abi_encode(["uint16"], [reserve_id]),
            words=1,
        )
        address = Web3.to_checksum_address(asset.to_bytes(32, "big")[-20:])
        if flags & 0b01:
            borrowed.append(address)
        if flags & 0b10:
            collateral.append(address)
    return positions_core.PoolAccount(
        chain_id=chain_id,
        pool=pool,
        total_collateral_base=total_collateral,
        total_debt_base=total_debt,
        health_factor=(
            None if health == _MAX_UINT256 or total_debt == 0 else health / _WAD
        ),
        borrowed_assets=tuple(borrowed),
        collateral_assets=tuple(collateral),
    )


def read_book(
    client: object,
    address: str,
    config: object | None = None,
    *,
    rows: Sequence[LoopRow] | None = None,
) -> tuple[positions_core.Positions, tuple[str, ...]]:
    """The account's positions, with every loop reported at its equity.

    Pools holding a leg of a discovered loop pair are read on chain; a pool
    with no debt changes nothing. A pool that cannot be read raises rather
    than reporting its collateral gross. When the loop screen itself cannot be
    read, the book is returned undecomposed with a warning saying so.
    """
    book = positions_core.read_positions(client, address)
    if not book.holdings:
        return book, ()
    if rows is None:
        try:
            rows = loop_rows(client)
        except Exception as error:  # noqa: BLE001 - reported, not guessed
            return book, (
                "levered positions were not decomposed: the loop screen could "
                f"not be read ({type(error).__name__}); a loop's collateral "
                "would be reported gross of its debt",
            )
    pairs = loop_pairs(rows)
    accounts: list[positions_core.PoolAccount] = []
    for chain_id, pool in positions_core.pool_legs(book, pairs):
        rpc_url = chains.rpc_url(chain_id, config)
        if rpc_url is None:
            raise PoolReadError(
                f"no RPC for chain {chain_id}, so pool {pool} cannot be read for "
                f"debt; set RPC_URL_{chain_id}"
            )
        accounts.append(
            read_pool_account(
                Web3(HTTPProvider(rpc_url)),
                chain_id=chain_id,
                pool=next(pair.pool for pair in pairs if pair.pool.casefold() == pool),
                account=address,
            )
        )
    return positions_core.decompose_levered(book, pairs=pairs, accounts=accounts), ()


def _pool_call(w3: Web3, pool: str, data: bytes, *, words: int) -> list[int]:
    try:
        raw = w3.eth.call(
            {"to": Web3.to_checksum_address(pool), "data": "0x" + data.hex()}
        )
    except Exception as error:
        # The type only: a provider error can quote its URL, and RPC URLs carry
        # API keys.
        raise PoolReadError(
            f"pool {pool} call failed ({type(error).__name__})"
        ) from None
    if len(raw) < 32 * words:
        raise PoolReadError(f"pool {pool} returned {len(raw)} bytes")
    return [
        int.from_bytes(raw[32 * index : 32 * (index + 1)], "big")
        for index in range(words)
    ]


@dataclass(frozen=True)
class _Expiring:
    instrument_id: str
    expires_at: int | None


__all__ = [
    "BUNDLE_ACTIONS",
    "LOOP_CALLDATA_ENDPOINT",
    "KillSwitch",
    "LoopAnnouncement",
    "LoopDivergenceError",
    "LoopLegAmount",
    "LoopPolicyError",
    "LoopSkipped",
    "LoopTarget",
    "PoolPosition",
    "PoolReadError",
    "announce",
    "check_measurement",
    "discover_loop_vaults",
    "equity_raw",
    "fetch_loop_calldata",
    "leverage_param",
    "loop_pairs",
    "loop_rows",
    "read_book",
    "read_pool_account",
    "loop_vaults",
    "plan_loop_bundle",
    "pool_positions",
    "refine_levered_vaults",
    "params_key",
    "refresh_loop_bundle",
    "save_params",
    "request_loop_bundle",
    "validate_loop_calldata",
]
