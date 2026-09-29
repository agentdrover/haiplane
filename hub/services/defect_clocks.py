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

The same set of rows — ``found_in = 'prod'`` filed in the window — is also the
measured escape count (#918), and a prod defect bound to a release through
``release_id`` is what makes that release a failed change (change failure
rate). One definition of "prod defect" serves all three numbers.
"""

from __future__ import annotations

import statistics
from typing import Any

import aiosqlite

from hub.db import fetchall
from hub.services.defect_release import project_scope

# The one definition of "a prod defect of the window" (#916, #918).
PROD_DEFECT_IN_WINDOW_SQL = "t.found_in = 'prod' AND t.created_at >= datetime('now', ?)"

# Below this many deploys a share is noise: the page and the MCP text print
# "small sample" instead of a percentage; the data still carries the rate.
CFR_MIN_DEPLOYS = 5

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
        f"WHERE {PROD_DEFECT_IN_WINDOW_SQL}",  # nosec B608 - constant SQL
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


async def measured_escapes(db: aiosqlite.Connection, since: str) -> int:
    """Prod defects filed in the window, read from ``found_in`` (#918).

    Recorded, not derived: no feature ancestor and no completion stamp are
    needed, so a prod defect hanging under an epic or under nothing is counted
    here instead of disappearing into ``bugs_without_feature``.
    """
    rows = await fetchall(
        db,
        f"SELECT COUNT(*) AS n FROM tasks t WHERE {PROD_DEFECT_IN_WINDOW_SQL}",  # nosec B608 - constant SQL
        (since,),
    )
    return int(rows[0]["n"] or 0) if rows else 0


# #914: the stages in the order a miss gets dearer, unknown last and apart.
SHIFT_LEFT_STAGES = ("review", "ci", "test", "staging", "prod", "unknown")

# What counts as a defect for the shift-left slice: a bug, or any task whose
# stage was recorded. The second half keeps the prod bucket equal to
# ``measured_escapes`` — a prod defect filed under another work type is counted
# in both, never in one only.
DEFECT_IN_WINDOW_SQL = (
    "(t.work_type = 'bug' OR COALESCE(t.found_in, 'unknown') != 'unknown') "
    "AND t.created_at >= datetime('now', ?)"
)


def _share(part: int, whole: int) -> float | None:
    return round(part / whole, 3) if whole else None


async def shift_left(db: aiosqlite.Connection, since: str) -> dict[str, Any]:
    """Defects filed in the window by the stage that caught them (#914).

    Every stage gets a row, an empty one too: a missing ``staging`` row and a
    zero in it read differently. ``unknown`` is a row like the others AND is
    repeated as ``unknown`` / ``unknown_share``: a share of the recorded
    stages alone would read as a finished picture while a third of the rows
    say nothing. With no defects the shares are ``None``, not zero.
    """
    rows = await fetchall(
        db,
        "SELECT COALESCE(t.found_in, 'unknown') AS stage, COUNT(*) AS n "
        f"FROM tasks t WHERE {DEFECT_IN_WINDOW_SQL} "  # nosec B608 - constant SQL
        "GROUP BY stage",
        (since,),
    )
    counts = {str(r["stage"]): int(r["n"] or 0) for r in rows}
    total = sum(counts.values())
    unknown = counts.get("unknown", 0)
    return {
        "defects": total,
        "recorded": total - unknown,
        "unknown": unknown,
        "unknown_share": _share(unknown, total),
        "by_stage": [
            {
                "stage": stage,
                "defects": counts.get(stage, 0),
                "share": _share(counts.get(stage, 0), total),
            }
            for stage in SHIFT_LEFT_STAGES
        ],
    }


async def _project_key(
    db: aiosqlite.Connection, project_id: int | None, cache: dict[Any, Any]
) -> int | None:
    """The project a release belongs to, by the #915 rule in ``project_scope``:
    a project-less release is the default project's; a scope names its own
    project last."""
    if project_id not in cache:
        cache[project_id] = (await project_scope(db, project_id))[-1]
    return cache[project_id]


def _cfr_row(slug: str, deploys: int, failed: int) -> dict[str, Any]:
    return {
        "project": slug,
        "deploys": deploys,
        "failed_deploys": failed,
        "rate": round(failed / deploys, 3),
        "small_sample": deploys < CFR_MIN_DEPLOYS,
    }


async def change_failure_rate(db: aiosqlite.Connection, since: str) -> dict[str, Any]:
    """Share of successful deploys of the window that a prod defect points at.

    Denominator: ``releases`` rows with status ``success`` whose
    ``deployed_at`` falls in the window, per project. Numerator: those among
    them with at least one ``found_in = 'prod'`` task whose ``release_id`` is
    that release (#917) — two defects on one deploy make it failed once.
    Prod defects of the window with no release bound cannot point at a deploy;
    they are counted in ``defects_without_release``, never guessed.
    """
    rows = await fetchall(
        db,
        "SELECT r.project_id, EXISTS (SELECT 1 FROM tasks t "
        "WHERE t.release_id = r.id AND t.found_in = 'prod') AS failed "
        "FROM releases r WHERE r.status = 'success' "
        "AND r.deployed_at >= datetime('now', ?)",
        (since,),
    )
    counts: dict[int | None, list[int]] = {}
    cache: dict[Any, Any] = {}
    for row in rows:
        key = await _project_key(db, row["project_id"], cache)
        bucket = counts.setdefault(key, [0, 0])
        bucket[0] += 1
        bucket[1] += 1 if row["failed"] else 0
    slugs = {
        int(r["id"]): str(r["slug"])
        for r in await fetchall(db, "SELECT id, slug FROM projects")
    }
    unbound = await fetchall(
        db,
        "SELECT COUNT(*) AS n FROM tasks t "  # nosec B608 - constant SQL
        f"WHERE {PROD_DEFECT_IN_WINDOW_SQL} AND t.release_id IS NULL",
        (since,),
    )
    by_project = [
        _cfr_row(
            slugs.get(key, f"project-{key}") if key is not None else "default", *pair
        )
        for key, pair in counts.items()
    ]
    return {
        "min_sample": CFR_MIN_DEPLOYS,
        "by_project": sorted(by_project, key=lambda r: r["project"]),
        "defects_without_release": int(unbound[0]["n"] or 0) if unbound else 0,
    }
