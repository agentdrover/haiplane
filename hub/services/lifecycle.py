"""Task lifecycle transitions: create, approve, reject, start, Q&A, decide, etc."""

from __future__ import annotations

import json
import logging
from typing import Any

import aiosqlite
from fastapi import HTTPException

from hub import config
from hub import db as db_module
from hub import repository as repo
from hub.db import log_activity, structured_fields_from_row
from hub.integrations.registry import plugins
from hub.models import (
    TaskAnswer,
    TaskApprove,
    TaskBreadcrumb,
    TaskChildSummary,
    TaskCreate,
    TaskDecide,
    TaskForceComplete,
    TaskProgress,
    TaskQuestion,
    TaskReject,
    TaskReorder,
    TaskSource,
    TaskStart,
    TaskType,
    TaskUpdateCreate,
    TaskUpdateView,
    TaskView,
)
from hub.services.orchestration import dispatch_task

log = logging.getLogger("hub")


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
        branch=d.get("branch"),
        pr_number=d.get("pr_number"),
        created_at=d["created_at"],
        updated_at=d["updated_at"],
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

    return task_view


async def create_task(db: aiosqlite.Connection, body: TaskCreate) -> TaskView:
    """Create a new task, optionally dispatching it immediately."""
    err = await db_module.validate_hierarchy(db, body.task_type.value, body.parent_id)
    if err:
        raise HTTPException(400, err)

    if body.task_type in (TaskType.epic, TaskType.feature):
        initial_status = "open"
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
    )

    row = await repo.get_task(db, task_id)
    updates = await repo.get_task_updates(db, task_id)
    return row_to_task(row, updates=updates)  # type: ignore[arg-type]


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
    await db.commit()
    await log_activity(db, "task_rejected", f"Task #{task_id} rejected")

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
    await log_activity(db, "task_started", f"Task #{task_id} dispatched")

    row = await repo.get_task(db, task_id)
    updates = await repo.get_task_updates(db, task_id)
    return row_to_task(row, updates=updates)  # type: ignore[arg-type]


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
    if task["status"] != "running":
        raise HTTPException(
            400,
            f"can only ask questions on running tasks, current status: {task['status']}",
        )

    await repo.add_task_update(db, task_id, body.agent, "question", body.question)
    await repo.update_task(db, task_id, status="needs_info")
    await db.commit()
    await log_activity(db, "task_question", f"Task #{task_id}: agent asked a question")

    row = await repo.get_task(db, task_id)
    updates = await repo.get_task_updates(db, task_id)
    return row_to_task(row, updates=updates)  # type: ignore[arg-type]


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
    await db.commit()

    if body.resume:
        row = await repo.get_task(db, task_id)
        await dispatch_task(db, task_id, dict(row))  # type: ignore[arg-type]
        await log_activity(
            db,
            "task_answered",
            f"Task #{task_id}: answered and re-dispatched",
        )
    else:
        await repo.update_task(db, task_id, status="open")
        await db.commit()
        await log_activity(
            db,
            "task_answered",
            f"Task #{task_id}: answered, moved to open",
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
        await db.commit()
        await log_activity(
            db,
            "task_decided",
            f"Task #{task_id}: accepted after arbitration",
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
    """Add an update to a task, auto-completing pending_report tasks on done."""
    row = await repo.get_task(db, task_id)
    if not row:
        raise HTTPException(404, "task not found")
    task = dict(row)
    update_id = await repo.add_task_update(
        db, task_id, body.agent, body.kind, body.content
    )
    await repo.update_task(db, task_id)

    if body.kind == "done" and task["status"] == "pending_report":
        await repo.update_task(db, task_id, status="completed")
        await log_activity(
            db,
            "task_completed",
            f"Task #{task_id} completed with report from {body.agent}",
        )

    await db.commit()
    await log_activity(
        db,
        "task_update",
        f"Task #{task_id} update from {body.agent}: {body.content[:80]}",
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
        new_status = task["status"]
        if job.get("status") == "completed":
            new_status = "completed"
        elif job.get("status") == "failed":
            new_status = "failed"
        elif job.get("status") == "running":
            new_status = "running"

        await repo.update_task(
            db,
            task_id,
            status=new_status,
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


async def force_complete_task(
    db: aiosqlite.Connection,
    task_id: int,
    body: TaskForceComplete | None = None,
) -> TaskView:
    """Force-complete a pending_report task without a done report.

    The optional ``body.comment`` is recorded as the audit-trail message; if
    omitted, a default human-override message is used.
    """
    row = await repo.get_task(db, task_id)
    if not row:
        raise HTTPException(404, "task not found")
    task = dict(row)
    if task["status"] != "pending_report":
        raise HTTPException(
            400,
            f"can only force-complete pending_report tasks, current: {task['status']}",
        )
    comment = (body.comment.strip() if body else "") or (
        "Force-completed by human without agent report."
    )
    await repo.add_task_update(db, task_id, "human", "done", comment)
    await repo.update_task(db, task_id, status="completed")
    await db.commit()
    await log_activity(
        db,
        "task_force_completed",
        f"Task #{task_id} force-completed without report",
    )
    row = await repo.get_task(db, task_id)
    updates = await repo.get_task_updates(db, task_id)
    return row_to_task(row, updates=updates)  # type: ignore[arg-type]
