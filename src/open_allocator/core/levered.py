"""Levered-loop economics: ``f(L)``, the health factor, and the depeg budget.

A levered loop supplies collateral, borrows against it, swaps the borrowed
asset back to the collateral asset and re-supplies, ``n`` times. It turns a
supply rate ``rs`` and a borrow cost ``rb`` into

    f(L) = L*rs - (L-1)*rb  =  rs + (L-1)*(rs - rb)

🔑 **f(L) IS LINEAR IN L, AND THAT IS THE WHOLE SHAPE OF THE QUESTION.**
Leverage scales one per-turn gradient ``rs - rb`` and changes nothing else
about it. A loop whose gradient is negative is negative at every L; a loop
whose gradient is thin is not rescued by stacking turns, it is only levered
harder into the same thin number. So the only thing worth arguing about is the
gradient. **L is a risk budget, not an edge.**

Two things follow from the linearity, and both are load-bearing:

- The gradient is a **difference of differences** between two legs' rates, so a
  small move in either borrow rate moves it a lot. Utilisation moves borrow
  rates fast. Every number here is a snapshot.
- Because the gradient is small and the rates are large, the *reward* term can
  dominate it outright, to the point where a pair carries only while its reward
  programme does. That is why :class:`LeveredSpec` carries a
  ``reward_price_basis`` and why rewards are counted only when they were priced
  at a traded quote — see :func:`quote`.

This module is pure, offline and deterministic. It models a snapshot; it never
reads a chain, never picks L, and never decides whether a loop is allowed. It
models the health factor *before*; the venue's own simulation measures it
*after*, and a divergence between the two is a stop rather than a warning.

Rates are APY **in percent** (``4.0`` == 4%/yr), matching :attr:`Vault.apy` and
:mod:`open_allocator.core.riskmetrics`.

CAVEAT: this is rate arithmetic on a stored snapshot. It does not model our own
market impact (borrowing at size moves the rate we just quoted), it does not
model liquidation mechanics beyond the health factor, and it has no view on
whether anything will unwind the position when a floor breaks.
"""

from __future__ import annotations

import math
from typing import Literal

from pydantic import Field, model_validator

from open_allocator.core.types import FrozenModel, RewardPriceBasis

# Turns are bounded so a leverage that the LTV can only reach asymptotically
# cannot spin a loop forever. Well above anything policy should ever allow.
MAX_TURNS = 64

# Floating-point slack when asking "does this many turns reach that leverage?".
_TURN_EPSILON = 1e-12


def max_leverage(ltv: float) -> float:
    """The leverage ``ltv`` reaches with infinite turns: ``1 / (1 - ltv)``.

    A ceiling, never a target: it is reached only in the limit, and at it the
    health factor equals the liquidation threshold.
    """
    _require_fraction(ltv, "ltv")
    return 1.0 / (1.0 - ltv)


def leverage_for_turns(ltv: float, turns: int) -> float:
    """Leverage after ``turns`` borrow-and-resupply iterations.

    ``sum(ltv**k for k in range(turns + 1))`` — turn 0 is the initial supply,
    which is why ``turns=0`` is leverage 1.0 and not zero.
    """
    _require_fraction(ltv, "ltv")
    if turns < 0:
        raise ValueError(f"turns must be non-negative, got {turns}")
    if turns > MAX_TURNS:
        raise ValueError(f"turns must be at most {MAX_TURNS}, got {turns}")
    return sum(ltv**k for k in range(turns + 1))


def turns_for_leverage(ltv: float, leverage: float) -> int:
    """Fewest turns that reach ``leverage`` at ``ltv``.

    Raises when ``leverage`` is at or above :func:`max_leverage`, which no
    finite number of turns reaches.
    """
    _require_leverage(leverage)
    ceiling = max_leverage(ltv)
    if leverage > ceiling or math.isclose(leverage, ceiling, rel_tol=1e-9):
        raise ValueError(
            f"leverage {leverage} is not reachable at ltv {ltv}: "
            f"the ceiling is {ceiling:.6f}"
        )
    for turns in range(MAX_TURNS + 1):
        if leverage_for_turns(ltv, turns) >= leverage - _TURN_EPSILON:
            return turns
    raise ValueError(
        f"leverage {leverage} needs more than {MAX_TURNS} turns at ltv {ltv}"
    )


