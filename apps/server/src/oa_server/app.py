"""The FastAPI app: approval routes, with the MCP server at `/mcp`.

One process: the MCP tools a client calls store their plans in the same
`PlanStore` the Approve route takes them from.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from oa_server import __version__
from oa_server.approval import approve
from oa_server.auth import LocalAccessMiddleware
from oa_server.settings import Settings
from open_allocator.mcp import build_mcp
from open_allocator.service import ServiceError
from open_allocator.service.plan_store import PlanStore

JsonObject = dict[str, Any]

# How a refused approval maps to HTTP.
_APPROVAL_STATUS = {
    "plan_not_found": 404,
    "plan_expired": 410,
    "plan_used": 409,
    "plan_mismatch": 409,
    "policy_violation": 422,
}


class ApproveRequest(BaseModel):
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class ApproveResponse(BaseModel):
    plan_hash: str
    result: JsonObject


class PlanResponse(BaseModel):
    plan_hash: str
    kind: str
    plan: JsonObject
    created_at: datetime
    expires_at: datetime
    used_at: datetime | None


def create_app(settings: Settings, plan_store: PlanStore) -> FastAPI:
    mcp = build_mcp(plan_store)
    mcp_app = mcp.streamable_http_app(streamable_http_path="/mcp", host=settings.host)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        async with mcp.session_manager.run():
            yield

    app = FastAPI(title="Open Allocator", version=__version__, lifespan=lifespan)

    @app.get("/api/health")
    def health() -> JsonObject:
        return {"ok": True, "version": __version__}

    @app.get("/api/plans/{plan_hash}", response_model=PlanResponse)
    def get_plan(plan_hash: str) -> PlanResponse:
        """The stored plan an approval would apply. Show this, not the model's copy."""
        stored = plan_store.get(plan_hash)
        if stored is None:
            raise HTTPException(404, {"error": f"no plan {plan_hash}"})
        return PlanResponse(
            plan_hash=stored.plan_hash,
            kind=stored.kind,
            plan=stored.plan,
            created_at=stored.created_at,
            expires_at=stored.expires_at,
            used_at=stored.used_at,
        )

    # A plain `def`: approval blocks on the network, so FastAPI runs it in a
    # worker thread rather than on the event loop.
    @app.post("/api/approve", response_model=ApproveResponse)
    def approve_plan(request: ApproveRequest) -> ApproveResponse:
        """Apply the stored plan with this hash, once, after a policy re-check."""
        try:
            result = approve(plan_store, request.plan_hash, policy=settings.policy_path)
        except ServiceError as error:
            raise HTTPException(
                _APPROVAL_STATUS.get(error.code, 400),
                {"error": error.detail, "code": error.code},
            ) from error
        except Exception as error:
            raise HTTPException(500, {"error": str(error)}) from error
        return ApproveResponse(plan_hash=request.plan_hash, result=result)

    # The MCP endpoint, served by the same process and store.
    app.router.routes.extend(mcp_app.routes)
    app.add_middleware(
        LocalAccessMiddleware,
        token=settings.token,
        allowed_hosts=settings.allowed_hosts,
        allowed_origins=settings.allowed_origins,
    )
    return app
