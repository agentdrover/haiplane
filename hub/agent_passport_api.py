"""REST entrance and project-read check shared by passport surfaces."""

import aiosqlite
from fastapi import APIRouter, HTTPException, Request, Response

from hub import repository as repo
from hub.agent_passport_contract import AgentPassport
from hub.auth import current_identity
from hub.db import fetchall
from hub.services import agent_passport, project_access

router = APIRouter()


async def check_project_reader(
    db: aiosqlite.Connection, request: Request, project: aiosqlite.Row
) -> None:
    identity = current_identity(request)
    if (
        not identity.has_permission("tasks.read")
        or identity.scopes_damaged
        or (identity.scopes is not None and project["slug"] not in identity.scopes)
    ):
        raise HTTPException(404, "project not found")
    private = await fetchall(
        db,
        "SELECT 1 FROM private_project_security WHERE project_id=?",
        (project["id"],),
    )
    if private:
        decision = await project_access.decide(
            db, identity, permission="tasks.read", project_id=project["id"]
        )
        if not decision.allowed:
            raise HTTPException(404, "project not found")


async def get_agent_passport(request: Request, slug: str) -> AgentPassport:
    db = getattr(request.state, "db", None) or request.app.state.db
    project = await repo.get_project_by_slug(db, slug)
    if project is None:
        raise HTTPException(404, "project not found")
    await check_project_reader(db, request, project)
    return await agent_passport.build_agent_passport(db, project)


@router.get("/api/projects/{slug}/agent-passport")
async def api_agent_passport(
    slug: str, request: Request, response: Response
) -> AgentPassport:
    response.headers["Cache-Control"] = "no-store"
    return await get_agent_passport(request, slug)
