# 03-04 — Mainnet execution proof (M3 gate)

**Phase:** 3 — Execution (self-custody)
**Depends on:** 03-03
**Status:** done

## Goal
Since there's no testnet, prove one **tiny real deposit** end-to-end on Base before building rebalance/exit
on top. Everything after M3 is gated on this.

## Scope / Deliverables
- A documented, funded-wallet runbook: fund an EOA with a few USDC + a little native gas on Base,
  set `ONE_TX_PRIVATE_KEY` (dotenvx-encrypted) + `RPC_URL_8453`, a `policy.yaml` with a low
  `max_deploy_per_cycle_usd`.
- Run: `list-vaults` → `build-allocation --amount <tiny>` → `check-policy` → `simulate` →
  `build-tx` → `execute --confirm`. Capture the `ExecutionReport` + tx hashes.
- Record outcomes/gotchas in `docs/m3-proof.md` (cross-chain timing, share-balance behavior).

## Tests
- The `@pytest.mark.integration` deposit test from 03-02 passes against Base with real (tiny) funds.
- A dry `execute` (no `--confirm`) shows the exact plan that the confirmed run then executes (parity).

## Acceptance criteria
- [x] One real Base deposit confirmed on-chain within the spend cap.
- [x] `docs/m3-proof.md` written; Phase 4 unblocked.

## References
plan_allocator.md § Build Order M3 (no testnet), § Prerequisites to Run
