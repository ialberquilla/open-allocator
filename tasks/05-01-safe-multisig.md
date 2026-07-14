# 05-01 — SafeSigner (multisig)

**Phase:** 5 — Hardening (v2)
**Depends on:** 03-00, 02-01
**Status:** done

## Goal
Treasury-grade governance: the agent *proposes*, humans co-sign, the Safe executes and pays gas — using
the **same** 1Tx calldata as v1 (signer swap, not an execution rewrite).

## Scope / Deliverables
- `src/open_allocator/exec/safe_signer.py` (`safe-eth-py`) implementing `Signer`:
  - wrap each `{to,data,value}` step as a Safe transaction; propose to the Safe Transaction Service;
    return a pending id; execute once threshold signatures are collected.
- **On-chain policy guard**: a Safe guard/module enforcing the `policy.yaml` allowlist on-chain, so
  co-signers are enforcing not merely trusting (design + integration notes; contract may be out of scope).
- Add `safe-eth-py` (exact-pinned).

## Tests
- The Safe tx wraps the identical calldata the EOA path would sign (byte-equal `to/data/value`).
- Propose → (mock) collect signatures → execute flow; below-threshold cannot execute.
- Guard rejects a tx outside policy (unit test against the guard logic / mocked module).

## Acceptance criteria
- [x] v1→v2 is a signer swap: executor + tx-builders unchanged.
- [x] A single leaked co-signer key cannot move funds alone.

## References
plan_allocator.md § Wallet & Execution Model (v2), § Enforcement tiers (hard)