def levered_apy(
    *,
    supply_apy_pct: float,
    borrow_cost_pct: float,
    leverage: float,
) -> float:
    """``f(L) = L*rs - (L-1)*rb``."""
    _require_leverage(leverage)
    return leverage * supply_apy_pct - (leverage - 1.0) * borrow_cost_pct


def gradient_pct(*, supply_apy_pct: float, borrow_cost_pct: float) -> float:
    """The per-turn gradient ``rs - rb``: what one more turn of L adds."""
    return supply_apy_pct - borrow_cost_pct


def health_factor(
    *,
    leverage: float,
    liquidation_threshold: float,
    debt_price_ratio: float = 1.0,
) -> float | None:
    """``HF = (collateral * liqThreshold) / (debt * r)``.

    ``debt_price_ratio`` is an adverse move in the debt asset against the
    collateral asset (``1.0`` = today). ``None`` when there is no debt, which
    is not "infinitely safe" reported as a number — it is a position with no
    liquidation to have a factor about.
    """
    _require_leverage(leverage)
    _require_fraction(liquidation_threshold, "liquidation_threshold", allow_one=True)
    if debt_price_ratio <= 0:
        raise ValueError(f"debt_price_ratio must be positive, got {debt_price_ratio}")
    debt = leverage - 1.0
    if debt <= 0:
        return None
    return leverage * liquidation_threshold / (debt * debt_price_ratio)


def leverage_for_health_factor(
    *,
    target_health_factor: float,
    liquidation_threshold: float,
) -> float:
    """The largest L whose health factor is still ``target_health_factor``.

    Inverts :func:`health_factor` at ``debt_price_ratio=1``:
    ``L = HF / (HF - liqThreshold)``. HF falls monotonically in L from
    unbounded at L=1 toward ``liquidation_threshold`` in the limit, so a target
    at or below the threshold is met by *every* leverage — a floor that does
    not bind is a floor set wrong, and this raises rather than returning a
    ceiling it cannot justify.
    """
    _require_fraction(liquidation_threshold, "liquidation_threshold", allow_one=True)
    if target_health_factor <= liquidation_threshold:
        raise ValueError(
            f"target health factor {target_health_factor} is at or below the "
            f"liquidation threshold {liquidation_threshold}: it does not bind"
        )
    return target_health_factor / (target_health_factor - liquidation_threshold)


def depeg_buffer_bps(health_factor_value: float | None) -> int | None:
    """The adverse debt/collateral move a position survives, in bps.

    Liquidation happens at ``r = HF``, so the buffer is exactly ``HF - 1``.
    On a cross-asset loop this is not only a liquidation guard, it **is** the
    depeg budget, and at high leverage it is a few percent — smaller than moves
    stablecoins have actually made. ``None`` when there is no debt.
    """
    if health_factor_value is None:
        return None
    return int(round((health_factor_value - 1.0) * 10_000))


