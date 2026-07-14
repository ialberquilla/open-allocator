<div align="center">

# open-allocator

**Agent-operated, policy-bounded DeFi yield allocator on the [1Tx](https://1tx.com) API — run as a CLI.**

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python 3.12](https://img.shields.io/badge/Python-3.12-blue.svg)](pyproject.toml)
[![Tests](https://img.shields.io/badge/tests-338%20passing-brightgreen.svg)](#development)
[![Status: alpha](https://img.shields.io/badge/status-alpha-orange.svg)](#)

[Install](#install) · [Commands](#commands) · [Agent Operation](#agent-operation) · [Safety](#safety)

</div>

`open-allocator` is an open-source, agent-operated DeFi yield allocator built on the [1Tx](https://1tx.com) API and run as a CLI. It discovers the live 1Tx instrument universe, scores yield venues transparently, builds policy-bounded allocations, and executes through a self-custody wallet — only after explicit confirmation.

This is an end-user allocator, not Morpho's curator-side Allocator role.

> APY is descriptive, not predictive. Rates move, rewards end, liquidity changes, and smart-contract risk remains. Every metric here is yield-path only — never principal, depeg, bridge, or contract-loss risk.

## Why It Exists

- **Dynamic discovery** — no hardcoded protocol or chain universe; new networks and instruments are picked up automatically from 1Tx.
- **Transparent scoring** — every allocation and risk score maps to visible inputs. Unknown fields stay `Unknown` instead of being guessed.
- **Policy-bounded execution** — allowlists and caps block unsafe proposals before signing. Policy can only tighten, never loosen.
- **CLI-first** — agents and humans use the same JSON-out commands. Every command prints one JSON object to stdout.
- **Self-custody** — the wallet signs and broadcasts its own transactions and pays its own gas.

## How It Works

The system has two planes:

- **Research / decision plane (agentic).** Agents and humans discover the universe, compare scored instruments, screen by risk, backtest, and propose weights — freely and read-only.
- **Execution plane (deterministic).** Python in `open_allocator.core` and `open_allocator.exec` validates schemas, enforces policy, builds transaction plans, and blocks unsafe execution. The executor never runs agent-authored code.

A decision leaves the research plane only as a **validated artifact** — explicit weights or a named+parameterized strategy — and must pass `check-policy` before any transaction is built.

## Install

Requires Python `>=3.12,<3.13` and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run open-allocator --help
```

## Configure

Copy [.env.example](.env.example) to `.env` (or run under `dotenvx`) and set:

| Variable | Purpose |
| --- | --- |
| `ONE_TX_API_URL`, `ONE_TX_API_KEY` | 1Tx API endpoint and key. |
| `SIGNER_MODE` | `local-eoa` (default), `remote`, or `safe`. |
| `ONE_TX_PRIVATE_KEY` | Funded EOA key; required only for `local-eoa`. |
| `RPC_URL_<chainId>` | Override the built-in public RPC for a chain (required for broadcast). |
| `ONE_TX_SLIPPAGE_BPS`, `ONE_TX_FAST_TRANSFER` | 1Tx transaction options. |

Secrets may be dotenvx-encrypted at rest; this package does not decrypt `.env` itself. At-rest encryption does not hide values from a process that can read the decrypted runtime environment — a leaked key is only truly out of an agent's reach with a `remote` signer enclave or a Safe multisig.

Governance lives in [policy.yaml](policy.yaml) — the allocator's constitution. It defines `allowed` axes (protocols, chains, `asset_categories`, `stablecoin_only`, assets, curators), `caps` (per-instrument / protocol / curator / chain weight, min TVL, max LLTV, max reward dependence), and `gates` (new-instrument approval, autonomous rebalance, max deploy per cycle). Allowlists are narrowing filters over discovery (`null` = all); they never replace discovery.

## Commands

Every command emits one JSON object on stdout; errors emit one JSON object on stderr and exit non-zero.

**Discovery & read-only**

```bash
uv run open-allocator wallet-status                 # address, USDC + native-gas readiness per chain
uv run open-allocator list-vaults --sort score      # discover + score the live universe
uv run open-allocator score-vault --instrument-id <id>
uv run open-allocator positions                     # reconcile current on-chain holdings
```

**Analysis & planning** (read-only)

```bash
uv run open-allocator screen --min-sharpe 1.0 --max-drawdown 0.1   # advisory risk narrowing
uv run open-allocator build-allocation --amount 10000 --risk balanced
uv run open-allocator simulate  --allocation allocation.json       # forward blended-APY / concentration
uv run open-allocator backtest  --allocation allocation.json       # daily-compounded NAV vs benchmark
uv run open-allocator check-policy --allocation allocation.json    # block-only policy gate
```

`build-allocation` supports risk presets, allocation strategies (`--strategy`), advisory screening flags, `--exclude`, pinned weights (`--pin id=weight`), and a full [allocation-spec](schemas/allocation-spec.schema.json) via `--spec`. Available strategies: `score_weighted` (default), `equal_weight`, `risk_parity`/`inverse_vol`, `core_satellite`, and `sleeves`/`ladder`.

**Execution** (confirmation-gated)

```bash
uv run open-allocator build-tx  --allocation allocation.json       # calldata plan (dry run)
uv run open-allocator execute   --allocation allocation.json --confirm
uv run open-allocator rebalance --current positions.json --target allocation.json --confirm
uv run open-allocator withdraw  --position <id> --confirm
```

Without `--confirm`, execution commands return a plan / dry-run report and broadcast nothing. `execute --confirm` is the spend path. Exits are share-denominated (ERC-4626 shares, not USDC guesses).

## Agent Operation

Agents start with [AGENT_GUIDE.md](AGENT_GUIDE.md), the operating contract for this repository. Shared architecture and invariants are in [PROJECT_CONTEXT.md](PROJECT_CONTEXT.md); the original implementation plan is in [plan_allocator.md](plan_allocator.md).

Stage skills and workflow graphs describe how to drive the CLI and review artifacts:

- Skills: [discover](skills/discover.md), [score](skills/score.md), [build-allocation](skills/build-allocation.md), [agentic-allocation](skills/agentic-allocation.md), [execute-with-1tx](skills/execute-with-1tx.md), [rebalance](skills/rebalance.md), [withdraw](skills/withdraw.md), plus [risk-review](skills/meta/risk-review.md) and [checkpoint-protocol](skills/meta/checkpoint-protocol.md).
- Workflows: [allocate](workflows/allocate.yaml), [rebalance](workflows/rebalance.yaml), [withdraw](workflows/withdraw.yaml).
- Artifact schemas: [schemas/](schemas/).

## Safety

- Never sign, broadcast, rebalance, or withdraw without first announcing the exact action (wallet, chains, instruments, amounts, gas assets, policy result, failure modes) and obtaining confirmation.
- Policy violations abort before any transaction is built or signed. `--unsafe` / `--autonomous` are not shortcuts — use them only when policy and task explicitly require it.
- Keep private keys out of logs and artifacts.
- Treat high APY as a risk input, not a promise.

## Development

```bash
uv run ruff check
uv run pytest            # 338 passed, 4 integration tests skipped without live creds
```

Unit tests mock 1Tx over `httpx.MockTransport` and the chain over `eth-tester`; no live network is touched. Live API/RPC tests are opt-in behind `@pytest.mark.integration` and explicit credential gates.

Layout: `src/open_allocator/core` (allocation, scoring, policy, risk metrics, strategies, screening, backtest, positions, checkpoints), `src/open_allocator/exec` (1Tx client, signers, executor, RPC registry), `schemas/` (JSON artifact contracts), `skills/` + `workflows/` (agent instruction layer), `docs/` (reference notes).

The live 1Tx risk-factor field refresh remains credential-gated; see [docs/onetx-analysis-fields.md](docs/onetx-analysis-fields.md).
