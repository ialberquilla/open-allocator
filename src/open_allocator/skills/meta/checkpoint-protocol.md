# Checkpoint Protocol Meta-Skill

Use checkpoints to make long allocation flows auditable and resumable. Checkpoints are code artifacts from `open_allocator.core.checkpoint`; there is no separate checkpoint CLI command.

## When To Checkpoint

- After durable stage artifacts: discovery, scores, allocation, simulation, policy result, transaction plan, execution report, positions snapshot.
- Before waiting for human approval: status `awaiting_human` with the exact artifact being approved.
- After confirmed execution: status `completed` with execution/withdraw/rebalance report and completed idempotency keys.
- On partial failure or in-progress cross-chain state: status `failed` or `in_progress` with completed step keys and messages.
- A submission that has settled nothing on chain — a Safe transaction awaiting threshold signatures, a user operation the bundler has not included — also checkpoints `in_progress`, never `completed`. The step keys are still recorded so a resume does not submit it twice; the report's messages name what is unconfirmed.

## What To Store

- `stage`: workflow stage name such as `build-allocation`, `execute`, `rebalance`, or `withdraw`.
- `status`: `completed`, `awaiting_human`, `in_progress`, or `failed`.
- `artifact`: JSON-compatible CLI/core output.
- `artifact_type` or `schema_name` when known: `allocation`, `tx-plan`, `policy`, `vault-score`, or execution report payloads containing a `plan`.
- `completed_keys`: idempotency keys for sent/skipped/completed transaction steps.
- `metadata`: policy path, command, source artifact paths, approval note, or run id.

## APIs

- `write_checkpoint(stage, status, artifact, checkpoint_dir=..., artifact_type=..., metadata=..., completed_keys=...)`
- `read_checkpoint(checkpoint_id, checkpoint_dir=...)`
- `resume_state(checkpoint_id, checkpoint_dir=...)`
- `idempotency_store_from_checkpoint(checkpoint_id, checkpoint_dir=...)`
- `write_allocation_log_entry(...)`, `read_allocation_log(...)`, `allocation_log_totals(...)`, `reconcile_allocation_log(...)`

## Resume Protocol

1. Load the most recent relevant checkpoint with `read_checkpoint` or `resume_state`.
2. Build an idempotency store with `idempotency_store_from_checkpoint`.
3. Re-enter the same execution/rebalance/withdraw path with the same source artifacts.
4. Let execution code skip completed keys; do not manually remove or replay completed transactions.
5. After completion, read the allocation log and reconcile it with `positions`.

## Quality Bar

- `completed` and `awaiting_human` checkpoints for known artifact types must schema-validate.
- Human approval checkpoints preserve the exact artifact and action that was approved.
- Allocation-log entries are append-only and reconcile against positions after confirmed execution.
- Checkpoints aid resume; they do not authorize execution without policy success and human confirmation.