class LeveredSpec(FrozenModel):
    """One loopable pair, as it was snapshotted.

    Every rate here is a stored observation, never a live read at decision
    time, so a proposal built from it is reproducible. The pair is the thing
    that is held, so the spec is keyed on the pair — ``leverage`` is a
    parameter of a :func:`quote`, not a property of the pair, which is why one
    row serves every L.
    """

    loop_id: str = Field(min_length=1)
    chain_id: int = Field(ge=1)
    protocol: str = Field(min_length=1)
    collateral_instrument_id: str = Field(min_length=1)
    debt_instrument_id: str = Field(min_length=1)
    collateral_asset: str = Field(min_length=1)
    debt_asset: str = Field(min_length=1)

    # Collateral leg, supply side.
    supply_apy_base_pct: float
    supply_apy_reward_pct: float = 0.0
    # Debt leg, borrow side. ``borrow_apy_base_pct`` is what the debt costs;
    # a borrow-side reward is paid TO the borrower and therefore *reduces*
    # that cost. Keeping them separate is what lets rewards be switched off in
    # one place: excluding them raises the borrow cost and lowers the supply
    # yield at once, which is the conservative direction on both legs.
    borrow_apy_base_pct: float
    borrow_apy_reward_pct: float = 0.0

    # EFFECTIVE for this pair — an e-mode category, a per-collateral factor, a
    # market LLTV — never a pool default. ``params_basis`` names which, for the
    # same reason ``apy_basis`` exists: a decision must stay explainable from
    # visible inputs.
    ltv: float = Field(gt=0, lt=1)
    liquidation_threshold: float = Field(gt=0, le=1)
    params_basis: str = "unknown"
    oracle_source: str | None = None
    # ``setUserEMode`` is account-wide per pool, not per-position: opening this
    # loop re-prices every other position the account holds in the same pool.
    requires_account_config: bool = False

    reward_price_basis: RewardPriceBasis | None = None
    # Spot price of the reward token in the snapshot, if it had one. The kill
    # switch is derived from it.
    reward_price_usd: float | None = Field(default=None, gt=0)
    # 24h traded volume of the thinnest reward stream. ``None`` means
    # unmeasured, which is never zero and must never read as "pass".
    reward_liquidity_usd: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _threshold_is_not_below_the_borrow_ceiling(self) -> "LeveredSpec":
        if self.liquidation_threshold < self.ltv:
            raise ValueError(
                f"{self.loop_id}: liquidation threshold "
                f"{self.liquidation_threshold} is below ltv {self.ltv}, which "
                "would liquidate a position at the moment it was opened"
            )
        return self

    @property
    def same_asset(self) -> bool:
        """Whether both legs are the same asset.

        A cross-asset loop carries a second risk axis a same-asset one does
        not — the collateral/debt price ratio — and it is also the better loop,
        because the borrow-side rate asymmetry between two assets in one pool
        is usually worth more than any supply-side difference.
        """
        return self.collateral_asset.casefold() == self.debt_asset.casefold()

    @property
    def max_leverage(self) -> float:
        return max_leverage(self.ltv)

    @property
    def rewards_are_traded(self) -> bool:
        return self.reward_price_basis == "traded"

    @property
    def pays_rewards(self) -> bool:
        return bool(self.supply_apy_reward_pct or self.borrow_apy_reward_pct)


class LeveredQuote(FrozenModel):
    """``f(L)`` and its risk terms at one chosen leverage, on one snapshot."""

    loop_id: str
    leverage: float = Field(ge=1)
    # Borrow-and-resupply iterations needed to reach ``leverage``.
    turns: int = Field(ge=0)
    max_leverage: float
    same_asset: bool

    equity_usd: float = Field(gt=0)
    collateral_usd: float = Field(ge=0)
    debt_usd: float = Field(ge=0)
    # Notional crossed per direction: zero on a same-asset loop, ``(L-1) x
    # equity`` on a cross-asset one. Published so execution cost is charged on
    # the notional that actually trades rather than on the equity.
    swap_notional_usd: float = Field(ge=0)

    supply_apy_pct: float
    borrow_cost_pct: float
    # ``rs - rb``. The only number that decides whether the loop is worth doing.
    gradient_pct: float
    unlevered_apy_pct: float
    levered_apy_pct: float
    uplift_pct: float

    # ``None`` when there is no debt.
    health_factor: float | None = None
    # Cross-asset only: on a same-asset loop the health factor is an interest
    # -drift guard and there is no price ratio to budget for.
    depeg_buffer_bps: int | None = None

    # What ``levered_apy_pct`` counted.
    apy_basis: Literal["base", "base_plus_traded_reward"]
    rewards_counted: bool
    reward_price_basis: RewardPriceBasis | None = None
    reward_liquidity_usd: float | None = None
    # Reward value this position accrues per day, at the snapshot price.
    # ``None`` when rewards are not counted.
    reward_income_usd_per_day: float | None = None
    # Fraction of the snapshot reward price at which the gradient reaches zero,
    # and that fraction applied to the snapshot price. A price trigger, not a
    # judgement. ``0.0`` means the gradient survives a worthless reward token.
    kill_switch_reward_price_ratio: float | None = None
    kill_switch_reward_price_usd: float | None = None

    params_basis: str
    requires_account_config: bool

    def warnings(self) -> tuple[str, ...]:
        warnings: list[str] = []
        if not self.rewards_counted and self.reward_price_basis != "none":
            warnings.append(
                f"levered_reward_unpriced:basis={self.reward_price_basis or 'unknown'}:"
                "reward APY not priced at a traded quote, counted as zero"
            )
        if self.gradient_pct <= 0:
            warnings.append(
                f"levered_gradient_nonpositive:gradient_pct={self.gradient_pct:.4f}:"
                "leverage cannot make this positive at any L"
            )
        if not self.same_asset and self.depeg_buffer_bps is not None:
            warnings.append(
                f"levered_depeg_budget:buffer_bps={self.depeg_buffer_bps}:"
                "cross-asset — the health factor is the depeg budget, "
                "not only a liquidation guard"
            )
        if self.reward_liquidity_usd is None and self.rewards_counted:
            warnings.append(
                "levered_reward_liquidity_unknown:"
                "exit capacity unmeasured — treat as failing, never as passing"
            )
        return tuple(warnings)


