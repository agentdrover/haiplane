"""Web UI routes (HTML / HTMX) for OpenClaw Hub dashboard."""

from __future__ import annotations

import hashlib
import html
import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

import aiosqlite
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from hub import config
from hub import db as db_module
from hub import repository as repo
from hub import services
from hub.actionable_errors import human_only_gate_detail
from hub.auth import (
    CSRF_COOKIE_NAME,
    current_user,
    current_identity,
    require_human_or_admin,
    generate_csrf_token,
    login_limiter,
    require_permission,
    verify_csrf,
)
from hub.integrations.registry import plugins
from hub.models import (
    BatchApprove,
    ProjectCreate,
    ProjectPatch,
    ReviewFinding,
    ReviewSeverity,
    RuntimeChoice,
    TaskAnswer,
    TaskApprove,
    TaskCreate,
    TaskDecide,
    TaskForceComplete,
    TaskReject,
    TaskReviewVerdict,
    TaskStart,
    TaskStatus,
    TaskType,
    WorkType,
)

HERE = Path(__file__).parent


def _user_context(request: Request) -> dict[str, Any]:
    """Inject ``current_user`` into every Jinja template automatically."""
    return {"current_user": getattr(request.state, "user", "anonymous")}


TEMPLATES = Jinja2Templates(
    directory=str(HERE / "templates"),
    context_processors=[_user_context],
)

router = APIRouter()


def _optional_int_query(value: str | int | None, field: str) -> int | None:
    """Treat empty HTMX form values as omitted optional integer query params."""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, f"{field} must be an integer") from exc


# ---------------------------------------------------------------------------
# Login / logout
# ---------------------------------------------------------------------------


def _safe_next(raw: str | None) -> str:
    """Whitelist redirect target so /login?next=... cannot leave the host."""
    if not raw:
        return "/"
    if raw.startswith("/") and not raw.startswith("//"):
        return raw
    return "/"


@router.get("/login", response_class=HTMLResponse)
async def web_login_form(
    request: Request,
    next: str = Query(default="/"),
    error: str = Query(default=""),
):
    csrf_token = generate_csrf_token()
    response = TEMPLATES.TemplateResponse(
        request,
        "login.html",
        {"next": _safe_next(next), "error": error, "csrf_token": csrf_token},
    )
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=csrf_token,
        max_age=600,
        httponly=True,
        samesite="strict",
        secure=config.HUB_COOKIE_SECURE,
    )
    return response


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@router.post("/login")
async def web_login_submit(
    request: Request,
    username: str = Form(default=""),
    password: str = Form(default=""),
    csrf_token: str = Form(default=""),
    next: str = Form("/"),
):
    safe_next = _safe_next(next)

    if config.HUB_AUTH_DISABLED or (
        not config.HUB_TOKENS and not _has_db_principals(request)
    ):
        return RedirectResponse(safe_next, status_code=303)

    # CSRF verification
    csrf_cookie = request.cookies.get(CSRF_COOKIE_NAME, "")
    if not verify_csrf(csrf_token, csrf_cookie):
        return RedirectResponse(
            f"/login?error=Invalid%20form%20submission.%20Please%20try%20again.&next={safe_next}",
            status_code=303,
        )

    # Rate limiting
    client_ip = _client_ip(request)
    if login_limiter.is_blocked(client_ip):
        return RedirectResponse(
            f"/login?error=Too%20many%20login%20attempts.%20Please%20wait%20a%20few%20minutes.&next={safe_next}",
            status_code=303,
        )

    # Username + password login (DB-backed)
    if username and password:
        from hub.services import admin as admin_svc

        login_limiter.record(client_ip)
        db = _db(request)
        ip_hash = hashlib.sha256(client_ip.encode()).hexdigest()[:16]
        principal_id = await admin_svc.authenticate_password(
            db, username.strip(), password
        )
        if not principal_id:
            return RedirectResponse(
                f"/login?error=Invalid%20credentials&next={safe_next}",
                status_code=303,
            )
        session_token = await admin_svc.create_browser_session(
            db,
            principal_id,
            ip_hash=ip_hash,
            user_agent=request.headers.get("user-agent", "")[:200],
            max_age_seconds=config.HUB_COOKIE_MAX_AGE,
        )
        response = RedirectResponse(safe_next, status_code=303)
        response.set_cookie(
            key=config.HUB_COOKIE_NAME,
            value=session_token,
            max_age=config.HUB_COOKIE_MAX_AGE,
            httponly=True,
            samesite="lax",
            secure=config.HUB_COOKIE_SECURE,
        )
        response.delete_cookie(CSRF_COOKIE_NAME)
        return response

    return RedirectResponse(
        f"/login?error=Please%20enter%20credentials&next={safe_next}",
        status_code=303,
    )


def _has_db_principals(request: Request) -> bool:
    """Quick check if DB principals exist (non-blocking heuristic)."""
    try:
        return hasattr(request.app.state, "db")
    except Exception:
        return False


@router.post("/logout")
async def web_logout(request: Request):
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(config.HUB_COOKIE_NAME)
    return response


def _db(request: Request) -> aiosqlite.Connection:
    return request.app.state.db


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def _require_human_web(request: Request) -> None:
    """Reject agent tokens on human-only web mutations (mirrors REST gates)."""
    if current_identity(request).is_agent:
        raise HTTPException(403, detail=human_only_gate_detail())


def _dispatch_available() -> bool:
    return plugins.dispatch.is_available()


ANALYST_PREPARED_MARKER = "Analyst preparation complete"


async def _analyst_ready_info(
    db: aiosqlite.Connection,
    task_id: int,
    readiness: Any | None = None,
    task: Any | None = None,
) -> dict[str, Any]:
    """Derived UI signal: DoR passed and an analyst preparation update exists."""
    if readiness is None:
        readiness = await services.get_readiness(db, task_id, explain=False)
    prepared_by = getattr(task, "prepared_by", "") if task is not None else ""
    prepared_at = getattr(task, "prepared_at", "") if task is not None else ""
    if readiness.dor_passed and prepared_by:
        return {
            "ready": True,
            "agent": prepared_by,
            "created_at": prepared_at or "",
        }
    updates = await repo.get_task_updates(db, task_id)
    prep_update = next(
        (
            dict(update)
            for update in reversed(updates)
            if ANALYST_PREPARED_MARKER in (dict(update).get("content") or "")
        ),
        None,
    )
    return {
        "ready": bool(readiness.dor_passed and prep_update),
        "agent": prep_update.get("agent", "") if prep_update else "",
        "created_at": prep_update.get("created_at", "") if prep_update else "",
    }


async def _analyst_ready_map(
    db: aiosqlite.Connection,
    tasks: list[Any],
) -> dict[int, bool]:
    result: dict[int, bool] = {}
    for task in tasks:
        info = await _analyst_ready_info(db, task.id, task=task)
        result[task.id] = bool(info["ready"])
    return result


async def _apply_analyst_ready_filter(
    db: aiosqlite.Connection,
    tasks: list[Any],
    *,
    analyst_ready: bool = False,
) -> tuple[list[Any], dict[int, bool]]:
    ready_by_id = await _analyst_ready_map(db, tasks)
    if analyst_ready:
        tasks = [task for task in tasks if ready_by_id.get(task.id)]
    return tasks, ready_by_id


async def _htmx_task_done_fragment(request: Request, task_id: int) -> HTMLResponse:
    """Return a small 'done' indicator for HTMX-swapped items."""
    db = _db(request)
    row = await repo.get_task(db, task_id)
    if not row:
        return HTMLResponse("")
    t = services.row_to_task(row)
    safe_title = html.escape(t.title[:40])
    safe_status = html.escape(t.status.value)
    fragment = (
        f'<div class="inbox-item-done" id="inbox-task-{t.id}">'
        f'<span class="badge badge-{safe_status}">{safe_status}</span> '
        f"#{t.id} {safe_title}</div>"
    )
    return HTMLResponse(fragment)


