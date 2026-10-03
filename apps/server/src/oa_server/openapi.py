"""Write the server's OpenAPI document, which the web app generates its types from.

uv run python -m oa_server.openapi <path>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from oa_server.app import create_app
from oa_server.settings import Settings
from open_allocator.service.plan_store import InMemoryPlanStore


def main(argv: list[str] | None = None) -> None:
    (target,) = sys.argv[1:] if argv is None else argv
    app = create_app(Settings(token="", mcp_token=""), InMemoryPlanStore())
    document = json.dumps(app.openapi(), indent=2) + "\n"
    Path(target).write_text(document, encoding="utf-8")


if __name__ == "__main__":
    main()
