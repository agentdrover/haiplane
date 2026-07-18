"""Orchestration helpers: dispatch, review, CI fix, arbiter, Vast cleanup."""

from __future__ import annotations

import logging
import os
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


def machine_review_required(task: dict[str, Any], project_policy: str = "auto") -> bool:
    """Machine-review applicability cascade (#382).

    task override > project policy > auto rules from task metadata.
    Computed at submit/verdict time, not at creation — size and risks are
    refined along the way and the decision must see current values.
    """
    import json as _json

    override = (task.get("machine_review_override") or "").strip()
    if override == "require":
        return True
    if override == "skip":
        return False
    policy = (project_policy or "auto").strip()
    if policy == "off":
        return False
    if policy == "always":
        return True
    # auto rules: high/security risk always wins
    try:
        risks = _json.loads(task.get("risks") or "[]")
    except ValueError:
        risks = []
    for risk in risks:
        if isinstance(risk, dict) and (
            risk.get("severity") == "high" or risk.get("kind") == "security"
        ):
            return True
    work_type = (task.get("work_type") or "feature").strip()
    if work_type in ("docs", "chore", "spike"):
        return False
    if work_type == "refactor":
        return True
    if (task.get("size") or "").strip() in ("XS", "S"):
        return False
    # feature/bug/incident sized M+ (or unsized — err toward review)
    return True


async def machine_review_gap(
    db: aiosqlite.Connection, task: dict[str, Any]
) -> str | None:
    """None when policy is satisfied; otherwise a human-readable gap reason."""
    project = await repo.resolve_project_for_task(db, task["id"])
    keys = project.keys() if project is not None else []
    policy = project["machine_review"] if "machine_review" in keys else "auto"
    if not machine_review_required(task, policy):
        return None
    generation = task.get("submission_generation") or 0
    mr = await repo.get_latest_machine_review(db, task["id"])
    if mr is None:
        return "machine-review отсутствует для текущего сабмишена"
    if mr["submission_generation"] != generation:
        return "machine-review устарел (работа пересдана) — прогоните харнесс заново"
    return None


async def practice_metrics(
    db: aiosqlite.Connection, *, since_days: int = 90
) -> dict[str, Any]:
    """Practice economics (#384): machine-review costs, filtration rate,
    harness-version comparison, recurring finding categories, cycle times.

    Aggregated on the fly from machine_reviews and task timestamps —
    ``updated_at`` of a completed task is the completion proxy (the
    dedicated completed_at column was never populated). Token/duration
    fields are optional in reports, so aggregates carry ``reports_without_tokens``
    instead of pretending coverage is full.
    """
    import statistics

    since = f"-{since_days} days"

    totals_rows = await db.execute_fetchall(
        "SELECT COUNT(*) AS reviews, "
        "COALESCE(SUM(raw_count), 0) AS raw_total, "
        "COALESCE(SUM(json_array_length(findings_confirmed)), 0) AS confirmed_total, "
        "COALESCE(SUM(json_array_length(findings_rejected)), 0) AS rejected_total, "
        "COALESCE(SUM(tokens_spent), 0) AS tokens_total, "
        "COALESCE(SUM(duration_ms), 0) AS duration_ms_total, "
        "SUM(CASE WHEN tokens_spent IS NULL THEN 1 ELSE 0 END) AS reports_without_tokens "
        "FROM machine_reviews WHERE created_at >= datetime('now', ?)",
        (since,),
    )
    totals = dict(totals_rows[0])
    confirmed = totals["confirmed_total"] or 0
    raw = totals["raw_total"] or 0
    totals["tokens_per_confirmed"] = (
        round(totals["tokens_total"] / confirmed) if confirmed else None
    )
    totals["filtration_rate"] = round(1 - confirmed / raw, 3) if raw else None

    harness_rows = await db.execute_fetchall(
        "SELECT harness_skill, harness_version, COUNT(*) AS reviews, "
        "COALESCE(SUM(raw_count), 0) AS raw_total, "
        "COALESCE(SUM(json_array_length(findings_confirmed)), 0) AS confirmed_total, "
        "COALESCE(SUM(tokens_spent), 0) AS tokens_total "
        "FROM machine_reviews WHERE created_at >= datetime('now', ?) "
        "GROUP BY harness_skill, harness_version "
        "ORDER BY harness_skill, harness_version",
        (since,),
    )

    category_rows = await db.execute_fetchall(
        "SELECT COALESCE(json_extract(f.value, '$.category'), '') AS category, "
        "COUNT(*) AS findings, COUNT(DISTINCT mr.task_id) AS tasks "
        "FROM machine_reviews mr, json_each(mr.findings_confirmed) f "
        "WHERE mr.created_at >= datetime('now', ?) "
        "GROUP BY category HAVING category != '' "
        "ORDER BY findings DESC LIMIT 50",
        (since,),
    )
    recurring = [dict(r) | {"recurring": r["tasks"] > 1} for r in category_rows]

    cycle_rows = await db.execute_fetchall(
        "SELECT work_type, "
        "(julianday(updated_at) - julianday(ready_at)) * 24.0 AS hours "
        "FROM tasks WHERE status='completed' AND ready_at IS NOT NULL "
        "AND updated_at >= datetime('now', ?)",
        (since,),
    )
    by_type: dict[str, list[float]] = {}
    for r in cycle_rows:
        if r["hours"] is not None and r["hours"] >= 0:
            by_type.setdefault(r["work_type"] or "feature", []).append(r["hours"])
    cycle_times = [
        {
            "work_type": wt,
            "tasks": len(hours),
            "median_hours": round(statistics.median(hours), 2),
        }
        for wt, hours in sorted(by_type.items())
    ]

    return {
        "since_days": since_days,
        "machine_reviews": totals,
        "by_harness": [dict(r) for r in harness_rows],
        "recurring_categories": recurring,
        "cycle_times": cycle_times,
    }


