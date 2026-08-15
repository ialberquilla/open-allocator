"""Every CLI invocation written in a packaged skill must actually be runnable.

A skill is not documentation. It is a set of instructions an agent will follow
literally, so every command in one is a *claim about the CLI* and needs the same
verification as code. This has now gone wrong twice:

  * ``screen --policy policy.yaml`` — caught during review before shipping.
  * ``list-vaults --enrich`` — NOT caught, and shipped in the wheel. An agent
    following ``skills/mandate.md`` step 2 hits ``No such option: --enrich`` on
    its first look at the shelf.

Both were written by reasoning about what the command ought to accept rather
than by checking what it does. Prose review does not reliably catch this, which
is what these tests are for: the flags are extracted from the skill text and
checked against the Typer app itself, so the *source of truth is the CLI* and a
renamed flag breaks the suite rather than an agent run.

Deliberately narrow in scope. These tests verify that a command exists and that
the options named on it are real. They say nothing about whether the argument
VALUES are sensible — ``--policy some-nonexistent.yaml`` passes here — because
that needs a live shelf and belongs in the skill's own worked example.
"""

from __future__ import annotations

import re

import pytest
import typer

from open_allocator import resources
from open_allocator.cli import app

# Matches an `open-allocator <command> ...` invocation up to the end of the
# line or the closing backtick of an inline code span, whichever comes first.
_INVOCATION = re.compile(r"open-allocator\s+([a-z][a-z0-9-]*)((?:[^\n`]|\\\n)*)")
_OPTION = re.compile(r"(--[a-z][a-z0-9-]*)")


def _cli_surface() -> dict[str, set[str]]:
    """Map every registered command name to the option strings it accepts.

    Read off the Typer app itself rather than from a hand-kept list, so the CLI
    stays the single source of truth: renaming a flag updates this surface on
    the next run and breaks any skill still naming the old one. Typer vendors
    its own click (`typer._click`), so this goes through `typer.main` and the
    group's own `.commands` rather than importing click directly.
    """
    group = typer.main.get_command(app)
    surface: dict[str, set[str]] = {}
    for name, sub in group.commands.items():
        options: set[str] = set()
        for param in sub.params:
            options.update(param.opts)
            options.update(param.secondary_opts)
        surface[name] = options
    return surface


def _skill_documents() -> list[tuple[str, str]]:
    """Every packaged skill, as (name, text)."""
    paths = sorted(resources.SKILLS_DIR.glob("*.md"))
    assert paths, f"no skills found under {resources.SKILLS_DIR}"
    return [(path.name, path.read_text(encoding="utf-8")) for path in paths]


def _invocations(text: str) -> list[tuple[str, list[str]]]:
    found = []
    for match in _INVOCATION.finditer(text):
        found.append((match.group(1), _OPTION.findall(match.group(2))))
    return found


@pytest.fixture(scope="module")
def surface() -> dict[str, set[str]]:
    return _cli_surface()


def test_the_extractor_sees_something():
    """Guard the guard: a regex that matches nothing would pass every test below."""
    total = sum(len(_invocations(text)) for _, text in _skill_documents())
    assert total > 10, (
        f"only {total} CLI invocations found across the packaged skills — the "
        "extractor is probably broken, which would make every other test in "
        "this file vacuous"
    )


def test_every_command_named_in_a_skill_exists(surface):
    unknown = []
    for name, text in _skill_documents():
        for command, _ in _invocations(text):
            if command not in surface:
                unknown.append(f"{name}: open-allocator {command}")
    assert not unknown, (
        "skills name CLI commands that do not exist:\n  "
        + "\n  ".join(sorted(set(unknown)))
        + f"\navailable: {', '.join(sorted(surface))}"
    )


def test_every_flag_named_in_a_skill_exists(surface):
    """The `--enrich` case. This is the test that would have caught it."""
    unknown = []
    for name, text in _skill_documents():
        for command, options in _invocations(text):
            if command not in surface:
                continue  # reported by the test above; do not double-report
            for option in options:
                if option not in surface[command]:
                    unknown.append(
                        f"{name}: `open-allocator {command} {option}` — "
                        f"{command} accepts {', '.join(sorted(surface[command]))}"
                    )
    assert not unknown, "skills name CLI flags that do not exist:\n  " + "\n  ".join(
        sorted(set(unknown))
    )
