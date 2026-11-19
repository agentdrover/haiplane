"""REST operations for the explicit roadmap source."""

from fastapi import APIRouter, Depends, Header, Path, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from typing import Annotated

from hub.auth import current_identity, require_admin
from hub.config import TokenIdentity
from hub.executor_host_api import _db
from hub.services import roadmap_requests, role_orders

router = APIRouter()

_MAX_ID = 2**63 - 1
ResourceId = Annotated[int, Path(gt=0, le=_MAX_ID)]


class RoadmapGrant(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: int = Field(gt=0, le=_MAX_ID)
    steward_principal_id: int = Field(gt=0, le=_MAX_ID)
    roadmap_principal_id: int = Field(gt=0, le=_MAX_ID)
    signal_task_id: int | None = Field(default=None, gt=0, le=_MAX_ID)
    ttl_days: int = Field(default=7, ge=1, le=30)


class RoadmapRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: int = Field(gt=0, le=_MAX_ID)
    source_order_id: int | None = Field(default=None, gt=0, le=_MAX_ID)
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    question: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)
    ]


@router.put("/api/tasks/{task_id}/roadmap-grant")
async def api_grant(
    task_id: ResourceId,
    body: RoadmapGrant,
    request: Request,
    who: TokenIdentity = Depends(require_admin),
):
    return await roadmap_requests.grant(
        _db(request),
        who,
        task_id,
        body.plan_id,
        body.steward_principal_id,
        body.roadmap_principal_id,
        body.signal_task_id,
        body.ttl_days,
    )


@router.delete("/api/tasks/{task_id}/roadmap-grant")
async def api_revoke(
    task_id: ResourceId, request: Request, who: TokenIdentity = Depends(require_admin)
):
    return await roadmap_requests.revoke(_db(request), who, task_id)


@router.get("/api/tasks/{task_id}/roadmap-grant")
async def api_signal(
    task_id: ResourceId,
    request: Request,
    response: Response,
    who: TokenIdentity = Depends(current_identity),
    after_order_id: int = Query(default=0, ge=0, le=_MAX_ID),
):
    response.headers["Cache-Control"] = "no-store"
    return await roadmap_requests.grant_signal(
        _db(request), who, task_id, after_order_id
    )


@router.post("/api/tasks/{task_id}/roadmap-requests")
async def api_issue(
    task_id: ResourceId,
    body: RoadmapRequest,
    request: Request,
    who: TokenIdentity = Depends(current_identity),
):
    return await roadmap_requests.issue(
        _db(request),
        who,
        task_id,
        body.plan_id,
        body.request_id,
        body.question,
        body.source_order_id,
    )


@router.get("/api/roadmap/orders/{order_id}")
async def api_started_question(
    order_id: ResourceId,
    request: Request,
    response: Response,
    who: TokenIdentity = Depends(current_identity),
    source_key: str = Header(default="", alias="X-Hub-Roadmap-Source"),
):
    response.headers["Cache-Control"] = "no-store"
    return await roadmap_requests.started_question(
        _db(request), who, order_id, source_key, role_orders.RUN_MAX_MINUTES
    )
