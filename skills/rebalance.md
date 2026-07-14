# Rebalance Skill

Use this stage to compare current positions to a target allocation and execute deltas only. Rebalance is not a redeploy of unchanged legs.

## Runnable Workflow

1. Snapshot current holdings: `open-allocator positions --address <wallet>`.
2. Build or load the target allocation artifact and run `simulate` plus `check-policy` on it.
3. Dry-run deltas: `open-allocator rebalance --current <positions.json> --target <allocation.json> --policy <policy.yaml> --min-trade-usd <usd>`.
4. Review sells before buys, skipped dust deltas, share amounts for exits, gas needs, and policy result.
5. Announce exact exits, deposits, chains, amounts, risks, and expected transactions.
6. Wait for human approval, then run the same command with `--confirm`.

## Quality Bar

- Current positions and target allocation are both current artifacts.
- Rebalance plan includes only material deltas after `--min-trade-usd` filtering.
- Target allocation passes policy before any transaction is built or signed.
- Sells use yield-token share balances; unchanged positions are not redeployed.

## Relevant CLI Commands

- `open-allocator positions --address <wallet>`
- `open-allocator simulate --allocation <allocation.json>`
- `open-allocator check-policy --allocation <allocation.json> --policy <policy.yaml>`
- `open-allocator rebalance --current <positions.json> --target <allocation.json> --policy <policy.yaml> --min-trade-usd 1`
- `open-allocator rebalance --current <positions.json> --target <allocation.json> --policy <policy.yaml> --min-trade-usd 1 --confirm`

## Produced Artifacts

- Current positions JSON.
- Target allocation JSON.
- Rebalance dry-run/execution report JSON.
- Checkpoint and allocation-log updates for confirmed actions.

## Safety Gates

- Policy violations block rebalance execution.
- Human approval is required by default; autonomous rebalance requires explicit flag plus policy authorization.
- Do not execute dust trades below the configured threshold.
- Resume from checkpoints/idempotency keys after partial execution instead of duplicating transactions.

## Review Focus

- Delta-only behavior.
- Policy conformance of the target allocation.
- Share-balance math for exits.
- Gas readiness, checkpointing, and idempotent resume.