def _htmx_dor_failed_fragment(task_id: int, detail: dict[str, Any]) -> HTMLResponse:
    """Self-contained HTMX block shown when plain Approve hits the DoR gate.

    Explains exactly what the task is missing to move further along the
    route (the unmet Definition of Ready fields and their recommendations)
    so the human knows what to fill in next. Returned with HTTP 200 so HTMX
    swaps it in place instead of erroring.
    """
    missing = detail.get("missing_required") or []
    recommendations = detail.get("recommendations") or []
    score = detail.get("score")
    score_text = f" (score {html.escape(str(score))})" if score is not None else ""
    if recommendations:
        items = "".join(
            f"<li><b>{html.escape(str(rec.get('field', '')))}:</b> "
            f"{html.escape(str(rec.get('message', '')))}</li>"
            for rec in recommendations
        )
        what_missing = f"<ul class='dor-gate-list'>{items}</ul>"
    elif missing:
        what_missing = f"<p>Не хватает: <b>{html.escape(', '.join(missing))}</b>.</p>"
    else:
        what_missing = "<p>Не заполнены обязательные поля Definition of Ready.</p>"
    fragment = (
        f'<div class="dor-gate-warning" id="dor-warn-{task_id}">'
        f'<span class="badge badge-draft">DoR не пройден</span>'
        f"<p>Задача #{task_id}{score_text} ещё не готова к одобрению. "
        f"Заполните недостающее, чтобы двигаться дальше по маршруту:</p>"
        f"{what_missing}"
        f'<a class="btn btn-secondary btn-xs" href="/tasks/{task_id}">Открыть задачу</a>'
        f"</div>"
    )
    return HTMLResponse(fragment, status_code=200)


# ---------------------------------------------------------------------------
# Partials (HTMX fragments)
# ---------------------------------------------------------------------------


@router.get("/partials/inbox", response_class=HTMLResponse)
async def web_partial_inbox(
    request: Request,
    human_owner: str | None = None,
    claimed_by: str | None = None,
    mine: str | None = None,
    project: str | None = Query(None),
):
    db = _db(request)
    inbox = await services.get_inbox_data(
        db,
        human_owner=human_owner,
        claimed_by=claimed_by,
        mine=mine,
    )
    allowed, _, _ = await _project_filter_ctx(db, project)
    if allowed is not None:
        for key in (
            "drafts",
            "questions",
            "decisions",
            "pending_reports",
            "ci_check_tasks",
            "fix_requested_tasks",
            "stale_tasks",
        ):
            inbox[key] = _filter_by_ids(inbox[key], allowed)
    inbox["dispatch_available"] = _dispatch_available()
    return TEMPLATES.TemplateResponse(request, "partials/inbox.html", inbox)


@router.get("/partials/epics", response_class=HTMLResponse)
async def web_partial_epics(request: Request):
    epics = await services.get_epics_enriched(_db(request))
    return TEMPLATES.TemplateResponse(
        request, "partials/epic_list.html", {"epics": epics}
    )


@router.get("/partials/kanban", response_class=HTMLResponse)
async def web_partial_kanban(request: Request, project: str | None = Query(None)):
    db = _db(request)
    data = await services.get_dashboard_data(db)
    allowed, _, _ = await _project_filter_ctx(db, project)
    if allowed is not None:
        for field in (
            "active_tasks",
            "draft_tasks",
            "review_tasks",
            "needs_decision_tasks",
            "needs_info_tasks",
        ):
            if hasattr(data, field):
                setattr(data, field, _filter_by_ids(getattr(data, field), allowed))
    tasks_for_badges = [
        *data.active_tasks,
        *data.draft_tasks,
        *data.review_tasks,
        *data.needs_decision_tasks,
        *data.needs_info_tasks,
    ]
    return TEMPLATES.TemplateResponse(
        request,
        "partials/kanban.html",
        {
            "data": data,
            "dispatch_available": _dispatch_available(),
            "analyst_ready_by_id": await _analyst_ready_map(
                _db(request), tasks_for_badges
            ),
        },
    )


@router.get("/tasks/list", response_class=HTMLResponse)
async def web_tasks_list_partial(
    request: Request,
    status: str | None = None,
    task_type: str | None = Query(default=None, alias="type"),
    priority: str | None = None,
    source: str | None = None,
    parent_id: str | None = None,
    human_owner: str | None = None,
    human_reviewer: str | None = None,
    claimed_by: str | None = None,
    mine: str | None = None,
    analyst_ready: bool = Query(default=False),
    limit: int = Query(default=100, le=200),
):
    """HTML fragment: task table body for HTMX swap."""
    parsed_parent_id = _optional_int_query(parent_id, "parent_id")
    tasks = await services.list_tasks(
        _db(request),
        status=status,
        task_type=task_type,
        priority=priority,
        source=source,
        parent_id=parsed_parent_id,
        human_owner=human_owner,
        human_reviewer=human_reviewer,
        claimed_by=claimed_by,
        mine=mine,
        limit=limit,
    )
    tasks, ready_by_id = await _apply_analyst_ready_filter(
        _db(request), tasks, analyst_ready=analyst_ready
    )
    return TEMPLATES.TemplateResponse(
        request,
        "partials/task_table.html",
        {
            "tasks": tasks,
            "dispatch_available": _dispatch_available(),
            "analyst_ready_by_id": ready_by_id,
        },
    )


# ---------------------------------------------------------------------------
# Full-page web routes
# ---------------------------------------------------------------------------


async def _project_filter_ctx(
    db: aiosqlite.Connection, project: str | None
) -> tuple[set[int] | None, list[dict[str, Any]], str]:
    """(allowed task ids | None, projects for the selector, current slug) (#339)."""
    rows = await repo.list_projects(db, only_active=True)
    projects = [dict(r) for r in rows]
    allowed: set[int] | None = None
    current = (project or "").strip()
    if current:
        prow = await repo.get_project_by_slug(db, current)
        allowed = (
            await repo.list_task_ids_for_project(db, prow["id"])
            if prow is not None
            else set()
        )
    return allowed, projects, current


def _filter_by_ids(items: list[Any], allowed: set[int] | None) -> list[Any]:
    if allowed is None:
        return items
    return [t for t in items if (t.id if hasattr(t, "id") else t.get("id")) in allowed]


@router.get("/", response_class=HTMLResponse)
async def web_dashboard(request: Request, project: str | None = Query(None)):
    db = _db(request)
    allowed, projects_list, current_project = await _project_filter_ctx(db, project)
    data = await services.get_dashboard_data(db)
    inbox = await services.get_inbox_data(db)
    if allowed is not None:
        for field in (
            "active_tasks",
            "draft_tasks",
            "review_tasks",
            "needs_decision_tasks",
            "needs_info_tasks",
        ):
            if hasattr(data, field):
                setattr(data, field, _filter_by_ids(getattr(data, field), allowed))
        for key in (
            "drafts",
            "questions",
            "decisions",
            "pending_reports",
            "ci_check_tasks",
            "fix_requested_tasks",
            "stale_tasks",
        ):
            inbox[key] = _filter_by_ids(inbox[key], allowed)
    epics = await services.get_epics_enriched(db)
    if allowed is not None:
        epics = [
            e
            for e in epics
            if (e.get("id") if isinstance(e, dict) else getattr(e, "id", None))
            in allowed
        ]
    inbox_total = (
        len(inbox["drafts"])
        + len(inbox["questions"])
        + len(inbox["decisions"])
        + len(inbox["pending_reports"])
        + len(inbox["ci_check_tasks"])
        + len(inbox["fix_requested_tasks"])
        + len(inbox["stale_tasks"])
    )
    ctx: dict[str, Any] = {
        "data": data,
        "epics_enriched": epics,
        "epics": epics,
        "inbox_total": inbox_total,
        "dispatch_available": _dispatch_available(),
        "projects_list": projects_list,
        "current_project": current_project,
    }
    ctx.update(inbox)
    tasks_for_badges = [
        *data.active_tasks,
        *data.draft_tasks,
        *data.review_tasks,
        *data.needs_decision_tasks,
        *data.needs_info_tasks,
    ]
    analyst_ready_by_id = await _analyst_ready_map(db, tasks_for_badges)
    ctx["analyst_ready_by_id"] = analyst_ready_by_id
    ctx["analyst_ready_count"] = sum(
        1 for ready in analyst_ready_by_id.values() if ready
    )
    return TEMPLATES.TemplateResponse(request, "dashboard.html", ctx)


