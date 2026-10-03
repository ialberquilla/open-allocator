"""The chat bridge: what `claude -p` is allowed, and how its stream is read."""

from __future__ import annotations

import ast
import asyncio
import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from oa_server import chat as chat_module
from oa_server.app import create_app
from oa_server.chat import (
    TOOL_PREFIX,
    ChatBridge,
    SessionBusy,
    build_command,
    child_env,
    parse_stream,
)
from oa_server.settings import Settings
from open_allocator.service.plan_store import InMemoryPlanStore

FIXTURES = Path(__file__).parent / "fixtures"
# A real `claude -p` turn: build-allocation, then execute (stream-json, recorded).
EXECUTE_TURN = FIXTURES / "chat_execute_turn.ndjson"


def fake_claude(tmp_path: Path, body: str) -> Path:
    """An executable standing in for `claude`."""
    script = tmp_path / "claude"
    script.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def replaying(tmp_path: Path, fixture: Path) -> Path:
    return fake_claude(
        tmp_path,
        "import sys\n"
        f"sys.stdout.write(open({str(fixture)!r}, encoding='utf-8').read())\n",
    )


async def collect(bridge: ChatBridge, message: str) -> list[dict]:
    return [event async for event in bridge.turn(message)]


def test_the_command_allows_only_this_servers_tools(tmp_path: Path) -> None:
    command = build_command(
        "claude", "hi", mcp_config_path=tmp_path / "mcp.json", session_id="abc"
    )

    def value(flag: str) -> str:
        return command[command.index(flag) + 1]

    assert value("--tools") == ""
    assert value("--allowedTools") == f"{TOOL_PREFIX}*"
    assert value("--setting-sources") == ""
    assert value("--resume") == "abc"
    assert "--strict-mcp-config" in command
    assert "--disable-slash-commands" in command
    assert "--dangerously-skip-permissions" not in command


def test_the_child_process_does_not_inherit_secrets() -> None:
    environ = {
        "PATH": "/bin",
        "HOME": "/home/me",
        "ONE_TX_PRIVATE_KEY": "0x" + "11" * 32,
        "ONE_TX_API_KEY": "key",
        "PIMLICO_API_KEY": "key",
        "OA_DATABASE_URL": "postgresql://user:password@host/db",
    }

    assert child_env(environ) == {"PATH": "/bin", "HOME": "/home/me"}


def test_the_bridge_never_reaches_approval() -> None:
    tree = ast.parse(Path(chat_module.__file__).read_text(encoding="utf-8"))
    modules = {
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)} | {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }

    assert not {m for m in modules if "approval" in m or "execution" in m}
    assert not {n for n in names if "approve" in n or n.startswith("apply")}


def test_a_recorded_turn_parses_into_chat_events() -> None:
    events = parse_stream(EXECUTE_TURN.read_text(encoding="utf-8").splitlines())
    kinds = [event["type"] for event in events]

    assert kinds[0] == "session"
    assert kinds[-1] == "result"
    assert "text" in kinds
    tool_names = [event["name"] for event in events if event["type"] == "tool_use"]
    assert f"{TOOL_PREFIX}build-allocation" in tool_names
    assert f"{TOOL_PREFIX}execute" in tool_names
    (execute_result,) = [
        event
        for event in events
        if event["type"] == "tool_result" and event["name"] == f"{TOOL_PREFIX}execute"
    ]
    assert execute_result["content"]["plan_required"] is True
    (required,) = [event for event in events if event["type"] == "plan_required"]
    assert required["plan_hash"] == execute_result["content"]["plan_hash"]
    assert required["kind"] == "execute"


def test_plan_required_comes_only_from_this_servers_tools() -> None:
    lines = [
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "id": "t1", "name": "other", "input": {}}
                    ]
                },
            }
        ),
        json.dumps(
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t1",
                            "content": json.dumps(
                                {"plan_required": True, "plan_hash": "f" * 64}
                            ),
                        }
                    ]
                },
            }
        ),
        "not json",
    ]

    kinds = [event["type"] for event in parse_stream(lines)]

    assert kinds == ["tool_use", "tool_result"]


