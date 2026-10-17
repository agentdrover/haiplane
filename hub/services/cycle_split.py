"""Time of a task split in two: the queue and the path after the start (#1790).

``_cycle_times`` has always reported ``completed_at − ready_at`` as ONE number,
and that number joins two different things: waiting for somebody to start
(approval, the executor queue) and what happens after the start (work, CI,
review, rework, delivery). The 17.10.2026 analysis of production showed the
cost: the feature median "grew" from 3 to 41 hours, but 50 of 137 features were
old backlog that had waited more than 48 hours (median 229 hours), while the
time after the start went from 0.5 to 3.8 hours.

The two intervals are measured here, each over its OWN sample with its OWN
reasons for leaving a row out, and are attached to the rows of the old total,
which is not touched:

* ``queue``       = ``started_at − ready_at``;
* ``since_start`` = ``completed_at − started_at``. It is "from the start to
  the completion" and NOT the executor's working time: it includes review, CI,
  questions, repeated queueing and delivery.

A missing fact is a reason, never a number (#810): a row without a stamp is
counted under the reason it has no stamp. ``tasks.started_at`` exists only for
work that began after it started being written; older rows carry
``start_history_unknown`` and are named so instead of being dated today.
A zero or negative interval follows the rule of the total: it is not a fast
task, it is a pair of stamps that cannot be a measurement, and it is counted
apart (``zero`` / ``negative`` or ``ready_after_start``).

Only real leaves are measured: ``task_type`` task/subtask with no children.
A container (epic, feature, or any task that has children) closes by roll-up
when its children do, so its interval says nothing about work; it is counted in
``rollup`` and kept out of the medians.
"""

from __future__ import annotations

import statistics
from typing import Any

import aiosqlite

from hub.db import fetchall
from hub.services.metrics_scope import Scope

# Reasons a row stays out of a median. The keys are the contract: the page,
# the CLI JSON and the MCP echo carry them as they are.
NO_COMPLETION = "no_completion"
NO_READY_AT = "no_ready_at"
NO_STARTED_AT = "no_started_at"
READY_AFTER_START = "ready_after_start"
START_HISTORY_UNKNOWN = "start_history_unknown"
NEGATIVE = "negative"
ZERO = "zero"
UNREADABLE = "unreadable"

QUEUE_REASONS = (
    NO_COMPLETION,
    NO_READY_AT,
    NO_STARTED_AT,
    READY_AFTER_START,
    ZERO,
    UNREADABLE,
)
SINCE_START_REASONS = (
    NO_COMPLETION,
    START_HISTORY_UNKNOWN,
    NO_STARTED_AT,
    NEGATIVE,
    ZERO,
    UNREADABLE,
)

_LEAF_SQL = (
    "(task_type IN ('task', 'subtask') "
    "AND NOT EXISTS (SELECT 1 FROM tasks c WHERE c.parent_id = tasks.id))"
)


def _span(hours: float | None, negative: str) -> tuple[float | None, str]:
    """The measured hours, or the reason a computed span cannot be one."""
    if hours is None:
        # Both stamps are present but one of them is not a date SQLite reads.
        return None, UNREADABLE
    if hours < 0:
        return None, negative
    if hours == 0:
        return None, ZERO
    return hours, ""


def _queue_of(row: Any) -> tuple[float | None, str]:
    if row["no_completion"]:
        return None, NO_COMPLETION
    if row["ready_at"] is None:
        return None, NO_READY_AT
    if row["started_at"] is None:
        return None, NO_STARTED_AT
    return _span(row["queue_hours"], READY_AFTER_START)


def _since_start_of(row: Any) -> tuple[float | None, str]:
    if row["no_completion"]:
        return None, NO_COMPLETION
    if row["start_history_unknown"]:
        return None, START_HISTORY_UNKNOWN
    if row["started_at"] is None:
        return None, NO_STARTED_AT
    return _span(row["since_start_hours"], NEGATIVE)


def _interval(
    values: list[float], reasons: dict[str, int], keys: tuple[str, ...]
) -> dict[str, Any]:
    return {
        "tasks": len(values),
        "median_hours": round(statistics.median(values), 2) if values else None,
        **{key: reasons.get(key, 0) for key in keys},
    }


def _empty_row() -> dict[str, Any]:
    return {
        "queue": ([], {}),
        "since_start": ([], {}),
        "rollup": 0,
    }


def empty_split() -> dict[str, Any]:
    """The split of a work type that has no completed row in the window."""
    return {
        "queue": _interval([], {}, QUEUE_REASONS),
        "since_start": _interval([], {}, SINCE_START_REASONS),
        "rollup": {"tasks": 0},
    }


async def cycle_split(db: aiosqlite.Connection, scope: Scope) -> dict[str, dict]:
    """Queue, since-start and roll-up per work type, over the window's rows.

    The window is the one of the total: a row belongs to it by ``completed_at``
    and, only when it has none, by ``updated_at`` as a "recent enough to count"
    test — never as a duration (#810). Such a row is counted as
    ``no_completion`` in both intervals.
    """
    in_completed, p_completed = scope.where("completed_at", task_column="id")
    in_updated, p_updated = scope.where("updated_at", task_column="id")
    rows = await fetchall(
        db,
        "SELECT work_type, completed_at IS NULL AS no_completion, ready_at, "
        "started_at, start_history_unknown, "
        f"{_LEAF_SQL} AS is_leaf, "  # nosec B608 - constant fragment
        "(julianday(started_at) - julianday(ready_at)) * 24.0 AS queue_hours, "
        "(julianday(completed_at) - julianday(started_at)) * 24.0 "
        "AS since_start_hours "
        "FROM tasks WHERE status='completed' "
        f"AND ({in_completed} "  # nosec B608 - constant fragment
        f"OR (completed_at IS NULL AND {in_updated}))",
        (*p_completed, *p_updated),
    )
    acc: dict[str, dict[str, Any]] = {}
    for row in rows:
        entry = acc.setdefault(row["work_type"] or "feature", _empty_row())
        if not row["is_leaf"]:
            entry["rollup"] += 1
            continue
        for name, measure in (("queue", _queue_of), ("since_start", _since_start_of)):
            hours, reason = measure(row)
            values, reasons = entry[name]
            if hours is not None:
                values.append(hours)
            else:
                reasons[reason] = reasons.get(reason, 0) + 1
    return {
        wt: {
            "queue": _interval(e["queue"][0], e["queue"][1], QUEUE_REASONS),
            "since_start": _interval(
                e["since_start"][0], e["since_start"][1], SINCE_START_REASONS
            ),
            "rollup": {"tasks": e["rollup"]},
        }
        for wt, e in acc.items()
    }
