# 00-03 — Domain types (pydantic models)

**Phase:** 0 — Foundations
**Depends on:** 00-01
**Status:** done

## Goal
The typed vocabulary every layer shares, so scoring, allocation, policy, and execution pass structured
objects, not dicts.

## Scope / Deliverables
- `src/open_allocator/core/types.py`:
  - `Vault` — an 1Tx instrument: `instrument_id`, `protocol`, `chain_id`, `asset`, `apy`, `tvl_usd`,
    plus optional risk fields (`curator`, `lltv`, `reward_dependence`, `oracle`, `fee`, …) that may be
    `None`/`Unknown` (never invented — see plan § dynamic universe).
  - `VaultScore` — composite `score` + a `factors: dict[str, FactorScore]` where each factor records its
    raw input, normalized value, weight, and an `Unknown` flag. Must be fully explainable from its parts.
  - `AllocationLeg` (`instrument_id`, `weight`, `usd`) and `Allocation` (`legs`, `total_usd`, metadata).
  - `TxPlan` — ordered `TxStep`s (`{to, data, value, chain_id, kind: approve|buy|sell}`) + summary.
  - `Policy` — mirrors `policy.yaml` (see 02-02): `allowed`, `caps`, `gates`, `wallet`.
- All models frozen/immutable where practical; `extra="forbid"` to catch typos.

## Tests
- Round-trip: model → `model_dump()` → re-parse equals original.
- Optional risk fields accept `None` and a sentinel `Unknown` without error.
- `extra="forbid"` rejects unknown keys.
- `VaultScore` is reconstructable: recomputing `score` from `factors` matches the stored `score`.

## Acceptance criteria
- [x] Every later layer imports its types from here (no ad-hoc dicts across module boundaries).
- [x] Unknown/missing risk data is representable and distinguishable from zero.

## References
plan_allocator.md § Transparent Risk Model, § Repo Layout (core/types.py)
