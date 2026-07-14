# 04-00 — Positions & reconciliation

**Phase:** 4 — Rebalance & exit
**Depends on:** 01-00, 00-03
**Status:** done

## Goal
Read current on-chain holdings and reconcile them against an intended allocation.

## Scope / Deliverables
- `src/open_allocator/core/positions.py`:
  - `read_positions(client, address) -> Positions` via `POST /positions` + `GET
    /transactions/balances/:address` (yield-token share balances + idle USDC per chain).
  - `reconcile(positions, target_allocation) -> Diff` — current vs target weights → per-instrument deltas
    (buy/sell amounts), including idle USDC to deploy.
- `positions` CLI command → JSON of holdings + idle balances.

## Tests
- Mocked responses parse into `Positions` with per-instrument **share** balances (not USD-only).
- `reconcile` computes correct deltas; a matched book yields empty deltas.
- Idle USDC surfaced and included in deploy deltas.

## Acceptance criteria
- [x] Positions expose share balances (required for correct ERC-4626 exits in 04-02).
- [x] `reconcile` is deterministic and drives 04-01.

## References
plan_allocator.md § How the Allocator Runs Safely (step 10), § DeFi Reality Checks (share balance)