@router.get("/projects", response_class=HTMLResponse)
async def web_projects(request: Request, project_error: str = Query("")):
    """Project list with create/edit/archive forms (#339, #344)."""
    rows = await repo.list_projects(_db(request), include_archived=True)
    return TEMPLATES.TemplateResponse(
        request,
        "projects.html",
        {"projects": [dict(r) for r in rows], "project_error": project_error},
    )


def _parse_policy_form(raw: str) -> tuple[dict[str, Any] | None, str | None]:
    """(policy dict | None if absent, error message | None)."""
    raw = raw.strip()
    if not raw:
        return None, None
    try:
        parsed = json.loads(raw)
    except (ValueError, RecursionError):
        # RecursionError: на Python <3.13 json.loads падает рекурсией на глубоко
        # вложенном вводе ('['*20000) — это не ValueError и без перехвата даёт 500.
        return None, "policy: не парсится как JSON"
    if not isinstance(parsed, dict):
        return None, "policy: ожидается JSON-объект"
    return parsed, None


def _projects_error_redirect(message: str) -> RedirectResponse:
    return RedirectResponse(
        f"/projects?project_error={quote(message)}", status_code=303
    )


@router.post("/projects/web-create")
async def web_create_project(request: Request):
    """Create-project form (#344): thin wrapper over the API handler —
    the same slug validation, 409 duplication and agent→pending rules."""
    form = await request.form()
    policy, err = _parse_policy_form(str(form.get("default_branch_policy") or ""))
    if err:
        return _projects_error_redirect(err)
    try:
        body = ProjectCreate(
            slug=str(form.get("slug") or "").strip(),
            name=str(form.get("name") or "").strip(),
            repo=str(form.get("repo") or "").strip(),
            workspace_path=str(form.get("workspace_path") or "").strip(),
            default_branch=str(form.get("default_branch") or "").strip() or "develop",
            default_branch_policy=policy or {},
        )
    except ValidationError as exc:
        first = exc.errors()[0]
        loc = ".".join(str(p) for p in first.get("loc", ()))
        return _projects_error_redirect(f"{loc}: {first.get('msg', 'invalid')}")

    from hub.app import api_create_project

    try:
        await api_create_project(body, request, identity=current_identity(request))
    except HTTPException as exc:
        if exc.status_code == 409:
            return _projects_error_redirect(str(exc.detail))
        raise
    return RedirectResponse("/projects", status_code=303)


async def _web_patch_project(
    request: Request, project_id: int, body: ProjectPatch
) -> None:
    """Shared web→API bridge: the same human gate as PATCH /api/projects."""
    from hub.app import api_patch_project

    await api_patch_project(
        project_id, body, request, _identity=require_human_or_admin(request)
    )


@router.post("/projects/{project_id}/web-edit")
async def web_edit_project(project_id: int, request: Request):
    """Inline-edit form (#344): exactly the ProjectPatch fields."""
    form = await request.form()
    fields: dict[str, Any] = {}
    for key in ("name", "repo", "workspace_path", "default_branch"):
        if key in form:
            value = str(form.get(key) or "").strip()
            if not value and key in ("name", "default_branch"):
                continue  # обязательные в модели — пустое значит «не менять»
            fields[key] = value
    policy, err = _parse_policy_form(str(form.get("default_branch_policy") or ""))
    if err:
        return _projects_error_redirect(err)
    if policy is not None:
        fields["default_branch_policy"] = policy
    if not fields:
        return RedirectResponse("/projects", status_code=303)
    try:
        body = ProjectPatch(**fields)
    except ValidationError as exc:
        first = exc.errors()[0]
        loc = ".".join(str(p) for p in first.get("loc", ()))
        return _projects_error_redirect(f"{loc}: {first.get('msg', 'invalid')}")
    await _web_patch_project(request, project_id, body)
    return RedirectResponse("/projects", status_code=303)


@router.post("/projects/{project_id}/web-archive")
async def web_archive_project(
    project_id: int, request: Request, archived: bool = Form(...)
):
    await _web_patch_project(request, project_id, ProjectPatch(archived=archived))
    return RedirectResponse("/projects", status_code=303)


@router.post("/projects/{project_id}/web-activate")
async def web_activate_project(project_id: int, request: Request):
    """Activate a pending agent proposal (#345) from the UI."""
    await _web_patch_project(request, project_id, ProjectPatch(status="active"))
    return RedirectResponse("/projects", status_code=303)


@router.post("/projects/{project_id}/web-provision")
async def web_provision_project(project_id: int, request: Request):
    """Provision button (#348): same human gate and service as the API.

    An error outcome is shown next to the form; the status badge on the
    page reflects provision_status either way."""
    identity = require_human_or_admin(request)
    db = _db(request)
    if await repo.get_project(db, project_id) is None:
        return _projects_error_redirect("project not found")
    result = await services.provision_project(db, project_id, actor=identity.username)
    if result["provision_status"] != "ok":
        return _projects_error_redirect(f"Provision: {result['provision_detail']}")
    return RedirectResponse("/projects", status_code=303)


@router.post("/tasks/{task_id}/web-request-machine-review")
async def web_request_machine_review(task_id: int, request: Request):
    """Reviewer explicitly demands a machine review (#382): sets the task
    override, leaves an alert for the agent and wakes it via the feed."""
    identity = require_human_or_admin(request)
    db = _db(request)
    if await repo.get_task(db, task_id) is None:
        raise HTTPException(404, "task not found")
    await repo.update_task(db, task_id, machine_review_override="require")
    await repo.add_task_update(
        db,
        task_id,
        identity.username,
        "alert",
        "Reviewer запросил machine-review: hub_get_skill('multi-agent-review') "
        "→ прогон харнесса → hub_submit_machine_review, затем ждите вердикт.",
    )
    await repo.insert_event(
        db,
        kind="machine_review_requested",
        task_id=task_id,
        actor=identity.username,
    )
    await db.commit()
    return RedirectResponse(f"/tasks/{task_id}", status_code=303)


@router.get("/metrics", response_class=HTMLResponse)
async def web_metrics(request: Request, since_days: int = Query(default=90, ge=1)):
    """Practice metrics page (#384)."""
    data = await services.practice_metrics(_db(request), since_days=since_days)
    return TEMPLATES.TemplateResponse(request, "metrics.html", {"m": data})


@router.get("/skills", response_class=HTMLResponse)
async def web_skills(request: Request, skill_error: str = Query("")):
    """Skills library (#380): latest version per name; create form (#385)."""
    from hub.models import SkillView

    rows = await repo.list_skills(_db(request))
    return TEMPLATES.TemplateResponse(
        request,
        "skills.html",
        {
            "skills": [SkillView(**dict(r)) for r in rows],
            "skill_error": skill_error,
        },
    )


