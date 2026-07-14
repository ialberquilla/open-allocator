from __future__ import annotations

import importlib
import re
from pathlib import Path

import yaml

from open_allocator import cli

REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT_POINTERS = ["CLAUDE.md", "CODEX.md", "AGENTS.md", "OPENCODE.md"]
SKIPPED_DOC_DIRS = {".pytest_cache", ".ruff_cache", ".venv", "__pycache__"}
EXPECTED_SKILLS = {
    "skills/discover.md",
    "skills/score.md",
    "skills/build-allocation.md",
    "skills/execute-with-1tx.md",
    "skills/rebalance.md",
    "skills/withdraw.md",
    "skills/meta/risk-review.md",
    "skills/meta/checkpoint-protocol.md",
}
WORKFLOW_STAGE_KEYS = {
    "stage",
    "skill",
    "command",
    "produces",
    "review_focus",
    "human_approval_default",
}
REVIEW_FOCUS_CHECKS = {
    "allocation_concentration": ("open_allocator.core.allocator", "build_allocation"),
    "allocation_log": ("open_allocator.core.checkpoint", "write_allocation_log_entry"),
    "apy_descriptive": None,
    "checkpoint": ("open_allocator.core.checkpoint", "write_checkpoint"),
    "concentration": ("open_allocator.core.allocator", "build_allocation"),
    "deltas_only": ("open_allocator.core.rebalance", "plan_rebalance"),
    "dynamic_universe": None,
    "execution_announcement": None,
    "gas_readiness": ("open_allocator.exec.execute", "GasCheck"),
    "human_confirmation": None,
    "idempotency_resume": ("open_allocator.core.checkpoint", "resume_state"),
    "liquidity": ("open_allocator.core.scoring", "score_vault"),
    "min_trade_threshold": ("open_allocator.core.rebalance", "plan_rebalance"),
    "policy_candidate_filter": ("open_allocator.core.policy", "check"),
    "policy_conformance": ("open_allocator.core.policy", "check"),
    "positions_reconcile": ("open_allocator.core.positions", "reconcile"),
    "reward_dependence": ("open_allocator.core.scoring", "score_vault"),
    "scoring_factors": ("open_allocator.core.scoring", "score_vault"),
    "share_balance_exit": ("open_allocator.core.withdraw", "plan_withdraw"),
    "tx_plan_parity": ("open_allocator.exec.execute", "execute_allocation"),
    "unknown_fields": None,
    "wallet_identity": None,
    "withdrawal_constraints": ("open_allocator.core.withdraw", "plan_withdraw"),
}


def registered_cli_commands() -> list[str]:
    return [
        command.name
        for command in cli.app.registered_commands
        if command.name is not None
    ]


def documented_cli_commands() -> list[str]:
    content = (REPO_ROOT / "AGENT_GUIDE.md").read_text()
    inventory_match = re.search(
        r"<!-- command-inventory:start -->(.*?)<!-- command-inventory:end -->",
        content,
        re.DOTALL,
    )
    assert inventory_match is not None
    return re.findall(
        r"^- `([^`]+)`$",
        inventory_match.group(1),
        re.MULTILINE,
    )


def markdown_files() -> list[Path]:
    return [
        path
        for path in REPO_ROOT.rglob("*.md")
        if not any(part in SKIPPED_DOC_DIRS for part in path.parts)
    ]


def test_per_agent_files_are_thin_pointers_to_agent_guide() -> None:
    for filename in AGENT_POINTERS:
        content = (REPO_ROOT / filename).read_text()

        assert "AGENT_GUIDE.md" in content
        assert len(content.splitlines()) <= 4


def test_relative_markdown_links_point_to_existing_files() -> None:
    missing_links: list[str] = []
    link_pattern = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")

    for markdown_file in markdown_files():
        content = markdown_file.read_text()
        for raw_target in link_pattern.findall(content):
            target = raw_target.strip()
            if (
                target.startswith("#")
                or target.startswith("http://")
                or target.startswith("https://")
                or target.startswith("mailto:")
            ):
                continue
            target_without_anchor = target.split("#", maxsplit=1)[0]
            target_without_query = target_without_anchor.split("?", maxsplit=1)[0]
            linked_path = (markdown_file.parent / target_without_query).resolve()
            if not linked_path.exists():
                relative_file = markdown_file.relative_to(REPO_ROOT)
                missing_links.append(f"{relative_file}: {target}")

    assert missing_links == []


def test_agent_guide_command_inventory_matches_registered_cli_commands() -> None:
    assert documented_cli_commands() == registered_cli_commands()


def test_agent_guide_links_every_skill() -> None:
    content = (REPO_ROOT / "AGENT_GUIDE.md").read_text()

    for skill_path in EXPECTED_SKILLS:
        assert f"]({skill_path})" in content


def test_workflows_reference_existing_skills_and_documented_commands() -> None:
    documented_commands = set(documented_cli_commands())
    workflow_paths = sorted((REPO_ROOT / "workflows").glob("*.yaml"))

    assert {path.name for path in workflow_paths} == {
        "allocate.yaml",
        "rebalance.yaml",
        "withdraw.yaml",
    }

    for workflow_path in workflow_paths:
        workflow = yaml.safe_load(workflow_path.read_text())
        assert workflow["status"] == "active"
        stages = workflow["stages"]
        assert isinstance(stages, list)
        assert stages

        for stage in stages:
            assert WORKFLOW_STAGE_KEYS <= set(stage)
            skill_path = REPO_ROOT / stage["skill"]
            assert skill_path.exists(), f"{workflow_path.name}:{stage['stage']}"
            assert stage["command"] in documented_commands
            assert isinstance(stage["produces"], list)
            assert stage["produces"]
            assert isinstance(stage["review_focus"], list)
            assert stage["review_focus"]
            assert isinstance(stage["human_approval_default"], bool)


def test_workflow_review_focus_items_are_known_checks() -> None:
    missing_focus: list[str] = []

    for workflow_path in sorted((REPO_ROOT / "workflows").glob("*.yaml")):
        workflow = yaml.safe_load(workflow_path.read_text())
        for stage in workflow["stages"]:
            for focus in stage["review_focus"]:
                if focus not in REVIEW_FOCUS_CHECKS:
                    missing_focus.append(f"{workflow_path.name}:{stage['stage']}:{focus}")

    assert missing_focus == []

    for check in REVIEW_FOCUS_CHECKS.values():
        if check is None:
            continue
        module_name, attribute_name = check
        module = importlib.import_module(module_name)
        assert hasattr(module, attribute_name)
