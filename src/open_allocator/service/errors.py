from __future__ import annotations


class ServiceError(Exception):
    """A failure the caller can act on, with a stable machine-readable code.

    `str(error)` is the detail alone, so the CLI's `{"error": ...}` output is the
    same whether a service raises this or a plain exception.
    """

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
