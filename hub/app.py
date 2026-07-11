"""OpenClaw Hub — FastAPI application with REST API and web dashboard."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles

from hub import config, services
from hub import db as db_module
from hub import repository as repo
from hub.db import get_db
from hub.integrations.registry import plugins
from hub.workflow_reference import lifecycle_map_lines
from hub.models import (
    BulkChildTasksCreate,
    BulkRefine,
    BulkRefineResult,
    AcceptanceCriterion,
    ActivityItem,
    DashboardData,
    ReadinessReport,
    ReadinessTreeReport,
    BatchApprove,
    BatchApproveResult,
    ReviewBrief,
    TaskAnswer,
    TaskReviewVerdict,
    TaskSubmitReview,
    TaskApprove,
    TaskArchive,
    TaskClaim,
    TaskContextView,
    TaskCreate,
    TaskDecide,
    TaskForceComplete,
    TaskQuestion,
    TaskRefine,
    TaskPairStart,
    TaskReject,
    TaskRelease,
    TaskReorder,
    TaskRisk,
    TaskSource,
    TaskStart,
    TaskTreeNode,
    TaskUnarchive,
    TaskUpdateCreate,
    TaskUpdateView,
    TaskView,
)
from hub.auth import (
    AuthMiddleware,
    current_identity,
    require_agent_caller,
    require_human_or_admin,
    require_permission,
)
from hub.host_security import HostAllowlistMiddleware
from hub.mcp_http_compat import McpStreamableAcceptCompatMiddleware
from hub.mcp_server import mcp as mcp_server
from hub.services.refinement import (
    DuplicateAcceptanceCriterionError,
    TaskNotFoundError,
)
from hub.services.tree_output import (
    TreeOutputOptions,
    apply_tree_limits,
    truncate_text,
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

    if config.VAST_ENABLED and config.VAST_JOB_BIN:
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
    config.validate_network_auth()
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

            # Bootstrap token guard
            if config.HUB_BOOTSTRAP_TOKEN:
                from hub.db import has_active_admin

                if await has_active_admin(app.state.db):
                    log.warning(
                        "SECURITY: OPENCLAW_HUB_BOOTSTRAP_ADMIN_TOKEN is still set "
                        "but an admin already exists. Remove it from the environment "
                        "to prevent unauthorized bootstrap attempts."
                    )
            yield
    finally:
        poll_task.cancel()
        await app.state.db.close()


app = FastAPI(title="OpenClaw Hub", version="0.2.0", lifespan=lifespan)
app.add_middleware(AuthMiddleware)
# After Auth: runs first on the request — fixes MCP clients that omit Accept.
app.add_middleware(McpStreamableAcceptCompatMiddleware)
# Added last so it runs first. Empty allowlist preserves local/dev behavior;
# production deployments can set OPENCLAW_HUB_ALLOWED_HOSTS.
app.add_middleware(HostAllowlistMiddleware, allowed_hosts=config.HUB_ALLOWED_HOSTS)
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
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


@app.post("/api/tasks/{parent_id}/subtasks", response_model=list[TaskView])
async def api_create_subtasks_bulk(
    parent_id: int,
    body: BulkChildTasksCreate,
    request: Request,
):
    """Atomically create multiple child tasks under ``parent_id``."""
    return await services.create_subtasks_bulk(_db(request), parent_id, body)


@app.get("/api/integrations/notes")
async def api_notes_availability():
    """Diagnose the notesforllm link (#251): available | no_binary | no_space | error."""
    return await plugins.notes.availability()


@app.get("/api/tasks", response_model=None)
async def api_list_tasks(
    request: Request,
    status: str | None = None,
    task_type: str | None = Query(default=None, alias="type"),
    priority: str | None = None,
    parent_id: int | None = None,
    human_owner: str | None = None,
    human_reviewer: str | None = None,
    claimed_by: str | None = None,
    mine: str | None = Query(default=None, description="Filter owner OR claim holder"),
    limit: int = Query(default=50, le=200),
    include_archived: bool = Query(default=False, alias="include_archived"),
    after_id: int | None = Query(
        default=None,
        ge=0,
        description="Cursor (#254): 0 starts a paged walk, then pass next_cursor",
    ),
    mode: str = Query(default="full", pattern="^(full|summary)$"),
):
    """List tasks. Plain list without after_id/mode=summary (backward
    compatible); paged/summary calls return {tasks, next_cursor} (#254)."""
    return await services.list_tasks(
        _db(request),
        status=status,
        task_type=task_type,
        priority=priority,
        parent_id=parent_id,
        human_owner=human_owner,
        human_reviewer=human_reviewer,
        claimed_by=claimed_by,
        mine=mine,
        limit=limit,
        include_archived=include_archived,
        after_id=after_id,
        mode=mode,
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


@app.post("/api/tasks/{task_id}/archive", response_model=TaskView)
async def api_archive_task(
    task_id: int,
    request: Request,
    body: TaskArchive | None = None,
    _identity=Depends(require_permission("tasks.archive")),
):
    cascade = body.cascade if body else True
    try:
        return await services.archive_task(_db(request), task_id, cascade=cascade)
    except TaskNotFoundError as exc:
        raise _not_found_to_http(exc) from exc


@app.post("/api/tasks/{task_id}/withdraw", response_model=TaskView)
async def api_withdraw_own_draft(
    task_id: int,
    request: Request,
    identity=Depends(require_agent_caller),
):
    """Agent-only: archive own agent draft without children (narrow withdraw)."""
    try:
        return await services.withdraw_own_draft(
            _db(request),
            task_id,
            caller=identity.username,
        )
    except TaskNotFoundError as exc:
        raise _not_found_to_http(exc) from exc


@app.post("/api/tasks/{task_id}/unarchive", response_model=TaskView)
async def api_unarchive_task(
    task_id: int,
    request: Request,
    body: TaskUnarchive | None = None,
    _identity=Depends(require_permission("tasks.archive")),
):
    cascade = body.cascade if body else True
    try:
        return await services.unarchive_task(_db(request), task_id, cascade=cascade)
    except TaskNotFoundError as exc:
        raise _not_found_to_http(exc) from exc


@app.delete(
    "/api/tasks/{task_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def api_delete_task(
    task_id: int,
    request: Request,
    _identity=Depends(require_permission("tasks.delete")),
):
    try:
        await services.delete_task_tree(_db(request), task_id)
    except TaskNotFoundError as exc:
        raise _not_found_to_http(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- Hierarchy endpoints ---


@app.get("/api/tasks/{task_id}/tree", response_model=TaskTreeNode)
async def api_task_tree(
    task_id: int,
    request: Request,
    response: Response,
    depth: int | None = Query(default=None, ge=0),
    max_nodes: int | None = Query(default=None, ge=1),
    mode: str = Query(default="full", pattern="^(full|summary)$"),
):
    """Get recursive tree of a task and all descendants.

    Without limit parameters the full tree is returned (backward compatible).
    Use ``mode=summary`` or explicit ``depth`` / ``max_nodes`` to cap output size.
    """
    db = _db(request)
    tree = await db_module.build_tree(db, task_id)
    if not tree:
        raise HTTPException(404, "task not found")
    options = TreeOutputOptions(depth=depth, max_nodes=max_nodes, mode=mode)  # type: ignore[arg-type]
    limited, truncated = apply_tree_limits(tree, options)
    if truncated:
        response.headers["X-Hub-Truncated"] = "true"
    return limited


@app.get("/api/tasks/{task_id}/context", response_model=TaskContextView)
async def api_task_context(
    task_id: int,
    request: Request,
    response: Response,
    max_chars: int | None = Query(default=None, ge=1),
    mode: str = Query(default="full", pattern="^(full|summary)$"),
):
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
    if task_view.latest_review:
        lr = task_view.latest_review
        freshness = "current" if lr.is_current else "stale — work resubmitted"
        lines.append(
            f"Latest review: {lr.verdict.value.upper()} "
            f"for submission #{lr.submission_generation} ({freshness})"
        )
        for finding in lr.findings[:10]:
            lines.append(
                f"  {finding.id}. [{finding.severity.value}] {finding.message}"
            )
    lines.append(
        f"Readiness: score={readiness_summary['score']} "
        f"dor_passed={'yes' if readiness_summary['dor_passed'] else 'no'}"
    )
    if missing_required:
        lines.append(f"  Missing required: {', '.join(missing_required)}")
    if mode == "full":
        lines.append("")
        lines.extend(lifecycle_map_lines())

    effective_max_chars = max_chars
    if mode == "summary" and effective_max_chars is None:
        effective_max_chars = 4000

    context_text, char_truncated = truncate_text("\n".join(lines), effective_max_chars)
    if char_truncated:
        response.headers["X-Hub-Truncated"] = "true"

    return {
        "task_id": task_id,
        "breadcrumb": breadcrumb,
        "siblings": siblings,
        "children": children,
        "progress": progress,
        "context_text": context_text,
        "task": task_view,
        "readiness": readiness_summary,
        "parent_goal": parent_goal,
    }


@app.post("/api/tasks/{task_id}/submit-review", response_model=TaskView)
async def api_submit_for_review(
    task_id: int,
    request: Request,
    body: TaskSubmitReview | None = None,
):
    """Submit the current work of a pair task for client-driven review (#307).

    Canonical REST operation behind hub_submit_for_review and the
    ``oc-hub submit-review`` CLI: running pair task → status=review with a
    bumped submission generation.
    """
    return await services.submit_for_review(_db(request), task_id, body)


@app.post("/api/tasks/{task_id}/review-verdict", response_model=TaskView)
async def api_review_verdict(
    task_id: int,
    request: Request,
    body: TaskReviewVerdict,
    identity=Depends(current_identity),
):
    """Record a review verdict for the current submission (#307).

    Separation of duties (#318): the agent principal that implemented the
    task (assigned_agent or claimed_by) may not review it. The check uses
    the AUTHENTICATED identity — the ``agent`` field in the body is
    display-only. Human/admin tokens always pass;
    ``OPENCLAW_REVIEW_SELF_APPROVE=allow`` is the solo-mode opt-out.

    Canonical REST operation behind hub_submit_review and the
    ``oc-hub review-verdict`` CLI. Client-driven review returns the task to
    ``running``; this endpoint never completes a task.
    """
    db = _db(request)
    row = await repo.get_task(db, task_id)
    if row is not None:
        services.ensure_reviewer_independence(
            dict(row),
            is_agent=identity.is_agent,
            principal_id=identity.principal_id,
            username=identity.username,
        )
    return await services.record_review_verdict(db, task_id, body)


@app.get("/api/tasks/{task_id}/review-brief", response_model=ReviewBrief)
async def api_review_brief(task_id: int, request: Request):
    """Everything a reviewer agent needs in one response (#308).

    Bundles acceptance criteria, scope, validation commands, review
    checklist, branch/PR metadata with an advisory diff command, and the
    latest submission context — so review never depends on scraping task
    prose. Works without a GitHub PR: ``pr_number`` is optional metadata.
    """
    db = _db(request)
    row = await repo.get_task(db, task_id)
    if not row:
        raise HTTPException(404, "task not found")
    task_view = services.row_to_task(row)
    ac_rows = await repo.list_acceptance_criteria(db, task_id)

    # Latest submission context: the most recent done report, falling back
    # to the most recent status update when the task has not reported yet.
    latest_submission_summary = ""
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    for kind in ("done", "status"):
        for u in reversed(updates):
            if u.get("kind") == kind:
                latest_submission_summary = u.get("content", "")
                break
        if latest_submission_summary:
            break

    diff_command = ""
    if task_view.branch:
        diff_command = f"git diff develop...{task_view.branch}"

    return ReviewBrief(
        task_id=task_view.id,
        title=task_view.title,
        status=task_view.status,
        description=task_view.description,
        acceptance_criteria=[services.row_to_ac(r) for r in ac_rows],
        scope_in=task_view.scope_in,
        scope_out=task_view.scope_out,
        out_of_scope_for_review=task_view.out_of_scope_for_review,
        review_checklist=task_view.review_checklist,
        validation_commands=task_view.validation_commands,
        constraints=task_view.constraints,
        technical_hints=task_view.technical_hints,
        branch=task_view.branch,
        pr_number=task_view.pr_number,
        diff_command=diff_command,
        review_cycle=task_view.review_cycle,
        submission_generation=task_view.submission_generation,
        latest_submission_summary=latest_submission_summary,
        latest_review=task_view.latest_review,
    )


@app.patch("/api/tasks/{task_id}/reorder", response_model=TaskView)
async def api_reorder_task(task_id: int, body: TaskReorder, request: Request):
    return await services.reorder_task(_db(request), task_id, body)


# --- Approve / Reject / Start ---


@app.post("/api/tasks/batch-approve", response_model=BatchApproveResult)
async def api_batch_approve(
    body: BatchApprove,
    request: Request,
    _identity=Depends(require_human_or_admin),
):
    """Approve many drafts in one human operation with per-task guards (#252).

    Human-only like single approve; ``force`` is intentionally unsupported —
    overrides remain individual, audited actions.
    """
    return await services.batch_approve_tasks(_db(request), body)


@app.post("/api/tasks/{task_id}/approve", response_model=TaskView)
async def api_approve_task(
    task_id: int,
    request: Request,
    body: TaskApprove | None = None,
    _identity=Depends(require_human_or_admin),
):
    return await services.approve_task(_db(request), task_id, body)


@app.post("/api/tasks/{task_id}/reject", response_model=TaskView)
async def api_reject_task(
    task_id: int,
    request: Request,
    body: TaskReject | None = None,
    _identity=Depends(require_human_or_admin),
):
    return await services.reject_task(_db(request), task_id, body)


@app.post("/api/tasks/{task_id}/start", response_model=TaskView)
async def api_start_task(
    task_id: int,
    request: Request,
    body: TaskStart | None = None,
    _identity=Depends(require_human_or_admin),
):
    return await services.start_task(_db(request), task_id, body)


@app.post("/api/tasks/{task_id}/pair-start", response_model=TaskView)
async def api_pair_start_task(
    task_id: int,
    request: Request,
    body: TaskPairStart | None = None,
    identity=Depends(current_identity),
):
    """Start pair mode: running without headless dispatch (human or agent)."""
    return await services.pair_start_task(
        _db(request),
        task_id,
        body,
        caller=identity.username,
        implementer_principal_id=(identity.principal_id if identity.is_agent else None),
    )


@app.post("/api/tasks/{task_id}/claim", response_model=TaskView)
async def api_claim_task(
    task_id: int,
    body: TaskClaim,
    request: Request,
    identity=Depends(current_identity),
):
    """Claim an open task for one Cursor agent/session."""
    if not body.agent.strip():
        body = TaskClaim(agent=identity.username, session_id=body.session_id)
    return await services.claim_task(
        _db(request),
        task_id,
        body,
        implementer_principal_id=(identity.principal_id if identity.is_agent else None),
    )


@app.post("/api/tasks/{task_id}/release", response_model=TaskView)
async def api_release_task(
    task_id: int,
    body: TaskRelease,
    request: Request,
    identity=Depends(current_identity),
):
    """Release a claimed task back to open."""
    if not body.agent.strip():
        body = TaskRelease(agent=identity.username, session_id=body.session_id)
    return await services.release_task(_db(request), task_id, body)


# --- Q&A: Question / Answer ---


@app.post("/api/tasks/{task_id}/question", response_model=TaskView)
async def api_task_question(task_id: int, body: TaskQuestion, request: Request):
    return await services.ask_question(_db(request), task_id, body)


@app.post("/api/tasks/{task_id}/answer", response_model=TaskView)
async def api_task_answer(
    task_id: int,
    body: TaskAnswer,
    request: Request,
    _identity=Depends(require_human_or_admin),
):
    return await services.answer_question(_db(request), task_id, body)


# --- Decide (after arbiter) ---


@app.post("/api/tasks/{task_id}/decide", response_model=TaskView)
async def api_decide_task(
    task_id: int,
    body: TaskDecide,
    request: Request,
    _identity=Depends(require_human_or_admin),
):
    return await services.decide_task(_db(request), task_id, body)


@app.post("/api/tasks/{task_id}/force-complete", response_model=TaskView)
async def api_force_complete_task(
    task_id: int,
    request: Request,
    body: TaskForceComplete | None = None,
    _identity=Depends(require_human_or_admin),
):
    """Force-complete a stuck task (pending_report/claimed/pair-running)."""
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


@app.post("/api/tasks/refine-bulk", response_model=BulkRefineResult)
async def api_refine_tasks_bulk(body: BulkRefine, request: Request):
    """Apply a TaskRefine PATCH to many tasks in one atomic request.

    Either all items land or none do. Returns a per-task audit
    (fields set, AC/risks counts, readiness).
    """
    db = _db(request)
    try:
        return await services.refine_tasks_bulk(db, body)
    except TaskNotFoundError as exc:
        raise _not_found_to_http(exc) from exc
    except DuplicateAcceptanceCriterionError as exc:
        raise _duplicate_to_http(exc, 422) from exc


@app.post(
    "/api/tasks/{task_id}/risks",
    response_model=TaskView,
    status_code=status.HTTP_201_CREATED,
)
async def api_add_risk(task_id: int, body: TaskRisk, request: Request):
    """Atomically append one risk without replacing the existing risk list."""
    db = _db(request)
    try:
        await services.add_risk(db, task_id, body)
    except TaskNotFoundError as exc:
        raise _not_found_to_http(exc) from exc

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
    task_id: int, body: AcceptanceCriterion, request: Request, response: Response
):
    """Append one AC. Idempotent by ``id``: duplicate ac_id returns existing row."""
    try:
        ac, created = await services.add_acceptance_criterion(
            _db(request), task_id, body
        )
    except TaskNotFoundError as exc:
        raise _not_found_to_http(exc) from exc
    except DuplicateAcceptanceCriterionError as exc:
        raise _duplicate_to_http(exc, status.HTTP_409_CONFLICT) from exc
    if not created:
        response.status_code = status.HTTP_200_OK
    return ac


@app.put(
    "/api/tasks/{task_id}/acceptance_criteria",
    response_model=list[AcceptanceCriterion],
)
async def api_replace_acceptance_criteria(
    task_id: int, body: list[AcceptanceCriterion], request: Request
):
    """Atomic replace of the AC list. Returns 422 on duplicate ids in payload."""
    from hub.models import MAX_ACCEPTANCE_CRITERIA

    if len(body) > MAX_ACCEPTANCE_CRITERIA:
        raise HTTPException(
            422,
            f"too many acceptance criteria: {len(body)} exceeds limit of {MAX_ACCEPTANCE_CRITERIA}",
        )
    try:
        return await services.replace_acceptance_criteria(_db(request), task_id, body)
    except TaskNotFoundError as exc:
        raise _not_found_to_http(exc) from exc
    except DuplicateAcceptanceCriterionError as exc:
        raise _duplicate_to_http(exc, 422) from exc


@app.put(
    "/api/tasks/{task_id}/acceptance_criteria/{ac_id}",
    response_model=AcceptanceCriterion,
)
async def api_upsert_acceptance_criterion(
    task_id: int,
    ac_id: str,
    body: AcceptanceCriterion,
    request: Request,
    response: Response,
):
    """Idempotent upsert of one AC by ``ac_id``.

    Re-sending the same payload is a no-op; a changed payload overwrites the
    row instead of returning 409. The body ``id`` must match the path ``ac_id``.
    Returns 201 when a new criterion was created, 200 when one was updated.
    """
    if body.id != ac_id:
        raise HTTPException(422, f"ac id mismatch: path {ac_id!r} != body {body.id!r}")
    try:
        ac, created = await services.upsert_acceptance_criterion(
            _db(request), task_id, body
        )
    except TaskNotFoundError as exc:
        raise _not_found_to_http(exc) from exc
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return ac


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


@app.get(
    "/api/tasks/{task_id}/readiness-tree",
    response_model=ReadinessTreeReport,
)
async def api_task_readiness_tree(
    task_id: int,
    request: Request,
    include_root: bool = Query(default=False),
):
    """DoR rollup for a subtree: which descendants are not ready and why.

    Set ``include_root=true`` to include the queried task itself.
    """
    try:
        return await services.readiness_tree(
            _db(request), task_id, include_root=include_root
        )
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


if config.VAST_ENABLED:

    @app.post("/api/vast/up")
    async def api_vast_up(_identity=Depends(require_human_or_admin)):
        return await plugins.vast.vast_up()

    @app.get("/api/vast/status")
    async def api_vast_status():
        return await plugins.vast.vast_status()

    @app.post("/api/vast/down")
    async def api_vast_down(_identity=Depends(require_human_or_admin)):
        return await plugins.vast.vast_down()


# ---------------------------------------------------------------------------
# REST API — Admin section (Stage 4)
# ---------------------------------------------------------------------------


@app.get("/api/admin/summary")
async def api_admin_summary(
    request: Request, _identity=Depends(require_permission("admin.read"))
):
    from hub.services import admin as admin_svc

    return await admin_svc.admin_summary(_db(request))


@app.get("/api/admin/principals")
async def api_admin_list_principals(
    request: Request,
    kind: str | None = None,
    status: str | None = None,
    limit: int = Query(default=100, le=500),
    _identity=Depends(require_permission("admin.read")),
):
    from hub.services import admin as admin_svc
    from hub.models import PrincipalView

    rows = await admin_svc.list_principals(
        _db(request), kind=kind, status=status, limit=limit
    )
    return [PrincipalView(**r) for r in rows]


@app.post("/api/admin/principals")
async def api_admin_create_principal(
    request: Request,
    _identity=Depends(require_permission("admin.users.write")),
):
    from hub.models import PrincipalCreate, PrincipalView
    from hub.services import admin as admin_svc

    identity = _identity
    body = PrincipalCreate(**(await request.json()))
    p = await admin_svc.create_principal(
        _db(request),
        kind=body.kind.value,
        username=body.username,
        display_name=body.display_name,
        email=body.email,
        notes=body.notes,
        password=body.password,
        role_slug=body.role or None,
        created_by=identity.principal_id,
    )
    await admin_svc.write_audit(
        _db(request),
        actor_id=identity.principal_id,
        action="create_principal",
        target_type="principal",
        target_id=str(p["id"]),
        summary=f"Created {body.kind.value} principal {body.username!r}",
    )
    return PrincipalView(**p)


@app.get("/api/admin/principals/{principal_id}")
async def api_admin_get_principal(
    principal_id: int,
    request: Request,
    _identity=Depends(require_permission("admin.read")),
):
    from hub.models import PrincipalView
    from hub.services import admin as admin_svc

    p = await admin_svc.get_principal(_db(request), principal_id)
    if not p:
        raise HTTPException(404, "principal not found")
    return PrincipalView(**p)


@app.patch("/api/admin/principals/{principal_id}")
async def api_admin_update_principal(
    principal_id: int,
    request: Request,
    _identity=Depends(require_permission("admin.users.write")),
):
    from hub.models import PrincipalUpdate, PrincipalView
    from hub.services import admin as admin_svc

    identity = _identity
    body = PrincipalUpdate(**(await request.json()))
    p = await admin_svc.update_principal(
        _db(request),
        principal_id,
        display_name=body.display_name,
        email=body.email,
        notes=body.notes,
    )
    if not p:
        raise HTTPException(404, "principal not found")
    await admin_svc.write_audit(
        _db(request),
        actor_id=identity.principal_id,
        action="update_principal",
        target_type="principal",
        target_id=str(principal_id),
        summary=f"Updated principal #{principal_id}",
    )
    return PrincipalView(**p)


@app.post("/api/admin/principals/{principal_id}/disable")
async def api_admin_disable_principal(
    principal_id: int,
    request: Request,
    _identity=Depends(require_permission("admin.users.write")),
):
    from hub.models import PrincipalView
    from hub.services import admin as admin_svc
    from hub.services.admin import LastAdminError

    identity = _identity
    try:
        p = await admin_svc.disable_principal(_db(request), principal_id)
    except LastAdminError as e:
        raise HTTPException(409, str(e)) from e
    if not p:
        raise HTTPException(404, "principal not found")
    await admin_svc.write_audit(
        _db(request),
        actor_id=identity.principal_id,
        action="disable_principal",
        target_type="principal",
        target_id=str(principal_id),
        summary=f"Disabled principal #{principal_id}",
    )
    return PrincipalView(**p)


@app.post("/api/admin/principals/{principal_id}/enable")
async def api_admin_enable_principal(
    principal_id: int,
    request: Request,
    _identity=Depends(require_permission("admin.users.write")),
):
    from hub.models import PrincipalView
    from hub.services import admin as admin_svc

    identity = _identity
    p = await admin_svc.enable_principal(_db(request), principal_id)
    if not p:
        raise HTTPException(404, "principal not found")
    await admin_svc.write_audit(
        _db(request),
        actor_id=identity.principal_id,
        action="enable_principal",
        target_type="principal",
        target_id=str(principal_id),
        summary=f"Enabled principal #{principal_id}",
    )
    return PrincipalView(**p)


@app.get("/api/admin/roles")
async def api_admin_list_roles(
    request: Request,
    _identity=Depends(require_permission("admin.read")),
):
    from hub.models import RoleView
    from hub.services import admin as admin_svc

    rows = await admin_svc.list_roles(_db(request))
    return [RoleView(**r) for r in rows]


@app.put("/api/admin/principals/{principal_id}/roles")
async def api_admin_set_roles(
    principal_id: int,
    request: Request,
    _identity=Depends(require_permission("admin.roles.write")),
):
    from hub.models import RolesUpdatePayload
    from hub.services import admin as admin_svc
    from hub.services.admin import LastAdminError

    identity = _identity
    body = RolesUpdatePayload(**(await request.json()))
    try:
        slugs = await admin_svc.set_principal_roles(
            _db(request), principal_id, body.roles, granted_by=identity.principal_id
        )
    except LastAdminError as e:
        raise HTTPException(409, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    await admin_svc.write_audit(
        _db(request),
        actor_id=identity.principal_id,
        action="set_roles",
        target_type="principal",
        target_id=str(principal_id),
        summary=f"Set roles for principal #{principal_id}: {', '.join(slugs)}",
    )
    return {"principal_id": principal_id, "roles": slugs}


@app.get("/api/admin/api-keys")
async def api_admin_list_keys(
    request: Request,
    principal_id: int | None = None,
    limit: int = Query(default=100, le=500),
    _identity=Depends(require_permission("admin.read")),
):
    from hub.models import ApiKeyView
    from hub.services import admin as admin_svc

    rows = await admin_svc.list_api_keys(
        _db(request), principal_id=principal_id, limit=limit
    )
    return [ApiKeyView(**r) for r in rows]


@app.post("/api/admin/principals/{principal_id}/api-keys")
async def api_admin_create_key(
    principal_id: int,
    request: Request,
    _identity=Depends(require_permission("admin.credentials.write")),
):
    from hub.models import ApiKeyCreate, ApiKeyCreated
    from hub.services import admin as admin_svc

    identity = _identity
    body = ApiKeyCreate(**(await request.json()))
    key_data = await admin_svc.create_api_key(
        _db(request),
        principal_id,
        name=body.name,
        expires_days=body.expires_days,
        created_by=identity.principal_id,
    )
    await admin_svc.write_audit(
        _db(request),
        actor_id=identity.principal_id,
        action="create_api_key",
        target_type="api_key",
        target_id=str(key_data["id"]),
        summary=f"Created API key {body.name!r} for principal #{principal_id}",
    )
    return ApiKeyCreated(**key_data)


@app.post("/api/admin/api-keys/{key_id}/revoke")
async def api_admin_revoke_key(
    key_id: int,
    request: Request,
    _identity=Depends(require_permission("admin.credentials.write")),
):
    from hub.services import admin as admin_svc

    identity = _identity
    revoked = await admin_svc.revoke_api_key(_db(request), key_id)
    if not revoked:
        raise HTTPException(404, "key not found or already revoked")
    await admin_svc.write_audit(
        _db(request),
        actor_id=identity.principal_id,
        action="revoke_api_key",
        target_type="api_key",
        target_id=str(key_id),
        summary=f"Revoked API key #{key_id}",
    )
    return {"revoked": True, "key_id": key_id}


@app.post("/api/admin/principals/{principal_id}/password")
async def api_admin_set_password(
    principal_id: int,
    request: Request,
    _identity=Depends(require_permission("admin.credentials.write")),
):
    from hub.models import PasswordSetPayload
    from hub.services import admin as admin_svc

    identity = _identity
    body = PasswordSetPayload(**(await request.json()))
    await admin_svc.set_password(_db(request), principal_id, body.password)
    await admin_svc.write_audit(
        _db(request),
        actor_id=identity.principal_id,
        action="set_password",
        target_type="principal",
        target_id=str(principal_id),
        summary=f"Password set for principal #{principal_id}",
    )
    return {"ok": True}


@app.get("/api/admin/audit")
async def api_admin_audit(
    request: Request,
    limit: int = Query(default=50, le=500),
    offset: int = Query(default=0, ge=0),
    _identity=Depends(require_permission("admin.audit.read")),
):
    from hub.models import AuditEntry
    from hub.services import admin as admin_svc

    rows = await admin_svc.list_audit(_db(request), limit=limit, offset=offset)
    return [AuditEntry(**r) for r in rows]


@app.post("/api/admin/bootstrap")
async def api_admin_bootstrap(request: Request):
    """Bootstrap the first admin. Requires the bootstrap token or open mode."""
    from hub.models import AdminBootstrap, PrincipalView
    from hub.services import admin as admin_svc

    if not _is_open_mode() and not _check_bootstrap_token(request):
        raise HTTPException(
            403, "bootstrap requires OPENCLAW_HUB_BOOTSTRAP_ADMIN_TOKEN or open mode"
        )
    body = AdminBootstrap(**(await request.json()))
    try:
        p = await admin_svc.bootstrap_admin(
            _db(request),
            username=body.username,
            password=body.password,
            display_name=body.display_name,
            email=body.email,
        )
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
    return PrincipalView(**p)


def _is_open_mode() -> bool:
    if config.HUB_AUTH_DISABLED:
        return True
    return not config.HUB_TOKENS


def _check_bootstrap_token(request: Request) -> bool:
    if not config.HUB_BOOTSTRAP_TOKEN:
        return False
    bearer = request.headers.get("Authorization", "")
    if bearer.startswith("Bearer "):
        token = bearer[7:].strip()
        return token == config.HUB_BOOTSTRAP_TOKEN
    return False


# MCP transport for remote agents (Cursor, etc.). FastMCP's streamable HTTP app
# already owns the /mcp route, so mount it at root and keep this mount last so
# Hub REST/Web routes above continue to take precedence.
app.mount("/", _mcp_streamable_app)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main():
    uvicorn.run("hub.app:app", host=config.HUB_HOST, port=config.HUB_PORT, reload=False)


if __name__ == "__main__":
    main()
