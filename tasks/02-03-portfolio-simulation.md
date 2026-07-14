# 02-03 — Portfolio simulation / scorecard wrappers

**Phase:** 2 — Allocation + policy
**Depends on:** 01-00, 00-03
**Status:** done

## Goal
Wrap 1Tx's portfolio endpoints so an `Allocation` can be measured before execution.

## Scope / Deliverables
- `src/open_allocator/core/simulate.py`:
  - `analyze(client, allocation)` → `POST /portfolios/analyze` (blended APY, effective positions,
    concentration flags, one-failure cost).
  - `compare(client, before, after)` → `POST /portfolios/compare` (per-metric deltas for a tweak).
  - `simulate(client, allocation, benchmark=None)` → `POST /portfolios/simulate` (descriptive backtest).
  - Map allocation legs to the `id:weight_bps` shape the API expects; parse results into typed models.
- Present results as **descriptive, not predictive** (label carried into output).

## Tests
- Allocation → request payload mapping (weights → bps) is correct and sums as expected.
- Mocked responses parse into typed scorecards; concentration flags surfaced.
- `compare` returns per-metric deltas for a known before/after.

## Acceptance criteria
- [x] The allocate flow can analyze + simulate a book end-to-end against a mocked API.
- [x] Output is clearly marked descriptive.

## References
plan_allocator.md § How the Allocator Runs Safely (step 6), § Data & Execution Layer
