"""Task lifecycle transitions: create, approve, reject, start, Q&A, decide, etc."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

import aiosqlite
from fastapi import HTTPException, status

from hub import config
from hub import db as db_module
from hub.actionable_errors import (
    done_report_error_detail,
    hierarchy_error_detail,
    self_review_forbidden_detail,
    withdraw_own_draft_error_detail,
)
from hub import repository as repo
from hub.hub_instance import mutation_activity_detail
from hub.db import log_activity, structured_fields_from_row
from hub.integrations.registry import plugins
from hub.mcp_envelope import enrich_error_payload
from hub.models import (
    ACTIVE_STATUSES,
    BatchApprove,
    BatchApproveResult,
    BatchApproveSkipped,
    BulkChildTasksCreate,
    FINAL_STATUSES,
    FindingScope,
    LatestReview,
    ReviewFinding,
    SelfReviewWarning,
    TaskAnswer,
    TaskApprove,
    TaskClaim,
    TaskBreadcrumb,
    TaskChildSummary,
    TaskCreate,
    TaskDecide,
    TaskForceComplete,
    TaskPairStart,
    TaskProgress,
    TaskProjectRef,
    TaskQuestion,
    TaskRefine,
    TaskReject,
    TaskRelease,
    TaskReorder,
    TaskReviewVerdict,
    TaskSource,
    TaskStart,
    TaskSubmitReview,
    TaskType,
    TaskUpdateCreate,
    TaskUpdateView,
    TaskView,
)
from hub.integrations.git_ops import PairBranchConflictError
from hub.services.orchestration import (
    completion_requires_review,
    detect_branch_stacking,
    dispatch_task,
    prepare_pair_branch,
    review_approved_for_current_submission,
    transition_after_agent_done,
)
from hub.services.refinement import (
    TaskNotFoundError,
    get_readiness,
    get_write_lock,
    list_acceptance_criteria,
)

log = logging.getLogger("hub")

_ROLLUP_PARENT_TYPES = frozenset({"feature", "epic"})


def compute_lifecycle_hint(task: dict[str, Any]) -> str | None:
    """Human-readable explanation for non-obvious lifecycle waits."""
    status = task.get("status")
    if status != "ci_check":
        return None
    if task.get("job_id"):
        return (
            "Headless task in ci_check: background poller watches PR CI and "
            "dispatches review when checks pass."
        )
    branch = task.get("branch") or ""
    pr = task.get("pr_number")
    if not branch:
        return (
            "Pair task skipped ci_check (no branch). If you still see ci_check, "
            "await human decision or force-complete."
        )
    if not pr:
        return (
            "Awaiting CI conveyor: pair task in ci_check without PR yet. "
            "Poller will create PR from branch or escalate to needs_decision "
            "after retries."
        )
    return (
        f"Awaiting CI conveyor: PR #{pr} on branch {branch}. "
        "Poller advances to review when CI passes."
    )


async def maybe_rollup_parent(db: aiosqlite.Connection, child_id: int) -> None:
    """Auto-complete feature/epic when all direct children are completed."""
    row = await repo.get_task(db, child_id)
    if not row:
        return
    parent_id = dict(row).get("parent_id")
    if not parent_id:
        return

    parent_row = await repo.get_task(db, parent_id)
    if not parent_row:
        return
    parent = dict(parent_row)
    if parent.get("task_type") not in _ROLLUP_PARENT_TYPES:
        return
    if parent["status"] in {s.value for s in FINAL_STATUSES}:
        return

    children = await db_module.get_children(db, parent_id)
    if not children:
        return
    if not all(c["status"] == "completed" for c in children):
        return

    if not await repo.transition_status_if(
        db,
        parent_id,
        expected_from=parent["status"],
        new_status="completed",
    ):
        refreshed = await repo.get_task(db, parent_id)
        if refreshed and dict(refreshed)["status"] == "completed":
            await maybe_rollup_parent(db, parent_id)
        return

    await log_activity(
        db,
        "task_completed",
        f"Task #{parent_id} auto-completed: all children done",
    )
    await maybe_rollup_parent(db, parent_id)


async def repair_stale_parent_completions(db: aiosqlite.Connection) -> int:
    """Repair feature/epic rows left open while all children are completed."""
    rows = await db.execute_fetchall(
        "SELECT id FROM tasks WHERE archived=0 AND task_type IN ('feature','epic') "
        "AND status NOT IN ('completed','failed','rejected') "
        "ORDER BY CASE task_type WHEN 'feature' THEN 0 ELSE 1 END, id ASC"
    )
    repaired = 0
    for row in rows:
        parent_id = row["id"]
        parent_row = await repo.get_task(db, parent_id)
        if not parent_row:
            continue
        children = await db_module.get_children(db, parent_id)
        if children and all(c["status"] == "completed" for c in children):
            await repo.update_task(db, parent_id, status="completed")
            repaired += 1
    if repaired:
        await db.commit()
        log.info("Repaired %d stale parent task(s) to completed", repaired)
    return repaired


def _done_report_error(
    task: dict[str, Any],
    *,
    reason: str,
    hint: str,
    required_status: str,
) -> dict[str, Any]:
    return done_report_error_detail(
        task,
        reason=reason,
        hint=hint,
        required_status=required_status,
    )


def _validate_done_report(task: dict[str, Any]) -> None:
    """Raise HTTPException when a done report must not be recorded."""
    status = task["status"]
    if status == "pending_report":
        return
    if status in ("running", "claimed") and not task.get("job_id"):
        return
    if status in {s.value for s in FINAL_STATUSES}:
        raise HTTPException(
            409,
            detail=_done_report_error(
                task,
                reason="task_already_terminal",
                hint="Task is already finished; no further done report is needed.",
                required_status=status,
            ),
        )
    if status == "open" and not task.get("job_id"):
        raise HTTPException(
            400,
            detail=_done_report_error(
                task,
                reason="pair_start_required",
                hint="Call hub_pair_start (or hub_claim_task then pair-start) before hub_report_done.",
                required_status="running",
            ),
        )
    if status == "ci_check":
        raise HTTPException(
            400,
            detail=_done_report_error(
                task,
                reason="awaiting_ci_conveyor",
                hint="Task is in ci_check; wait for poller or use hub_decide_task / human gate.",
                required_status="ci_check",
            ),
        )
    if status == "needs_decision":
        raise HTTPException(
            400,
            detail=_done_report_error(
                task,
                reason="human_decision_required",
                hint="Task awaits hub_decide_task or human Decision Gate.",
                required_status="needs_decision",
            ),
        )
    raise HTTPException(
        400,
        detail=_done_report_error(
            task,
            reason="invalid_status_for_done",
            hint="Start work via hub_pair_start or hub_start_task before reporting done.",
            required_status="running",
        ),
    )


def ensure_reviewer_independence(
    task: dict[str, Any],
    *,
    is_agent: bool,
    principal_id: int | None,
    username: str,
) -> bool:
    """Raise 403 when the caller implemented the task (#318/#320).

    Shared by the REST endpoint and the web review panel so verdict
    independence has exactly one definition. Principal comparison wins;
    the name-based check is the fallback for env tokens and legacy tasks.
    Humans and the solo opt-out (OPENCLAW_REVIEW_SELF_APPROVE=allow) pass.

    Returns True only when the caller IS the implementer and passed solely
    because of the solo opt-out — so the verdict can be audited as
    self-approved (#434). Independent reviewers and humans return False.
    """
    if not is_agent:
        return False
    if not caller_implemented_task(task, principal_id=principal_id, username=username):
        return False
    if config.REVIEW_SELF_APPROVE == "allow":
        return True
    raise HTTPException(403, detail=self_review_forbidden_detail(username))


def caller_implemented_task(
    task: dict[str, Any],
    *,
    principal_id: int | None,
    username: str,
) -> bool:
    """True when the caller is the implementer of the task (#318/#320).

    Single definition of implementer identity, shared by the verdict gate
    and the review-brief warning (#433). Principal comparison wins; the
    name-based check (assigned_agent/claimed_by) is the fallback for env
    tokens and legacy tasks.
    """
    implementer_pid = task.get("implementer_principal_id")
    if (
        implementer_pid is not None
        and principal_id is not None
        and principal_id == implementer_pid
    ):
        return True
    implementers = {
        (task.get("assigned_agent") or "").strip(),
        (task.get("claimed_by") or "").strip(),
    } - {""}
    return username in implementers


def self_review_brief_warning(
    task: dict[str, Any],
    *,
    is_agent: bool,
    principal_id: int | None,
    username: str,
) -> SelfReviewWarning | None:
    """Fail-fast self-review notice for the review brief (#433).

    Mirrors ensure_reviewer_independence but warns instead of raising: the
    implementer may still read the brief for self-checking, yet must know
    BEFORE spending review effort that hub_submit_review will reject the
    verdict. With OPENCLAW_REVIEW_SELF_APPROVE=allow the warning becomes an
    informational solo-mode note. Humans and non-implementers get None.
    """
    if not is_agent:
        return None
    if not caller_implemented_task(task, principal_id=principal_id, username=username):
        return None
    if config.REVIEW_SELF_APPROVE == "allow":
        return SelfReviewWarning(
            reason="solo_mode_self_review",
            message=(
                f"agent '{username}' implemented this task; solo mode permits "
                "self-review"
            ),
            hint=(
                "OPENCLAW_REVIEW_SELF_APPROVE=allow is active: hub_submit_review "
                "will accept your verdict. This note is informational."
            ),
            required_role=None,
        )
    return SelfReviewWarning(
        reason="self_review_forbidden",
        message=(f"agent '{username}' implemented this task and cannot review it"),
        hint=(
            "Stop before running the review: hub_submit_review will reject "
            "your verdict. The Universal Review Gate requires an independent "
            "reviewer — another agent principal or a human token. You may "
            "still use this brief for self-checking. "
            "Solo mode: set OPENCLAW_REVIEW_SELF_APPROVE=allow."
        ),
        required_role="independent_reviewer",
    )


def parse_review_findings(raw: Any) -> list[ReviewFinding]:
    """Decode the review_findings JSON column into models, failing soft.

    Malformed rows return an empty list rather than breaking every task
    view: findings are advisory review data, not lifecycle-critical state.
    """
    if not raw:
        return []
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
        return [ReviewFinding(**f) for f in data]
    except (ValueError, TypeError):
        log.warning("Malformed review_findings JSON ignored: %.80r", raw)
        return []


def latest_review_projection(task: dict[str, Any]) -> LatestReview | None:
    """Build the latest-review projection for status/context (#308)."""
    verdict = task.get("review_verdict")
    if not verdict:
        return None
    verdict_generation = task.get("review_verdict_generation") or 0
    return LatestReview(
        verdict=verdict,
        submission_generation=verdict_generation,
        is_current=verdict_generation == (task.get("submission_generation") or 0),
        self_approved=bool(task.get("review_self_approved") or 0),
        findings=parse_review_findings(task.get("review_findings")),
    )


def row_to_task(
    row: aiosqlite.Row,
    updates: list[aiosqlite.Row] | None = None,
) -> TaskView:
    """Convert a DB row to a TaskView Pydantic model."""
    d = dict(row)
    log_tail = None
    if d.get("job_id"):
        log_tail = plugins.dispatch.job_log_tail(d["job_id"], max_lines=20)
    upd_list = [TaskUpdateView(**dict(u)) for u in updates] if updates else None
    # Structured task form fields (Epic #32) are stored on tasks row but
    # need JSON-decoding for list columns. structured_fields_from_row
    # returns only the keys present in the row, so absent columns fall
    # back to TaskView defaults — safe for both legacy and new rows.
    structured = structured_fields_from_row(row)
    # Drop None values so model defaults (e.g. "" for str, [] for list)
    # win over a NULL DB column instead of being overwritten with None.
    structured_clean = {k: v for k, v in structured.items() if v is not None}
    return TaskView(
        id=d["id"],
        title=d["title"],
        description=d.get("description", ""),
        status=d["status"],
        task_type=d.get("task_type", "task"),
        parent_id=d.get("parent_id"),
        priority=d.get("priority", "medium"),
        position=d.get("position", 0),
        runtime=d.get("runtime", "auto"),
        source=d.get("source", "human"),
        assigned_agent=d.get("assigned_agent", ""),
        rationale=d.get("rationale", ""),
        human_owner=d.get("human_owner", ""),
        human_reviewer=d.get("human_reviewer", ""),
        job_id=d.get("job_id"),
        exit_code=d.get("exit_code"),
        result_text=d.get("result_text"),
        log_tail=log_tail,
        updates=upd_list,
        review_cycle=d.get("review_cycle", 0),
        ci_fix_cycle=d.get("ci_fix_cycle", 0),
        auto_review=bool(d.get("auto_review", 1)),
        review_job_id=d.get("review_job_id"),
        submission_generation=d.get("submission_generation", 0) or 0,
        review_verdict=d.get("review_verdict"),
        review_verdict_generation=d.get("review_verdict_generation"),
        review_approved_current=review_approved_for_current_submission(d),
        latest_review=latest_review_projection(d),
        branch=d.get("branch"),
        pr_number=d.get("pr_number"),
        claimed_by=d.get("claimed_by"),
        claim_session_id=d.get("claim_session_id"),
        claimed_at=d.get("claimed_at"),
        created_at=d["created_at"],
        updated_at=d["updated_at"],
        archived=bool(d.get("archived", 0)),
        **structured_clean,
    )


async def enrich_task_view(
    db: aiosqlite.Connection,
    task_view: TaskView,
) -> TaskView:
    """Add breadcrumb, children, and progress to a TaskView."""
    if task_view.parent_id is not None:
        crumbs = await db_module.get_breadcrumb(db, task_view.id)
        task_view.breadcrumb = [
            TaskBreadcrumb(id=c["id"], title=c["title"], task_type=c["task_type"])
            for c in crumbs[:-1]
        ]

    children = await db_module.get_children(db, task_view.id)
    if children:
        task_view.children = [
            TaskChildSummary(
                id=c["id"],
                title=c["title"],
                task_type=c["task_type"],
                status=c["status"],
                priority=c.get("priority", "medium"),
            )
            for c in children
        ]
        progress_data = await db_module.get_progress(db, task_view.id)
        task_view.progress = TaskProgress(**progress_data)

    acs = await list_acceptance_criteria(db, task_view.id)
    if acs:
        task_view.acceptance_criteria = acs

    row = await repo.get_task(db, task_view.id)
    if row:
        task_view.lifecycle_hint = compute_lifecycle_hint(dict(row))

    project_row = await repo.resolve_project_for_task(db, task_view.id)
    if project_row is not None:
        task_view.project = TaskProjectRef(
            id=project_row["id"], slug=project_row["slug"]
        )

    return task_view


async def create_task(db: aiosqlite.Connection, body: TaskCreate) -> TaskView:
    """Create a new task, optionally dispatching it immediately."""
    err = await db_module.validate_hierarchy(db, body.task_type.value, body.parent_id)
    if err:
        raise HTTPException(
            400,
            detail=hierarchy_error_detail(
                err,
                task_type=body.task_type.value,
                parent_id=body.parent_id,
            ),
        )

    # Bind an epic to a project at creation (#346). Only epics carry
    # project_id — children resolve it by walking up to the root epic.
    project_id: int | None = None
    if body.project:
        if body.task_type != TaskType.epic:
            raise HTTPException(
                422, "project can only be set on epics; children inherit it"
            )
        project_row = await repo.get_project_by_slug(db, body.project)
        if project_row is None:
            raise HTTPException(422, f"unknown project slug: {body.project!r}")
        if project_row["archived"] or project_row["status"] != "active":
            raise HTTPException(
                422,
                f"project {body.project!r} is not active "
                "(pending proposals and archived projects cannot take epics)",
            )
        project_id = project_row["id"]

    if body.task_type in (TaskType.epic, TaskType.feature):
        # Agents PROPOSE features/epics as drafts (#323) — the human
        # approval gate owns the decomposition. Human-created ones stay
        # open (the pre-#323 invariant, now scoped to source=human).
        initial_status = "draft" if body.source == TaskSource.agent else "open"
        body.run_immediately = False
        body.auto_review = False
    elif body.source == TaskSource.agent:
        initial_status = "draft"
    elif body.run_immediately:
        initial_status = "running"
    else:
        initial_status = "open"

    if body.task_type == TaskType.subtask and body.auto_review:
        body.auto_review = False

    # Use the structured-aware insert so all fields from TaskCreate
    # (work_type, scope_in/out, user_story, etc.) actually persist.
    # The legacy repo.create_task only knew about the original columns
    # and silently dropped the rest of the payload (#46 / review C1).
    task_id = await repo.create_task_full(db, body, status=initial_status)
    if project_id is not None:
        await repo.update_task(db, task_id, project_id=project_id)
    await db.commit()

    result: dict[str, Any] = {}
    if body.run_immediately and body.source != TaskSource.agent:
        row = await repo.get_task(db, task_id)
        result = await dispatch_task(db, task_id, dict(row))  # type: ignore[arg-type]

    await log_activity(
        db,
        "task_created",
        f"{body.task_type.value.capitalize()} #{task_id}: {body.title}",
        json.dumps(result, ensure_ascii=False) if result else None,
    )

    row = await repo.get_task(db, task_id)
    return row_to_task(row)  # type: ignore[arg-type]


async def create_subtasks_bulk(
    db: aiosqlite.Connection,
    parent_id: int,
    body: BulkChildTasksCreate,
) -> list[TaskView]:
    """Create multiple child tasks under ``parent_id`` in one transaction."""
    if body.task_type in (TaskType.epic, TaskType.feature):
        raise HTTPException(
            400,
            "bulk create supports task_type task or subtask only",
        )

    err = await db_module.validate_hierarchy(db, body.task_type.value, parent_id)
    if err:
        raise HTTPException(
            400,
            detail=hierarchy_error_detail(
                err,
                task_type=body.task_type.value,
                parent_id=parent_id,
            ),
        )

    if await repo.get_task(db, parent_id) is None:
        raise HTTPException(404, "parent task not found")

    if body.source == TaskSource.agent:
        initial_status = "draft"
    else:
        initial_status = "open"

    auto_review = body.auto_review
    if body.task_type == TaskType.subtask:
        auto_review = False

    created_ids: list[int] = []
    async with get_write_lock(db):
        await db.execute("SAVEPOINT bulk_child_tasks")
        try:
            for idx, item in enumerate(body.items):
                payload = TaskCreate(
                    title=item.title,
                    description=item.description,
                    task_type=body.task_type,
                    parent_id=parent_id,
                    priority=item.priority,
                    source=body.source,
                    agent=body.agent,
                    auto_review=auto_review,
                    run_immediately=False,
                )
                task_id = await repo.create_task_full(
                    db,
                    payload,
                    status=initial_status,
                    position=idx,
                )
                if item.risks is not None:
                    await repo.update_task_structured(
                        db, task_id, TaskRefine(risks=item.risks)
                    )
                if item.acceptance_criteria is not None:
                    await repo.replace_acceptance_criteria(
                        db, task_id, item.acceptance_criteria
                    )
                created_ids.append(task_id)
        except Exception:
            await db.execute("ROLLBACK TO SAVEPOINT bulk_child_tasks")
            await db.execute("RELEASE SAVEPOINT bulk_child_tasks")
            raise
        else:
            await db.execute("RELEASE SAVEPOINT bulk_child_tasks")
            await db.commit()

    titles = ", ".join(f"#{tid}" for tid in created_ids)
    await log_activity(
        db,
        "tasks_bulk_created",
        f"{body.task_type.value}: {len(created_ids)} under #{parent_id} ({titles})",
    )

    views: list[TaskView] = []
    for task_id in created_ids:
        row = await repo.get_task(db, task_id)
        views.append(row_to_task(row))  # type: ignore[arg-type]
    return views


async def approve_task(
    db: aiosqlite.Connection,
    task_id: int,
    body: TaskApprove | None = None,
) -> TaskView:
    """Approve a draft task, optionally dispatching it.

    The approval is gated by the Definition of Ready (#36). A task with
    unsatisfied required checks cannot be approved unless ``body.force``
    is explicitly set — force-approvals are allowed for genuine edge
    cases (production incidents, exploratory drafts) but are logged as
    ``alert`` updates and tagged in the activity log so the audit trail
    stays intact.
    """
    row = await repo.get_task(db, task_id)
    if not row:
        raise HTTPException(404, "task not found")
    task = dict(row)
    if task["status"] != "draft":
        raise HTTPException(
            400,
            f"can only approve draft tasks, current status: {task['status']}",
        )

    body = body or TaskApprove()

    # --- DoR gate -----------------------------------------------------------
    # Import locally to avoid a circular dependency (services.recommendations
    # imports from .readiness which imports from .dor; lifecycle is called
    # from many modules and should stay lightweight at import time).
    from hub.services.recommendations import (
        calculate_readiness_with_recommendations,
    )

    readiness = await calculate_readiness_with_recommendations(db, task_id)
    dor_override_summary: str | None = None
    if not readiness.dor_passed:
        # Use the report's required-only list. Filtering dor_checks ourselves
        # would mistakenly include checks that failed but aren't required
        # for this work_type (review I1).
        missing = readiness.missing_required
        if not body.force:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "dor_failed",
                    "task_id": task_id,
                    "score": readiness.score,
                    "missing_required": missing,
                    "recommendations": [
                        {
                            "field": r.field,
                            "severity": r.severity,
                            "message": r.message,
                            "expected_score_delta": r.expected_score_delta,
                        }
                        for r in readiness.recommendations
                    ],
                    "hint": "pass force=true to override the DoR gate",
                },
            )
        dor_override_summary = ", ".join(missing) or "<unknown>"
        log.warning(
            "DoR override on approve for task #%s (missing: %s)",
            task_id,
            dor_override_summary,
        )
        override_message = (
            f"Approve override: DoR failed (missing: {dor_override_summary}); "
            f"approved with force=true"
        )
        if body.comment:
            override_message += f". Comment: {body.comment}"
        await repo.add_task_update(db, task_id, "", "alert", override_message)
    elif body.force:
        # DoR passed but the caller still set force=true. Record the
        # explicit human override so post-mortems can spot "we forced
        # this even though we didn't strictly need to" — review I7.
        force_message = (
            "Approve override: force=true requested (DoR was already passing)"
        )
        if body.comment:
            force_message += f". Comment: {body.comment}"
        await repo.add_task_update(db, task_id, "", "alert", force_message)

    if body.comment and dor_override_summary is None and not body.force:
        await repo.add_task_update(
            db, task_id, "", "status", f"Approved: {body.comment}"
        )

    if body.runtime:
        await repo.update_task(db, task_id, runtime=body.runtime.value)
        task["runtime"] = body.runtime.value

    # Atomic conditional transition: a concurrent second approve will see
    # ``rowcount == 0`` and get a 409 instead of being silently double-
    # processed. Even though aiosqlite serializes a shared connection
    # today, this guards us when someone moves to a per-request connection
    # or a pool. Review I5.
    transitioned = await repo.transition_status_if(
        db, task_id, expected_from="draft", new_status="open"
    )
    if transitioned:
        await repo.insert_event(
            db,
            kind="task_approved",
            task_id=task_id,
            actor="human",
            payload={"run": bool(body.run), "force": bool(body.force)},
        )
    await db.commit()
    if not transitioned:
        raise HTTPException(409, "task is no longer draft (concurrent approve?)")

    if body.run:
        task["status"] = "open"
        await dispatch_task(db, task_id, task)

    activity_suffix = ""
    if body.run:
        activity_suffix = f" (run={body.run})"
    if dor_override_summary is not None:
        activity_suffix += f" (force=true, missing={dor_override_summary})"
    elif body.force:
        activity_suffix += " (force=true)"
    await log_activity(
        db,
        "task_approved",
        f"Task #{task_id} approved{activity_suffix}",
        detail=mutation_activity_detail(),
    )

    row = await repo.get_task(db, task_id)
    updates = await repo.get_task_updates(db, task_id)
    return row_to_task(row, updates=updates)  # type: ignore[arg-type]


async def batch_approve_tasks(
    db: aiosqlite.Connection,
    body: BatchApprove,
) -> BatchApproveResult:
    """Approve many drafts with per-task guards and partial success (#252).

    Каждая задача проверяется независимо: не-draft, непройденный DoR,
    низкий readiness или high-риски дают skipped с причиной, не ломая
    остальную пачку. force не поддерживается намеренно — override
    остаётся одиночным, аудируемым действием.
    """
    result = BatchApproveResult()
    for task_id in body.task_ids:
        row = await repo.get_task(db, task_id)
        if row is None:
            result.skipped.append(
                BatchApproveSkipped(task_id=task_id, reason="not_found")
            )
            continue
        task = dict(row)
        if task["status"] != "draft":
            result.skipped.append(
                BatchApproveSkipped(
                    task_id=task_id,
                    reason=f"not_draft:{task['status']}",
                )
            )
            continue

        dor_passed = task.get("dor_passed")
        score = task.get("readiness_score")
        if dor_passed is None or score is None:
            # Legacy row without persisted readiness (#250): compute lazily.
            report = await get_readiness(db, task_id)
            dor_passed = report.dor_passed
            score = report.score
        if body.require_dor_passed and not dor_passed:
            result.skipped.append(
                BatchApproveSkipped(task_id=task_id, reason="dor_failed")
            )
            continue
        if body.min_readiness is not None and (score or 0) < body.min_readiness:
            result.skipped.append(
                BatchApproveSkipped(
                    task_id=task_id,
                    reason=f"readiness_below_{body.min_readiness}",
                )
            )
            continue
        if body.exclude_high_risks:
            risks = db_module.deserialize_risks(task.get("risks"))
            if any(r.get("severity") == "high" for r in risks):
                result.skipped.append(
                    BatchApproveSkipped(task_id=task_id, reason="high_risk")
                )
                continue

        try:
            await approve_task(db, task_id, TaskApprove(comment=body.comment))
        except HTTPException as exc:
            reason = "approve_failed"
            detail = exc.detail
            if isinstance(detail, dict):
                reason = detail.get("reason") or detail.get("error") or reason
            result.skipped.append(
                BatchApproveSkipped(task_id=task_id, reason=f"{reason}")
            )
            continue
        result.approved.append(task_id)
    return result


async def reject_task(
    db: aiosqlite.Connection,
    task_id: int,
    body: TaskReject | None = None,
) -> TaskView:
    """Reject a draft task."""
    row = await repo.get_task(db, task_id)
    if not row:
        raise HTTPException(404, "task not found")
    task = dict(row)
    if task["status"] != "draft":
        raise HTTPException(
            400,
            f"can only reject draft tasks, current status: {task['status']}",
        )

    body = body or TaskReject()
    if body.comment:
        await repo.add_task_update(
            db, task_id, "", "status", f"Rejected: {body.comment}"
        )

    await repo.update_task(db, task_id, status="rejected")
    await repo.insert_event(db, kind="task_rejected", task_id=task_id, actor="human")
    await db.commit()
    await log_activity(
        db,
        "task_rejected",
        f"Task #{task_id} rejected",
        detail=mutation_activity_detail(),
    )

    row = await repo.get_task(db, task_id)
    return row_to_task(row)  # type: ignore[arg-type]


async def start_task(
    db: aiosqlite.Connection,
    task_id: int,
    body: TaskStart | None = None,
) -> TaskView:
    """Start an open task by dispatching it."""
    row = await repo.get_task(db, task_id)
    if not row:
        raise HTTPException(404, "task not found")
    task = dict(row)
    if task["status"] != "open":
        raise HTTPException(
            400,
            f"can only start open tasks, current status: {task['status']}",
        )

    body = body or TaskStart()

    if body.plan:
        await repo.add_task_update(
            db,
            task_id,
            task.get("assigned_agent", ""),
            "status",
            f"Plan: {body.plan}",
        )

    if not await repo.has_plan_updates(db, task_id):
        raise HTTPException(
            400,
            "Plan required before starting a task. "
            "Either pass 'plan' field in start request or create an update "
            "with kind='status' and content starting with 'Plan:'.",
        )

    if body.runtime:
        await repo.update_task(db, task_id, runtime=body.runtime.value)
        task["runtime"] = body.runtime.value

    await dispatch_task(db, task_id, task)
    await log_activity(
        db,
        "task_started",
        f"Task #{task_id} dispatched",
        detail=mutation_activity_detail(),
    )

    row = await repo.get_task(db, task_id)
    updates = await repo.get_task_updates(db, task_id)
    return row_to_task(row, updates=updates)  # type: ignore[arg-type]


async def pair_start_task(
    db: aiosqlite.Connection,
    task_id: int,
    body: TaskPairStart | None = None,
    *,
    caller: str = "",
    implementer_principal_id: int | None = None,
) -> TaskView:
    """Start an open task in pair mode: running without headless dispatch.

    ``implementer_principal_id`` records WHO implements as an authenticated
    principal (#320) so the self-review ban can compare identities instead
    of free-text agent names. None (env tokens, anonymous, humans) keeps
    the name-based fallback of #318.
    """
    row = await repo.get_task(db, task_id)
    if not row:
        raise HTTPException(404, "task not found")
    task = dict(row)
    assigned_agent = (body.assigned_agent or "").strip() if body else ""
    if not assigned_agent:
        assigned_agent = (caller or "").strip() or task.get("assigned_agent", "")

    if task["status"] == "claimed":
        holder = (task.get("claimed_by") or "").strip()
        if holder and assigned_agent and holder != assigned_agent:
            raise HTTPException(
                409,
                f"Task #{task_id} is claimed by {holder}; pair-start denied",
            )
    elif task["status"] != "open":
        raise HTTPException(
            400,
            f"can only pair-start open or own-claimed tasks, current status: {task['status']}",
        )

    body = body or TaskPairStart()

    if body.plan:
        await repo.add_task_update(
            db,
            task_id,
            task.get("assigned_agent", ""),
            "status",
            f"Plan: {body.plan}",
        )

    if not await repo.has_plan_updates(db, task_id):
        raise HTTPException(
            400,
            "Plan required before pair-start. "
            "Either pass 'plan' field in pair-start request or create an update "
            "with kind='status' and content starting with 'Plan:'.",
        )

    try:
        branch = await prepare_pair_branch(
            db, task_id, task, branch_slug=(body.branch_slug or "").strip()
        )
    except PairBranchConflictError as exc:
        raise HTTPException(422, str(exc)) from exc
    if branch:
        task["branch"] = branch

    assigned_agent = (body.assigned_agent or "").strip()
    if not assigned_agent:
        assigned_agent = (caller or "").strip() or task.get("assigned_agent", "")

    update_fields: dict[str, Any] = {
        "status": "running",
        "job_id": None,
        "assigned_agent": assigned_agent,
    }
    if implementer_principal_id is not None:
        update_fields["implementer_principal_id"] = implementer_principal_id
    if branch:
        update_fields["branch"] = branch

    await repo.update_task(db, task_id, **update_fields)
    await db.commit()
    await log_activity(
        db,
        "task_pair_started",
        f"Task #{task_id} pair session started",
        detail=mutation_activity_detail(),
    )

    row = await repo.get_task(db, task_id)
    updates = await repo.get_task_updates(db, task_id)
    return row_to_task(row, updates=updates)  # type: ignore[arg-type]


async def submit_for_review(
    db: aiosqlite.Connection,
    task_id: int,
    body: TaskSubmitReview | None = None,
) -> TaskView:
    """Submit the current work of a pair task for client-driven review (#305).

    Valid only from pair ``running`` (no ``job_id``): headless tasks are
    submitted by their done report and reviewed by the poller conveyor.
    Bumps the submission generation — which invalidates any APPROVED verdict
    recorded for earlier work — and moves the task into ``status=review``
    with no ``review_job_id``, marking the review as client-driven.
    """
    row = await repo.get_task(db, task_id)
    if not row:
        raise HTTPException(404, "task not found")
    task = dict(row)
    body = body or TaskSubmitReview()

    if task["status"] != "running" or task.get("job_id"):
        if task.get("job_id"):
            raise HTTPException(
                400,
                "headless tasks are submitted for review by their done report; "
                "submit-for-review is only for pair tasks without a dispatch job",
            )
        raise HTTPException(
            400,
            f"can only submit running pair tasks for review, "
            f"current status: {task['status']}",
        )

    async with get_write_lock(db):
        if not await repo.transition_status_if(
            db, task_id, expected_from="running", new_status="review"
        ):
            raise HTTPException(
                409,
                f"Task #{task_id} left running state during submit; retry from "
                "its current status",
            )
        generation = await repo.bump_submission_generation(db, task_id)
        # Client-driven review: no dispatch job. A stale review_job_id from a
        # previous headless cycle would make the poller treat this task as its
        # own, so clear it explicitly.
        await repo.update_task(db, task_id, review_job_id=None)
        agent = (body.agent or "").strip() or task.get("assigned_agent", "")
        summary = (body.summary or "").strip()
        content = f"Submitted for review (submission #{generation})."
        if summary:
            content += f" {summary}"
        await repo.add_task_update(db, task_id, agent, "status", content)
        await db.commit()
        await log_activity(
            db,
            "task_submitted_for_review",
            f"Task #{task_id} submitted for review (generation {generation})",
            detail=mutation_activity_detail(),
        )

    # Advisory branch-stacking detection (#438): warn — never block — when
    # this branch carries commits of another unmerged task branch. A stack
    # can be a deliberate decision, so the finding is an alert update plus
    # a response hint, not a failed submission.
    stacking = await detect_branch_stacking(db, task_id, task.get("branch") or "")
    if stacking:
        await repo.add_task_update(db, task_id, "hub", "alert", stacking["message"])
        await db.commit()

    row = await repo.get_task(db, task_id)
    updates = await repo.get_task_updates(db, task_id)
    view = row_to_task(row, updates=updates)  # type: ignore[arg-type]

    # Machine-review policy (#382): tell the submitting agent right away
    # when the harness run is expected before the human verdict.
    from hub.services.orchestration import machine_review_gap

    gap = await machine_review_gap(db, dict(row))
    if gap:
        view.lifecycle_hint = (
            f"Machine-review требуется ({gap}): hub_get_skill('multi-agent-review') "
            "→ прогон → hub_submit_machine_review — до человеческого вердикта."
        )
    if stacking:
        view.lifecycle_hint = (
            f"{view.lifecycle_hint}\n{stacking['message']}"
            if view.lifecycle_hint
            else stacking["message"]
        )
    return view


async def record_review_verdict(
    db: aiosqlite.Connection,
    task_id: int,
    body: TaskReviewVerdict,
    *,
    self_approved: bool = False,
) -> TaskView:
    """Record an explicit review verdict for the current submission (#305).

    Persists the verdict bound to the current submission generation, so a
    later resubmission automatically invalidates an APPROVED verdict. For
    client-driven review (status=review, no review_job_id) the task returns
    to ``running`` so the developer can fix findings or report done (#307);
    headless transitions remain with the poller. Never a completion path.

    Finding scope (#435): a ``changes_requested`` verdict with findings must
    include at least one ``in_scope`` finding — if everything is out of
    scope there is nothing to fix in this task, so the verdict should be
    ``approved`` with the out-of-scope findings kept as recommendations
    (incident #392: the source task hung in review while every finding went
    to parallel tasks). Out-of-scope findings without ``linked_task_id``
    produce a non-blocking warning in the review update.

    ``self_approved=True`` (the ensure_reviewer_independence solo opt-out
    result) marks the verdict as non-independent: the flag is persisted on
    the task row, echoed in the task update, and logged as a warning so a
    weakened Review Gate stays visible in hindsight (#434).
    """
    row = await repo.get_task(db, task_id)
    if not row:
        raise HTTPException(404, "task not found")
    task = dict(row)

    if (task.get("submission_generation") or 0) == 0:
        raise HTTPException(
            400,
            "no submission to review yet: the task has never been submitted for review",
        )

    if body.verdict.value == "changes_requested" and body.findings:
        if all(f.scope == FindingScope.out_of_scope for f in body.findings):
            raise HTTPException(
                422,
                detail=enrich_error_payload(
                    {
                        "reason": "changes_requested_requires_in_scope_finding",
                        "message": (
                            "changes_requested requires at least one in_scope "
                            "finding; all findings are out_of_scope"
                        ),
                        "hint": (
                            "If nothing needs fixing in this task, submit "
                            "verdict=approved and keep out-of-scope findings "
                            "as recommendations (linked to follow-up tasks "
                            "via linked_task_id)."
                        ),
                        "suggested_tool": "hub_submit_review",
                    }
                ),
            )

    # Machine-review hard gate (#382): only in OPENCLAW_MACHINE_REVIEW=require,
    # and only for APPROVED — the reviewer must always be able to reject work
    # (changes_requested), harness or no harness. Default 'warn' keeps every
    # verdict available; the panel shows the gap.
    if config.MACHINE_REVIEW_MODE == "require" and body.verdict.value == "approved":
        from hub.services.orchestration import machine_review_gap

        gap = await machine_review_gap(db, task)
        if gap:
            raise HTTPException(
                422,
                f"machine-review обязателен для аппрува этой задачи: {gap}",
            )

    async with get_write_lock(db):
        findings_json = json.dumps(
            [f.model_dump(exclude_none=True) for f in body.findings],
            ensure_ascii=False,
        )
        await repo.record_review_verdict(
            db,
            task_id,
            body.verdict.value,
            findings_json=findings_json,
            self_approved=self_approved,
        )
        agent = (body.agent or "").strip() or "reviewer"
        content = f"Review verdict: {body.verdict.value.upper()}"
        if self_approved:
            content += " [self-approved: solo mode, OPENCLAW_REVIEW_SELF_APPROVE=allow]"
            log.warning(
                "Task #%s: review verdict %s accepted via "
                "OPENCLAW_REVIEW_SELF_APPROVE=allow — reviewer '%s' "
                "implemented this task (no independent review)",
                task_id,
                body.verdict.value,
                agent,
            )
        if body.findings:
            # Human-readable echo only; the canonical structured findings
            # live on the task row, so the update text can stay compact.
            for f in body.findings[:20]:
                place = (
                    f" ({f.file}:{f.line})"
                    if f.file and f.line
                    else (f" ({f.file})" if f.file else "")
                )
                scope_mark = ""
                if f.scope == FindingScope.out_of_scope:
                    scope_mark = (
                        f" [out-of-scope → #{f.linked_task_id}]"
                        if f.linked_task_id
                        else " [out-of-scope]"
                    )
                content += (
                    f"\n{f.id}. [{f.severity.value}]{place}{scope_mark} {f.message}"
                )
            if len(body.findings) > 20:
                content += f"\n… and {len(body.findings) - 20} more findings"
            unlinked = [
                f.id
                for f in body.findings
                if f.scope == FindingScope.out_of_scope and not f.linked_task_id
            ]
            if unlinked:
                ids = ", ".join(str(i) for i in unlinked)
                content += (
                    f"\nWarning: out-of-scope finding(s) {ids} have no "
                    "linked_task_id — create follow-up task(s) and link them."
                )
        if body.comments.strip():
            content += f"\n{body.comments.strip()}"
        await repo.add_task_update(db, task_id, agent, "review", content)

        # Client-driven review only (status=review, no review_job_id): hand
        # the task back to the developer so the loop continues — fix findings
        # on CHANGES_REQUESTED, or report done on APPROVED. Headless review
        # transitions stay with the poller, which owns tasks that carry a
        # review_job_id (#307).
        if task["status"] == "review" and not task.get("review_job_id"):
            await repo.transition_status_if(
                db, task_id, expected_from="review", new_status="running"
            )
            if body.verdict.value == "changes_requested":
                await repo.update_task(
                    db, task_id, review_cycle=(task.get("review_cycle") or 0) + 1
                )

        await repo.insert_event(
            db,
            kind="review_verdict_recorded",
            task_id=task_id,
            actor=agent,
            payload={
                "verdict": body.verdict.value,
                "submission_generation": task.get("submission_generation") or 0,
                "self_approved": self_approved,
            },
        )
        await db.commit()
        await log_activity(
            db,
            "task_review_verdict",
            f"Task #{task_id} review verdict: {body.verdict.value}",
            detail=mutation_activity_detail(),
        )

    row = await repo.get_task(db, task_id)
    updates = await repo.get_task_updates(db, task_id)
    return row_to_task(row, updates=updates)  # type: ignore[arg-type]


async def claim_task(
    db: aiosqlite.Connection,
    task_id: int,
    body: TaskClaim,
    *,
    implementer_principal_id: int | None = None,
) -> TaskView:
    """Claim an open task for a single Cursor agent/session.

    ``implementer_principal_id`` records the claiming agent's authenticated
    principal (#320) for the identity-based self-review ban.
    """
    row = await repo.get_task(db, task_id)
    if not row:
        raise HTTPException(404, "task not found")
    task = dict(row)

    if task["status"] == "claimed":
        if (
            task.get("claimed_by") == body.agent
            and (task.get("claim_session_id") or "") == body.session_id
        ):
            row = await repo.get_task(db, task_id)
            updates = await repo.get_task_updates(db, task_id)
            return row_to_task(row, updates=updates)  # type: ignore[arg-type]
        holder = task.get("claimed_by") or "unknown"
        raise HTTPException(
            409,
            f"Task #{task_id} already claimed by {holder}",
        )

    if task["status"] != "open":
        raise HTTPException(
            400,
            f"can only claim open tasks, current status: {task['status']}",
        )

    if not await repo.transition_status_if(
        db, task_id, expected_from="open", new_status="claimed"
    ):
        row = await repo.get_task(db, task_id)
        task = dict(row)
        if task["status"] == "claimed" and task.get("claimed_by") == body.agent:
            updates = await repo.get_task_updates(db, task_id)
            return row_to_task(row, updates=updates)  # type: ignore[arg-type]
        raise HTTPException(409, f"Task #{task_id} claim conflict")

    session_note = f" session={body.session_id}" if body.session_id else ""
    claim_fields: dict[str, Any] = {
        "claimed_by": body.agent,
        "claim_session_id": body.session_id or None,
        "claimed_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "assigned_agent": body.agent,
    }
    if implementer_principal_id is not None:
        claim_fields["implementer_principal_id"] = implementer_principal_id
    await repo.update_task(db, task_id, **claim_fields)
    await repo.add_task_update(
        db,
        task_id,
        body.agent,
        "status",
        f"Claimed by {body.agent}{session_note}",
    )
    await db.commit()
    await log_activity(
        db,
        "task_claimed",
        f"Task #{task_id} claimed by {body.agent}",
        detail=mutation_activity_detail(),
    )

    row = await repo.get_task(db, task_id)
    updates = await repo.get_task_updates(db, task_id)
    return row_to_task(row, updates=updates)  # type: ignore[arg-type]


async def release_task(
    db: aiosqlite.Connection,
    task_id: int,
    body: TaskRelease,
) -> TaskView:
    """Release an active claim and return the task to open."""
    row = await repo.get_task(db, task_id)
    if not row:
        raise HTTPException(404, "task not found")
    task = dict(row)

    if task["status"] != "claimed":
        raise HTTPException(
            400,
            f"can only release claimed tasks, current status: {task['status']}",
        )

    holder = task.get("claimed_by") or ""
    if holder != body.agent:
        raise HTTPException(
            409,
            f"Task #{task_id} is claimed by {holder}, not {body.agent}",
        )
    if body.session_id and (task.get("claim_session_id") or "") != body.session_id:
        raise HTTPException(
            409,
            f"Task #{task_id} claim session mismatch",
        )

    if not await repo.transition_status_if(
        db, task_id, expected_from="claimed", new_status="open"
    ):
        raise HTTPException(409, f"Task #{task_id} release conflict")

    await repo.update_task(
        db,
        task_id,
        claimed_by=None,
        claim_session_id=None,
        claimed_at=None,
        # The claim is the implementer's reservation: releasing it also
        # releases the recorded implementer identity (#320).
        implementer_principal_id=None,
    )
    await repo.add_task_update(
        db,
        task_id,
        body.agent,
        "status",
        f"Claim released by {body.agent}",
    )
    await db.commit()
    await log_activity(
        db,
        "task_released",
        f"Task #{task_id} claim released",
        detail=mutation_activity_detail(),
    )

    row = await repo.get_task(db, task_id)
    updates = await repo.get_task_updates(db, task_id)
    return row_to_task(row, updates=updates)  # type: ignore[arg-type]


def _can_ask_question(task: dict) -> bool:
    """Pair agents may ask from ``open`` (pre pair-start) or ``running``."""
    status = task["status"]
    if status == "running":
        return True
    if status == "open" and not task.get("job_id"):
        return True
    if status == "claimed" and not task.get("job_id"):
        return True
    return False


async def ask_question(
    db: aiosqlite.Connection,
    task_id: int,
    body: TaskQuestion,
) -> TaskView:
    """Record an agent question and move task to needs_info."""
    row = await repo.get_task(db, task_id)
    if not row:
        raise HTTPException(404, "task not found")
    task = dict(row)
    if not _can_ask_question(task):
        raise HTTPException(
            400,
            "can only ask questions on running tasks or open pair tasks "
            f"(no job_id), current status: {task['status']}",
        )

    await repo.add_task_update(db, task_id, body.agent, "question", body.question)
    await repo.update_task(db, task_id, status="needs_info")
    await db.commit()
    await log_activity(
        db,
        "task_question",
        f"Task #{task_id}: agent asked a question",
        detail=mutation_activity_detail(),
    )

    row = await repo.get_task(db, task_id)
    updates = await repo.get_task_updates(db, task_id)
    return row_to_task(row, updates=updates)  # type: ignore[arg-type]


def _is_pair_task(task: dict) -> bool:
    """Pair tasks have no headless dispatch ``job_id``."""
    return not task.get("job_id")


def _pair_resume_status(task: dict) -> str:
    """After needs_info, return to ``running`` if pair-start ran, else ``open``."""
    if task.get("branch"):
        return "running"
    return "open"


async def answer_question(
    db: aiosqlite.Connection,
    task_id: int,
    body: TaskAnswer,
) -> TaskView:
    """Answer a question and optionally resume the task."""
    row = await repo.get_task(db, task_id)
    if not row:
        raise HTTPException(404, "task not found")
    task = dict(row)
    if task["status"] != "needs_info":
        raise HTTPException(
            400,
            f"can only answer needs_info tasks, current status: {task['status']}",
        )

    await repo.add_task_update(db, task_id, "", "answer", body.answer)
    await repo.insert_event(
        db,
        kind="question_answered",
        task_id=task_id,
        actor="human",
        payload={"resume": bool(body.resume)},
    )
    await db.commit()

    if body.resume:
        if _is_pair_task(task):
            resume_status = _pair_resume_status(task)
            await repo.update_task(db, task_id, status=resume_status, job_id=None)
            await db.commit()
            await log_activity(
                db,
                "task_answered",
                f"Task #{task_id}: answered, pair resumed to {resume_status}",
                detail=mutation_activity_detail(),
            )
        else:
            row = await repo.get_task(db, task_id)
            await dispatch_task(db, task_id, dict(row))  # type: ignore[arg-type]
            await log_activity(
                db,
                "task_answered",
                f"Task #{task_id}: answered and re-dispatched",
                detail=mutation_activity_detail(),
            )
    else:
        await repo.update_task(db, task_id, status="open")
        await db.commit()
        await log_activity(
            db,
            "task_answered",
            f"Task #{task_id}: answered, moved to open",
            detail=mutation_activity_detail(),
        )

    row = await repo.get_task(db, task_id)
    updates = await repo.get_task_updates(db, task_id)
    return row_to_task(row, updates=updates)  # type: ignore[arg-type]


async def decide_task(
    db: aiosqlite.Connection,
    task_id: int,
    body: TaskDecide,
) -> TaskView:
    """Human decision after arbiter review: accept or rework.

    When ``decision_summary`` is provided it is always recorded in the
    task update log regardless of the ``record_decision`` flag, so the
    context is visible even without a notes integration.

    When ``record_decision`` is True the summary is additionally persisted
    through the notes plugin (if configured). If the notes adapter is
    unavailable or returns None the core lifecycle continues unaffected.
    """
    row = await repo.get_task(db, task_id)
    if not row:
        raise HTTPException(404, "task not found")
    task = dict(row)
    if task["status"] != "needs_decision":
        raise HTTPException(
            400,
            f"can only decide on needs_decision tasks, current status: {task['status']}",
        )

    summary_text = body.decision_summary.strip() if body.decision_summary else ""

    if body.action == "accept":
        update_content = "Human accepted task after arbiter review."
        if summary_text:
            update_content += f"\nDecision: {summary_text}"
        await repo.add_task_update(db, task_id, "human", "decision", update_content)
        await repo.update_task(db, task_id, status="completed")
        await repo.insert_event(
            db,
            kind="task_completed",
            task_id=task_id,
            actor="human",
            payload={"via": "decide_accept"},
        )
        await db.commit()
        await maybe_rollup_parent(db, task_id)
        await log_activity(
            db,
            "task_decided",
            f"Task #{task_id}: accepted after arbitration",
            detail=mutation_activity_detail(),
        )
    else:
        instructions = body.instructions or "Fix remaining issues."
        update_content = f"Human requested rework after arbiter review: {instructions}"
        if summary_text:
            update_content += f"\nDecision: {summary_text}"
        await repo.add_task_update(db, task_id, "human", "decision", update_content)
        await repo.update_task(db, task_id, review_cycle=0)
        await db.commit()

        message = plugins.dispatch.build_fix_message(
            task_id=task_id,
            title=task["title"],
            description=task.get("description", ""),
            review_comments=instructions,
            review_cycle=0,
            max_cycles=config.MAX_REVIEW_CYCLES,
        )
        runtime = task.get("runtime", "auto")
        result = await plugins.dispatch.submit_task(
            message, runtime=runtime, task_id=task_id
        )
        job_id = result.get("job_id")
        if job_id:
            await repo.update_task(db, task_id, status="fix_requested", job_id=job_id)
        else:
            await repo.update_task(db, task_id, status="open")
        await db.commit()
        await log_activity(
            db,
            "task_decided",
            f"Task #{task_id}: rework requested after arbitration",
            detail=mutation_activity_detail(),
        )

    if body.record_decision and summary_text:
        try:
            await plugins.notes.save_decision(
                task_id=task_id,
                action=body.action,
                summary=summary_text,
                context=body.instructions or "",
            )
        except Exception:
            log.warning(
                "Failed to save decision to notes for task #%s (non-fatal)",
                task_id,
                exc_info=True,
            )

    row = await repo.get_task(db, task_id)
    updates = await repo.get_task_updates(db, task_id)
    return row_to_task(row, updates=updates)  # type: ignore[arg-type]


async def add_update(
    db: aiosqlite.Connection,
    task_id: int,
    body: TaskUpdateCreate,
) -> TaskUpdateView:
    """Add an update to a task; route done reports through shared post-done logic."""
    row = await repo.get_task(db, task_id)
    if not row:
        raise HTTPException(404, "task not found")
    task = dict(row)

    if body.kind == "done":
        _validate_done_report(task)

    # Serialize the whole mutation (insert + status transition + commits) on the
    # shared connection so it cannot interleave with a refinement ``_atomic``
    # SAVEPOINT (see get_write_lock). Nothing inside acquires the lock again, so
    # there is no re-entrancy/deadlock. Note: log_activity commits inside here,
    # which is why it must run under the same lock.
    async with get_write_lock(db):
        update_id = await repo.add_task_update(
            db, task_id, body.agent, body.kind, body.content
        )

        if body.kind == "done":
            if task["status"] == "pending_report":
                if completion_requires_review(task):
                    # Universal Review Gate (#306): even the pending_report
                    # path may not complete unreviewed work — the done
                    # report becomes a submission for client-driven review.
                    generation = await repo.bump_submission_generation(db, task_id)
                    await repo.update_task(
                        db, task_id, status="review", review_job_id=None
                    )
                    await repo.add_task_update(
                        db,
                        task_id,
                        "hub",
                        "status",
                        f"Universal Review Gate: done report routed to review "
                        f"(submission #{generation}). Obtain an APPROVED "
                        "verdict via hub_submit_review, then report done "
                        "again.",
                    )
                    await log_activity(
                        db,
                        "task_review_required",
                        f"Task #{task_id} → review (gate) on report from {body.agent}",
                        detail=mutation_activity_detail(),
                    )
                else:
                    await repo.update_task(db, task_id, status="completed")
                    await log_activity(
                        db,
                        "task_completed",
                        f"Task #{task_id} completed with report from {body.agent}",
                        detail=mutation_activity_detail(),
                    )
                    await maybe_rollup_parent(db, task_id)
            elif task["status"] in ("running", "claimed") and not task.get("job_id"):
                # A done report on a pair-running task OR on a reserved (claimed)
                # task must never be silently dropped: route both through the
                # shared post-done transition. A claimed task never pair-started,
                # so it has no branch — transition_after_agent_done then routes
                # it to completed (no branch ⇒ ci_check is skipped).
                was_claimed = task["status"] == "claimed"
                updates_rows = await repo.get_task_updates(db, task_id)
                updates_list = [dict(r) for r in updates_rows]
                if any(u.get("kind") == "blocker" for u in updates_list):
                    await repo.update_task(db, task_id, status="needs_decision")
                    await log_activity(
                        db,
                        "task_needs_decision",
                        f"Task #{task_id} → needs_decision (blocker in done flow)",
                        detail=mutation_activity_detail(),
                    )
                else:
                    await transition_after_agent_done(db, task, has_done=True)
                if was_claimed:
                    await repo.update_task(
                        db, task_id, claimed_by=None, claim_session_id=None
                    )
                refreshed = await repo.get_task(db, task_id)
                if refreshed and dict(refreshed)["status"] == "completed":
                    await maybe_rollup_parent(db, task_id)
            # open + done without pair-start: rejected before insert (AC-2)
        else:
            await repo.update_task(db, task_id)

        await db.commit()
        await log_activity(
            db,
            "task_update",
            f"Task #{task_id} update from {body.agent}: {body.content[:80]}",
            detail=mutation_activity_detail(),
        )

    update_row = await repo.get_task_update_by_id(db, update_id)
    return TaskUpdateView(**dict(update_row))  # type: ignore[arg-type]


async def refresh_task(
    db: aiosqlite.Connection,
    task_id: int,
) -> TaskView:
    """Sync task status with dispatch job status."""
    row = await repo.get_task(db, task_id)
    if not row:
        raise HTTPException(404, "task not found")
    task = dict(row)
    job_id = task.get("job_id")
    if not job_id:
        return row_to_task(row)

    job = plugins.dispatch.get_job(job_id)
    if job:
        if job.get("status") == "completed":
            # Universal Review Gate (#309): a finished dispatch job must go
            # through the same gate-checked post-done transition as the
            # poller, not straight to completed — otherwise a manual
            # refresh call bypasses the review requirement.
            has_done = await repo.has_done_updates(db, task_id)
            await transition_after_agent_done(
                db,
                task,
                has_done=has_done,
                exit_code=job.get("exit_code"),
                result_text=job.get("result_text"),
            )
            await db.commit()
        elif job.get("status") in ("failed", "running"):
            await repo.update_task(
                db,
                task_id,
                status=job["status"],
                exit_code=job.get("exit_code"),
                result_text=job.get("result_text"),
            )
            await db.commit()

    row = await repo.get_task(db, task_id)
    return row_to_task(row)  # type: ignore[arg-type]


async def reorder_task(
    db: aiosqlite.Connection,
    task_id: int,
    body: TaskReorder,
) -> TaskView:
    """Change the position of a task within its parent."""
    row = await repo.get_task(db, task_id)
    if not row:
        raise HTTPException(404, "task not found")
    await repo.update_task(db, task_id, position=body.position)
    await db.commit()
    row = await repo.get_task(db, task_id)
    return row_to_task(row)  # type: ignore[arg-type]


_TERMINAL_DISPATCH_JOB_STATUSES = frozenset({"completed", "failed"})


def _dispatch_job_blocks_force_complete(
    job_id: str,
    job: dict[str, Any] | None,
) -> tuple[bool, str | None]:
    """Return (blocks, dispatch_status_or_missing)."""
    if job is None:
        return False, "missing"
    job_status = (job.get("status") or "").strip() or "unknown"
    if job_status in _TERMINAL_DISPATCH_JOB_STATUSES:
        return False, job_status
    return True, job_status


def _force_complete_job_overlay_note(
    field: str,
    job_id: str,
    dispatch_status: str | None,
) -> str:
    if dispatch_status == "missing":
        return f"Closed over {field}={job_id!r} (dispatch job missing from registry)."
    return (
        f"Closed over {field}={job_id!r} "
        f"(dispatch job terminal status={dispatch_status!r})."
    )


def _build_force_complete_comment(
    base_comment: str,
    *,
    from_status: str,
    job_id: str | None,
    review_job_id: str | None,
    overlay_notes: list[str],
) -> str:
    audit_bits = [f"from_status={from_status}"]
    if job_id:
        audit_bits.append(f"job_id={job_id}")
    if review_job_id:
        audit_bits.append(f"review_job_id={review_job_id}")
    parts = [base_comment, "[force-complete audit] " + ", ".join(audit_bits)]
    parts.extend(overlay_notes)
    return "\n".join(parts)


async def _has_incomplete_descendants(
    db: aiosqlite.Connection,
    root_id: int,
) -> bool:
    rows = await db.execute_fetchall(
        """
        WITH RECURSIVE sub(id) AS (
            SELECT id FROM tasks WHERE parent_id = ?
            UNION ALL
            SELECT t.id FROM tasks t JOIN sub ON t.parent_id = sub.id
        )
        SELECT 1 FROM tasks t
        JOIN sub ON t.id = sub.id
        WHERE t.archived = 0
          AND t.status NOT IN ('completed', 'failed', 'rejected')
        LIMIT 1
        """,
        (root_id,),
    )
    return bool(rows)


_FORCE_COMPLETE_DEFAULT_COMMENT_STATUSES = frozenset({"pending_report", "claimed"})


async def force_complete_task(
    db: aiosqlite.Connection,
    task_id: int,
    body: TaskForceComplete | None = None,
) -> TaskView:
    """Force-complete a stuck task without going through review.

    Human-only audited override: allowed from any non-terminal ``task`` or
    ``subtask`` when no *active* dispatch job backs ``job_id`` or
    ``review_job_id``. Missing or terminal dispatch jobs are permitted and
    noted in the audit trail. ``epic``/``feature`` rows are rejected when
    they still have incomplete descendants.

    The optional ``body.comment`` is recorded as the audit-trail message; if
    omitted, a default human-override message is used.
    """
    row = await repo.get_task(db, task_id)
    if not row:
        raise HTTPException(404, "task not found")
    task = dict(row)
    status = task["status"]
    final_values = {s.value for s in FINAL_STATUSES}
    if status in final_values:
        raise HTTPException(
            400,
            f"cannot force-complete terminal task, current status: {status}",
        )

    task_type = task.get("task_type") or "task"
    if task_type in ("epic", "feature"):
        if await _has_incomplete_descendants(db, task_id):
            raise HTTPException(
                400,
                f"cannot force-complete {task_type} with incomplete descendants",
            )

    overlay_notes: list[str] = []
    for field, label in (("job_id", "job_id"), ("review_job_id", "review_job_id")):
        job_ref = (task.get(field) or "").strip()
        if not job_ref:
            continue
        job = plugins.dispatch.get_job(job_ref)
        blocks, dispatch_status = _dispatch_job_blocks_force_complete(job_ref, job)
        if blocks:
            raise HTTPException(
                409,
                f"active dispatch {label} {job_ref!r} status={dispatch_status!r} "
                "blocks force-complete",
            )
        overlay_notes.append(
            _force_complete_job_overlay_note(label, job_ref, dispatch_status)
        )

    comment_raw = body.comment.strip() if body else ""
    active_statuses = {s.value for s in ACTIVE_STATUSES}
    if (
        status in active_statuses
        and status not in _FORCE_COMPLETE_DEFAULT_COMMENT_STATUSES
        and not comment_raw
    ):
        raise HTTPException(
            400,
            f"force-complete from active status {status!r} requires a non-empty comment",
        )

    base_comment = comment_raw or ("Force-completed by human without agent report.")
    comment = _build_force_complete_comment(
        base_comment,
        from_status=status,
        job_id=(task.get("job_id") or None),
        review_job_id=(task.get("review_job_id") or None),
        overlay_notes=overlay_notes,
    )

    update_fields: dict[str, Any] = {"status": "completed"}
    if task.get("claimed_by") or task.get("claim_session_id") or task.get("claimed_at"):
        update_fields["claimed_by"] = None
        update_fields["claim_session_id"] = None
        update_fields["claimed_at"] = None

    # Serialize against refinement _atomic savepoints on the shared connection.
    async with get_write_lock(db):
        await repo.add_task_update(db, task_id, "human", "done", comment)
        await repo.update_task(db, task_id, **update_fields)
        await db.commit()
        await maybe_rollup_parent(db, task_id)
    row = await repo.get_task(db, task_id)
    updates = await repo.get_task_updates(db, task_id)
    return row_to_task(row, updates=updates)  # type: ignore[arg-type]


async def withdraw_own_draft(
    db: aiosqlite.Connection,
    task_id: int,
    *,
    caller: str,
    caller_principal_id: int | None = None,
) -> TaskView:
    """Archive a single agent-owned draft (no cascade). Agent-only narrow path."""
    row = await repo.get_task(db, task_id)
    if not row:
        raise TaskNotFoundError(f"task {task_id} not found")
    task = dict(row)

    if task.get("source") != TaskSource.agent.value:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail=withdraw_own_draft_error_detail(
                reason="not_agent_draft",
                message="only agent-created drafts can be withdrawn",
                hint=(
                    "hub_withdraw_own_draft applies to source=agent drafts you own. "
                    "For other tasks ask a human to archive."
                ),
                suggested_tool="hub_archive_task",
                required_role="human",
            ),
        )

    if task.get("status") != "draft":
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail=withdraw_own_draft_error_detail(
                reason="invalid_status_for_withdraw",
                message=f"can only withdraw draft tasks, current: {task.get('status')}",
                hint="Only draft tasks can be withdrawn. Approved or active work cannot.",
                current_status=task.get("status"),
                required_status="draft",
                suggested_tool="hub_task_status",
            ),
        )

    assigned = (task.get("assigned_agent") or "").strip()
    principal_match = (
        caller_principal_id is not None
        and task.get("implementer_principal_id") is not None
        and task.get("implementer_principal_id") == caller_principal_id
    )
    if assigned != caller.strip() and not principal_match:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail=withdraw_own_draft_error_detail(
                reason="not_task_owner",
                message="caller is not the assigned agent for this draft",
                hint=(
                    "You can only withdraw drafts assigned to you "
                    "(assigned_agent must match your token identity)."
                ),
            ),
        )

    children = await db_module.get_children(db, task_id)
    if children:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail=withdraw_own_draft_error_detail(
                reason="withdraw_has_children",
                message="draft has non-archived child tasks",
                hint=(
                    "Archive or delete child tasks first, or ask a human to "
                    "hub_archive_task with cascade."
                ),
                suggested_tool="hub_archive_task",
                required_role="human",
            ),
        )

    await repo.set_tasks_archived(db, [task_id], 1)
    await db.commit()
    detail_payload = {
        **json.loads(mutation_activity_detail()),
        "actor": caller.strip(),
        "action": "withdraw_own_draft",
    }
    await log_activity(
        db,
        "task_withdrawn",
        f"Agent {caller} withdrew draft #{task_id}",
        json.dumps(detail_payload, ensure_ascii=False),
    )

    row = await repo.get_task(db, task_id)
    updates = await repo.get_task_updates(db, task_id)
    task_view = row_to_task(row, updates=updates)  # type: ignore[arg-type]
    return await enrich_task_view(db, task_view)


