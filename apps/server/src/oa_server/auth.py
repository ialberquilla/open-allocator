"""Local-only access: a per-start token, an allowed Host, and a same-origin check.

The server binds 127.0.0.1, but any page in the user's browser can still send it
requests. The Host check stops DNS rebinding, the Origin check stops cross-site
requests, and the token stops anything that did not get it from the launcher.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Iterable
from http.cookies import SimpleCookie

from starlette.types import ASGIApp, Receive, Scope, Send

TOKEN_COOKIE = "oa_token"
# Reachable without the token: it reports nothing about the wallet.
PUBLIC_PATHS = frozenset({"/api/health"})


def new_token() -> str:
    return secrets.token_urlsafe(32)


class LocalAccessMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        token: str,
        allowed_hosts: Iterable[str],
        allowed_origins: Iterable[str],
    ) -> None:
        self.app = app
        self.token = token
        self.allowed_hosts = frozenset(allowed_hosts)
        self.allowed_origins = frozenset(allowed_origins)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {
            name.decode("latin-1").lower(): value.decode("latin-1")
            for name, value in scope["headers"]
        }
        refusal = self._refusal(scope["path"], headers)
        if refusal is not None:
            status, message = refusal
            await _reply(send, status, message)
            return
        await self.app(scope, receive, send)

    def _refusal(self, path: str, headers: dict[str, str]) -> tuple[int, str] | None:
        if headers.get("host") not in self.allowed_hosts:
            return 421, "host not allowed"
        origin = headers.get("origin")
        if origin is not None and origin not in self.allowed_origins:
            return 403, "origin not allowed"
        if path in PUBLIC_PATHS:
            return None
        if not self._has_token(headers):
            return 401, "missing or wrong token"
        return None

    def _has_token(self, headers: dict[str, str]) -> bool:
        supplied: list[str] = []
        authorization = headers.get("authorization", "")
        if authorization.lower().startswith("bearer "):
            supplied.append(authorization[7:].strip())
        if "cookie" in headers:
            cookie = SimpleCookie()
            cookie.load(headers["cookie"])
            if TOKEN_COOKIE in cookie:
                supplied.append(cookie[TOKEN_COOKIE].value)
        return any(secrets.compare_digest(value, self.token) for value in supplied)


async def _reply(send: Send, status: int, message: str) -> None:
    body = json.dumps({"error": message}).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
