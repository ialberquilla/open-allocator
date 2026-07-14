from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from math import ceil
from typing import TYPE_CHECKING

from open_allocator.core import riskmetrics
from open_allocator.core.types import Unknown

if TYPE_CHECKING:
    from open_allocator.core.allocator import ScoredVault

_EPSILON = 1e-12

# Default volatility floor (in APY percent points) for risk_parity, so a
# zero/near-zero-vol series does not collapse to an unbounded weight.
_DEFAULT_VOL_FLOOR = 0.1

# Composite strategies may reference these as sleeve sub-strategies; recursion
# into other composites is rejected to keep dispatch finite and auditable.
_FLAT_NAMES = ("score_weighted", "equal_weight", "risk_parity", "inverse_vol")

# Default score-tier ladder for `sleeves`/`ladder`: (name, min_score, max_score,
# target_weight). Ranges are [min, max); the top tier's max is > 1 to include 1.
_DEFAULT_TIERS: tuple[dict[str, object], ...] = (
    {"name": "safe", "min_score": 0.6, "max_score": 1.01, "weight": 0.5},
    {"name": "med", "min_score": 0.3, "max_score": 0.6, "weight": 0.3},
    {"name": "risky", "min_score": 0.0, "max_score": 0.3, "weight": 0.2},
)


class StrategyError(ValueError):
    """Raised for an unknown strategy name or invalid strategy params."""


@dataclass(frozen=True)
class StrategyContext:
    score_power: float = 1.0
    apy_weight: float = 0.0
    params: Mapping[str, object] = field(default_factory=dict)


StrategyResult = tuple[list[float], list[str]]
StrategyFn = Callable[[Sequence["ScoredVault"], StrategyContext], StrategyResult]


def available() -> tuple[str, ...]:
    return tuple(sorted(STRATEGIES))


def desired_weights(
    strategy: str,
    records: Sequence[ScoredVault],
    *,
    score_power: float = 1.0,
    apy_weight: float = 0.0,
    params: Mapping[str, object] | None = None,
) -> StrategyResult:
    """Return ``(normalized_weights, warnings)`` for ``records`` under a strategy.

    Weights are aligned to ``records`` order and sum to 1 (or are all-zero only
    when ``records`` is empty).
    """
    fn = STRATEGIES.get(strategy)
    if fn is None:
        raise StrategyError(
            f"unknown strategy {strategy!r}; expected one of: {', '.join(available())}"
        )
    if not records:
        return [], []
    context = StrategyContext(
        score_power=score_power,
        apy_weight=apy_weight,
        params=dict(params or {}),
    )
    raw, warnings = fn(records, context)
    return _normalize(raw), warnings


# --- flat strategies -------------------------------------------------------


def _score_weighted(
    records: Sequence[ScoredVault],
    ctx: StrategyContext,
) -> StrategyResult:
    max_positive_apy = max(
        (max(0.0, record.vault.apy) for record in records),
        default=0.0,
    )
    raw: list[float] = []
    for record in records:
        score_component = max(0.0, record.score.score) ** ctx.score_power
        apy_component = (
            max(0.0, record.vault.apy) / max_positive_apy
            if max_positive_apy
            else 0.0
        )
        raw.append(score_component * (1 + ctx.apy_weight * apy_component))

    if sum(raw) <= _EPSILON:
        equal = 1 / len(records)
        return [equal for _ in records], ["all_scores_zero:using_equal_weights"]
    return raw, []


def _equal_weight(
    records: Sequence[ScoredVault],
    ctx: StrategyContext,
) -> StrategyResult:
    top_n = _int_param(ctx.params, "top_n", None, minimum=1)
    count = len(records)
    if top_n is None or top_n >= count:
        return [1.0 for _ in records], []

    order = sorted(
        range(count),
        key=lambda index: (
            -records[index].score.score,
            records[index].vault.instrument_id,
        ),
    )[:top_n]
    keep = set(order)
    weights = [1.0 if index in keep else 0.0 for index in range(count)]
    return weights, [f"equal_weight:top_n={top_n}:kept={len(keep)}"]


