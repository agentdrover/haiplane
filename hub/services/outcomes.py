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

from datetime import UTC, datetime, timedelta
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


# Observation window after the fix reached production (#1568): the mode and the
# median of the real ``outcome_deadline`` phrasings. A constant, not a column and
# not a parse of that free text (#839).
OUTCOME_WINDOW_DAYS = 14

# Task types whose release is the release of their descendants.
_ROLLUP_TYPES = frozenset({"epic", "feature"})


def outcome_due_at(fix_released_at: str | None) -> datetime | None:
    """First deploy of the fix plus the window; None when there is no such deploy."""
    released = _parse_stamp(fix_released_at)
    if released is None:
        return None
    return released + timedelta(days=OUTCOME_WINDOW_DAYS)


_VERDICT_TO_STATUS = {
    OutcomeVerdict.moved.value: OutcomeHypothesisStatus.confirmed,
    OutcomeVerdict.not_moved.value: OutcomeHypothesisStatus.refuted,
    OutcomeVerdict.unmeasurable.value: OutcomeHypothesisStatus.unmeasurable,
}


def derive_outcome_status(
    *,
    outcome_metric: str,
    answers: list[dict[str, Any]],
    fix_released_at: str | None,
    task_status: str | None = None,
    now: datetime | None = None,
) -> OutcomeHypothesisStatus:
    """Assemble the hypothesis state from facts that already exist (#576).

    ``fix_released_at`` is the first deploy of the task's fix (#1568). Without
    one the deadline cannot be computed: unknown, not overdue (#839).
    """
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
    due = outcome_due_at(fix_released_at)
    if due is None:
        return OutcomeHypothesisStatus.unknown
    if due <= (now or datetime.now(UTC)):
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


async def resolve_outcome_status(
    db: aiosqlite.Connection,
    task: dict[str, Any],
    answers: list[dict[str, Any]],
) -> tuple[OutcomeHypothesisStatus, str | None]:
    """The one place a task's outcome status and due date are decided (#1568).

    The card, the review brief and the debt list all call this, so they cannot
    disagree. Returns the status and ``due_on`` (an ISO date, or None).
    """
    fix_at: str | None = None
    if task.get("status") == "completed":
        fix_at = await repository.first_fix_deploy_at(
            db,
            int(task["id"]),
            with_descendants=str(task.get("task_type") or "") in _ROLLUP_TYPES,
        )
    status = derive_outcome_status(
        outcome_metric=str(task.get("outcome_metric") or ""),
        answers=answers,
        fix_released_at=fix_at,
        task_status=str(task.get("status") or ""),
    )
    due = outcome_due_at(fix_at)
    return status, due.date().isoformat() if due else None


async def outcome_status_for_task(
    db: aiosqlite.Connection, task: dict[str, Any]
) -> OutcomeHypothesisStatus:
    """Derived status for one task read (#576)."""
    answers = [
        _answer_view(row)
        for row in await repository.list_outcome_answers_for_task(db, int(task["id"]))
    ]
    status, _ = await resolve_outcome_status(db, task, answers)
    return status


async def outcome_debt(db: aiosqlite.Connection) -> dict[str, Any]:
    """Completed tasks whose stated outcome has never been answered.

    ``outcome_deadline`` is returned verbatim and never parsed: it is free text
    holding event descriptions rather than dates, so it is something a human
    reads, not something this list filters on.
    """
    rows = await repository.list_outcome_debt(db)
    answers = await _answers_by_task(db)
    items: list[dict[str, Any]] = []
    answered_items: list[dict[str, Any]] = []
    for row in rows:
        finished = row["completed_at"] or row["updated_at"]
        task_answers = answers.get(row["id"], [])
        status, due_on = await resolve_outcome_status(
            db, {**dict(row), "status": "completed"}, task_answers
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
            "due_on": due_on,
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

    def _with(status: OutcomeHypothesisStatus) -> list[dict[str, Any]]:
        return [i for i in items if i["outcome_status"] == status.value]

    overdue = _with(OutcomeHypothesisStatus.unanswered)
    observing = _with(OutcomeHypothesisStatus.not_due)
    unknown = _with(OutcomeHypothesisStatus.unknown)
    return {
        "total": len(items),
        "answered_total": len(answered_items),
        "items": items,
        "answered": answered_items,
        "overdue": overdue,
        "overdue_total": len(overdue),
        "observing": observing,
        "observing_total": len(observing),
        "unknown": unknown,
        "unknown_total": len(unknown),
        "window_days": OUTCOME_WINDOW_DAYS,
        "note": (
            "Every task in `items` promised a number would move and was never "
            "asked whether it did. `answered` holds the ones somebody came back "
            "to, with the last verdict and what was measured - including "
            "not_moved and unmeasurable, which are answers too. "
            f"`overdue`: the fix first reached production {OUTCOME_WINDOW_DAYS} "
            "days ago or more (`due_on` passed). `observing`: the window is "
            "still open. `unknown`: no recorded release of the fix (no merge, "
            "unreleased merge, or a release the hub never saw) - a gap in "
            "the record, not a debt. The due date is a machine fact, not a "
            "parse of outcome_deadline: that is free text and is not used for "
            "filtering, so nothing is hidden behind a value that cannot be parsed."
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
