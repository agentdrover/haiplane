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


async def project_git_context(
    db: aiosqlite.Connection,
    task_id: int,
) -> dict[str, Any]:
    """Git kwargs from the task's project (#337).

    Empty project fields are omitted so git_ops falls back to env — the
    seeded default project behaves exactly like the pre-project hub.
    """
    row = await repo.resolve_project_for_task(db, task_id)
    if row is None:
        return {}
    d = dict(row)
    ctx: dict[str, Any] = {}
    if (d.get("workspace_path") or "").strip():
        ctx["repo"] = d["workspace_path"].strip()
    if (d.get("repo") or "").strip():
        ctx["gh_repo"] = d["repo"].strip()
    if (d.get("default_branch") or "").strip():
        ctx["base_branch"] = d["default_branch"].strip()
    # The default project mirrors env values; dropping them keeps call
    # sites byte-identical to legacy behavior for it.
    if d.get("slug") == "default":
        return {}
    return ctx


def _split_git_kwargs(ctx: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """(local_git_kwargs, pr_kwargs) — local ops need repo/base, PR needs all."""
    local = {k: v for k, v in ctx.items() if k in ("repo", "base_branch")}
    return local, dict(ctx)


async def dispatch_task(
    db: aiosqlite.Connection,
    task_id: int,
    task: dict[str, Any],
) -> dict[str, Any]:
    """Dispatch a task via oc-dev-dispatch, creating a branch if needed."""
    ctx = await project_git_context(db, task_id)
    local_kw, _ = _split_git_kwargs(ctx)
    branch = task.get("branch") or ""
    if not branch:
        branch = await plugins.git_ops.create_branch(task_id, task["title"], **local_kw)
        if branch:
            await repo.update_task(db, task_id, branch=branch)
            await db.commit()
    else:
        await plugins.git_ops.checkout(branch, repo=local_kw.get("repo"))

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


async def prepare_pair_branch(
    db: aiosqlite.Connection,
    task_id: int,
    task: dict[str, Any],
    *,
    branch_slug: str = "",
) -> str:
    """Create or checkout a task branch without dispatching a headless agent."""
    ctx = await project_git_context(db, task_id)
    local_kw, _ = _split_git_kwargs(ctx)
    branch = (task.get("branch") or "").strip()
    if branch:
        await plugins.git_ops.checkout(branch, repo=local_kw.get("repo"))
        return branch
    return await plugins.git_ops.pair_prepare_branch(
        task_id,
        task["title"],
        branch_slug=branch_slug,
        **local_kw,
    )


def review_approved_for_current_submission(task: dict[str, Any]) -> bool:
    """True only when an APPROVED verdict applies to the latest submission.

    A verdict recorded against an earlier submission generation is stale:
    the work changed since it was approved. A task with no submissions yet
    (generation 0) can never count as approved.
    """
    generation = task.get("submission_generation") or 0
    return (
        generation > 0
        and task.get("review_verdict") == "approved"
        and task.get("review_verdict_generation") == generation
    )


def completion_requires_review(task: dict[str, Any]) -> bool:
    """Universal Review Gate (#306): the single completion-gate predicate.

    Normal completion paths (done reports across API/MCP/poller) must not
    complete a task unless the CURRENT submission carries an APPROVED
    verdict. ``auto_review=False`` is the explicit human-controlled opt-out
    (subtasks default to it); human overrides (decide accept,
    force_complete) bypass this predicate by design and stay audited.
    """
    return bool(task.get("auto_review")) and not review_approved_for_current_submission(
        task
    )


async def transition_after_agent_done(
    db: aiosqlite.Connection,
    task: dict[str, Any],
    *,
    has_done: bool,
    exit_code: int | None = None,
    result_text: str | None = None,
) -> str:
    """Post-done lifecycle shared by headless poller and pair mode."""
    task_id = task["id"]
    branch = task.get("branch")

    if has_done and not completion_requires_review(task):
        # Review gate satisfied: either an explicit auto_review opt-out or
        # the current submission already has an APPROVED verdict. Complete
        # WITHOUT bumping the generation — no new work is being submitted,
        # and a bump would invalidate the very approval that authorizes
        # this completion (#306).
        await repo.update_task(
            db,
            task_id,
            status="completed",
            exit_code=exit_code,
            result_text=result_text,
        )
        await repo.insert_event(
            db,
            kind="task_completed",
            task_id=task_id,
            actor=task.get("assigned_agent") or "agent",
            payload={"via": "report_done"},
        )
        log.info("Task #%d → completed after done report", task_id)
        return "completed"

    if has_done:
        # Unreviewed done report = a work submission (#305): bumping the
        # generation invalidates any APPROVED verdict from earlier work.
        await repo.bump_submission_generation(db, task_id)

    if (
        task.get("auto_review")
        and task.get("review_cycle", 0) < config.MAX_REVIEW_CYCLES
        and has_done
        and branch
    ):
        ctx = await project_git_context(db, task_id)
        workspace = ctx.get("repo")
        await plugins.git_ops.checkout(branch, repo=workspace)
        await plugins.git_ops.auto_commit(
            task_id, title=task.get("title", ""), repo=workspace
        )
        squashed = await plugins.git_ops.squash_branch(
            task_id,
            task.get("title", ""),
            branch,
            repo=workspace,
        )
        await plugins.git_ops.push_branch(branch, repo=workspace, force=squashed)
        if not task.get("pr_number"):
            pr_num = await plugins.git_ops.create_pr(
                task_id,
                task["title"],
                task.get("description", ""),
                branch,
                repo=workspace,
                gh_repo=ctx.get("gh_repo"),
                base_branch=ctx.get("base_branch"),
            )
            if pr_num:
                await repo.update_task(db, task_id, pr_number=pr_num)
                task["pr_number"] = pr_num
        await repo.update_task(
            db,
            task_id,
            status="ci_check",
            exit_code=exit_code,
            result_text=result_text,
        )
        log.info("Task #%d → ci_check after done report", task_id)
        return "ci_check"

    if has_done and task.get("review_cycle", 0) >= config.MAX_REVIEW_CYCLES:
        # Review cycle limit reached without approval: escalate to the human
        # Decision Gate instead of looping through review forever (#306).
        await repo.update_task(
            db,
            task_id,
            status="needs_decision",
            exit_code=exit_code,
            result_text=result_text,
        )
        await repo.add_task_update(
            db,
            task_id,
            "hub",
            "alert",
            f"Review cycle limit reached ({task.get('review_cycle', 0)}/"
            f"{config.MAX_REVIEW_CYCLES}) without APPROVED review. "
            "Human decision required (hub_decide_task).",
        )
        await repo.insert_event(
            db,
            kind="needs_decision",
            task_id=task_id,
            actor="hub",
            payload={"reason": "review_cycle_limit"},
        )
        log.info("Task #%d → needs_decision (review cycle limit)", task_id)
        return "needs_decision"

    if has_done:
        # Universal Review Gate (#306): a done report on an unreviewed task
        # is a submission for review, not a completion. Route to
        # client-driven review (no review_job_id) and tell the agent how to
        # obtain the verdict.
        generation = (await repo.get_task(db, task_id)) or {}
        generation_num = dict(generation).get("submission_generation", 0)
        await repo.update_task(
            db,
            task_id,
            status="review",
            review_job_id=None,
            exit_code=exit_code,
            result_text=result_text,
        )
        await repo.add_task_update(
            db,
            task_id,
            "hub",
            "status",
            f"Universal Review Gate: done report routed to review "
            f"(submission #{generation_num}). Obtain an APPROVED verdict via "
            "hub_submit_review (reviewer: hub_get_review_brief), then report "
            "done again.",
        )
        log.info("Task #%d → review after done report (review gate)", task_id)
        return "review"

    await repo.update_task(
        db,
        task_id,
        status="pending_report",
        exit_code=exit_code,
        result_text=result_text,
    )
    log.info("Task #%d → pending_report after done report", task_id)
    return "pending_report"


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
        # Universal Review Gate (#309): failure to dispatch a reviewer must
        # never complete the task — escalate to the human Decision Gate.
        await repo.update_task(db, task_id, status="needs_decision")
        await repo.insert_event(
            db,
            kind="needs_decision",
            task_id=task_id,
            actor="hub",
            payload={"reason": "review_dispatch_failed"},
        )
        await repo.add_task_update(
            db,
            task_id,
            "hub",
            "alert",
            f"Reviewer dispatch failed: {result.get('error', 'no job_id')}. "
            "Universal Review Gate: manual decision required (hub_decide_task).",
        )
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
        # Universal Review Gate (#309): CHANGES_REQUESTED work must not
        # silently complete when the fix dispatch fails.
        await repo.update_task(
            db,
            task_id,
            status="needs_decision",
            review_cycle=review_cycle,
        )
        await repo.insert_event(
            db,
            kind="needs_decision",
            task_id=task_id,
            actor="hub",
            payload={"reason": "fix_dispatch_failed"},
        )
        await repo.add_task_update(
            db,
            task_id,
            "hub",
            "alert",
            f"Fix dispatch failed after CHANGES_REQUESTED: "
            f"{result.get('error', 'no job_id')}. Manual decision required.",
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
        await repo.insert_event(
            db,
            kind="needs_decision",
            task_id=task_id,
            actor="hub",
            payload={"reason": "arbiter_dispatch_failed"},
        )
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
        await repo.insert_event(
            db,
            kind="needs_decision",
            task_id=task_id,
            actor="hub",
            payload={"reason": "ci_fix_dispatch_failed"},
        )
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