async def provision_project(
    db: aiosqlite.Connection, project_id: int, *, actor: str = ""
) -> dict[str, str]:
    """Clone/verify a project workspace and record the outcome (#347).

    Never raises for git failures — the outcome lands in
    ``provision_status``/``provision_detail`` so the operator can read
    WHY instead of getting a 500. Missing repo/workspace are provision
    errors too, not validation errors: the button must always answer.
    """
    row = await repo.get_project(db, project_id)
    if row is None:
        return {"provision_status": "error", "provision_detail": "project not found"}
    project = dict(row)
    if not (project.get("repo") or "").strip():
        ok, detail = False, "project has no repo configured"
    elif not (project.get("workspace_path") or "").strip():
        ok, detail = False, "project has no workspace_path configured"
    else:
        ok, detail = await plugins.git_ops.clone_repo(
            project["repo"].strip(),
            project["workspace_path"].strip(),
            (project.get("default_branch") or "develop").strip() or "develop",
        )
    status = "ok" if ok else "error"
    await repo.update_project(
        db, project_id, provision_status=status, provision_detail=detail[:1000]
    )
    await repo.insert_event(
        db,
        kind="project_provisioned",
        project_id=project_id,
        actor=actor or "hub",
        payload={"status": status, "slug": project.get("slug", "")},
    )
    await db.commit()
    await log_activity(
        db,
        "project_provisioned",
        f"Project {project.get('slug', project_id)} provision: {status} — {detail[:200]}",
    )
    return {"provision_status": status, "provision_detail": detail}


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


def worktree_per_task_enabled() -> bool:
    """Opt-in git-worktree isolation for pair tasks (#459).

    Default OFF keeps the single-working-tree behavior of #451/#457 unchanged
    in production; set OPENCLAW_WORKTREE_PER_TASK=1 to give each task its own
    worktree so concurrent pair-starts never share a working tree.
    """
    return os.environ.get("OPENCLAW_WORKTREE_PER_TASK") == "1"


def _slug_for_task(task_id: int, task: dict[str, Any], branch_slug: str) -> str:
    """Reuse an existing task branch's slug so its worktree stays stable (#459)."""
    slug = (branch_slug or "").strip()
    if slug:
        return slug
    existing = (task.get("branch") or "").strip()
    prefix = f"task-{task_id}/"
    if existing.startswith(prefix):
        return existing[len(prefix) :]
    return ""


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
    if worktree_per_task_enabled():
        return await plugins.git_ops.pair_prepare_worktree(
            task_id,
            task["title"],
            branch_slug=_slug_for_task(task_id, task, branch_slug),
            **local_kw,
        )
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


