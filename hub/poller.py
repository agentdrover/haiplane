"""Background poller: sync running/review/ci_check tasks with dispatch job status."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterator
from typing import Any
import logging
from datetime import UTC, datetime

from fastapi import FastAPI

from hub import config, lifecycle_matrix, services
from hub import repository as repo
from hub.db import fetchall, log_activity
from hub.integrations.git_ops import WorkspaceNotReadyError
from hub.integrations.protocols import CIProbeOutcome
from hub.integrations.registry import plugins

log = logging.getLogger("hub")

# Last refusal reported per project by the release sweep (#812). Kept in memory
# on purpose: it exists to avoid repeating the same line every cycle, and a
# restart repeating one line is cheaper than a table nobody reads.
_release_notices: dict[str, str] = {}

POLL_INTERVAL = 30  # seconds

CI_GRACE_PERIOD = 180  # wait >=3 min after push before checking CI

MAX_CI_NO_PR_ATTEMPTS = 3  # give up creating a PR after this many polls


def _seconds_since(iso_ts: str | None) -> float | None:
    """Seconds elapsed since a stored ``datetime('now')`` timestamp (#416).

    CI push time now lives in the row (``ci_check_started_at``) as a naive-UTC
    ``YYYY-MM-DD HH:MM:SS`` string, so the grace period is measured from the
    real push and survives a restart. Returns ``None`` when unset/unparseable.
    """
    if not iso_ts:
        return None
    try:
        started = datetime.strptime(iso_ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    except ValueError:
        return None
    return (datetime.now(UTC) - started).total_seconds()


async def _handle_missing_job(db, task: dict, *, reason: str) -> None:
    """Grace-then-escalate for a headless task whose dispatch job is gone (#417).

    A missing job used to be a silent ``continue`` — the task then sat in a
    machine status forever. Now the first miss stamps a durable clock; once the
    grace passes the task escalates once to needs_decision with a machine
    reason. The clock lives in the row, so the decision survives a restart and
    matches a continuous run. Pair-running and client-driven review are never
    routed here — they carry no job id and are excluded by the selection.
    """
    elapsed = _seconds_since(task.get("job_missing_since"))
    if elapsed is None:
        await repo.mark_job_missing(db, task["id"])
        await db.commit()
        return
    if elapsed < config.MISSING_JOB_GRACE_MINUTES * 60:
        return
    await repo.add_task_update(
        db,
        task["id"],
        "hub",
        "alert",
        "Dispatch job missing beyond grace period. Manual decision required.",
    )
    await repo.update_task(db, task["id"], status="needs_decision")
    await repo.insert_event(
        db,
        kind="needs_decision",
        task_id=task["id"],
        actor="hub",
        payload={"reason": reason},
    )
    await repo.clear_job_missing(db, task["id"])
    await db.commit()
    log.warning(
        "Poll: task #%d dispatch job missing beyond grace → needs_decision",
        task["id"],
    )
    await services.maybe_destroy_vast(db, task)


@contextlib.contextmanager
def _task_isolation(stage: str, task_id: Any) -> Iterator[None]:
    """One task's failure must not skip the rest of the tick (#363 I5).

    The whole sweep sits inside a single ``try/except Exception``, so any
    exception — a timed-out git call was the reported cause — abandoned every
    later stage of that tick: review, ci_check, stale sweeps, claim expiry. And
    it did so again every tick until the cause cleared.

    Swallowing here is deliberate and narrow. The poller re-reads state from the
    database on every tick and is idempotent by design, so a task skipped once
    is retried in seconds. ``CancelledError`` derives from ``BaseException`` and
    is not caught, so shutdown still stops the loop promptly.
    """
    try:
        yield
    except Exception:
        log.exception("Poll: %s failed for task #%s — tick continues", stage, task_id)


async def _poll_running_tasks(app: FastAPI) -> None:
    """Background task: sync running/review/fix_requested tasks with dispatch job status."""
    while True:
        await asyncio.sleep(POLL_INTERVAL)
        try:
            db = app.state.db

            rows = await repo.list_running_dispatchable(db)
            for row in rows:
                with _task_isolation("running", dict(row).get("id")):
                    task = dict(row)
                    job = plugins.dispatch.get_job(task["job_id"])
                    if not job:
                        await _handle_missing_job(
                            db, task, reason="dispatch_job_missing"
                        )
                        continue
                    if task.get("job_missing_since"):
                        await repo.clear_job_missing(db, task["id"])
                        await db.commit()
                    job_status = job.get("status")
                    if job_status not in ("completed", "failed"):
                        continue

                    if job_status == "failed":
                        await repo.update_task(
                            db,
                            task["id"],
                            status="failed",
                            exit_code=job.get("exit_code"),
                            result_text=job.get("result_text"),
                        )
                        await db.commit()
                        log.info(
                            "Poll: task #%d → failed (exit=%s)",
                            task["id"],
                            job.get("exit_code"),
                        )
                        await services.maybe_destroy_vast(db, task)
                        continue

                    has_done = await repo.has_done_updates(db, task["id"])
                    has_blocker = any(
                        u.get("kind") == "blocker"
                        for u in [
                            dict(r) for r in await repo.get_task_updates(db, task["id"])
                        ]
                    )
                    if has_blocker:
                        await repo.update_task(
                            db,
                            task["id"],
                            status="needs_decision",
                            exit_code=job.get("exit_code"),
                            result_text=job.get("result_text"),
                        )
                        await repo.insert_event(
                            db,
                            kind="needs_decision",
                            task_id=task["id"],
                            actor="hub",
                            payload={"reason": "blocker_reported"},
                        )
                        await db.commit()
                        log.info(
                            "Poll: task #%d → needs_decision (blocker reported)",
                            task["id"],
                        )
                        await services.maybe_destroy_vast(db, task)
                        continue
                    if not has_done and job_status == "completed":
                        summary = _extract_agent_summary(
                            plugins.dispatch.job_log_full(task["job_id"])
                        )
                        if summary:
                            await repo.add_task_update(
                                db, task["id"], "agent", "done", summary
                            )
                            await db.commit()
                            has_done = True
                            log.info(
                                "Poll: task #%d — synthetic done from dispatch log",
                                task["id"],
                            )
                    next_status = await services.transition_after_agent_done(
                        db,
                        task,
                        has_done=has_done,
                        exit_code=job.get("exit_code"),
                        result_text=job.get("result_text"),
                    )
                    if next_status == "ci_check":
                        await repo.mark_ci_check_started(db, task["id"])
                    log.info(
                        "Poll: task #%d → %s (exit=%s)",
                        task["id"],
                        next_status,
                        job.get("exit_code"),
                    )
                    await db.commit()
                    await services.maybe_destroy_vast(db, task)

            review_rows = await repo.list_review_tasks(db)
            for row in review_rows:
                with _task_isolation("review", dict(row).get("id")):
                    task = dict(row)
                    job = plugins.dispatch.get_job(task["review_job_id"])
                    if not job:
                        await _handle_missing_job(db, task, reason="review_job_missing")
                        continue
                    if task.get("job_missing_since"):
                        await repo.clear_job_missing(db, task["id"])
                        await db.commit()
                    job_status = job.get("status")
                    if job_status not in ("completed", "failed"):
                        continue

                    updates_rows = await repo.get_task_updates(db, task["id"])
                    updates_list = [dict(r) for r in updates_rows]

                    # Server-owned arbiter termination (#422): when the current
                    # review job IS the arbiter job, the Hub — not a voluntary
                    # agent update — ends the arbiter phase. Any terminal state
                    # routes to the human Decision Gate with an audit summary taken
                    # from the job result/log; a free-text APPROVED never
                    # auto-completes. This replaces the old kind="arbitration"
                    # inference (and the never-matching last_rework_at filter).
                    is_arbiter_job = bool(
                        task.get("arbiter_state") == "running"
                        and task.get("arbiter_job_id")
                        and task.get("arbiter_job_id") == task.get("review_job_id")
                    )
                    if is_arbiter_job:
                        summary = (job.get("result_text") or "").strip()
                        if not summary:
                            summary = _extract_agent_summary(
                                plugins.dispatch.job_log_full(task["review_job_id"])
                            )
                        reason = (
                            "arbiter_job_failed"
                            if job_status == "failed"
                            else "arbitration_finished"
                        )
                        await repo.mark_arbiter_finished(db, task["id"])
                        await repo.update_task(db, task["id"], status="needs_decision")
                        await repo.insert_event(
                            db,
                            kind="needs_decision",
                            task_id=task["id"],
                            actor="hub",
                            payload={"reason": reason},
                        )
                        await db.commit()
                        await repo.add_task_update(
                            db,
                            task["id"],
                            "hub",
                            "alert",
                            "Arbiter phase finished — human decision required "
                            "(hub_decide_task)."
                            + (f"\n\nArbiter summary:\n{summary}" if summary else ""),
                        )
                        await db.commit()
                        log.info(
                            "Poll: task #%d arbiter %s → needs_decision",
                            task["id"],
                            job_status,
                        )
                        await services.maybe_destroy_vast(db, task)
                        continue

                    if job_status == "failed":
                        # Universal Review Gate (#309): a crashed review job must
                        # never complete the task — no verdict exists.
                        await repo.update_task(db, task["id"], status="needs_decision")
                        await repo.insert_event(
                            db,
                            kind="needs_decision",
                            task_id=task["id"],
                            actor="hub",
                            payload={"reason": "review_job_failed"},
                        )
                        await db.commit()
                        await repo.add_task_update(
                            db,
                            task["id"],
                            "hub",
                            "alert",
                            f"Review job failed (exit={job.get('exit_code')}) "
                            "without a verdict. Universal Review Gate: manual "
                            "decision required (hub_decide_task).",
                        )
                        await db.commit()
                        log.info(
                            "Poll: review job failed for task #%d → needs_decision",
                            task["id"],
                        )
                        await services.maybe_destroy_vast(db, task)
                        continue

                    # Structured channel first (#326): a persisted verdict for
                    # the CURRENT submission wins; text scanning stays as the
                    # fallback for legacy reviewers and dispatch logs.
                    persisted = task.get("review_verdict")
                    if persisted and task.get("review_verdict_generation") == task.get(
                        "submission_generation"
                    ):
                        verdict = persisted
                        log.info(
                            "Poll: task #%d verdict '%s' from persisted review state",
                            task["id"],
                            verdict,
                        )
                    else:
                        verdict = services.extract_review_verdict(
                            task["id"], task["review_job_id"], updates_list
                        )

                    if verdict:
                        # Canonical verdict state (#305): bind the verdict to the
                        # current submission generation so a later resubmission
                        # invalidates this approval.
                        await repo.record_review_verdict(db, task["id"], verdict)

                    if verdict == "approved":
                        pr_num = task.get("pr_number")
                        branch = task.get("branch")
                        merged = False
                        if pr_num:
                            mctx = await services.project_git_context(db, task["id"])
                            mworkspace = mctx.get("repo")
                            mgh_repo = mctx.get("gh_repo")
                            ci = await plugins.git_ops.check_pr_ci(
                                pr_num, repo=mworkspace, gh_repo=mgh_repo
                            )
                            if ci.outcome == CIProbeOutcome.passed:
                                merged = await plugins.git_ops.merge_pr(
                                    pr_num,
                                    task["id"],
                                    task["title"],
                                    repo=mworkspace,
                                    gh_repo=mgh_repo,
                                )
                                if merged:
                                    # Record the commit the merge produced, not
                                    # the PR number: the number lives in the
                                    # subject the pusher writes, the SHA does
                                    # not. Project resolved through the task's
                                    # epic rather than its own column, so a
                                    # task without one cannot record a merge
                                    # that counts for every project (#534).
                                    merge_sha = ""
                                    try:
                                        merge_sha = (
                                            await plugins.git_ops.merge_commit_sha(
                                                pr_num,
                                                repo=mworkspace,
                                                gh_repo=mgh_repo,
                                            )
                                        )
                                    except Exception:
                                        log.exception(
                                            "could not read the merge commit "
                                            "for task #%s; the drift guard "
                                            "will flag it once",
                                            task["id"],
                                        )
                                    proj = await repo.resolve_project_for_task(
                                        db, task["id"]
                                    )
                                    await repo.record_pipeline_merge(
                                        db,
                                        pr_number=pr_num,
                                        merge_sha=merge_sha,
                                        project_id=(dict(proj)["id"] if proj else None),
                                        task_id=task["id"],
                                    )
                                    mbase = mctx.get("base_branch")
                                    # Post-merge tidying, not delivery: the work
                                    # is already merged, so a workspace we
                                    # cannot return to base is reported and
                                    # left to a human rather than failing the
                                    # task (#552).
                                    try:
                                        await plugins.git_ops.pull_main(
                                            repo=mworkspace, base_branch=mbase
                                        )
                                        if branch:
                                            await plugins.git_ops.delete_branch(
                                                branch,
                                                repo=mworkspace,
                                                base_branch=mbase,
                                            )
                                    except WorkspaceNotReadyError as exc:
                                        log.warning(
                                            "Poll: task #%d merged, workspace not"
                                            " tidied: %s",
                                            task["id"],
                                            exc,
                                        )
                                        await repo.add_task_update(
                                            db,
                                            task["id"],
                                            "hub",
                                            "alert",
                                            f"PR #{pr_num} влит, но рабочий "
                                            f"каталог не возвращён на базовую "
                                            f"ветку: {exc}",
                                        )
                                    log.info(
                                        "Poll: task #%d PR #%d merged on GitHub",
                                        task["id"],
                                        pr_num,
                                    )
                            elif ci.outcome == CIProbeOutcome.failed:
                                log.warning(
                                    "Poll: task #%d CI failed on PR #%d",
                                    task["id"],
                                    pr_num,
                                )
                                await repo.add_task_update(
                                    db,
                                    task["id"],
                                    "hub",
                                    "alert",
                                    f"CI failed on PR #{pr_num}. Manual check required.",
                                )
                            else:
                                log.info(
                                    "Poll: task #%d CI %s on PR #%d, will retry",
                                    task["id"],
                                    ci.outcome.value,
                                    pr_num,
                                )
                                continue
                        if pr_num and not merged:
                            # #363. An approved verdict is not delivery. The
                            # branch below completes the task, and it used to
                            # run even when the merge had not happened — red CI
                            # only added an alert, and a merge_pr that GitHub
                            # refused set merged=False and was never read. The
                            # task then read "completed" while its work sat
                            # unmerged in a branch, which is the one thing the
                            # status is supposed to rule out. Pending probes
                            # already `continue` above and retry; reaching here
                            # without a merge means a human has to look.
                            reason = (
                                "ci_failed"
                                if ci.outcome == CIProbeOutcome.failed
                                else "merge_failed"
                            )
                            await repo.add_task_update(
                                db,
                                task["id"],
                                "hub",
                                "blocker",
                                f"Ревью одобрено, но PR #{pr_num} не влит "
                                f"({reason}). Задача не может считаться "
                                "выполненной, пока работа не в базовой ветке. "
                                "Решите через hub_decide_task.",
                            )
                            await repo.update_task(
                                db, task["id"], status="needs_decision"
                            )
                            await repo.insert_event(
                                db,
                                kind="needs_decision",
                                task_id=task["id"],
                                actor="hub",
                                payload={"reason": reason, "pr": pr_num},
                            )
                            await db.commit()
                            log.warning(
                                "Poll: task #%d approved but PR #%d not merged (%s)"
                                " → needs_decision",
                                task["id"],
                                pr_num,
                                reason,
                            )
                            await services.maybe_destroy_vast(db, task)
                            continue
                        if not merged and not pr_num:
                            log.info("Poll: task #%d approved (no PR)", task["id"])
                        # Converge on the same gate-checked completion used by
                        # pair done reports (#309): the verdict recorded above
                        # makes completion_requires_review false, so the shared
                        # transition completes without bumping the generation.
                        refreshed_row = await repo.get_task(db, task["id"])
                        refreshed = dict(refreshed_row) if refreshed_row else task
                        await services.transition_after_agent_done(
                            db, refreshed, has_done=True
                        )
                        await db.commit()
                        log.info("Poll: task #%d review → approved", task["id"])
                        await services.maybe_destroy_vast(db, task)
                    elif verdict == "changes_requested":
                        review_text = ""
                        for u in reversed(updates_list):
                            if u.get("kind") == "review":
                                review_text = u.get("content", "")
                                break
                        if not review_text:
                            full_log = plugins.dispatch.job_log_full(
                                task["review_job_id"]
                            )
                            if full_log:
                                review_text = _extract_review_from_log(full_log)
                        if not review_text:
                            review_text = (
                                "Ревьюер запросил изменения, но конкретные замечания "
                                "не удалось извлечь. Проверь git diff и исправь проблемы."
                            )

                        if services.review_budget_exhausted(
                            task.get("review_cycle", 0)
                        ):
                            await repo.update_task(
                                db,
                                task["id"],
                                review_cycle=task.get("review_cycle", 0) + 1,
                            )
                            await db.commit()
                            await services.dispatch_arbiter(db, task, updates_list)
                        else:
                            branch = task.get("branch")
                            if branch:
                                await plugins.git_ops.checkout(branch)
                            await services.dispatch_fix(db, task, review_text)
                    else:
                        log.warning(
                            "Poll: task #%d review job done but no clear verdict → needs_decision",
                            task["id"],
                        )
                        await repo.add_task_update(
                            db,
                            task["id"],
                            "hub",
                            "alert",
                            "Review job completed but no clear verdict (APPROVED/CHANGES_REQUESTED). "
                            "Manual decision required.",
                        )
                        await repo.update_task(db, task["id"], status="needs_decision")
                        await repo.insert_event(
                            db,
                            kind="needs_decision",
                            task_id=task["id"],
                            actor="hub",
                            payload={"reason": "no_clear_verdict"},
                        )
                        await db.commit()
                        await services.maybe_destroy_vast(db, task)

            ci_rows = await repo.list_ci_check_tasks(db)
            for row in ci_rows:
                with _task_isolation("ci_check", dict(row).get("id")):
                    task = dict(row)
                    ctx = await services.project_git_context(db, task["id"])
                    workspace = ctx.get("repo")
                    if not task.get("pr_number"):
                        branch = task.get("branch")
                        if branch:
                            await plugins.git_ops.push_branch(
                                branch, repo=workspace, force=True
                            )
                            pr_num = await plugins.git_ops.create_pr(
                                task["id"],
                                task["title"],
                                task.get("description", ""),
                                branch,
                                repo=workspace,
                                gh_repo=ctx.get("gh_repo"),
                                base_branch=ctx.get("base_branch"),
                            )
                            if pr_num:
                                await repo.update_task(db, task["id"], pr_number=pr_num)
                                task["pr_number"] = pr_num
                                await repo.mark_ci_check_started(db, task["id"])
                                await db.commit()
                                log.info(
                                    "Poll: task #%d created PR #%d (was missing)",
                                    task["id"],
                                    pr_num,
                                )
                        if not task.get("pr_number"):
                            attempts = await repo.increment_ci_no_pr_attempts(
                                db, task["id"]
                            )
                            if attempts >= MAX_CI_NO_PR_ATTEMPTS:
                                log.warning(
                                    "Poll: task #%d ci_check without PR after %d retries → needs_decision",
                                    task["id"],
                                    MAX_CI_NO_PR_ATTEMPTS,
                                )
                                await repo.add_task_update(
                                    db,
                                    task["id"],
                                    "hub",
                                    "alert",
                                    "Cannot create PR: no commits on branch or push failed. Manual decision required.",
                                )
                                await repo.update_task(
                                    db, task["id"], status="needs_decision"
                                )
                                await repo.reset_ci_check_state(db, task["id"])
                                await db.commit()
                                await services.maybe_destroy_vast(db, task)
                            else:
                                await db.commit()
                            continue
                    elapsed = _seconds_since(task.get("ci_check_started_at"))
                    if elapsed is not None and elapsed < CI_GRACE_PERIOD:
                        log.debug(
                            "Poll: task #%d CI grace period (%ds remaining)",
                            task["id"],
                            int(CI_GRACE_PERIOD - elapsed),
                        )
                        continue
                    ci = await plugins.git_ops.check_pr_ci(
                        task["pr_number"],
                        repo=workspace,
                        gh_repo=ctx.get("gh_repo"),
                    )
                    if ci.outcome == CIProbeOutcome.pending:
                        continue
                    if ci.outcome == CIProbeOutcome.unavailable:
                        # The probe could not be read. Keep the diagnostic and retry;
                        # the #418 deadline backstop escalates if it persists (#419).
                        log.warning(
                            "Poll: task #%d CI probe unavailable (%s), will retry",
                            task["id"],
                            ci.reason,
                        )
                        continue
                    await repo.reset_ci_check_state(db, task["id"])
                    if ci.outcome == CIProbeOutcome.absent:
                        # A PR with no checks is a definite answer, not a wait: skip
                        # the CI conveyor and go straight to review (#419).
                        await repo.add_task_update(
                            db,
                            task["id"],
                            "hub",
                            "status",
                            "CI checks absent on PR — skipping CI, dispatching review.",
                        )
                        log.info(
                            "Poll: task #%d CI absent on PR #%s, dispatching review",
                            task["id"],
                            task.get("pr_number"),
                        )
                        await services.dispatch_review(db, task)
                    elif ci.outcome == CIProbeOutcome.passed:
                        log.info(
                            "Poll: task #%d CI passed on PR #%s, dispatching review",
                            task["id"],
                            task.get("pr_number"),
                        )
                        await services.dispatch_review(db, task)
                    elif ci.outcome == CIProbeOutcome.failed:
                        ci_fix_cycle = task.get("ci_fix_cycle", 0)
                        if ci_fix_cycle < config.MAX_CI_FIX_CYCLES:
                            ci_details = await plugins.git_ops.get_ci_failure_logs(
                                task["pr_number"],
                                task.get("branch", ""),
                                repo=workspace,
                                gh_repo=ctx.get("gh_repo"),
                            )
                            log.info(
                                "Poll: task #%d CI failed (cycle %d/%d), dispatching CI fix",
                                task["id"],
                                ci_fix_cycle + 1,
                                config.MAX_CI_FIX_CYCLES,
                            )
                            await services.dispatch_ci_fix(db, task, ci_details)
                        else:
                            await repo.add_task_update(
                                db,
                                task["id"],
                                "hub",
                                "alert",
                                f"CI fix cycle limit reached ({ci_fix_cycle}/{config.MAX_CI_FIX_CYCLES}). "
                                "Manual intervention required.",
                            )
                            await repo.update_task(
                                db, task["id"], status="needs_decision"
                            )
                            await repo.insert_event(
                                db,
                                kind="needs_decision",
                                task_id=task["id"],
                                actor="hub",
                                payload={"reason": "ci_fix_cycle_limit"},
                            )
                            await db.commit()
                            log.info(
                                "Poll: task #%d CI fix cycle limit → needs_decision",
                                task["id"],
                            )
                            await services.maybe_destroy_vast(db, task)

            stale_rows = await repo.list_stale_running(
                db, config.STALE_THRESHOLD_MINUTES
            )
            for row in stale_rows:
                with _task_isolation("stale review", dict(row).get("id")):
                    task = dict(row)
                    if await repo.has_stale_alert(db, task["id"], "running"):
                        continue
                    await repo.add_task_update(
                        db,
                        task["id"],
                        "hub",
                        "alert",
                        "Task stale in running: no updates for "
                        f"{config.STALE_THRESHOLD_MINUTES}+ minutes.",
                    )
                    await db.commit()
                    await log_activity(
                        db,
                        "task_stale",
                        f"Task #{task['id']} has no updates for {config.STALE_THRESHOLD_MINUTES}+ min",
                    )
                    log.warning(
                        "Poll: task #%d is stale (no updates for %d+ min)",
                        task["id"],
                        config.STALE_THRESHOLD_MINUTES,
                    )

            # Stale watchdog for silent dead-end statuses (#319, #393). Only
            # client-driven review is watched (headless review belongs to
            # this conveyor); statuses never change here — alerts only. The
            # machine-owned dead-ends (ci_check, fix_requested, pending_report)
            # get visibility here until F2 lands durable deadline transitions.
            stale_specs = (
                (
                    "review",
                    config.STALE_REVIEW_MINUTES,
                    True,
                    "Awaiting review verdict: reviewer should run "
                    "hub_get_review_brief and hub_submit_review.",
                ),
                (
                    "claimed",
                    config.STALE_CLAIMED_MINUTES,
                    False,
                    "Claim held without pair start: call hub_pair_start or "
                    "hub_release_task.",
                ),
                (
                    "needs_info",
                    config.STALE_NEEDS_INFO_MINUTES,
                    False,
                    "Question awaits a human hub_answer_question.",
                ),
                (
                    "ci_check",
                    config.STALE_CI_CHECK_MINUTES,
                    False,
                    "CI conveyor stalled: inspect the PR/CI or recover with "
                    "hub_force_complete_task.",
                ),
                (
                    "fix_requested",
                    config.STALE_FIX_REQUESTED_MINUTES,
                    False,
                    "Fix dispatch stalled: inspect the job or recover with "
                    "hub_force_complete_task.",
                ),
                (
                    "pending_report",
                    config.STALE_PENDING_REPORT_MINUTES,
                    False,
                    "Awaiting agent hub_report_done, or recover with "
                    "hub_force_complete_task.",
                ),
            )
            for status_name, threshold, null_review_job, action in stale_specs:
                rows = await repo.list_stale_tasks(
                    db,
                    status_name,
                    threshold,
                    require_null_review_job=null_review_job,
                )
                for row in rows:
                    with _task_isolation("stale sweep", dict(row).get("id")):
                        task = dict(row)
                        if await repo.has_stale_alert(db, task["id"], status_name):
                            continue
                        await repo.add_task_update(
                            db,
                            task["id"],
                            "hub",
                            "alert",
                            f"Task stale in {status_name}: no updates for "
                            f"{threshold}+ minutes. {action}",
                        )
                        await db.commit()
                        await log_activity(
                            db,
                            "task_stale",
                            f"Task #{task['id']} stale in {status_name} "
                            f"for {threshold}+ min",
                        )
                        log.warning(
                            "Poll: task #%d is stale in %s (no updates for %d+ min)",
                            task["id"],
                            status_name,
                            threshold,
                        )

            # Unrefined drafts (#751): a draft the DoR gate would refuse is a
            # quiet dead end — approval 422s, batch approve silently skips it,
            # and the author finds out only when the owner hits the button.
            # One alert per draft (has_stale_alert dedup, same as above);
            # a draft brought to DoR after the alert is left alone.
            unrefined = await fetchall(
                db,
                "SELECT id, assigned_agent FROM tasks "
                "WHERE status='draft' AND archived=0 "
                "AND (dor_passed IS NULL OR dor_passed=0) "
                "AND created_at <= datetime('now', ?)",
                (f"-{config.UNREFINED_DRAFT_MINUTES} minutes",),
            )
            for row in unrefined:
                with _task_isolation("unrefined draft sweep", dict(row).get("id")):
                    task = dict(row)
                    if await repo.has_stale_alert(db, task["id"], "draft"):
                        continue
                    from hub.services.recommendations import (
                        calculate_readiness_with_recommendations,
                    )

                    readiness = await calculate_readiness_with_recommendations(
                        db, task["id"]
                    )
                    missing = ", ".join(readiness.missing_required) or "—"
                    await repo.add_task_update(
                        db,
                        task["id"],
                        "hub",
                        "alert",
                        f"Task stale in draft: DoR не пройден "
                        f"{config.UNREFINED_DRAFT_MINUTES}+ минут "
                        f"(missing: {missing}). hub_refine_task доведёт "
                        "постановку до готовности — без этого одобрение "
                        "владельцем невозможно (422 dor_failed).",
                    )
                    await db.commit()
                    await log_activity(
                        db,
                        "task_stale",
                        f"Task #{task['id']} stale in draft: DoR not passed "
                        f"for {config.UNREFINED_DRAFT_MINUTES}+ min",
                    )
                    log.warning(
                        "Poll: draft #%d unrefined for %d+ min (missing: %s)",
                        task["id"],
                        config.UNREFINED_DRAFT_MINUTES,
                        missing,
                    )

            # Autopilot digests (#739): one per project per UTC day of
            # autopilot activity. Idempotent via the UNIQUE key, so every
            # poll pass may try; a failure must not kill the loop.
            try:
                from hub.services.digest import generate_due_digests

                await generate_due_digests(db)
            except Exception:  # noqa: BLE001 - oversight must not stop polling
                log.exception("autopilot digest generation failed")

            # Cross-model review dispatches (#757): settle finished cloud
            # reviewer runs — reports get their usage cross-check, silent
            # finishes fail loudly.
            try:
                from hub.services.review_dispatch import sweep_review_dispatches

                await sweep_review_dispatches(db)
            except Exception:  # noqa: BLE001 - the sweep must not kill the loop
                log.exception("review dispatch sweep failed")

            # Claim lease expiry (#417): a claim held past the lease without a
            # pair start is auto-released back to open so the task returns to
            # the queue instead of sitting owned by a dead session forever.
            # Status change makes this idempotent — an expired claim is only
            # seen once. Release does not dispatch; the task waits in open.
            expired_claims = await repo.list_expired_claims(
                db, config.CLAIM_LEASE_MINUTES
            )
            for row in expired_claims:
                with _task_isolation("claim expiry", dict(row).get("id")):
                    task = dict(row)
                    await repo.update_task(
                        db,
                        task["id"],
                        status="open",
                        claimed_by=None,
                        claim_session_id=None,
                        claimed_at=None,
                    )
                    await repo.insert_event(
                        db,
                        kind="claim_expired",
                        task_id=task["id"],
                        actor="hub",
                        payload={"reason": "claim_lease_expired"},
                    )
                    await db.commit()
                    log.info("Poll: task #%d claim lease expired → open", task["id"])

            # Machine-owned deadline backstop (#418): the ownership/deadline
            # matrix is the source of truth. Any machine-owned instance that
            # sits past its (generous) deadline is transitioned to
            # needs_decision once, so no status+discriminator combination can
            # stay stuck without an owner. claimed → open is handled above.
            for policy in lifecycle_matrix.machine_deadline_policies():
                if policy.escalation != "needs_decision":
                    continue
                if policy.deadline_config is None:
                    # Матрица требует конечный дедлайн у каждой machine-политики
                    # (см. инварианты в hub/lifecycle_matrix.py), но здесь это
                    # держалось на честном слове: getattr(config, None) упал бы
                    # на первом же нарушении инварианта.
                    continue
                threshold = getattr(config, policy.deadline_config)
                overdue = await repo.list_past_status_deadline(
                    db,
                    policy.status,
                    threshold,
                    require_job_id=policy.require_job_id,
                    require_review_job_id=policy.require_review_job_id,
                )
                for row in overdue:
                    with _task_isolation("deadline sweep", dict(row).get("id")):
                        task = dict(row)
                        await repo.add_task_update(
                            db,
                            task["id"],
                            "hub",
                            "alert",
                            f"{policy.instance} exceeded its {threshold}m deadline. "
                            "Manual decision required.",
                        )
                        await repo.update_task(db, task["id"], status="needs_decision")
                        await repo.insert_event(
                            db,
                            kind="needs_decision",
                            task_id=task["id"],
                            actor="hub",
                            payload={"reason": policy.reason},
                        )
                        await db.commit()
                        log.warning(
                            "Poll: task #%d past %s deadline (%dm) → needs_decision",
                            task["id"],
                            policy.instance,
                            threshold,
                        )
                        await services.maybe_destroy_vast(db, task)

            # Arbiter dispatch ambiguity (#421): a marker stuck in 'dispatching'
            # past the grace means submit started but the job id was never
            # recorded — a crash window. Fail safe to needs_decision rather than
            # risk a duplicate paid dispatch; never re-submit automatically.
            stale_arbiter = await repo.list_stale_arbiter_dispatching(
                db, config.ARBITER_DISPATCH_GRACE_MINUTES
            )
            for row in stale_arbiter:
                with _task_isolation("arbiter sweep", dict(row).get("id")):
                    task = dict(row)
                    await repo.add_task_update(
                        db,
                        task["id"],
                        "hub",
                        "alert",
                        "Arbiter dispatch ambiguous: submit started but no job id "
                        "recorded. Manual decision required.",
                    )
                    await repo.update_task(
                        db,
                        task["id"],
                        status="needs_decision",
                        arbiter_state="finished",
                    )
                    await repo.insert_event(
                        db,
                        kind="needs_decision",
                        task_id=task["id"],
                        actor="hub",
                        payload={"reason": "arbiter_dispatch_ambiguous"},
                    )
                    await db.commit()
                    log.warning(
                        "Poll: task #%d arbiter dispatch ambiguous → needs_decision",
                        task["id"],
                    )

            # Events feed retention (#349): the feed is a notification
            # channel, not an archive — activity_log keeps the history.
            pruned = await repo.prune_events(db, keep_days=14)
            if pruned:
                await db.commit()
                log.info("Poll: pruned %d events older than 14 days", pruned)

            # Session registry retention (#771): same reasoning as the feed —
            # the registry answers "who is around now", and a session with no
            # sign of life for weeks answers nothing.
            pruned_sessions = await repo.prune_agent_sessions(
                db, keep_days=config.SESSION_RETENTION_DAYS
            )
            if pruned_sessions:
                await db.commit()
                log.info(
                    "Poll: pruned %d agent sessions with no sign of life for %d days",
                    pruned_sessions,
                    config.SESSION_RETENTION_DAYS,
                )

            # Release policy (#812): projects that release by policy get their
            # open develop→main PR merged as soon as CI is green. A red or
            # missing CI is reported ONCE per reason — the poller walks this
            # every cycle, and a line per cycle is how a real signal gets muted
            # (#534). Deploy stays with CI on the merge, as before.
            try:
                from hub.services.release import merge_ready_release

                for project_row in await repo.list_projects(db):
                    merged, reason = await merge_ready_release(db, project_row)
                    slug = dict(project_row).get("slug") or "?"
                    if merged:
                        log.info("Poll: %s — %s", slug, reason)
                        _release_notices.pop(slug, None)
                    elif reason and _release_notices.get(slug) != reason:
                        _release_notices[slug] = reason
                        log.warning("Poll: %s — %s", slug, reason)
            except Exception:
                log.exception("Poll: release policy sweep failed")

            # Message retention (#773): the channel is for coordinating work in
            # flight, and the tasks themselves keep the record of what was done.
            pruned_messages = await repo.prune_agent_messages(
                db, keep_days=config.MESSAGE_RETENTION_DAYS
            )
            if pruned_messages:
                await db.commit()
                log.info(
                    "Poll: pruned %d messages older than %d days",
                    pruned_messages,
                    config.MESSAGE_RETENTION_DAYS,
                )

            # MCP usage telemetry retention (#780): the records answer "what
            # does the Agent API cost now", and the horizon is deliberately
            # longer than the longest report window so a 90-day report is
            # never quietly drawn from 90 days minus whatever was pruned.
            pruned_calls = await repo.prune_mcp_call_events(
                db, keep_days=config.MCP_TELEMETRY_RETENTION_DAYS
            )
            if pruned_calls:
                await db.commit()
                log.info(
                    "Poll: pruned %d MCP call records older than %d days",
                    pruned_calls,
                    config.MCP_TELEMETRY_RETENTION_DAYS,
                )

        except Exception:
            log.exception("Poll error")


def _extract_agent_summary(full_log: str | None) -> str:
    """Extract a short summary from a dispatch log when the agent didn't call oc-hub update."""
    if not full_log:
        return ""
    import json

    texts: list[str] = []
    for line in full_log.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        if '"text"' in stripped:
            try:
                value = stripped.split(":", 1)[1].strip().strip(",")
                parsed = json.loads(value)
                if isinstance(parsed, str) and len(parsed) > 30:
                    texts.append(parsed)
            except (json.JSONDecodeError, IndexError):
                pass
    if texts:
        return texts[-1][:1500]
    return ""


def _extract_review_from_log(full_log: str) -> str:
    """Extract reviewer's text from a dispatch log, skipping JSON metadata."""
    import json

    lines = full_log.split("\n")
    texts: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith('"text"'):
            try:
                value = stripped.split(":", 1)[1].strip().strip(",")
                parsed = json.loads(value)
                if isinstance(parsed, str) and len(parsed) > 20:
                    texts.append(parsed)
            except (json.JSONDecodeError, IndexError):
                pass
    if texts:
        return texts[-1][:2000]
    return ""


SESSION_CLEANUP_INTERVAL = 3600  # 1 hour


async def _session_reaper(app: FastAPI) -> None:
    """Periodically purge expired browser sessions and clean up the login rate limiter."""
    while True:
        await asyncio.sleep(SESSION_CLEANUP_INTERVAL)
        try:
            db = app.state.db
            cursor = await db.execute(
                "DELETE FROM browser_sessions WHERE expires_at < datetime('now') "
                "OR revoked_at IS NOT NULL"
            )
            deleted = cursor.rowcount
            await db.commit()
            if deleted:
                log.info("Session reaper: removed %d expired/revoked sessions", deleted)

            from hub.auth import login_limiter

            login_limiter._cleanup()
        except Exception:
            log.exception("Session reaper error")


DRIFT_CHECK_INTERVAL = 900  # 15 minutes
RED_BASE_CHECK_INTERVAL = 180  # 3 minutes


async def _drift_watch(app: FastAPI) -> None:
    """Run the base-branch drift check on a schedule (#534).

    Submission #1 shipped the checker with no caller at all — code and table
    in place, nothing ever running them. A guard nobody invokes is not a
    guard, and review caught it. This is the trigger.

    Its own interval rather than the 30s poll tick: each run fetches from the
    remote, and drift is not an emergency — noticing within the quarter hour
    is the point, not noticing instantly.
    """
    from hub.services import drift_guard

    while True:
        await asyncio.sleep(DRIFT_CHECK_INTERVAL)
        try:
            reports = await drift_guard.check_all_projects(app.state.db)
            for report in reports:
                if report.status == "unknown":
                    # Never silent: "could not check" is a state an operator
                    # has to be able to see, not an absence of news.
                    log.warning(
                        "drift check skipped for %s: %s",
                        report.project_slug,
                        report.reason,
                    )
        except Exception:
            log.exception("Drift watch error")


async def arm_workspace_hooks(db) -> list[tuple[str, str]]:
    """Point every existing workspace at its pre-push hook (#532).

    Arming inside clone_repo covered only the moment a workspace is created or
    re-provisioned by hand. Every workspace on this server already exists and
    nobody presses Provision in a normal week, so that path never ran and the
    release would have changed nothing — the same finding as the early return,
    one level up. Startup is the moment that does happen.

    Cheap and offline: reads git config, writes at most one key. A workspace
    whose repository carries no hook is left alone and said so.
    """
    from hub import git_policy
    from hub.services.project_policy import base_branch_of, release_base_of

    outcomes: list[tuple[str, str]] = []
    try:
        rows = await repo.list_projects(db)
    except Exception:  # noqa: BLE001 - never block startup
        log.exception("could not list projects to arm their pre-push hooks")
        return outcomes

    for row in rows:
        project = dict(row)
        workspace = (project.get("workspace_path") or "").strip()
        if not workspace or project.get("archived"):
            continue
        # #475: the hook protects the branches of THIS project. Startup is the
        # only moment the hub touches every existing workspace, so it is also
        # the only moment the recorded policy can be corrected after an owner
        # changes a project's default_branch.
        status = git_policy.activate_quietly(
            workspace,
            base_branch=base_branch_of(row),
            release_branch=release_base_of(row),
        )
        outcomes.append((project.get("slug") or "?", status.state))
        log.info(
            "pre-push hook in workspace of %s: %s (%s)",
            project.get("slug"),
            status.state,
            status.reason,
        )
    return outcomes


async def _red_base_watch(app: FastAPI) -> None:
    """Watch the base branch's own CI and announce a fresh breakage (#929).

    Its own interval, faster than the drift check: measured in #921, a red
    base stood for 8 hours 25 minutes because nobody was looking, while every
    branch taken from it inherited the red. Three minutes is the difference
    between "found by whoever was unlucky" and "known".

    Cheap by construction: one API call per project per tick, and nothing at
    all is written unless the base is red for a reason not already announced.
    """
    from hub.services import red_base

    while True:
        await asyncio.sleep(RED_BASE_CHECK_INTERVAL)
        try:
            for state in await red_base.check_all_projects(app.state.db):
                if state.status == red_base.UNKNOWN:
                    # Never silent: "could not look" is a state the operator
                    # has to see, not an absence of news (#725).
                    log.warning(
                        "base CI state unknown for %s: %s", state.branch, state.reason
                    )
        except Exception:
            log.exception("Red base watch error")


def start_poller(app: FastAPI) -> asyncio.Task[None]:
    """Create and return the background poller task."""
    task = asyncio.create_task(_poll_running_tasks(app))
    asyncio.create_task(_session_reaper(app))
    asyncio.create_task(_drift_watch(app))
    asyncio.create_task(_red_base_watch(app))
    asyncio.create_task(arm_workspace_hooks(app.state.db))
    log.info(
        "Background poller started (every %ds; drift check every %ds; "
        "red-base check every %ds)",
        POLL_INTERVAL,
        DRIFT_CHECK_INTERVAL,
        RED_BASE_CHECK_INTERVAL,
    )
    return task
