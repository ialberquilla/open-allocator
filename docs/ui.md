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

- `positions` adds a `warnings` list. The CLI prints those warnings to stderr; over stdio, stdout is the protocol.
- Errors come back as `{"error": ..., "code": ...}` in the tool result; `code` is present for `ServiceError`.

Exposed tools: `wallet-status`, `safe-address`, `positions`, `rewards`, all read-only.

## Invariants

- Local only. Nothing is hosted, and the signer keys stay in the Python process.
- The model never holds the confirmation. Execution tools return a plan-required response and accept no `confirm` / `unsafe` / `autonomous` argument. Approval is a human action outside the model's reach and executes exactly the plan whose hash was approved.