async def restore_pair_workspace_base(
    db: aiosqlite.Connection,
    task_id: int,
) -> None:
    """Best-effort cleanup after a pair task leaves running (#451/#459).

    Worktree mode (#459): remove the task's worktree. Legacy mode: return the
    single working tree to its base branch. Either way the main clone ends on base.
    """
    ctx = await project_git_context(db, task_id)
    local_kw, _ = _split_git_kwargs(ctx)
    if worktree_per_task_enabled():
        await plugins.git_ops.pair_remove_worktree(task_id, **local_kw)
        return
    await plugins.git_ops.pair_restore_workspace_base(task_id, **local_kw)


async def switch_pair_workspace_to_task(
    db: aiosqlite.Connection,
    task_id: int,
) -> None:
    """Best-effort: make the task branch available for rework after CHANGES_REQUESTED.

    Worktree mode (#459): re-create the task's worktree (removed on submit) so
    fixes land there. Legacy mode (#457): switch the single working tree to the
    task branch instead of the local base restored by #451.
    """
    row = await repo.get_task(db, task_id)
    task = dict(row) if row else {}
    branch = (task.get("branch") or "").strip()
    if not branch:
        return
    ctx = await project_git_context(db, task_id)
    local_kw, _ = _split_git_kwargs(ctx)
    if worktree_per_task_enabled():
        await plugins.git_ops.pair_prepare_worktree(
            task_id,
            task.get("title") or "",
            branch_slug=_slug_for_task(task_id, task, ""),
            **local_kw,
        )
        return
    await plugins.git_ops.pair_switch_to_task_branch(task_id, branch, **local_kw)


# Statuses whose branch is active-but-unmerged: work in progress or waiting
# for a review verdict. Stacking on top of such a branch is what incident
# #392 produced (#424→#425→#426 on top of unmerged task-392).
STACK_ADVISORY_STATUSES = ["running", "review"]


