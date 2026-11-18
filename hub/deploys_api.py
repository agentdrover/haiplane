"""Read the deployment pipeline and history of one authorized project."""

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from hub import repository as repo
from hub.auth import require_permission
from hub.config import TokenIdentity
from hub.db import fetchall
from hub.services import project_access
from hub.services.deploys_view import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    DeploysView,
    deploys_view,
)

router = APIRouter()


async def _authorize_project(db, identity: TokenIdentity, project_id: int) -> None:
    private = await fetchall(
        db, "SELECT 1 FROM private_project_security WHERE project_id = ?", (project_id,)
    )
    if private:
        decision = await project_access.decide(
            db, identity, permission="tasks.read", project_id=project_id
        )
        if not decision.allowed:
            raise HTTPException(404, "project not found")


@router.get("/api/deploys", response_model=DeploysView)
async def api_deploys(
    request: Request,
    project: str = "default",
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    identity: TokenIdentity = Depends(require_permission("tasks.read")),
) -> DeploysView:
    db = getattr(request.state, "db", None) or request.app.state.db
    if identity.scopes is not None and project not in identity.scopes:
        raise HTTPException(404, "project not found")
    row = await repo.get_project_by_slug(db, project)
    if row is None:
        raise HTTPException(404, "project not found")
    project_id = int(row["id"])
    await _authorize_project(db, identity, project_id)
    return await deploys_view(db, project_id, limit)
