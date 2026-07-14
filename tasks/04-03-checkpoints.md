# 04-03 — Checkpoints & allocation log

**Phase:** 4 — Rebalance & exit
**Depends on:** 03-02, 00-04
**Status:** done

## Goal
An auditable, resumable record: every allocation, rebalance, and exit is a schema-valid stored artifact.

## Scope / Deliverables
- `src/open_allocator/core/checkpoint.py`:
  - `write_checkpoint(stage, status, artifact, ...)` / `read_checkpoint(id)` — snapshot after each stage
    (`completed|failed|awaiting_human|in_progress`) embedding the canonical artifact (allocation /
    execution report / rebalance plan).
  - `allocation_log`: append-only JSON-lines of executed actions (instrument, chain, shares/USD, tx hash,
    timestamp) for reconciliation + audit.
- Resume: a failed/awaiting checkpoint can be re-entered without re-executing completed legs (ties to
  03-02 idempotency).

## Tests
- A `completed`/`awaiting_human` checkpoint embeds a **schema-valid** artifact (00-04) — invalid = failure.
- Resume from a mid-execution checkpoint skips completed legs.
- `allocation_log` entries are append-only and reconcile to positions (04-00).

## Acceptance criteria
- [x] Every spend produces a stored, schema-valid record.
- [x] Interrupted flows resume from the last checkpoint without double execution.

## References
plan_allocator.md § Architecture Mapping (checkpoints), § Canonical Artifacts
