# Agent Guide

This is the operating contract for agents and humans working in this repository. Project architecture and domain invariants are in [PROJECT_CONTEXT.md](PROJECT_CONTEXT.md).

## Operating Rules

- Treat the CLI as the source of truth for repo operations.
- Keep allocation and risk logic in Python code, not hidden prompt instructions.
- Prefer JSON artifacts validated by [schemas/](src/open_allocator/schemas/) over prose handoffs.
- Never hardcode protocol, chain, or instrument universes; discover from 1Tx and narrow by policy.
- Do not sign, broadcast, rebalance, or withdraw without first announcing the exact action and obtaining the required confirmation.
- Frame APY as descriptive, not predictive.
- Never hand-derive a policy. A model-authored policy is legitimate only as a mandate: derive it per [skills/mandate.md](src/open_allocator/skills/mandate.md), give every moved knob a `because` naming the measurement, and gate it through `validate-mandate` against the baseline. A derived policy may only narrow.
- Never split a chain's plan steps into separate smart-account operations; they are batched deliberately (see [PROJECT_CONTEXT.md](PROJECT_CONTEXT.md) safety invariants).

## Command Inventory

The registered CLI commands are exactly:

<!-- command-inventory:start -->
- `wallet-status`
- `safe-address`
- `list-vaults`
- `score-vault`
- `build-allocation`
- `screen`
- `simulate`
- `backtest`
- `check-policy`
- `validate-mandate`
- `drift`
- `build-tx`
- `execute`
- `positions`
- `rewards`
- `rebalance`
- `withdraw`
- `loop-close`
- `bridge`
<!-- command-inventory:end -->

Every command must print one JSON object to stdout. Errors must print one JSON object to stderr and exit non-zero. Execution commands must return a plan-required response unless `--confirm`, `--unsafe`, or `--autonomous` is explicitly supplied.

## Allocation Loop

Use this loop for deposits and new books.

1. Load policy and signer configuration. If a mandate governs the book, run
   `drift` **first** — it is the daily gate, and if `drifted` is false, stop here
   rather than running anything expensive. It reports drift when a check cannot
   be run, so a `false` is a real answer.
2. Run `wallet-status` and check wallet address, USDC, and gas readiness per chain.
   Each row names the gas model it was judged under in `gas_mode`: `native` reports a
   native balance, `usdc_paymaster` reports none to hold — gas is paid in USDC by the
   smart account, and a chain with no USDC at all is still executable because an exit
   funds itself ([docs/gasless-execution.md](docs/gasless-execution.md)). Read
   `executable` and `not_executable_reasons`, never a native balance alone.
