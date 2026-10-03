import json
import re
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError

from open_allocator import mcp as mcp_module
from open_allocator.core import positions as positions_core
from open_allocator.core import universe as universe_core
from open_allocator.core.types import Vault
from open_allocator.exec import execute as execute_exec
from open_allocator.exec import gas as gas_module
from open_allocator.exec import loops as loops_exec
from open_allocator.exec.client import RewardsResponse
from open_allocator.mcp import build_mcp
from open_allocator.service import ServiceError
from open_allocator.service import allocation as allocation_service
from open_allocator.service import execution as execution_service
from open_allocator.service import positions as positions_service
from open_allocator.service import universe as universe_service
from open_allocator.service import wallet as wallet_service
from open_allocator.service.plan_store import InMemoryPlanStore, plan_hash

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


def test_tools_are_cli_commands_that_change_nothing() -> None:
    tools = list_tools()

    assert {tool.name for tool in tools} == {
        "wallet-status",
        "safe-address",
        "positions",
        "rewards",
        "list-vaults",
        "score-vault",
        "screen",
        "build-allocation",
        "simulate",
        "execute",
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


def vault(instrument_id: str, *, chain_id: int, apy: float) -> Vault:
    return Vault(
        instrument_id=instrument_id,
        protocol=instrument_id.split("-")[1],
        chain_id=chain_id,
        asset="USDC",
        apy=apy,
        tvl_usd=10_000_000,
        apy_series=(apy, apy * 0.98, apy * 1.02, apy),
    )


SHELF = [
    vault("base-aave-usdc", chain_id=8453, apy=4.0),
    vault("arb-morpho-usdc", chain_id=42161, apy=5.0),
    vault("op-compound-usdc", chain_id=10, apy=4.5),
]


def test_list_vaults_carries_skipped_instruments_in_the_result(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A shrunk universe has to be visible, and stdout is the protocol."""
    skipped = universe_core.SkippedInstrument(
        instrument_id="eth-broken-usdc", reason="missing tvl"
    )
    monkeypatch.setenv("ONE_TX_API_URL", "http://localhost:3001/api/v1")
    monkeypatch.setenv("ONE_TX_API_KEY", "test-api-key")
    monkeypatch.setattr(universe_service, "OneTxClient", FakeClient)
    monkeypatch.setattr(
        universe_core, "discover_instruments", lambda _client: (SHELF, (skipped,))
    )
    monkeypatch.setattr(
        universe_service, "enrich_vaults", lambda _client, vaults, days: vaults
    )

    result = call("list-vaults", {"sort": "apy"})

    assert not result.is_error
    payload = result.structured_content
    assert [row["instrument_id"] for row in payload["vaults"]] == [
        "arb-morpho-usdc",
        "op-compound-usdc",
        "base-aave-usdc",
    ]
    assert payload["warnings"] == [
        {
            "warning": "skipped_instruments",
            "instruments": [
                {"instrument_id": "eth-broken-usdc", "reason": "missing tvl"}
            ],
        }
    ]
    assert capsys.readouterr().out == ""


def test_score_vault_reports_an_unknown_instrument_with_its_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        universe_service, "discover_vaults", lambda **_kwargs: list(SHELF)
    )

    with pytest.raises(ToolError) as raised:
        call("score-vault", {"instrument_id": "nope"})

    assert tool_error(raised, "score-vault") == {
        "error": "instrument not found: nope",
        "code": "not_found",
    }


def test_build_allocation_result_feeds_simulate_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The model passes `allocation` from one tool straight into the next."""
    monkeypatch.setattr(
        allocation_service, "discover_vaults", lambda **_kwargs: list(SHELF)
    )
    monkeypatch.setattr(gas_module, "live_pricing", lambda _chain_ids: None)
    simulated: list[object] = []

    def fake_simulate(allocation: object, **_kwargs: object) -> dict[str, Any]:
        simulated.append(allocation_service.parse_allocation(allocation))
        return {"scorecard": "ok"}

    monkeypatch.setattr(allocation_service, "simulate", fake_simulate)
    arguments = {"amount": 10_000, "policy_path": str(ROOT / "policy.yaml")}

    built = call("build-allocation", arguments)

    assert not built.is_error
    allocation = built.structured_content["allocation"]
    assert allocation == allocation_service.build_allocation(
        10_000, policy=ROOT / "policy.yaml"
    )
    assert built.structured_content["warnings"] == []

    scorecard = call("simulate", {"allocation": allocation})

    assert scorecard.structured_content == {"scorecard": "ok", "warnings": []}
    assert simulated == [allocation_service.parse_allocation(allocation)]


def test_build_allocation_requires_an_amount() -> None:
    with pytest.raises(ToolError) as raised:
        call("build-allocation", {})

    assert tool_error(raised, "build-allocation") == {
        "error": "amount required: pass --amount or set amount_usd in the spec",
        "code": "invalid_input",
    }


def test_execute_stores_the_plan_for_approval_and_sends_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = {"account": WALLET, "plan": {"steps": []}}
    report = {"status": "planned", "messages": ["dry-run only"]}

    def fake_plan(allocation: object, **kwargs: Any) -> dict[str, Any]:
        assert allocation == {"legs": [], "total_usd": 0}
        kwargs["on_warning"]({"warning": "skipped_instruments", "instruments": []})
        return {
            "kind": "execute",
            "plan": document,
            "plan_hash": plan_hash("execute", document),
            "report": report,
        }

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("an execution tool applied a plan")

    monkeypatch.setattr(execution_service, "plan_execute", fake_plan)
    monkeypatch.setattr(execution_service, "apply_execute", refuse)
    monkeypatch.setattr(execute_exec, "apply_allocation_plan", refuse)
    store = InMemoryPlanStore()

    result = anyio.run(
        build_mcp(store).call_tool,
        "execute",
        {"allocation": {"legs": [], "total_usd": 0}},
    )

    payload = result.structured_content
    assert payload["plan_required"] is True
    assert payload["plan_hash"] == plan_hash("execute", document)
    assert payload["plan"] == report
    assert payload["warnings"] == [
        {"warning": "skipped_instruments", "instruments": []}
    ]
    # Over stdio there is no approval page to link to.
    assert "approval_url" not in payload
    stored = store.get(payload["plan_hash"])
    assert stored is not None and stored.used_at is None
    assert stored.plan == document


def test_execute_links_to_the_approval_page_when_the_host_has_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = {"account": WALLET, "plan": {"steps": []}}

    def fake_plan(_allocation: object, **_kwargs: Any) -> dict[str, Any]:
        return {
            "kind": "execute",
            "plan": document,
            "plan_hash": plan_hash("execute", document),
            "report": {"status": "planned"},
        }

    monkeypatch.setattr(execution_service, "plan_execute", fake_plan)
    server = build_mcp(
        InMemoryPlanStore(), approval_url=lambda digest: f"http://host/approve/{digest}"
    )

    result = anyio.run(
        server.call_tool, "execute", {"allocation": {"legs": [], "total_usd": 0}}
    )

    payload = result.structured_content
    assert payload["approval_url"] == f"http://host/approve/{payload['plan_hash']}"


def test_nothing_in_the_mcp_adapter_can_apply_a_plan() -> None:
    source = Path(mcp_module.__file__).read_text(encoding="utf-8")

    for name in (
        "apply_execute",
        "apply_approved",
        "apply_allocation_plan",
        "take(",
        "reject(",
    ):
        assert name not in source, name
