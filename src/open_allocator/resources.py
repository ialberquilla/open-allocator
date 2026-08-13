"""Where the library's own data files live, resolved the one way that survives install.

`schemas/`, `skills/` and `workflows/` are package data, not repo files. They used
to sit at the repository root and be reached by walking `__file__` upward, which is
correct in a source checkout and wrong everywhere else: from
`site-packages/open_allocator/core/` the same walk lands on the environment's
`lib/python3.12/`, which holds nothing, so every schema-validating command failed on
an installed library while the whole test suite passed from the source tree.

So the directories moved *inside* the package and are resolved through
`importlib.resources`. The point of the move is that there is now only one layout to
be right about — source tree and wheel resolve the same path by the same means, and
there is no fallback branch that only one of them ever takes.

`workflows/*.yaml` name their skills relative to this package root (`skills/x.md`),
which is what lets a workflow be read by a container that has no repo checkout.
"""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

# open_allocator is always installed unzipped (wheel, editable, or source tree), so
# the traversable is a real directory and `Path` keeps `.glob` / `.exists` available.
PACKAGE_ROOT = Path(str(files("open_allocator")))

SCHEMAS_DIR = PACKAGE_ROOT / "schemas"
SKILLS_DIR = PACKAGE_ROOT / "skills"
WORKFLOWS_DIR = PACKAGE_ROOT / "workflows"
