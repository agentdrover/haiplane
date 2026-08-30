"""Background poller: sync running/review/ci_check tasks with dispatch job status."""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Iterator
from typing import Any
import logging
import time
from datetime import UTC, datetime

from fastapi import FastAPI

from hub import config, lifecycle_matrix, services
from hub import repository as repo
from hub.db import connect as db_connect
from hub.db import fetchall, log_activity
from hub.integrations.git_ops import WorkspaceNotReadyError
from hub.integrations.protocols import CIProbeOutcome
from hub.integrations.registry import plugins

log = logging.getLogger("hub")

# Last refusal reported per project by the release sweep (#812). Kept in memory
# on purpose: it exists to avoid repeating the same line every cycle, and a
# restart repeating one line is cheaper than a table nobody reads.
_release_notices: dict[str, str] = {}

# Per project: the current refusal, how many consecutive cycles it has held,
# and whether it already reached the activity feed (#962). In memory like
# _release_notices — a restart re-counting the threshold costs ~3 cycles,
# while the silent alternative cost a day of reading server logs on 26.08.
_release_stalls: dict[str, tuple[str, int, bool]] = {}

# One refused cycle is a flicker (a network hiccup, a race with CI); the same
# refusal this many cycles in a row is a stall a human has to resolve.
RELEASE_STALL_CYCLES = 3

# Per task: the delivery-gate refusal already reported while waiting (#971).
# In memory for the same reason as _release_notices — it exists to stop the
# same line repeating every thirty seconds, and a restart repeating one line
# is cheaper than a table nobody reads.
_pair_delivery_waits: dict[int, str] = {}

POLL_INTERVAL = 30  # seconds

CI_GRACE_PERIOD = (
    config.CI_GRACE_PERIOD
)  # wait after push before treating missing runs as a fact

MAX_CI_NO_PR_ATTEMPTS = 3  # give up creating a PR after this many polls

# When the delivery discrepancy sweep last ran (#897). In memory for the same
# reason as _release_notices: it schedules work, it is not a fact about the
# world, and a restart running one sweep early costs nothing.
_last_delivery_scan: float = 0.0


def _due_for_delivery_scan() -> bool:
    """True at most once per ``DELIVERY_SCAN_MINUTES``, and marks the run."""
    global _last_delivery_scan
    now = time.monotonic()
    if now - _last_delivery_scan < config.DELIVERY_SCAN_MINUTES * 60:
        return False
    _last_delivery_scan = now
    return True


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


@contextlib.asynccontextmanager
async def _own_connection(app: FastAPI):
    """Своё соединение фоновому циклу — не то, которым отвечают на запросы (#1065).

    Раньше поллер делил общее соединение с обработчиками, и его commit
    фиксировал их незакоммиченное ровно так же, как их commit — его. Циклов
    четыре, соединений теперь четыре: цикл внутри себя последователен, так что
    одного на цикл достаточно, а открывать его на каждый тик значило бы
    платить за открытие раз в несколько секунд без всякой пользы.

    Без DSN (так поднимают приложение тесты, инжектируя готовое соединение)
    отдаётся app.state.db — подсовывать циклу нечего, и притворяться, что
    соединение своё, было бы хуже, чем сказать правду вызывающему.
    """
    dsn = getattr(app.state, "dsn", None)
    if not dsn:
        yield app.state.db
        return
    conn = await db_connect(dsn)
    try:
        yield conn
    finally:
        await conn.close()


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


async def _sweep_running_dispatch(db) -> None:
    """Running headless tasks: sync each with its dispatch job."""
    rows = await repo.list_running_dispatchable(db)
    for row in rows:
        with _task_isolation("running", dict(row).get("id")):
            task = dict(row)
            job = plugins.dispatch.get_job(task["job_id"])
            if not job:
                await _handle_missing_job(db, task, reason="dispatch_job_missing")
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
                for u in [dict(r) for r in await repo.get_task_updates(db, task["id"])]
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
                # #1018: the log's last long passage is KEPT, because a person
                # reading the inbox wants it — but it no longer counts as the
                # agent saying "done". The passage is chosen by LENGTH, and
                # length cannot tell a report of finished work from a thought
                # about what to do next; an agent that died mid-task or hit a
                # limit used to "report" its last reasoning, and that opened
                # the whole git tail — commit, squash, push, create_pr — plus a
                # reviewer run of up to 300k tokens over code nobody finished.
                #
                # has_done stays False on purpose: the task goes to
                # pending_report, the status that exists for exactly this — a
                # human decides whether the work is done.
                summary = _extract_agent_summary(
                    plugins.dispatch.job_log_full(task["job_id"])
                )
                if summary:
                    await repo.add_task_update(
                        db,
                        task["id"],
                        "agent",
                        "done",
                        "Отчёт НЕ заявлен агентом — это последний длинный "
                        "фрагмент лога прогона, сохранённый хабом. Прав "
                        "отчёта у него нет: конвейер не открыт, решение за "
                        "человеком (#1018).\n\n" + summary,
                        agent_claimed=False,
                    )
                    await db.commit()
                    log.info(
                        "Poll: task #%d — log passage kept, NOT taken as a done "
                        "report (#1018)",
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


async def _finish_arbiter(db, task: dict, job: dict, job_status: str) -> None:
    """Server-owned arbiter termination (#422): any terminal state → Decision Gate."""
    summary = (job.get("result_text") or "").strip()
    if not summary:
        summary = _extract_agent_summary(
            plugins.dispatch.job_log_full(task["review_job_id"])
        )
    reason = "arbiter_job_failed" if job_status == "failed" else "arbitration_finished"
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
        "(hub_decide_task)." + (f"\n\nArbiter summary:\n{summary}" if summary else ""),
    )
    await db.commit()
    log.info(
        "Poll: task #%d arbiter %s → needs_decision",
        task["id"],
        job_status,
    )
    await services.maybe_destroy_vast(db, task)
    return


