"""Owner sees the signed scope before making a decision."""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from hub.auth import CSRF_FIELD_NAME
from hub.epic_plan_api import check_reader, require_signed_browser_request, _db
from hub.epic_plan_view import PlanConfirm, PlanTerminate
from hub.services import epic_plan


def include_plan_page(router: APIRouter, templates: Jinja2Templates) -> None:
    @router.get("/tasks/{epic_id}/epic-plan", response_class=HTMLResponse)
    async def view_epic_plan(epic_id: int, request: Request):
        identity = check_reader(request, epic_id)
        plan = await epic_plan.read_plan(_db(request), epic_id, identity)
        return templates.TemplateResponse(
            request,
            "epic_plan.html",
            {"plan": plan},
            headers={"Cache-Control": "no-store"},
        )

    @router.post("/tasks/{epic_id}/epic-plan")
    async def decide_epic_plan(epic_id: int, request: Request):
        identity = check_reader(request, epic_id)
        form = await request.form()
        token = form.get(CSRF_FIELD_NAME)
        require_signed_browser_request(
            request, token if isinstance(token, str) else None
        )
        try:
            confirm = PlanConfirm.model_validate({"version": form.get("version")})
            action = form.get("action")
            terminal = (
                None
                if action == "approve"
                else PlanTerminate.model_validate(
                    {
                        "version": confirm.version,
                        "state": action,
                        "reason": form.get("reason"),
                    }
                )
            )
        except ValidationError as exc:
            raise HTTPException(422, "invalid plan decision form") from exc
        if terminal is None:
            await epic_plan.approve(_db(request), epic_id, confirm.version, identity)
        else:
            await epic_plan.terminate(_db(request), epic_id, terminal, identity)
        return RedirectResponse(f"/tasks/{epic_id}/epic-plan", status_code=303)
