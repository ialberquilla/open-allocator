"""The FastAPI app: approval routes and page, with the MCP server at `/mcp`.

One process: the MCP tools a client calls store their plans in the same
`PlanStore` the Approve route takes them from.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

from oa_server import __version__
from oa_server.approval import approve, plan_status, reject
from oa_server.auth import LocalAccessMiddleware
from oa_server.schemas import (
    ApproveResponse,
    ExecuteReview,
    PlanHashRequest,
    PlanResponse,
    PlanSummary,
    RejectResponse,
)
from oa_server.settings import Settings
from open_allocator.mcp import build_mcp
from open_allocator.service import ServiceError
from open_allocator.service.execution import review_plan
from open_allocator.service.plan_store import PlanStore

JsonObject = dict[str, Any]

# The built web app (`make web`); not in the repository.
WEB_DIST = Path(__file__).resolve().parent / "web_dist"

NOT_BUILT_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Open Allocator</title></head>
<body style="font-family: system-ui, sans-serif; max-width: 36rem; margin: 4rem auto;
padding: 0 1rem; line-height: 1.5">
<h1 style="font-size: 1.25rem">The web app is not built</h1>
<p>Run <code>make web</code> from the repository root, then reload.</p>
</body></html>
"""

# How a refused approval or rejection maps to HTTP.
_APPROVAL_STATUS = {
    "plan_not_found": 404,
    "plan_expired": 410,
    "plan_used": 409,
    "plan_mismatch": 409,
    "policy_violation": 422,
}


def _refused(error: ServiceError) -> HTTPException:
    return HTTPException(
        _APPROVAL_STATUS.get(error.code, 400),
        {"error": error.detail, "code": error.code},
    )


def create_app(
    settings: Settings,
    plan_store: PlanStore,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    web_dist: Path = WEB_DIST,
) -> FastAPI:
    mcp = build_mcp(plan_store, approval_url=settings.approval_url)
    mcp_app = mcp.streamable_http_app(streamable_http_path="/mcp", host=settings.host)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        async with mcp.session_manager.run():
            yield

    app = FastAPI(title="Open Allocator", version=__version__, lifespan=lifespan)

    @app.get("/api/health")
    def health() -> JsonObject:
        return {"ok": True, "version": __version__}

    @app.get("/api/plans", response_model=list[PlanSummary])
    def list_plans(limit: int = 20) -> list[PlanSummary]:
        """The most recently proposed plans, newest first."""
        now = clock()
        return [
            PlanSummary(
                plan_hash=stored.plan_hash,
                kind=stored.kind,
                status=plan_status(stored, now),
                created_at=stored.created_at,
                expires_at=stored.expires_at,
            )
            for stored in plan_store.recent(min(max(limit, 1), 100))
        ]

    @app.get("/api/plans/{plan_hash}", response_model=PlanResponse)
    def get_plan(plan_hash: str) -> PlanResponse:
        """The stored plan an approval would apply. Show this, not the model's copy."""
        stored = plan_store.get(plan_hash)
        if stored is None:
            raise HTTPException(404, {"error": f"no plan {plan_hash}"})
        review: ExecuteReview | None = None
        review_error: str | None = None
        try:
            review = ExecuteReview.model_validate(review_plan(stored.kind, stored.plan))
        except Exception as error:
            review_error = str(error)
        return PlanResponse(
            plan_hash=stored.plan_hash,
            kind=stored.kind,
            status=plan_status(stored, clock()),
            review=review,
            review_error=review_error,
            plan=stored.plan,
            created_at=stored.created_at,
            expires_at=stored.expires_at,
            used_at=stored.used_at,
            result=stored.result,
            error=stored.error,
        )

    # Plain `def`s: approval blocks on the network, so FastAPI runs these in a
    # worker thread rather than on the event loop.
    @app.post("/api/approve", response_model=ApproveResponse)
    def approve_plan(request: PlanHashRequest) -> ApproveResponse:
        """Apply the stored plan with this hash, once, after a policy re-check."""
        try:
            result = approve(plan_store, request.plan_hash, policy=settings.policy_path)
        except ServiceError as error:
            raise _refused(error) from error
        except Exception as error:
            raise HTTPException(500, {"error": str(error)}) from error
        return ApproveResponse(plan_hash=request.plan_hash, result=result)

    @app.post("/api/reject", response_model=RejectResponse)
    def reject_plan(request: PlanHashRequest) -> RejectResponse:
        """Retire the stored plan with this hash without applying it."""
        try:
            reject(plan_store, request.plan_hash)
        except ServiceError as error:
            raise _refused(error) from error
        return RejectResponse(plan_hash=request.plan_hash, status="rejected")

    # The MCP endpoint, served by the same process and store.
    app.router.routes.extend(mcp_app.routes)

    # The web app: static assets, and its page for every route it handles.
    if (web_dist / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=web_dist / "assets"), "assets")

    def web_page() -> Response:
        index = web_dist / "index.html"
        if not index.is_file():
            return HTMLResponse(NOT_BUILT_PAGE, status_code=503)
        return FileResponse(index, headers={"cache-control": "no-store"})

    @app.get("/", include_in_schema=False)
    def home() -> Response:
        return web_page()

    @app.get("/approve/{plan_hash}", include_in_schema=False)
    def approval_page(plan_hash: str) -> Response:
        return web_page()

    app.add_middleware(
        LocalAccessMiddleware,
        token=settings.token,
        mcp_token=settings.mcp_token,
        allowed_hosts=settings.allowed_hosts,
        allowed_origins=settings.allowed_origins,
    )
    return app
