"""Web UI routes (HTML / HTMX) for Haiplane Hub dashboard."""

from __future__ import annotations

import hashlib
import html
import json
import logging
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote

import aiosqlite
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from hub import brand
from hub import config
from hub import db as db_module
from hub import repository as repo
from hub import services
from hub.actionable_errors import (
    agent_create_forbidden_detail,
    human_only_gate_detail,
)
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
from hub.services import project_policy
from hub.version import get_app_version
from hub.models import (
    FindingDisposition,
    FindingDispositionItem,
    MessageSend,
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
TEMPLATES.env.globals["product_name"] = brand.PRODUCT_NAME
TEMPLATES.env.globals["product_title"] = brand.PRODUCT_TITLE
TEMPLATES.env.globals["app_version"] = get_app_version()

router = APIRouter()

log = logging.getLogger("hub.web")


def _enum_from_form(enum_cls, value: str, field: str):
    """Build an enum from a form field, refusing garbage with a 400.

    Constructing the enum inline raised a bare ValueError out of the handler,
    which reaches the client as a 500 — a server-fault answer to a malformed
    request (#367). The message lists the accepted values, because the usual
    cause is a renamed member, not an attack.
    """
    try:
        return enum_cls(value)
    except ValueError as exc:
        allowed = ", ".join(m.value for m in enum_cls)
        raise HTTPException(
            400, f"{field} must be one of: {allowed} (got {value!r})"
        ) from exc


def _page_query(request: Request) -> int:
    """Read a 1-based ``page`` query param.

    Two different failures hide here, and only the first one looks like a bug.
    A non-integer raised ValueError straight out of the handler — a 500. A
    non-positive integer parsed fine and produced a negative OFFSET, which
    SQLite quietly treats as none: the page rendered as if it were page 1
    while the template still called it page 0, and the "previous" link walked
    further into negative numbers. Guarding int() alone would have fixed the
    crash and left that in place (#367).
    """
    raw = request.query_params.get("page", "")
    if raw == "":
        return 1
    try:
        page = int(raw)
    except ValueError as exc:
        raise HTTPException(400, f"page must be an integer (got {raw!r})") from exc
    return max(page, 1)


def _state_query(value: str | None) -> str | None:
    """Validate the named status-set mode, refusing anything unknown (#617).

    Deliberately NOT lenient: a ``state`` the server does not recognise must
    fail loudly, because a filter that silently does nothing leaves the caller
    believing a list was narrowed when it was not — which is the class of defect
    this task exists to remove.
    """
    if value is None or value == "":
        return None
    try:
        repo.task_state_condition(value)
    except repo.UnknownTaskStateError as exc:
        raise HTTPException(400, str(exc)) from exc
    return value


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
    """Log out: revoke the server-side session, then drop the cookie.

    Dropping the cookie alone left the browser_sessions row live, so a
    session token captured beforehand kept authenticating for the rest of
    its lifetime — up to 30 days — and logging out did nothing to a
    compromised session (#368).
    """
    from hub.services import admin as admin_svc

    session_token = request.cookies.get(config.HUB_COOKIE_NAME)
    if session_token:
        try:
            await admin_svc.revoke_browser_session(_db(request), session_token)
        except Exception:
            # Logging out must always end at the login page. A failure to
            # reach the DB is worth a log line, not a 500 that strands the
            # user on a page they are trying to leave — the cookie still
            # goes away below.
            log.exception("logout: could not revoke the server-side session")

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


# #630: the mode a bare /tasks opens in. Named once so the handler, the chip
# markup and the tests cannot drift apart on which one is the default.
QUEUE_STATE = "awaiting"

# What a person has to DO, in the order idleness costs most. Membership is by
# status only — the headless-review exclusion is NOT repeated here, because
# TASK_STATE_FILTERS['awaiting'] has already applied it upstream: a review with
# review_job_id belongs to the poller's conveyor, not to a reader (#567, #617).
QUEUE_GROUPS: tuple[tuple[str, str, str], ...] = (
    (
        "draft",
        "Одобрить постановку",
        "ждут гейта DoR — пока не одобрите, никто не начнёт",
    ),
    ("needs_info", "Ответить на вопрос", "агент остановился и ждёт ответа"),
    (
        "needs_decision",
        "Принять решение",
        "развилка, которую агент не вправе закрыть сам",
    ),
    ("review", "Согласовать сдачу", "работа сдана и ждёт вердикта"),
)


def _queue_groups(tasks: list[Any]) -> list[dict[str, Any]]:
    """Split an already-filtered awaiting list into the actions it asks for.

    Empty groups are KEPT: "Принять решение 0" is an answer, and a group that
    vanishes when it empties makes the reader wonder whether it was there at
    all — the same rule #615 settled for silence elsewhere.
    """
    by_status: dict[str, list[Any]] = {key: [] for key, _, _ in QUEUE_GROUPS}
    for task in tasks:
        status = getattr(task.status, "value", task.status)
        if status in by_status:
            by_status[status].append(task)
    return [
        {"key": key, "label": label, "why": why, "items": by_status[key]}
        for key, label, why in QUEUE_GROUPS
    ]


async def _analyst_ready_map(
    db: aiosqlite.Connection,
    tasks: list[Any],
) -> dict[int, bool]:
    """The badge for a whole list, in at most ONE extra query (#629).

    What it used to do: for every row, recompute readiness (which reads all of
    that task's updates and, on a mismatch, WRITES the repaired values under a
    write lock — refinement.py, "lazy repair" from #250) and then, for rows
    without ``prepared_by``, read the updates AGAIN looking for the legacy
    marker. On production that was up to 100 recomputes plus ~80 update scans
    to draw one binary badge — measured: only 41 of 200 tasks carry both
    dor_passed and prepared_by, so four rows in five took the slow path.

    The signal itself is unchanged: DoR passed AND evidence that an analyst
    prepared the task — ``prepared_by`` for modern rows, an update carrying
    ANALYST_PREPARED_MARKER for rows older than that column. Only the source of
    dor_passed moves, from a recompute to the persisted column; the repair
    still runs when a single task is opened and at refine.
    """
    result: dict[int, bool] = {}
    legacy_candidates: list[int] = []
    for task in tasks:
        if not getattr(task, "dor_passed", False):
            result[task.id] = False
        elif (getattr(task, "prepared_by", "") or "").strip():
            result[task.id] = True
        else:
            legacy_candidates.append(task.id)
    if legacy_candidates:
        marked = await repo.task_ids_with_update_marker(
            db, legacy_candidates, ANALYST_PREPARED_MARKER
        )
        for task_id in legacy_candidates:
            result[task_id] = task_id in marked
    return result


def _form_strings(values: list[Any]) -> list[str]:
    """Только текстовые значения поля формы.

    ``form.getlist`` отдаёт ``UploadFile | str``: одно и то же имя поля может
    принести файл. Для ролей это не режим работы, а мусор на входе, и он
    отбрасывается здесь, а не превращается в имя роли где-то ниже (#848).
    """
    return [v for v in values if isinstance(v, str)]


def _task_list(result: list[Any] | dict[str, Any]) -> list[Any]:
    """Список задач из ответа ``services.list_tasks``.

    Он возвращает либо список, либо страничный конверт (#254). Страницы и
    HTMX-фрагменты никогда не пагинируют, поэтому конверт здесь означал бы не
    другой режим, а изменение контракта под нами — и лучше сказать об этом
    сразу, чем отрисовать пустую таблицу (#848).
    """
    if isinstance(result, dict):
        raise HTTPException(500, "list_tasks вернул страничный конверт без запроса")
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
    # #627: the project goes into the queries, not over their results. These
    # lists are capped at 20 apiece, so the old pass afterwards silently dropped
    # every row of a project whose work was not among the newest.
    inbox = await services.get_inbox_data(
        db,
        human_owner=human_owner,
        claimed_by=claimed_by,
        mine=mine,
        project=project,
    )
    inbox["dispatch_available"] = _dispatch_available()
    return TEMPLATES.TemplateResponse(request, "partials/inbox.html", inbox)


@router.get("/partials/epics", response_class=HTMLResponse)
async def web_partial_epics(request: Request):
    epics = await services.get_epics_enriched(_db(request))
    return TEMPLATES.TemplateResponse(
        request,
        "partials/epic_list.html",
        # #571: the count travels WITH the epic list, because a task outside
        # every epic is invisible in exactly this view.
        # #570: and the list arrives grouped by project with the finished epics
        # as a counted block — computed server-side, so this fragment and the
        # dashboard cannot disagree about either.
        {
            "epics": epics,
            "board": await services.get_epic_board(_db(request)),
            "orphan_live": await repo.count_live_orphan_tasks(_db(request)),
        },
    )


@router.get("/partials/kanban", response_class=HTMLResponse)
async def web_partial_kanban(request: Request, project: str | None = Query(None)):
    db = _db(request)
    # #627: narrowed in the query. This fragment refreshes every 30 seconds, so
    # the old post-filter did not merely hide a project's board once — it kept
    # re-hiding it.
    data = await services.get_dashboard_data(db, project=project)
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
    # #571: a SEPARATE flag rather than a magic parent_id value — a blank
    # parent_id must keep meaning "no filter" (pinned by
    # test_tasks_list_filters_ignore_blank_parent_id).
    no_epic: bool = Query(default=False),
    # #617: named status-set mode (live | awaiting | inflight). Wired into BOTH
    # routes on purpose — a parameter that only the fragment honours gives a link
    # that silently ignores it, and the counter's link points at the page.
    state: str | None = Query(default=None),
    # #626: the same rule, applied to the parameter that slipped past it. The
    # page honoured ``project`` and this fragment did not, so the first change to
    # any filter swapped one project's rows for every project's — while the
    # selector went on naming the project the reader thought they were in.
    project: str | None = Query(default=None),
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
        no_epic=no_epic,
        state=_state_query(state),
        human_owner=human_owner,
        human_reviewer=human_reviewer,
        claimed_by=claimed_by,
        mine=mine,
        limit=limit,
        # Same path the page takes since #621 — into the query, before the
        # LIMIT. Two routes answering one question must not answer it twice.
        project=(project or "").strip() or None,
    )
    tasks, ready_by_id = await _apply_analyst_ready_filter(
        _db(request), _task_list(tasks), analyst_ready=analyst_ready
    )
    # #630: the fragment renders the SAME shape the page does. Serving a table
    # here while the page shows the queue would rearrange the screen under a
    # reader who only changed a priority — the swap target is the same div.
    if state == QUEUE_STATE:
        return TEMPLATES.TemplateResponse(
            request,
            "partials/task_queue.html",
            {
                "queue_groups": _queue_groups(tasks),
                "current_project": (project or "").strip(),
                "dispatch_available": _dispatch_available(),
                "analyst_ready_by_id": ready_by_id,
            },
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


async def _project_selector_ctx(
    db: aiosqlite.Connection, project: str | None
) -> tuple[list[dict[str, Any]], str]:
    """(projects for the selector, current slug) — no subtree walk (#621).

    What a page needs to DRAW the selector is separate from what it needs to
    narrow a list. A page that scopes its query by project — the only correct
    way when the query is limited — wants this and nothing more.
    """
    rows = await repo.list_projects(db, only_active=True)
    return [dict(r) for r in rows], (project or "").strip()


async def _project_filter_ctx(
    db: aiosqlite.Connection, project: str | None
) -> tuple[set[int] | None, list[dict[str, Any]], str]:
    """(allowed task ids | None, projects for the selector, current slug) (#339).

    The remaining caller is the epic board, and only because ``list_live_epics``
    carries no LIMIT — narrowing an uncut list in Python cannot lose a row.
    Everywhere a list IS limited the project must reach the query instead; that
    mistake has now been made and fixed three times (#370, #621, #627), so the
    generic post-filter helper is gone rather than left within reach.
    """
    projects, current = await _project_selector_ctx(db, project)
    allowed: set[int] | None = None
    if current:
        prow = await repo.get_project_by_slug(db, current)
        allowed = (
            await repo.list_task_ids_for_project(db, prow["id"])
            if prow is not None
            else set()
        )
    return allowed, projects, current


@router.get("/", response_class=HTMLResponse)
async def web_dashboard(request: Request, project: str | None = Query(None)):
    db = _db(request)
    allowed, projects_list, current_project = await _project_filter_ctx(db, project)
    # #627: both aggregates narrow inside their own queries now. Every list they
    # build is capped at 20, and a cap applied before the project filter is what
    # made a project's whole board disappear.
    data = await services.get_dashboard_data(db, project=project)
    inbox = await services.get_inbox_data(db, project=project)
    epics = await services.get_epics_enriched(db)
    # The same project scope the other dashboard lists get — passed in, because
    # a board computed around the filter shows another project's epics (#570).
    board = await services.get_epic_board(db, allowed)
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
    # Coordination panels (#775): who is around, and what the sessions are
    # saying to each other. Deliberately unfiltered by project — a session
    # belongs to an agent, not to a project, and a thread the owner cannot see
    # is the one thing this feature is not allowed to have.
    agent_sessions = await services.get_agent_sessions_panel(db)
    message_threads = await services.get_message_threads_panel(db)
    ctx: dict[str, Any] = {
        "data": data,
        "agent_sessions": agent_sessions,
        "message_threads": message_threads,
        "epics_enriched": epics,
        "epics": epics,
        "board": board,
        "inbox_total": inbox_total,
        "dispatch_available": _dispatch_available(),
        "projects_list": projects_list,
        "current_project": current_project,
    }

    # #500: the delivery snapshot, read from the builder that already answers
    # REST, CLI and MCP (#499) — a fourth reader of one truth, not a fourth
    # version of it. Best-effort: a dashboard must render even when the
    # snapshot cannot be built, and the section says so rather than showing
    # empty lists that read as "nothing shipped".
    try:
        from hub.services.prod_state import prod_state as _prod_state

        ctx["prod_state"] = await _prod_state(db)
    except Exception as exc:  # noqa: BLE001 - the dashboard must render
        log.warning("prod-state section failed: %s", exc)
        ctx["prod_state"] = None
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
    # #567: the page stops being a routing table — each project carries its live
    # epics and three numbers, computed server-side.
    cards = await services.get_project_cards(_db(request))
    return TEMPLATES.TemplateResponse(
        request,
        "projects.html",
        {
            "projects": [dict(r) for r in rows],
            "cards": cards,
            "project_error": project_error,
            # #475: the create form must prefill the branch the hub would
            # actually fall back to, not a literal that stops matching it.
            "default_base_branch": config.PAIR_BASE_BRANCH,
            # Same number in the second consumer, from the same function: a
            # project holds epics, so orphan tasks belong to no project either.
            "orphan_live": await repo.count_live_orphan_tasks(_db(request)),
        },
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


# The gate-policy keys the project card actually renders (#753/#760/#805).
# For these — and only these — an empty field means "remove this knob": the
# form showed them, so the submitter had a say. Everything else in the stored
# policy is carried through untouched (#886).
_FORM_GATE_POLICY_KEYS = frozenset(
    {"dor", "verdict", "review", "dor_max_class", "risk_map", "release"}
)


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
            default_branch=(
                str(form.get("default_branch") or "").strip() or config.PAIR_BASE_BRANCH
            ),
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
    # Gate policy selects (#753). Present only for non-default projects in
    # the template; the shared PATCH path below re-checks the default lock
    # anyway — the presentation layer is not trusted.
    if any(
        key in form
        for key in (
            "gate_policy_dor",
            "gate_policy_verdict",
            "gate_policy_review",
            "gate_policy_release",
            "gate_policy_dor_max_class",
            "gate_policy_risk_map",
        )
    ):
        gate_policy: dict[str, Any] = {
            "dor": str(form.get("gate_policy_dor") or "human").strip() or "human",
            "verdict": str(form.get("gate_policy_verdict") or "human").strip()
            or "human",
        }
        # #805: the review key is offered to EVERY project, including default
        # — dispatching a reviewer takes no human out of any gate, so the
        # #743 lock (which is about 'auto' on dor/verdict) does not apply.
        # Only the recognised value is stored; anything else is dropped
        # rather than saved as a knob nothing reads.
        if str(form.get("gate_policy_review") or "").strip() == "dispatch":
            gate_policy["review"] = "dispatch"
        # #926: the release knob follows the same shape as review — one
        # recognised value stored, everything else dropped — and for the same
        # reason it is offered to EVERY project including default: the #743
        # lock is about taking a human OUT of a gate, and the content of a
        # release was already approved task by task (#812). manual is the
        # ABSENCE of the key, which is why 'release' belongs in
        # _FORM_GATE_POLICY_KEYS above: the form shows this knob, so an
        # un-chosen one really does mean "remove it". Left out of that set,
        # the carry-through below would restore the stored 'auto' and the
        # switch would only work in one direction — a stop-lever that cannot
        # stop.
        if (
            str(form.get("gate_policy_release") or "").strip()
            == project_policy.RELEASE_AUTO
        ):
            gate_policy["release"] = project_policy.RELEASE_AUTO
        # #760: the form carries the WHOLE policy, so an emptied field means
        # "remove this knob", not "leave it alone" — the same semantics the
        # selects already have, and the only ones a form can honestly offer.
        ceiling = str(form.get("gate_policy_dor_max_class") or "").strip()
        if ceiling:
            gate_policy["dor_max_class"] = ceiling
        risk_map, err = _parse_policy_form(str(form.get("gate_policy_risk_map") or ""))
        if err:
            return _projects_error_redirect(err.replace("policy:", "risk_map:"))
        if risk_map is not None:
            gate_policy["risk_map"] = risk_map
        # Keys the form does not offer (#886: ci_runner) are carried
        # over untouched. "The form carries the whole policy" is true of the
        # knobs it shows; a knob it never showed cannot be said to have been
        # emptied by the person who submitted it, and dropping it here would
        # undo an API-set value with no trace — the silent rollback this
        # task exists to remove.
        stored = await repo.get_project(_db(request), project_id)
        if stored is not None:
            kept = project_policy.gate_policy_of(stored)
            for key, value in kept.items():
                if key not in _FORM_GATE_POLICY_KEYS:
                    gate_policy[key] = value
        fields["gate_policy"] = gate_policy
    if not fields:
        return RedirectResponse("/projects", status_code=303)
    try:
        body = ProjectPatch(**fields)
    except ValidationError as exc:
        first = exc.errors()[0]
        loc = ".".join(str(p) for p in first.get("loc", ()))
        return _projects_error_redirect(f"{loc}: {first.get('msg', 'invalid')}")
    try:
        await _web_patch_project(request, project_id, body)
    except HTTPException as exc:
        # Only VALIDATION refusals (e.g. the default-project gate lock,
        # #743) land in the form as an error. Everything else — the 403 an
        # agent token earns first of all — stays a raw refusal: converting
        # it to a redirect would soften the human-only contract this route
        # is tested on.
        if exc.status_code != 422:
            raise
        detail = exc.detail
        message = (
            detail.get("hint") or detail.get("error")
            if isinstance(detail, dict)
            else str(detail)
        )
        return _projects_error_redirect(message or "запрос отклонён")
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


@router.post("/tasks/{task_id}/web-finding-dispositions")
async def web_finding_dispositions(task_id: int, request: Request):
    """The gate says what each confirmed finding turned out to be (#876).

    One form for the whole report: findings left on "не размечено" are simply
    not submitted, so a half-judged report stays half-judged instead of being
    silently completed with a default.
    """
    identity = require_human_or_admin(request)
    db = _db(request)
    form = await request.form()
    items: list[FindingDispositionItem] = []
    for key, value in form.items():
        if not key.startswith("disposition-") or not value:
            continue
        try:
            index = int(key.removeprefix("disposition-"))
        except ValueError:
            continue
        try:
            items.append(
                FindingDispositionItem(
                    finding_index=index,
                    disposition=FindingDisposition(str(value)),
                    note=str(form.get(f"note-{index}") or ""),
                )
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    if not items:
        # Nothing chosen is not an error and not a judgement: the gate looked
        # and marked nothing, which must leave the report exactly as it was.
        return RedirectResponse(f"/tasks/{task_id}", status_code=303)
    try:
        await services.record_finding_dispositions(
            db, task_id, items, decided_by=identity.username
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return RedirectResponse(f"/tasks/{task_id}", status_code=303)


@router.get("/metrics", response_class=HTMLResponse)
async def web_metrics(request: Request, since_days: int = Query(default=90, ge=1)):
    """Practice metrics page (#384)."""
    data = await services.practice_metrics(_db(request), since_days=since_days)
    return TEMPLATES.TemplateResponse(request, "metrics.html", {"m": data})


@router.get("/metrics/agent-api", response_class=HTMLResponse)
async def web_agent_api_metrics(
    request: Request, window_days: int = Query(default=14, ge=1)
):
    """Agent API usage, errors and cost (#780).

    A page of its own rather than a section on practice metrics: this one
    reads a different window (bounded by telemetry retention) and answers a
    different question — what the tool surface costs, not how the practice
    performs.
    """
    from hub.mcp_catalog import (
        HEADROOM_WARN_PCT,
        catalog_snapshot,
        check_budget,
        load_baseline,
        load_budget,
        load_measured,
    )
    from hub.services.mcp_telemetry import usage_report

    snapshot = await catalog_snapshot()
    data = await usage_report(_db(request), window_days=window_days, catalog=snapshot)
    # The same check CI runs, rendered where a human actually looks (#832).
    # Headroom bought the mergeability of the budget file; it stays honest
    # only while somebody can watch it shrink.
    budget = check_budget(snapshot, load_budget(), load_baseline(), load_measured())
    return TEMPLATES.TemplateResponse(
        request,
        "agent_api.html",
        {"u": data, "budget": budget, "headroom_warn_pct": HEADROOM_WARN_PCT},
    )


@router.get("/digests", response_class=HTMLResponse)
async def web_digests(request: Request):
    """Autopilot daily digests with the audit sample (#739)."""
    import json as _json

    rows = await repo.list_digests(_db(request), limit=30)
    digests = []
    for row in rows:
        d = dict(row)
        try:
            d["data"] = _json.loads(d.get("payload") or "{}")
        except ValueError:
            d["data"] = {}
        digests.append(d)
    return TEMPLATES.TemplateResponse(request, "digests.html", {"digests": digests})


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
    # #571: a SEPARATE flag rather than a magic parent_id value — a blank
    # parent_id must keep meaning "no filter" (pinned by
    # test_tasks_list_filters_ignore_blank_parent_id).
    no_epic: bool = Query(default=False),
    # #617: named status-set mode (live | awaiting | inflight). Wired into BOTH
    # routes on purpose — a parameter that only the fragment honours gives a link
    # that silently ignores it, and the counter's link points at the page.
    state: str | None = Query(default=None),
    analyst_ready: bool = Query(default=False),
    project: str | None = Query(default=None),
):
    db = _db(request)
    projects_list, current_project = await _project_selector_ctx(db, project)
    # #630: a bare /tasks answers "what is needed from me", not "what exists".
    # Measured before: the first screen held 100 rows, 61 of them completed,
    # with the 18 drafts waiting for this reader's approval mixed in between.
    #
    # The default applies ONLY to a request with no query at all. Any parameter
    # — project included — means the caller asked for something specific, and
    # honouring it is what keeps "Вся доска проекта" from turning into "the
    # queue of that project" behind the same words (#621 fixed that link; this
    # must not un-fix it).
    defaulted_to_queue = not request.query_params
    if defaulted_to_queue:
        state = QUEUE_STATE
    parsed_parent_id = _optional_int_query(parent_id, "parent_id")
    # The project goes INTO the query, not into a pass over its result (#621).
    # This list is limited, and post-filtering a limited list spends the limit on
    # other projects' rows: audit-evidence promised 17 queued and opened zero,
    # which reads as "this project has no work". ``repo.list_tasks_filtered``
    # applies project_id BEFORE the LIMIT and carries #370's comment saying why.
    tasks = await services.list_tasks(
        db,
        status=status,
        task_type=task_type,
        priority=priority,
        source=source,
        parent_id=parsed_parent_id,
        no_epic=no_epic,
        state=_state_query(state),
        human_owner=human_owner,
        human_reviewer=human_reviewer,
        limit=100,
        project=current_project or None,
    )
    tasks, ready_by_id = await _apply_analyst_ready_filter(
        db, _task_list(tasks), analyst_ready=analyst_ready
    )

    parent_breadcrumb = None
    if parsed_parent_id is not None:
        crumbs = await db_module.get_breadcrumb(db, parsed_parent_id)
        parent_breadcrumb = crumbs

    all_statuses = [s.value for s in TaskStatus]
    all_types = [t.value for t in TaskType]
    all_priorities = ["critical", "high", "medium", "low"]

    # #630: the four counters come from ONE query built out of the same
    # TASK_STATE_FILTERS conditions the lists use, so a chip cannot promise a
    # number the page behind it does not hold.
    project_row = (
        await repo.get_project_by_slug(db, current_project) if current_project else None
    )
    mode_counts = await repo.count_tasks_by_state(
        db, project_row["id"] if project_row is not None else None
    )
    queue_groups = _queue_groups(tasks) if state == QUEUE_STATE else None

    return TEMPLATES.TemplateResponse(
        request,
        "tasks.html",
        {
            "tasks": tasks,
            "mode_counts": mode_counts,
            "queue_groups": queue_groups,
            "queue_state": QUEUE_STATE,
            "defaulted_to_queue": defaulted_to_queue,
            "filter_status": status or "",
            "filter_type": task_type or "",
            "filter_priority": priority or "",
            "filter_source": source or "",
            "filter_parent_id": parsed_parent_id,
            "filter_human_owner": human_owner or "",
            "filter_human_reviewer": human_reviewer or "",
            "filter_analyst_ready": analyst_ready,
            # #626: the scope has to reach the template, or the filter bar
            # cannot hand it back on the next request.
            "filter_state": state or "",
            "filter_no_epic": no_epic,
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

    # Machine review (#381): summary next to the verdict buttons, assembled
    # by the same builder the review brief uses (#808) — the human at the
    # gate and the reviewing agent must not read two different reports.
    from hub.services.review_evidence import review_report as _review_report

    mr_row = await repo.get_latest_machine_review(db, task_id)
    review_report = await _review_report(db, dict(row), mr_row)
    machine_review = review_report.machine_review

    # #823: the evidence the reviewing agent has always had — per-AC test
    # results, CI against the pinned sha, statement freshness, call sites and
    # the coverage verdict over them — assembled by the SAME builder that
    # answers /review-brief. Two readers, one assembly: the card and the brief
    # cannot drift apart because there is nothing to keep in step.
    from hub.services.review_brief import gate_evidence

    evidence = await gate_evidence(db, dict(row))

    # #497: merged is not running. The hub records both facts now — its own
    # merge (#534) and the deploy CI reported (#839, #496) — and this compares
    # them. Computed, never stored: the answer changes with every release.
    delivery = None
    if task.status.value in ("completed", "review", "fix_requested"):
        from hub.services.delivery_state import delivery_state

        try:
            delivery = await delivery_state(db, task_id)
        except Exception as exc:  # noqa: BLE001 - the card must render regardless
            log.warning("delivery state failed for #%s: %s", task_id, exc)
            delivery = None

    # #825: the criteria and the changes, laid against each other. Built from
    # the numstat of the pinned submission — paths, not hunks: the hunks load
    # on demand (#824), and this map only needs to know which files moved.
    change_map = None
    if evidence is not None:
        from hub.services import change_map as change_map_service
        from hub.services.task_diff import READ, submission_files

        listing = await submission_files(db, task_id)
        if listing["state"] == READ:
            findings = []
            if machine_review is not None and machine_review.is_current:
                findings = list(machine_review.findings_confirmed or [])
            change_map = change_map_service.build(
                listing["files"],
                evidence.acceptance_criteria,
                evidence.ac_test_results,
                task.affected_areas or [],
                findings,
            )
        else:
            # Same rule as every other block here: no map is a stated cause,
            # never an empty list that reads as "nothing changed" (#725).
            change_map = {"unavailable": listing["reason"]}

    # #893: what this task has cost in REVIEW RUNS. Measured across eleven
    # billed runs, the run is the unit that tracks spend — none billed under
    # 777k tokens, and the size of the diff explained none of the spread — so
    # five resubmissions cost five entry prices. A sum alone would hide that;
    # the count is the point. Runs whose bill never arrived say "неизвестно",
    # never nothing, because a missing bill is not a free run (#725).
    review_runs = [
        {
            "generation": int(r["submission_generation"] or 0),
            "profile": (r["profile"] or "") or "не заявлен",
            "provider_tokens": r["provider_tokens"],
            "tokens_spent": r["tokens_spent"],
            "created_at": r["created_at"],
        }
        for r in map(dict, await repo.list_machine_reviews(db, task_id))
    ]
    review_runs_billed = [
        r["provider_tokens"] for r in review_runs if r["provider_tokens"] is not None
    ]
    review_runs_cost = {
        "runs": len(review_runs),
        "billed_runs": len(review_runs_billed),
        "provider_total": sum(review_runs_billed) if review_runs_billed else None,
    }

    # Machine-review policy gap (#382): warning in the verdict panel.
    machine_review_gap_text = None
    if task.status.value == "review" and not task.review_job_id:
        from hub.services.orchestration import machine_review_gap

        machine_review_gap_text = await machine_review_gap(db, dict(row))

    # The conversation about this task (#775) — a separate list from the
    # update feed above it: updates are the lifecycle journal, messages are
    # people and agents talking. Merged into one stream both would lose their
    # meaning.
    task_messages = [
        services.message_view(row) for row in await repo.list_task_messages(db, task_id)
    ]

    # #814: what was observed in production, and on which build. The delivered
    # sha is what the evidence should be about — a record taken elsewhere is
    # shown with that said out loud rather than quietly counted as proof.
    delivered_sha = await repo.merge_sha_for_task(db, task_id)
    live_checks = [
        {
            **services.live_check_view(row),
            "sha_mismatch": bool(
                delivered_sha
                and (dict(row).get("sha") or "")
                and dict(row).get("sha") != delivered_sha
            ),
        }
        for row in await repo.list_live_checks(db, task_id)
    ]

    return TEMPLATES.TemplateResponse(
        request,
        "task_detail.html",
        {
            "task": task,
            "task_messages": task_messages,
            "live_checks": live_checks,
            "delivered_sha": delivered_sha,
            "delivery": delivery,
            "evidence": evidence,
            "change_map": change_map,
            "machine_review": machine_review,
            "review_runs": review_runs,
            "review_runs_cost": review_runs_cost,
            "review_report": review_report,
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


@router.get("/tasks/{task_id}/diff", response_class=HTMLResponse)
async def web_task_diff(task_id: int, request: Request):
    """The changes of the PINNED submission, rendered inside the hub (#824).

    No revision parameter by construction: this serves one task's submission,
    not arbitrary history, and the diff a reader sees must be the diff the
    verdict is cast on. Authentication is the card's own — the middleware
    covers this path like every other page.
    """
    db = _db(request)
    row = await repo.get_task(db, task_id)
    if not row:
        raise HTTPException(404, "task not found")

    from hub.services.task_diff import submission_diff

    diff = await submission_diff(db, task_id)
    return TEMPLATES.TemplateResponse(
        request,
        "partials/task_diff.html",
        {"diff": diff, "task_id": task_id},
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
    # Ready work is human-only (#360). This form builds TaskCreate with the
    # default source=human and honours run_immediately, so without the gate an
    # agent token created a task straight in running — the same bypass the REST
    # endpoint was closed against, through a different door.
    #
    # Deliberately NOT _require_human_web: that helper answers human_only_gate,
    # whose remediation is "retry with a human token" — something an agent
    # cannot do. Creation is the one refusal with an agent-reachable
    # alternative, so it must point at hub_propose_task exactly as the REST
    # endpoints do. The other web routes keep human_only_gate, because for
    # approve/reject/decide "a human must do this" really is the whole answer.
    if current_identity(request).is_agent:
        raise HTTPException(403, detail=agent_create_forbidden_detail())
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
        task_type=_enum_from_form(TaskType, task_type, "task_type"),
        parent_id=parent_id,
        work_type=(
            _enum_from_form(WorkType, work_type, "work_type")
            if task_type == "task"
            else WorkType.feature
        ),
        priority=priority,
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
        # Проверка и её следствие должны стоять рядом: isinstance, спрятанный
        # в булев флаг, не сужает тип на строках ниже — и dict там держался на
        # честном слове автора.
        if (
            exc.status_code != 422
            or not isinstance(detail, dict)
            or detail.get("error") != "dor_failed"
        ):
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


@router.post("/tasks/{task_id}/web-message")
async def web_send_message(
    task_id: int,
    request: Request,
    body: str = Form(...),
    to_kind: str = Form("task"),
    to_ref: str = Form(""),
    kind: str = Form("note"),
):
    """The owner writes into the same channel the agents use (#775).

    Same endpoint, same table, same provenance rules — a separate path for
    humans would make the UI and the API tell different stories about who said
    what. The identity is the web session's own.
    """
    _require_human_web(request)
    identity = current_identity(request)
    payload = MessageSend(
        to_kind=to_kind or "task",
        to_ref=(to_ref or str(task_id)),
        body=body,
        kind=kind or "note",
        related_task_id=task_id,
    )
    await services.send_message(
        _db(request),
        payload,
        agent=identity.username,
        principal_id=identity.principal_id,
    )
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


@router.post("/tasks/web-batch-approve-selected")
async def web_batch_approve_selected(
    request: Request,
    task_ids: Annotated[list[int], Form()] = [],  # noqa: B006 - FastAPI Form default
    project: Annotated[str, Form()] = "",
):
    """Approve the drafts the reader TICKED — the group path of #628.

    It replaces "approve every ready draft", whose set the server computed:
    with a computed set the reader cannot see what the click will do, and the
    label proved it — viewing a project with one ready draft, the button read
    "(1)" and approved two, the second from a project not on screen. Both left
    the human DoR gate without being looked at.

    An empty selection approves NOTHING. Reading it as "then everything" would
    rebuild the same defect in new clothes, and a test pins that.

    Per-task guards and force-never live in ``batch_approve_tasks`` (#252) and
    are unchanged; what changed is who chooses the set. Its result is rendered
    instead of discarded, because silently dropping a task the reader
    explicitly ticked is its own lie.
    """
    db = _db(request)
    identity = current_identity(request)
    if identity.is_agent:
        raise HTTPException(403, detail=human_only_gate_detail())
    result = None
    if task_ids:
        result = await services.batch_approve_tasks(
            db,
            BatchApprove(task_ids=task_ids, comment="Approved from inbox selection"),
        )
    if _is_htmx(request):
        # Re-rendered INSIDE the project the reader was looking at: refreshing
        # unscoped would widen the list under them right after they acted.
        inbox = await services.get_inbox_data(db, project=project or None)
        inbox["dispatch_available"] = _dispatch_available()
        inbox["batch_result"] = result
        return TEMPLATES.TemplateResponse(request, "partials/inbox.html", inbox)
    query = f"?project={quote(project)}" if project else ""
    return RedirectResponse(f"/{query}", status_code=303)


def _split_anchor(message: str) -> tuple[str, str, int | None]:
    """Split a trailing ``@ path:line`` anchor off a finding's text (#826).

    Optional by design. A required anchor would be answered with a click on
    whatever line happened to be under the cursor, and a made-up address is
    worse than an honest none — this is the same reason the panel never
    invents an attribution (#825).
    """
    head, sep, tail = message.rpartition("@")
    if not sep or not head.strip():
        return message, "", None
    path, colon, number = tail.strip().rpartition(":")
    if not colon or not number.strip().isdigit() or not path.strip():
        return message, "", None
    return head.strip(), path.strip(), int(number.strip())


def _parse_findings_form(text: str) -> list[ReviewFinding]:
    """Parse the review-panel findings textarea into structured findings.

    One finding per line, optionally prefixed with a severity:
    ``high: message`` / ``medium: message`` / ``low: message``. Lines
    without a recognized severity prefix default to medium. Ids are
    assigned by position — stable within this submission (#308).

    A line may end with ``@ path/to/file.py:42`` (#826) — clicking a line in
    the rendered diff appends exactly that. The anchor lands in the finding's
    own ``file``/``line`` fields, the ones machine review already fills, so a
    human finding and an agent finding stay one kind of thing.
    """
    findings: list[ReviewFinding] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        severity, _, rest = line.partition(":")
        sev_token = severity.strip().lower()
        if sev_token in ReviewSeverity.__members__ and rest.strip():
            message, path, number = _split_anchor(rest.strip())
            findings.append(
                ReviewFinding(
                    id=len(findings) + 1,
                    severity=ReviewSeverity(sev_token),
                    message=message,
                    file=path,
                    line=number,
                )
            )
        else:
            message, path, number = _split_anchor(line)
            findings.append(
                ReviewFinding(
                    id=len(findings) + 1,
                    severity=ReviewSeverity.medium,
                    message=message,
                    file=path,
                    line=number,
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
            verdict=verdict,
            agent=identity.username,
            comments=comments,
            findings=_parse_findings_form(findings_text),
        )
    except ValidationError as exc:
        errors = exc.errors()
        first_msg = (
            errors[0].get("msg", "validation error") if errors else "validation error"
        )
        msg = f"Invalid review form: {first_msg}"
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
    """The machine-identities page — one renderer shared with every POST (#613).

    This used to assemble the context itself, a copy of ``_render_agents_page``.
    Two renderers meant a change had to be made twice or the page disagreed with
    itself: the first fix here left service identities visible after an edit and
    invisible on load.
    """
    _require_admin_web(request)
    return await _render_agents_page(request)


@router.get("/admin/roles", response_class=HTMLResponse)
async def web_admin_roles(request: Request):
    _require_admin_web(request)
    from hub.services import admin as admin_svc

    # #614: which of these permissions actually gate anything. Taken from the
    # single source in hub/db.py rather than spelled out in the template: a
    # second copy of the list is a second thing to forget.
    from hub.db import DECLARED_ONLY_PERMISSIONS

    db = _db(request)
    roles = await admin_svc.list_roles(db)
    nav = await _admin_nav_counts(db)
    return TEMPLATES.TemplateResponse(
        request,
        "admin/roles.html",
        {
            "roles": roles,
            "declared_only_permissions": sorted(DECLARED_ONLY_PERMISSIONS),
            "active": "admin",
            **nav,
        },
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
    page = _page_query(request)
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
    # Machine identities of BOTH kinds (#613). Filtering kind='agent' made every
    # `service` principal invisible outside the keys page — and `service` is what
    # a CI reporter is (#546), so an identity could be created through the API
    # and then never administered again. Two calls rather than a new query: the
    # service layer filters by a single kind and nothing here needs more.
    principals = sorted(
        [
            *await admin_svc.list_principals(db, kind="agent"),
            *await admin_svc.list_principals(db, kind="service"),
        ],
        key=lambda p: p["id"],
    )
    roles = await admin_svc.list_roles(db)
    nav = await _admin_nav_counts(db)
    resp = TEMPLATES.TemplateResponse(
        request,
        "admin/agents.html",
        {"principals": principals, "roles": roles, "active": "admin", **nav},
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
            db, principal_id, _form_strings(selected_roles), granted_by=actor_id
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
    role: str = Form("agent"),
    kind: str = Form("agent"),
):
    _require_admin_web(request)
    from hub.services import admin as admin_svc

    db = _db(request)
    actor_id = _admin_actor_id(request)
    try:
        # #613: the role comes from the form, defaulting to `agent` so an admin
        # who chooses nothing gets exactly the previous behaviour. Hardcoding it
        # meant every identity created here was an `agent` — the one role that
        # deliberately lacks tasks.ci_report (#546), so the narrow role existed
        # and could not be granted.
        principal = await admin_svc.create_principal(
            db,
            kind=kind.strip() or "agent",
            username=username.strip(),
            display_name=display_name.strip(),
            notes=notes.strip(),
            role_slug=role.strip() or "agent",
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


@router.post("/admin/agents/{principal_id}/edit-roles", response_class=HTMLResponse)
async def web_admin_edit_agent_roles(principal_id: int, request: Request):
    """Change a machine identity's roles from the page where it is visible (#613).

    Deliberately a separate handler from the users one rather than a shared
    route: htmx swaps by ``hx-select``, so a response carrying the users table
    would silently replace the agents table with a list of humans. The
    assignment itself stays in ``admin_svc.set_principal_roles`` — a second copy
    of that logic would be a second place for the last-admin guard to be
    forgotten.
    """
    _require_admin_web(request)
    from hub.services import admin as admin_svc

    db = _db(request)
    actor_id = _admin_actor_id(request)
    form = await request.form()
    selected_roles = form.getlist("roles")
    if not selected_roles:
        return await _render_agents_page(
            request, flash_msg="At least one role must be selected", flash_level="error"
        )
    try:
        new_slugs = await admin_svc.set_principal_roles(
            db, principal_id, _form_strings(selected_roles), granted_by=actor_id
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
        return await _render_agents_page(
            request, flash_msg=f"Roles updated for '{uname}'"
        )
    except admin_svc.LastAdminError:
        return await _render_agents_page(
            request,
            flash_msg="Cannot remove admin role from the last active admin",
            flash_level="error",
        )
    except ValueError as exc:
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
