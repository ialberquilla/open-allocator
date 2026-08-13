"""The library must work when installed, not only when run from a checkout.

Every one of these would have passed while `validate-mandate` was broken on an
installed wheel, had they existed: the suite runs from the source tree, where
walking `__file__` up to the repository root happens to land somewhere real. The
tests below assert the two things the source tree cannot vouch for on its own —
that runtime data lives inside the package, and that the wheel actually carries it.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from open_allocator.resources import (
    PACKAGE_ROOT,
    SCHEMAS_DIR,
    SKILLS_DIR,
    WORKFLOWS_DIR,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIRS = {
    "schemas": SCHEMAS_DIR,
    "skills": SKILLS_DIR,
    "workflows": WORKFLOWS_DIR,
}


def test_runtime_data_lives_inside_the_package() -> None:
    """Anything read at runtime has to travel with the package into site-packages."""
    for name, directory in RUNTIME_DIRS.items():
        assert directory.is_dir(), f"{name} is missing from {PACKAGE_ROOT}"
        assert directory.is_relative_to(PACKAGE_ROOT), (
            f"{name} sits outside the package, so a wheel cannot carry it"
        )
        assert any(directory.iterdir()), f"{name} is empty"


def test_no_module_resolves_paths_above_the_package() -> None:
    """`Path(__file__).parents[3]` is the repository root only in a checkout.

    From `site-packages/open_allocator/core/` the same expression lands on the
    environment's `lib/python3.12/`, which holds nothing. Reach for
    `open_allocator.resources` instead.
    """
    offenders = [
        f"{path.relative_to(REPO_ROOT)}:{number}"
        for path in (REPO_ROOT / "src").rglob("*.py")
        for number, line in enumerate(path.read_text().splitlines(), start=1)
        if re.search(r"__file__.*parents\[", line)
    ]

    assert offenders == [], (
        "these resolve a path by walking __file__ upward, which breaks once "
        f"installed: {offenders}"
    )


def test_the_built_wheel_carries_the_runtime_data(tmp_path: Path) -> None:
    """The packaging half: being inside the package only helps if the build ships it."""
    if shutil.which("uv") is None:
        pytest.skip("uv is required to build the wheel")

    build = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(tmp_path)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert build.returncode == 0, build.stderr

    (wheel,) = tmp_path.glob("*.whl")
    entries = zipfile.ZipFile(wheel).namelist()

    for name, directory in RUNTIME_DIRS.items():
        expected = {
            f"open_allocator/{name}/{path.relative_to(directory)}"
            for path in directory.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        }
        assert expected <= set(entries), (
            f"the wheel is missing {sorted(expected - set(entries))}"
        )


def test_schemas_resolve_by_name_from_the_package() -> None:
    """The failure that started this: `available schemas: none` once installed."""
    from open_allocator.core.schema import _schema_paths

    assert set(_schema_paths()) >= {"policy", "mandate", "allocation"}
