# Project Context

OpenAllocator is an agent-operated, CLI-first DeFi yield allocator. It discovers the live 1Tx instrument universe, scores yield venues transparently, builds policy-bounded allocations, and executes only after explicit approval.

This file is the shared architecture source of truth. Agent operating rules live in [AGENT_GUIDE.md](AGENT_GUIDE.md).

## Architecture

The project has two planes.

- Allocation plane: agents and humans inspect the universe, compare scored instruments, propose weights, and explain the risk tradeoffs.
- Deterministic plane: Python code in `open_allocator.core` and `open_allocator.exec` validates schemas, scores inputs, enforces policy, builds transaction plans, and prevents unsafe execution. The executor never runs agent-authored code.

The public interface is the `open-allocator` CLI. Each command emits exactly one JSON object on stdout; failures emit one JSON object on stderr and return non-zero.

## Dynamic Universe Rule

The investable universe is whatever 1Tx returns from live discovery. Do not hardcode protocol lists, chain lists, or instrument lists in allocator logic.

- New protocols, chains, and instruments should be picked up automatically by discovery.
- Static chain data is limited to RPC configuration needed for broadcast.
- Adding or changing RPC support for a chain is configuration (`RPC_URL_<chainId>`), not discovery code.
- Policy allowlists are narrowing filters over discovery results, never a replacement for discovery.
- Unknown fields are surfaced as unknown; never guess missing metrics.

## Policy Layer

Policy is block-only governance. It can reject or tighten a proposed allocation, but it cannot loosen risk limits or bypass confirmation.

The policy surface is allowed protocols, chains, asset categories, assets, and curators; caps for instrument, protocol, curator, chain, and sector weight, plus minimum TVL and maximum reward dependence; and gates for new-instrument approval, autonomous rebalance, and deploy size. JSON schemas live in [schemas/](src/open_allocator/schemas/).

Caps are ceilings on *labels*, and a label ceiling can be satisfied by holding many names that are one bet. `min_effective_positions` is the measured counterpart: a **floor** on the number of independent positions, computed from the instruments' own APY history. It fails closed — an allocation whose independence cannot be measured is rejected, not passed, and instruments too new to score are charged as fully correlated.

## Mandate Layer

A mandate is a plain-language ask plus the knobs derived from it plus a reason per knob. It is authored by a model; nothing about it is trusted on that basis.

- A derived policy may only **narrow** the baseline. `validate-mandate` rejects any loosening — ceilings up, floors down, allowlists widened, flags moved off their restricting value — and treats a dropped knob as a loosening, because absent reads as permissive rather than neutral.
- The mandate is bound to its derived policy by hash, so a rationale cannot drift away from the number it argued for.
- `version`, `wallet.mode`, and `wallet.signer` are fixed: a mandate may not touch them at all.
- `validate-mandate` reads files only — no discovery, no network. It and `check-policy` report through an `ok` field while still exiting 0.
- `drift` is the daily gate: it asks whether the book still matches the mandate, and answers "drifted" whenever a check cannot be run rather than reporting a clean result it could not verify.

## Self-Custody Execution Model

Users control the wallet. The signer is composed from three independent axes — what holds the funds (`eoa` or `safe`), how the transaction reaches the chain (`rpc` or `erc4337-paymaster`), and where the key lives (`local` or `remote`).

- 1Tx transaction builders produce calldata.
- The wallet signs and broadcasts through configured RPC endpoints.
- How gas is paid depends on the submission axis:
  - `rpc` — the wallet pays native gas on every chain it signs on: source chains for deposits, the position's chain for exits.
  - `erc4337-paymaster` — gas is paid in USDC by the smart account, so no chain needs native gas. See [docs/gasless-execution.md](docs/gasless-execution.md).
- USDC is sourced from whichever chain the wallet is funded on; the destination chain is encoded in the `instrumentId` and 1Tx bridges (CCTP) automatically. See [docs/funding-and-bridging.md](docs/funding-and-bridging.md).
- Execution commands are gated by `--confirm` or explicit `--unsafe` / `--autonomous` flags.

## Safety Invariants

- APY is descriptive, not predictive.
- Allocation decisions must be explainable from visible inputs.
- Policy violations abort before any transaction is built or signed.
- Agents must announce exact vaults, chains, amounts, risks, and expected transactions before asking for confirmation.
- ERC-4626 exits use share balances, not USDC value guesses.
- A smart account submits a plan's steps for one chain as a single atomic operation. This is not an optimisation: the paymaster charges after execution, so batching is what lets an exit pay its gas out of what it just redeemed. Splitting a plan back into one operation per step breaks gasless exits.
