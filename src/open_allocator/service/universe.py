"""The shelf: discovering, scoring and screening what can be allocated to."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Literal

from open_allocator.core import apy_accounting, fixed_rate, universe
from open_allocator.core import riskmetrics as riskmetrics_core
from open_allocator.core import screen as screen_core
from open_allocator.core.metrics import enrich as enrich_vaults
from open_allocator.core.schema import validate
from open_allocator.core.scoring import score_vault as score_vault_model
from open_allocator.core.state import json_safe
from open_allocator.core.types import Unknown, Vault, VaultScore
from open_allocator.exec.client import OneTxClient
from open_allocator.exec.config import ReadOnlyOneTxConfig
from open_allocator.service._common import JsonObject
from open_allocator.service.errors import ServiceError

# History window for every command that fetches metrics.
#
# Must exceed `diversify.MIN_OVERLAP` (60), the *shared* days a pair needs
# before it is scored at all: below that every pair is unmeasured, unmeasured
# fails closed to "one bet", and `caps.min_effective_positions` becomes
# impossible to satisfy rather than merely strict. Single-instrument metrics
# (coefficient of variation, reward dependence) read the same window.
HISTORY_DAYS = 180

VaultSort = Literal["apy", "tvl", "score"]

# Receives a structured warning such as
# `{"warning": "skipped_instruments", "instruments": [...]}`.
OnWarning = Callable[[JsonObject], None]


def discover_vaults(
    *,
    enrich: bool = False,
    on_warning: OnWarning | None = None,
) -> list[Vault]:
    with OneTxClient(ReadOnlyOneTxConfig()) as client:
        return discover_vaults_from_client(client, enrich=enrich, on_warning=on_warning)


def discover_vaults_from_client(
    client: object,
    *,
    enrich: bool = False,
    loops: bool = False,
    on_warning: OnWarning | None = None,
) -> list[Vault]:
    """The discovered universe; with ``loops``, plus every loopable pair.

    Loop rows are levered synthetic instruments keyed by their loop id. They
    are added for execution, where an allocation may name one, and are not yet
    on the scored shelf.

    A shrunk universe still has to be visible — an instrument silently missing
    looks exactly like one that never existed — so every skip is reported
    through ``on_warning``.
    """
    warn = on_warning or _ignore
    vaults, skipped = universe.discover_instruments(client)
    if skipped:
        warn(
            {
                "warning": "skipped_instruments",
                "instruments": [s.model_dump() for s in skipped],
            }
        )
    if enrich:
        vaults = enrich_vaults(client, vaults, days=HISTORY_DAYS)
    if loops:
        from open_allocator.exec import loops as loops_exec

        levered, skipped_loops = loops_exec.discover_loop_vaults(client, vaults)
        if skipped_loops:
            warn(
                {
                    "warning": "skipped_loops",
                    "loops": [s.model_dump() for s in skipped_loops],
                }
            )
        vaults = [*vaults, *levered]
    return vaults


def filter_vaults(
    vaults: list[Vault],
    *,
    chain: int | None,
    asset: str | None,
    protocol: str | None,
) -> list[Vault]:
    return [
        vault
        for vault in vaults
        if (chain is None or vault.chain_id == chain)
        and (asset is None or vault.asset.casefold() == asset.casefold())
        and (protocol is None or vault.protocol.casefold() == protocol.casefold())
    ]


def score_by_instrument(vaults: list[Vault]) -> dict[str, VaultScore]:
    return {vault.instrument_id: score_vault_model(vault) for vault in vaults}


def list_vaults(
    *,
    chain: int | None = None,
    asset: str | None = None,
    protocol: str | None = None,
    sort: VaultSort | None = None,
    on_warning: OnWarning | None = None,
) -> list[JsonObject]:
    vaults = filter_vaults(
        discover_vaults(enrich=True, on_warning=on_warning),
        chain=chain,
        asset=asset,
        protocol=protocol,
    )
    scores = score_by_instrument(vaults)

    if sort == "apy":
        vaults.sort(key=lambda vault: vault.apy, reverse=True)
    elif sort == "tvl":
        vaults.sort(key=lambda vault: vault.tvl_usd, reverse=True)
    elif sort == "score":
        vaults.sort(key=lambda vault: scores[vault.instrument_id].score, reverse=True)

    return [vault_summary(vault, scores[vault.instrument_id]) for vault in vaults]


def score_vault(
    instrument_id: str,
    *,
    on_warning: OnWarning | None = None,
) -> JsonObject:
    for vault in discover_vaults(enrich=True, on_warning=on_warning):
        if vault.instrument_id == instrument_id:
            payload = score_vault_model(vault).model_dump(mode="json")
            payload["risk_metrics"] = risk_metrics(vault)
            validate(payload, "vault-score")
            return payload

    raise ServiceError("not_found", f"instrument not found: {instrument_id}")


def screen_criteria(
    *,
    min_sharpe: float | None = None,
    max_drawdown: float | None = None,
    max_reward_dependence: float | None = None,
    min_history_days: int | None = None,
    curators: Sequence[str] | None = None,
    min_tvl_usd: float | None = None,
) -> screen_core.ScreenCriteria:
    return screen_core.ScreenCriteria(
        min_sharpe=min_sharpe,
        max_drawdown=max_drawdown,
        max_reward_dependence=max_reward_dependence,
        min_history_days=min_history_days,
        curators=tuple(curators) if curators else None,
        min_tvl_usd=min_tvl_usd,
    )


def screen(
    criteria: screen_core.ScreenCriteria,
    *,
    on_warning: OnWarning | None = None,
) -> JsonObject:
    """Advisory metric screen over the live universe.

    Narrows only; policy (``check-policy``) still applies downstream and cannot
    be loosened by any screen.
    """
    discovered = discover_vaults(enrich=True, on_warning=on_warning)
    scores = score_by_instrument(discovered)
    result = screen_core.screen(discovered, criteria)
    return {
        "label": "advisory-not-policy",
        "criteria": {
            "min_sharpe": criteria.min_sharpe,
            "max_drawdown": criteria.max_drawdown,
            "max_reward_dependence": criteria.max_reward_dependence,
            "min_history_days": criteria.min_history_days,
            "curators": list(criteria.curators) if criteria.curators else None,
            "min_tvl_usd": criteria.min_tvl_usd,
        },
        "kept": [
            vault_summary(vault, scores[vault.instrument_id]) for vault in result.kept
        ],
        "dropped": [
            {
                "instrument_id": drop.instrument_id,
                "rule": drop.rule,
                "detail": drop.detail,
            }
            for drop in result.dropped
        ],
    }


def vault_summary(vault: Vault, score: VaultScore) -> JsonObject:
    # `reward_apy` stays the advertised number — APY is descriptive here, and
    # hiding what upstream said would make the row harder to check, not safer.
    # `priced_reward_apy` is the one this allocator is willing to count, and it
    # reads Unknown for a reward APY priced at the emission schedule rather
    # than at a quote something would fill.
    priced_reward = apy_accounting.priced_reward_apy(vault)
    return {
        "instrument_id": vault.instrument_id,
        "protocol": vault.protocol,
        "chain_id": vault.chain_id,
        "asset": vault.asset,
        "apy": vault.apy,
        "advertised_apy": vault.apy,
        "base_apy": vault.apy_base,
        "reward_apy": vault.apy_reward,
        "priced_reward_apy": (
            priced_reward if priced_reward is not None else json_safe(Unknown)
        ),
        "reward_apy_basis": vault.reward_price_basis or "unknown",
        "reward_tokens": list(vault.reward_tokens),
        "reward_dependence": json_safe(vault.reward_dependence),
        "tvl_usd": vault.tvl_usd,
        # A levered row's usd value is its equity; its gross exposure is up to
        # max_leverage times that, which no weight cap can see.
        "levered": vault.is_levered,
        "max_leverage": vault.max_leverage,
        # A fixed-term row's apy is the rate locked to maturity, and its risk
        # metrics are the holder's mark-to-market path (core.fixed_rate).
        "maturity": vault.maturity.isoformat() if vault.maturity else None,
        "days_to_maturity": fixed_rate.days_to_maturity(vault),
        "term_return_pct": vault.term_return_pct,
        "score": score.score,
        "risk_metrics": risk_metrics(vault),
    }


def risk_metrics(vault: Vault) -> JsonObject:
    # Yield-path risk only; never principal/depeg/contract loss. Unknown stays
    # Unknown when history is insufficient.
    return {
        name: json_safe(value)
        for name, value in riskmetrics_core.summary(vault).items()
    }


def _ignore(_warning: JsonObject) -> None:
    pass
