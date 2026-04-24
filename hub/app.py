"""OpenClaw Hub — FastAPI application with REST API and web dashboard."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite
import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles

from hub import config, services
from hub import db as db_module
from hub import repository as repo
from hub.db import get_db
from hub.integrations.registry import plugins
from hub.models import (
    AcceptanceCriterion,
    ActivityItem,
    DashboardData,
    ReadinessReport,
    TaskAnswer,
    TaskApprove,
    TaskContextView,
    TaskCreate,
    TaskDecide,
    TaskForceComplete,
    TaskQuestion,
    TaskRefine,
    TaskReject,
    TaskReorder,
    TaskSource,
    TaskStart,
    TaskTreeNode,
    TaskUpdateCreate,
    TaskUpdateView,
    TaskView,
)
from hub.auth import AuthMiddleware
from hub.mcp_server import mcp as mcp_server
from hub.services.refinement import (
    DuplicateAcceptanceCriterionError,
    TaskNotFoundError,
)
from hub.poller import start_poller
from hub.web import router as web_router

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
)
log = logging.getLogger("hub")

HERE = Path(__file__).parent


def _register_plugins() -> None:
    """Register concrete integration plugins based on available binaries/config."""
    from pathlib import Path

    if config.DISPATCH_BIN and Path(config.DISPATCH_BIN).exists():
        from hub.integrations.dispatch import DispatchIntegration

        plugins.dispatch = DispatchIntegration()

    if config.WORKSPACE_REPO_LINK and config.WORKSPACE_REPO_LINK.exists():
        from hub.integrations.git_ops import GitOpsIntegration

        plugins.git_ops = GitOpsIntegration()

    if config.GH_BIN:
        from hub.integrations.github import GitHubIntegration

        plugins.github = GitHubIntegration()

    if config.N4L_BIN:
        from hub.integrations.notes import NotesIntegration

        plugins.notes = NotesIntegration()

    if config.VAST_JOB_BIN:
        from hub.integrations.vast import VastIntegration

        plugins.vast = VastIntegration()

    if config.TRANSCRIPTS_DIR:
        from hub.integrations.transcripts import TranscriptsIntegration

        plugins.transcripts = TranscriptsIntegration()


# MCP streamable-HTTP ASGI app. We instantiate it once at import time so it
# can be mounted before lifespan runs; its session manager is started inside
# our own lifespan via Starlette's lifespan_context.
_mcp_streamable_app = mcp_server.streamable_http_app()


@asynccontextmanager
async def lifespan(app: FastAPI):
    _register_plugins()
    app.state.db = await get_db()
    log.info("Hub database ready at %s", config.HUB_DB_PATH)
    poll_task = start_poller(app)

    # Drive the MCP session manager lifespan inside ours so /mcp/* requests
    # have a live transport. Keeps everything in a single uvicorn process.
    mcp_lifespan = _mcp_streamable_app.router.lifespan_context(_mcp_streamable_app)
    try:
        async with mcp_lifespan:
            if config.HUB_TOKENS:
                log.info(
                    "Hub auth ENABLED (%d token(s) configured)",
                    len(config.HUB_TOKENS),
                )
            else:
                log.info(
                    "Hub auth DISABLED (open mode — set OPENCLAW_HUB_TOKENS to enable)"
                )
            yield
    finally:
        poll_task.cancel()
        await app.state.db.close()


app = FastAPI(title="OpenClaw Hub", version="0.2.0", lifespan=lifespan)
app.add_middleware(AuthMiddleware)
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
# MCP transport for remote agents (Cursor, etc.). Bearer token enforced by
# AuthMiddleware — see deploy/TAILSCALE.md for the client-side config.
app.mount("/mcp", _mcp_streamable_app)
app.include_router(web_router)


def _db(request: Request) -> aiosqlite.Connection:
    return request.app.state.db


@app.get("/healthz", response_class=PlainTextResponse)
async def healthz() -> str:
    """Liveness probe — always public, used by VPN / load balancer health checks."""
    return "ok"


# ---------------------------------------------------------------------------
# REST API — Tasks
# ---------------------------------------------------------------------------


@app.post("/api/tasks", response_model=TaskView)
async def api_create_task(body: TaskCreate, request: Request):
    return await services.create_task(_db(request), body)


@app.get("/api/tasks", response_model=list[TaskView])
async def api_list_tasks(
    request: Request,
    status: str | None = None,
    task_type: str | None = Query(default=None, alias="type"),
    priority: str | None = None,
    parent_id: int | None = None,
    limit: int = Query(default=50, le=200),
):
    return await services.list_tasks(
        _db(request),
        status=status,
        task_type=task_type,
        priority=priority,
        parent_id=parent_id,
        limit=limit,
    )


@app.get("/api/tasks/{task_id}", response_model=TaskView)
async def api_get_task(task_id: int, request: Request):
    db = _db(request)
    row = await repo.get_task(db, task_id)
    if not row:
        raise HTTPException(404, "task not found")
    updates = await repo.get_task_updates(db, task_id)
    task_view = services.row_to_task(row, updates=updates)
    return await services.enrich_task_view(db, task_view)


# --- Hierarchy endpoints ---


@app.get("/api/tasks/{task_id}/tree", response_model=TaskTreeNode)
async def api_task_tree(task_id: int, request: Request):
    """Get recursive tree of a task and all descendants."""
    db = _db(request)
    tree = await db_module.build_tree(db, task_id)
    if not tree:
        raise HTTPException(404, "task not found")
    return tree


@app.get("/api/tasks/{task_id}/context", response_model=TaskContextView)
async def api_task_context(task_id: int, request: Request):
    """Full developer contract for a task (#41).

    Returns a single envelope covering:
    - legacy navigation: breadcrumb, siblings, children, progress
    - the current task as a fully-hydrated TaskView (structured fields + ACs)
    - a compact readiness summary (score, dor_passed, blocking recommendations)
    - parent_goal: the nearest epic/feature in the hierarchy, so the work is
      grounded in a larger business goal even for deeply-nested subtasks
    - context_text: an LLM-friendly markdown digest of the same data
    """
    db = _db(request)
    row = await repo.get_task(db, task_id)
    if not row:
        raise HTTPException(404, "task not found")
    task = dict(row)

    breadcrumb = await db_module.get_breadcrumb(db, task_id)
    children = await db_module.get_children(db, task_id)
    progress = await db_module.get_progress(db, task_id) if children else None

    siblings: list[dict[str, Any]] = []
    if task.get("parent_id"):
        sib_rows = await repo.get_siblings(db, task["parent_id"], task_id)
        siblings = [dict(r) for r in sib_rows]

    # --- Hydrate the current task with ACs (structured fields already
    # come out of row_to_task after #39).
    task_view = services.row_to_task(row)
    ac_rows = await repo.list_acceptance_criteria(db, task_id)
    task_view.acceptance_criteria = [services.row_to_ac(r) for r in ac_rows]

    # --- Readiness summary. Reuse the same calculator as /readiness so
    # /context and /readiness can never drift.
    readiness_full = await services.get_readiness(db, task_id, explain=False)
    # Required-only list comes straight from ReadinessReport (review I1);
    # do NOT recompute it from dor_checks — the latter contains every
    # check, including those optional for the current work_type.
    missing_required = readiness_full.missing_required
    blocking_recommendations = [
        r for r in readiness_full.recommendations if r.severity == "blocking"
    ][:5]
    readiness_summary = {
        "score": readiness_full.score,
        "dor_passed": readiness_full.dor_passed,
        "missing_required": missing_required,
        "blocking_recommendations": [r.model_dump() for r in blocking_recommendations],
    }

    # --- Parent goal: nearest ancestor of type epic/feature. The
    # breadcrumb already includes the current task as its last element;
    # iterate in reverse, skip self, and stop at the first match.
    parent_goal: dict[str, Any] | None = None
    for node in reversed(breadcrumb[:-1]):  # exclude current task
        if node.get("task_type") in ("epic", "feature"):
            goal_row = await repo.get_task(db, node["id"])
            if goal_row is not None:
                goal_task = dict(goal_row)
                parent_goal = {
                    "id": goal_task["id"],
                    "task_type": goal_task.get("task_type", "task"),
                    "title": goal_task.get("title", ""),
                    "problem_statement": goal_task.get("problem_statement") or "",
                    "business_value": goal_task.get("business_value") or "",
                }
            break

    # --- Human/LLM digest. Structure matters: keep stable section headers
    # so downstream prompts can grep/extract predictably.
    breadcrumb_str = " > ".join(
        f"{c['task_type'].capitalize()}: {c['title']} (#{c['id']})" for c in breadcrumb
    )
    lines = ["## Current Work Context", f"Path: {breadcrumb_str}"]
    lines.append(
        f"Type: {task.get('task_type', 'task')} | Status: {task['status']} "
        f"| Priority: {task.get('priority', 'medium')}"
    )
    if progress:
        lines.append(
            f"Progress: {progress['completed']}/{progress['total']} "
            f"completed ({progress['percent']}%)"
        )
    if siblings:
        sib_strs = [f"{s['title']} (#{s['id']}/{s['status']})" for s in siblings[:5]]
        lines.append(f"Siblings: {', '.join(sib_strs)}")
    if children:
        child_strs = [f"{c['title']} (#{c['id']}/{c['status']})" for c in children[:8]]
        lines.append(f"Children: {', '.join(child_strs)}")
    if parent_goal:
        lines.append(
            f"Parent goal: {parent_goal['task_type'].capitalize()} "
            f"#{parent_goal['id']} — {parent_goal['title']}"
        )
        if parent_goal["problem_statement"]:
            lines.append(f"  Problem: {parent_goal['problem_statement']}")
        if parent_goal["business_value"]:
            lines.append(f"  Value: {parent_goal['business_value']}")
    if task_view.user_story:
        lines.append(f"User story: {task_view.user_story}")
    if task_view.problem_statement:
        lines.append(f"Problem: {task_view.problem_statement}")
    if task_view.business_value:
        lines.append(f"Value: {task_view.business_value}")
    if task_view.scope_in:
        lines.append(f"In-scope: {', '.join(task_view.scope_in)}")
    if task_view.scope_out:
        lines.append(f"Out-of-scope: {', '.join(task_view.scope_out)}")
    if task_view.validation_commands:
        lines.append("Validation: " + " && ".join(task_view.validation_commands))
    if task_view.acceptance_criteria:
        ac_ids = ", ".join(ac.id for ac in task_view.acceptance_criteria)
        lines.append(
            f"Acceptance criteria ({len(task_view.acceptance_criteria)}): {ac_ids}"
        )
    if task_view.risks:
        # Use .value so the digest reads as 'security:high', not
        # 'RiskKind.security:RiskSeverity.high' (review I4).
        risk_brief = ", ".join(
            f"{r.kind.value}:{r.severity.value}" for r in task_view.risks[:5]
        )
        lines.append(f"Risks ({len(task_view.risks)}): {risk_brief}")
    lines.append(
        f"Readiness: score={readiness_summary['score']} "
        f"dor_passed={'yes' if readiness_summary['dor_passed'] else 'no'}"
    )
    if missing_required:
        lines.append(f"  Missing required: {', '.join(missing_required)}")

    return {
        "task_id": task_id,
        "breadcrumb": breadcrumb,
        "siblings": siblings,
        "children": children,
        "progress": progress,
        "context_text": "\n".join(lines),
        "task": task_view,
        "readiness": readiness_summary,
        "parent_goal": parent_goal,
    }


@app.patch("/api/tasks/{task_id}/reorder", response_model=TaskView)
async def api_reorder_task(task_id: int, body: TaskReorder, request: Request):
    return await services.reorder_task(_db(request), task_id, body)


# --- Approve / Reject / Start ---


@app.post("/api/tasks/{task_id}/approve", response_model=TaskView)
async def api_approve_task(
    task_id: int, request: Request, body: TaskApprove | None = None
):
    return await services.approve_task(_db(request), task_id, body)


@app.post("/api/tasks/{task_id}/reject", response_model=TaskView)
async def api_reject_task(
    task_id: int, request: Request, body: TaskReject | None = None
):
    return await services.reject_task(_db(request), task_id, body)


@app.post("/api/tasks/{task_id}/start", response_model=TaskView)
async def api_start_task(task_id: int, request: Request, body: TaskStart | None = None):
    return await services.start_task(_db(request), task_id, body)


# --- Q&A: Question / Answer ---


@app.post("/api/tasks/{task_id}/question", response_model=TaskView)
async def api_task_question(task_id: int, body: TaskQuestion, request: Request):
    return await services.ask_question(_db(request), task_id, body)


@app.post("/api/tasks/{task_id}/answer", response_model=TaskView)
async def api_task_answer(task_id: int, body: TaskAnswer, request: Request):
    return await services.answer_question(_db(request), task_id, body)


# --- Decide (after arbiter) ---


@app.post("/api/tasks/{task_id}/decide", response_model=TaskView)
async def api_decide_task(task_id: int, body: TaskDecide, request: Request):
    return await services.decide_task(_db(request), task_id, body)


@app.post("/api/tasks/{task_id}/force-complete", response_model=TaskView)
async def api_force_complete_task(
    task_id: int, request: Request, body: TaskForceComplete | None = None
):
    """Force-complete a pending_report task after explicit human acceptance."""
    return await services.force_complete_task(_db(request), task_id, body)


# --- Task Updates ---


@app.post("/api/tasks/{task_id}/updates", response_model=TaskUpdateView)
async def api_add_task_update(task_id: int, body: TaskUpdateCreate, request: Request):
    return await services.add_update(_db(request), task_id, body)


@app.get("/api/tasks/{task_id}/updates", response_model=list[TaskUpdateView])
async def api_list_task_updates(task_id: int, request: Request):
    db = _db(request)
    row = await repo.get_task(db, task_id)
    if not row:
        raise HTTPException(404, "task not found")
    updates = await repo.get_task_updates(db, task_id)
    return [TaskUpdateView(**dict(u)) for u in updates]


@app.post("/api/tasks/{task_id}/refresh", response_model=TaskView)
async def api_refresh_task(task_id: int, request: Request):
    return await services.refresh_task(_db(request), task_id)


# ---------------------------------------------------------------------------
# REST API — Structured form, refinement, readiness (Epic #32)
# ---------------------------------------------------------------------------


def _not_found_to_http(exc: TaskNotFoundError) -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))


def _duplicate_to_http(
    exc: DuplicateAcceptanceCriterionError, code: int
) -> HTTPException:
    return HTTPException(code, str(exc))


@app.post("/api/tasks/{task_id}/refine", response_model=TaskView)
async def api_refine_task(task_id: int, body: TaskRefine, request: Request):
    """PATCH-style update of structured fields and (optionally) ACs.

    - Omitted fields are left untouched.
    - Passing ``acceptance_criteria=[]`` deliberately clears the list.
    - On duplicate ac_id within the payload returns 422.
    """
    db = _db(request)
    try:
        await services.refine_task(db, task_id, body)
    except TaskNotFoundError as exc:
        raise _not_found_to_http(exc) from exc
    except DuplicateAcceptanceCriterionError as exc:
        raise _duplicate_to_http(exc, 422) from exc

    row = await repo.get_task(db, task_id)
    updates = await repo.get_task_updates(db, task_id)
    task_view = services.row_to_task(row, updates=updates)
    return await services.enrich_task_view(db, task_view)


@app.get(
    "/api/tasks/{task_id}/acceptance_criteria",
    response_model=list[AcceptanceCriterion],
)
async def api_list_acceptance_criteria(task_id: int, request: Request):
    try:
        return await services.list_acceptance_criteria(_db(request), task_id)
    except TaskNotFoundError as exc:
        raise _not_found_to_http(exc) from exc


@app.post(
    "/api/tasks/{task_id}/acceptance_criteria",
    response_model=AcceptanceCriterion,
    status_code=status.HTTP_201_CREATED,
)
async def api_add_acceptance_criterion(
    task_id: int, body: AcceptanceCriterion, request: Request
):
    """Append one AC. Returns 409 if ``id`` collides with an existing one."""
    try:
        return await services.add_acceptance_criterion(_db(request), task_id, body)
    except TaskNotFoundError as exc:
        raise _not_found_to_http(exc) from exc
    except DuplicateAcceptanceCriterionError as exc:
        raise _duplicate_to_http(exc, status.HTTP_409_CONFLICT) from exc


@app.put(
    "/api/tasks/{task_id}/acceptance_criteria",
    response_model=list[AcceptanceCriterion],
)
async def api_replace_acceptance_criteria(
    task_id: int, body: list[AcceptanceCriterion], request: Request
):
    """Atomic replace of the AC list. Returns 422 on duplicate ids in payload."""
    try:
        return await services.replace_acceptance_criteria(_db(request), task_id, body)
    except TaskNotFoundError as exc:
        raise _not_found_to_http(exc) from exc
    except DuplicateAcceptanceCriterionError as exc:
        raise _duplicate_to_http(exc, 422) from exc


@app.delete(
    "/api/tasks/{task_id}/acceptance_criteria/{ac_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def api_delete_acceptance_criterion(task_id: int, ac_id: str, request: Request):
    try:
        removed = await services.delete_acceptance_criterion(
            _db(request), task_id, ac_id
        )
    except TaskNotFoundError as exc:
        raise _not_found_to_http(exc) from exc
    if not removed:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"acceptance criterion {ac_id!r} not found for task {task_id}",
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/api/tasks/{task_id}/readiness", response_model=ReadinessReport)
async def api_task_readiness(
    task_id: int,
    request: Request,
    explain: bool = Query(default=False),
):
    try:
        return await services.get_readiness(_db(request), task_id, explain=explain)
    except TaskNotFoundError as exc:
        raise _not_found_to_http(exc) from exc


# ---------------------------------------------------------------------------
# Full log endpoints
# ---------------------------------------------------------------------------


@app.get("/api/tasks/{task_id}/log")
async def api_task_log(task_id: int, request: Request, job: str = Query("main")):
    """Return full dispatch log. ?job=main (default) or ?job=review."""
    db = _db(request)
    row = await repo.get_task(db, task_id)
    if not row:
        raise HTTPException(404, "task not found")
    task = dict(row)
    job_id = task.get("review_job_id") if job == "review" else task.get("job_id")
    if not job_id:
        raise HTTPException(404, f"no {job} job_id for this task")
    content = plugins.dispatch.job_log_full(job_id)
    if not content:
        raise HTTPException(404, f"log file not found for job {job_id}")
    return PlainTextResponse(content)


# ---------------------------------------------------------------------------
# Deprecated — Proposal endpoints (backward compatibility)
# ---------------------------------------------------------------------------


@app.post("/api/proposals", response_model=TaskView)
async def api_create_proposal_compat(request: Request):
    """Deprecated: creates a draft task instead of a proposal."""
    raw = await request.json()
    body = TaskCreate(
        title=raw.get("title", ""),
        description=raw.get("description", ""),
        source=TaskSource.agent,
        agent=raw.get("agent", ""),
        rationale=raw.get("rationale", ""),
    )
    return await services.create_task(_db(request), body)


@app.get("/api/proposals", response_model=list[TaskView])
async def api_list_proposals_compat(
    request: Request,
    status: str | None = None,
    limit: int = Query(default=50, le=200),
):
    """Deprecated: lists draft/rejected tasks instead of proposals."""
    db = _db(request)
    status_map = {"pending": "draft", "approved": "open", "rejected": "rejected"}
    mapped = status_map.get(status, status) if status else None
    rows = await repo.list_agent_tasks(db, mapped, limit=limit)
    return [services.row_to_task(r) for r in rows]


@app.post("/api/proposals/{proposal_id}/action", response_model=TaskView)
async def api_proposal_action_compat(proposal_id: int, request: Request):
    """Deprecated: approve/reject via the old proposal action format."""
    raw = await request.json()
    action = raw.get("action", "")
    comment = raw.get("comment", "")
    if action == "approved":
        body = TaskApprove(comment=comment, run=True)
        return await services.approve_task(_db(request), proposal_id, body)
    elif action == "rejected":
        body = TaskReject(comment=comment)
        return await services.reject_task(_db(request), proposal_id, body)
    raise HTTPException(400, f"unknown action: {action}")


# ---------------------------------------------------------------------------
# REST API — Dashboard data
# ---------------------------------------------------------------------------


@app.get("/api/dashboard", response_model=DashboardData)
async def api_dashboard(request: Request):
    return await services.get_dashboard_data(_db(request))


@app.get("/api/activity", response_model=list[ActivityItem])
async def api_activity(request: Request, limit: int = Query(default=30, le=100)):
    return await services.list_activity(_db(request), limit=limit)


@app.get("/api/dispatch/jobs", response_model=list[dict[str, Any]])
async def api_dispatch_jobs(limit: int = Query(default=30, le=100)):
    return plugins.dispatch.list_jobs(limit)


@app.get("/api/transcripts", response_model=list[dict[str, Any]])
async def api_transcripts(limit: int = Query(default=10, le=30)):
    return plugins.transcripts.list_recent_transcripts(limit)


# ---------------------------------------------------------------------------
# Vast.ai instance management
# ---------------------------------------------------------------------------


@app.post("/api/vast/up")
async def api_vast_up():
    return await plugins.vast.vast_up()


@app.get("/api/vast/status")
async def api_vast_status():
    return await plugins.vast.vast_status()


@app.post("/api/vast/down")
async def api_vast_down():
    return await plugins.vast.vast_down()


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main():
    uvicorn.run("hub.app:app", host=config.HUB_HOST, port=config.HUB_PORT, reload=False)


if __name__ == "__main__":
    main()
