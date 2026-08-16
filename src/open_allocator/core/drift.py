"""The daily gate: has the book moved far enough to be worth thinking about?

This runs first, every day, before anything expensive. If it says no, the caller
stops -- no agent step, no model call, no transaction. On a stable book that is
most days, and it is the whole reason a daily agent is affordable rather than a
standing bill.

Which makes the asymmetry between its two answers the thing to design around.
`drifted: true` costs one agent run. `drifted: false` is an instruction to *skip
looking*, so a wrong `false` is a book that quietly stops being governed by the
mandate it claims to follow. Nothing here may return `false` because it could
not tell:

  - A check the mandate asks for but that cannot be run emits an `unevaluated`
    reason and drift is true. "I could not measure the weight bands" and "the
    weight bands are fine" are the same value to a caller reading a boolean, and
    opposite instructions.
  - An instrument held but missing from the shelf is not silently dropped from a
    sleeve total. Missing information must never read as compliance -- the same
    rule `simulate.sector_concentration` states for sectors.

Nothing here decides what to *do* about drift. It reports; the agent proposes
and deterministic policy code gates. A drift report is an input to a decision,
never a decision.

**Two deviations from the design spec, both forced by what a mandate contains:**

`--allocation` exists. The spec's own example emits `target_bps` per instrument,
but a mandate carries *tier* weights, not per-instrument weights -- so a
per-instrument target is unobtainable from a mandate and a positions file alone.
The target allocation is the book the positions were built to, and it is the
only honest source of that number. Omit it and `weight_band` reports itself
unevaluated rather than guessing.

`shelf_change` reads a previous shelf from a file rather than a state backend.
The spec keeps that hash in the caller's database row, which would make a
question about two files require a service dependency; this library is
CLI-first, filesystem-state and free of those on purpose. The caller passes
whatever path it likes and stores the returned snapshot however it likes.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Literal, TypeAlias

from pydantic import Field

from open_allocator.core import diversify, simulate
from open_allocator.core import policy as policy_core
from open_allocator.core.mandate import Mandate
from open_allocator.core.positions import Positions
from open_allocator.core.scoring import score_vault
from open_allocator.core.types import (
    Allocation,
    AllocationLeg,
    FrozenModel,
    Policy,
    Vault,
)

HASH_PREFIX = "sha256:"
_BPS = 10_000


class WeightBandReason(FrozenModel):
    type: Literal["weight_band"] = "weight_band"
    instrument_id: str
    target_bps: int
    actual_bps: int
    band_bps: int


class SleeveBandReason(FrozenModel):
    type: Literal["sleeve_band"] = "sleeve_band"
    sleeve: str
    target_bps: int
    actual_bps: int
    band_bps: int


class PolicyViolationReason(FrozenModel):
    type: Literal["policy_violation"] = "policy_violation"
    rule: str
    required: Any
    actual: Any


class ShelfChangeReason(FrozenModel):
    type: Literal["shelf_change"] = "shelf_change"
    new_instruments: int
    delisted: int


class UnevaluatedReason(FrozenModel):
    """A check the mandate asks for that could not be run.

    Carried in `reasons` rather than in a field of its own so that a caller
    reading `drifted` cannot act on the boolean while ignoring the caveat. It
    is a reason to look, because not knowing is a reason to look.
    """

    type: Literal["unevaluated"] = "unevaluated"
    check: str
    because: str


DriftReason: TypeAlias = (
    WeightBandReason
    | SleeveBandReason
    | PolicyViolationReason
    | ShelfChangeReason
    | UnevaluatedReason
)


class ShelfSnapshot(FrozenModel):
    """What the shelf held, small enough to store every day.

    Ids and a hash of them, not the vault records: the question `shelf_change`
    answers is which instruments appeared and disappeared, and keeping the
    metrics would store a snapshot that changes every day for reasons this
    check does not care about.
    """

    instrument_ids: tuple[str, ...]
    hash: str

    @classmethod
    def of(cls, instrument_ids: Iterable[str]) -> "ShelfSnapshot":
        unique = tuple(sorted(set(instrument_ids)))
        digest = hashlib.sha256(
            json.dumps(unique, separators=(",", ":")).encode("utf-8"),
        ).hexdigest()
        return cls(instrument_ids=unique, hash=HASH_PREFIX + digest)

    @classmethod
    def parse(cls, payload: object) -> "ShelfSnapshot":
        """Accept a stored snapshot or a raw `list-vaults` array.

        The second form matters more than it looks: it means yesterday's
        `list-vaults` output is a valid `--previous-shelf` with no conversion
        step, so the check can be used before any caller has built storage for
        it.
        """
        if isinstance(payload, ShelfSnapshot):
            return payload
        if isinstance(payload, Mapping):
            ids = payload.get("instrument_ids")
            if ids is None:
                raise ValueError("shelf snapshot has no instrument_ids")
            return cls.of(str(item) for item in _sequence(ids))
        return cls.of(_instrument_id(item) for item in _sequence(payload))


class DriftReport(FrozenModel):
    drifted: bool
    reasons: tuple[DriftReason, ...]
    effective_positions: float | None
    effective_sectors: float | None
    total_usd: float = Field(ge=0)
    shelf: ShelfSnapshot | None = None


def evaluate(
    mandate: Mandate,
    positions: Positions | Mapping[str, object],
    policy: Policy | Mapping[str, object],
    *,
    target: Allocation | Mapping[str, object] | None = None,
    known_instruments: Iterable[Vault | Mapping[str, object]] = (),
    previous_shelf: object | None = None,
) -> DriftReport:
    """Compare a book against the mandate that governs it.

    `known_instruments` is the current shelf, enriched. It serves three
    purposes at once -- scoring held instruments into sleeves, supplying the
    history the independence floor is measured from, and defining today's shelf
    for `shelf_change` -- so an empty one degrades three checks rather than
    silently passing them.
    """
    positions_model = _positions(positions)
    policy_model = _policy(policy)
    vaults = tuple(_vault(item) for item in known_instruments)
    vault_by_id = {vault.instrument_id: vault for vault in vaults}
    held = _held_bps(positions_model)

    current = _as_allocation(positions_model, held)
    reasons: list[DriftReason] = []

    reasons.extend(_weight_band_reasons(mandate, held, target))
    reasons.extend(_sleeve_band_reasons(mandate, held, vault_by_id))
    reasons.extend(_policy_reasons(current, policy_model, vaults))

    shelf = ShelfSnapshot.of(vault_by_id) if vaults else None
    reasons.extend(_shelf_reasons(shelf, previous_shelf))

    return DriftReport(
        drifted=bool(reasons),
        reasons=tuple(reasons),
        effective_positions=_effective_positions(held, vault_by_id),
        effective_sectors=(
            simulate.sector_concentration(current, vaults).effective_sectors
            if vaults and held
            else None
        ),
        total_usd=positions_model.total_position_usd,
        shelf=shelf,
    )


def _weight_band_reasons(
    mandate: Mandate,
    held: Mapping[str, int],
    target: Allocation | Mapping[str, object] | None,
) -> list[DriftReason]:
    band = mandate.bands.weight_drift_bps
    if target is None:
        return [
            UnevaluatedReason(
                check="weight_band",
                because=(
                    f"the mandate sets weight_drift_bps={band} but no target "
                    "allocation was supplied, and a mandate carries tier "
                    "weights rather than per-instrument ones"
                ),
            )
        ]

    target_bps = _target_bps(target)
    reasons: list[DriftReason] = []
    # Union, not the target's legs: a position held outside the target has a
    # target of zero and is the most drifted a leg can be, so iterating the
    # target alone would miss exactly the case that matters most.
    for instrument_id in sorted(set(target_bps) | set(held)):
        actual = held.get(instrument_id, 0)
        wanted = target_bps.get(instrument_id, 0)
        if abs(actual - wanted) > band:
            reasons.append(
                WeightBandReason(
                    instrument_id=instrument_id,
                    target_bps=wanted,
                    actual_bps=actual,
                    band_bps=band,
                )
            )
    return reasons


def _sleeve_band_reasons(
    mandate: Mandate,
    held: Mapping[str, int],
    vault_by_id: Mapping[str, Vault],
) -> list[DriftReason]:
    tiers = _tiers(mandate)
    if not tiers:
        return []
    if not held:
        return []

    unscorable = sorted(key for key in held if key not in vault_by_id)
    if unscorable:
        # Not assigned to a bucket and not dropped. Either would answer a
        # question we cannot answer: dropping shrinks every sleeve total by the
        # missing weight, and bucketing guesses which sleeve it belonged to.
        return [
            UnevaluatedReason(
                check="sleeve_band",
                because=(
                    "held instruments are absent from the shelf and cannot be "
                    f"scored into a sleeve: {', '.join(unscorable)}"
                ),
            )
        ]

    band = mandate.bands.sleeve_drift_bps
    actual_by_tier: dict[str, int] = {tier["name"]: 0 for tier in tiers}
    for instrument_id, weight_bps in held.items():
        score = score_vault(vault_by_id[instrument_id]).score
        tier = _tier_for(score, tiers)
        if tier is None:
            return [
                UnevaluatedReason(
                    check="sleeve_band",
                    because=(
                        f"{instrument_id} scores {score:.4f}, which no tier in "
                        "the mandate's ladder covers"
                    ),
                )
            ]
        actual_by_tier[tier["name"]] += weight_bps

    reasons: list[DriftReason] = []
    for tier in tiers:
        wanted = int(round(float(tier["weight"]) * _BPS))
        actual = actual_by_tier[tier["name"]]
        if abs(actual - wanted) > band:
            reasons.append(
                SleeveBandReason(
                    sleeve=str(tier["name"]),
                    target_bps=wanted,
                    actual_bps=actual,
                    band_bps=band,
                )
            )
    return reasons


def _policy_reasons(
    current: Allocation,
    policy: Policy,
    vaults: Sequence[Vault],
) -> list[DriftReason]:
    if not current.legs:
        return []
    result = policy_core.check(current, policy, vaults)
    return [
        PolicyViolationReason(
            rule=violation.rule,
            required=violation.limit,
            actual=violation.actual,
        )
        for violation in result.violations
    ]


def _shelf_reasons(
    shelf: ShelfSnapshot | None,
    previous: object | None,
) -> list[DriftReason]:
    if previous is None:
        return []
    if shelf is None:
        return [
            UnevaluatedReason(
                check="shelf_change",
                because="a previous shelf was supplied but today's shelf is empty",
            )
        ]

    before = set(ShelfSnapshot.parse(previous).instrument_ids)
    now = set(shelf.instrument_ids)
    added = len(now - before)
    removed = len(before - now)
    if not added and not removed:
        return []
    return [ShelfChangeReason(new_instruments=added, delisted=removed)]


def _effective_positions(
    held: Mapping[str, int],
    vault_by_id: Mapping[str, Vault],
) -> float | None:
    """The independent-bet count, or None when it could not be measured.

    None rather than 0.0: a book of one instrument genuinely is one bet, and a
    book whose history is missing is an unknown. Collapsing the two would let
    an unmeasurable book read as the most concentrated possible one, which is
    alarming in the wrong direction and, worse, actionable.
    """
    if not held:
        return None
    series_by_id = {
        instrument_id: dict(vault_by_id[instrument_id].apy_daily)
        for instrument_id in held
        if instrument_id in vault_by_id and vault_by_id[instrument_id].apy_daily
    }
    if not series_by_id:
        return None
    matrix = diversify.co_movement_matrix(series_by_id)
    return diversify.effective_positions(dict(held), matrix)


def _held_bps(positions: Positions) -> dict[str, int]:
    """Weights over the *deployed* book, excluding idle USDC.

    Idle cash is not a position and cannot drift from a target weight; folding
    it in would make every leg read as under-weight by however much was waiting
    to be deployed.
    """
    total = sum(holding.usd_value for holding in positions.holdings)
    if total <= 0:
        return {}
    weights: dict[str, int] = {}
    for holding in positions.holdings:
        share = int(round(holding.usd_value / total * _BPS))
        weights[holding.instrument_id] = weights.get(holding.instrument_id, 0) + share
    return {key: value for key, value in weights.items() if value > 0}


def _as_allocation(positions: Positions, held: Mapping[str, int]) -> Allocation:
    total = sum(holding.usd_value for holding in positions.holdings)
    usd_by_id: dict[str, float] = {}
    for holding in positions.holdings:
        usd_by_id[holding.instrument_id] = (
            usd_by_id.get(holding.instrument_id, 0.0) + holding.usd_value
        )
    return Allocation(
        legs=tuple(
            AllocationLeg(
                instrument_id=instrument_id,
                weight=weight_bps / _BPS,
                usd=usd_by_id.get(instrument_id, 0.0),
            )
            for instrument_id, weight_bps in sorted(held.items())
        ),
        total_usd=max(total, 0.0),
        metadata={"source": "positions"},
    )


def _target_bps(target: Allocation | Mapping[str, object]) -> dict[str, int]:
    model = (
        target if isinstance(target, Allocation) else Allocation.model_validate(target)
    )
    weights: dict[str, int] = {}
    for leg in model.legs:
        share = int(round(leg.weight * _BPS))
        weights[leg.instrument_id] = weights.get(leg.instrument_id, 0) + share
    return {key: value for key, value in weights.items() if value > 0}


def _tiers(mandate: Mandate) -> list[dict[str, Any]]:
    raw = mandate.strategy_params.get("tiers")
    if not isinstance(raw, Sequence) or isinstance(raw, str | bytes):
        return []
    return [dict(tier) for tier in raw if isinstance(tier, Mapping)]


def _tier_for(
    score: float,
    tiers: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    for tier in tiers:
        low = float(tier.get("min_score", 0.0))
        high = float(tier.get("max_score", 1.0))
        # Half-open upward so a score sitting exactly on a boundary lands in
        # one tier rather than two, and the top tier keeps its own ceiling.
        if low <= score < high or (score >= high and high >= 1.0):
            return tier
    return None


def _positions(value: Positions | Mapping[str, object]) -> Positions:
    return value if isinstance(value, Positions) else Positions.model_validate(value)


def _policy(value: Policy | Mapping[str, object]) -> Policy:
    return value if isinstance(value, Policy) else Policy.model_validate(value)


def _vault(value: Vault | Mapping[str, object]) -> Vault:
    return value if isinstance(value, Vault) else Vault.model_validate(value)


def _sequence(value: object) -> Sequence[object]:
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return value
    raise ValueError("expected a list")


def _instrument_id(item: object) -> str:
    if isinstance(item, Mapping):
        value = item.get("instrument_id") or item.get("instrumentId")
        if value is not None:
            return str(value)
        raise ValueError("shelf entry has no instrument_id")
    return str(item)


__all__ = [
    "DriftReason",
    "DriftReport",
    "PolicyViolationReason",
    "ShelfChangeReason",
    "ShelfSnapshot",
    "SleeveBandReason",
    "UnevaluatedReason",
    "WeightBandReason",
    "evaluate",
]
