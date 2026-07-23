# Project Context

`open-allocator` is an agent-operated, CLI-first DeFi yield allocator. It discovers the live 1Tx instrument universe, scores yield venues transparently, builds policy-bounded allocations, and executes only after explicit approval.

This file is the shared architecture source of truth. Agent operating rules live in [AGENT_GUIDE.md](AGENT_GUIDE.md).

## Architecture

The project has two planes.

- Allocation plane: agents and humans inspect the universe, compare scored instruments, propose weights, and explain the risk tradeoffs.
- Deterministic plane: Python code in `open_allocator.core` validates schemas, scores inputs, enforces policy, builds transaction plans, and prevents unsafe execution.

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

The intended policy surface includes allowed protocols, chains, assets, and curators plus caps for instrument, protocol, curator, chain, TVL, LLTV, reward dependence, and deploy size. JSON schemas live in [schemas/](schemas/).

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
