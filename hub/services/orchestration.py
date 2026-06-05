"""Orchestration helpers: dispatch, review, CI fix, arbiter, Vast cleanup."""

from __future__ import annotations

import logging
from typing import Any

import aiosqlite

from hub import config
from hub import repository as repo
from hub.db import get_breadcrumb, log_activity
from hub.integrations.registry import plugins

log = logging.getLogger("hub")


async def dispatch_task(
    db: aiosqlite.Connection,
    task_id: int,
    task: dict[str, Any],
) -> dict[str, Any]:
    """Dispatch a task via oc-dev-dispatch, creating a branch if needed."""
    branch = task.get("branch") or ""
    if not branch:
        branch = await plugins.git_ops.create_branch(task_id, task["title"])
        if branch:
            await repo.update_task(db, task_id, branch=branch)
            await db.commit()
    else:
        await plugins.git_ops.checkout(branch)

    updates_rows = await repo.get_task_updates(db, task_id)
    updates = [dict(r) for r in updates_rows] if updates_rows else None

    message = plugins.dispatch.build_enriched_message(
        task["title"],
        task.get("description", ""),
        updates,
        branch=branch,
    )
    runtime = task.get("runtime", "auto")
    result = await plugins.dispatch.submit_task(
        message, runtime=runtime, task_id=task_id
    )
    job_id = result.get("job_id")

    if job_id:
        assigned_agent = (
            result.get("assigned_agent")
            or result.get("agent")
            or task.get("assigned_agent")
            or "developer-agent"
        )
        await repo.update_task(
            db,
            task_id,
            status="running",
            job_id=job_id,
            assigned_agent=assigned_agent,
        )
    else:
        error = result.get("error", "dispatch returned no job_id")
        await repo.update_task(
            db,
            task_id,
            status="open",
            job_id=None,
            result_text=error,
        )
        await repo.add_task_update(
            db,
            task_id,
            "hub",
            "alert",
            f"Developer-agent dispatch unavailable: {error}",
        )
    await db.commit()
    return result


async def dispatch_review(
    db: aiosqlite.Connection,
    task: dict[str, Any],
) -> None:
    """Dispatch a code-review job for a completed task."""
    task_id = task["id"]
    review_cycle = task.get("review_cycle", 0)
    breadcrumb = await get_breadcrumb_str(db, task_id)
    message = plugins.dispatch.build_review_message(
        task_id=task_id,
        title=task["title"],
        description=task.get("description", ""),
        review_cycle=review_cycle,
        max_cycles=config.MAX_REVIEW_CYCLES,
        branch=task.get("branch", ""),
        pr_number=task.get("pr_number"),
        breadcrumb=breadcrumb,
    )
    result = await plugins.dispatch.submit_task(
        message,
        runtime=config.REVIEW_RUNTIME,
        agent=config.REVIEW_AGENT,
        task_id=task_id,
    )
    review_job_id = result.get("job_id")
    if review_job_id:
        await repo.update_task(
            db, task_id, status="review", review_job_id=review_job_id
        )
        log.info(
            "Poll: task #%d → review (job=%s, agent=%s, cycle=%d)",
            task_id,
            review_job_id,
            config.REVIEW_AGENT,
            review_cycle + 1,
        )
    else:
        log.warning(
            "Poll: failed to dispatch review for task #%d: %s",
            task_id,
            result.get("error"),
        )
        await repo.update_task(db, task_id, status="completed")
    await db.commit()


async def dispatch_fix(
    db: aiosqlite.Connection,
    task: dict[str, Any],
    review_comments: str,
) -> None:
    """Dispatch a fix job back to the developer agent."""
    task_id = task["id"]
    review_cycle = task.get("review_cycle", 0) + 1
    message = plugins.dispatch.build_fix_message(
        task_id=task_id,
        title=task["title"],
        description=task.get("description", ""),
        review_comments=review_comments,
        review_cycle=review_cycle,
        max_cycles=config.MAX_REVIEW_CYCLES,
        branch=task.get("branch", ""),
    )
    runtime = task.get("runtime", "auto")
    result = await plugins.dispatch.submit_task(
        message, runtime=runtime, task_id=task_id
    )
    job_id = result.get("job_id")
    if job_id:
        await repo.update_task(
            db,
            task_id,
            status="fix_requested",
            job_id=job_id,
            review_cycle=review_cycle,
        )
        log.info(
            "Poll: task #%d → fix_requested (job=%s, cycle=%d/%d)",
            task_id,
            job_id,
            review_cycle,
            config.MAX_REVIEW_CYCLES,
        )
    else:
        log.warning(
            "Poll: failed to dispatch fix for task #%d: %s",
            task_id,
            result.get("error"),
        )
        await repo.update_task(
            db,
            task_id,
            status="completed",
            review_cycle=review_cycle,
        )
    await db.commit()


