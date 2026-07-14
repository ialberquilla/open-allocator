# 00-05 — CLI skeleton (Typer, JSON-out)

**Phase:** 0 — Foundations
**Depends on:** 00-01
**Status:** done

## Goal
The `open-allocator` entrypoint and the shared command conventions every later command follows.

## Scope / Deliverables
- `src/open_allocator/cli.py` with `main()` (the `[project.scripts]` target) using Typer.
- A shared decorator/helper that:
  - serializes a command's return value as **one JSON object** to stdout,
  - routes errors to stderr with a non-zero exit + a JSON `{ "error": ... }`,
  - enforces the planning/execution split: execution commands require `--confirm`
    (or explicit `--unsafe`/`--autonomous`), otherwise they emit the plan and exit 0 without spending.
- Registered no-op stubs for the full command surface (from plan § Recommended CLI): `wallet-status`,
  `list-vaults`, `score-vault`, `build-allocation`, `simulate`, `check-policy`, `build-tx`, `execute`,
  `positions`, `rebalance`, `withdraw` — each returns `{"status":"not_implemented"}` until its task lands.

## Tests
- Invoke each command via Typer's `CliRunner`; assert stdout is a single parseable JSON object.
- A raising command exits non-zero with a JSON error on stderr.
- An execution stub without `--confirm` returns a plan and does **not** call any executor (assert via a spy).

## Acceptance criteria
- [x] `open-allocator --help` lists every planned command.
- [x] JSON-out + error + confirm-gate conventions hold uniformly (one helper, tested once).

## References
plan_allocator.md § Recommended CLI, § CLI rules
