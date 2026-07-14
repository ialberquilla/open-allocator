# 03-02 — Executor (build → sign → broadcast → poll)

**Phase:** 3 — Execution (self-custody)
**Depends on:** 03-00, 03-01, 01-00, 02-01
**Status:** done

## Goal
Execute an approved `Allocation` as on-chain deposits via the 1Tx builders + the signer — self-custody,
wallet pays gas.

## Scope / Deliverables
- `src/open_allocator/exec/execute.py`:
  - `execute_allocation(client, signer, allocation, policy, confirm=False) -> ExecutionReport`.
  - Re-run `policy.check` (02-01) and **abort on `ok=False`** before building anything.
  - For each leg: `POST /transactions/buy` → ordered `{to,data,value,chainId}` steps → `signer.send` each
    **in order** (approve before router call) → collect receipts.
  - **Gas preflight**: check the wallet holds native gas on each target chain (via RPC) + USDC; warn/abort
    before starting so a book isn't left half-executed.
  - Cross-chain (CCTP) buys: `confirming_source`/pending is **success-in-progress**, surfaced, not failed.
  - Idempotency guard: a retried leg must not double-submit a buy.
  - Without `confirm=True`, return the `TxPlan` and do nothing on-chain.

## Tests
- `eth-tester` + mocked `OneTxClient`:
  - Happy path: approve→buy order preserved; receipts collected; `ExecutionReport` schema-valid.
  - Policy violation aborts **before** any signer call (spy asserts zero broadcasts).
  - Gas preflight failure aborts before the first send.
  - Retry after a mid-book failure resumes without re-submitting completed legs (idempotency).
  - Cross-chain pending is reported as in-progress, not error.
- `@pytest.mark.integration`: one tiny real mainnet deposit on Base (M3 gate), skipped without creds/funds.

## Acceptance criteria
- [x] Nothing broadcasts without `--confirm` and a passing policy check.
- [x] A partially-failed book is safely resumable; no double spends.

## References
plan_allocator.md § Wallet & Execution Model (v1), § Build Order M3, § Gotchas
