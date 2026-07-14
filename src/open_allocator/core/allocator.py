from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from math import floor, isfinite
from typing import Any, Literal

from pydantic import model_validator

from open_allocator.core import strategies
from open_allocator.core.schema import validate
from open_allocator.core.types import (
    Allocation,
    AllocationLeg,
    FrozenModel,
    Vault,
    VaultScore,
    curator_bucket,
)

RiskPresetName = Literal["conservative", "balanced", "aggressive"]

DEFAULT_STRATEGY = "score_weighted"

_EPSILON = 1e-12


class ScoredVault(FrozenModel):
    score: VaultScore
    vault: Vault

    @model_validator(mode="after")
    def _score_matches_vault(self) -> "ScoredVault":
        if self.score.instrument_id != self.vault.instrument_id:
            raise ValueError("score instrument_id must match vault instrument_id")
        return self


@dataclass(frozen=True)
class _RiskPreset:
    score_power: float
    apy_weight: float


@dataclass(frozen=True)
class _Caps:
    max_weight_per_instrument: float = 1.0
    max_weight_per_protocol: float = 1.0
    max_weight_per_curator: float = 1.0
    max_weight_per_chain: float = 1.0


@dataclass(frozen=True)
class _Solution:
    legs: tuple[AllocationLeg, ...]
    warnings: tuple[str, ...]


# Risk presets are deterministic and deliberately simple:
# - conservative: score^3, no APY boost; strongest tilt toward safer scores.
# - balanced: score^2 with a small APY boost; risk remains dominant.
# - aggressive: score with a larger APY boost; high descriptive APY can change rank.
RISK_PRESETS: Mapping[RiskPresetName, _RiskPreset] = {
    "conservative": _RiskPreset(score_power=3.0, apy_weight=0.0),
    "balanced": _RiskPreset(score_power=2.0, apy_weight=0.5),
    "aggressive": _RiskPreset(score_power=1.0, apy_weight=2.0),
}


