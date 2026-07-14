# 02-04 — CLI: `build-allocation` + `check-policy` + `simulate`

**Phase:** 2 — Allocation + policy
**Depends on:** 02-00, 02-01, 02-02, 02-03, 00-05
**Status:** done

## Goal
The allocation playground (M2): build a policy-conformant book, gate it, and measure it — JSON out,
shareable, no execution.

## Scope / Deliverables
- `build-allocation --risk balanced --amount 10000 [--policy policy.yaml]` → discover → score → allocate
  (02-00) → run policy check (02-01) → emit an `Allocation` JSON (+ any violations/warnings).
- `check-policy --allocation allocation.json [--policy policy.yaml]` → explicit gate; JSON `PolicyResult`.
- `simulate --allocation allocation.json [--benchmark ...]` → 02-03 scorecard, JSON out.
- Allocations are shareable JSON files consumed by later commands.

## Tests
- `CliRunner` + mocked client: `build-allocation` emits a schema-valid `Allocation` and never emits one
  that fails `check-policy`.
- `check-policy` on a crafted violating allocation exits with `ok:false` + violations.
- `simulate` emits a descriptive scorecard.

## Acceptance criteria
- [x] The three commands compose over JSON files (output of one is input to the next).
- [x] No spend path exists in Phase 2 (no signer imported).

## References
plan_allocator.md § Build Order M2, § Recommended CLI
