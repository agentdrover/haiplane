"""Weekly share of executor orders submitted without a human (#1980).

One formula in the hub instead of hand counts that disagreed (4/7 by memory,
3/9 by ``executor_orders``). The unit is a closed order. ``autonomous`` is a
PROXY for "without a human": the launcher reported a submission
(``report_outcome='submitted'``) AND the hub closed the order for it
(``close_reason='submitted'``). A late report (``reported_at`` after
``closed_at``) changes ``report_outcome`` and so recounts the week of closing.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from typing import Any

import aiosqlite

from hub.db import fetchall
from hub.services.ci_timing import ci_minutes_metrics

TARGET = 0.9
DEFINITION = (
    "autonomous = report_outcome 'submitted' AND close_reason 'submitted' "
    "(proxy for 'without a human'); rate = autonomous / orders_closed over "
    "orders with state closed, weeks by closed_at, ISO week, UTC; a late "
    "report recounts the week of closing"
)
_TS_FORMAT = "%Y-%m-%d %H:%M:%S"
#: A window longer than this starts its week grid at the first closed order, so an
#: open start (``date_to`` alone, from 1970) does not print thousands of empty weeks.
MAX_EMPTY_LEAD_WEEKS = 104


def _monday(moment: datetime) -> datetime:
    day = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    return day - timedelta(days=day.weekday())


def _week_key(monday: datetime) -> str:
    iso = monday.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _week_row(monday: datetime, start: datetime, end: datetime) -> dict[str, Any]:
    return {
        "week": _week_key(monday),
        "start": monday.date().isoformat(),
        "partial": monday < start or monday + timedelta(days=7) > end,
        "orders_closed": 0,
        "autonomous": 0,
        "rate": None,
        "by_outcome": {},
        "by_reason_code": {},
    }


def _fill(row: dict[str, Any], orders: list[Any]) -> None:
    outcomes: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    autonomous = 0
    for order in orders:
        outcome = order["report_outcome"] or "none"
        outcomes[outcome] += 1
        if outcome == "submitted" and order["close_reason"] == "submitted":
            autonomous += 1
        else:
            reasons[order["report_reason_code"] or "unknown"] += 1
    row["orders_closed"] = len(orders)
    row["autonomous"] = autonomous
    row["rate"] = round(autonomous / len(orders), 3) if orders else None
    row["by_outcome"] = dict(outcomes.most_common())
    row["by_reason_code"] = dict(sorted(reasons.items(), key=lambda i: (-i[1], i[0])))


async def executor_autonomy_metrics(
    db: aiosqlite.Connection, scope: Any
) -> dict[str, Any]:
    """Per host, per ISO week of the window: orders closed and how many were unaided."""
    where, params = scope.where("o.closed_at", task_column="o.task_id")
    rows = await fetchall(
        db,
        "SELECT o.host, o.closed_at, o.report_outcome, o.close_reason, "  # nosec B608
        "o.report_reason_code FROM executor_orders o "
        f"WHERE o.state = 'closed' AND o.closed_at IS NOT NULL AND {where} "
        "ORDER BY o.id",
        params,
    )
    start, end = scope.bounds()
    first = (
        _monday(datetime.strptime(min(r["closed_at"] for r in rows)[:19], _TS_FORMAT))
        if rows
        else None
    )
    cursor = _monday(start)
    if first is not None and first - cursor > timedelta(weeks=MAX_EMPTY_LEAD_WEEKS):
        cursor = first
    grid: list[datetime] = []
    while cursor < end:
        grid.append(cursor)
        cursor += timedelta(days=7)
    buckets: dict[str, dict[datetime, list[Any]]] = {}
    for row in rows:
        closed = datetime.strptime(row["closed_at"][:19], _TS_FORMAT)
        buckets.setdefault(row["host"], {}).setdefault(_monday(closed), []).append(row)
    hosts: dict[str, Any] = {}
    for host in sorted(buckets):
        weeks = []
        for monday in grid:
            week = _week_row(monday, start, end)
            _fill(week, buckets[host].get(monday, []))
            weeks.append(week)
        hosts[host] = {"weeks": weeks}
    return {"target": TARGET, "definition": DEFINITION, "hosts": hosts}


async def statistics_sections(db: aiosqlite.Connection, scope: Any) -> dict[str, Any]:
    """Sections that are statistics outside compare/series: CI minutes (#1700) and
    executor autonomy (#1980)."""
    return {
        "ci_minutes": await ci_minutes_metrics(db, scope),
        "executor_autonomy": await executor_autonomy_metrics(db, scope),
    }