def build_allocation(
    scored_vaults: Iterable[
        ScoredVault | tuple[VaultScore, Vault] | tuple[Vault, VaultScore]
    ],
    amount_usd: float,
    risk: RiskPresetName = "balanced",
    caps: Mapping[str, object] | object | None = None,
    *,
    strategy: str = DEFAULT_STRATEGY,
    strategy_params: Mapping[str, object] | None = None,
    max_positions: int | None = None,
    min_position_usd: float | None = None,
    overrides: Mapping[str, float] | None = None,
    exclude: Iterable[str] | None = None,
    score_power: float | None = None,
    apy_weight: float | None = None,
) -> Allocation:
    """Build a policy-shaped allocation proposal.

    The default ``score_weighted`` strategy is a deterministic function of
    scores, APY, the risk preset, and caps. A named ``strategy`` (see
    :mod:`open_allocator.core.strategies`) swaps in a different, still
    deterministic, desired-weight vector; every strategy output flows through
    the same caps waterfall and ``check-policy``. Optional knobs:

    - ``strategy`` / ``strategy_params``: pick and parameterize a vetted
      strategy (``score_weighted``, ``equal_weight``, ``risk_parity``,
      ``core_satellite``, ``sleeves``/``ladder``, …).
    - ``max_positions`` / ``min_position_usd``: concentrate into the best few
      and drop dust legs (applied after the strategy produces desired weights).
    - ``score_power`` / ``apy_weight``: tilt the ``score_weighted`` formula
      beyond the three presets.
    - ``exclude``: veto specific instruments before allocating.
    - ``overrides``: pin per-instrument weights. When any pin is supplied the
      agent "takes the wheel": pinned weights are honored as-is, remaining mass
      is distributed by the chosen strategy, and internal cap-clamping plus
      ``max_positions``/``min_position_usd`` are skipped. Cap enforcement then
      belongs to the policy layer (``check-policy``), which is block-only.
    """
    if strategy not in strategies.STRATEGIES:
        supported = ", ".join(strategies.available())
        raise ValueError(
            f"unsupported strategy {strategy!r}; expected one of: {supported}"
        )
    params = dict(strategy_params or {})
    amount = _finite_nonnegative(amount_usd, "amount_usd")
    preset = _effective_preset(_preset(risk), score_power, apy_weight)
    cap_settings = _caps(caps)
    exclude_set = {str(item) for item in (exclude or ())}
    override_map = _normalized_overrides(overrides)
    max_pos = _validated_max_positions(max_positions)
    min_ticket = (
        _finite_nonnegative(min_position_usd, "min_position_usd")
        if min_position_usd is not None
        else 0.0
    )

    all_records = _records(scored_vaults)
    records = tuple(
        record
        for record in all_records
        if record.vault.instrument_id not in exclude_set
    )

    base_warnings: list[str] = []
    if exclude_set:
        base_warnings.append(f"excluded:count={len(exclude_set)}")

    if not records:
        return _finalize(
            legs=(),
            amount=amount,
            risk=risk,
            preset=preset,
            caps=cap_settings,
            warnings=[*base_warnings, "empty_universe:no allocation built"],
            extra_metadata={
                "strategy": strategy,
                **({"excluded": sorted(exclude_set)} if exclude_set else {}),
            },
        )

    if override_map:
        conflicting = override_map.keys() - {r.vault.instrument_id for r in records}
        if conflicting:
            raise ValueError(
                "override targets not in universe: " + ", ".join(sorted(conflicting))
            )
        if max_pos is not None or min_ticket > 0:
            base_warnings.append("shaping_knobs_ignored:overrides_present")
        solution = _solve_overrides(
            records, amount, preset, override_map, strategy, params
        )
        dropped: list[str] = []
    else:
        solution, dropped = _solve_formula(
            records, amount, preset, cap_settings, max_pos, min_ticket, strategy, params
        )

    extra_metadata: dict[str, Any] = {"strategy": strategy}
    if exclude_set:
        extra_metadata["excluded"] = sorted(exclude_set)
    if override_map:
        extra_metadata["pinned"] = sorted(override_map)
    if dropped:
        extra_metadata["dropped_below_min_position"] = dropped

    return _finalize(
        legs=solution.legs,
        amount=amount,
        risk=risk,
        preset=preset,
        caps=cap_settings,
        warnings=[*base_warnings, *solution.warnings],
        extra_metadata=extra_metadata,
    )


def _solve_formula(
    records: tuple[ScoredVault, ...],
    amount: float,
    preset: _RiskPreset,
    caps: _Caps,
    max_positions: int | None,
    min_ticket: float,
    strategy: str,
    params: Mapping[str, object],
) -> tuple[_Solution, list[str]]:
    active = records
    dropped: list[str] = []

    while True:
        desired, select_warnings = _strategy_desired(
            active, preset, strategy, params, max_positions
        )
        weights, cap_warnings = _apply_caps(active, desired, caps)
        legs = _legs(active, weights, amount)
        warnings = [
            *select_warnings,
            *cap_warnings,
            *_concentration_warnings(active, weights, caps),
        ]

        if min_ticket <= 0 or len(legs) <= 1:
            break

        sub_min = [leg for leg in legs if leg.usd < min_ticket - 1e-9]
        if not sub_min:
            break

        smallest = min(sub_min, key=lambda leg: (leg.usd, leg.instrument_id))
        active = tuple(
            record
            for record in active
            if record.vault.instrument_id != smallest.instrument_id
        )
        dropped.append(smallest.instrument_id)
        if not active:
            legs = ()
            warnings = ["min_position:dropped_all_below_min"]
            break

    if dropped:
        warnings.append(f"min_position:dropped={len(dropped)}:limit={min_ticket:.2f}")

    return _Solution(legs=tuple(legs), warnings=tuple(warnings)), dropped