async def dispatch_arbiter(
    db: aiosqlite.Connection,
    task: dict[str, Any],
    updates_list: list[dict[str, Any]],
) -> None:
    """Dispatch an arbiter (Claude Sonnet) when review cycle limit is reached."""
    task_id = task["id"]
    review_cycle = task.get("review_cycle", 0)

    review_history = [
        u
        for u in updates_list
        if u.get("kind") in ("review", "done", "status", "alert")
    ]

    await repo.add_task_update(
        db,
        task_id,
        "hub",
        "alert",
        f"Review cycle limit reached ({review_cycle}/{config.MAX_REVIEW_CYCLES}). "
        "Dispatching arbiter for independent assessment.",
    )
    await db.commit()
    await log_activity(
        db,
        "review_cycle_limit",
        f"Task #{task_id}: review cycle limit ({review_cycle}/{config.MAX_REVIEW_CYCLES}), dispatching arbiter",
    )

    message = plugins.dispatch.build_arbiter_message(
        task_id=task_id,
        title=task["title"],
        description=task.get("description", ""),
        review_history=review_history,
        review_cycle=review_cycle,
        max_cycles=config.MAX_REVIEW_CYCLES,
        branch=task.get("branch", ""),
    )
    result = await plugins.dispatch.submit_task(
        message,
        runtime=config.ARBITER_RUNTIME,
        agent=config.ARBITER_AGENT,
        task_id=task_id,
    )
    arbiter_job_id = result.get("job_id")
    if arbiter_job_id:
        await repo.update_task(
            db, task_id, status="review", review_job_id=arbiter_job_id
        )
        log.info("Poll: task #%d → arbiter review (job=%s)", task_id, arbiter_job_id)
    else:
        log.warning(
            "Poll: failed to dispatch arbiter for task #%d: %s",
            task_id,
            result.get("error"),
        )
        await repo.update_task(db, task_id, status="needs_decision")
    await db.commit()


async def dispatch_ci_fix(
    db: aiosqlite.Connection,
    task: dict[str, Any],
    ci_failures: dict[str, Any],
) -> None:
    """Dispatch developer to fix CI failures."""
    task_id = task["id"]
    ci_fix_cycle = task.get("ci_fix_cycle", 0)
    message = plugins.dispatch.build_ci_fix_message(
        task_id=task_id,
        title=task["title"],
        description=task.get("description", ""),
        ci_failures=ci_failures,
        ci_fix_cycle=ci_fix_cycle,
        max_cycles=config.MAX_CI_FIX_CYCLES,
        branch=task.get("branch", ""),
    )
    runtime = task.get("runtime", "auto")
    result = await plugins.dispatch.submit_task(
        message, runtime=runtime, task_id=task_id
    )
    job_id = result.get("job_id")
    if job_id:
        branch = task.get("branch")
        if branch:
            await plugins.git_ops.checkout(branch)
        await repo.update_task(
            db,
            task_id,
            status="running",
            job_id=job_id,
            ci_fix_cycle=ci_fix_cycle + 1,
        )
        log.info(
            "Poll: task #%d → running (CI fix, cycle=%d/%d)",
            task_id,
            ci_fix_cycle + 1,
            config.MAX_CI_FIX_CYCLES,
        )
    else:
        log.warning(
            "Poll: failed to dispatch CI fix for task #%d: %s",
            task_id,
            result.get("error"),
        )
        await repo.update_task(db, task_id, status="needs_decision")
    await db.commit()


def extract_review_verdict(
    task_id: int,
    review_job_id: str,
    db_updates: list[dict[str, Any]],
) -> str | None:
    """Return 'approved' or 'changes_requested' from task_updates or full dispatch log.

    Search order:
    1. task_updates with kind='review' — scan all lines for verdict
    2. Full dispatch log — scan all lines for the LAST occurrence of a verdict keyword
    """
    for u in reversed(db_updates):
        if u.get("kind") == "review":
            text = u.get("content", "").strip()
            verdict = scan_text_for_verdict(text)
            if verdict:
                return verdict

    full_log = plugins.dispatch.job_log_full(review_job_id)
    if full_log:
        verdict = scan_text_for_verdict(full_log)
        if verdict:
            log.info(
                "Poll: task #%d verdict '%s' extracted from dispatch log",
                task_id,
                verdict,
            )
            return verdict

    return None


def scan_text_for_verdict(text: str) -> str | None:
    """Scan text for the last occurrence of APPROVED or CHANGES_REQUESTED."""
    last_verdict: str | None = None
    for line in text.split("\n"):
        line_lower = line.strip().lower()
        if "changes_requested" in line_lower:
            last_verdict = "changes_requested"
        elif (
            line_lower.rstrip().endswith("approved") or line_lower.strip() == "approved"
        ):
            last_verdict = "approved"
    return last_verdict


async def maybe_destroy_vast(
    db: aiosqlite.Connection,
    task: dict[str, Any],
) -> None:
    """Destroy Vast instance if no active Vast tasks remain."""
    if not config.VAST_ENABLED:
        return
    if await plugins.vast.has_active_vast_tasks(db):
        return
    status = await plugins.vast.vast_status()
    if not status.get("managed"):
        return
    log.info("No active Vast tasks remaining, destroying instance")
    await plugins.vast.vast_down()
    await log_activity(
        db,
        "vast_shutdown",
        f"Vast instance destroyed after task #{task['id']} finished",
    )


async def get_breadcrumb_str(
    db: aiosqlite.Connection,
    task_id: int,
) -> str:
    """Build a human-readable breadcrumb string for dispatch messages."""
    crumbs = await get_breadcrumb(db, task_id)
    if len(crumbs) <= 1:
        return ""
    return " > ".join(
        f"{c['task_type'].capitalize()}: {c['title']} (#{c['id']})" for c in crumbs
    )
