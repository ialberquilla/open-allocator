"""Chat through the user's own `claude` CLI, with only this server's tools.

Each turn spawns `claude -p` in a scratch directory with no built-in tools, no
settings sources, and an MCP config naming only this server's `/mcp`. Its
stream-json output is parsed into chat events for the browser.

The bridge never approves anything. A `plan_required` event only tells the UI
which stored plan to show; approving it is a separate human request.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import tempfile
from collections.abc import AsyncIterator, Iterable
from pathlib import Path
from typing import Any

JsonObject = dict[str, Any]

MCP_SERVER_NAME = "open-allocator"
TOOL_PREFIX = f"mcp__{MCP_SERVER_NAME}__"

SYSTEM_PROMPT = """\
You are the chat of Open Allocator, a policy-bounded DeFi yield allocator on 1Tx, \
running locally for the user. Your only tools are the open-allocator MCP tools. \
They return the same JSON as the CLI command of the same name.

- Discover before you propose: never invent instruments, chains or numbers.
- APY is descriptive, not predictive. Unknown fields are unknown; never fill them in.
- You cannot execute anything. An execution tool such as `execute` plans the \
transactions, stores the plan and returns `plan_required` with a `plan_hash`. \
Summarize the plan and its blockers, then tell the user to review and approve it \
in the Approve panel. Only the user can approve, and only there.
- The trade-off between yield and independence is the user's: ask when a request \
does not say which construction rule (`strategy`) to use.
"""

# What the spawned CLI inherits. Signer keys and API keys in the server's own
# environment stay out of the model's process.
_PASSED_ENV = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "LANG",
    "LC_ALL",
    "TERM",
    "TMPDIR",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_RUNTIME_DIR",
    "CLAUDE_CONFIG_DIR",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
)


class ChatUnavailable(RuntimeError):
    pass


class SessionBusy(RuntimeError):
    pass


def mcp_config(mcp_url: str, token: str) -> JsonObject:
    return {
        "mcpServers": {
            MCP_SERVER_NAME: {
                "type": "http",
                "url": mcp_url,
                "headers": {"Authorization": f"Bearer {token}"},
            }
        }
    }


def build_command(
    claude_bin: str,
    message: str,
    *,
    mcp_config_path: Path,
    session_id: str | None = None,
) -> list[str]:
    command = [
        claude_bin,
        "-p",
        message,
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--mcp-config",
        str(mcp_config_path),
        "--strict-mcp-config",
        # No built-in tools: no shell, no file access, no web.
        "--tools",
        "",
        "--allowedTools",
        f"{TOOL_PREFIX}*",
        # No user, project or local settings: their hooks, permissions and MCP
        # servers do not apply to this chat.
        "--setting-sources",
        "",
        "--disable-slash-commands",
        "--append-system-prompt",
        SYSTEM_PROMPT,
    ]
    if session_id:
        command += ["--resume", session_id]
    return command


def child_env(environ: dict[str, str] | None = None) -> dict[str, str]:
    source = os.environ if environ is None else environ
    return {name: source[name] for name in _PASSED_ENV if name in source}


def _tool_result_payload(content: object) -> object:
    """The tool's JSON object when the result is one, else the raw content."""
    if isinstance(content, list):
        texts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        content = "".join(texts) if len(texts) == len(content) else content
    if isinstance(content, str):
        try:
            return json.loads(content)
        except ValueError:
            return content
    return content