def test_a_turn_streams_the_cli_events(tmp_path: Path) -> None:
    bridge = ChatBridge(
        claude_bin=str(replaying(tmp_path, EXECUTE_TURN)),
        mcp_url="http://127.0.0.1:8787/mcp",
        token="secret",
    )
    try:
        events = asyncio.run(collect(bridge, "hi"))
    finally:
        bridge.close()

    assert events[0]["type"] == "session"
    assert events[-1]["type"] == "result"
    assert any(event["type"] == "plan_required" for event in events)


def test_a_cli_failure_is_an_error_event(tmp_path: Path) -> None:
    claude = fake_claude(
        tmp_path, "import sys\nsys.stderr.write('Not logged in')\nsys.exit(1)\n"
    )
    bridge = ChatBridge(
        claude_bin=str(claude), mcp_url="http://127.0.0.1:8787/mcp", token="secret"
    )
    try:
        events = asyncio.run(collect(bridge, "hi"))
    finally:
        bridge.close()

    assert events == [{"type": "error", "error": "Not logged in"}]


def test_the_cli_runs_in_a_scratch_directory_with_a_private_config(
    tmp_path: Path,
) -> None:
    seen = tmp_path / "seen.json"
    claude = fake_claude(
        tmp_path,
        "import json, os, sys\n"
        "config = sys.argv[sys.argv.index('--mcp-config') + 1]\n"
        "observed = {'cwd': os.getcwd(), 'config': config,\n"
        "    'mode': os.stat(config).st_mode & 0o777,\n"
        "    'servers': json.load(open(config))}\n"
        f"json.dump(observed, open({str(seen)!r}, 'w'))\n",
    )
    bridge = ChatBridge(
        claude_bin=str(claude), mcp_url="http://127.0.0.1:8787/mcp", token="secret"
    )
    try:
        asyncio.run(collect(bridge, "hi"))
    finally:
        bridge.close()

    observed = json.loads(seen.read_text(encoding="utf-8"))
    assert Path(observed["cwd"]).name.startswith("oa-chat-")
    assert Path(observed["config"]).parent == Path(observed["cwd"])
    assert observed["mode"] == 0o600
    (server,) = observed["servers"]["mcpServers"].values()
    assert server["url"] == "http://127.0.0.1:8787/mcp"
    assert server["headers"] == {"Authorization": "Bearer secret"}
    assert not Path(observed["cwd"]).exists()


def test_closing_a_turn_early_stops_the_cli(tmp_path: Path) -> None:
    pid_file = tmp_path / "pid"
    claude = fake_claude(
        tmp_path,
        "import json, os, sys, time\n"
        f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
        "print(json.dumps({'type': 'system', 'subtype': 'init', "
        "'session_id': 's'}), flush=True)\n"
        "time.sleep(60)\n",
    )
    bridge = ChatBridge(
        claude_bin=str(claude), mcp_url="http://127.0.0.1:8787/mcp", token="secret"
    )

    async def first_event_then_disconnect() -> dict:
        turn = bridge.turn("hi")
        event = await anext(turn)
        await turn.aclose()
        return event

    try:
        event = asyncio.run(first_event_then_disconnect())
    finally:
        bridge.close()

    assert event == {"type": "session", "session_id": "s"}
    pid = int(pid_file.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("the CLI kept running after the turn was closed")


def test_a_session_runs_one_turn_at_a_time() -> None:
    bridge = ChatBridge(
        claude_bin="claude", mcp_url="http://127.0.0.1:8787/mcp", token="secret"
    )
    try:
        bridge.reserve("s1")
        with pytest.raises(SessionBusy):
            bridge.reserve("s1")
        bridge.release("s1")
        bridge.reserve("s1")
    finally:
        bridge.close()


def test_the_chat_route_streams_server_sent_events(tmp_path: Path) -> None:
    settings = Settings(
        token="test-token", claude_bin=str(replaying(tmp_path, EXECUTE_TURN))
    )
    app = create_app(settings, InMemoryPlanStore())
    with TestClient(app, base_url="http://127.0.0.1:8787") as client:
        response = client.post(
            "/api/chat",
            json={"message": "hi"},
            headers={"Authorization": "Bearer test-token"},
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    names = [
        line.removeprefix("event: ")
        for line in response.text.splitlines()
        if line.startswith("event: ")
    ]
    assert names[0] == "session"
    assert names[-1] == "result"
    assert "plan_required" in names
