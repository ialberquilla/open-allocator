# 03-01 — Chain → RPC registry

**Phase:** 3 — Execution (self-custody)
**Depends on:** 00-02
**Status:** done

## Goal
Resolve a chain id to an RPC URL for broadcasting — the net-new config vs 1tx-skill (which relied on
Circle to broadcast).

## Scope / Deliverables
- `src/open_allocator/exec/chains.py`:
  - A default chain-id → public-RPC registry for the chains 1Tx serves.
  - `rpc_url(chain_id)` — env override (`RPC_URL_<chainId>`, via 00-02) wins, else the default, else
    `None` (chain discoverable + scorable but **not executable** until an RPC is configured).
  - `chain_name(chain_id)` for display; never used to gate discovery.
- Document that adding an RPC for a new chain is config, not code.

## Tests
- Override precedence: env `RPC_URL_8453` beats the default; unknown chain → `None`.
- A `None` RPC makes execution (03-02) raise a clear "no RPC for chain X" error, not a crash mid-flight.
- No chain enum gates discovery (a chain with no RPC still lists/scored).

## Acceptance criteria
- [x] Every 1Tx-served chain either has a default RPC or is clearly flagged not-executable.
- [x] Overrides work; missing RPC is a friendly, early error.

## References
plan_allocator.md § Prerequisites to Run (RPC), § No Hardcoded Universe
