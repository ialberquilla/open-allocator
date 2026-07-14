# 00-01 — Python project setup (uv, pinned, supply-chain hardened)

**Phase:** 0 — Foundations
**Depends on:** —
**Status:** done

## Goal
Stand up the `open_allocator` package with **uv**, exact pins, and a rolling `exclude-newer` cooldown.

## Scope / Deliverables
- `pyproject.toml` (hatchling backend) per the template below.
- `uv.lock` created (`uv lock`) and kept in the tree.
- Layout: `src/open_allocator/` (importable) + `tests/`.
- `uv sync --frozen` works from a clean checkout.
- Dev tooling: `ruff` (E, F, I) + `pytest`, exact-pinned.

## pyproject.toml template
`==<pin>` = "pin the exact resolved version via `uv add`", not a hand-written number. Only **foundation**
deps go in now; heavier deps enter via later tasks.

```toml
[build-system]
requires = ["hatchling>=1.27.0"]
build-backend = "hatchling.build"

[project]
name = "open-allocator"
version = "0.1.0"
description = "Agent-operated, policy-bounded DeFi yield allocator on 1Tx (CLI)."
requires-python = ">=3.12,<3.13"
dependencies = [
  # --- foundation (add now) ---
  "httpx==<pin>",            # 1Tx REST client
  "pydantic==<pin>",         # domain models (Vault, Allocation, TxPlan, Policy)
  "pydantic-settings==<pin>",# env/config loading
  "pyyaml==<pin>",           # policy.yaml + workflows/*.yaml
  "jsonschema==<pin>",       # artifact/policy schema validation
  "typer==<pin>",            # CLI (JSON-out commands)
  # --- added later, by their own tasks (do NOT add now) ---
  # pandas, numpy   -> 01-03 / 02-00 (scoring, allocation)
  # web3, eth-account -> 03-00 / 03-02 (signer, broadcast)
  # scipy / cvxpy   -> 02-00 only if constrained optimization is used
  # safe-eth-py     -> 05-01 (multisig)
]

[dependency-groups]
dev = [
  "pytest==<pin>",
  "ruff==<pin>",
  "eth-tester==<pin>",       # in-proc EVM for execution tests (Phase 3+)
]

[project.scripts]
open-allocator = "open_allocator.cli:main"

[tool.uv]
default-groups = ["dev"]
exclude-newer = "7 days"     # rolling cooldown; committed uv.lock keeps installs reproducible

[tool.ruff]
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "I"]

[tool.hatch.build.targets.wheel]
packages = ["src/open_allocator"]

[tool.pytest.ini_options]
testpaths = ["tests"]
markers = ["integration: hits live 1Tx API / RPC; skipped without creds"]

[tool.hatch.build.targets.sdist]
include = ["src/open_allocator", "pyproject.toml", "uv.lock"]
```

## Tests
- `tests/test_smoke.py`: `import open_allocator` succeeds; `open_allocator.__version__` present.
- CI-style check documented: `uv sync --frozen && uv run ruff check && uv run pytest`.

## Acceptance criteria
- [x] `uv sync --frozen` succeeds on a clean checkout.
- [x] Every dependency is exact-pinned; `uv.lock` present.
- [x] `exclude-newer = "7 days"` set; `requires-python` pinned to 3.12.
- [x] `uv run pytest` green (smoke test).

## References
plan_allocator.md § v1 Scope (Stack), § Proposed Repo Layout; user-provided pyproject sample
