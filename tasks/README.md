# open-allocator — Implementation Tasks

Numbered, independently-implementable tasks derived from [`../plan_allocator.md`](../plan_allocator.md).
Each task ships **its own tests** and is not "done" until `uv run pytest` and `uv run ruff check` are
green for it.

## Phases

| Phase | Milestone (plan) | Tasks |
|---|---|---|
| 0 — Foundations | M0 scaffold | `00-01` … `00-06` |
| 1 — 1Tx client + read-only | M1 | `01-00` … `01-04` |
| 2 — Allocation + policy | M2 | `02-00` … `02-04` |
| 3 — Execution (self-custody) | M3 | `03-00` … `03-04` |
| 4 — Rebalance & exit | M4 | `04-00` … `04-03` |
| 5 — Hardening (v2) | M5 | `05-00` … `05-03` |

Work phases in order; within a phase respect each task's **Depends on**.

## Global conventions (apply to every task)

**Dependencies (uv, supply-chain hardened)**
- `uv` manages everything. Add deps with `uv add <pkg>==<ver>` — **exact `==` pins only**, never ranges.
- `uv.lock` is the source of truth for reproducible installs and is kept in the working tree.
- `pyproject.toml` sets `exclude-newer = "7 days"` — a **rolling cooldown**: nothing published in the
  last 7 days can enter a resolve, so a freshly-compromised release is quarantined. The committed lock
  still pins exact versions+hashes, so installs stay byte-reproducible. Bumping a dependency is a
  deliberate, reviewed `uv add`/`uv lock` — never blind.
- `requires-python = ">=3.12,<3.13"` for reproducible resolves.
- Add **only foundation deps** at setup (`00-01`); every heavier dep (`web3`, `pandas`, `scipy`, …)
  enters via the task that first needs it, exact-pinned.

**Testing (every task)**
- `pytest` unit tests live in `tests/`, mirroring the package path.
- **No live network in unit tests.** Mock 1Tx HTTP with `httpx.MockTransport`; mock/broadcast on-chain
  with `eth-tester` (a local in-proc EVM) — never a real RPC or real key.
- One **opt-in integration test** module may hit the real 1Tx API and a real RPC when creds are present;
  mark it `@pytest.mark.integration` and skip by default (no creds → skip, never fail).
- Determinism: given fixed inputs, scoring/allocation/policy produce identical output (assert exact).

**CLI**
- Every command prints **one JSON object** to stdout; errors → stderr + non-zero exit.
- Planning is separate from execution; any spend requires `--confirm` (or explicit `--unsafe/--autonomous`).

**Repo**
- No git yet. No web UI. No notebooks. CLI-only.
- Never hardcode chain/protocol enums — discover from the live 1Tx response (`plan § No Hardcoded Universe`).

## Status legend
`todo` · `in_progress` · `blocked` · `done`. All tasks start `todo`.
