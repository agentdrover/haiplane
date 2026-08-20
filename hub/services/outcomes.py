"""Outcome debt: the hypotheses the Hub collects and never checks.

Every task must state an ``outcome_metric`` to pass DoR, and nothing has ever
read one back. A process that treats an unverified assertion as an assumption
should apply that rule to its own assertions - this module is the first read
that does.

Deliberately read-only. Recording an answer needs storage and is a separate
slice; the point of shipping the list alone is to find out whether these
metrics can be answered at all before building anywhere to put the answers.
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
    items: list[dict[str, Any]] = []
    for row in rows:
        finished = row["completed_at"] or row["updated_at"]
        items.append(
            {
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
        )
    return {
        "total": len(items),
        "items": items,
        "note": (
            "Every task here promised a number would move and was never asked "
            "whether it did. outcome_deadline is free text and is not used for "
            "filtering, so nothing is hidden behind a value that cannot be parsed."
        ),
    }
