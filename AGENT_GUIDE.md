# Agent Guide

This is the operating contract for agents and humans working in this repository. Project architecture and domain invariants are in [PROJECT_CONTEXT.md](PROJECT_CONTEXT.md).

## Operating Rules

- Treat the CLI as the source of truth for repo operations.
- Keep allocation and risk logic in Python code, not hidden prompt instructions.
- Prefer JSON artifacts validated by [schemas/](schemas/) over prose handoffs.
- Never hardcode protocol, chain, or instrument universes; discover from 1Tx and narrow by policy.
- Do not sign, broadcast, rebalance, or withdraw without first announcing the exact action and obtaining the required confirmation.
- Frame APY as descriptive, not predictive.
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
- `build-tx`
- `execute`
- `positions`
- `rebalance`
- `withdraw`
<!-- command-inventory:end -->

Every command must print one JSON object to stdout. Errors must print one JSON object to stderr and exit non-zero. Execution commands must return a plan-required response unless `--confirm`, `--unsafe`, or `--autonomous` is explicitly supplied.

## Allocation Loop

Use this loop for deposits and new books.

1. Load policy and signer configuration.
2. Run `wallet-status` and check wallet address, USDC, and native gas balances.
   Under `SIGNER_SUBMISSION=erc4337-paymaster` there is no native gas to check —
   gas is paid in USDC by the smart account ([docs/gasless-execution.md](docs/gasless-execution.md)).
3. Run `list-vaults` to discover the full live 1Tx universe.
4. Run `score-vault` over candidate instruments and keep unknown fields visible.
5. Run `build-allocation` to create a weighted, policy-conformant proposal.
6. Run `simulate` to inspect blended APY, concentration, and failure-cost flags.
7. Run `check-policy`; stop on any violation.
8. Announce vaults, chains, amounts, risks, expected transactions, and gas requirements.
9. Wait for human approval.
10. Run `build-tx`, then `execute --confirm` only after approval.
11. Run `positions` and retain the checkpoint/allocation-log artifacts.

## Rebalance Loop

1. Run `positions` and compare current holdings to the target allocation.
2. Build deltas only; do not redeploy unchanged positions.
3. Run scoring, simulation, and `check-policy` for the proposed deltas.
4. Announce the exact exits, deposits, chains, amounts, risks, and expected transactions.
5. Wait for approval, then run `rebalance --confirm`.

## Withdraw Loop

1. Identify the position and its share balance.
2. Check liquidity, withdrawal constraints, gas, and any cross-chain timing. A gasless
   exit funds itself from the redeem and needs nothing pre-positioned on the chain,
   but its proceeds must exceed its gas.
3. Announce the share amount, expected destination asset, chain, risks, and transactions.
4. Wait for approval, then run `withdraw --confirm`.

## Confirmation Discipline

Announce before execute. A valid execution announcement includes the wallet, source and destination chains, instruments, amounts, calldata source, policy result, expected gas assets, and failure modes.

`--unsafe` and `--autonomous` are not shortcuts. Use them only when the policy and task explicitly require them and the bounds are documented before execution.

## Instruction Layer

Use these stage skills and workflow graphs for agent-operated runs. They describe how to call the CLI and review artifacts; deterministic allocation, scoring, policy, and execution logic remains in Python code.

- [skills/discover.md](skills/discover.md)
- [skills/score.md](skills/score.md)
- [skills/build-allocation.md](skills/build-allocation.md)
- [skills/agentic-allocation.md](skills/agentic-allocation.md)
- [skills/execute-with-1tx.md](skills/execute-with-1tx.md)
- [skills/rebalance.md](skills/rebalance.md)
- [skills/withdraw.md](skills/withdraw.md)
- [skills/meta/risk-review.md](skills/meta/risk-review.md)
- [skills/meta/checkpoint-protocol.md](skills/meta/checkpoint-protocol.md)
- [workflows/allocate.yaml](workflows/allocate.yaml)
- [workflows/rebalance.yaml](workflows/rebalance.yaml)
- [workflows/withdraw.yaml](workflows/withdraw.yaml)

Risk review is advisory-only. It can flag critical/suggestion/nitpick findings, but only policy failures and missing/denied human confirmation block execution.
