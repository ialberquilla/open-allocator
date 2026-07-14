# 00-06 — Agent-facing docs

**Phase:** 0 — Foundations
**Depends on:** 00-05
**Status:** done

## Goal
Make the repo agent-operated in the OpenMontage style: one operating guide, thin per-agent pointers.

## Scope / Deliverables
- `PROJECT_CONTEXT.md` — single source of truth: architecture, planes, dynamic-universe rule, policy
  layer, self-custody execution model.
- `AGENT_GUIDE.md` — the operating contract: command inventory, the allocate/rebalance/withdraw loop,
  the announce-before-execute + confirmation discipline, safety rules.
- `CLAUDE.md`, `CODEX.md`, `AGENTS.md`, `OPENCODE.md` — thin files that point to `AGENT_GUIDE.md`.
- `README.md` — marketing surface + quickstart + risk disclaimer ("APY is descriptive, not predictive").
- Skeleton `skills/` and `workflows/` (empty stage files referenced by the plan; filled in Phase 5).

## Tests
- A docs test asserts every per-agent pointer file references `AGENT_GUIDE.md`.
- A link-check over relative markdown links (no dead links to files that should exist).
- The command list in `AGENT_GUIDE.md` matches the CLI's registered commands (00-05) — drift fails.

## Acceptance criteria
- [x] Per-agent files are thin pointers, not duplicated content.
- [x] `AGENT_GUIDE.md` command list is kept in sync with the CLI by a test.

## References
plan_allocator.md § Repo Layout, § Relationship to Sibling Projects (OpenMontage packaging)
