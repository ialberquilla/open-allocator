from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from math import sqrt

from pydantic import BaseModel

from open_allocator.core.types import Unknown, Vault

_MISSING = object()


def enrich(client: object, vaults: Iterable[Vault], days: int) -> list[Vault]:
    vault_list = list(vaults)
    if not vault_list:
        return []

    instrument_ids = tuple(vault.instrument_id for vault in vault_list)
    metrics_by_id = {
        str(_required(item, "instrument_id", "instrumentId")): item
        for item in _metric_items(client.metrics_bulk(instrument_ids, days))
    }

    enriched: list[Vault] = []
    for vault in vault_list:
        instrument_metrics = metrics_by_id.get(vault.instrument_id)
        metric_points = _metric_points(instrument_metrics)
        apy_series = _series(metric_points, "apy")
        tvl_series = _series(metric_points, "tvl_usd", "tvlUsd", "tvl")
        analysis = client.instrument_analysis(vault.instrument_id)

        updates: dict[str, object] = {
            "apy_series": apy_series,
            "tvl_usd_series": tvl_series,
            "apy_stability": _preserve_known(
                _coefficient_of_variation(apy_series), vault.apy_stability
            ),
            "reward_dependence": _preserve_known(
                _reward_dependence(analysis, metric_points), vault.reward_dependence
            ),
            "liquidity": _preserve_known(_liquidity(analysis), vault.liquidity),
            "curator": _preserve_known(
                _known_or_unknown(analysis, "curator"), vault.curator
            ),
            "oracle": _preserve_known(
                _known_or_unknown(analysis, "oracle"), vault.oracle
            ),
            "fee": _preserve_known(_known_or_unknown(analysis, "fee"), vault.fee),
            "market_concentration": _preserve_known(
                _known_or_unknown(
                    analysis,
                    "market_concentration",
                    "marketConcentration",
                ),
                vault.market_concentration,
            ),
            "collateral_mix": _preserve_known(
                _known_or_unknown(analysis, "collateral_mix", "collateralMix"),
                vault.collateral_mix,
            ),
        }

        tvl = _analysis_tvl(analysis)
        if tvl is _MISSING and tvl_series:
            tvl = tvl_series[-1]
        if tvl is not _MISSING:
            updates["tvl_usd"] = tvl

        enriched.append(vault.model_copy(update=updates))

    return enriched


def _metric_items(response: object) -> Sequence[object]:
    data = _value(response, "data")
    if data is not _MISSING:
        return _sequence(data)
    return _sequence(response)


def _metric_points(instrument_metrics: object) -> Sequence[object]:
    if instrument_metrics is None:
        return ()
    metrics = _value(instrument_metrics, "metrics")
    if metrics is _MISSING or metrics is None:
        return ()
    return _sequence(metrics)


def _sequence(value: object) -> Sequence[object]:
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return value
    raise TypeError("metrics_bulk() must return a sequence or an object with data")


def _series(points: Sequence[object], *names: str) -> tuple[float, ...]:
    values: list[float] = []
    for point in points:
        value = _number(_value(point, *names))
        if value is not None:
            values.append(value)
    return tuple(values)


def _coefficient_of_variation(series: Sequence[float]) -> float | object:
    if not series:
        return Unknown

    mean = sum(series) / len(series)
    if mean == 0:
        return Unknown

    variance = sum((value - mean) ** 2 for value in series) / len(series)
    return sqrt(variance) / abs(mean)


def _reward_dependence(analysis: object, points: Sequence[object]) -> float | object:
    yield_fields = _value(analysis, "yield_", "yield")
    if yield_fields is not _MISSING and yield_fields is not None:
        reward_share_pct = _number(
            _value(yield_fields, "reward_share_pct", "rewardSharePct")
        )
        if reward_share_pct is not None:
            return reward_share_pct / 100

        reward_share = _number(_value(yield_fields, "reward_share", "rewardShare"))
        if reward_share is not None:
            return reward_share

    shares: list[float] = []
    for point in points:
        reward = _number(_value(point, "apy_reward", "apyReward"))
        apy = _number(_value(point, "apy"))
        if reward is not None and apy not in (None, 0):
            shares.append(reward / apy)

    if not shares:
        return Unknown
    return sum(shares) / len(shares)


def _liquidity(analysis: object) -> float | object:
    liquidity = _value(analysis, "liquidity")
    if liquidity is _MISSING or liquidity is None:
        return Unknown

    low_liquidity = _value(liquidity, "low_liquidity", "lowLiquidity")
    if low_liquidity is not _MISSING and low_liquidity is not None:
        return 1.0 if bool(low_liquidity) else 0.0

    tvl = _number(_value(liquidity, "tvl_usd", "tvlUsd", "tvl"))
    if tvl is not None:
        return tvl
    return Unknown


def _analysis_tvl(analysis: object) -> float | object:
    liquidity = _value(analysis, "liquidity")
    if liquidity is not _MISSING and liquidity is not None:
        tvl = _number(_value(liquidity, "tvl_usd", "tvlUsd", "tvl"))
        if tvl is not None:
            return tvl
    return _MISSING


def _known_or_unknown(value: object, *names: str) -> object:
    found = _value(value, *names)
    if found is _MISSING or found is None:
        return Unknown
    return found


def _preserve_known(new_value: object, existing_value: object) -> object:
    if new_value == Unknown and existing_value != Unknown:
        return existing_value
    return new_value


def _required(value: object, *names: str) -> object:
    found = _value(value, *names)
    if found is _MISSING or found is None:
        joined_names = ", ".join(names)
        raise ValueError(f"metrics item is missing required field: {joined_names}")
    return found


def _number(value: object) -> float | None:
    if value is _MISSING or value is None:
        return None
    return float(value)


def _value(value: object, *names: str) -> object:
    for source in _sources(value):
        for name in names:
            found = _source_value(source, name)
            if found is not _MISSING:
                return found
    return _MISSING


def _source_value(source: object, name: str) -> object:
    if isinstance(source, Mapping):
        if name in source:
            return source[name]
    elif hasattr(source, name):
        return getattr(source, name)
    return _MISSING


def _sources(value: object) -> tuple[object, ...]:
    if isinstance(value, BaseModel):
        return (
            value,
            value.model_dump(by_alias=False),
            value.model_dump(by_alias=True),
        )
    if isinstance(value, Mapping):
        return (value,)
    return (value,)
