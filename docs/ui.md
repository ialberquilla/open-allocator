# Local UI, MCP server and chat

A local dashboard and chat on top of the library.

## Layers

- **`open_allocator.service`**: the logic behind each command, as functions that take typed arguments and return the JSON object the CLI prints. They never print or exit, and raise `ServiceError(code, detail)` for failures the caller can act on.
- **Adapters** stay thin and must not diverge: the CLI (`cli.py`) and the MCP server (`open_allocator/mcp.py`). An HTTP API belongs in a separate app that the library never imports.

## MCP server

Install the extra and run over stdio:

```bash
uv sync --extra mcp
uv run open-allocator-mcp
```

Clients that read a project [.mcp.json](../.mcp.json) pick it up from this repository. It uses the same `.env` as the CLI.

Tools are named after the CLI commands and return the same objects, with these differences:

- Every tool except `wallet-status`, `safe-address` and `rewards` adds a `warnings` list. The CLI prints those warnings to stderr; over stdio, stdout is the protocol.
- `list-vaults` returns `{"vaults": [...], "warnings": [...]}` instead of a bare array.
- `build-allocation` returns `{"allocation": {...}, "warnings": [...]}`. The `allocation` object is what `simulate` takes, unchanged, in place of the CLI's `--allocation` file.
- Arguments are typed values, not CLI strings: `pins` and `strategy_params` are objects, `spec` is an allocation-spec object rather than a path. `policy_path` is still a path, resolved against the server's working directory.
- Errors come back as `{"error": ..., "code": ...}` in the tool result; `code` is present for `ServiceError`.

Exposed tools: `wallet-status`, `safe-address`, `positions`, `rewards`, `list-vaults`, `score-vault`, `screen`, `build-allocation`, `simulate`, `execute`. None of them changes anything on chain.

## Plans and approval

Execution is split in the service layer (`open_allocator.service.execution`):

- `plan_execute(allocation, policy=...)` discovers, policy-checks, sizes and prepares the deposits and sends nothing. It returns `{kind, plan, plan_hash, report}`. `plan` is a complete `AllocationPlan` (`exec/allocation_plan.py`), `plan_hash` is sha256 over the canonical JSON of the kind and plan, and `report` is the dry run `execute` prints without `--confirm`.
- `apply_execute(plan, expected_hash=...)` executes that plan as it stands. It never discovers, sizes or plans again. It refuses a plan that does not match the hash, a plan built for another signer, and a plan the wallet has moved past (a leg sent or bridging since it was built). Calldata close to expiry is re-quoted for the same leg, account and amount just before signing, as on the CLI.
- `execute --confirm` runs plan then apply in one process. Its output is unchanged.

The MCP `execute` tool takes the `allocation` object from `build-allocation`. It calls `plan_execute`, stores the plan in a `PlanStore` (`service/plan_store.py`) and returns:

```json
{"plan_required": true, "kind": "execute", "plan_hash": "…", "expires_at": "…", "plan": {"status": "planned", …}, "warnings": []}
```

Nothing in the MCP adapter can apply a plan. `apply_approved(store, plan_hash)` is the approval entry point for a human-facing surface (the local server's Approve button): it marks the stored plan used before running it, so a plan runs at most once, and refuses unknown (`plan_not_found`), expired (`plan_expired`, 15 minutes by default) or used (`plan_used`) plans. The stdio server keeps plans in memory and has no approval surface yet. Over stdio, approve by running `execute --confirm` yourself.

Not yet split: `rebalance`, `withdraw`, `loop-open`, `loop-close`, `bridge`. Policy is not re-checked at approval yet; the plan carries the result it was built under.

## Invariants

- Local only. Nothing is hosted, and the signer keys stay in the Python process.
- The model never holds the confirmation. Execution tools return a plan-required response and accept no `confirm` / `unsafe` / `autonomous` argument. Approval is a human action outside the model's reach and executes exactly the plan whose hash was approved.
