# Local UI and MCP server

A local dashboard and approval surface on top of the library. Conversation happens in the user's own MCP client (Claude Code, Claude Desktop, …); this repository ships no chat.

## Layers

- **`open_allocator.service`**: the logic behind each command, as functions that take typed arguments and return the JSON object the CLI prints. They never print or exit, and raise `ServiceError(code, detail)` for failures the caller can act on.
- **Adapters** stay thin and must not diverge: the CLI (`cli.py`) and the MCP server (`open_allocator/mcp.py`). The HTTP API lives in a separate app, `apps/server` (`oa_server`), that the library never imports.

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
{"plan_required": true, "kind": "execute", "plan_hash": "…", "expires_at": "…", "plan": {"status": "planned", …}, "approval_url": "http://127.0.0.1:8787/approve/…", "warnings": []}
```

`approval_url` is present when the host serves an approval page (`build_mcp(store, approval_url=...)`); the stdio server has none.

Nothing in the MCP adapter can apply or reject a plan. `apply_approved(store, plan_hash, policy=...)` is the approval entry point for a human-facing surface (the local server's Approve route). It marks the stored plan used before anything else, so a plan runs at most once, and refuses unknown (`plan_not_found`), expired (`plan_expired`, 15 minutes by default) or used (`plan_used`) plans, and a stored plan that no longer hashes to the approved hash (`plan_mismatch`). With `policy`, it then re-checks the plan's allocation against that policy on today's shelf (`recheck_execute_policy`) and refuses on a violation (`policy_violation`). A refused or failed approval leaves the plan used: plan again. `reject(store, plan_hash)` retires a plan without applying it, with the same refusals.

`review_plan(kind, plan)` describes a stored plan for the person approving it, from the plan alone: account, legs (target and planned USD), bundles in submission order (action, chain, amounts in token units, step kinds, quote expiry, bridge), loops, funding against balances read at planning, the policy result, notes and blockers.

The stdio server keeps plans in memory and has no approval surface. Over stdio, approve by running `execute --confirm` yourself.

Not yet split: `rebalance`, `withdraw`, `loop-open`, `loop-close`, `bridge`.

## Local server (`apps/server`)

`oa_server` is a separate package in the uv workspace. It depends on the library; the library never imports it. One process serves:

- `/mcp`: the same MCP server over streamable HTTP, storing plans in Postgres (`PostgresPlanStore`).
- `/` and `/approve/<hash>`: the web app (`apps/web`): recent plans, and one plan's review with Approve and Reject.
- `GET /api/plans`: recent plans, newest first, with their status (`pending`, `expired`, `applying`, `applied`, `failed`, `rejected`).
- `GET /api/plans/{hash}`: the stored plan an approval would apply, its `review` and status, and the recorded result or error. The page shows this, not the model's copy.
- `POST /api/approve {"plan_hash"}`: applies that stored plan once, after re-checking it against the operator's policy (`OA_POLICY_PATH`, default `policy.yaml`), whatever policy the model planned under. Records the result or error on the plan row. `404` unknown, `410` expired, `409` used or mismatched, `422` policy violation.
- `POST /api/reject {"plan_hash"}`: retires that stored plan without applying it.
- `GET /api/health`: needs no token.

Run it from the directory with the CLI's `.env`:

```bash
make install                  # uv sync + the web app's dependencies (Node 22, corepack pnpm)
make serve                    # builds the web app, starts Postgres, migrates, serves http://127.0.0.1:8787
```

The server prints a sign-in link and opens it in the browser (`--no-browser` to skip), and prints the `claude mcp add` command for its MCP endpoint. Settings: `OA_DATABASE_URL`, `OA_HOST`, `OA_PORT`, `OA_POLICY_PATH`, `OA_MCP_TOKEN`.

From a terminal instead of the page: `uv run open-allocator-ui approve <hash>` or `reject <hash>`, against the same database and `.env`.

For web development, run the server, then `make dev` (Vite on `:5173`, proxying `/api` and `/login` to `:8787`). `make types` regenerates `apps/web/src/lib/api-types.ts` from the server's OpenAPI; the review's shape is pinned in `oa_server/schemas.py`.

### Access

- Binds `127.0.0.1`. A request whose `Host` is not the server's own is refused (`421`, against DNS rebinding), as is one with a foreign `Origin` (`403`) or without the right token (`401`).
- Two tokens. The **MCP token** opens `/mcp` only; it is kept in `.open_allocator/mcp-token` (`0600`, or `OA_MCP_TOKEN`) so a client is configured once. It sits in the client's config, where a model with file access could read it, so it can propose plans but never approve or reject them. The **browser token** is new at every start and opens everything else. The sign-in link (`/login?token=…`) swaps it for an `HttpOnly`, `SameSite=Lax` cookie and redirects to a path on this server; page code never sees it.
- Signer keys stay in the server process. No response carries them.

### MCP clients

Point any MCP client that speaks streamable HTTP at `http://127.0.0.1:8787/mcp` with the header `Authorization: Bearer <MCP token>`. For Claude Code:

```bash
claude mcp add --transport http open-allocator http://127.0.0.1:8787/mcp \
  --header "Authorization: Bearer <MCP token>"
```

Plans proposed over this endpoint land in Postgres. The client never approves: a `plan_required` result carries the `approval_url`, and approving is a click on that page (or the `approve` command), never an MCP call.

### Storage

SQLAlchemy models in `oa_server/db/models.py`; Alembic migrations are autogenerated from them (`uv run --directory apps/server alembic revision --autogenerate -m "…"`), never hand-written. The launcher runs `upgrade head` at start. `test_migrations.py` runs `alembic check`, so a model change without its migration fails.

Tables: `plan` (hash, kind, plan, created/expires/used, result, error).

### Tests

`uv run pytest apps/server/tests`. Postgres comes from `OA_TEST_DATABASE_URL`, or a throwaway container when Docker is running; without either the database tests skip.

## Invariants

- Local only. Nothing is hosted, and the signer keys stay in the Python process.
- The model never holds the confirmation. Execution tools return a plan-required response and accept no `confirm` / `unsafe` / `autonomous` argument. Approval is a human action outside the model's reach and executes exactly the plan whose hash was approved.
