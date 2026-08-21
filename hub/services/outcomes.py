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
    return {
        "total": len(items),
        "answered_total": len(answered_items),
        "items": items,
        "answered": answered_items,
        "note": (
            "Every task in `items` promised a number would move and was never "
            "asked whether it did. `answered` holds the ones somebody came back "
            "to, with the last verdict and what was measured - including "
            "not_moved and unmeasurable, which are answers too. "
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
        grouped.setdefault(row["task_id"], []).append(
            {
                "id": row["id"],
                "verdict": row["verdict"],
                "measured_value": row["measured_value"],
                "note": row["note"],
                "answered_by": row["answered_by"],
                "answered_at": row["answered_at"],
            }
        )
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
    )
    answers = (await _answers_by_task(db)).get(task_id, [])
    return {
        "answer_id": answer_id,
        "task_id": task_id,
        "answers": len(answers),
        "latest_answer": answers[-1] if answers else None,
    }
