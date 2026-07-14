# 05-03 — Director skills, workflows & risk-review meta-skill

**Phase:** 5 — Hardening (v2)
**Depends on:** 00-06, 02-01
**Status:** done

## Goal
Fill in the instruction layer so the repo is fully agent-operated: how to run each stage, and a
self-review the agent runs before presenting a book.

## Scope / Deliverables
- `skills/`: `discover.md`, `score.md`, `build-allocation.md`, `execute-with-1tx.md`, `rebalance.md`,
  `withdraw.md` — each teaches the workflow, quality bar, and review focus for that stage.
- `skills/meta/risk-review.md` — advisory self-review (concentration, reward-dependence, liquidity,
  policy conformance, APY-descriptive framing); grades findings critical/suggestion/nitpick; max 2 rounds;
  never hard-blocks (the policy gate + human confirmation do the blocking).
- `skills/meta/checkpoint-protocol.md` — when/what to checkpoint (ties to 04-03).
- `workflows/allocate.yaml`, `rebalance.yaml`, `withdraw.yaml` — declarative stage graphs (stage → skill →
  command → produces → review_focus → human_approval_default).

## Tests
- A workflow-loader test: each `workflows/*.yaml` references skills/commands that exist (no dangling refs).
- `AGENT_GUIDE.md` links every skill; the reviewer's `review_focus` items map to real check functions
  where applicable.

## Acceptance criteria
- [x] The allocate/rebalance/withdraw flows are documented as runnable stage graphs.
- [x] Risk-review is advisory-only; blocking stays with policy + human confirm.

## References
plan_allocator.md § Architecture Mapping, § Reviewer/announce-before-execute, § Repo Layout (skills/, workflows/)
