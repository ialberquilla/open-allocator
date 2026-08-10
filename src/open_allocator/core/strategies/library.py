from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from math import ceil
from typing import TYPE_CHECKING

from open_allocator.core import diversify, riskmetrics
from open_allocator.core.types import Unknown

if TYPE_CHECKING:
    from open_allocator.core.allocator import ScoredVault

_EPSILON = 1e-12

# Default volatility floor (in APY percent points) for risk_parity, so a
# zero/near-zero-vol series does not collapse to an unbounded weight.
_DEFAULT_VOL_FLOOR = 0.1

# Composite strategies may reference these as sleeve sub-strategies; recursion
# into other composites is rejected to keep dispatch finite and auditable.
_FLAT_NAMES = (
    "score_weighted",
    "equal_weight",
    "risk_parity",
    "inverse_vol",
    "decorrelated",
)

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
            max(0.0, record.vault.apy) / max_positive_apy if max_positive_apy else 0.0
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


def _decorrelated(
    records: Sequence[ScoredVault],
    ctx: StrategyContext,
) -> StrategyResult:
    """Score-weighted, then tilted away from positions that move together.

    Every other strategy here ranks instruments one at a time, so a shelf full
    of near-duplicates produces a book that scores well and holds one bet. This
    one prices each candidate against what is already in the book, using the
    measured co-movement in :mod:`open_allocator.core.diversify` rather than
    protocol/chain/sector labels — labels are what
    :data:`~open_allocator.core.types.PolicyCaps.min_effective_positions`
    exists to stop trusting.

    Two stages, both deterministic:

    - **Selection** (only when ``top_n`` is given): greedily take the candidate
      with the highest ``base_weight * (1 - strongest correlation to anything
      already selected)``, so the second copy of a position is worth much less
      than the first.
    - **Weighting**: divide each base weight by that instrument's *correlation
      load* — the summed positive correlation to the rest of the book,
      including itself. An instrument duplicating three others carries roughly
      a quarter of the weight its score alone would earn.

    Params: ``top_n`` (select this many, default = keep all and only re-weight),
    ``unknown_correlation`` (what an unmeasurable pair counts as, default 1.0 —
    fully correlated, matching the fail-closed rule in ``diversify`` and
    ``policy``).

    Degrades rather than blocking. Construction is advisory here; the binding
    check is the policy layer. With no history at all this reduces exactly to
    ``score_weighted`` plus a warning, instead of collapsing to a single leg.
    """
    top_n = _int_param(ctx.params, "top_n", None, minimum=1)
    unknown_correlation = _float_param(
        ctx.params, "unknown_correlation", 1.0, minimum=0.0, maximum=1.0
    )

    base, warnings = _score_weighted(records, ctx)
    base = _normalize(base)

    series_by_id = {
        record.vault.instrument_id: dict(record.vault.apy_daily)
        for record in records
        if record.vault.apy_daily
    }
    if not series_by_id:
        return base, [*warnings, "decorrelated:no_history:using_score_weights"]

    matrix = diversify.co_movement_matrix(series_by_id)
    unmeasured = len(records) - len(series_by_id)
    if unmeasured:
        warnings.append(f"decorrelated:unmeasured_instruments={unmeasured}")

    def correlation(left: int, right: int) -> float:
        if left == right:
            return 1.0
        key = tuple(
            sorted(
                (
                    records[left].vault.instrument_id,
                    records[right].vault.instrument_id,
                )
            )
        )
        pair = matrix.get(key)
        if pair is None or pair.correlation is None:
            return unknown_correlation
        return pair.correlation

    candidates = [index for index, weight in enumerate(base) if weight > _EPSILON]
    if not candidates:
        return base, warnings

    selected = _select_decorrelated(records, base, candidates, correlation, top_n)
    if top_n is not None:
        warnings.append(f"decorrelated:top_n={top_n}:kept={len(selected)}")

    weights = [0.0 for _ in records]
    for index in selected:
        # Positive correlation only: a negatively correlated pair genuinely
        # hedges, and letting it *reduce* the load would pay an instrument
        # twice for the same diversification the selection stage already
        # rewarded. Self-correlation keeps the load at >= 1.
        load = sum(max(correlation(index, other), 0.0) for other in selected)
        weights[index] = base[index] / load if load > _EPSILON else base[index]

    return weights, warnings


def _select_decorrelated(
    records: Sequence[ScoredVault],
    base: Sequence[float],
    candidates: Sequence[int],
    correlation: Callable[[int, int], float],
    top_n: int | None,
) -> list[int]:
    """Greedy pick by base weight discounted by redundancy against the book."""
    if top_n is None or top_n >= len(candidates):
        return list(candidates)

    remaining = list(candidates)
    seed = min(remaining, key=lambda i: (-base[i], records[i].vault.instrument_id))
    selected = [seed]
    remaining.remove(seed)

    while len(selected) < top_n and remaining:
        merits = {
            index: base[index]
            * (1.0 - max(correlation(index, other) for other in selected))
            for index in remaining
        }
        best = min(
            remaining,
            key=lambda i: (-merits[i], -base[i], records[i].vault.instrument_id),
        )
        # Everything left duplicates the book (or is unmeasurable and charged
        # as such). Diversification has nothing more to say, so fall back to
        # base preference rather than picking arbitrarily.
        if merits[best] <= _EPSILON:
            best = min(
                remaining,
                key=lambda i: (-base[i], records[i].vault.instrument_id),
            )
        selected.append(best)
        remaining.remove(best)

    return selected