async def archive_task(
    db: aiosqlite.Connection,
    task_id: int,
    *,
    cascade: bool = True,
) -> TaskView:
    """Mark task (and optionally subtree) archived — excluded from default lists."""
    row = await repo.get_task(db, task_id)
    if not row:
        raise TaskNotFoundError(f"task {task_id} not found")
    ids = await repo.collect_subtree_ids(db, task_id) if cascade else [task_id]
    await repo.set_tasks_archived(db, ids, 1)
    await db.commit()
    await log_activity(
        db,
        "task_archived",
        f"Task #{task_id} archived (cascade={cascade}, count={len(ids)})",
        detail=mutation_activity_detail(),
    )
    row = await repo.get_task(db, task_id)
    updates = await repo.get_task_updates(db, task_id)
    task_view = row_to_task(row, updates=updates)  # type: ignore[arg-type]
    return await enrich_task_view(db, task_view)


async def unarchive_task(
    db: aiosqlite.Connection,
    task_id: int,
    *,
    cascade: bool = True,
) -> TaskView:
    """Clear archived flag for task and optionally its subtree."""
    row = await repo.get_task(db, task_id)
    if not row:
        raise TaskNotFoundError(f"task {task_id} not found")
    ids = await repo.collect_subtree_ids(db, task_id) if cascade else [task_id]
    await repo.set_tasks_archived(db, ids, 0)
    await db.commit()
    await log_activity(
        db,
        "task_unarchived",
        f"Task #{task_id} unarchived (cascade={cascade}, count={len(ids)})",
        detail=mutation_activity_detail(),
    )
    row = await repo.get_task(db, task_id)
    updates = await repo.get_task_updates(db, task_id)
    task_view = row_to_task(row, updates=updates)  # type: ignore[arg-type]
    return await enrich_task_view(db, task_view)


async def delete_task_tree(db: aiosqlite.Connection, task_id: int) -> None:
    """Permanently remove a task and all descendants (DB rows and updates)."""
    row = await repo.get_task(db, task_id)
    if not row:
        raise TaskNotFoundError(f"task {task_id} not found")
    n = await repo.delete_task_subtree(db, task_id)
    await db.commit()
    await log_activity(
        db,
        "task_deleted",
        f"Deleted task subtree rooted at #{task_id} ({n} tasks)",
        detail=mutation_activity_detail(),
    )
