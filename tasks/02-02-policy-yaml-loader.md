# 02-02 — `policy.yaml` schema + loader

**Phase:** 2 — Allocation + policy
**Depends on:** 00-04, 00-03
**Status:** done

## Goal
Load and validate the allocator's "constitution" from `policy.yaml` into a typed `Policy`.

## Scope / Deliverables
- `schemas/policy.schema.json` (finalized) matching the plan's `policy.yaml`:
  `wallet {mode, signer}`, `allowed {protocols, chains, assets, curators}` (null = all),
  `caps {...}`, `gates {...}`.
- `src/open_allocator/core/policy_loader.py`: `load_policy(path) -> Policy` — YAML → schema-validate
  (00-04) → `Policy` model (00-03), with clear errors on unknown keys / bad types / out-of-range caps.
- A committed `policy.yaml` example at repo root (the plan's default).

## Tests
- The example `policy.yaml` loads and validates.
- `null`/omitted allowlists parse as "all"; explicit lists parse as-is.
- Unknown key, wrong type, or cap out of [0,1] → clear error.
- Round-trip: `Policy` re-serialized still validates.

## Acceptance criteria
- [x] `load_policy` is the single entrypoint; 02-01 consumes its output.
- [x] The default `policy.yaml` is valid and documented.

## References
plan_allocator.md § The Policy Layer (policy.yaml)
