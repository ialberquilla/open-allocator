"""The HTTP surface: local-only access, and the path from an MCP proposal to a
human approval that applies it."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from mcp.types import LATEST_PROTOCOL_VERSION
from test_cli import (
    DEPOSIT_KINDS,
    ExecutionSignerSpy,
    install_execution_surface_mocks,
    write_execution_files,
)

from oa_server.app import create_app
from oa_server.auth import TOKEN_COOKIE
from oa_server.settings import Settings
from open_allocator.service.plan_store import InMemoryPlanStore

TOKEN = "test-token"
BASE_URL = "http://127.0.0.1:8787"
ZERO_HASH = "0" * 64


def settings(policy_path: Path | None = None, **overrides: Any) -> Settings:
    values: dict[str, Any] = {"token": TOKEN, **overrides}
    if policy_path is not None:
        values["policy_path"] = policy_path
    return Settings(**values)


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = create_app(settings(), InMemoryPlanStore())
    with TestClient(app, base_url=BASE_URL) as test_client:
        yield test_client


def authorized(**headers: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}", **headers}


def test_health_needs_no_token(client: TestClient) -> None:
    assert client.get("/api/health").json()["ok"] is True


@pytest.mark.parametrize(
    ("headers", "status"),
    [
        ({}, 401),
        ({"Authorization": "Bearer wrong"}, 401),
        (authorized(Origin="http://evil.example"), 403),
        (authorized(Host="evil.example:8787"), 421),
    ],
)
def test_requests_without_local_access_are_refused(
    client: TestClient, headers: dict[str, str], status: int
) -> None:
    approve = client.post(
        "/api/approve", json={"plan_hash": ZERO_HASH}, headers=headers
    )
    mcp = client.post("/mcp", json={}, headers=headers)

    assert approve.status_code == status
    assert mcp.status_code == status


def test_the_token_cookie_and_same_origin_are_accepted(client: TestClient) -> None:
    response = client.post(
        "/api/approve",
        json={"plan_hash": ZERO_HASH},
        headers={"Cookie": f"{TOKEN_COOKIE}={TOKEN}", "Origin": BASE_URL},
    )

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "plan_not_found"


def test_approve_takes_a_plan_hash_only(client: TestClient) -> None:
    response = client.post(
        "/api/approve", json={"plan": {}, "plan_hash": "nope"}, headers=authorized()
    )

    assert response.status_code == 422


def _mcp_messages(response: Any) -> list[dict[str, Any]]:
    """JSON-RPC messages from a streamable-HTTP response (JSON or SSE)."""
    if response.headers["content-type"].startswith("application/json"):
        return [response.json()]
    return [
        json.loads(line[len("data:") :])
        for line in response.text.splitlines()
        if line.startswith("data:") and line[len("data:") :].strip()
    ]


class McpSession:
    """A minimal streamable-HTTP MCP client over the test client."""

    def __init__(self, client: TestClient) -> None:
        self.client = client
        self.session_id: str | None = None
        self.next_id = 1
        self.request(
            "initialize",
            {
                "protocolVersion": LATEST_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            },
        )
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _post(self, body: dict[str, Any]) -> Any:
        headers = authorized(Accept="application/json, text/event-stream")
        if self.session_id:
            headers["mcp-session-id"] = self.session_id
        response = self.client.post("/mcp", json=body, headers=headers)
        assert response.status_code < 300, response.text
        self.session_id = response.headers.get("mcp-session-id", self.session_id)
        return response

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = self.next_id
        self.next_id += 1
        response = self._post(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        )
        (reply,) = [
            message
            for message in _mcp_messages(response)
            if message.get("id") == request_id
        ]
        assert "error" not in reply, reply
        return reply["result"]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = self.request("tools/call", {"name": name, "arguments": arguments})
        assert not result.get("isError"), result
        return result["structuredContent"]


def test_an_mcp_proposal_runs_only_when_a_human_approves_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    signer: ExecutionSignerSpy = install_execution_surface_mocks(monkeypatch)
    allocation_path, policy_path = write_execution_files(tmp_path)
    allocation = json.loads(allocation_path.read_text(encoding="utf-8"))
    app = create_app(settings(policy_path), InMemoryPlanStore())

    with TestClient(app, base_url=BASE_URL) as client:
        mcp = McpSession(client)
        tools = mcp.request("tools/list", {})["tools"]
        execute_tool = next(tool for tool in tools if tool["name"] == "execute")
        assert set(execute_tool["inputSchema"]["properties"]) == {
            "allocation",
            "policy_path",
        }

        proposal = mcp.call_tool(
            "execute", {"allocation": allocation, "policy_path": str(policy_path)}
        )
        assert proposal["plan_required"] is True
        assert signer.sent == []

        stored = client.get(f"/api/plans/{proposal['plan_hash']}", headers=authorized())
        assert stored.status_code == 200
        assert stored.json()["used_at"] is None
        assert (
            stored.json()["plan"]["plan"]["steps"] == proposal["plan"]["plan"]["steps"]
        )

        approved = client.post(
            "/api/approve",
            json={"plan_hash": proposal["plan_hash"]},
            headers=authorized(Origin=BASE_URL),
        )
        replayed = client.post(
            "/api/approve",
            json={"plan_hash": proposal["plan_hash"]},
            headers=authorized(Origin=BASE_URL),
        )

    assert approved.status_code == 200, approved.text
    assert approved.json()["result"]["status"] == "success"
    assert replayed.status_code == 409
    assert replayed.json()["detail"]["code"] == "plan_used"
    assert len(signer.sent) == len(DEPOSIT_KINDS)
    # The signer's key never leaves the process.
    for response in (stored, approved, replayed):
        assert "11" * 32 not in response.text


def test_chat_reports_a_missing_claude(tmp_path: Path) -> None:
    app = create_app(
        settings(claude_bin=str(tmp_path / "no-claude")), InMemoryPlanStore()
    )
    with TestClient(app, base_url=BASE_URL) as client:
        response = client.post(
            "/api/chat", json={"message": "hi"}, headers=authorized()
        )

    assert response.status_code == 503
    assert "not installed" in response.json()["detail"]["error"]