def _solve_overrides(
    records: tuple[ScoredVault, ...],
    amount: float,
    preset: _RiskPreset,
    override_map: Mapping[str, float],
    strategy: str,
    params: Mapping[str, object],
) -> _Solution:
    formula, _ = strategies.desired_weights(
        strategy,
        records,
        score_power=preset.score_power,
        apy_weight=preset.apy_weight,
        params=params,
    )
    pinned_total = sum(override_map.values())
    remaining = max(0.0, 1.0 - pinned_total)

    non_pinned = [
        index
        for index, record in enumerate(records)
        if record.vault.instrument_id not in override_map
    ]
    weights = [0.0 for _ in records]
    for index, record in enumerate(records):
        pin = override_map.get(record.vault.instrument_id)
        if pin is not None:
            weights[index] = pin

    if remaining > _EPSILON and non_pinned:
        formula_total = sum(formula[index] for index in non_pinned)
        if formula_total > _EPSILON:
            for index in non_pinned:
                weights[index] = remaining * formula[index] / formula_total
        else:
            equal = remaining / len(non_pinned)
            for index in non_pinned:
                weights[index] = equal

    warnings = [
        f"overrides_applied:pinned={len(override_map)}",
        "caps_deferred_to_policy:overrides_present",
    ]
    legs = _legs(records, weights, amount)
    return _Solution(legs=tuple(legs), warnings=tuple(warnings))


def _records(
    scored_vaults: Iterable[
        ScoredVault | tuple[VaultScore, Vault] | tuple[Vault, VaultScore]
    ],
) -> tuple[ScoredVault, ...]:
    records: list[ScoredVault] = []
    seen: set[str] = set()

    for item in scored_vaults:
        if isinstance(item, ScoredVault):
            record = item
        elif isinstance(item, tuple) and len(item) == 2:
            first, second = item
            if isinstance(first, VaultScore) and isinstance(second, Vault):
                record = ScoredVault(score=first, vault=second)
            elif isinstance(first, Vault) and isinstance(second, VaultScore):
                record = ScoredVault(score=second, vault=first)
            else:
                raise TypeError(
                    "scored_vaults items must be ScoredVault or score/vault pairs"
                )
        else:
            raise TypeError(
                "scored_vaults items must be ScoredVault or score/vault pairs"
            )

        instrument_id = record.vault.instrument_id
        if instrument_id in seen:
            raise ValueError(f"duplicate instrument_id: {instrument_id}")
        seen.add(instrument_id)
        records.append(record)

    return tuple(sorted(records, key=lambda record: record.vault.instrument_id))


def _preset(risk: str) -> _RiskPreset:
    preset = RISK_PRESETS.get(risk)
    if preset is None:
        supported = ", ".join(RISK_PRESETS)
        raise ValueError(
            f"unsupported risk preset {risk!r}; expected one of: {supported}"
        )
    return preset


def _effective_preset(
    preset: _RiskPreset,
    score_power: float | None,
    apy_weight: float | None,
) -> _RiskPreset:
    power = preset.score_power if score_power is None else score_power
    weight = preset.apy_weight if apy_weight is None else apy_weight
    if power < 0:
        raise ValueError("score_power must be non-negative")
    if weight < 0:
        raise ValueError("apy_weight must be non-negative")
    if not (isfinite(power) and isfinite(weight)):
        raise ValueError("score_power and apy_weight must be finite")
    return _RiskPreset(score_power=power, apy_weight=weight)


def _normalized_overrides(
    overrides: Mapping[str, float] | None,
) -> dict[str, float]:
    if not overrides:
        return {}
    normalized: dict[str, float] = {}
    for instrument_id, raw in overrides.items():
        weight = _finite_nonnegative(raw, f"override[{instrument_id}]")
        if weight > 1:
            raise ValueError(f"override[{instrument_id}] must be between 0 and 1")
        normalized[str(instrument_id)] = weight
    total = sum(normalized.values())
    if total > 1 + 1e-9:
        raise ValueError(f"pinned override weights sum to {total:.6f}; must be <= 1.0")
    return normalized


def _validated_max_positions(max_positions: int | None) -> int | None:
    if max_positions is None:
        return None
    if int(max_positions) != max_positions or max_positions < 1:
        raise ValueError("max_positions must be a positive integer")
    return int(max_positions)