def _risk_parity(
    records: Sequence[ScoredVault],
    ctx: StrategyContext,
) -> StrategyResult:
    floor = _float_param(ctx.params, "vol_floor", _DEFAULT_VOL_FLOOR, minimum=_EPSILON)
    weights = [0.0 for _ in records]
    unknown = 0
    for index, record in enumerate(records):
        volatility = riskmetrics.stddev(record.vault.apy_series)
        if volatility == Unknown:
            unknown += 1
            continue
        weights[index] = 1.0 / max(float(volatility), floor)

    if sum(weights) <= _EPSILON:
        equal = 1 / len(records)
        return (
            [equal for _ in records],
            ["risk_parity:no_volatility_history:using_equal_weights"],
        )
    warnings: list[str] = []
    if unknown:
        warnings.append(f"risk_parity:excluded_unknown_vol={unknown}")
    return weights, warnings


# --- composite strategies --------------------------------------------------


@dataclass(frozen=True)
class _Bucket:
    name: str
    indices: tuple[int, ...]
    target: float
    strategy: str


def _core_satellite(
    records: Sequence[ScoredVault],
    ctx: StrategyContext,
) -> StrategyResult:
    count = len(records)
    core_weight = _float_param(
        ctx.params, "core_weight", 0.8, minimum=0.0, maximum=1.0
    )
    core_selector = _flat_param(ctx.params, "core_selector", "score_weighted")
    satellite_selector = _flat_param(
        ctx.params, "satellite_selector", "score_weighted"
    )
    default_core_count = ceil(count / 2)
    core_count = _int_param(
        ctx.params, "core_count", default_core_count, minimum=0, maximum=count
    )

    order = sorted(
        range(count),
        key=lambda index: (
            -records[index].score.score,
            records[index].vault.instrument_id,
        ),
    )
    core_indices = tuple(sorted(order[:core_count]))
    satellite_indices = tuple(sorted(order[core_count:]))
    buckets = (
        _Bucket("core", core_indices, core_weight, core_selector),
        _Bucket("satellite", satellite_indices, 1.0 - core_weight, satellite_selector),
    )
    return _allocate_buckets(records, ctx, buckets)


def _sleeves(
    records: Sequence[ScoredVault],
    ctx: StrategyContext,
) -> StrategyResult:
    tier_specs = _tier_specs(ctx.params)
    assigned: list[list[int]] = [[] for _ in tier_specs]
    for index, record in enumerate(records):
        tier_index = _tier_for_score(record.score.score, tier_specs)
        assigned[tier_index].append(index)

    buckets = tuple(
        _Bucket(
            name=str(spec["name"]),
            indices=tuple(assigned[tier_index]),
            target=float(spec["weight"]),
            strategy=str(spec.get("strategy", "score_weighted")),
        )
        for tier_index, spec in enumerate(tier_specs)
    )
    return _allocate_buckets(records, ctx, buckets)


def _allocate_buckets(
    records: Sequence[ScoredVault],
    ctx: StrategyContext,
    buckets: Sequence[_Bucket],
) -> StrategyResult:
    weights = [0.0 for _ in records]
    warnings: list[str] = []

    active = [
        bucket
        for bucket in buckets
        if bucket.indices and bucket.target > _EPSILON
    ]
    for bucket in buckets:
        if bucket.target > _EPSILON and not bucket.indices:
            warnings.append(f"sleeve_empty:{bucket.name}:weight_redistributed")

    active_target = sum(bucket.target for bucket in active)
    if active_target <= _EPSILON:
        equal = 1 / len(records)
        warnings.append("sleeves:no_populated_tiers:using_equal_weights")
        return [equal for _ in records], warnings

    for bucket in active:
        subset = [records[index] for index in bucket.indices]
        sub_fn = _flat_strategy(bucket.strategy)
        sub_raw, sub_warnings = sub_fn(subset, ctx)
        sub_weights = _normalize(sub_raw)
        share = bucket.target / active_target
        for position, index in enumerate(bucket.indices):
            weights[index] = share * sub_weights[position]
        warnings.extend(f"{bucket.name}:{warning}" for warning in sub_warnings)

    return weights, warnings