@router.get("/skills/{name}", response_class=HTMLResponse)
async def web_skill_detail(name: str, request: Request, skill_error: str = Query("")):
    from hub.models import SkillView

    rows = await repo.list_skill_versions(_db(request), name)
    if not rows:
        raise HTTPException(404, "skill not found")
    versions = [SkillView(**dict(r)) for r in rows]
    active = next((v for v in versions if v.status == "active"), versions[0])
    return TEMPLATES.TemplateResponse(
        request,
        "skill_detail.html",
        {
            "name": name,
            "versions": versions,
            "active_content": active.content,
            "skill_error": skill_error,
        },
    )


def _skills_error_redirect(message: str, name: str = "") -> RedirectResponse:
    target = f"/skills/{name}" if name else "/skills"
    return RedirectResponse(f"{target}?skill_error={quote(message)}", status_code=303)


async def _web_create_skill_version(request: Request, form: Any, name_hint: str = ""):
    """Shared web→API bridge for create and new-version (#385)."""
    from hub.app import api_create_skill
    from hub.models import SkillCreate

    tags = [t.strip() for t in str(form.get("tags") or "").split(",") if t.strip()]
    try:
        body = SkillCreate(
            name=(name_hint or str(form.get("name") or "")).strip(),
            kind=str(form.get("kind") or "prompt").strip(),
            content=str(form.get("content") or ""),
            tags=tags,
        )
    except ValidationError as exc:
        first = exc.errors()[0]
        loc = ".".join(str(p) for p in first.get("loc", ()))
        return _skills_error_redirect(
            f"{loc}: {first.get('msg', 'invalid')}", name_hint
        )
    await api_create_skill(body, request, identity=current_identity(request))
    return RedirectResponse(f"/skills/{body.name}", status_code=303)


@router.post("/skills/web-create")
async def web_create_skill(request: Request):
    """Create-skill form (#385): thin wrapper over api_create_skill; the
    human path publishes an active version, agents still land as drafts."""
    return await _web_create_skill_version(request, await request.form())


@router.post("/skills/{name}/web-new-version")
async def web_new_skill_version(name: str, request: Request):
    """Edit as new version (#385): immutable history — always a new INSERT."""
    if not await repo.list_skill_versions(_db(request), name):
        raise HTTPException(404, "skill not found")
    return await _web_create_skill_version(request, await request.form(), name)


@router.post("/skills/{name}/versions/{version}/web-activate")
async def web_activate_skill(name: str, version: int, request: Request):
    """Activate a proposed skill version — same human gate as the API."""
    from hub.app import api_activate_skill

    await api_activate_skill(
        name, version, request, _identity=require_human_or_admin(request)
    )
    return RedirectResponse(f"/skills/{name}", status_code=303)


@router.get("/tasks", response_class=HTMLResponse)
async def web_tasks(
    request: Request,
    status: str | None = None,
    task_type: str | None = Query(default=None, alias="type"),
    priority: str | None = None,
    source: str | None = None,
    parent_id: str | None = None,
    human_owner: str | None = None,
    human_reviewer: str | None = None,
    analyst_ready: bool = Query(default=False),
    project: str | None = Query(default=None),
):
    db = _db(request)
    allowed, projects_list, current_project = await _project_filter_ctx(db, project)
    parsed_parent_id = _optional_int_query(parent_id, "parent_id")
    tasks = await services.list_tasks(
        db,
        status=status,
        task_type=task_type,
        priority=priority,
        source=source,
        parent_id=parsed_parent_id,
        human_owner=human_owner,
        human_reviewer=human_reviewer,
        limit=100,
    )
    if allowed is not None:
        tasks = _filter_by_ids(tasks, allowed)
    tasks, ready_by_id = await _apply_analyst_ready_filter(
        db, tasks, analyst_ready=analyst_ready
    )

    parent_breadcrumb = None
    if parsed_parent_id is not None:
        crumbs = await db_module.get_breadcrumb(db, parsed_parent_id)
        parent_breadcrumb = crumbs

    all_statuses = [s.value for s in TaskStatus]
    all_types = [t.value for t in TaskType]
    all_priorities = ["critical", "high", "medium", "low"]

    return TEMPLATES.TemplateResponse(
        request,
        "tasks.html",
        {
            "tasks": tasks,
            "filter_status": status or "",
            "filter_type": task_type or "",
            "filter_priority": priority or "",
            "filter_source": source or "",
            "filter_parent_id": parsed_parent_id,
            "filter_human_owner": human_owner or "",
            "filter_human_reviewer": human_reviewer or "",
            "filter_analyst_ready": analyst_ready,
            "projects_list": projects_list,
            "current_project": current_project,
            "parent_breadcrumb": parent_breadcrumb,
            "all_statuses": all_statuses,
            "all_types": all_types,
            "all_priorities": all_priorities,
            "dispatch_available": _dispatch_available(),
            "analyst_ready_by_id": ready_by_id,
        },
    )


@router.get("/tasks/{task_id}", response_class=HTMLResponse)
async def web_task_detail(
    task_id: int,
    request: Request,
    approve_error: str = Query(""),
    review_error: str = Query(""),
):
    db = _db(request)
    row = await repo.get_task(db, task_id)
    if not row:
        raise HTTPException(404, "task not found")
    updates = await repo.get_task_updates(db, task_id)
    task_view = services.row_to_task(row, updates=updates)
    task_view.acceptance_criteria = await services.list_acceptance_criteria(db, task_id)
    task = await services.enrich_task_view(db, task_view)
    readiness = await services.get_readiness(db, task_id, explain=False)
    analyst_ready = await _analyst_ready_info(db, task_id, readiness, task=task)
    identity = current_identity(request)

    # Machine review (#381): summary next to the verdict buttons.
    machine_review = None
    mr_row = await repo.get_latest_machine_review(db, task_id)
    if mr_row is not None:
        from hub.models import MachineReviewView

        machine_review = MachineReviewView(**dict(mr_row))
        machine_review.is_current = machine_review.submission_generation == (
            task.submission_generation or 0
        )

    # Machine-review policy gap (#382): warning in the verdict panel.
    machine_review_gap_text = None
    if task.status.value == "review" and not task.review_job_id:
        from hub.services.orchestration import machine_review_gap

        machine_review_gap_text = await machine_review_gap(db, dict(row))

    return TEMPLATES.TemplateResponse(
        request,
        "task_detail.html",
        {
            "task": task,
            "machine_review": machine_review,
            "machine_review_gap": machine_review_gap_text,
            "readiness": readiness,
            "analyst_ready": analyst_ready,
            "can_archive": identity.has_permission("tasks.archive"),
            "can_delete": identity.has_permission("tasks.delete"),
            "dispatch_available": _dispatch_available(),
            "approve_error": approve_error,
            "review_error": review_error,
        },
    )


@router.get("/tasks/{task_id}/log", response_class=HTMLResponse)
async def web_task_log(task_id: int, request: Request, job: str = Query("main")):
    db = _db(request)
    row = await repo.get_task(db, task_id)
    if not row:
        raise HTTPException(404, "task not found")
    task = dict(row)
    job_id = task.get("review_job_id") if job == "review" else task.get("job_id")
    log_content = plugins.dispatch.job_log_full(job_id) if job_id else ""
    label = "Review Log" if job == "review" else "Dispatch Log"
    return TEMPLATES.TemplateResponse(
        request,
        "task_log.html",
        {
            "task_id": task_id,
            "task_title": task.get("title", ""),
            "job_id": job_id or "—",
            "job_type": job,
            "label": label,
            "log_content": log_content,
        },
    )


# ---------------------------------------------------------------------------
# Web form actions
# ---------------------------------------------------------------------------