def quote(
    spec: LeveredSpec,
    *,
    leverage: float | None = None,
    turns: int | None = None,
    equity_usd: float = 1.0,
    count_rewards: bool | None = None,
) -> LeveredQuote:
    """Price ``spec`` at one leverage, given as ``leverage`` or as ``turns``.

    ``count_rewards`` defaults to **whether the snapshot priced its rewards at
    a traded quote**. That default is the whole point: an emission-priced
    reward APY is the emission schedule's opinion of what the token is worth,
    and on a thin token it is not what the token sells for. Counting it floats
    reward-heavy loops to the top of a ranking. Passing ``True`` or ``False``
    explicitly overrides it — ``False`` answers "does this loop carry with the
    rewards switched off", which is a different and often much shorter
    question.
    """
    if (leverage is None) == (turns is None):
        raise ValueError("pass exactly one of leverage or turns")

    if turns is not None:
        resolved_leverage = leverage_for_turns(spec.ltv, turns)
        resolved_turns = turns
    else:
        resolved_leverage = float(leverage or 0.0)
        _require_leverage(resolved_leverage)
        resolved_turns = turns_for_leverage(spec.ltv, resolved_leverage)

    if equity_usd <= 0:
        raise ValueError(f"equity_usd must be positive, got {equity_usd}")

    rewards_counted = (
        spec.rewards_are_traded if count_rewards is None else count_rewards
    )
    supply_reward = spec.supply_apy_reward_pct if rewards_counted else 0.0
    borrow_reward = spec.borrow_apy_reward_pct if rewards_counted else 0.0
    supply = spec.supply_apy_base_pct + supply_reward
    borrow_cost = spec.borrow_apy_base_pct - borrow_reward

    collateral_usd = resolved_leverage * equity_usd
    debt_usd = (resolved_leverage - 1.0) * equity_usd
    hf = health_factor(
        leverage=resolved_leverage,
        liquidation_threshold=spec.liquidation_threshold,
    )
    ratio = zero_gradient_reward_ratio(spec) if rewards_counted else None
    levered = levered_apy(
        supply_apy_pct=supply,
        borrow_cost_pct=borrow_cost,
        leverage=resolved_leverage,
    )

    return LeveredQuote(
        loop_id=spec.loop_id,
        leverage=resolved_leverage,
        turns=resolved_turns,
        max_leverage=spec.max_leverage,
        same_asset=spec.same_asset,
        equity_usd=equity_usd,
        collateral_usd=collateral_usd,
        debt_usd=debt_usd,
        swap_notional_usd=0.0 if spec.same_asset else debt_usd,
        supply_apy_pct=supply,
        borrow_cost_pct=borrow_cost,
        gradient_pct=gradient_pct(
            supply_apy_pct=supply,
            borrow_cost_pct=borrow_cost,
        ),
        unlevered_apy_pct=supply,
        levered_apy_pct=levered,
        uplift_pct=levered - supply,
        health_factor=hf,
        depeg_buffer_bps=None if spec.same_asset else depeg_buffer_bps(hf),
        apy_basis="base_plus_traded_reward" if rewards_counted else "base",
        rewards_counted=rewards_counted,
        reward_price_basis=spec.reward_price_basis,
        reward_liquidity_usd=spec.reward_liquidity_usd,
        reward_income_usd_per_day=(
            (collateral_usd * supply_reward + debt_usd * borrow_reward) / 100.0 / 365.0
            if rewards_counted
            else None
        ),
        kill_switch_reward_price_ratio=ratio,
        kill_switch_reward_price_usd=(
            ratio * spec.reward_price_usd
            if ratio is not None and spec.reward_price_usd is not None
            else None
        ),
        params_basis=spec.params_basis,
        requires_account_config=spec.requires_account_config,
    )


