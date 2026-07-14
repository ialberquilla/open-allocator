# 01-04 — CLI: `list-vaults` + `score-vault`

**Phase:** 1 — 1Tx client + read-only
**Depends on:** 01-01, 01-03, 00-05
**Status:** done

## Goal
The shareable, read-only surface (M1): discover and score vaults from the CLI, JSON out. No wallet.

## Scope / Deliverables
- `list-vaults [--chain X] [--asset USDC] [--protocol P] [--sort apy|tvl|score]` → discover (01-01),
  optionally score (01-03), emit a JSON array of vault summaries (protocol · chain · asset · APY · TVL ·
  score). Defaults to the **full** live universe.
- `score-vault --instrument-id <id>` → the full `VaultScore` with every factor's raw+normalized+weight,
  so the output is self-explaining.

## Tests
- `CliRunner` + mocked `OneTxClient`: `list-vaults` returns a JSON array; filters/sort applied.
- `score-vault` output contains the factor breakdown and validates against `vault-score.schema.json`.
- No-narrowing invocation returns all fixture protocols/chains (dynamic universe end-to-end).

## Acceptance criteria
- [x] Both commands emit one JSON value; `score-vault` output is schema-valid.
- [x] Runs with only `ONE_TX_API_URL`/`ONE_TX_API_KEY` set (no wallet/RPC needed).

## References
plan_allocator.md § Build Order M1, § Recommended CLI