async def _escalate_failed_review_job(db, task: dict, job: dict) -> None:
    """Universal Review Gate (#309): a crashed review job never completes the task."""
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
    return


# A scanned approval was refused and the task already routed to a human. Not a
# verdict and not "nothing found" — both of those have their own handling, and
# collapsing this into either would either deliver the work or hide why it stopped.
SCANNED_APPROVAL_DEFERRED = "scanned_approval_deferred"


async def _resolve_review_verdict(db, task: dict, updates_list: list[dict]) -> str:
    """Verdict for the CURRENT submission: persisted state first (#326), then text.

    A found verdict is recorded bound to the submission generation (#305),
    so a later resubmission invalidates this approval.
    """
    # Structured channel first (#326): a persisted verdict for
    # the CURRENT submission wins; text scanning stays as the
    # fallback for legacy reviewers and dispatch logs.
    persisted = task.get("review_verdict")
    if persisted and task.get("review_verdict_generation") == task.get(
        "submission_generation"
    ):
        log.info(
            "Poll: task #%d verdict '%s' from persisted review state",
            task["id"],
            persisted,
        )
        # Canonical verdict state (#305): bind the verdict to the
        # current submission generation so a later resubmission
        # invalidates this approval.
        await repo.record_review_verdict(db, task["id"], persisted)
        return str(persisted)

    scanned = services.extract_review_verdict(
        task["id"], task["review_job_id"], updates_list
    )
    if scanned is None:
        return ""

    if scanned.verdict == "approved":
        # #1019: a line ending in "approved" is not an approval. The word turns
        # up in a quoted finding, in a plan, in a stretch of diff that reached
        # the log — and the consequence here is delivery. Every other way past
        # the Universal Review Gate (force_complete, decide accept) leaves a
        # human decision in the audit; this one left an ordinary-looking
        # APPROVED, so the bypass was the one nobody could see afterwards.
        await _defer_scanned_approval(db, task, scanned)
        return SCANNED_APPROVAL_DEFERRED

    # changes_requested keeps working: it returns work to its author instead of
    # opening delivery, so a false positive costs a round, not a merge.
    await repo.record_review_verdict(db, task["id"], scanned.verdict)
    return scanned.verdict


async def _defer_scanned_approval(db, task: dict, scanned) -> None:
    """Hand a scanned approval to a human, quoting what was taken for a verdict."""
    log.warning(
        "Poll: task #%d approval came from a text match in %s, not from a "
        "review submission → needs_decision",
        task["id"],
        scanned.source,
    )
    quoted = scanned.line.strip()
    if len(quoted) > 300:
        quoted = quoted[:300] + "…"
    await repo.add_task_update(
        db,
        task["id"],
        "hub",
        "alert",
        "Одобрение найдено СКАНИРОВАНИЕМ текста, а не сдано ревьюером, "
        "поэтому доставку оно не открывает.\n"
        f"Источник: {scanned.source}.\n"
        f"Строка, принятая за вердикт: «{quoted}»\n"
        "Если это действительно вердикт — вынесите его через ревью "
        "(hub_submit_review) или примите решение вручную (hub_decide_task).",
    )
    await repo.update_task(db, task["id"], status="needs_decision")
    await repo.insert_event(
        db,
        kind="needs_decision",
        task_id=task["id"],
        actor="hub",
        payload={
            "reason": "scanned_approval_not_a_verdict",
            "source": scanned.source,
            "line": quoted,
        },
    )
    await db.commit()
    await services.maybe_destroy_vast(db, task)