@router.post("/tasks/create", response_class=HTMLResponse)
async def web_create_task(
    request: Request,
    title: str = Form(...),
    description: str = Form(""),
    runtime: str = Form("auto"),
    run_immediately: bool = Form(False),
    task_type: str = Form("task"),
    parent_id: int | None = Form(None),
    priority: str = Form("medium"),
    work_type: str = Form("feature"),
    user_story: str = Form(""),
    problem_statement: str = Form(""),
    scope_in: str = Form(""),
    human_owner: str = Form(""),
    human_reviewer: str = Form(""),
    after_create: str = Form("backlog"),
):
    user = current_user(request)
    # scope_in arrives as a textarea, one item per line
    scope_in_items: list[str] = [
        line.strip() for line in scope_in.splitlines() if line.strip()
    ]

    body = TaskCreate(
        title=title,
        description=description,
        runtime=runtime,
        run_immediately=run_immediately,
        task_type=TaskType(task_type),
        parent_id=parent_id,
        priority=priority,
        work_type=WorkType(work_type) if task_type == "task" else WorkType.feature,
        user_story=user_story,
        problem_statement=problem_statement,
        scope_in=scope_in_items,
        human_owner=human_owner,
        human_reviewer=human_reviewer,
        agent=user,
    )
    created = (await services.create_task(_db(request), body)).task
    if after_create == "refine":
        return RedirectResponse(f"/tasks/{created.id}", status_code=303)
    return RedirectResponse("/tasks", status_code=303)


@router.post("/tasks/{task_id}/web-approve")
async def web_approve_task(
    task_id: int,
    request: Request,
    comment: str = Form(""),
    run: bool = Form(False),
    runtime: str = Form("auto"),
    force: bool = Form(False),
):
    """Web-approve passes ``force`` through to the DoR gate (#40).

    UI buttons that should bypass DoR (e.g. an explicit 'Force approve'
    affordance in the sidebar) set ``force=true`` as a hidden form value;
    plain 'Approve' keeps the gate active.
    """
    _require_human_web(request)
    body = TaskApprove(
        comment=comment,
        run=run,
        runtime=RuntimeChoice(runtime) if runtime else None,
        force=force,
    )
    try:
        await services.approve_task(_db(request), task_id, body)
    except HTTPException as exc:
        detail = exc.detail
        is_dor = (
            exc.status_code == 422
            and isinstance(detail, dict)
            and detail.get("error") == "dor_failed"
        )
        if not is_dor:
            raise
        if _is_htmx(request):
            return _htmx_dor_failed_fragment(task_id, detail)
        return RedirectResponse(
            f"/tasks/{task_id}?approve_error=dor_failed", status_code=303
        )
    if _is_htmx(request):
        return await _htmx_task_done_fragment(request, task_id)
    return RedirectResponse(f"/tasks/{task_id}", status_code=303)


@router.post("/tasks/{task_id}/web-reject")
async def web_reject_task(
    task_id: int,
    request: Request,
    comment: str = Form(""),
):
    _require_human_web(request)
    body = TaskReject(comment=comment)
    await services.reject_task(_db(request), task_id, body)
    if _is_htmx(request):
        return await _htmx_task_done_fragment(request, task_id)
    return RedirectResponse(f"/tasks/{task_id}", status_code=303)


@router.post("/tasks/{task_id}/web-start")
async def web_start_task(
    task_id: int,
    request: Request,
    runtime: str = Form("auto"),
):
    _require_human_web(request)
    body = TaskStart(
        plan="Developer-agent dispatch requested from Hub UI.",
        runtime=RuntimeChoice(runtime) if runtime else None,
    )
    await services.start_task(_db(request), task_id, body)
    if _is_htmx(request):
        return await _htmx_task_done_fragment(request, task_id)
    return RedirectResponse(f"/tasks/{task_id}", status_code=303)


@router.post("/tasks/{task_id}/web-answer")
async def web_answer_task(
    task_id: int,
    request: Request,
    answer: str = Form(...),
    resume: bool = Form(True),
):
    _require_human_web(request)
    body = TaskAnswer(answer=answer, resume=resume)
    await services.answer_question(_db(request), task_id, body)
    if _is_htmx(request):
        return await _htmx_task_done_fragment(request, task_id)
    return RedirectResponse(f"/tasks/{task_id}", status_code=303)


@router.post("/tasks/{task_id}/web-decide")
async def web_decide_task(
    task_id: int,
    request: Request,
    action: str = Form("accept"),
    instructions: str = Form(""),
    decision_summary: str = Form(""),
    record_decision: bool = Form(False),
):
    _require_human_web(request)
    body = TaskDecide(
        action=action,
        instructions=instructions,
        decision_summary=decision_summary,
        record_decision=record_decision,
    )
    await services.decide_task(_db(request), task_id, body)
    if _is_htmx(request):
        return await _htmx_task_done_fragment(request, task_id)
    return RedirectResponse(f"/tasks/{task_id}", status_code=303)


@router.post("/tasks/web-batch-approve-ready")
async def web_batch_approve_ready(request: Request):
    """Inbox bulk action (#252): approve every DoR-ready draft without
    high risks in one click. Same guards as the API — force never."""
    db = _db(request)
    identity = current_identity(request)
    if identity.is_agent:
        raise HTTPException(403, detail=human_only_gate_detail())
    draft_rows = await repo.list_tasks_by_status(db, "draft", limit=100)
    task_ids = [dict(r)["id"] for r in draft_rows]
    if task_ids:
        await services.batch_approve_tasks(
            db,
            BatchApprove(task_ids=task_ids, comment="Batch-approved from inbox"),
        )
    if _is_htmx(request):
        inbox = await services.get_inbox_data(db)
        inbox["dispatch_available"] = _dispatch_available()
        return TEMPLATES.TemplateResponse(request, "partials/inbox.html", inbox)
    return RedirectResponse("/", status_code=303)


def _parse_findings_form(text: str) -> list[ReviewFinding]:
    """Parse the review-panel findings textarea into structured findings.

    One finding per line, optionally prefixed with a severity:
    ``high: message`` / ``medium: message`` / ``low: message``. Lines
    without a recognized severity prefix default to medium. Ids are
    assigned by position — stable within this submission (#308).
    """
    findings: list[ReviewFinding] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        severity, _, rest = line.partition(":")
        sev_token = severity.strip().lower()
        if sev_token in ReviewSeverity.__members__ and rest.strip():
            findings.append(
                ReviewFinding(
                    id=len(findings) + 1,
                    severity=ReviewSeverity(sev_token),
                    message=rest.strip(),
                )
            )
        else:
            findings.append(
                ReviewFinding(
                    id=len(findings) + 1,
                    severity=ReviewSeverity.medium,
                    message=line,
                )
            )
    return findings


@router.post("/tasks/{task_id}/web-review-verdict")
async def web_review_verdict(
    task_id: int,
    request: Request,
    verdict: str = Form(...),
    comments: str = Form(""),
    findings_text: str = Form(""),
):
    """Submit a review verdict from the task card panel (#321).

    Same semantics as POST /api/tasks/{id}/review-verdict: the shared
    independence check plus the canonical record_review_verdict service —
    no web-only verdict logic. The verdict is recorded under the logged-in
    identity, not a free-text name.
    """
    db = _db(request)
    identity = current_identity(request)
    row = await repo.get_task(db, task_id)
    if not row:
        raise HTTPException(404, "task not found")
    self_approved = services.ensure_reviewer_independence(
        dict(row),
        is_agent=identity.is_agent,
        principal_id=identity.principal_id,
        username=identity.username,
    )
    try:
        body = TaskReviewVerdict(
            verdict=verdict,  # type: ignore[arg-type]
            agent=identity.username,
            comments=comments,
            findings=_parse_findings_form(findings_text),
        )
    except ValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {}
        msg = f"Invalid review form: {first.get('msg', 'validation error')}"
        if _is_htmx(request):
            return HTMLResponse(
                f'<div class="task-action-note">{msg}</div>', status_code=422
            )
        return RedirectResponse(
            f"/tasks/{task_id}?review_error={quote(msg)}", status_code=303
        )
    await services.record_review_verdict(db, task_id, body, self_approved=self_approved)
    if _is_htmx(request):
        return await _htmx_task_done_fragment(request, task_id)
    return RedirectResponse(f"/tasks/{task_id}", status_code=303)


