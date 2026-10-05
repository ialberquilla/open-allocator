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
- `build-allocation` returns `{"allocation": {...}, "warnings": [...]}`. The `allocation` object is what `simulate`, `backtest` and `check-policy` take, unchanged, in place of the CLI's `--allocation` file.
- `check-policy` takes an optional `against` book, the `positions` tool result (its `warnings` are ignored), in place of the CLI's `--against` file.
- Arguments are typed values, not CLI strings: `pins` and `strategy_params` are objects, `spec` is an allocation-spec object rather than a path. `policy_path` is still a path, resolved against the server's working directory.
- Errors come back as `{"error": ..., "code": ...}` in the tool result; `code` is present for `ServiceError`.

Exposed tools: `wallet-status`, `safe-address`, `positions`, `rewards`, `list-vaults`, `score-vault`, `screen`, `build-allocation`, `simulate`, `backtest`, `check-policy`, `execute`, `rebalance`, `withdraw`, `loop-open`, `loop-close`, `bridge`. None of them changes anything on chain. `drift` and `validate-mandate` are not exposed.

Resources, under `open-allocator://` and addressed by their path in the package: the guides (`guides/AGENT_GUIDE.md`, `guides/PROJECT_CONTEXT.md`, verbatim copies of the root files, kept equal by `tests/test_docs.py`; copy them over after editing either), every skill (`skills/withdraw.md`, `skills/meta/risk-review.md`, …), the JSON schemas and the workflows. A workflow's `skill:` entry is its resource path.

Prompts `allocate` (optional `amount`), `rebalance` and `withdraw` (optional `position`) render the matching `workflows/*.yaml` as numbered stages: the tool to call, the skill resource, what to review. Each execution tool is called once; the approval stage tells the model to hand the user `approval_url` and stop. `build-tx` stages say it is CLI only: the execution tool's plan covers it.

## Plans and approval

Execution is split in the service layer (`open_allocator.service.execution`):

- `plan_execute(allocation, policy=...)` discovers, policy-checks, sizes and prepares the deposits and sends nothing. It returns `{kind, plan, plan_hash, report}`. `plan` is a complete `AllocationPlan` (`exec/allocation_plan.py`), `plan_hash` is sha256 over the canonical JSON of the kind and plan, and `report` is the dry run `execute` prints without `--confirm`.
- `apply_execute(plan, expected_hash=...)` executes that plan as it stands. It never discovers, sizes or plans again. It refuses a plan that does not match the hash, a plan built for another signer, and a plan the wallet has moved past (a leg sent or bridging since it was built). Calldata close to expiry is re-quoted for the same leg, account and amount just before signing, as on the CLI.
- `execute --confirm` runs plan then apply in one process. Its output is unchanged.
- `plan_withdraw(position, amount=...)` reads the signer's book live, picks the position by instrument id and plans its exit (a full exit when `amount` is omitted or at least the position's value). `plan` is a `WithdrawalPlan` (`exec/withdraw.py`): the position and amount it was planned from, the withdraw and sell details, the bundle and its preparation. `apply_withdraw(plan, expected_hash=...)` executes it as it stands and refuses a plan for another signer or one whose withdrawal was sent since it was built. `withdraw --confirm` is plan then apply, output unchanged.
- `plan_rebalance(target, positions=..., min_trade_usd=..., policy=...)` reads the signer's book live (or takes `positions`), policy-checks the target, and plans each chain's withdrawals before its deposits. `plan` is a `RebalancingPlan` (`exec/rebalance.py`): the book and minimum trade it was planned from, the trades, the bundles, sizing notes and their preparation. `apply_rebalance(plan, expected_hash=...)` executes it as it stands and refuses a plan for another signer or one with a trade sent since it was built. `rebalance --confirm` (and `--autonomous`, gated by the policy as before) is plan then apply, output unchanged.
- `plan_loop_open(loop, equity_usd=..., leverage=..., policy=...)` reads the loop screen and the signer's book live, requires the equity idle on the loop's chain, scores the open against the book it joins (`check_incremental`), and plans the one atomic loop bundle with its announcement. `plan` is a `LoopOpeningPlan` (`exec/loop_open.py`). `plan_loop_close(loop, policy=...)` plans the unwind as a `LoopClosingPlan` (`exec/loop_close.py`). Their `apply_*` execute the bundle as it stands and refuse a plan for another signer or one whose bundle was sent since it was built. `loop-open --confirm` and `loop-close --confirm` are plan then apply, output unchanged.
- `plan_bridge(from_chain_id, to_chain_id, amount, ref=...)` plans a CCTP transfer of the Safe's USDC with no deposit: the burn, sized down to leave the paymaster's charge, route-checked. `plan` is a `TransferPlan` (`exec/transfer.py`), which also carries both chains' USDC so applying needs no discovery. The same arguments name the same transfer (`bridge_scope`, `amount` normalised to a float): while one is under way, planning again returns a plan that carries it as `existing`, with no burn, and applying that plan advances it (the mint into the Safe once Circle attests). A settled or failed transfer is refused (`invalid_input`); another `ref` starts another. `apply_bridge` refuses a new-burn plan when a transfer was started since it was built. `bridge --confirm` is plan then apply, output unchanged.

