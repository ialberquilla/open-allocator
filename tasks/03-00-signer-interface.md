# 03-00 — Signer interface + LocalEoaSigner

**Phase:** 3 — Execution (self-custody)
**Depends on:** 00-02
**Status:** done

## Goal
The one seam that carries "EOA now → remote enclave → Safe later": a tiny signer interface with a
local-key backend.

## Scope / Deliverables
- `src/open_allocator/exec/signer.py`:
  - `Signer` protocol: `address() -> str` and `send(tx: TxStep, rpc_url: str) -> Receipt`.
  - `LocalEoaSigner` (`eth-account`): loads the key from config (00-02), signs the raw
    `{to,data,value,chainId}` and broadcasts via the chain RPC (03-01), returns the receipt.
  - Stubs/registration points for `RemoteSigner` (05-00) and `SafeSigner` (05-01) — same interface.
- Add `web3` + `eth-account` deps here (exact-pinned).
- Key never logged; `LocalEoaSigner.__repr__` redacts.

## Tests
- Use `eth-tester` (in-proc EVM) — no real RPC/key:
  - `address()` matches the account derived from a test key.
  - `send()` signs and broadcasts a simple tx; receipt returned; nonce handled across sequential sends.
  - A failed/reverted tx surfaces a typed error, not a silent pass.
- Interface conformance test that any `Signer` implements `address` + `send`.

## Acceptance criteria
- [x] Swapping signer backends needs no change in the executor (03-02).
- [x] No secret material appears in logs/reprs (asserted).

## References
plan_allocator.md § Pluggable Signer interface, § Wallet & Execution Model (v1)
