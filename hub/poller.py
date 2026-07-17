"""Background poller: sync running/review/ci_check tasks with dispatch job status."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from fastapi import FastAPI

from hub import config, lifecycle_matrix, services
from hub import repository as repo
from hub.db import log_activity
from hub.integrations.registry import plugins

log = logging.getLogger("hub")

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


async def _poll_running_tasks(app: FastAPI) -> None:
    """Background task: sync running/review/fix_requested tasks with dispatch job status."""
    while True:
        await asyncio.sleep(POLL_INTERVAL)
        try:
            db = app.state.db

            rows = await repo.list_running_dispatchable(db)
            for row in rows:
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
                task = dict(row)
                job = plugins.dispatch.get_job(task["review_job_id"])
                if not job:
                    await _handle_missing_job(
                        db, task, reason="review_job_missing"
                    )
                    continue
                if task.get("job_missing_since"):
                    await repo.clear_job_missing(db, task["id"])
                    await db.commit()
                job_status = job.get("status")
                if job_status not in ("completed", "failed"):
                    continue

                updates_rows = await repo.get_task_updates(db, task["id"])
                updates_list = [dict(r) for r in updates_rows]

                last_rework_at = ""
                for u in reversed(updates_list):
                    if (
                        u.get("agent") == "human"
                        and u.get("kind") == "status"
                        and "rework" in u.get("content", "").lower()
                    ):
                        last_rework_at = u.get("created_at", "")
                        break

                recent_updates = (
                    [
                        u
                        for u in updates_list
                        if u.get("created_at", "") > last_rework_at
                    ]
                    if last_rework_at
                    else updates_list
                )

                has_alert = any(
                    u.get("kind") == "alert"
                    and "cycle limit" in u.get("content", "").lower()
                    for u in recent_updates
                )
                has_arbitration = any(
                    u.get("kind") == "arbitration" for u in recent_updates
                )

                if job_status == "failed":
                    if has_alert or has_arbitration:
                        await repo.update_task(db, task["id"], status="needs_decision")
                        await repo.insert_event(
                            db,
                            kind="needs_decision",
                            task_id=task["id"],
                            actor="hub",
                            payload={"reason": "review_job_failed_after_limit"},
                        )
                        await db.commit()
                        await repo.add_task_update(
                            db,
                            task["id"],
                            "hub",
                            "alert",
                            f"Review/arbiter job failed (exit={job.get('exit_code')}). Manual decision required.",
                        )
                        await db.commit()
                        log.info(
                            "Poll: review/arbiter job failed for task #%d after cycle limit → needs_decision",
                            task["id"],
                        )
                        await services.maybe_destroy_vast(db, task)
                    else:
                        # Universal Review Gate (#309): a crashed review job
                        # must never complete the task — no verdict exists.
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

                if has_arbitration:
                    await repo.update_task(db, task["id"], status="needs_decision")
                    await repo.insert_event(
                        db,
                        kind="needs_decision",
                        task_id=task["id"],
                        actor="hub",
                        payload={"reason": "arbitration_finished"},
                    )
                    await db.commit()
                    log.info("Poll: task #%d arbiter done → needs_decision", task["id"])
                    await services.maybe_destroy_vast(db, task)
                    continue

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
                        ci = await plugins.git_ops.check_pr_ci(pr_num)
                        if ci == "pass":
                            merged = await plugins.git_ops.merge_pr(
                                pr_num, task["id"], task["title"]
                            )
                            if merged:
                                await plugins.git_ops.pull_main()
                                if branch:
                                    await plugins.git_ops.delete_branch(branch)
                                log.info(
                                    "Poll: task #%d PR #%d merged on GitHub",
                                    task["id"],
                                    pr_num,
                                )
                        elif ci == "fail":
                            log.warning(
                                "Poll: task #%d CI failed on PR #%d", task["id"], pr_num
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
                                "Poll: task #%d CI pending on PR #%d, will retry",
                                task["id"],
                                pr_num,
                            )
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
                        full_log = plugins.dispatch.job_log_full(task["review_job_id"])
                        if full_log:
                            review_text = _extract_review_from_log(full_log)
                    if not review_text:
                        review_text = (
                            "Ревьюер запросил изменения, но конкретные замечания "
                            "не удалось извлечь. Проверь git diff и исправь проблемы."
                        )

                    if task.get("review_cycle", 0) + 1 >= config.MAX_REVIEW_CYCLES:
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
                ci = await plugins.git_ops.check_pr_ci(task["pr_number"])
                if ci == "pending":
                    continue
                await repo.reset_ci_check_state(db, task["id"])
                if ci == "pass":
                    log.info(
                        "Poll: task #%d CI passed on PR #%s, dispatching review",
                        task["id"],
                        task.get("pr_number"),
                    )
                    await services.dispatch_review(db, task)
                elif ci == "fail":
                    ci_fix_cycle = task.get("ci_fix_cycle", 0)
                    if ci_fix_cycle < config.MAX_CI_FIX_CYCLES:
                        ci_details = await plugins.git_ops.get_ci_failure_logs(
                            task["pr_number"],
                            task.get("branch", ""),
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
                        await repo.update_task(db, task["id"], status="needs_decision")
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
                task = dict(row)
                if await repo.has_stale_alert(db, task["id"]):
                    continue
                await repo.add_task_update(
                    db,
                    task["id"],
                    "hub",
                    "alert",
                    f"Task stale: no updates for {config.STALE_THRESHOLD_MINUTES}+ minutes.",
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

            # Stale watchdog for silent dead-end statuses (#319). Only
            # client-driven review is watched (headless review belongs to
            # this conveyor); statuses never change here — alerts only.
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
            )
            for status_name, threshold, null_review_job, action in stale_specs:
                rows = await repo.list_stale_tasks(
                    db,
                    status_name,
                    threshold,
                    require_null_review_job=null_review_job,
                )
                for row in rows:
                    task = dict(row)
                    if await repo.has_stale_alert(db, task["id"]):
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

            # Claim lease expiry (#417): a claim held past the lease without a
            # pair start is auto-released back to open so the task returns to
            # the queue instead of sitting owned by a dead session forever.
            # Status change makes this idempotent — an expired claim is only
            # seen once. Release does not dispatch; the task waits in open.
            expired_claims = await repo.list_expired_claims(
                db, config.CLAIM_LEASE_MINUTES
            )
            for row in expired_claims:
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
                log.info(
                    "Poll: task #%d claim lease expired → open", task["id"]
                )

            # Machine-owned deadline backstop (#418): the ownership/deadline
            # matrix is the source of truth. Any machine-owned instance that
            # sits past its (generous) deadline is transitioned to
            # needs_decision once, so no status+discriminator combination can
            # stay stuck without an owner. claimed → open is handled above.
            for policy in lifecycle_matrix.machine_deadline_policies():
                if policy.escalation != "needs_decision":
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
                    task = dict(row)
                    await repo.add_task_update(
                        db,
                        task["id"],
                        "hub",
                        "alert",
                        f"{policy.instance} exceeded its {threshold}m deadline. "
                        "Manual decision required.",
                    )
                    await repo.update_task(
                        db, task["id"], status="needs_decision"
                    )
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

            # Events feed retention (#349): the feed is a notification
            # channel, not an archive — activity_log keeps the history.
            pruned = await repo.prune_events(db, keep_days=14)
            if pruned:
                await db.commit()
                log.info("Poll: pruned %d events older than 14 days", pruned)

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


def start_poller(app: FastAPI) -> asyncio.Task[None]:
    """Create and return the background poller task."""
    task = asyncio.create_task(_poll_running_tasks(app))
    asyncio.create_task(_session_reaper(app))
    log.info("Background poller started (every %ds)", POLL_INTERVAL)
    return task
