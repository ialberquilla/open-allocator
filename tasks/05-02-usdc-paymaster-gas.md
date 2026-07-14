# 05-02 — ERC-4337 USDC-paymaster gas backend (optional)

**Phase:** 5 — Hardening (v2)
**Depends on:** 03-00, 05-01
**Status:** done

## Goal
Kill the "fund native gas on every chain" chore **without giving up self-custody or the Safe**: pay gas
in USDC from your own funds via a paymaster. Only build this if gas ergonomics become the real pain
(recommended over Circle — see plan § Gas strategy).

## Scope / Deliverables
- A `Signer`/execution backend that routes txs as ERC-4337 userOps through a paymaster accepting USDC,
  with `Safe4337Module` for the multisig case. Provider behind a thin adapter (Pimlico/Alchemy/Candide).
- Config: paymaster/bundler endpoints + creds (00-02).
- Preserve the invariant: same 1Tx calldata, just a different submission path; policy check still runs first.

## Tests
- Adapter mocked: a deposit becomes a userOp with a USDC-paying paymaster; no native-gas balance required
  in the flow (assert preflight does not demand native gas in this mode).
- Falls back / errors clearly if the paymaster rejects or a chain is unsupported.
- Policy check still gates before submission.

## Acceptance criteria
- [x] Deposits execute with zero native-gas balance, funded by USDC.
- [x] Composes with `SafeSigner`; self-custody preserved.

## References
plan_allocator.md § Gas strategy — deferred behind the Signer seam