@router.post("/tasks/{task_id}/web-force-complete")
async def web_force_complete_task(
    task_id: int,
    request: Request,
    comment: str = Form(""),
):
    """Force-complete a non-terminal task/subtask from the Web UI (human-only).

    Same semantics as REST/MCP force-complete: any non-terminal task/subtask when
    no active dispatch job backs job_id or review_job_id; 409 if active.
    Missing/terminal jobs are audited. Comment required for most active
    lifecycle states (pending_report/claimed may use the default).

    The audit-trail comment can be supplied either via the ``HX-Prompt``
    header (populated by htmx ``hx-prompt``) or via a ``comment`` form field
    for non-htmx clients. The header takes precedence.
    """
    _require_human_web(request)
    reason = request.headers.get("HX-Prompt", "") or comment
    body = TaskForceComplete(comment=reason) if reason else None
    await services.force_complete_task(_db(request), task_id, body)
    if _is_htmx(request):
        return await _htmx_task_done_fragment(request, task_id)
    return RedirectResponse(f"/tasks/{task_id}", status_code=303)


@router.post("/tasks/{task_id}/web-delete")
async def web_delete_task(
    task_id: int,
    request: Request,
    _identity=Depends(require_permission("tasks.delete")),
):
    """Permanently remove a task subtree from the Web UI."""
    db = _db(request)
    row = await repo.get_task(db, task_id)
    if not row:
        raise HTTPException(404, "task not found")
    parent_id = dict(row).get("parent_id")
    await services.delete_task_tree(db, task_id)
    dest = f"/tasks/{parent_id}" if parent_id else "/tasks"
    return RedirectResponse(dest, status_code=303)


@router.post("/tasks/{task_id}/web-archive")
async def web_archive_task(
    task_id: int,
    request: Request,
    cascade: str = Form("true"),
    _identity=Depends(require_permission("tasks.archive")),
):
    cascade_flag = cascade.lower() in ("1", "true", "yes", "on")
    await services.archive_task(_db(request), task_id, cascade=cascade_flag)
    return RedirectResponse(f"/tasks/{task_id}", status_code=303)


@router.post("/tasks/{task_id}/web-unarchive")
async def web_unarchive_task(
    task_id: int,
    request: Request,
    cascade: str = Form("true"),
    _identity=Depends(require_permission("tasks.archive")),
):
    cascade_flag = cascade.lower() in ("1", "true", "yes", "on")
    await services.unarchive_task(_db(request), task_id, cascade=cascade_flag)
    return RedirectResponse(f"/tasks/{task_id}", status_code=303)


# ---------------------------------------------------------------------------
# Deprecated web proposal routes
# ---------------------------------------------------------------------------


@router.post("/proposals/{proposal_id}/approve")
async def web_approve_proposal_compat(
    proposal_id: int,
    request: Request,
    comment: str = Form(""),
):
    _require_human_web(request)
    body = TaskApprove(comment=comment, run=True)
    await services.approve_task(_db(request), proposal_id, body)
    return RedirectResponse("/", status_code=303)


@router.post("/proposals/{proposal_id}/reject")
async def web_reject_proposal_compat(
    proposal_id: int,
    request: Request,
    comment: str = Form(""),
):
    _require_human_web(request)
    body = TaskReject(comment=comment)
    await services.reject_task(_db(request), proposal_id, body)
    return RedirectResponse("/", status_code=303)


@router.get("/proposals", response_class=HTMLResponse)
async def web_proposals_compat(request: Request):
    """Deprecated: redirects to tasks filtered by agent source."""
    return RedirectResponse("/tasks?source=agent", status_code=302)


# ---------------------------------------------------------------------------
# Admin web routes (Stage 4)
# ---------------------------------------------------------------------------


def _require_admin_web(request: Request) -> None:
    identity = getattr(request.state, "identity", None)
    if not identity or not identity.is_admin:
        raise HTTPException(403, "admin access required")


async def _admin_nav_counts(db) -> dict[str, int]:
    from hub.services import admin as admin_svc

    summary = await admin_svc.admin_summary(db)
    return {
        "nav_users": summary["active_users"],
        "nav_agents": summary["active_agents"],
        "nav_keys": summary["active_api_keys"],
    }


def _nav_from_summary(summary: dict) -> dict[str, int]:
    return {
        "nav_users": summary["active_users"],
        "nav_agents": summary["active_agents"],
        "nav_keys": summary["active_api_keys"],
    }


@router.get("/admin", response_class=HTMLResponse)
async def web_admin_summary(request: Request):
    _require_admin_web(request)
    from hub.services import admin as admin_svc

    db = _db(request)
    summary = await admin_svc.admin_summary(db)
    nav = _nav_from_summary(summary)
    return TEMPLATES.TemplateResponse(
        request, "admin/summary.html", {"summary": summary, "active": "admin", **nav}
    )


@router.get("/admin/users", response_class=HTMLResponse)
async def web_admin_users(request: Request):
    _require_admin_web(request)
    from hub.services import admin as admin_svc

    db = _db(request)
    principals = await admin_svc.list_principals(db, kind="human")
    roles = await admin_svc.list_roles(db)
    nav = await _admin_nav_counts(db)
    return TEMPLATES.TemplateResponse(
        request,
        "admin/users.html",
        {"principals": principals, "roles": roles, "active": "admin", **nav},
    )


@router.get("/admin/agents", response_class=HTMLResponse)
async def web_admin_agents(request: Request):
    _require_admin_web(request)
    from hub.services import admin as admin_svc

    db = _db(request)
    principals = await admin_svc.list_principals(db, kind="agent")
    nav = await _admin_nav_counts(db)
    return TEMPLATES.TemplateResponse(
        request,
        "admin/agents.html",
        {"principals": principals, "active": "admin", **nav},
    )


@router.get("/admin/roles", response_class=HTMLResponse)
async def web_admin_roles(request: Request):
    _require_admin_web(request)
    from hub.services import admin as admin_svc

    db = _db(request)
    roles = await admin_svc.list_roles(db)
    nav = await _admin_nav_counts(db)
    return TEMPLATES.TemplateResponse(
        request, "admin/roles.html", {"roles": roles, "active": "admin", **nav}
    )


@router.get("/admin/keys", response_class=HTMLResponse)
async def web_admin_keys(request: Request):
    _require_admin_web(request)
    from hub.services import admin as admin_svc

    db = _db(request)
    filter_status = request.query_params.get("status", "")
    filter_owner = request.query_params.get("owner", "")

    keys = await admin_svc.list_api_keys(db)
    principals = await admin_svc.list_principals(db)
    pid_to_name = {p["id"]: p["username"] for p in principals}

    if filter_status == "active":
        keys = [k for k in keys if not k.get("revoked_at")]
    elif filter_status == "revoked":
        keys = [k for k in keys if k.get("revoked_at")]

    if filter_owner:
        try:
            owner_id = int(filter_owner)
            keys = [k for k in keys if k["principal_id"] == owner_id]
        except ValueError:
            pass

    nav = await _admin_nav_counts(db)
    return TEMPLATES.TemplateResponse(
        request,
        "admin/keys.html",
        {
            "keys": keys,
            "pid_to_name": pid_to_name,
            "principals": principals,
            "filter_status": filter_status,
            "filter_owner": filter_owner,
            "active": "admin",
            **nav,
        },
    )


