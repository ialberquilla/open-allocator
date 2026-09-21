# Execute With 1Tx Skill

Use this stage to turn an approved allocation into 1Tx calldata and, only after explicit human approval, sign and broadcast with the configured self-custody signer.

Before announcing, understand the funding model: read [docs/funding-and-bridging.md](../../../docs/funding-and-bridging.md). In short — a leg's destination chain is encoded in its `instrumentId`, USDC is sourced from whichever chain the wallet is funded on, and 1Tx bridges (CCTP) to the destination automatically. Under `rpc` submission the wallet needs native gas on the chains it signs on (source chains for deposits; the position's chain for exits), not on every destination. Under `erc4337-paymaster` it needs none at all: gas is paid in USDC by the smart account, and a chain's steps go out as one batched operation — see [docs/gasless-execution.md](../../../docs/gasless-execution.md).

## Runnable Workflow

1. Verify `check-policy` is `ok: true` for the exact allocation artifact.
2. Build the dry transaction plan: `open-allocator build-tx --allocation <allocation.json> --policy <policy.yaml>`.
3. Announce wallet, chains, instruments, amounts, transaction step count/types, gas assets, policy result, calldata source, and failure modes. For a calldata plan, include the dry run's `funding` rows (required vs. held per token, with any shortfall); a shortfall is a blocker, not a warning. Report wallet gas from `preparations` (and whether it `includes_deployment`), never a bundle's `protocol_gas` in its place; a preparation with `assumed_balances` was estimated as if funded and cannot be executed as it stands. Announce the deposit amounts the plan's bundles carry, not the allocation's: a calldata deposit may be sized down to leave the paymaster's gas charge in the Safe, and the dry run says so. A calldata `bridge` bundle is a CCTP burn for a leg that deposits on another chain: announce its source and destination chains, the burned amount, fast or standard transfer, and that the deposit is built only after Circle attests, sized to the mint less Circle's fee and the destination paymaster charge. A levered leg (a `loop_open` bundle) is announced from its entry in the report's `loops`: collateral, debt and equity, requested and simulated leverage, modelled and simulated health factor, depeg buffer on a cross-asset pair, the kill-switch reward price with its caveats, the `max_leftovers` it may leave unlevered in the wallet, and — when `requires_account_config` is true — every position in `pool_positions`, because changing the account's e-mode re-prices each of them. A loop whose `confirmable` is false is not announced as executable.
4. Wait for explicit human approval for that exact action.
5. Execute only after approval: `open-allocator execute --allocation <allocation.json> --policy <policy.yaml> --confirm`.
6. Run `open-allocator positions --address <wallet>` and reconcile expected holdings.

## Quality Bar

- Dry-run `build-tx` and confirmed `execute` refer to the same allocation and policy artifacts.
- The approval request is specific enough to reject accidental chain, amount, or instrument drift.
- Gas preflight results are visible before broadcast.
- Execution report, receipts, checkpoint, and allocation-log entries are retained.

## Relevant CLI Commands

- `open-allocator build-tx --allocation <allocation.json> --policy <policy.yaml>`
- `open-allocator execute --allocation <allocation.json> --policy <policy.yaml> --confirm`
- `open-allocator positions --address <wallet>`

## Produced Artifacts

- Transaction plan JSON.
- Execution report JSON.
- Post-execution positions JSON.
- `.open_allocator/checkpoints/*.json` when configured/defaulted.
- `.open_allocator/allocation-log.jsonl` entries for confirmed actions.

## Safety Gates

- No `execute --confirm` before exact human approval.
- Policy violations block execution planning and execution.
- `--unsafe` and `--autonomous` are not shortcuts; use only when the task and policy explicitly authorize them.
- Failed or in-progress cross-chain operations must be checkpointed and resumed idempotently, not blindly retried.
- A calldata report with `bridges` not all `completed` is resumed by rerunning the same `execute --confirm` (same allocation file): it never burns again, and advances each leg only as far as Circle and the chain allow. A `failed` bridge whose burn landed is never redeemed automatically; report its `last_error` and source transaction to a human.

## Review Focus

- Transaction-plan parity with the approved allocation.
- Announcement completeness.
- Gas readiness and failure modes, in whichever asset the submission axis pays in.
- Checkpoint and allocation-log integrity.
