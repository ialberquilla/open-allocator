import json
import re
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError

from open_allocator.core import positions as positions_core
from open_allocator.exec import loops as loops_exec
from open_allocator.exec.client import RewardsResponse
from open_allocator.mcp import build_mcp
from open_allocator.service import ServiceError
from open_allocator.service import positions as positions_service
from open_allocator.service import wallet as wallet_service

ROOT = Path(__file__).resolve().parents[1]
WALLET = "0x" + "ab" * 20


def call(name: str, arguments: dict[str, Any]) -> Any:
    return anyio.run(build_mcp().call_tool, name, arguments)


def tool_error(raised: pytest.ExceptionInfo[ToolError], name: str) -> Any:
    """The JSON error object, as the model reads it after the SDK's prefix."""
    assert not isinstance(raised.value, UnexpectedToolError), "reported as a crash"
    prefix = f"Error executing tool {name}: "
    message = str(raised.value)
    assert message.startswith(prefix)
    return json.loads(message.removeprefix(prefix))


def list_tools() -> list[Any]:
    return anyio.run(build_mcp().list_tools)


def cli_inventory() -> set[str]:
    guide = (ROOT / "AGENT_GUIDE.md").read_text(encoding="utf-8")
    block = re.search(
        r"<!-- command-inventory:start -->(.*?)<!-- command-inventory:end -->",
        guide,
        re.S,
    )
    assert block is not None
    return set(re.findall(r"`([a-z-]+)`", block.group(1)))


class FakeClient:
    def __init__(self, _config: object) -> None:
        pass

    def __enter__(self) -> "FakeClient":
        return self

    def __exit__(self, *args: object) -> None:
        pass


def test_tools_are_the_read_only_cli_commands() -> None:
    tools = list_tools()

    assert {tool.name for tool in tools} == {
        "wallet-status",
        "safe-address",
        "positions",
        "rewards",
    }
    assert {tool.name for tool in tools} <= cli_inventory()
    for tool in tools:
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is True, tool.name
        assert tool.description, tool.name


def test_no_tool_accepts_a_confirmation() -> None:
    """Approval is a human action; no tool argument may stand in for it."""
    for tool in list_tools():
        arguments = set(tool.input_schema.get("properties", {}))
        assert not arguments & {"confirm", "unsafe", "autonomous"}, tool.name


def test_rewards_returns_the_cli_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = json.loads(
        (ROOT / "tests" / "fixtures" / "rewards-bearing.json").read_text(
            encoding="utf-8"
        )
    )
    requests: list[tuple[str, int | None]] = []

    class RewardsClient(FakeClient):
        def rewards(self, wallet: str, chain_id: int | None) -> RewardsResponse:
            requests.append((wallet, chain_id))
            return RewardsResponse.model_validate(fixture)

    monkeypatch.setenv("ONE_TX_API_URL", "http://localhost:3001/api/v1")
    monkeypatch.setenv("ONE_TX_API_KEY", "test-api-key")
    monkeypatch.setattr(positions_service, "OneTxClient", RewardsClient)

    result = call("rewards", {"wallet": fixture["wallet"], "chain": 8453})

    assert not result.is_error
    assert requests == [(fixture["wallet"], 8453)]
    assert result.structured_content == positions_service.rewards(
        fixture["wallet"], 8453
    )


def test_positions_carries_read_warnings_in_the_result(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Over stdio, stdout is the protocol: a warning printed there corrupts it."""
    book = positions_core.Positions(
        address=WALLET,
        holdings=(),
        idle_balances=(),
        total_position_usd=0,
        total_idle_usdc=0,
        total_usd=0,
    )
    monkeypatch.setenv("ONE_TX_API_URL", "http://localhost:3001/api/v1")
    monkeypatch.setenv("ONE_TX_API_KEY", "test-api-key")
    monkeypatch.setattr(positions_service, "OneTxClient", FakeClient)
    monkeypatch.setattr(
        loops_exec, "read_book", lambda *_args: (book, ("loop screen unreadable",))
    )

    result = call("positions", {"address": WALLET})

    assert result.structured_content == {
        **book.model_dump(mode="json"),
        "warnings": ["loop screen unreadable"],
    }
    assert capsys.readouterr().out == ""


def test_service_errors_reach_the_model_with_their_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def misconfigured(_chain_ids: object) -> object:
        raise ServiceError(
            "invalid_config", "safe-address requires SIGNER_ACCOUNT=safe"
        )

    monkeypatch.setattr(wallet_service, "safe_address", misconfigured)

    with pytest.raises(ToolError) as raised:
        call("safe-address", {})

    assert tool_error(raised, "safe-address") == {
        "error": "safe-address requires SIGNER_ACCOUNT=safe",
        "code": "invalid_config",
    }


def test_unexpected_errors_keep_their_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The SDK would otherwise reduce any crash to "Error executing tool"."""

    class DownClient(FakeClient):
        def rewards(self, _wallet: str, _chain_id: int | None) -> RewardsResponse:
            raise ConnectionError("1Tx unreachable")

    monkeypatch.setenv("ONE_TX_API_URL", "http://localhost:3001/api/v1")
    monkeypatch.setenv("ONE_TX_API_KEY", "test-api-key")
    monkeypatch.setattr(positions_service, "OneTxClient", DownClient)

    with pytest.raises(ToolError) as raised:
        call("rewards", {"wallet": WALLET})

    assert tool_error(raised, "rewards") == {"error": "1Tx unreachable"}