# --- params helpers --------------------------------------------------------


def _tier_specs(params: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
    raw = params.get("tiers")
    if raw is None:
        return _DEFAULT_TIERS
    if not isinstance(raw, Sequence) or isinstance(raw, str | bytes):
        raise StrategyError("sleeves 'tiers' must be a list of tier objects")
    specs: list[Mapping[str, object]] = []
    for tier in raw:
        if not isinstance(tier, Mapping):
            raise StrategyError("each sleeves tier must be an object")
        for key in ("name", "min_score", "max_score", "weight"):
            if key not in tier:
                raise StrategyError(f"sleeves tier missing required key: {key}")
        if "strategy" in tier:
            _flat_strategy(str(tier["strategy"]))
        specs.append(tier)
    if not specs:
        raise StrategyError("sleeves 'tiers' must not be empty")
    return tuple(specs)


def _tier_for_score(
    score: float,
    tiers: Sequence[Mapping[str, object]],
) -> int:
    for index, tier in enumerate(tiers):
        low = float(tier["min_score"])
        high = float(tier["max_score"])
        if low <= score < high:
            return index
    # Fall back to the tier with the lowest min_score (catches score == 1.0 when
    # a custom top tier used an inclusive-looking max of exactly 1.0).
    return min(
        range(len(tiers)),
        key=lambda index: float(tiers[index]["min_score"]),
    )


def _flat_strategy(name: str) -> StrategyFn:
    if name not in _FLAT_NAMES:
        raise StrategyError(
            f"sleeve sub-strategy must be one of {_FLAT_NAMES}, got: {name!r}"
        )
    return STRATEGIES[name]


def _flat_param(params: Mapping[str, object], key: str, default: str) -> str:
    value = params.get(key, default)
    name = str(value)
    _flat_strategy(name)
    return name


def _int_param(
    params: Mapping[str, object],
    key: str,
    default: int | None,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int | None:
    if key not in params:
        return default
    raw = params[key]
    if isinstance(raw, bool) or not isinstance(raw, int | float) or int(raw) != raw:
        raise StrategyError(f"strategy param {key!r} must be an integer")
    value = int(raw)
    if minimum is not None and value < minimum:
        raise StrategyError(f"strategy param {key!r} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise StrategyError(f"strategy param {key!r} must be <= {maximum}")
    return value


def _float_param(
    params: Mapping[str, object],
    key: str,
    default: float,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    if key not in params:
        return default
    raw = params[key]
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        raise StrategyError(f"strategy param {key!r} must be a number")
    value = float(raw)
    if minimum is not None and value < minimum:
        raise StrategyError(f"strategy param {key!r} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise StrategyError(f"strategy param {key!r} must be <= {maximum}")
    return value


def _normalize(weights: Sequence[float]) -> list[float]:
    if not weights:
        return []
    clipped = [max(0.0, weight) for weight in weights]
    total = sum(clipped)
    if total <= _EPSILON:
        equal = 1 / len(clipped)
        return [equal for _ in clipped]
    return [weight / total for weight in clipped]


STRATEGIES: dict[str, StrategyFn] = {
    "score_weighted": _score_weighted,
    "equal_weight": _equal_weight,
    "risk_parity": _risk_parity,
    "inverse_vol": _risk_parity,
    "core_satellite": _core_satellite,
    "sleeves": _sleeves,
    "ladder": _sleeves,
}
