# Withdraw Skill

Use this stage to exit one position to the destination asset through 1Tx. ERC-4626 exits are based on yield-token share balance, not a guessed USDC amount.

## Runnable Workflow

1. Snapshot positions: `open-allocator positions --address <wallet>`.
2. Identify the exact `instrument_id`, chain, share balance, share decimals, and current USD value.
3. Dry-run withdrawal: `open-allocator withdraw --position <instrument_id> --amount <usd> --positions <positions.json> --policy <policy.yaml>` or omit `--amount` for full exit.
4. Review the planned `yield_token_amount`, expected USDC, chain, transaction steps, gas needs, and liquidity/withdrawal messages.
5. Announce the exact withdrawal and wait for human approval.
6. Execute only after approval with the same command plus `--confirm`.
7. Run `positions` again and reconcile the allocation log.

## Quality Bar

- The position exists in the current positions artifact.
- Partial exits round down to valid share units and never exceed the share balance.
- Full exits use the full share balance.
- Any in-progress 1Tx messages are surfaced as in-progress, not silently treated as success or failure.

## Relevant CLI Commands

- `open-allocator positions --address <wallet>`
- `open-allocator withdraw --position <instrument_id> --positions <positions.json> --policy <policy.yaml>`
- `open-allocator withdraw --position <instrument_id> --amount <usd> --positions <positions.json> --policy <policy.yaml>`
- `open-allocator withdraw --position <instrument_id> --amount <usd> --positions <positions.json> --policy <policy.yaml> --confirm`

## Produced Artifacts

- Pre-withdraw positions JSON.
- Withdraw dry-run/execution report JSON.
- Post-withdraw positions JSON.
- Checkpoint and allocation-log updates for confirmed exits.

## Safety Gates

- No confirmed withdrawal before exact human approval.
- Do not use USDC value guesses as the transaction amount; use computed share amounts.
- Stop if position, share balance, or gas readiness is unknown.
- Resume from checkpoints/idempotency keys after partial execution.

## Review Focus

- Share-balance exit math.
- Withdrawal constraints and liquidity messages.
- Destination asset/chain clarity.
- Checkpointing and allocation-log reconciliation.
