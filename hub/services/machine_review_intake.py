"""One intake for a machine-review report, whoever brings it (#1036).

The REST route used to hold this logic inline, which was fine while MCP was
the only way a report could arrive. It is not any more: when Cursor stopped
delivering ``mcpServers`` into cloud runs, the reviewer's report started
living in the run's final text instead, and the sweep has to record it too.

Two write paths for one fact drift — #519 and #546 are both that story — so
the second caller does not get its own INSERT. It calls this, and the only
thing that differs is ``origin``: who handed the report over, and therefore
how far it can be trusted. A report typed into a text field cannot be checked
the way one submitted through the contract can, and the difference is kept in
the data rather than in a comment.
"""

from __future__ import annotations

import logging

import aiosqlite
from fastapi import HTTPException

from hub import db as db_module
from hub import repository as repo
from hub import services
from hub.db import fetchall
from hub.models import MachineReviewSubmit, MachineReviewView

log = logging.getLogger("hub")

# Where a report came from. ``mcp`` is the contract path — the reviewer called
# hub_submit_machine_review itself, authenticated as itself. Anything else is
# the hub transcribing what a run left behind, and says so in the stored row.
ORIGIN_MCP = "mcp"
ORIGIN_RUN_TEXT = "cursor-cloud-result"


async def record_machine_review(
    db: aiosqlite.Connection,
    task_id: int,
    body: MachineReviewSubmit,
    *,
    principal_id: int | None,
    username: str,
    origin: str = ORIGIN_MCP,
) -> MachineReviewView:
    """Store one report and run everything that hangs off it (#381, #1036).

    ``principal_id`` is the OWNER of the report, always taken from a token or
    from the dispatch row — never from what the report says about itself
    (#893, #1025). ``origin`` records how it reached us.
    """
    import json as _json

    row = await repo.get_task(db, task_id)
    if row is None:
        raise HTTPException(404, "task not found")
    task = dict(row)
    generation = task.get("submission_generation") or 0
    if generation == 0:
        raise HTTPException(
            400,
            "no submission to review: submit_for_review must run at least once",
        )
    # raw_count is self-reported and was stored unchecked, so reports arrived
    # claiming fewer raw findings than the findings they themselves listed —
    # on production one had raw_count=0 alongside two confirmed findings
    # (#519). Normalised upward rather than rejected: the recorded risk asks
    # not to break existing clients, and a report with a miscounted header is
    # still worth keeping — its findings are real.
    # Where each confirmed finding sits, before anything is stored (#1007):
    # a report that never placed its findings cannot be matched against a diff
    # later, and the gap is invisible once the report is in the ground.
    services.require_locator_decision(body.findings_confirmed)
    services.refuse_supplied_uid(body.findings_confirmed)
    adjudicated = len(body.findings_confirmed) + len(body.findings_rejected)
    raw_count = body.raw_count
    if raw_count < adjudicated:
        log.warning(
            "machine review for task #%s: raw_count=%s is below the %s findings "
            "it lists; normalised upward",
            task_id,
            raw_count,
            adjudicated,
        )
        raw_count = adjudicated
    # #807: the profile is taken from the DISPATCH, not from the report — a
    # run's own claim about how thoroughly it ran is exactly the evidence
    # #750 showed to be worthless. No dispatch behind the report leaves the
    # profile empty, which reads as "unknown", not as "cheap".
    dispatch = await repo.get_review_dispatch_for_generation(db, task_id, generation)
    profile = (dispatch["profile"] if dispatch is not None else "") or ""
    # #807 forced incomplete=true on a lite run whose SELF-REPORTED spend
    # reached the ceiling. Removed in #893: it never once fired, and could
    # not. Eleven runs measured against the provider's bill cost 777k-6.0M
    # while reporting 25k-312k — the report's own number missed the bill by
    # 12-62x every time, so a run that burned 1.5M and declared 36k sailed
    # through as complete. A guard that reads the checked party's estimate of
    # itself is not a guard; keeping it would only say we have one.
    #
    # Coverage honesty stays, and it never depended on the number: the
    # reviewer declares which files it did not read, and that IS checkable
    # against the diff. The provider-vs-self gap keeps being recorded as an
    # audit signal (#828), where it belongs — beside the numbers, not
    # pretending to bound them.
    incomplete = body.incomplete
    # #728: the twin of the human path's guard. hub_submit_review refuses a
    # verdict from the implementer and the brief warns before the effort is
    # spent, while this door had no check at all — so the rule was bypassable
    # by choosing it, and the operator said what that bought: "для аудита
    # слабо, для пропуска в очередь — ок".
    #
    # Recorded rather than refused: the statement rules out removing machine
    # review as a queue mechanism, and a report of one's own work is still
    # worth keeping — its findings are real. What must never happen is that it
    # passes for an independent one, so the fact travels with it (and the
    # auto-verdict acts on it). Same definition of "implementer" as the verdict
    # gate — caller_implemented_task — not a second one that could drift.
    #
    # From the TOKEN, never from submitted_by: that field is written below as
    # `body.agent or username`, i.e. the caller names itself, and a
    # guard reading the checked party's account of itself is not a guard (#893).
    self_reviewed = services.caller_implemented_task(
        task,
        principal_id=principal_id,
        username=username,
    )
    await repo.insert_machine_review(
        db,
        task_id=task_id,
        submission_generation=generation,
        profile=profile,
        self_reviewed=self_reviewed,
        harness_skill=body.harness_skill,
        harness_version=body.harness_version,
        agent_count=body.agent_count,
        tokens_spent=body.tokens_spent,
        duration_ms=body.duration_ms,
        # #1036: origin travels in the DATA, not only in a feed line. A report
        # the hub transcribed from a run's final text is a weaker fact than one
        # the reviewer submitted through the contract — the run wrote it about
        # itself with nothing on the way to contradict it — and metrics or a
        # future steward must be able to weigh the two differently. The
        # orchestrator the report claims is kept beside the marker, not
        # overwritten: it is still the reviewer's own statement.
        orchestrator=(
            body.orchestrator
            if origin == ORIGIN_MCP
            else f"{origin}:{body.orchestrator or 'unknown'}"[:100]
        ),
        model=body.model,
        raw_count=raw_count,
        findings_confirmed=_json.dumps(
            [f.model_dump(exclude_none=True) for f in body.findings_confirmed],
            ensure_ascii=False,
        ),
        findings_rejected=_json.dumps(
            [f.model_dump(exclude_none=True) for f in body.findings_rejected],
            ensure_ascii=False,
        ),
        submitted_by=(body.agent or username)[:100],
        incomplete=incomplete,
        unresolved=_json.dumps(
            [f.model_dump(exclude_none=True) for f in body.unresolved],
            ensure_ascii=False,
        ),
        lost_dimensions=_json.dumps(body.lost_dimensions, ensure_ascii=False),
        # #1025: the report's owner as the TOKEN says, beside submitted_by
        # which stays the caller's self-description. The dispatch sweep
        # matches on this and on nothing self-reported.
        principal_id=principal_id,
    )
    await repo.insert_event(
        db,
        kind="machine_review_completed",
        task_id=task_id,
        actor=(body.agent or username)[:100],
        payload={
            "confirmed": len(body.findings_confirmed),
            "rejected": len(body.findings_rejected),
            "raw": raw_count,
            "generation": generation,
        },
    )
    # #750: a report that surfaced NO candidates, ran ONE agent and counted
    # NO tokens is the shape of a harness that never actually ran — 60 such
    # reports landed in 36 minutes on 2026-08-19 (cursor_cloud), silently
    # gutting the filtration metrics and, later, the auto-verdict (#745
    # already refuses raw_count=0). Warned once per generation, never
    # refused: the report itself is still worth keeping as evidence.
    if raw_count == 0:
        prior_zero = await fetchall(
            db,
            "SELECT COUNT(*) AS n FROM machine_reviews "
            "WHERE task_id=? AND submission_generation=? AND raw_count=0",
            (task_id, generation),
        )
        if int(prior_zero[0]["n"]) == 1:
            single_agent = (body.agent_count or 0) <= 1
            no_tokens = body.tokens_spent is None
            detail = (
                "похоже, харнесс не запускался (agent_count≤1, токены не посчитаны)"
                if single_agent and no_tokens
                else "проверьте, что фазы измерений и адъюдикации исполнялись"
            )
            await repo.add_task_update(
                db,
                task_id,
                "hub",
                "alert",
                (
                    "Machine-review с raw_count=0: ноль кандидатов — это "
                    f"отсутствие данных, а не отсутствие находок; {detail}. "
                    "Отчёт принят, но автовердикт по нему невозможен, а "
                    "«чисто» не подтверждено (#750)."
                ),
            )
    # #1012/#1025: the hub may already have called a reviewer for this very
    # submission. A second report is not refused — two profiles on one
    # generation is a real shape (#879) — but it must not arrive silently:
    # on 2026-08-28 a hand-run report of 71296 tokens was compared with a
    # dispatch that had spent 2574930, raising an audit alert about nobody's
    # dishonesty (#1011 gen 1). With a pinned reviewer principal the hub now
    # KNOWS whose report arrived: the dispatched run's own report passes
    # without ceremony, a foreign one is named once and stays out of the
    # spend reconciliation. Without a pin nobody can tell, and the old
    # warning by agent name stands.
    dispatch = await repo.get_review_dispatch_for_generation(db, task_id, generation)
    if dispatch is not None and dispatch["reviewer_principal_id"] is not None:
        if (dispatch["status"] or "") == "active" and principal_id != dispatch[
            "reviewer_principal_id"
        ]:
            await repo.add_task_update(
                db,
                task_id,
                "hub",
                "alert",
                "Отчёт machine-review принят от другого принципала: по этой "
                "сдаче уже вызвано диспетчерское ревью, и его прогон ещё не "
                "отчитался. Диспетч продолжает ждать СВОЙ отчёт, сверка "
                "расходов по чужому отчёту не выполняется (#1012, #1025).",
            )
    elif dispatch is not None and (dispatch["agent_id"] or "").strip() not in (
        "",
        (body.agent or username or "").strip(),
    ):
        await repo.add_task_update(
            db,
            task_id,
            "hub",
            "alert",
            (
                "Второй отчёт по этой сдаче: хаб уже вызвал ревьюера "
                f"{dispatch['model'] or 'неизвестной модели'} "
                f"(агент {dispatch['agent_id']}, статус {dispatch['status']}). "
                "Отчёт принят и не заменяет первый, но сверка расходов "
                "сопоставляет задекларированные токены с расходом "
                "ДИСПЕТЧЕРСКОГО прогона — расхождение в алерте аудита будет "
                "означать столкновение двух прогонов, а не чью-то нечестность "
                "(#757, #1012)."
            ),
        )
    await db.commit()
    await db_module.log_activity(
        db,
        "machine_review_completed",
        f"Task #{task_id}: machine review — {raw_count} raw → "
        f"{len(body.findings_confirmed)} confirmed, "
        f"{len(body.findings_rejected)} rejected",
    )
    saved = await repo.get_latest_machine_review(db, task_id)
    if saved is None:  # pragma: no cover - the INSERT above just ran
        raise HTTPException(404, "machine review not found")
    view = MachineReviewView(**dict(saved))
    view.is_current = view.submission_generation == generation

    # Auto-verdict (#745): a clean report in a project whose policy allows
    # it gets its APPROVED right here. Best-effort by contract — the report
    # intake must never fail because the autopilot stumbled.
    try:
        from hub.services.auto_verdict import maybe_auto_verdict

        await maybe_auto_verdict(db, task_id)
    except Exception:  # noqa: BLE001 - degradation is the contract
        log.exception("auto-verdict failed for task #%s", task_id)

    # The ladder (#879): a cheap run that declared it did not finish buys the
    # heavy profile instead of handing a human unfinished work. After the
    # auto-verdict, not before — an incomplete report can never earn one
    # (auto_verdict refuses on `incomplete`), so the order costs nothing and
    # keeps the clean path first. Best-effort like the verdict above: the
    # report intake must not fail because the ladder stumbled.
    try:
        from hub.services.review_dispatch import maybe_top_up_incomplete

        await maybe_top_up_incomplete(db, task_id)
    except Exception:  # noqa: BLE001 - degradation is the contract
        log.exception("review top-up failed for task #%s", task_id)

    return view