async def _record_merge_and_tidy(db, task: dict, pr_num: int, mctx: dict) -> None:
    """After a merge: record the produced commit (#534) and tidy the workspace (#552).

    Recording and tidying are post-merge bookkeeping, never delivery: the
    work is already in the base branch, so a workspace that cannot be
    returned to base is reported and left to a human rather than failing
    the task.
    """
    branch = task.get("branch")
    mworkspace = mctx.get("repo")
    mgh_repo = mctx.get("gh_repo")
    merge_sha = ""
    try:
        merge_sha = await plugins.git_ops.merge_commit_sha(
            pr_num,
            repo=mworkspace,
            gh_repo=mgh_repo,
        )
    except Exception:
        log.exception(
            "could not read the merge commit "
            "for task #%s; the drift guard "
            "will flag it once",
            task["id"],
        )
    proj = await repo.resolve_project_for_task(db, task["id"])
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
        await plugins.git_ops.pull_main(repo=mworkspace, base_branch=mbase)
        if branch:
            await plugins.git_ops.delete_branch(
                branch,
                repo=mworkspace,
                base_branch=mbase,
            )
    except WorkspaceNotReadyError as exc:
        log.warning(
            "Poll: task #%d merged, workspace not tidied: %s",
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


async def _deliver_approved_review(db, task: dict) -> None:
    """An approved verdict is not delivery (#363): merge first, complete after."""
    pr_num = task.get("pr_number")
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
                await _record_merge_and_tidy(db, task, pr_num, mctx)
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
            return
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
        reason = "ci_failed" if ci.outcome == CIProbeOutcome.failed else "merge_failed"
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
        await repo.update_task(db, task["id"], status="needs_decision")
        await repo.insert_event(
            db,
            kind="needs_decision",
            task_id=task["id"],
            actor="hub",
            payload={"reason": reason, "pr": pr_num},
        )
        await db.commit()
        log.warning(
            "Poll: task #%d approved but PR #%d not merged (%s) → needs_decision",
            task["id"],
            pr_num,
            reason,
        )
        await services.maybe_destroy_vast(db, task)
        return
    if not merged and not pr_num:
        log.info("Poll: task #%d approved (no PR)", task["id"])
    # Converge on the same gate-checked completion used by
    # pair done reports (#309): the verdict recorded above
    # makes completion_requires_review false, so the shared
    # transition completes without bumping the generation.
    refreshed_row = await repo.get_task(db, task["id"])
    refreshed = dict(refreshed_row) if refreshed_row else task
    await services.transition_after_agent_done(db, refreshed, has_done=True)
    await db.commit()
    log.info("Poll: task #%d review → approved", task["id"])
    await services.maybe_destroy_vast(db, task)


async def _request_review_fixes(db, task: dict, updates_list: list[dict]) -> None:
    """changes_requested: extract the review text and dispatch a fix or the arbiter."""
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

    if services.review_budget_exhausted(task.get("review_cycle", 0)):
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


async def _mark_no_clear_verdict(db, task: dict) -> None:
    """A finished review job with no readable verdict goes to the Decision Gate."""
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


async def _review_task_tick(db, task: dict, job: dict) -> None:
    """One review task against its finished review job."""
    job_status = str(job.get("status") or "")
    updates_rows = await repo.get_task_updates(db, task["id"])
    updates_list = [dict(r) for r in updates_rows]

    is_arbiter_job = bool(
        task.get("arbiter_state") == "running"
        and task.get("arbiter_job_id")
        and task.get("arbiter_job_id") == task.get("review_job_id")
    )
    if is_arbiter_job:
        await _finish_arbiter(db, task, job, job_status)
        return
    if job_status == "failed":
        await _escalate_failed_review_job(db, task, job)
        return

    verdict = await _resolve_review_verdict(db, task, updates_list)
    if verdict == "approved":
        await _deliver_approved_review(db, task)
    elif verdict == "changes_requested":
        await _request_review_fixes(db, task, updates_list)
    elif verdict == SCANNED_APPROVAL_DEFERRED:
        return  # already routed to a human, with the matched line quoted
    else:
        await _mark_no_clear_verdict(db, task)


async def _sweep_review(db) -> None:
    """Review tasks: settle finished review jobs into verdict handling."""
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
            if job.get("status") not in ("completed", "failed"):
                continue
            await _review_task_tick(db, task, job)


async def _ensure_pr_for_ci(db, task: dict, ctx: dict, workspace) -> bool:
    """A ci_check task without a PR gets one, or eventually a human (#419).

    Returns False when this task is done for the pass — the PR could not
    be created and either a retry is counted or the task escalated.
    """
    branch = task.get("branch")
    if branch:
        await plugins.git_ops.push_branch(branch, repo=workspace, force=True)
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
        attempts = await repo.increment_ci_no_pr_attempts(db, task["id"])
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
            await repo.update_task(db, task["id"], status="needs_decision")
            await repo.reset_ci_check_state(db, task["id"])
            await db.commit()
            await services.maybe_destroy_vast(db, task)
        else:
            await db.commit()
        return False
    return True


async def _sweep_ci_check(db) -> None:
    """ci_check tasks: ensure a PR exists, read CI, route the outcome."""
    ci_rows = await repo.list_ci_check_tasks(db)
    for row in ci_rows:
        with _task_isolation("ci_check", dict(row).get("id")):
            task = dict(row)
            ctx = await services.project_git_context(db, task["id"])
            workspace = ctx.get("repo")
            if not task.get("pr_number"):
                if not await _ensure_pr_for_ci(db, task, ctx, workspace):
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
            if ci.outcome == CIProbeOutcome.missing_run:
                # Grace already elapsed — we only probe after it. Workflows
                # exist and this SHA was never checked; skipping to review
                # is how #505–#510 landed untested (#1041).
                sha = (ci.details or "").strip() or "unknown"
                named = f"workflow есть, прогона по {sha} нет"
                await repo.add_task_update(
                    db,
                    task["id"],
                    "hub",
                    "alert",
                    f"CI: {named} — код не проверялся.",
                )
                await repo.update_task(db, task["id"], status="needs_decision")
                await repo.insert_event(
                    db,
                    kind="needs_decision",
                    task_id=task["id"],
                    actor="hub",
                    payload={"reason": "ci_untested", "sha": sha},
                )
                await repo.reset_ci_check_state(db, task["id"])
                await db.commit()
                log.info(
                    "Poll: task #%d CI missing run for %s → needs_decision",
                    task["id"],
                    sha,
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


# #957: the escalation ladder. Silence is not one fact but a growing one, so
# the watchdog answers it with RUNGS: the status's own threshold first, then a
# day, three days, a week. A rung, once raised for a task+status, is never
# raised again — an honest feed entry must not reopen the first rung (#927
# collected an alert per report exactly that way), and a task that stays
# silent climbs to the next rung instead of hiding behind its only alert
# (#443 lay quiet for a week that way). Labels are embedded in the alert text
# next to the parseable "stale in {status}" key.
_STALE_LADDER_MINUTES = ((1440, "24h"), (4320, "72h"), (10080, "7d"))


def _silence_minutes(last_at: str) -> float | None:
    seconds = _seconds_since(last_at)
    return seconds / 60 if seconds is not None else None


def _age_phrase(minutes: float) -> str:
    if minutes >= 1440:
        return f"{minutes / 1440:.0f} сут"
    if minutes >= 60:
        return f"{minutes / 60:.0f} ч"
    return f"{int(minutes)} мин"


async def _stale_task_tick(
    db, task: dict, status: str, threshold: int, action: str
) -> None:
    """One task against the watchdog: a lapsed wait or the silence ladder (#957)."""
    task_id = task["id"]
    waiting_for = str(task.get("waiting_for") or "")
    waiting_until = str(task.get("waiting_until") or "")

    if waiting_for and waiting_until:
        # The declared wait has lapsed (a current one never reaches this
        # sweep — the SQL filters it out). Escalate from the DEADLINE, not
        # from the feed: the task said "judge me by this date", so it is.
        overdue = _silence_minutes(waiting_until)
        if overdue is None:
            overdue = 0.0
        rungs = ((0, "просрочка"), *_STALE_LADDER_MINUTES)
        due = [label for gate, label in rungs if overdue >= gate]
        if not due:
            return
        rung = due[-1]
        if await repo.stale_rung_raised(db, task_id, status, rung):
            return
        await repo.add_task_update(
            db,
            task_id,
            "hub",
            "alert",
            f"Ожидание просрочено (stale in {status}) [рубеж {rung}]: ждали "
            f"«{waiting_for}» до {waiting_until}, срок вышел "
            f"{_age_phrase(overdue)} назад. {action}",
        )
        await db.commit()
        await log_activity(
            db,
            "task_stale",
            f"Task #{task_id}: объявленное ожидание просрочено на "
            f"{_age_phrase(overdue)} — ждали «{waiting_for}»"[:200],
        )
        log.warning(
            "Poll: task #%d declared wait lapsed %s ago (%s)",
            task_id,
            _age_phrase(overdue),
            rung,
        )
        return

    last_at = await repo.last_activity_at(db, task_id)
    silence = _silence_minutes(last_at)
    if silence is None:
        # No parseable activity at all — fall back to the entry threshold so
        # a task with an empty feed is not invisible to the watchdog.
        silence = float(threshold)
    rungs = ((threshold, f"{threshold}m"), *_STALE_LADDER_MINUTES)
    due = [label for gate, label in rungs if silence >= gate]
    if not due:
        return
    rung = due[-1]
    if await repo.stale_rung_raised(db, task_id, status, rung):
        return
    await repo.add_task_update(
        db,
        task_id,
        "hub",
        "alert",
        f"Task stale in {status} [рубеж {rung}]: тишина уже "
        f"{_age_phrase(silence)} (последняя запись {last_at or 'неизвестна'}). "
        f"{action}",
    )
    await db.commit()
    await log_activity(
        db,
        "task_stale",
        f"Task #{task_id} stale in {status}: тишина {_age_phrase(silence)}",
    )
    log.warning(
        "Poll: task #%d stale in %s for %s (%s)",
        task_id,
        status,
        _age_phrase(silence),
        rung,
    )


async def _sweep_stale_running(db) -> None:
    """Running tasks: lapsed waits and the silence ladder, one rung at a time."""
    stale_rows = await repo.list_stale_running(db, config.STALE_THRESHOLD_MINUTES)
    for row in stale_rows:
        with _task_isolation("stale review", dict(row).get("id")):
            await _stale_task_tick(
                db,
                dict(row),
                "running",
                config.STALE_THRESHOLD_MINUTES,
                "Разберитесь, что с задачей: живое ожидание объявляется с "
                "событием и сроком, брошенное — решается человеком.",
            )

    # Stale watchdog for silent dead-end statuses (#319, #393). Only
    # client-driven review is watched (headless review belongs to
    # this conveyor); statuses never change here — alerts only. The
    # machine-owned dead-ends (ci_check, fix_requested, pending_report)
    # get visibility here until F2 lands durable deadline transitions.


async def _sweep_stale_statuses(db) -> None:
    """Silent dead-end statuses (#319, #393): alert once, never transition."""
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
            "Claim held without pair start: call hub_pair_start or hub_release_task.",
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
            "Awaiting agent hub_report_done, or recover with hub_force_complete_task.",
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
                await _stale_task_tick(db, dict(row), status_name, threshold, action)

    # Unrefined drafts (#751): a draft the DoR gate would refuse is a
    # quiet dead end — approval 422s, batch approve silently skips it,
    # and the author finds out only when the owner hits the button.
    # One alert per draft (has_stale_alert dedup, same as above);
    # a draft brought to DoR after the alert is left alone.


async def _sweep_unrefined_drafts(db) -> None:
    """Drafts the DoR gate would refuse (#751): alert once with what is missing."""
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

            readiness = await calculate_readiness_with_recommendations(db, task["id"])
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


# What each human-owned instance is actually waiting for. The age alone does
# not tell a person what to do with the task — and "someone should look at
# this" is what the single lifetime alert already said, to no effect.
_HUMAN_QUEUE_ACTIONS: dict[str, str] = {
    "draft": "черновик ждёт одобрения или отклонения (hub_approve_task / hub_reject_task)",
    "needs_info": "агент ждёт ответа на вопрос (hub_answer_question)",
    "needs_decision": "задача ждёт решения человека (hub_decide_task)",
    "review:client": "сдача ждёт вердикта ревьюера (hub_submit_review)",
}


def _human_queue_rows_sql(policy) -> tuple[str, tuple]:
    """Rows in this human-owned instance, oldest wait first."""
    sql = (
        "SELECT id, title, status, status_entered_at, updated_at "
        "FROM tasks WHERE status=? AND archived=0"
    )
    params: tuple = (policy.status,)
    if policy.discriminator == "client":
        # review:client is the half of review that waits on a person; the
        # headless half is the conveyor's own and escalates by deadline.
        sql += " AND (review_job_id IS NULL OR review_job_id='')"
    return sql + " ORDER BY status_entered_at ASC", params


async def _sweep_human_queue(db) -> None:
    """The human queue gets hours (#1020): 24h / 72h / 168h, one rung each.

    Volume only. Status, owner and next actor are untouched — the matrix's
    invariant is that a human-owned instance never auto-transitions, and a
    reminder is not a deadline. The reminder goes to the events feed and the
    activity log, NOT to the task's own updates: that feed is the story of the
    work, and an alarm clock ringing in it three times a week would be read as
    part of the work.
    """
    for policy in lifecycle_matrix.human_owned_policies():
        sql, params = _human_queue_rows_sql(policy)
        rows = await fetchall(db, sql, params)
        for row in rows:
            with _task_isolation("human queue", dict(row).get("id")):
                await _human_queue_tick(db, dict(row), policy)


async def _human_queue_tick(db, task: dict, policy) -> None:
    task_id = task["id"]
    entered_at = str(task.get("status_entered_at") or "")
    estimated = not entered_at
    if estimated:
        # #416 backfilled this column for every row, so this is a guard, not a
        # path anyone is expected to walk. It says so out loud rather than
        # presenting a guess as a measurement.
        entered_at = str(task.get("updated_at") or "")
    age = _silence_minutes(entered_at)
    if age is None:
        return

    due = [label for gate, label in config.HUMAN_QUEUE_LADDER_MINUTES if age >= gate]
    if not due:
        return
    rung = due[-1]
    if await repo.human_queue_rung_raised(
        db, task_id, policy.instance, entered_at, rung
    ):
        return
    if not await repo.record_human_queue_reminder(
        db,
        task_id=task_id,
        instance=policy.instance,
        entered_at=entered_at,
        rung=rung,
        age_minutes=int(age),
        age_estimated=estimated,
    ):
        return  # another pass got there first

    action = _HUMAN_QUEUE_ACTIONS.get(policy.instance, "задача ждёт человека")
    age_text = _age_phrase(age) + (" (возраст оценочный)" if estimated else "")
    await repo.insert_event(
        db,
        kind="human_queue_reminder",
        task_id=task_id,
        actor="hub",
        payload={
            "instance": policy.instance,
            "rung": rung,
            "age_minutes": int(age),
            "age_estimated": estimated,
            "surface": policy.surface,
            "action": action,
        },
    )
    await db.commit()
    await log_activity(
        db,
        "human_queue_reminder",
        f"Task #{task_id} ждёт человека {age_text} [рубеж {rung}]: {action}"[:200],
    )
    log.warning(
        "Poll: task #%d waiting on a human in %s for %s (%s)",
        task_id,
        policy.instance,
        age_text,
        rung,
    )


async def _sweep_autopilot_digests(db) -> None:
    # Autopilot digests (#739): one per project per UTC day of
    # autopilot activity. Idempotent via the UNIQUE key, so every
    # poll pass may try; a failure must not kill the loop.
    try:
        from hub.services.digest import generate_due_digests

        await generate_due_digests(db)
    except Exception:  # noqa: BLE001 - oversight must not stop polling
        log.exception("autopilot digest generation failed")


async def _sweep_delivery_discrepancies(db) -> None:
    # Completed, but is the work delivered? (#897) On a timer rather
    # than on every pass: the question costs a call to GitHub per
    # candidate, and the answer changes at the speed of merges, not of
    # polls. Alerts are damped inside the sweep — once per state, not
    # once per cycle — the same discipline the stale sweeps above use.
    if _due_for_delivery_scan():
        try:
            from hub.services.delivery_state import (
                scan_completed_deliveries,
            )

            found = await scan_completed_deliveries(
                db, lookback_days=config.DELIVERY_SCAN_LOOKBACK_DAYS
            )
            if found:
                log.warning(
                    "Poll: %d completed task(s) with an open PR: %s",
                    len(found),
                    ", ".join(f"#{f['task_id']}" for f in found),
                )
        except Exception:  # noqa: BLE001 - oversight must not stop polling
            log.exception("delivery discrepancy sweep failed")


async def _sweep_review_dispatches(db) -> None:
    # Cross-model review dispatches (#757): settle finished cloud
    # reviewer runs — reports get their usage cross-check, silent
    # finishes fail loudly.
    try:
        from hub.services.review_dispatch import sweep_review_dispatches

        await sweep_review_dispatches(db)
    except Exception:  # noqa: BLE001 - the sweep must not kill the loop
        log.exception("review dispatch sweep failed")


async def _sweep_expired_claims(db) -> None:
    # Claim lease expiry (#417): a claim held past the lease without a
    # pair start is auto-released back to open so the task returns to
    # the queue instead of sitting owned by a dead session forever.
    # Status change makes this idempotent — an expired claim is only
    # seen once. Release does not dispatch; the task waits in open.
    expired_claims = await repo.list_expired_claims(db, config.CLAIM_LEASE_MINUTES)
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
            # #966: в legacy-режиме общий клон мог остаться на ветке этой
            # задачи и заблокировать следующий pair-start reason'ом
            # pair_branch_unpushed. Возврат на базу best effort: реставрация
            # не имеет права ронять sweep — остальные истёкшие claim'ы важнее.
            try:
                from hub.services import orchestration

                await orchestration.restore_pair_workspace_base(db, task["id"])
            except Exception:
                log.warning(
                    "Poll: workspace restore after claim expiry of #%d failed",
                    task["id"],
                    exc_info=True,
                )
            log.info("Poll: task #%d claim lease expired → open", task["id"])


async def _sweep_machine_deadlines(db) -> None:
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


async def _sweep_stale_arbiter(db) -> None:
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


async def _sweep_events_retention(db) -> None:
    # Events feed retention (#349): the feed is a notification
    # channel, not an archive — activity_log keeps the history.
    pruned = await repo.prune_events(db, keep_days=14)
    if pruned:
        await db.commit()
        log.info("Poll: pruned %d events older than 14 days", pruned)


async def _sweep_stale_worktrees(db) -> None:
    # Worktrees of finished tasks (#1033): 189 directories had piled up on
    # production, some a week old. Since #989 the hub names a worktree to an
    # agent exactly when the directory exists, so an abandoned tree is not
    # disk noise — it is a live path to a forgotten branch.
    #
    # Deliberately gentle in three ways: only terminal tasks, only past the
    # retention age, and only trees that are actually on disk (a cheap stat
    # before any git). A dirty tree is never removed — pair_remove_worktree
    # refuses it and says why, and that refusal is somebody's unsaved work,
    # not an error to retry away.
    try:
        from hub.services.orchestration import project_git_context

        rows = await repo.tasks_with_retired_worktrees(
            db,
            keep_days=config.WORKTREE_RETENTION_DAYS,
            limit=config.WORKTREE_RETENTION_BATCH,
        )
        for row in rows:
            task = dict(row)
            task_id = int(task["id"])
            try:
                ctx = await project_git_context(db, task_id)
                workspace = (ctx.get("repo") or "").strip() or None
                path = plugins.git_ops.worktree_path(task_id, workspace)
                if not path or not os.path.isdir(path):
                    continue
                removed = await plugins.git_ops.pair_remove_worktree(
                    task_id, repo=workspace
                )
            except Exception:  # noqa: BLE001 - one task must not stop the sweep
                log.exception("worktree retirement failed for task #%s", task_id)
                continue
            if removed:
                log.info(
                    "Poll: retired worktree of task #%s (%s, finished %s)",
                    task_id,
                    task["status"],
                    task["completed_at"],
                )
            else:
                log.warning(
                    "Poll: worktree of task #%s kept — pair_remove_worktree "
                    "refused it (uncommitted work or git error)",
                    task_id,
                )
    except Exception:  # noqa: BLE001 - the sweep must not kill the loop
        log.exception("worktree retention sweep failed")


async def _sweep_sessions_retention(db) -> None:
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


async def _sweep_pair_delivery(db) -> None:
    """Deliver approved pair work whose agent is not coming back (#971).

    The headless conveyor has had this since #363: an approved verdict is
    merged and completed by the poller, with no agent involved. A pair task
    could not reach it — ``_sweep_review`` selects on ``status='review'`` and a
    ``review_job_id``, and a pair task has neither (client-driven review,
    #307, returns it to ``running``). So its only merge trigger was the
    agent's own done report (#605), which attached the merge to the one event
    that existed at the time rather than to a decision that it should be an
    agent's to make.

    What that cost, on 26.08.2026: a session submitted #954, died six minutes
    later, and the APPROVED verdict landed an hour after that against a green,
    mergeable PR. Nothing moved. The work sat in ``running`` until a human
    called force-complete — a route with no button in the UI.

    Nothing is loosened here. The same ``merge_before_completion`` runs, with
    the same refusals: a red CI still calls a human, a branch that moved after
    the approval still refuses (#612), and a CI still running is still a wait
    rather than a decision (#951). Only the trigger changes — a state instead
    of a call. Delivery stays exactly-once because the gate opens with
    ``pipeline_merge_recorded`` (#363), so an agent that comes back finds its
    work delivered rather than a refusal.
    """
    try:
        rows = await repo.list_pair_tasks_awaiting_delivery(db)
    except Exception:
        log.exception("Poll: pair delivery sweep could not list tasks")
        return
    for row in rows:
        task = dict(row)
        with _task_isolation("pair-delivery", task.get("id")):
            await _deliver_pair_task(db, task)


async def _deliver_pair_task(db, task: dict) -> None:
    """One approved pair task through the delivery gate (#971)."""
    task_id = task["id"]
    pr_num = task.get("pr_number")
    ok, detail = await services.merge_before_completion(db, task)
    if not ok:
        # #951: a temporary state is not a decision, and the poller is the
        # place that has always known it — it simply comes back next pass.
        # Said once, not once per cycle: a line every thirty seconds is how a
        # real signal gets muted (#534).
        if detail.startswith(services.TRANSIENT_GATE_PREFIXES):
            await _note_pair_delivery_wait(
                db,
                task_id,
                pr_num,
                detail,
                hint=(
                    services.PR_DRAFT_WAIT_HINT
                    if detail.startswith(services.PR_DRAFT_PREFIX)
                    else ""
                ),
            )
            return
        reason = "merge_gate"
        if detail.startswith(services.RECOVERABLE_GATE_PREFIXES):
            # #1030: this is the path #1015 actually took — the sweep, not the
            # done report. A red CI here is not a decision to make but work to
            # do, and the executor is the one who does it, so the task stays on
            # the conveyor. Two things bound the waiting, because they bound
            # different failures: the budget stops an executor that keeps
            # failing, and presence stops a task nobody is working on any more.
            budget_spent = False
            if detail.startswith(services.CI_BUDGET_GATE_PREFIXES):
                _cycle, budget_spent = await services.charge_ci_fix_budget(db, task)
            if not budget_spent:
                if await services.pair_executor_online(db, task):
                    await _note_pair_delivery_wait(
                        db,
                        task_id,
                        pr_num,
                        detail,
                        hint=services.RESUBMIT_AFTER_FIX_HINT,
                    )
                    return
                # Nobody is around to push the fix. Said out loud: without it
                # the human reads "the CI is red" and misses that the reason
                # they are being called is that the executor left.
                detail = f"{detail} (исполнитель не на связи)"
            else:
                reason = "ci_fix_cycle_limit"
                detail = (
                    f"{detail} (бюджет починки исчерпан: "
                    f"{task.get('ci_fix_cycle')}/{config.MAX_CI_FIX_CYCLES})"
                )
        await repo.update_task(db, task_id, status="needs_decision")
        await repo.add_task_update(
            db,
            task_id,
            "hub",
            "alert",
            f"Ревью одобрено, но PR #{pr_num} не доставлен — {detail}. "
            "Задача не может считаться выполненной, пока работа не в базовой "
            "ветке. Решение за человеком (hub_decide_task).",
        )
        await repo.insert_event(
            db,
            kind="needs_decision",
            task_id=task_id,
            actor="hub",
            payload={"reason": reason, "detail": detail, "via": "poller"},
        )
        await db.commit()
        log.info(
            "Poll: task #%d → needs_decision: merge gate refused (%s)", task_id, detail
        )
        return

    _pair_delivery_waits.pop(task_id, None)
    # #812: delivery grew the release range. Best effort, exactly as on the
    # done path — a release that could not be prepared is a reason in the log,
    # never a failure of work that is already in the base branch.
    try:
        from hub.services.release import open_release_for_task

        await open_release_for_task(db, task_id)
    except Exception as exc:  # noqa: BLE001 - a cause, not a failure
        log.warning("release PR not prepared for #%s: %s", task_id, exc)

    # Said out loud, because a completed task with no done report otherwise
    # reads as a lost record rather than as work the hub carried home (AC-5).
    await repo.add_task_update(
        db,
        task_id,
        "hub",
        "status",
        f"Доставлено хабом без отчёта агента: PR #{pr_num} влит по одобренному "
        "ревью. Условия доставки были выполнены целиком, ждать было нечего. "
        "Отчёт агента, если он придёт, ляжет сюда же рассказом о работе.",
    )
    # Completion WITHOUT bumping the generation: no new work is being
    # submitted, and a bump would invalidate the very approval that authorises
    # this delivery (#306).
    await repo.update_task(db, task_id, status="completed")
    await repo.insert_event(
        db,
        kind="task_completed",
        task_id=task_id,
        actor="hub",
        payload={"via": "poller_delivery"},
    )
    await db.commit()
    log.info("Poll: task #%d delivered and completed without a done report", task_id)


async def _note_pair_delivery_wait(
    db, task_id: int, pr_num, detail: str, *, hint: str = ""
) -> None:
    """Say once that delivery is waiting, and then be quiet (#534).

    ``hint`` names what happens next when it is not "the hub comes back on its
    own": a refusal the executor has to cure is also a wait, but waiting for a
    different actor, and the default sentence would promise the wrong one.
    """
    if _pair_delivery_waits.get(task_id) == detail:
        return
    _pair_delivery_waits[task_id] = detail
    await repo.add_task_update(
        db,
        task_id,
        "hub",
        "status",
        f"Доставка отложена: PR #{pr_num} — {detail}. "
        + (
            hint
            or "Это временное состояние, решение человека не требуется — хаб "
            "вернётся к нему следующим циклом."
        ),
    )
    await db.commit()
    log.info("Poll: task #%d waiting to deliver (%s)", task_id, detail)


async def _sweep_release_policy(db) -> None:
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
                _release_stalls.pop(slug, None)
                continue
            if not reason:
                _release_stalls.pop(slug, None)
                continue
            if _release_notices.get(slug) != reason:
                _release_notices[slug] = reason
                log.warning("Poll: %s — %s", slug, reason)
            await _note_release_stall(db, slug, reason)
    except Exception:
        log.exception("Poll: release policy sweep failed")


async def _note_release_stall(db, slug: str, reason: str) -> None:
    """Raise a persistent release refusal into the activity feed (#962).

    On 26.08.2026 GitHub refused the release merge three cycles in a row —
    develop and main had diverged after a squash release — and the only trace
    was the deduplicated warning above: the stalled policy was discovered by
    a human reading server logs, and resolved by a manual sync. The feed gets
    one entry per stall; a failed write is retried next cycle instead of
    breaking the sweep for the remaining projects.
    """
    prev_reason, streak, noted = _release_stalls.get(slug, ("", 0, False))
    if prev_reason != reason:
        streak, noted = 0, False
    streak += 1
    if streak >= RELEASE_STALL_CYCLES and not noted:
        try:
            await log_activity(
                db,
                "release",
                f"{slug}: релиз стоит — {reason}",
                f"{streak} цикл(ов) поллера подряд; политика ретраит сама, "
                "но расшивка причины — за человеком",
            )
            noted = True
        except Exception:
            log.exception("Poll: release stall of %s not written to activity", slug)
    _release_stalls[slug] = (reason, streak, noted)


async def _sweep_messages_retention(db) -> None:
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


async def _sweep_mcp_retention(db) -> None:
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


async def _poll_running_tasks(app: FastAPI) -> None:
    """Background loop: one pass = the sweeps below, in this exact order.

    The order is load-bearing and is the order the original 990-line body
    executed (#850): e.g. review settlement runs before the release-policy
    sweep, so a merge this pass produced is released this pass. Each sweep
    owns one lifecycle concern and fails alone: the shared try only proves
    the loop never dies, isolation per task lives in _task_isolation.
    """
    async with _own_connection(app) as db:
        while True:
            await asyncio.sleep(POLL_INTERVAL)
            try:
                await _poll_once(db)
            except Exception:
                log.exception("Poll error")


async def _poll_once(db) -> None:
    """Один проход конвейера. Порядок несущий — см. докстроку цикла выше."""
    await _sweep_running_dispatch(db)
    await _sweep_review(db)
    await _sweep_pair_delivery(db)
    await _sweep_ci_check(db)
    await _sweep_stale_running(db)
    await _sweep_stale_statuses(db)
    await _sweep_unrefined_drafts(db)
    await _sweep_human_queue(db)
    await _sweep_autopilot_digests(db)
    await _sweep_delivery_discrepancies(db)
    await _sweep_review_dispatches(db)
    await _sweep_expired_claims(db)
    await _sweep_machine_deadlines(db)
    await _sweep_stale_arbiter(db)
    await _sweep_events_retention(db)
    await _sweep_stale_worktrees(db)
    await _sweep_sessions_retention(db)
    await _sweep_release_policy(db)
    await _sweep_messages_retention(db)
    await _sweep_mcp_retention(db)


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
    async with _own_connection(app) as db:
        while True:
            await asyncio.sleep(SESSION_CLEANUP_INTERVAL)
            try:
                await _reap_sessions_once(db)
            except Exception:
                log.exception("Session reaper error")


async def _reap_sessions_once(db) -> None:
    """Один проход жатвы сессий."""
    cursor = await db.execute(
        "DELETE FROM browser_sessions WHERE expires_at < datetime('now') "
        "OR revoked_at IS NOT NULL"
    )
    deleted = cursor.rowcount
    await db.commit()
    if deleted:
        log.info("Session reaper: removed %d expired/revoked sessions", deleted)

    # Chat-pair rows die on the same hourly cycle (#961): the channel
    # is minutes-to-hours long, so a table that only ever grew would be
    # an archive of dead secrets' hashes.
    from hub.services import orchestration
    from hub.services.chat_pair import (
        chat_pair_limiter,
        purge_expired,
        release_expired_implementer_tasks,
    )

    released = await release_expired_implementer_tasks(db)
    purged = await purge_expired(db)
    for task_id in released:
        try:
            await orchestration.restore_pair_workspace_base(db, task_id)
        except Exception:
            log.warning(
                "Session reaper: workspace restore after implementer "
                "expiry of #%d failed",
                task_id,
                exc_info=True,
            )
    if purged:
        log.info("Session reaper: removed %d chat-pair rows", purged)

    from hub.auth import login_limiter

    login_limiter._cleanup()
    chat_pair_limiter._cleanup()


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

    async with _own_connection(app) as db:
        while True:
            await asyncio.sleep(DRIFT_CHECK_INTERVAL)
            try:
                reports = await drift_guard.check_all_projects(db)
                for report in reports:
                    if report.status == "unknown":
                        # Never silent: "could not check" is a state an
                        # operator has to be able to see, not an absence of
                        # news.
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

    async with _own_connection(app) as db:
        while True:
            await asyncio.sleep(RED_BASE_CHECK_INTERVAL)
            try:
                for state in await red_base.check_all_projects(db):
                    if state.status == red_base.UNKNOWN:
                        # Never silent: "could not look" is a state the
                        # operator has to see, not an absence of news (#725).
                        log.warning(
                            "base CI state unknown for %s: %s",
                            state.branch,
                            state.reason,
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
