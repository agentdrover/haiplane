"""Web UI routes (HTML / HTMX) for OpenClaw Hub dashboard."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import aiosqlite
from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from hub import config
from hub import db as db_module
from hub import repository as repo
from hub import services
from hub.auth import (
    CSRF_COOKIE_NAME,
    current_user,
    generate_csrf_token,
    login_limiter,
    verify_csrf,
)
from hub.integrations.registry import plugins
from hub.models import (
    RuntimeChoice,
    TaskAnswer,
    TaskApprove,
    TaskCreate,
    TaskDecide,
    TaskForceComplete,
    TaskReject,
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
    token: str = Form(default=""),
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

    # Legacy token login (env-based, deprecated)
    if token:
        login_limiter.record(client_ip)
        identity = config.HUB_TOKENS.get(token.strip())
        if not identity:
            return RedirectResponse(
                f"/login?error=Invalid%20token&next={safe_next}",
                status_code=303,
            )
        response = RedirectResponse(safe_next, status_code=303)
        response.set_cookie(
            key=config.HUB_COOKIE_NAME,
            value=token.strip(),
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


async def _htmx_task_done_fragment(request: Request, task_id: int) -> HTMLResponse:
    """Return a small 'done' indicator for HTMX-swapped items."""
    import html as html_mod

    db = _db(request)
    row = await repo.get_task(db, task_id)
    if not row:
        return HTMLResponse("")
    t = services.row_to_task(row)
    safe_title = html_mod.escape(t.title[:40])
    safe_status = html_mod.escape(t.status.value)
    fragment = (
        f'<div class="inbox-item-done" id="inbox-task-{t.id}">'
        f'<span class="badge badge-{safe_status}">{safe_status}</span> '
        f"#{t.id} {safe_title}</div>"
    )
    return HTMLResponse(fragment)


# ---------------------------------------------------------------------------
# Partials (HTMX fragments)
# ---------------------------------------------------------------------------


@router.get("/partials/inbox", response_class=HTMLResponse)
async def web_partial_inbox(request: Request):
    inbox = await services.get_inbox_data(_db(request))
    return TEMPLATES.TemplateResponse(request, "partials/inbox.html", inbox)


@router.get("/partials/epics", response_class=HTMLResponse)
async def web_partial_epics(request: Request):
    epics = await services.get_epics_enriched(_db(request))
    return TEMPLATES.TemplateResponse(
        request, "partials/epic_list.html", {"epics": epics}
    )


@router.get("/partials/kanban", response_class=HTMLResponse)
async def web_partial_kanban(request: Request):
    data = await services.get_dashboard_data(_db(request))
    return TEMPLATES.TemplateResponse(request, "partials/kanban.html", {"data": data})


@router.get("/tasks/list", response_class=HTMLResponse)
async def web_tasks_list_partial(
    request: Request,
    status: str | None = None,
    task_type: str | None = Query(default=None, alias="type"),
    priority: str | None = None,
    source: str | None = None,
    parent_id: int | None = None,
    human_owner: str | None = None,
    human_reviewer: str | None = None,
    limit: int = Query(default=100, le=200),
):
    """HTML fragment: task table body for HTMX swap."""
    tasks = await services.list_tasks(
        _db(request),
        status=status,
        task_type=task_type,
        priority=priority,
        source=source,
        parent_id=parent_id,
        human_owner=human_owner,
        human_reviewer=human_reviewer,
        limit=limit,
    )
    return TEMPLATES.TemplateResponse(
        request, "partials/task_table.html", {"tasks": tasks}
    )


# ---------------------------------------------------------------------------
# Full-page web routes
# ---------------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
async def web_dashboard(request: Request):
    db = _db(request)
    data = await services.get_dashboard_data(db)
    inbox = await services.get_inbox_data(db)
    epics = await services.get_epics_enriched(db)
    inbox_total = (
        len(inbox["drafts"])
        + len(inbox["questions"])
        + len(inbox["decisions"])
        + len(inbox["pending_reports"])
        + len(inbox["stale_tasks"])
    )
    ctx: dict[str, Any] = {
        "data": data,
        "epics_enriched": epics,
        "epics": epics,
        "inbox_total": inbox_total,
    }
    ctx.update(inbox)
    return TEMPLATES.TemplateResponse(request, "dashboard.html", ctx)


@router.get("/tasks", response_class=HTMLResponse)
async def web_tasks(
    request: Request,
    status: str | None = None,
    task_type: str | None = Query(default=None, alias="type"),
    priority: str | None = None,
    source: str | None = None,
    parent_id: int | None = None,
    human_owner: str | None = None,
    human_reviewer: str | None = None,
):
    db = _db(request)
    tasks = await services.list_tasks(
        db,
        status=status,
        task_type=task_type,
        priority=priority,
        source=source,
        parent_id=parent_id,
        human_owner=human_owner,
        human_reviewer=human_reviewer,
        limit=100,
    )

    parent_breadcrumb = None
    if parent_id is not None:
        crumbs = await db_module.get_breadcrumb(db, parent_id)
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
            "filter_parent_id": parent_id,
            "filter_human_owner": human_owner or "",
            "filter_human_reviewer": human_reviewer or "",
            "parent_breadcrumb": parent_breadcrumb,
            "all_statuses": all_statuses,
            "all_types": all_types,
            "all_priorities": all_priorities,
        },
    )


@router.get("/tasks/{task_id}", response_class=HTMLResponse)
async def web_task_detail(task_id: int, request: Request):
    db = _db(request)
    row = await repo.get_task(db, task_id)
    if not row:
        raise HTTPException(404, "task not found")
    updates = await repo.get_task_updates(db, task_id)
    task_view = services.row_to_task(row, updates=updates)
    task = await services.enrich_task_view(db, task_view)
    return TEMPLATES.TemplateResponse(request, "task_detail.html", {"task": task})


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
    created = await services.create_task(_db(request), body)
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
    body = TaskApprove(
        comment=comment,
        run=run,
        runtime=RuntimeChoice(runtime) if runtime else None,
        force=force,
    )
    await services.approve_task(_db(request), task_id, body)
    if _is_htmx(request):
        return await _htmx_task_done_fragment(request, task_id)
    return RedirectResponse(f"/tasks/{task_id}", status_code=303)


@router.post("/tasks/{task_id}/web-reject")
async def web_reject_task(
    task_id: int,
    request: Request,
    comment: str = Form(""),
):
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
    body = TaskStart(runtime=RuntimeChoice(runtime) if runtime else None)
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


@router.post("/tasks/{task_id}/web-force-complete")
async def web_force_complete_task(
    task_id: int,
    request: Request,
    comment: str = Form(""),
):
    """Force-complete a pending_report task from the Web UI.

    The audit-trail comment can be supplied either via the ``HX-Prompt``
    header (populated by htmx ``hx-prompt``) or via a ``comment`` form field
    for non-htmx clients. The header takes precedence.
    """
    reason = request.headers.get("HX-Prompt", "") or comment
    body = TaskForceComplete(comment=reason) if reason else None
    await services.force_complete_task(_db(request), task_id, body)
    if _is_htmx(request):
        return await _htmx_task_done_fragment(request, task_id)
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
    body = TaskApprove(comment=comment, run=True)
    await services.approve_task(_db(request), proposal_id, body)
    return RedirectResponse("/", status_code=303)


@router.post("/proposals/{proposal_id}/reject")
async def web_reject_proposal_compat(
    proposal_id: int,
    request: Request,
    comment: str = Form(""),
):
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


@router.get("/admin", response_class=HTMLResponse)
async def web_admin_summary(request: Request):
    _require_admin_web(request)
    from hub.services import admin as admin_svc

    summary = await admin_svc.admin_summary(_db(request))
    return TEMPLATES.TemplateResponse(
        request, "admin/summary.html", {"summary": summary, "active": "admin"}
    )


@router.get("/admin/users", response_class=HTMLResponse)
async def web_admin_users(request: Request):
    _require_admin_web(request)
    from hub.services import admin as admin_svc

    principals = await admin_svc.list_principals(_db(request), kind="human")
    return TEMPLATES.TemplateResponse(
        request, "admin/users.html", {"principals": principals, "active": "admin"}
    )


@router.get("/admin/agents", response_class=HTMLResponse)
async def web_admin_agents(request: Request):
    _require_admin_web(request)
    from hub.services import admin as admin_svc

    principals = await admin_svc.list_principals(_db(request), kind="agent")
    return TEMPLATES.TemplateResponse(
        request, "admin/agents.html", {"principals": principals, "active": "admin"}
    )


@router.get("/admin/roles", response_class=HTMLResponse)
async def web_admin_roles(request: Request):
    _require_admin_web(request)
    from hub.services import admin as admin_svc

    roles = await admin_svc.list_roles(_db(request))
    return TEMPLATES.TemplateResponse(
        request, "admin/roles.html", {"roles": roles, "active": "admin"}
    )


@router.get("/admin/keys", response_class=HTMLResponse)
async def web_admin_keys(request: Request):
    _require_admin_web(request)
    from hub.services import admin as admin_svc

    keys = await admin_svc.list_api_keys(_db(request))
    principals = await admin_svc.list_principals(_db(request))
    pid_to_name = {p["id"]: p["username"] for p in principals}
    return TEMPLATES.TemplateResponse(
        request,
        "admin/keys.html",
        {"keys": keys, "pid_to_name": pid_to_name, "active": "admin"},
    )


@router.get("/admin/audit", response_class=HTMLResponse)
async def web_admin_audit(request: Request):
    _require_admin_web(request)
    from hub.services import admin as admin_svc

    entries = await admin_svc.list_audit(_db(request), limit=100)
    return TEMPLATES.TemplateResponse(
        request, "admin/audit.html", {"entries": entries, "active": "admin"}
    )
