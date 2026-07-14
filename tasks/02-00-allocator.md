# 02-00 — Allocation builder

**Phase:** 2 — Allocation + policy
**Depends on:** 01-03, 00-03
**Status:** done

## Goal
Turn scored vaults + a risk preference + an amount into a weighted, concentration-aware `Allocation`.

## Scope / Deliverables
- `src/open_allocator/core/allocator.py`:
  - `build_allocation(scored_vaults, amount_usd, risk="balanced", caps=None) -> Allocation`.
  - Weighting: score-tilted (e.g. score-weighted or inverse-risk), with `risk` presets
    (`conservative|balanced|aggressive`) documented and deterministic.
  - Respect caps during construction: per-instrument/protocol/curator/chain weight ceilings; renormalize
    after clamping; surface concentration warnings.
  - Optional constrained optimization (add `scipy`/`cvxpy` only if a preset needs it — pin then).
- Pure function; no network. Emits per-leg USD amounts summing to `amount_usd` (within rounding).

## Tests
- Weights sum to 1.0 (± epsilon); leg USD sums to `amount_usd`.
- Caps enforced: a cap forces clamping + renormalization; over-concentration warned.
- Determinism per (inputs, risk preset); presets rank differently as documented.
- Empty/one-vault universes handled gracefully (clear result, no crash).

## Acceptance criteria
- [x] Produced `Allocation` validates against `allocation.schema.json`.
- [x] Construction-time caps never emit an allocation that later fails 02-01's policy gate.

## References
plan_allocator.md § How the Allocator Runs Safely (steps 3–5), § DeFi Reality Checks (concentration)