def _caps(caps: Mapping[str, object] | object | None) -> _Caps:
    return _Caps(
        max_weight_per_instrument=_cap(caps, "max_weight_per_instrument", "instrument"),
        max_weight_per_protocol=_cap(caps, "max_weight_per_protocol", "protocol"),
        max_weight_per_curator=_cap(caps, "max_weight_per_curator", "curator"),
        max_weight_per_chain=_cap(caps, "max_weight_per_chain", "chain"),
    )


def _cap(
    caps: Mapping[str, object] | object | None,
    canonical: str,
    alias: str,
) -> float:
    if caps is None:
        return 1.0

    if isinstance(caps, Mapping):
        raw = caps.get(canonical, caps.get(alias, 1.0))
    else:
        raw = getattr(caps, canonical, 1.0)
    if raw is None:
        return 1.0

    value = _finite_nonnegative(raw, canonical)
    if value > 1:
        raise ValueError(f"{canonical} must be between 0 and 1")
    return value


def _strategy_desired(
    records: Sequence[ScoredVault],
    preset: _RiskPreset,
    strategy: str,
    params: Mapping[str, object],
    max_positions: int | None,
) -> tuple[list[float], list[str]]:
    desired, warnings = strategies.desired_weights(
        strategy,
        records,
        score_power=preset.score_power,
        apy_weight=preset.apy_weight,
        params=params,
    )
    if max_positions is None:
        return desired, warnings

    positive = [index for index, weight in enumerate(desired) if weight > _EPSILON]
    if len(positive) <= max_positions:
        return desired, warnings

    keep = sorted(
        positive,
        key=lambda index: (-desired[index], records[index].vault.instrument_id),
    )[:max_positions]
    keep_set = set(keep)
    trimmed = [
        weight if index in keep_set else 0.0 for index, weight in enumerate(desired)
    ]
    total = sum(trimmed)
    trimmed = [weight / total for weight in trimmed]
    dropped = len(positive) - len(keep_set)
    return trimmed, [*warnings, f"max_positions:kept={len(keep_set)}:dropped={dropped}"]


def _apply_caps(
    records: Sequence[ScoredVault],
    desired: Sequence[float],
    caps: _Caps,
) -> tuple[list[float], list[str]]:
    weights = [0.0 for _ in records]
    desired_by_dimension = _dimension_totals(records, desired)
    remaining = 1.0

    while remaining > _EPSILON:
        capacities = [
            _capacity(records, weights, caps, index)
            for index in range(len(records))
        ]
        # Only ever place capital in instruments the strategy actually chose
        # (desired > 0). Redistributing capped-out weight onto zero-desire
        # instruments would silently resurrect legs the strategy excluded —
        # e.g. it would blow past --max-positions when the top-N hit their
        # caps. Weight that no chosen leg can absorb is reported as
        # unallocatable instead.
        active = [
            index
            for index, capacity in enumerate(capacities)
            if capacity > _EPSILON and desired[index] > _EPSILON
        ]
        if not active:
            # Concentration ceilings leave capital unplaceable. Degrade
            # gracefully: keep what fits and report the remainder as
            # unallocated rather than aborting the whole proposal.
            break

        priority_total = sum(desired[index] for index in active)
        if priority_total > _EPSILON:
            increments = {
                index: remaining * desired[index] / priority_total
                for index in active
            }
        else:
            equal_increment = remaining / len(active)
            increments = {index: equal_increment for index in active}

        scale = _increment_scale(records, weights, caps, increments)
        if scale <= _EPSILON:
            break

        progress = 0.0
        for index, increment in increments.items():
            addition = increment * scale
            weights[index] += addition
            progress += addition

        if progress <= _EPSILON:
            break
        remaining = max(0.0, 1.0 - sum(weights))

    warnings = _cap_warnings(records, weights, desired_by_dimension, caps)
    unallocatable = max(0.0, 1.0 - sum(weights))
    if unallocatable > 1e-9:
        warnings.append(f"caps_binding:unallocatable_weight={unallocatable:.6f}")
    return weights, warnings