def parse_line(line: str, tool_names: dict[str, str] | None = None) -> list[JsonObject]:
    """Chat events for one stream-json line; unknown lines yield none.

    `tool_names` maps tool_use ids to tool names across lines, so a result can
    be attributed to the tool that produced it.
    """
    names = tool_names if tool_names is not None else {}
    line = line.strip()
    if not line:
        return []
    try:
        message = json.loads(line)
    except ValueError:
        return []
    kind = message.get("type")
    if kind == "system" and message.get("subtype") == "init":
        return [{"type": "session", "session_id": message.get("session_id")}]
    if kind == "stream_event":
        event = message.get("event") or {}
        delta = event.get("delta") or {}
        if event.get("type") == "content_block_delta" and delta.get("type") == (
            "text_delta"
        ):
            return [{"type": "text", "text": delta.get("text", "")}]
        return []
    if kind == "assistant":
        events = []
        for block in _content(message):
            if block.get("type") == "tool_use":
                names[block.get("id", "")] = block.get("name", "")
                events.append(
                    {
                        "type": "tool_use",
                        "id": block.get("id"),
                        "name": block.get("name"),
                        "input": block.get("input"),
                    }
                )
        return events
    if kind == "user":
        events = []
        for block in _content(message):
            if block.get("type") != "tool_result":
                continue
            tool_use_id = block.get("tool_use_id")
            name = names.get(tool_use_id or "", "")
            payload = _tool_result_payload(block.get("content"))
            events.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "name": name or None,
                    "is_error": bool(block.get("is_error")),
                    "content": payload,
                }
            )
            if (
                name.startswith(TOOL_PREFIX)
                and isinstance(payload, dict)
                and payload.get("plan_required") is True
            ):
                events.append(
                    {
                        "type": "plan_required",
                        "plan_hash": payload.get("plan_hash"),
                        "kind": payload.get("kind"),
                        "expires_at": payload.get("expires_at"),
                    }
                )
        return events
    if kind == "result":
        return [
            {
                "type": "result",
                "session_id": message.get("session_id"),
                "is_error": bool(message.get("is_error")),
                "result": message.get("result"),
                "total_cost_usd": message.get("total_cost_usd"),
                "duration_ms": message.get("duration_ms"),
            }
        ]
    return []


def parse_stream(lines: Iterable[str]) -> list[JsonObject]:
    names: dict[str, str] = {}
    return [event for line in lines for event in parse_line(line, names)]


def _content(message: JsonObject) -> list[JsonObject]:
    content = (message.get("message") or {}).get("content")
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict)]


class ChatBridge:
    """Spawns one `claude -p` per turn and streams its events.

    A session runs one turn at a time; a second concurrent turn on the same
    session is refused rather than interleaved.
    """

    def __init__(self, *, claude_bin: str, mcp_url: str, token: str) -> None:
        self.claude_bin = claude_bin
        self._workdir = Path(tempfile.mkdtemp(prefix="oa-chat-"))
        self._mcp_config_path = self._workdir / "mcp.json"
        self._mcp_config_path.touch(mode=0o600)
        self._mcp_config_path.write_text(
            json.dumps(mcp_config(mcp_url, token)), encoding="utf-8"
        )
        self._busy: set[str] = set()

    def close(self) -> None:
        shutil.rmtree(self._workdir, ignore_errors=True)

    def available(self) -> bool:
        return shutil.which(self.claude_bin) is not None

    def reserve(self, session_id: str | None) -> str | None:
        """Claim the session for one turn; `SessionBusy` if a turn is running."""
        if session_id is not None:
            if session_id in self._busy:
                raise SessionBusy(session_id)
            self._busy.add(session_id)
        return session_id

    def release(self, session_id: str | None) -> None:
        if session_id is not None:
            self._busy.discard(session_id)

    async def turn(
        self, message: str, session_id: str | None = None
    ) -> AsyncIterator[JsonObject]:
        executable = shutil.which(self.claude_bin)
        if executable is None:
            raise ChatUnavailable(
                f"`{self.claude_bin}` is not installed or not on PATH; install "
                "Claude Code and log in with `claude` first"
            )
        process = await asyncio.create_subprocess_exec(
            *build_command(
                executable,
                message,
                mcp_config_path=self._mcp_config_path,
                session_id=session_id,
            ),
            cwd=self._workdir,
            env=child_env(),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=16 * 1024 * 1024,
        )
        names: dict[str, str] = {}
        finished = False
        try:
            assert process.stdout is not None
            async for raw in process.stdout:
                for event in parse_line(raw.decode("utf-8", "replace"), names):
                    if event["type"] == "result":
                        finished = True
                    yield event
            await process.wait()
            if not finished:
                stderr = b""
                if process.stderr is not None:
                    stderr = await process.stderr.read()
                yield {
                    "type": "error",
                    "error": stderr.decode("utf-8", "replace").strip()
                    or f"claude exited with status {process.returncode}",
                }
        finally:
            # The browser went away mid-turn: stop the model too.
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                await process.wait()