3. Run `list-vaults` to discover the full live 1Tx universe.
4. Run `score-vault` over candidate instruments and keep unknown fields visible.
5. Run `build-allocation` to create a weighted, policy-conformant proposal.
   Pick the construction rule deliberately — `--strategy` is a real choice, not a
   default to accept (see [Choosing a Construction Rule](#choosing-a-construction-rule)).
6. Run `simulate` to inspect blended APY, concentration, measured
   diversification, and failure-cost flags. Read `diversification.effective_positions`
   (independent-bet count), `median_tail_lift`, and `unmeasured_weight_bps`
   alongside label concentration — a book can satisfy every label cap and still
   be one bet.
7. Run `check-policy`; stop on any violation. `min_effective_positions` is a
   **floor** and it fails closed: an allocation whose independence cannot be
   measured is rejected, not passed. Do not route around it by loosening the
   policy — rebuild with a construction rule that earns the floor.
8. Announce vaults, chains, amounts, risks, expected transactions, and gas requirements.
9. Wait for human approval.
10. Run `build-tx`, then `execute --confirm` only after approval.
11. Run `positions` and retain the checkpoint/allocation-log artifacts.

## Choosing a Construction Rule

`--strategy` decides what the book optimizes for, and the trade-off between yield
and independence is the user's to set — never silently yours. If the task does
not say, ask, or build more than one and present the measured difference.

| Ask | Strategy | Params |
| --- | --- | --- |
| Highest scored yield | `score_weighted` (default) | — |
| Most independent book | `decorrelated` | `top_n`, `unknown_correlation` |
| A declared risk budget | `sleeves` / `ladder` | `tiers` — **what a mandate derives into** |
| No opinion | `equal_weight` | — |
| Volatility-balanced | `risk_parity` / `inverse_vol` | — |
| Core plus bets | `core_satellite` | `core_weight`, `core_count`, `core_selector`, `satellite_selector` |

Pass params as repeatable `--strategy-param key=value` (JSON scalar values).
Sleeve tiers are `{name, min_score, max_score, weight, strategy?}` keyed on the
composite score, so a sleeve is a computed quality band, not a label.

Never present an independence number as a property of the library. It is a
property of the shelf on the day it was measured — regenerate it per run.
Full surface and the known limits of every metric: [docs/capabilities.md](docs/capabilities.md).

## Rebalance Loop

1. Run `positions` and compare current holdings to the target allocation.
2. Build deltas only; do not redeploy unchanged positions.
3. Run scoring, simulation, and `check-policy` for the proposed deltas.
4. Announce the exact exits, deposits, chains, amounts, risks, and expected transactions.
   For a calldata plan, announce the deposit amounts the dry run actually built —
   a buy may be sized down to what its chain holds after its sells — not the target's.
5. Wait for approval, then run `rebalance --confirm`.

## Withdraw Loop

1. Identify the position and its share balance.
2. Check liquidity, withdrawal constraints, gas, and any cross-chain timing. A gasless
   exit funds itself from the redeem and needs nothing pre-positioned on the chain,
   but its proceeds must exceed its gas.
3. Dry-run it: `withdraw --position <id> [--amount <usd>]` without `--confirm` builds the plan and sends nothing.
4. Announce the share amount, expected destination asset, chain, risks, and transactions.
5. Wait for approval, then run `withdraw --confirm`.

## Loop Close Loop

Use this to take one levered loop off. `withdraw` refuses a loop — its collateral is
pledged against its debt — and `rebalance` reaches a close only as a consequence of a
target allocation that no longer holds the leg. This closes the loop and nothing else,
so de-risking does not require first deciding where the proceeds go.

1. Identify the loop id; it must still be listed by the loop screen.
2. Dry-run it: `loop-close --loop <loop id>` without `--confirm` builds the plan,
   announces it, and sends nothing.
3. Announce the announcement's `account_config` and `pool_positions`. A close that
   switches the account's e-mode re-prices every other position in that pool, and the
   dry run refuses outright when those positions cannot be read.
4. Wait for approval, then run `loop-close --confirm`. The bundle goes out as one
   atomic operation; a signer that cannot batch is refused, because a half-unwound
   loop is a levered position with its collateral withdrawn.

## Bridge Loop

Use this to move the Safe's USDC to another chain without depositing, e.g. to fund a
loop, which is built same-chain only. Never deposit and withdraw to move funds.

1. Run `wallet-status` and pick the source chain that holds the USDC.
2. Dry-run it: `bridge --from <chain id> --to <chain id> --amount <usdc>`. The plan's
   burn amount may be sized down to leave the source paymaster's charge.
3. Announce the source and destination chains, the burned amount, the `funding` rows,
   and that what lands is the attested mint less Circle's fee and the destination
   paymaster's charge.
4. Wait for approval, then run `bridge --confirm` with the same arguments, and rerun it
   until its `bridges` state is `completed`. Pass `--ref` only to start a second
   transfer of the same amount ([docs/funding-and-bridging.md](docs/funding-and-bridging.md)).

## Confirmation Discipline

Announce before execute. A valid execution announcement includes the wallet, source and destination chains, instruments, amounts, calldata source, policy result, expected gas assets, and failure modes. For a calldata plan it also includes the dry run's `funding` rows — each token the plan spends, the balance held, and any shortfall, including the paymaster's maximum USDC charge. A calldata leg that bridges (a `bridge` bundle) is announced as such: source and destination chain, burned amount, and that its deposit is sized only after Circle attests; rerun the same `execute --confirm` until its `bridges` state is `completed`. A levered leg is announced from the report's `loops` entry — collateral, debt, equity, leverage, modelled and simulated health factor, depeg buffer, kill-switch price, and every other position in the pool when the bundle changes the account's configuration; it cannot be confirmed while that pool-wide impact is unknown.

`--unsafe` and `--autonomous` are not shortcuts. Use them only when the policy and task explicitly require them and the bounds are documented before execution.

## Instruction Layer

Use these stage skills and workflow graphs for agent-operated runs. They describe how to call the CLI and review artifacts; deterministic allocation, scoring, policy, and execution logic remains in Python code.

- [docs/capabilities.md](docs/capabilities.md) — the knob surface: every strategy and its params, ceilings vs. the floor, what `simulate` reports, and the known limits of each metric. Read this before deriving capabilities from source.
- [src/open_allocator/skills/mandate.md](src/open_allocator/skills/mandate.md) — a sentence of intent into a governed policy, with a reason per knob. Read this before deriving any allocation config by hand.
- [src/open_allocator/skills/discover.md](src/open_allocator/skills/discover.md)
- [src/open_allocator/skills/score.md](src/open_allocator/skills/score.md)
- [src/open_allocator/skills/build-allocation.md](src/open_allocator/skills/build-allocation.md)
- [src/open_allocator/skills/agentic-allocation.md](src/open_allocator/skills/agentic-allocation.md)
- [src/open_allocator/skills/execute-with-1tx.md](src/open_allocator/skills/execute-with-1tx.md)
- [src/open_allocator/skills/rebalance.md](src/open_allocator/skills/rebalance.md)
- [src/open_allocator/skills/withdraw.md](src/open_allocator/skills/withdraw.md)
- [src/open_allocator/skills/meta/risk-review.md](src/open_allocator/skills/meta/risk-review.md)
- [src/open_allocator/skills/meta/checkpoint-protocol.md](src/open_allocator/skills/meta/checkpoint-protocol.md)
- [src/open_allocator/workflows/allocate.yaml](src/open_allocator/workflows/allocate.yaml)
- [src/open_allocator/workflows/rebalance.yaml](src/open_allocator/workflows/rebalance.yaml)
- [src/open_allocator/workflows/withdraw.yaml](src/open_allocator/workflows/withdraw.yaml)

Risk review is advisory-only. It can flag critical/suggestion/nitpick findings, but only policy failures and missing/denied human confirmation block execution.
