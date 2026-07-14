# 02-01 — Policy enforcement (block-only)

**Phase:** 2 — Allocation + policy
**Depends on:** 02-00, 02-02
**Status:** done

## Goal
The differentiator: a hard gate that can only ever **block or tighten**, never loosen. A proposed
allocation that violates policy is rejected before any tx is built.

## Scope / Deliverables
- `src/open_allocator/core/policy.py`:
  - `check(allocation, policy, known_instruments) -> PolicyResult` with `ok: bool` and a list of typed
    violations (each: rule, offending entity, limit, actual).
  - Rules: allowed protocols/chains/assets/curators; caps (per-instrument/protocol/curator/chain weight,
    min TVL, max LLTV, max reward dependence); gates (`new_instrument_needs_approval`,
    `max_deploy_per_cycle_usd`, `autonomous_rebalance`).
  - **No path loosens policy**: the API surface exposes only checks, never overrides (assert by design).
- Deterministic, pure, no network.

## Tests
- Each rule has a passing and a failing fixture; violations name the offending entity + limit + actual.
- A `null` allowlist means "all" (pass-through); an explicit allowlist blocks anything outside it.
- `new_instrument_needs_approval`: an unseen instrument is flagged for approval, not silently allowed.
- `max_deploy_per_cycle_usd` blocks an oversized cycle.
- Property test: no input makes `check` return `ok=True` for an allocation exceeding any cap.

## Acceptance criteria
- [x] Violations are actionable (agent can explain and fix).
- [x] Impossible to pass a policy-violating allocation; execution (Phase 3) calls `check` and aborts on `ok=False`.

## References
plan_allocator.md § The Policy Layer, § Core Architectural Rule (block-only invariant)
