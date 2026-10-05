"""Command logic shared by every adapter (CLI, MCP, HTTP).

Each function takes typed arguments and returns the JSON-able dict the CLI prints.
Nothing here writes to stdout or exits: the stdio MCP server owns stdout, and a
long-running server must not exit. Adapters translate `ServiceError` into their own
error shape.
"""

from open_allocator.service._common import use_state_backend
from open_allocator.service.errors import ServiceError

__all__ = ["ServiceError", "use_state_backend"]