def zero_gradient_reward_ratio(spec: LeveredSpec) -> float | None:
    """Fraction of the snapshot reward price at which the gradient hits zero.

    Reward APYs scale linearly with the reward token's price, so the gradient
    at a price ``k`` times the snapshot's is
    ``(rs_base - rb_base) + k * (rs_reward + rb_reward)`` and it crosses zero at
    ``k = (rb_base - rs_base) / (rs_reward + rb_reward)``. That is the kill
    switch: a price, not a judgement about whether the reward programme still
    looks healthy.

    ``None`` when the pair pays no reward — there is no price to trigger on.
    ``0.0`` when the base rates alone already carry the loop, which means no
    fall in the reward price can turn the gradient negative.
    """
    reward_slope = spec.supply_apy_reward_pct + spec.borrow_apy_reward_pct
    if reward_slope <= 0:
        return None
    base_gradient = spec.supply_apy_base_pct - spec.borrow_apy_base_pct
    if base_gradient >= 0:
        return 0.0
    return -base_gradient / reward_slope


def equity_cap_for_reward_liquidity(
    quoted: LeveredQuote,
    *,
    max_share_of_volume: float,
) -> float | None:
    """Largest equity whose daily reward income still exits inside ``max_share``.

    Capacity on a reward-driven loop binds on the **reward token's exit
    liquidity**, not on the lending pool's cash: the position accrues a token
    that has to be sold, and selling more of it per day than the market trades
    is not a fill, it is a price. Scales linearly with equity because the
    reward income does.

    ``None`` when the liquidity is unmeasured or no reward is counted — and
    ``None`` here means *unknown*, so a cap reading it must fail closed:
    treating a reward token's missing volume as "no limit" inverts the entire
    point of the field.
    """
    if max_share_of_volume <= 0:
        raise ValueError(
            f"max_share_of_volume must be positive, got {max_share_of_volume}"
        )
    income = quoted.reward_income_usd_per_day
    if income is None or income <= 0 or quoted.reward_liquidity_usd is None:
        return None
    sellable_per_day = quoted.reward_liquidity_usd * max_share_of_volume
    return quoted.equity_usd * sellable_per_day / income


# How far the venue's measurement may sit from the model before execution
# stops. Judgement, not calibration: both sides compute ``lt * L / (L - 1)`` at
# the pool oracle, so on a clean position they agree to rounding (the venue
# truncates its factor to six decimals). Anything wider is the model
# being wrong about something — another position in the pool, a leverage the
# planner overshot, a parameter that moved — and that is a stop, not a warning.
HEALTH_FACTOR_TOLERANCE = 0.001
LEVERAGE_TOLERANCE = 0.001  # relative


class SimulationCheck(FrozenModel):
    """OA's modelled health factor against the one the venue simulated.

    The model is ``health_factor(requested L, effective threshold)``; the
    measurement is what 1Tx's simulator read back from the pool after running
    the batch. ``divergences`` names every way they disagree; an empty tuple is
    the only passing result.
    """

    requested_leverage: float | None
    liquidation_threshold: float
    same_asset: bool
    modelled_health_factor: float | None
    modelled_depeg_buffer_bps: int | None
    measured_leverage: float | None
    measured_health_factor: float | None
    measured_depeg_buffer_bps: int | None
    divergences: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.divergences