The MCP `execute` tool takes the `allocation` object from `build-allocation`; `rebalance` takes a target `allocation` and an optional `min_trade_usd`; `withdraw` takes a `position` instrument id and an optional USD `amount`; `loop-open` takes a `loop` id, a USD `amount` and a `leverage`; `loop-close` takes a `loop` id; `bridge` takes `from_chain`, `to_chain`, a USDC `amount` and an optional `ref`. Each calls its `plan_*`, stores the plan in a `PlanStore` (`service/plan_store.py`) and returns:

```json
{"plan_required": true, "kind": "execute", "plan_hash": "…", "expires_at": "…", "plan": {"status": "planned", …}, "approval_url": "http://127.0.0.1:8787/approve/…", "warnings": []}
```

`approval_url` is present when the host serves an approval page (`build_mcp(store, approval_url=...)`); the stdio server has none.

Nothing in the MCP adapter can apply or reject a plan. `apply_approved(store, plan_hash, policy=...)` is the approval entry point for a human-facing surface (the local server's Approve route). It marks the stored plan used before anything else, so a plan runs at most once, and refuses unknown (`plan_not_found`), expired (`plan_expired`, 15 minutes by default) or used (`plan_used`) plans, and a stored plan that no longer hashes to the approved hash (`plan_mismatch`). With `policy`, it then re-checks an `execute` plan's allocation (`recheck_execute_policy`) or a `rebalance` plan's target (`recheck_rebalance_policy`) or a `loop-open` plan against today's book and loop screen (`recheck_loop_open_policy`) against that policy on today's shelf and refuses on a violation (`policy_violation`). A `withdraw` or `loop-close` plan has no re-check: the policy bounds what is entered, and an exit is never refused for it. A `bridge` plan touches no instrument, so it has none either. A refused or failed approval leaves the plan used: plan again. `reject(store, plan_hash)` retires a plan without applying it, with the same refusals.

`review_plan(kind, plan)` describes a stored plan for the person approving it, from the plan alone. For `execute`: account, legs (target and planned USD), bundles in submission order (action, chain, amounts in token units, step kinds, quote expiry, bridge), loops, funding against balances read at planning, the policy result, notes and blockers. For `withdraw`: account, position, full or partial exit, requested and current USD, shares sold against the share balance, expected USDC, the bundle (a partial exit's amount in the underlying asset, which is how 1Tx takes it), funding (shares held), notes and blockers. For `rebalance`: account, book and target USD, total sells and buys, each trade (current and target USD and weight, the buy's planned spend), deltas skipped under the minimum trade, bundles (partial sells in the underlying), funding, the policy result, notes and blockers. For `loop-open`: account, loop, equity and leverage, the bundle, the loop announcement (collateral, debt, modelled and simulated leverage, health factor and depeg buffer, account config change, other positions in the pool), the policy result and notes. For `loop-close`: account, loop, the bundle and the announcement. For `bridge`: account, chains, amount, `ref`, the transfer under way it advances (or null for a new burn), the burn bundle, funding, notes and blockers. The server's `review` is a union discriminated on `kind` (`ExecuteReview`, `WithdrawReview`, `RebalanceReview`, `LoopOpenReview`, `LoopCloseReview`, `BridgeReview`).

The stdio server keeps plans in memory and has no approval surface. Over stdio, approve by running the CLI command with `--confirm` yourself.

## Local server (`apps/server`)

`oa_server` is a separate package in the uv workspace. It depends on the library; the library never imports it. One process serves:

- `/mcp`: the same MCP server over streamable HTTP, storing plans in Postgres (`PostgresPlanStore`).
- `/`, `/book`, `/shelf`, `/performance`, `/activity` and `/approve/<hash>`: the web app (`apps/web`), styled after agent-showcase: the book with its claimable rewards, the shelf, the book's NAV history with each position's share of the return, recent plans, executions and background reads, and one plan's review with Approve and Reject.
- `GET /api/book` (`?refresh=true` to skip the one-minute cache): the live `positions` book with its aggregates (weights by protocol and chain, blended current APY, 1/Σw²), computed in Python.
- `GET /api/shelf` (`?refresh=true` to read it again): every instrument discovery can score, as `list-vaults` describes it (score, advertised and realized APY, priced rewards, yield-path risk metrics), best score first. Discovery with 180 days of history takes about a minute, so the server reads it on start and hourly and serves it from memory; a request during a read waits for it.
- `GET /api/rewards` (`?refresh=true` to read again): what the Safe can claim, as the `rewards` command reads it, each reward valued in USD only where it is USDC or 1Tx quotes a ready swap to USDC; the others are counted as unpriced. Read on start and hourly. Nothing is claimed from here.
- `GET /api/nav`: the NAV series, its summary, per-chain coverage, the last backfill run, and the current ledger's return by position and by protocol.
- `GET /api/executions`: the server's allocation log, newest first. What the CLI executed is in its own files, not here.
- `GET /api/jobs`: the last run of each job (`nav`, `shelf`, `rewards`), recent runs, and which are running. `POST /api/jobs/{name}` runs one now, in the background (`202`; `started: false` if it is already running). Every run of each leaves a `job_run` row; a failed shelf or rewards read keeps serving the last good one.
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

From a terminal instead of the page: `uv run open-allocator-ui approve <hash>` or `reject <hash>`, against the same database and `.env`. `uv run open-allocator-ui backfill [--since YYYY-MM-DD]` fills the NAV history without serving.

For web development, run the server, then `make dev` (Vite on `:5173`, proxying `/api` and `/login` to `:8787`). `make types` regenerates `apps/web/src/lib/api-types.ts` from the server's OpenAPI; the review's shape is pinned in `oa_server/schemas.py`.

### NAV history

The history is rebuilt from chain reads alone, so it covers days the server was not running and days the wallet was driven from somewhere else. 1Tx has no wallet-history endpoint and free RPC tiers serve no useful `eth_getLogs` ranges, so only state reads at a block are used.

- **What is tracked:** everything the configured Safe holds in instruments 1Tx lists (plus any it held and 1Tx has since delisted), from the day the Safe was first deployed on any chain (`nav_account.start_day`; `backfill --since` moves it).
- **A close:** for each chain and each finished UTC day, the last block at or before 23:59:59Z (found by interpolation search), and at it, one Multicall3 of every listed yield token's `balanceOf`, then the held ones valued as 1Tx's `/positions` values them (`open_allocator.exec.chain_book`): ERC-4626 `convertToAssets`, Aave and forks `balanceOf` with `scaledBalanceOf` as the base count, Comet principal, Moonwell `balanceOfUnderlying`, Pendle PTs at `getPtToAssetRate` in their SY's accounting asset. USDC is 1; other assets are priced from DeFiLlama's historical prices.
- **Loops:** an Aave position with debt is valued at its equity, as the live book values it: the collateral less the share the pool's debt-to-collateral ratio takes. That needs it to be the Safe's only position in that pool, borrowing one asset; otherwise the day is unknown. The debt token's scaled balance is stored beside the collateral's, so a day the loop was resized (either count changed) is known as one and its return is not split from its flow.
- **NAV:** darex's unit ledger (`open_allocator.core.nav`). NAV is the positions; idle USDC is outside it. A flow is the change in a position's base count valued at that day's price, so deposits, withdrawals and rebalances do not move the unit price. Unit price opens at 100. It is **gross of gas**, and value lost in a swap or bridge on the way between positions shows as a flow, not as a loss.
- **Gaps, never zeros:** a chain the RPC cannot answer, a position with no price, or a loop whose debt cannot be attributed leaves that day unknown, with its reason.
- **Attribution:** each position's return is its value change less its flow on each day it was held at both closes, summed over the current ledger (`position_yield`, rebuilt with `nav_day`). What a position earned on the day it entered or left is in NAV but in no position, so the parts need not add up exactly; the page says by how much.
- **No intraday NAV:** a day enters the history once it has closed. What is held now is the book, read live.
- **Idempotent:** a close is read once and stored (`chain_close`, `position_close`); an unvalued one is read again each run. `nav_day` is rebuilt whole from the stored closes, so the same closes always give the same series. The server backfills on start and hourly (`--no-backfill` to skip); a Postgres advisory lock keeps one at a time.
- **RPCs:** `RPC_URL_<chain>` from the environment or `.env`, else the public RPC. History needs an archive node: on a pruned RPC the backfill stops at the first refused day and the coverage table says which variable to set.

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

Tables: `plan` (hash, kind, plan, created/expires/used, result, error); the NAV history: `nav_account`, `chain_close`, `position_close`, `nav_day`, `position_yield`; `job_run` (every background read); and the execution state: `idempotency_key` (scope, key, value), `checkpoint` (id, checkpoint), `allocation_log` (append-only entries).

All of the server's state is here. At start it plugs `oa_server.state.PostgresStateBackend` into the library's state port (`open_allocator.service.use_state_backend`), so planning and approval read and write completed steps, bridge transfers, checkpoints and the allocation log in Postgres, not under `.open_allocator/`. The CLI never does this and keeps its files, so it works without Docker. The two are separate: drive a wallet from one or the other, since a transfer the CLI started is invisible to the server and the reverse.

### Tests

`uv run pytest apps/server/tests`. Postgres comes from `OA_TEST_DATABASE_URL`, or a throwaway container when Docker is running; without either the database tests skip.

## Invariants

- Local only. Nothing is hosted, and the signer keys stay in the Python process.
- The model never holds the confirmation. Execution tools return a plan-required response and accept no `confirm` / `unsafe` / `autonomous` argument. Approval is a human action outside the model's reach and executes exactly the plan whose hash was approved.