@router.get("/admin/audit", response_class=HTMLResponse)
async def web_admin_audit(request: Request):
    _require_admin_web(request)
    from hub.services import admin as admin_svc

    db = _db(request)
    page = int(request.query_params.get("page", "1"))
    per_page = 50
    offset = (page - 1) * per_page
    filter_action = request.query_params.get("action", "")

    entries = await admin_svc.list_audit(db, limit=per_page + 1, offset=offset)
    has_next = len(entries) > per_page
    entries = entries[:per_page]

    if filter_action:
        entries = [e for e in entries if e.get("action") == filter_action]

    all_actions = sorted(
        {e.get("action", "") for e in await admin_svc.list_audit(db, limit=500)}
    )

    nav = await _admin_nav_counts(db)
    return TEMPLATES.TemplateResponse(
        request,
        "admin/audit.html",
        {
            "entries": entries,
            "page": page,
            "has_next": has_next,
            "filter_action": filter_action,
            "all_actions": all_actions,
            "active": "admin",
            **nav,
        },
    )


# ---------------------------------------------------------------------------
# Admin write actions (HTMX)
# ---------------------------------------------------------------------------


def _admin_actor_id(request: Request) -> int | None:
    identity = getattr(request.state, "identity", None)
    return getattr(identity, "principal_id", None) if identity else None


def _flash_headers(message: str, level: str = "success") -> dict[str, str]:
    """Return HX-Trigger header that fires a showFlash event."""
    import json

    payload = json.dumps({"showFlash": {"message": message, "level": level}})
    return {"HX-Trigger": payload}


async def _render_users_page(
    request: Request,
    flash_msg: str = "",
    flash_level: str = "success",
) -> HTMLResponse:
    """Re-render the users page; HTMX callers use hx-select to pick the table."""
    from hub.services import admin as admin_svc

    db = _db(request)
    principals = await admin_svc.list_principals(db, kind="human")
    roles = await admin_svc.list_roles(db)
    nav = await _admin_nav_counts(db)
    resp = TEMPLATES.TemplateResponse(
        request,
        "admin/users.html",
        {"principals": principals, "roles": roles, "active": "admin", **nav},
    )
    if flash_msg:
        resp.headers.update(_flash_headers(flash_msg, flash_level))
    return resp


async def _render_agents_page(
    request: Request,
    flash_msg: str = "",
    flash_level: str = "success",
) -> HTMLResponse:
    from hub.services import admin as admin_svc

    db = _db(request)
    principals = await admin_svc.list_principals(db, kind="agent")
    nav = await _admin_nav_counts(db)
    resp = TEMPLATES.TemplateResponse(
        request,
        "admin/agents.html",
        {"principals": principals, "active": "admin", **nav},
    )
    if flash_msg:
        resp.headers.update(_flash_headers(flash_msg, flash_level))
    return resp


async def _render_keys_page(
    request: Request,
    flash_msg: str = "",
    flash_level: str = "success",
) -> HTMLResponse:
    from hub.services import admin as admin_svc

    db = _db(request)
    keys = await admin_svc.list_api_keys(db)
    all_principals = await admin_svc.list_principals(db)
    pid_to_name = {p["id"]: p["username"] for p in all_principals}
    nav = await _admin_nav_counts(db)
    resp = TEMPLATES.TemplateResponse(
        request,
        "admin/keys.html",
        {
            "keys": keys,
            "pid_to_name": pid_to_name,
            "principals": all_principals,
            "filter_status": "",
            "filter_owner": "",
            "active": "admin",
            **nav,
        },
    )
    if flash_msg:
        resp.headers.update(_flash_headers(flash_msg, flash_level))
    return resp


# -- Users --


@router.post("/admin/users/create", response_class=HTMLResponse)
async def web_admin_create_user(
    request: Request,
    username: str = Form(...),
    display_name: str = Form(""),
    email: str = Form(""),
    password: str = Form(...),
    role: str = Form("operator"),
):
    _require_admin_web(request)
    from hub.services import admin as admin_svc

    db = _db(request)
    actor_id = _admin_actor_id(request)
    try:
        principal = await admin_svc.create_principal(
            db,
            kind="human",
            username=username.strip(),
            display_name=display_name.strip(),
            email=email.strip(),
            password=password,
            role_slug=role,
            created_by=actor_id,
        )
        await admin_svc.write_audit(
            db,
            actor_id=actor_id,
            action="create_user",
            target_type="principal",
            target_id=str(principal["id"]),
            summary=f"Created user {username!r} with role {role!r}",
        )
        return await _render_users_page(
            request, flash_msg=f"User '{username}' created successfully"
        )
    except Exception as exc:
        return await _render_users_page(
            request, flash_msg=str(exc), flash_level="error"
        )


@router.post("/admin/users/{principal_id}/disable", response_class=HTMLResponse)
async def web_admin_disable_user(principal_id: int, request: Request):
    _require_admin_web(request)
    from hub.services import admin as admin_svc

    db = _db(request)
    actor_id = _admin_actor_id(request)
    try:
        p = await admin_svc.disable_principal(db, principal_id)
        if not p:
            return await _render_users_page(
                request, flash_msg="User not found", flash_level="error"
            )
        await admin_svc.write_audit(
            db,
            actor_id=actor_id,
            action="disable_user",
            target_type="principal",
            target_id=str(principal_id),
            summary=f"Disabled user {p['username']!r}",
        )
        return await _render_users_page(
            request, flash_msg=f"User '{p['username']}' disabled"
        )
    except admin_svc.LastAdminError:
        return await _render_users_page(
            request,
            flash_msg="Cannot disable the last active admin",
            flash_level="error",
        )


@router.post("/admin/users/{principal_id}/enable", response_class=HTMLResponse)
async def web_admin_enable_user(principal_id: int, request: Request):
    _require_admin_web(request)
    from hub.services import admin as admin_svc

    db = _db(request)
    actor_id = _admin_actor_id(request)
    p = await admin_svc.enable_principal(db, principal_id)
    if not p:
        return await _render_users_page(
            request, flash_msg="User not found", flash_level="error"
        )
    await admin_svc.write_audit(
        db,
        actor_id=actor_id,
        action="enable_user",
        target_type="principal",
        target_id=str(principal_id),
        summary=f"Enabled user {p['username']!r}",
    )
    return await _render_users_page(
        request, flash_msg=f"User '{p['username']}' enabled"
    )


@router.post("/admin/users/{principal_id}/reset-password", response_class=HTMLResponse)
async def web_admin_reset_password(principal_id: int, request: Request):
    _require_admin_web(request)
    from hub.services import admin as admin_svc

    db = _db(request)
    actor_id = _admin_actor_id(request)
    import re

    new_password = request.headers.get("HX-Prompt", "").strip()
    if not new_password or len(new_password) < 8:
        return await _render_users_page(
            request,
            flash_msg="Password must be at least 8 characters",
            flash_level="error",
        )
    if (
        not re.search(r"[a-zA-Z]", new_password)
        or not re.search(r"\d", new_password)
        or not re.search(r"[^a-zA-Z0-9]", new_password)
    ):
        return await _render_users_page(
            request,
            flash_msg="Password must contain letter, digit, and special character",
            flash_level="error",
        )
    p = await admin_svc.get_principal(db, principal_id)
    if not p:
        return await _render_users_page(
            request, flash_msg="User not found", flash_level="error"
        )
    await admin_svc.set_password(db, principal_id, new_password)
    await admin_svc.write_audit(
        db,
        actor_id=actor_id,
        action="reset_password",
        target_type="principal",
        target_id=str(principal_id),
        summary=f"Reset password for user {p['username']!r}",
    )
    return await _render_users_page(
        request, flash_msg=f"Password reset for '{p['username']}'"
    )


