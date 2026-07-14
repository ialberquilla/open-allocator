# 01-01 — Dynamic universe discovery

**Phase:** 1 — 1Tx client + read-only
**Depends on:** 01-00
**Status:** done

## Goal
Build the candidate universe from **whatever 1Tx returns** — no hardcoded chain/protocol enums — then
narrow by policy filters.

## Scope / Deliverables
- `src/open_allocator/core/universe.py`:
  - `discover(client, policy=None) -> list[Vault]`: fetch `GET /instruments`, map each into a `Vault`,
    grouping by the response's `protocol`/`chainId` fields (derived, never enumerated in code).
  - Optional narrowing by policy `allowed.protocols/chains/assets` (None = all) and `caps.min_instrument_tvl_usd`.
  - Surfaces new protocols/chains automatically; unknown fields → `None`/`Unknown`, never dropped silently.
- Expose the distinct protocols/chains actually seen (for CLI display + concentration checks).

## Tests
- A fixture instrument list containing a **novel** protocol + chain is discovered without code changes.
- Policy filters narrow correctly; `None` allowlist = pass-through (all).
- `min_instrument_tvl_usd` excludes thin pools.
- Missing risk fields yield `Unknown`, not fabricated values or exceptions.

## Acceptance criteria
- [x] No protocol/chain literal appears in the module (assert via a grep test).
- [x] Adding an instrument on an unseen chain/protocol needs zero code changes.

## References
plan_allocator.md § No Hardcoded Universe
