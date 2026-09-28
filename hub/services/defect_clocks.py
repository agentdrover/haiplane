"""Production defect clocks: time-to-detect and time-to-restore (#916).

* time-to-detect  = ``detected_at`` − ``releases.deployed_at`` of the release
  bound to the defect through ``tasks.release_id`` (#917);
* time-to-restore = ``resolved_at`` − ``detected_at``; ``resolved_at`` is
  stamped when the defect is closed (``RESOLVED_AT_ON_COMPLETION_SQL``).

Both are read from recorded facts and from nothing else. A row missing a fact
is counted under the reason it is missing and never enters a median — the
same rule cycle time follows since #810, where filling a missing stamp from
``updated_at`` turned the median into a measure of how many rows were filled
in. A negative duration is not a fast recovery, it is a wrong fact (a release
bound after the defect was seen, a stamp typed by hand), so it is counted
apart as well.

Window membership is decided by ``created_at`` — when the defect was filed —
and is never used as a duration.
"""

from __future__ import annotations

import statistics
from typing import Any

import aiosqlite

from hub.db import fetchall

# Reasons a row stays out of a median. The keys are the contract: the page,
# MCP and CLI print them as they are.
TTD_NO_DETECTED_AT = "no_detected_at"
TTD_NO_RELEASE = "no_release"
TTD_NO_DEPLOYED_AT = "no_deployed_at"
TTD_NEGATIVE = "detected_before_deploy"
TTR_NO_DETECTED_AT = "no_detected_at"
TTR_OPEN = "open"
TTR_NO_RESOLVED_AT = "closed_without_resolved_at"
TTR_NEGATIVE = "resolved_before_detected"


def _detect(row: dict[str, Any]) -> tuple[float | None, str]:
    if not row["detected_at"]:
        return None, TTD_NO_DETECTED_AT
    if row["release_id"] is None or row["release_found"] is None:
        return None, TTD_NO_RELEASE
    if not row["deployed_at"]:
        return None, TTD_NO_DEPLOYED_AT
    if row["detect_hours"] is None or row["detect_hours"] < 0:
        return None, TTD_NEGATIVE
    return float(row["detect_hours"]), ""


def _restore(row: dict[str, Any]) -> tuple[float | None, str]:
    if not row["detected_at"]:
        return None, TTR_NO_DETECTED_AT
    if not row["resolved_at"]:
        return None, TTR_OPEN if row["status"] != "completed" else TTR_NO_RESOLVED_AT
    if row["restore_hours"] is None or row["restore_hours"] < 0:
        return None, TTR_NEGATIVE
    return float(row["restore_hours"]), ""


def _summary(values: list[float], unmeasurable: dict[str, int]) -> dict[str, Any]:
    return {
        "measured": len(values),
        "median_hours": round(statistics.median(values), 2) if values else None,
        "unmeasurable": dict(sorted(unmeasurable.items())),
        "unmeasurable_total": sum(unmeasurable.values()),
    }


async def prod_defect_clocks(db: aiosqlite.Connection, since: str) -> dict[str, Any]:
    """Medians of both clocks over prod defects filed in the window."""
    rows = await fetchall(
        db,
        "SELECT t.status, t.detected_at, t.resolved_at, t.release_id, "
        "r.id AS release_found, r.deployed_at, "
        "(julianday(t.detected_at) - julianday(NULLIF(r.deployed_at, ''))) * 24.0 "
        "AS detect_hours, "
        "(julianday(t.resolved_at) - julianday(t.detected_at)) * 24.0 "
        "AS restore_hours "
        "FROM tasks t LEFT JOIN releases r ON r.id = t.release_id "
        "WHERE t.found_in = 'prod' AND t.created_at >= datetime('now', ?)",
        (since,),
    )
    clocks: dict[str, tuple[list[float], dict[str, int]]] = {
        "time_to_detect": ([], {}),
        "time_to_restore": ([], {}),
    }
    for raw in rows:
        row = dict(raw)
        for name, measure in (
            ("time_to_detect", _detect),
            ("time_to_restore", _restore),
        ):
            hours, reason = measure(row)
            values, unmeasurable = clocks[name]
            if reason:
                unmeasurable[reason] = unmeasurable.get(reason, 0) + 1
            elif hours is not None:
                values.append(hours)
    return {
        "defects": len(rows),
        **{name: _summary(*pair) for name, pair in clocks.items()},
    }