@router.post("/admin/users/{principal_id}/edit-roles", response_class=HTMLResponse)
async def web_admin_edit_roles(principal_id: int, request: Request):
    _require_admin_web(request)
    from hub.services import admin as admin_svc

    db = _db(request)
    actor_id = _admin_actor_id(request)
    form = await request.form()
    selected_roles = form.getlist("roles")
    if not selected_roles:
        return await _render_users_page(
            request, flash_msg="At least one role must be selected", flash_level="error"
        )
    try:
        new_slugs = await admin_svc.set_principal_roles(
            db, principal_id, list(selected_roles), granted_by=actor_id
        )
        p = await admin_svc.get_principal(db, principal_id)
        uname = p["username"] if p else f"#{principal_id}"
        await admin_svc.write_audit(
            db,
            actor_id=actor_id,
            action="set_roles",
            target_type="principal",
            target_id=str(principal_id),
            summary=f"Set roles for {uname!r}: {', '.join(new_slugs)}",
        )
        return await _render_users_page(
            request, flash_msg=f"Roles updated for '{uname}'"
        )
    except admin_svc.LastAdminError:
        return await _render_users_page(
            request,
            flash_msg="Cannot remove admin role from the last active admin",
            flash_level="error",
        )
    except ValueError as exc:
        return await _render_users_page(
            request, flash_msg=str(exc), flash_level="error"
        )


# -- Agents --


@router.post("/admin/agents/create", response_class=HTMLResponse)
async def web_admin_create_agent(
    request: Request,
    username: str = Form(...),
    display_name: str = Form(""),
    notes: str = Form(""),
):
    _require_admin_web(request)
    from hub.services import admin as admin_svc

    db = _db(request)
    actor_id = _admin_actor_id(request)
    try:
        principal = await admin_svc.create_principal(
            db,
            kind="agent",
            username=username.strip(),
            display_name=display_name.strip(),
            notes=notes.strip(),
            role_slug="agent",
            created_by=actor_id,
        )
        await admin_svc.write_audit(
            db,
            actor_id=actor_id,
            action="create_agent",
            target_type="principal",
            target_id=str(principal["id"]),
            summary=f"Created agent {username!r}",
        )
        return await _render_agents_page(
            request, flash_msg=f"Agent '{username}' created successfully"
        )
    except Exception as exc:
        return await _render_agents_page(
            request, flash_msg=str(exc), flash_level="error"
        )


@router.post("/admin/agents/{principal_id}/disable", response_class=HTMLResponse)
async def web_admin_disable_agent(principal_id: int, request: Request):
    _require_admin_web(request)
    from hub.services import admin as admin_svc

    db = _db(request)
    actor_id = _admin_actor_id(request)
    p = await admin_svc.disable_principal(db, principal_id)
    if not p:
        return await _render_agents_page(
            request, flash_msg="Agent not found", flash_level="error"
        )
    await admin_svc.write_audit(
        db,
        actor_id=actor_id,
        action="disable_agent",
        target_type="principal",
        target_id=str(principal_id),
        summary=f"Disabled agent {p['username']!r}",
    )
    return await _render_agents_page(
        request, flash_msg=f"Agent '{p['username']}' disabled"
    )


@router.post("/admin/agents/{principal_id}/enable", response_class=HTMLResponse)
async def web_admin_enable_agent(principal_id: int, request: Request):
    _require_admin_web(request)
    from hub.services import admin as admin_svc

    db = _db(request)
    actor_id = _admin_actor_id(request)
    p = await admin_svc.enable_principal(db, principal_id)
    if not p:
        return await _render_agents_page(
            request, flash_msg="Agent not found", flash_level="error"
        )
    await admin_svc.write_audit(
        db,
        actor_id=actor_id,
        action="enable_agent",
        target_type="principal",
        target_id=str(principal_id),
        summary=f"Enabled agent {p['username']!r}",
    )
    return await _render_agents_page(
        request, flash_msg=f"Agent '{p['username']}' enabled"
    )


@router.post("/admin/agents/{principal_id}/create-key", response_class=HTMLResponse)
async def web_admin_create_agent_key(principal_id: int, request: Request):
    _require_admin_web(request)
    from hub.services import admin as admin_svc

    db = _db(request)
    actor_id = _admin_actor_id(request)
    key_name = request.headers.get("HX-Prompt", "").strip() or "default"
    p = await admin_svc.get_principal(db, principal_id)
    if not p:
        headers = _flash_headers("Agent not found", "error")
        return HTMLResponse("<div></div>", headers=headers)
    key_info = await admin_svc.create_api_key(
        db,
        principal_id,
        name=key_name,
        created_by=actor_id,
    )
    await admin_svc.write_audit(
        db,
        actor_id=actor_id,
        action="create_api_key",
        target_type="api_key",
        target_id=str(key_info["id"]),
        summary=f"Created API key {key_name!r} for agent {p['username']!r}",
    )
    import html as html_mod

    safe_key = html_mod.escape(key_info["plaintext_key"])
    safe_name = html_mod.escape(key_name)
    safe_user = html_mod.escape(p["username"])
    fragment = (
        f'<div class="admin-key-reveal">'
        f'<span class="warning">This key will not be shown again!</span><br>'
        f"Key <b>{safe_name}</b> for agent <b>{safe_user}</b>:"
        f"<code>{safe_key}</code>"
        f"</div>"
    )
    headers = _flash_headers(f"API key '{key_name}' created for '{p['username']}'")
    return HTMLResponse(fragment, headers=headers)


# -- Keys --


@router.post("/admin/keys/create", response_class=HTMLResponse)
async def web_admin_create_key(
    request: Request,
    principal_id: int = Form(...),
    name: str = Form(...),
    expires_days: int = Form(0),
):
    _require_admin_web(request)
    from hub.services import admin as admin_svc

    db = _db(request)
    actor_id = _admin_actor_id(request)
    p = await admin_svc.get_principal(db, principal_id)
    if not p:
        return await _render_keys_page(
            request, flash_msg="Principal not found", flash_level="error"
        )
    key_info = await admin_svc.create_api_key(
        db,
        principal_id,
        name=name.strip(),
        expires_days=expires_days if expires_days > 0 else None,
        created_by=actor_id,
    )
    await admin_svc.write_audit(
        db,
        actor_id=actor_id,
        action="create_api_key",
        target_type="api_key",
        target_id=str(key_info["id"]),
        summary=f"Created API key {name!r} for {p['username']!r}",
    )
    import html as html_mod

    safe_key = html_mod.escape(key_info["plaintext_key"])
    safe_name = html_mod.escape(name)
    safe_user = html_mod.escape(p["username"])
    key_banner = (
        f'<div class="admin-key-reveal">'
        f'<span class="warning">This key will not be shown again!</span><br>'
        f"Key <b>{safe_name}</b> for <b>{safe_user}</b>:"
        f"<code>{safe_key}</code>"
        f"</div>"
    )
    headers = _flash_headers(f"API key '{name}' created for '{p['username']}'")
    return HTMLResponse(key_banner, headers=headers)


@router.post("/admin/keys/{key_id}/revoke", response_class=HTMLResponse)
async def web_admin_revoke_key(key_id: int, request: Request):
    _require_admin_web(request)
    from hub.services import admin as admin_svc

    db = _db(request)
    actor_id = _admin_actor_id(request)
    revoked = await admin_svc.revoke_api_key(db, key_id)
    if not revoked:
        return await _render_keys_page(
            request,
            flash_msg="Key not found or already revoked",
            flash_level="error",
        )
    await admin_svc.write_audit(
        db,
        actor_id=actor_id,
        action="revoke_api_key",
        target_type="api_key",
        target_id=str(key_id),
        summary=f"Revoked API key #{key_id}",
    )
    return await _render_keys_page(request, flash_msg=f"API key #{key_id} revoked")
