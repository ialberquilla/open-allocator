# 00-04 — JSON schemas + validator

**Phase:** 0 — Foundations
**Depends on:** 00-03
**Status:** done

## Goal
Canonical, versioned JSON-Schema contracts for the artifacts that cross stage/CLI boundaries, plus a
validation helper used everywhere they're produced or consumed.

## Scope / Deliverables
- `schemas/policy.schema.json`, `schemas/vault-score.schema.json`, `schemas/allocation.schema.json`,
  `schemas/tx-plan.schema.json` (draft 2020-12).
- `src/open_allocator/core/schema.py`: `validate(obj, schema_name)` → raises a typed error listing every
  violation; loads schemas from `schemas/`.
- Keep schemas and pydantic models (00-03) in sync (a test enforces this).

## Tests
- Each pydantic model's `model_dump()` for a valid instance passes its schema.
- Deliberately malformed artifacts fail with the offending path reported.
- Sync test: schema required-fields ⊆ model fields, and model-serialized samples validate — drift fails.
- `validate()` on an unknown schema name errors clearly.

## Acceptance criteria
- [x] All four artifacts have schemas; `validate()` is the single validation entrypoint.
- [x] A model/schema drift is caught by a test, not at runtime.

## References
plan_allocator.md § Canonical Artifacts, § Repo Layout (schemas/)
