# 05-00 — RemoteSigner (agent-can't-see-the-key)

**Phase:** 5 — Hardening (v2)
**Depends on:** 03-00
**Status:** done

## Goal
A signer backend where the key lives in an enclave and only signatures return — so a shell-capable agent
never holds key material.

## Scope / Deliverables
- `src/open_allocator/exec/remote_signer.py` implementing the `Signer` interface (03-00) against a remote
  provider (Turnkey / Privy / cloud KMS — pick one; keep provider behind a thin adapter).
  - `address()` from the provider; `send()` builds the tx, requests a signature, broadcasts via RPC.
  - Leverage the provider's own policy engine (spend/allowlist) as an independent second boundary.
- Config additions (00-02): provider creds; no raw private key present.

## Tests
- Adapter mocked: `send()` requests a signature and never has access to raw key bytes (assert interface
  surface — no key field exists on the signer).
- Interface conformance with 03-00 (drop-in for `LocalEoaSigner`).
- Provider-policy rejection surfaces as a typed error.

## Acceptance criteria
- [x] Swappable with `LocalEoaSigner` with zero executor changes.
- [x] No code path exposes the raw key to the process/agent.

## References
plan_allocator.md § Key management, § Pluggable Signer interface
