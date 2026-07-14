"""Risk screening — advisory, metric-driven narrowing of the candidate universe.

Screening sits **above** the block-only policy layer. It can only *shrink* the
candidate set (never loosen anything), and policy still runs on whatever
survives. It composes with ``--exclude`` and pins.

An instrument is dropped when a set threshold cannot be *verified* from visible
data: a metric that is :data:`Unknown` fails a threshold rather than being
guessed past it. Every drop carries a machine-readable reason.

Screens are per-run judgment (or optional ``policy.screen`` defaults); they are
not policy and cannot substitute for it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from open_allocator.core import riskmetrics
from open_allocator.core.types import Unknown, Vault, curator_bucket


@dataclass(frozen=True)
class ScreenCriteria:
    min_sharpe: float | None = None
    max_drawdown: float | None = None  # max tolerated dip magnitude, e.g. 0.1 == 10%
    max_reward_dependence: float | None = None
    min_history_days: int | None = None
    curators: tuple[str, ...] | None = None
    min_tvl_usd: float | None = None

    @property
    def active(self) -> bool:
        return any(
            value is not None
            for value in (
                self.min_sharpe,
                self.max_drawdown,
                self.max_reward_dependence,
                self.min_history_days,
                self.curators,
                self.min_tvl_usd,
            )
        )


@dataclass(frozen=True)
class ScreenDrop:
    instrument_id: str
    rule: str
    detail: str


@dataclass(frozen=True)
class ScreenResult:
    kept: tuple[Vault, ...]
    dropped: tuple[ScreenDrop, ...]

    def warnings(self) -> list[str]:
        return [
            f"screen_excluded:{drop.instrument_id}:{drop.rule}"
            for drop in self.dropped
        ]


def screen(vaults: Sequence[Vault], criteria: ScreenCriteria) -> ScreenResult:
    kept: list[Vault] = []
    dropped: list[ScreenDrop] = []

    for vault in vaults:
        drop = _drop_reason(vault, criteria)
        if drop is None:
            kept.append(vault)
        else:
            dropped.append(drop)

    return ScreenResult(kept=tuple(kept), dropped=tuple(dropped))


def _drop_reason(vault: Vault, criteria: ScreenCriteria) -> ScreenDrop | None:
    if criteria.min_history_days is not None:
        days = len(vault.apy_series)
        if days < criteria.min_history_days:
            return _drop(
                vault, "min_history_days", f"{days}<{criteria.min_history_days}"
            )

    if criteria.min_sharpe is not None:
        sharpe = riskmetrics.sharpe(vault.apy_series)
        if sharpe == Unknown:
            return _drop(vault, "min_sharpe", "unknown")
        if float(sharpe) < criteria.min_sharpe:
            return _drop(
                vault, "min_sharpe", f"{float(sharpe):.4f}<{criteria.min_sharpe}"
            )

    if criteria.max_drawdown is not None:
        drawdown = riskmetrics.max_drawdown(riskmetrics.nav_curve(vault.apy_series))
        if drawdown == Unknown:
            return _drop(vault, "max_drawdown", "unknown")
        magnitude = abs(float(drawdown))
        if magnitude > criteria.max_drawdown:
            return _drop(
                vault, "max_drawdown", f"{magnitude:.4f}>{criteria.max_drawdown}"
            )

    if criteria.max_reward_dependence is not None:
        reward = _number(vault.reward_dependence)
        if reward is None:
            return _drop(vault, "max_reward_dependence", "unknown")
        if reward > criteria.max_reward_dependence:
            return _drop(
                vault,
                "max_reward_dependence",
                f"{reward:.4f}>{criteria.max_reward_dependence}",
            )

    if criteria.min_tvl_usd is not None and vault.tvl_usd < criteria.min_tvl_usd:
        return _drop(vault, "min_tvl_usd", f"{vault.tvl_usd}<{criteria.min_tvl_usd}")

    if criteria.curators is not None:
        if vault.curator == Unknown or vault.curator is None:
            return _drop(vault, "curators", "unknown")
        bucket = curator_bucket(vault.instrument_id, vault.curator)
        if bucket not in criteria.curators:
            return _drop(vault, "curators", str(vault.curator))

    return None


def _drop(vault: Vault, rule: str, detail: str) -> ScreenDrop:
    return ScreenDrop(instrument_id=vault.instrument_id, rule=rule, detail=detail)


def _number(value: object) -> float | None:
    if value == Unknown or value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


__all__ = ["ScreenCriteria", "ScreenDrop", "ScreenResult", "screen"]
