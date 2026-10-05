"""The FastAPI app: approval routes and page, with the MCP server at `/mcp`.

One process: the MCP tools a client calls store their plans in the same
`PlanStore` the Approve route takes them from.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import TypeAdapter

from oa_server import __version__
from oa_server.approval import approve, plan_status, reject
from oa_server.auth import LocalAccessMiddleware
from oa_server.dashboard import Dashboard
from oa_server.schemas import (
    ApproveResponse,
    BackfillResponse,
    BookResponse,
    JobRun,
    NavResponse,
    PlanHashRequest,
    PlanResponse,
    PlanSummary,
    RejectResponse,
    Review,
    ShelfResponse,
)
from oa_server.settings import Settings
from open_allocator.mcp import build_mcp
from open_allocator.service import ServiceError
from open_allocator.service.execution import review_plan
from open_allocator.service.plan_store import PlanStore

JsonObject = dict[str, Any]

log = logging.getLogger(__name__)

# The web app's pages, each served the same `index.html`.
WEB_PAGES = ("/book", "/shelf", "/performance", "/activity")

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

_REVIEW: TypeAdapter[Review] = TypeAdapter(Review)

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


def _log_failure(task: asyncio.Task[Any]) -> None:
    if not task.cancelled() and task.exception() is not None:
        log.error("NAV backfill failed", exc_info=task.exception())


def create_app(
    settings: Settings,
    plan_store: PlanStore,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    web_dist: Path = WEB_DIST,
    dashboard: Dashboard | None = None,
    backfill_every: float | None = None,
    shelf_every: float | None = None,
) -> FastAPI:
    """`backfill_every` and `shelf_every` (seconds) run the NAV backfill and the
    shelf read on start and then on that interval; None runs them only when
    asked."""
    mcp = build_mcp(plan_store, approval_url=settings.approval_url)
    mcp_app = mcp.streamable_http_app(streamable_http_path="/mcp", host=settings.host)

    async def repeat(name: str, run: Callable[[], object], every: float) -> None:
        while True:
            try:
                await asyncio.to_thread(run)
            except Exception:
                log.exception("%s failed", name)
            await asyncio.sleep(every)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        tasks = []
        if dashboard is not None and backfill_every:
            tasks.append(
                asyncio.create_task(
                    repeat("NAV backfill", dashboard.backfill, backfill_every)
                )
            )
        if dashboard is not None and shelf_every:
            board = dashboard
            tasks.append(
                asyncio.create_task(
                    repeat("shelf read", lambda: board.shelf(refresh=True), shelf_every)
                )
            )
        async with mcp.session_manager.run():
            yield
        for task in tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

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
        review: Review | None = None
        review_error: str | None = None
        try:
            review = _REVIEW.validate_python(review_plan(stored.kind, stored.plan))
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

    def board() -> Dashboard:
        if dashboard is None:
            raise HTTPException(503, {"error": "the dashboard has no database"})
        return dashboard

    @app.get("/api/book", response_model=BookResponse)
    def get_book(refresh: bool = False) -> BookResponse:
        """The account's positions and idle cash, read live (cached a minute)."""
        try:
            return board().book(refresh=refresh)
        except HTTPException:
            raise
        except Exception as error:
            raise HTTPException(502, {"error": f"book read failed: {error}"}) from error

    @app.get("/api/shelf", response_model=ShelfResponse)
    def get_shelf(refresh: bool = False) -> ShelfResponse:
        """What can be allocated to, scored, with its yield-path metrics (read
        hourly; a refresh takes about a minute)."""
        try:
            return board().shelf(refresh=refresh)
        except HTTPException:
            raise
        except Exception as error:
            raise HTTPException(
                502, {"error": f"shelf read failed: {error}"}
            ) from error

    @app.get("/api/nav", response_model=NavResponse)
    def get_nav() -> NavResponse:
        """The NAV history the backfill has derived, and what it could not read."""
        return board().nav()

    @app.post("/api/nav/backfill", response_model=BackfillResponse, status_code=202)
    async def start_backfill() -> BackfillResponse:
        """Read any closed day still missing, then rebuild NAV, in the background."""
        current = board()
        if current.backfilling:
            return BackfillResponse(started=False)
        task = asyncio.create_task(asyncio.to_thread(current.backfill))
        task.add_done_callback(_log_failure)
        return BackfillResponse(started=True)

    @app.get("/api/jobs", response_model=list[JobRun])
    def get_jobs(limit: int = 20) -> list[JobRun]:
        return board().jobs(min(max(limit, 1), 100))

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

    for page in WEB_PAGES:
        app.add_api_route(page, web_page, include_in_schema=False, methods=["GET"])

    app.add_middleware(
        LocalAccessMiddleware,
        token=settings.token,
        mcp_token=settings.mcp_token,
        allowed_hosts=settings.allowed_hosts,
        allowed_origins=settings.allowed_origins,
    )
    return app
