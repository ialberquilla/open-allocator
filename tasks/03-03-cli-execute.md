# 03-03 — CLI: `wallet-status` + `build-tx` + `execute`

**Phase:** 3 — Execution (self-custody)
**Depends on:** 03-02, 00-05
**Status:** done

## Goal
The execution surface (M3): inspect the wallet, preview the tx plan, and (with confirmation) deposit.

## Scope / Deliverables
- `wallet-status` → signer address + USDC & native-gas balances per chain (RPC + 1Tx balances); flags any
  chain lacking gas/RPC. No spend.
- `build-tx --allocation allocation.json` → the ordered `TxPlan` (per-leg to/chain/amount/kind), JSON,
  **no execution**.
- `execute --allocation allocation.json --confirm` → run 03-02; stream/emit an `ExecutionReport`.
  Announce-before-execute: without `--confirm`, print the plan + expected effects and exit 0.

## Tests
- `CliRunner`: `wallet-status` JSON includes per-chain balances + not-executable flags.
- `build-tx` emits a schema-valid `tx-plan`; calls no signer (spy).
- `execute` without `--confirm` performs no broadcast; with `--confirm` (eth-tester + mocked client) it does.

## Acceptance criteria
- [x] Confirm-gate + announce-before-execute enforced at the CLI layer, tested.
- [x] `build-tx` and `execute --confirm` share the same plan (no divergence).

## References
plan_allocator.md § Recommended CLI, § Decision Communication (announce-before-execute)
