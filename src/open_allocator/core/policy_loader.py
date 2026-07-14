from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from open_allocator.core.schema import validate
from open_allocator.core.types import Policy


def load_policy(path: str | Path) -> Policy:
    policy_path = Path(path)
    with policy_path.open(encoding="utf-8") as file:
        raw_policy: Any = yaml.safe_load(file)

    validate(raw_policy, "policy")
    return Policy.model_validate(raw_policy)