# --- composite strategies --------------------------------------------------


@dataclass(frozen=True)
class _Bucket:
    name: str
    indices: tuple[int, ...]
    target: float
    strategy: str
    # Minimum instruments required before the bucket may hold its target. A
    # bucket that cannot meet it is dropped whole rather than held small: a
    # sleeve too thin to survive one failed instrument is not made safe by
    # shrinking it, so partial exposure at partial size is not on offer.
    min_positions: int = 0
    # Ordering key for redistribution. Higher is safer; a dropped bucket's
    # target may only move to buckets strictly above it.
    safety: float = 0.0


def _core_satellite(
    records: Sequence[ScoredVault],
    ctx: StrategyContext,
) -> StrategyResult:
    count = len(records)
    core_weight = _float_param(ctx.params, "core_weight", 0.8, minimum=0.0, maximum=1.0)
    core_selector = _flat_param(ctx.params, "core_selector", "score_weighted")
    satellite_selector = _flat_param(ctx.params, "satellite_selector", "score_weighted")
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
        _Bucket("core", core_indices, core_weight, core_selector, safety=1.0),
        _Bucket(
            "satellite",
            satellite_indices,
            1.0 - core_weight,
            satellite_selector,
            safety=0.0,
        ),
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
            min_positions=int(spec.get("min_positions", 0)),
            # A tier's floor score is its safety rank: redistribution may only
            # move weight to a band that demands a *higher* score than the one
            # being dropped.
            safety=float(spec["min_score"]),
        )
        for tier_index, spec in enumerate(tier_specs)
    )
    return _allocate_buckets(records, ctx, buckets)


def _allocate_buckets(
    records: Sequence[ScoredVault],
    ctx: StrategyContext,
    buckets: Sequence[_Bucket],
) -> StrategyResult:
    """Allocate across buckets, redistributing what an unfillable one gives up.

    A bucket must hold at least ``min_positions`` instruments (and always at
    least one) to be funded. One that cannot is dropped whole and its target is
    reassigned **upward only** — to funded buckets with a strictly higher
    ``safety``. Redistributing proportionally across all survivors would push
    weight *down* the ladder whenever the safest band is the one that came up
    short, so a shortage of safe instruments would silently buy more risk.
    """
    weights = [0.0 for _ in records]
    warnings: list[str] = []

    active: list[_Bucket] = []
    dropped: list[_Bucket] = []
    for bucket in buckets:
        if bucket.target <= _EPSILON:
            continue
        if len(bucket.indices) >= max(bucket.min_positions, 1):
            active.append(bucket)
        else:
            dropped.append(bucket)

    for bucket in dropped:
        if bucket.indices:
            warnings.append(
                f"sleeve_underfilled:{bucket.name}:"
                f"{len(bucket.indices)}/{bucket.min_positions}:weight_redistributed"
            )
        else:
            warnings.append(f"sleeve_empty:{bucket.name}:weight_redistributed")

    if not active:
        equal = 1 / len(records)
        warnings.append("sleeves:no_populated_tiers:using_equal_weights")
        return [equal for _ in records], warnings

    # Absorbed shares are computed against the absorbers' *original* targets, so
    # the outcome does not depend on the order the dropped buckets are visited.
    adjusted = [bucket.target for bucket in active]
    for bucket in dropped:
        absorbers = [
            position
            for position, candidate in enumerate(active)
            if candidate.safety > bucket.safety
        ]
        if not absorbers:
            # The safest funded band is the one that came up short. There is
            # nowhere up to go, so this falls back to spreading across what is
            # left — which does raise the book's risk, and says so.
            absorbers = list(range(len(active)))
            warnings.append(
                f"sleeve_no_safer_tier:{bucket.name}:weight_redistributed_down"
            )
        basis = sum(active[position].target for position in absorbers)
        for position in absorbers:
            adjusted[position] += bucket.target * active[position].target / basis

    active_target = sum(adjusted)
    for position, bucket in enumerate(active):
        subset = [records[index] for index in bucket.indices]
        sub_fn = _flat_strategy(bucket.strategy)
        sub_raw, sub_warnings = sub_fn(subset, ctx)
        sub_weights = _normalize(sub_raw)
        share = adjusted[position] / active_target
        for offset, index in enumerate(bucket.indices):
            weights[index] = share * sub_weights[offset]
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
        if "min_positions" in tier:
            _int_param(tier, "min_positions", None, minimum=0)
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
    "decorrelated": _decorrelated,
    "core_satellite": _core_satellite,
    "sleeves": _sleeves,
    "ladder": _sleeves,
}
