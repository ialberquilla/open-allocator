"""Local-only access: two tokens, an allowed Host, and a same-origin check.

The server binds 127.0.0.1, but any page in the user's browser can still send it
requests. The Host check stops DNS rebinding, the Origin check stops cross-site
requests, and the tokens stop anything that did not get one from the launcher.

The tokens are separate on purpose. The MCP token sits in the user's MCP client
config, where a model with file access could read it, so it opens `/mcp` and
nothing else. The browser token is printed at start and kept in an HttpOnly
cookie; only it reaches the approval routes.
"""

from __future__ import annotations

import html
import json
import os
import secrets
from collections.abc import Iterable
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs

from starlette.types import ASGIApp, Receive, Scope, Send

TOKEN_COOKIE = "oa_token"
MCP_PATH = "/mcp"
# Swaps the browser token in its query string for the cookie.
LOGIN_PATH = "/login"
# Reachable without a token: they report nothing about the wallet.
PUBLIC_PATHS = frozenset({"/api/health", LOGIN_PATH})

UNAUTHORIZED_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Open Allocator</title>
<meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style="font-family: system-ui, sans-serif; max-width: 36rem; margin: 4rem auto;
padding: 0 1rem; line-height: 1.5">
<h1 style="font-size: 1.25rem">Not signed in</h1>
<p>{message}. Open the link <code>open-allocator-ui</code> printed when it
started, then come back to this page.</p>
</body></html>
"""


def new_token() -> str:
    return secrets.token_urlsafe(32)


def load_mcp_token(path: Path) -> str:
    """The MCP token kept at `path`, created on first use.

    It survives restarts, so an MCP client is configured once.
    """
    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if token:
            return token
    path.parent.mkdir(parents=True, exist_ok=True)
    token = new_token()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(token + "\n")
    return token


def _is_mcp(path: str) -> bool:
    return path == MCP_PATH or path.startswith(MCP_PATH + "/")


class LocalAccessMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        token: str,
        mcp_token: str,
        allowed_hosts: Iterable[str],
        allowed_origins: Iterable[str],
    ) -> None:
        self.app = app
        self.token = token
        self.mcp_token = mcp_token
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
        path = scope["path"]
        refusal = self._refusal(path, headers)
        if refusal is not None:
            status, message = refusal
            await _reply(send, status, message, page=_wants_page(scope, headers))
            return
        if path == LOGIN_PATH:
            await self._login(scope, send)
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
        if _is_mcp(path):
            if not _matches(_bearer(headers), self.mcp_token):
                return 401, "missing or wrong MCP token"
            return None
        if not _matches([*_bearer(headers), *_cookie(headers)], self.token):
            return 401, "missing or wrong token"
        return None

    async def _login(self, scope: Scope, send: Send) -> None:
        query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
        if not _matches(query.get("token", []), self.token):
            await _reply(send, 401, "That sign-in link is stale or wrong", page=True)
            return
        # Only a path on this server, never another site.
        target = query.get("next", ["/"])[0]
        if not target.startswith("/") or target.startswith("//"):
            target = "/"
        cookie = (
            f"{TOKEN_COOKIE}={self.token}; Path=/; HttpOnly; SameSite=Lax"
        ).encode("latin-1")
        await send(
            {
                "type": "http.response.start",
                "status": 303,
                "headers": [
                    (b"location", target.encode("latin-1")),
                    (b"set-cookie", cookie),
                    (b"cache-control", b"no-store"),
                    (b"content-length", b"0"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": b""})


def _bearer(headers: dict[str, str]) -> list[str]:
    authorization = headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        return [authorization[7:].strip()]
    return []


def _cookie(headers: dict[str, str]) -> list[str]:
    if "cookie" not in headers:
        return []
    cookie = SimpleCookie()
    cookie.load(headers["cookie"])
    return [cookie[TOKEN_COOKIE].value] if TOKEN_COOKIE in cookie else []


def _matches(supplied: Iterable[str], token: str) -> bool:
    return any(secrets.compare_digest(value, token) for value in supplied)


def _wants_page(scope: Scope, headers: dict[str, str]) -> bool:
    """A browser navigating to a page gets HTML; API and MCP callers get JSON."""
    path = scope["path"]
    return (
        scope["method"] == "GET"
        and not path.startswith("/api/")
        and not _is_mcp(path)
        and "text/html" in headers.get("accept", "")
    )


async def _reply(send: Send, status: int, message: str, *, page: bool) -> None:
    if page:
        body = UNAUTHORIZED_PAGE.format(message=html.escape(message)).encode("utf-8")
        content_type = b"text/html; charset=utf-8"
    else:
        body = json.dumps({"error": message}).encode("utf-8")
        content_type = b"application/json"
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", content_type),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
