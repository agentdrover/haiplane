"""Outcome debt: the hypotheses the Hub collects, and the answers to them.

Every task must state an ``outcome_metric`` to pass DoR, and until #766 nothing
ever read one back. A process that treats an unverified assertion as an
assumption should apply that rule to its own assertions - this module is the
read that does.

#766 shipped the list read-only on purpose: the open question was whether these
metrics can be answered at all, and building storage before knowing that would
have been a guess. #810 answered it on a live case - the numbers promised
before the release were checked against production after it - so #819 adds the
place to record such a check.

The debt therefore has two sides now: tasks nobody has come back to, and tasks
somebody has. Both counts are reported. A list that could only grow measured
the age of the backlog rather than the habit of checking, which is the same
defect class this module exists to expose.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import aiosqlite

from hub import repository
from hub.models import OutcomeHypothesisStatus, OutcomeVerdict


def _days_since(stamp: str | None) -> int | None:
    """Whole days since an ISO-ish SQLite timestamp, or None if unusable."""
    if not stamp:
        return None
    text = str(stamp).strip().replace(" ", "T")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return max(0, (datetime.now(UTC) - moment).days)


def _parse_stamp(stamp: str | None) -> datetime | None:
    """ISO-ish SQLite timestamp, or None if it cannot be compared."""
    if not stamp:
        return None
    text = str(stamp).strip().replace(" ", "T")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment


def _snapshot_matches(answer: dict[str, Any], outcome_metric: str) -> bool:
    """Legacy rows have no snapshot and still count as an answer to the current metric."""
    snap = answer.get("hypothesis_snapshot")
    if snap is None or not str(snap).strip():
        return True
    return str(snap).strip() == outcome_metric.strip()


def _deadline_reached(
    completed_at: str | None, latest_release: dict[str, Any] | None
) -> bool:
    """True only when a successful release is known to have landed after the work finished.

    No record is unknown, not overdue: collapsing those is the #839 failure.
    ``outcome_deadline`` is never consulted — it is free text.
    """
    if not latest_release:
        return False
    finished = _parse_stamp(completed_at)
    released = _parse_stamp(latest_release.get("deployed_at"))
    if finished is None or released is None:
        return False
    return released >= finished


_VERDICT_TO_STATUS = {
    OutcomeVerdict.moved.value: OutcomeHypothesisStatus.confirmed,
    OutcomeVerdict.not_moved.value: OutcomeHypothesisStatus.refuted,
    OutcomeVerdict.unmeasurable.value: OutcomeHypothesisStatus.unmeasurable,
}


def derive_outcome_status(
    *,
    outcome_metric: str,
    answers: list[dict[str, Any]],
    completed_at: str | None,
    latest_release: dict[str, Any] | None,
    task_status: str | None = None,
) -> OutcomeHypothesisStatus:
    """Assemble the hypothesis state from facts that already exist (#576)."""
    if not str(outcome_metric or "").strip():
        return OutcomeHypothesisStatus.no_hypothesis

    matching = [row for row in answers if _snapshot_matches(row, outcome_metric)]
    if matching:
        verdict = str(matching[-1].get("verdict") or "")
        return _VERDICT_TO_STATUS.get(verdict, OutcomeHypothesisStatus.confirmed)
    if answers:
        return OutcomeHypothesisStatus.revised
    if task_status and task_status != "completed":
        return OutcomeHypothesisStatus.not_due
    if _deadline_reached(completed_at, latest_release):
        return OutcomeHypothesisStatus.unanswered
    return OutcomeHypothesisStatus.not_due


def _answer_view(row: aiosqlite.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "verdict": row["verdict"],
        "measured_value": row["measured_value"],
        "note": row["note"],
        "answered_by": row["answered_by"],
        "answered_at": row["answered_at"],
        "hypothesis_snapshot": row["hypothesis_snapshot"],
    }


async def outcome_status_for_task(
    db: aiosqlite.Connection, task: dict[str, Any]
) -> OutcomeHypothesisStatus:
    """Derived status for one task read (#576)."""
    answers = [
        _answer_view(row)
        for row in await repository.list_outcome_answers_for_task(db, int(task["id"]))
    ]
    project = await repository.resolve_project_for_task(db, int(task["id"]))
    release = None
    if project is not None:
        release = await repository.latest_successful_release(db, int(project["id"]))
    return derive_outcome_status(
        outcome_metric=str(task.get("outcome_metric") or ""),
        answers=answers,
        completed_at=task.get("completed_at") or task.get("updated_at"),
        latest_release=release,
        task_status=str(task.get("status") or ""),
    )


async def outcome_debt(db: aiosqlite.Connection) -> dict[str, Any]:
    """Completed tasks whose stated outcome has never been answered.

    ``outcome_deadline`` is returned verbatim and never parsed: it is free text
    holding event descriptions rather than dates, so it is something a human
    reads, not something this list filters on.
    """
    rows = await repository.list_outcome_debt(db)
    answers = await _answers_by_task(db)
    release_by_project: dict[int, dict[str, Any] | None] = {}
    items: list[dict[str, Any]] = []
    answered_items: list[dict[str, Any]] = []
    for row in rows:
        finished = row["completed_at"] or row["updated_at"]
        task_answers = answers.get(row["id"], [])
        project = await repository.resolve_project_for_task(db, row["id"])
        project_id = int(project["id"]) if project is not None else None
        if project_id is not None and project_id not in release_by_project:
            release_by_project[project_id] = await repository.latest_successful_release(
                db, project_id
            )
        status = derive_outcome_status(
            outcome_metric=str(row["outcome_metric"] or ""),
            answers=task_answers,
            completed_at=finished,
            latest_release=release_by_project.get(project_id) if project_id else None,
            task_status="completed",
        )
        entry = {
            "task_id": row["id"],
            "title": row["title"],
            "task_type": row["task_type"],
            "outcome_metric": row["outcome_metric"],
            "outcome_indicator": row["outcome_indicator"],
            # Free text, shown as written. See the module docstring.
            "outcome_deadline": row["outcome_deadline"],
            "outcome_revisit_condition": row["outcome_revisit_condition"],
            "completed_at": finished,
            "days_unanswered": _days_since(finished),
            "outcome_status": status.value,
        }
        if not task_answers:
            items.append(entry)
            continue
        # The latest answer is what a reader needs first; the count says
        # whether anyone came back more than once, which is the difference
        # between a released number and a number that held.
        entry["answers"] = len(task_answers)
        entry["latest_answer"] = task_answers[-1]
        answered_items.append(entry)
    overdue = [
        item
        for item in items
        if item["outcome_status"] == OutcomeHypothesisStatus.unanswered.value
    ]
    return {
        "total": len(items),
        "answered_total": len(answered_items),
        "items": items,
        "answered": answered_items,
        "overdue": overdue,
        "overdue_total": len(overdue),
        "note": (
            "Every task in `items` promised a number would move and was never "
            "asked whether it did. `answered` holds the ones somebody came back "
            "to, with the last verdict and what was measured - including "
            "not_moved and unmeasurable, which are answers too. "
            "`overdue` is the subset whose last successful release landed after "
            "completion — the machine due date, not a parse of outcome_deadline. "
            "outcome_deadline is free text and is not used for filtering, so "
            "nothing is hidden behind a value that cannot be parsed."
        ),
    }


async def _answers_by_task(
    db: aiosqlite.Connection,
) -> dict[int, list[dict[str, Any]]]:
    """Recorded answers grouped by task, oldest first within a task (#819)."""
    grouped: dict[int, list[dict[str, Any]]] = {}
    for row in await repository.list_outcome_answers(db):
        grouped.setdefault(row["task_id"], []).append(_answer_view(row))
    return grouped


async def answer_outcome(
    db: aiosqlite.Connection,
    *,
    task_id: int,
    verdict: str,
    measured_value: str,
    note: str = "",
    answered_by: str = "",
) -> dict[str, Any]:
    """Record one check of a completed task's outcome (#819).

    Refuses a task that is not completed and one that never stated a metric:
    an answer to a promise nobody made is noise in a list whose whole value is
    that every row means something.

    Does not touch the task: no status change, no claim required. The check
    happens after the work is done, often by someone who did not do it.
    """
    row = await repository.get_task(db, task_id)
    if row is None:
        raise LookupError(f"task {task_id} not found")
    task = dict(row)
    if task.get("status") != "completed":
        raise ValueError(
            f"task #{task_id} is {task.get('status')}, not completed - an "
            "outcome can only be answered after the work it describes shipped"
        )
    if not str(task.get("outcome_metric") or "").strip():
        raise ValueError(
            f"task #{task_id} never stated an outcome_metric, so there is "
            "nothing to answer"
        )
    answer_id = await repository.record_outcome_answer(
        db,
        task_id=task_id,
        verdict=verdict,
        measured_value=measured_value,
        note=note,
        answered_by=answered_by,
        hypothesis_snapshot=str(task.get("outcome_metric") or "").strip(),
    )
    answers = (await _answers_by_task(db)).get(task_id, [])
    return {
        "answer_id": answer_id,
        "task_id": task_id,
        "answers": len(answers),
        "latest_answer": answers[-1] if answers else None,
    }