def _increment_scale(
    records: Sequence[ScoredVault],
    weights: Sequence[float],
    caps: _Caps,
    increments: Mapping[int, float],
) -> float:
    scale = 1.0
    for dimension, cap in _cap_items(caps):
        current = _dimension_totals(records, weights)[dimension]
        increment = _dimension_totals_for_indexes(records, increments)[dimension]
        for key, increment_weight in increment.items():
            if increment_weight <= _EPSILON:
                continue
            residual = cap - current.get(key, 0.0)
            if residual < -_EPSILON:
                return 0.0
            if increment_weight > residual:
                scale = min(scale, max(0.0, residual) / increment_weight)
    return scale


def _capacity(
    records: Sequence[ScoredVault],
    weights: Sequence[float],
    caps: _Caps,
    index: int,
) -> float:
    totals = _dimension_totals(records, weights)
    return min(
        caps.max_weight_per_instrument
        - totals["instrument"][_key(records[index], "instrument")],
        caps.max_weight_per_protocol
        - totals["protocol"][_key(records[index], "protocol")],
        caps.max_weight_per_curator
        - totals["curator"][_key(records[index], "curator")],
        caps.max_weight_per_chain - totals["chain"][_key(records[index], "chain")],
    )


def _dimension_totals(
    records: Sequence[ScoredVault],
    weights: Sequence[float],
) -> dict[str, dict[str, float]]:
    totals: dict[str, dict[str, float]] = {
        "instrument": defaultdict(float),
        "protocol": defaultdict(float),
        "curator": defaultdict(float),
        "chain": defaultdict(float),
    }
    for record, weight in zip(records, weights, strict=True):
        for dimension in totals:
            totals[dimension][_key(record, dimension)] += weight
    return {dimension: dict(values) for dimension, values in totals.items()}


def _dimension_totals_for_indexes(
    records: Sequence[ScoredVault],
    weights_by_index: Mapping[int, float],
) -> dict[str, dict[str, float]]:
    weights = [0.0 for _ in records]
    for index, weight in weights_by_index.items():
        weights[index] = weight
    return _dimension_totals(records, weights)


def _key(record: ScoredVault, dimension: str) -> str:
    if dimension == "instrument":
        return record.vault.instrument_id
    if dimension == "protocol":
        return record.vault.protocol
    if dimension == "curator":
        return curator_bucket(record.vault.instrument_id, record.vault.curator)
    if dimension == "chain":
        return str(record.vault.chain_id)
    raise ValueError(f"unknown cap dimension: {dimension}")


def _cap_items(caps: _Caps) -> tuple[tuple[str, float], ...]:
    return (
        ("instrument", caps.max_weight_per_instrument),
        ("protocol", caps.max_weight_per_protocol),
        ("curator", caps.max_weight_per_curator),
        ("chain", caps.max_weight_per_chain),
    )


def _cap_warnings(
    records: Sequence[ScoredVault],
    weights: Sequence[float],
    desired_by_dimension: Mapping[str, Mapping[str, float]],
    caps: _Caps,
) -> list[str]:
    warnings: list[str] = []
    actual_by_dimension = _dimension_totals(records, weights)
    for dimension, cap in _cap_items(caps):
        if cap >= 1:
            continue
        desired = desired_by_dimension[dimension]
        actual = actual_by_dimension[dimension]
        for key in sorted(desired):
            if desired[key] > cap + _EPSILON and actual.get(key, 0.0) <= cap + 1e-9:
                warnings.append(f"cap_clamped:{dimension}:{key}:limit={cap:.6f}")
    return warnings