def check_simulation(
    *,
    requested_leverage: float | None,
    liquidation_threshold: float,
    same_asset: bool,
    measured_leverage: float | None,
    measured_health_factor: float | None,
    measured_depeg_buffer_bps: int | None,
    health_factor_tolerance: float = HEALTH_FACTOR_TOLERANCE,
    leverage_tolerance: float = LEVERAGE_TOLERANCE,
) -> SimulationCheck:
    """Compare the model with the venue's measurement of the same bundle.

    ``requested_leverage`` of ``None`` is a full unwind: the model is then no
    debt, so no health factor and no leverage. Symmetric on purpose — a
    measured factor *above* the model is as much a sign that the model does not
    describe this account as one below it.
    """
    _require_fraction(liquidation_threshold, "liquidation_threshold", allow_one=True)
    if requested_leverage is None:
        modelled_hf = None
    else:
        modelled_hf = health_factor(
            leverage=requested_leverage,
            liquidation_threshold=liquidation_threshold,
        )
    modelled_buffer = None if same_asset else depeg_buffer_bps(modelled_hf)

    divergences: list[str] = []
    if modelled_hf is None:
        if measured_health_factor is not None:
            divergences.append(
                f"the model leaves no debt, the simulation a health factor of "
                f"{measured_health_factor}"
            )
    elif measured_health_factor is None:
        divergences.append(
            f"the model has a health factor of {modelled_hf:.6f}, the simulation "
            "reports none"
        )
    elif abs(measured_health_factor - modelled_hf) > health_factor_tolerance:
        divergences.append(
            f"simulated health factor {measured_health_factor} is "
            f"{measured_health_factor - modelled_hf:+.6f} from the modelled "
            f"{modelled_hf:.6f} (tolerance {health_factor_tolerance})"
        )

    if requested_leverage is None:
        if measured_leverage is not None:
            divergences.append(
                f"a full unwind simulated at leverage {measured_leverage}"
            )
    elif measured_leverage is None:
        divergences.append(
            f"leverage {requested_leverage} was requested, the simulation reports none"
        )
    elif (
        abs(measured_leverage - requested_leverage) / requested_leverage
        > leverage_tolerance
    ):
        divergences.append(
            f"simulated leverage {measured_leverage} is not the requested "
            f"{requested_leverage} (tolerance {leverage_tolerance:.2%})"
        )

    if same_asset:
        if measured_depeg_buffer_bps is not None:
            divergences.append(
                "a same-asset loop reported a depeg buffer of "
                f"{measured_depeg_buffer_bps} bps"
            )
    elif measured_health_factor is not None:
        expected = depeg_buffer_bps(measured_health_factor)
        if measured_depeg_buffer_bps is None or (
            expected is not None and abs(measured_depeg_buffer_bps - expected) > 1
        ):
            divergences.append(
                f"simulated depeg buffer {measured_depeg_buffer_bps} bps does not "
                f"follow from its own health factor ({expected} bps)"
            )

    return SimulationCheck(
        requested_leverage=requested_leverage,
        liquidation_threshold=liquidation_threshold,
        same_asset=same_asset,
        modelled_health_factor=modelled_hf,
        modelled_depeg_buffer_bps=modelled_buffer,
        measured_leverage=measured_leverage,
        measured_health_factor=measured_health_factor,
        measured_depeg_buffer_bps=measured_depeg_buffer_bps,
        divergences=tuple(divergences),
    )


def _require_leverage(leverage: float) -> None:
    if not math.isfinite(leverage) or leverage < 1.0:
        raise ValueError(f"leverage must be at least 1.0, got {leverage}")


def _require_fraction(value: float, name: str, *, allow_one: bool = False) -> None:
    upper_ok = value <= 1.0 if allow_one else value < 1.0
    if not (0.0 < value and upper_ok):
        bound = "1.0]" if allow_one else "1.0)"
        raise ValueError(f"{name} must be in (0.0, {bound}, got {value}")


__all__ = [
    "HEALTH_FACTOR_TOLERANCE",
    "LEVERAGE_TOLERANCE",
    "MAX_TURNS",
    "LeveredQuote",
    "LeveredSpec",
    "SimulationCheck",
    "check_simulation",
    "depeg_buffer_bps",
    "equity_cap_for_reward_liquidity",
    "gradient_pct",
    "health_factor",
    "leverage_for_health_factor",
    "leverage_for_turns",
    "levered_apy",
    "max_leverage",
    "quote",
    "turns_for_leverage",
    "zero_gradient_reward_ratio",
]
