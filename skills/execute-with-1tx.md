# Execute With 1Tx Skill

Use this stage to turn an approved allocation into 1Tx calldata and, only after explicit human approval, sign and broadcast with the configured self-custody signer.

Before announcing, understand the funding model: read [docs/funding-and-bridging.md](../docs/funding-and-bridging.md). In short — a leg's destination chain is encoded in its `instrumentId`, USDC is sourced from whichever chain the wallet is funded on, and 1Tx bridges (CCTP) to the destination automatically. The wallet needs native gas only on the chains it signs on (source chains for deposits; the position's chain for exits), not on every destination.

## Runnable Workflow

1. Verify `check-policy` is `ok: true` for the exact allocation artifact.
2. Build the dry transaction plan: `open-allocator build-tx --allocation <allocation.json> --policy <policy.yaml>`.
3. Announce wallet, chains, instruments, amounts, transaction step count/types, gas assets, policy result, calldata source, and failure modes.
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

## Review Focus

- Transaction-plan parity with the approved allocation.
- Announcement completeness.
- Native gas readiness and failure modes.
- Checkpoint and allocation-log integrity.
