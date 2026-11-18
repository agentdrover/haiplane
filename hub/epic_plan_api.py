"""Plan read and owner-only decisions. No launch or verdict endpoint."""

from fastapi import APIRouter, HTTPException, Request, Response

from hub.auth import current_identity
from hub.epic_plan_view import PlanConfirm, PlanDecision, PlanTerminate, PlanView
from hub.services import epic_plan

router = APIRouter()


def _db(request: Request):
    return getattr(request.state, "db", None) or request.app.state.db


def check_reader(request: Request, epic_id: int):
    identity = current_identity(request)
    if (
        epic_id != epic_plan.EPIC_ID
        or not identity.has_permission("tasks.read")
        or identity.scopes_damaged
        or (identity.scopes is not None and epic_plan.PROJECT not in identity.scopes)
    ):
        raise HTTPException(404, detail={"reason": "pilot_not_found"})
    return identity


@router.get("/api/tasks/{epic_id}/epic-plan")
async def api_epic_plan(epic_id: int, request: Request, response: Response) -> PlanView:
    response.headers["Cache-Control"] = "no-store"
    return await epic_plan.read_plan(
        _db(request), epic_id, check_reader(request, epic_id)
    )


@router.post("/api/tasks/{epic_id}/epic-plan/approve")
async def api_approve_plan(
    epic_id: int, request: Request, body: PlanConfirm
) -> PlanDecision:
    return await epic_plan.approve(
        _db(request), epic_id, body.version, check_reader(request, epic_id)
    )


@router.post("/api/tasks/{epic_id}/epic-plan/terminate")
async def api_terminate_plan(
    epic_id: int, request: Request, body: PlanTerminate
) -> PlanDecision:
    return await epic_plan.terminate(
        _db(request), epic_id, body, check_reader(request, epic_id)
    )