async def detect_branch_stacking(
    db: aiosqlite.Connection,
    task_id: int,
    branch: str,
) -> dict[str, Any] | None:
    """Advisory branch-stacking detection at submission time (#438).

    Checks — via the project's git repo — whether ``branch`` contains
    commits of ANOTHER unmerged task branch in running/review status.
    Returns ``{"base_task_id", "base_task_branch", "base_task_status",
    "message"}`` for the first stacked base found, or None.

    Advisory by design: a stack can be a deliberate decision, so this never
    blocks and never raises. Graceful degradation: no branch, no plugin
    support, or any git failure silently skips the check.
    """
    branch = (branch or "").strip()
    if not branch:
        return None
    checker = getattr(plugins.git_ops, "branch_contains_unmerged_commits_of", None)
    if checker is None:
        return None

    ctx = await project_git_context(db, task_id)
    base = ctx.get("base_branch") or config.PAIR_BASE_BRANCH
    repo_path = ctx.get("repo")
    rows = await repo.list_unmerged_branch_tasks(
        db, exclude_task_id=task_id, statuses=STACK_ADVISORY_STATUSES
    )
    for row in rows:
        other = dict(row)
        other_branch = (other.get("branch") or "").strip()
        if not other_branch or other_branch == branch:
            continue
        try:
            stacked = await checker(
                branch, other_branch, base_branch=base, repo=repo_path
            )
        except Exception:  # noqa: BLE001 — advisory only; never break the caller
            log.debug(
                "branch stacking check skipped for #%d (%s vs %s)",
                task_id,
                branch,
                other_branch,
                exc_info=True,
            )
            return None
        if stacked:
            other_id = other["id"]
            other_status = other.get("status") or ""
            message = (
                f"ADVISORY branch stacking: '{branch}' contains unmerged "
                f"commits of task #{other_id} branch '{other_branch}' "
                f"(status: {other_status}). This branch cannot be verified "
                f"against '{base}' on its own and the merge order is "
                f"implicit. Alternatives: wait for task #{other_id} to merge "
                f"into '{base}', rebase, and resubmit — or, if the stack is "
                f"deliberate, merge task #{other_id}'s branch first and "
                f"state the merge order explicitly."
            )
            return {
                "base_task_id": other_id,
                "base_task_branch": other_branch,
                "base_task_status": other_status,
                "message": message,
            }
    return None


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
        and not review_budget_exhausted(task.get("review_cycle", 0))
        and has_done
        and branch
    ):
        ctx = await project_git_context(db, task_id)
        workspace = ctx.get("repo")
        # Worktree mode (#459): a PAIR task's branch is checked out in its own
        # worktree while the main clone stays on base; targeting the main clone
        # would silently fail checkout and let squash_branch reset the base
        # branch. Only redirect for pair tasks (no job_id) whose worktree
        # actually exists — headless dispatch tasks (job_id set) build their
        # branch in the main clone and never create a worktree, so redirecting
        # them at a nonexistent path would crash the poller's done-pipeline.
        git_repo = workspace
        if worktree_per_task_enabled() and not task.get("job_id"):
            wt = plugins.git_ops.worktree_path(task_id, workspace)
            if wt and os.path.isdir(wt):
                git_repo = wt
        await plugins.git_ops.checkout(branch, repo=git_repo)
        await plugins.git_ops.auto_commit(
            task_id, title=task.get("title", ""), repo=git_repo
        )
        squashed = await plugins.git_ops.squash_branch(
            task_id,
            task.get("title", ""),
            branch,
            repo=git_repo,
        )
        await plugins.git_ops.push_branch(branch, repo=git_repo, force=squashed)
        if not task.get("pr_number"):
            pr_num = await plugins.git_ops.create_pr(
                task_id,
                task["title"],
                task.get("description", ""),
                branch,
                repo=git_repo,
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

    if has_done and review_budget_exhausted(task.get("review_cycle", 0)):
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


def review_budget_exhausted(review_cycle: int, max_cycles: int | None = None) -> bool:
    """Whether the review fix budget is spent (#423) — one source of truth.

    ``review_cycle`` is the number of developer fix iterations already
    dispatched. The budget is exhausted — the next CHANGES_REQUESTED escalates
    (to arbiter / needs_decision) instead of dispatching another fix — once that
    count reaches ``max_cycles``. Pair and headless share this, so at MAX=3 both
    run fixes 1, 2 and 3 and escalate the 4th. ``MAX <= 0`` is exhausted
    immediately. No flow may compare review_cycle to MAX_REVIEW_CYCLES itself.
    """
    if max_cycles is None:
        max_cycles = config.MAX_REVIEW_CYCLES
    return review_cycle >= max_cycles


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
    """Dispatch an arbiter (Claude Sonnet) when review cycle limit is reached.

    At-most-once per submission generation (#421): a conditional claim persists
    a ``dispatching`` marker BEFORE the external submit, so a repeat poll or a
    restart finds it and never dispatches a second paid job. The marker moves to
    ``running`` with the job id on success; a crash between submit and job id
    leaves ``dispatching`` for the poller's ambiguity watchdog to resolve.
    """
    task_id = task["id"]
    generation = task.get("submission_generation") or 0

    claimed = await repo.claim_arbiter_dispatch(db, task_id, generation)
    if not claimed:
        await db.commit()
        log.info(
            "Poll: task #%d arbiter already claimed for generation %d, skipping",
            task_id,
            generation,
        )
        return
    # The marker must be durable before the external side effect.
    await db.commit()

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
        await repo.mark_arbiter_running(db, task_id, arbiter_job_id)
        await repo.update_task(
            db, task_id, status="review", review_job_id=arbiter_job_id
        )
        log.info("Poll: task #%d → arbiter review (job=%s)", task_id, arbiter_job_id)
    else:
        # A definite submit failure (the call returned, no job id): finish the
        # marker and escalate. Do not leave it dispatching — that is only for
        # the crash window where the call never returned.
        log.warning(
            "Poll: failed to dispatch arbiter for task #%d: %s",
            task_id,
            result.get("error"),
        )
        await repo.update_task(
            db, task_id, status="needs_decision", arbiter_state="finished"
        )
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
