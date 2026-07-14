# 04-01 — Rebalance (deltas only)

**Phase:** 4 — Rebalance & exit
**Depends on:** 04-00, 03-02, 02-01
**Status:** done

## Goal
Move a current book to a target allocation by executing **only the deltas**, under policy + confirmation.

## Scope / Deliverables
- `src/open_allocator/core/rebalance.py`:
  - `plan_rebalance(positions, target, policy) -> RebalancePlan` — sells then buys, deltas only, min-trade
    threshold to avoid dust churn; run `policy.check` on the resulting target.
  - Execute via 03-02's executor (sells first to free USDC, then buys); reuse gas preflight + idempotency.
  - `autonomous_rebalance` gate: only run unattended if `policy.gates.autonomous_rebalance` is true **and**
    within caps; otherwise require `--confirm`.
- `rebalance --current positions.json --target allocation.json --confirm` CLI.

## Tests
- Delta math: only changed legs trade; below-threshold deltas skipped.
- Sell-before-buy ordering; USDC freed before buys.
- Policy re-checked on target; violation aborts before any trade.
- `autonomous_rebalance=false` blocks unattended execution (requires `--confirm`).
- Idempotent resume after a mid-rebalance failure.

## Acceptance criteria
- [x] Only deltas execute; no full liquidation-and-rebuild.
- [x] Unattended rebalance is impossible unless explicitly enabled within caps.

## References
plan_allocator.md § How the Allocator Runs Safely (step 10), § The Policy Layer (gates)