def _concentration_warnings(
    records: Sequence[ScoredVault],
    weights: Sequence[float],
    caps: _Caps,
) -> list[str]:
    warnings: list[str] = []
    if len(records) == 1:
        warnings.append("concentration:single_vault:allocation has one instrument")

    totals = _dimension_totals(records, weights)
    for dimension, cap in _cap_items(caps):
        threshold = min(0.5, cap) if cap < 1 else 0.5
        for key, weight in sorted(totals[dimension].items()):
            if weight >= threshold - 1e-9 and weight > 0:
                warnings.append(f"concentration:{dimension}:{key}:weight={weight:.6f}")
    return warnings


def _legs(
    records: Sequence[ScoredVault],
    weights: Sequence[float],
    amount_usd: float,
) -> list[AllocationLeg]:
    weighted_records = [
        (record, _clean_weight(weight))
        for record, weight in zip(records, weights, strict=True)
        if weight > _EPSILON
    ]
    weighted_records.sort(key=lambda item: (-item[1], item[0].vault.instrument_id))
    clean_weights = [weight for _, weight in weighted_records]
    allocated_usd = _round_usd(amount_usd * sum(clean_weights))
    usd_amounts = _usd_amounts(clean_weights, allocated_usd)

    return [
        AllocationLeg(
            instrument_id=record.vault.instrument_id,
            weight=weight,
            usd=usd,
        )
        for (record, weight), usd in zip(weighted_records, usd_amounts, strict=True)
    ]


def _usd_amounts(weights: Sequence[float], amount_usd: float) -> list[float]:
    total_cents = int(round(amount_usd * 100))
    if not weights:
        return []

    weight_total = sum(weights)
    raw_cents = [weight / weight_total * total_cents for weight in weights]
    cents = [floor(value) for value in raw_cents]
    remainder = total_cents - sum(cents)
    order = sorted(
        range(len(weights)),
        key=lambda index: (-(raw_cents[index] - cents[index]), -weights[index]),
    )
    for index in order[:remainder]:
        cents[index] += 1
    return [value / 100 for value in cents]


def _finalize(
    *,
    legs: tuple[AllocationLeg, ...],
    amount: float,
    risk: str,
    preset: _RiskPreset,
    caps: _Caps,
    warnings: Sequence[str],
    extra_metadata: Mapping[str, Any],
) -> Allocation:
    requested = _round_usd(amount)
    allocated = _round_usd(sum(leg.usd for leg in legs))
    unallocated = _round_usd(requested - allocated)
    metadata = _metadata(
        risk=risk,
        preset=preset,
        caps=caps,
        warnings=warnings,
        requested=requested,
        unallocated=unallocated,
        extra=extra_metadata,
    )
    allocation = Allocation(
        legs=legs,
        total_usd=allocated,
        metadata=metadata,
    )
    validate(allocation.model_dump(mode="json"), "allocation")
    return allocation


def _metadata(
    *,
    risk: str,
    preset: _RiskPreset,
    caps: _Caps,
    warnings: Sequence[str],
    requested: float,
    unallocated: float,
    extra: Mapping[str, Any],
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "risk": risk,
        "requested_amount_usd": requested,
        "unallocated_usd": unallocated,
        "preset": {
            "score_power": preset.score_power,
            "apy_weight": preset.apy_weight,
        },
        "caps": {
            "max_weight_per_instrument": caps.max_weight_per_instrument,
            "max_weight_per_protocol": caps.max_weight_per_protocol,
            "max_weight_per_curator": caps.max_weight_per_curator,
            "max_weight_per_chain": caps.max_weight_per_chain,
        },
        "warnings": sorted(set(warnings)),
    }
    metadata.update(extra)
    return metadata


def _clean_weight(weight: float) -> float:
    if abs(weight) <= _EPSILON:
        return 0.0
    if abs(1 - weight) <= _EPSILON:
        return 1.0
    return weight


def _round_usd(amount: float) -> float:
    return int(round(amount * 100)) / 100


def _finite_nonnegative(raw: object, name: str) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be a finite non-negative number") from error
    if not isfinite(value) or value < 0:
        raise ValueError(f"{name} must be a finite non-negative number")
    return value


__all__ = ["RISK_PRESETS", "RiskPresetName", "ScoredVault", "build_allocation"]
