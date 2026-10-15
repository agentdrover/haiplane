"""How many minutes CI cost a task: the timing of finished attempts (#1700).

The trusted workflow (``.github/workflows/ci-report.yml``, #1686) reads GitHub's
own job records of every finished ``ci.yml`` run of a task branch and sends them
here, whether or not the run produced a usable artifact. The hub keeps one row
per (task, run, attempt) and sums them in ``practice_metrics``.

Two rules:

* This is statistics. No gate reads it, and a refusal of this route never
  touches the CI report (``ci_run_reports``, #1687).
* An unknown duration is unknown. A run whose jobs could not be read, or that was
  cancelled before a job started, is counted apart as ``unknown_duration``; it is
  never a zero and never part of a median.
"""

from __future__ import annotations

import json
import statistics
from datetime import UTC, datetime
from typing import Any, Literal

import aiosqlite
from pydantic import BaseModel, Field

from hub.db import fetchall

STATE_OK = "ok"
STATE_UNAVAILABLE = "timing_unavailable"
#: The steps whose minutes the owner wants named. The reporter maps GitHub step
#: names to these keys; the hub only knows the keys.
STEP_KEYS = ("test", "mutations", "baseline", "ac_collect")
_TS_FORMAT = "%Y-%m-%d %H:%M:%S"
_MAX_STEP_SECONDS = 24 * 3600.0
BY_TASK_LIMIT = 20

RECEIVED_NOTE = (
    "пропуски считаются только среди полученных отчётов CI (ci_report_attempts), "
    "а не среди всех прогонов GitHub; окно отчётов — по времени получения"
)


class CIStepTiming(BaseModel):
    key: Literal["test", "mutations", "baseline", "ac_collect"]
    name: str = Field("", max_length=200)
    conclusion: str = Field("", max_length=40)
    # None for a skipped step or one whose times GitHub did not give.
    seconds: float | None = Field(default=None, ge=0, le=_MAX_STEP_SECONDS)


class CIJobTiming(BaseModel):
    name: str = Field("", max_length=200)
    conclusion: str = Field("", max_length=40)
    started_at: str | None = Field(default=None, max_length=40)
    completed_at: str | None = Field(default=None, max_length=40)
    steps: list[CIStepTiming] = Field(default_factory=list, max_length=20)


class CIRunTimingSubmit(BaseModel):
    """What the trusted workflow says one finished attempt cost (#1700)."""

    run_id: str = Field(..., min_length=1, max_length=32)
    run_attempt: str = Field(..., min_length=1, max_length=16)
    head_sha: str = Field("", max_length=64)
    event: str = Field("", max_length=40)
    conclusion: str = Field("", max_length=40)
    # The completion time the EVENT carries (the run's updated_at).
    completed_at: str = Field(..., min_length=1, max_length=40)
    state: Literal["ok", "timing_unavailable"] = "ok"
    reason: str = Field("", max_length=500)
    jobs: list[CIJobTiming] = Field(default_factory=list, max_length=50)


def _parse(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        moment = datetime.fromisoformat(raw.strip())
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _job_seconds(job: CIJobTiming) -> float | None:
    start, end = _parse(job.started_at), _parse(job.completed_at)
    if start is None or end is None:
        return None
    return max(0.0, (end - start).total_seconds())


def summarize(body: CIRunTimingSubmit) -> tuple[float | None, dict[str, float]]:
    """(runner seconds or None when unknown, seconds per named step)."""
    if body.state != STATE_OK:
        return None, {}
    known = [s for s in map(_job_seconds, body.jobs) if s is not None]
    steps: dict[str, float] = {}
    for job in body.jobs:
        for step in job.steps:
            if step.seconds is not None:
                steps[step.key] = steps.get(step.key, 0.0) + step.seconds
    return (sum(known) if known else None), steps


async def store_ci_run_timing(
    db: aiosqlite.Connection, task_id: int, body: CIRunTimingSubmit, *, reported_by: str
) -> dict[str, Any]:
    """Keep one attempt's timing; the caller holds the write transaction.

    Idempotent by (task, run, attempt). A degraded resend never replaces timing
    that was read. LookupError: no such task; ValueError: unreadable time.
    """
    if not await fetchall(db, "SELECT 1 FROM tasks WHERE id = ?", (task_id,)):
        raise LookupError("task not found")
    completed = _parse(body.completed_at)
    if completed is None:
        raise ValueError(f"completed_at {body.completed_at!r} is not an ISO time")
    runner, steps = summarize(body)
    await db.execute(
        "INSERT INTO ci_attempt_timings (task_id, run_id, run_attempt, head_sha, "
        "event, conclusion, completed_at, state, reason, jobs, runner_seconds, "
        "steps_seconds, reported_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(task_id, run_id, run_attempt) DO UPDATE SET "
        "head_sha=excluded.head_sha, event=excluded.event, "
        "conclusion=excluded.conclusion, completed_at=excluded.completed_at, "
        "state=excluded.state, reason=excluded.reason, jobs=excluded.jobs, "
        "runner_seconds=excluded.runner_seconds, "
        "steps_seconds=excluded.steps_seconds, reported_by=excluded.reported_by, "
        "received_at=datetime('now') "
        "WHERE excluded.state = 'ok' OR ci_attempt_timings.state != 'ok'",
        (
            task_id,
            body.run_id,
            body.run_attempt,
            body.head_sha,
            body.event,
            body.conclusion,
            completed.astimezone(UTC).strftime(_TS_FORMAT),
            body.state,
            body.reason,
            json.dumps([j.model_dump() for j in body.jobs], ensure_ascii=False),
            runner,
            json.dumps(steps, sort_keys=True),
            reported_by,
        ),
    )
    return {"stored": True, "state": body.state, "runner_seconds": runner}


# ---- practice_metrics ------------------------------------------------------


def _minutes(seconds: float) -> float:
    return round(seconds / 60.0, 2)


def _task_totals(rows: list[Any]) -> dict[int, dict[str, Any]]:
    tasks: dict[int, dict[str, Any]] = {}
    for row in rows:
        item = tasks.setdefault(
            row["task_id"],
            {
                "task_id": row["task_id"],
                "attempts": 0,
                "unknown_attempts": 0,
                "seconds": 0.0,
            },
        )
        item["attempts"] += 1
        if row["runner_seconds"] is None:
            item["unknown_attempts"] += 1
        else:
            item["seconds"] += row["runner_seconds"]
    return tasks


def _class_rows(rows: list[Any], tasks: dict[int, dict[str, Any]]) -> list[dict]:
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        if row["runner_seconds"] is None:
            continue
        key = (row["work_type"] or "", row["size"] or "", row["risk_class"] or "")
        group = groups.setdefault(
            key, {"tasks": set(), "runner": 0.0, "mutations": 0.0, "ac_collect": 0.0}
        )
        steps = json.loads(row["steps_seconds"] or "{}")
        group["tasks"].add(row["task_id"])
        group["runner"] += row["runner_seconds"]
        group["mutations"] += steps.get("mutations", 0.0)
        group["ac_collect"] += steps.get("ac_collect", 0.0)
    out = []
    for (work_type, size, risk), group in sorted(groups.items()):
        totals = [tasks[t]["seconds"] for t in group["tasks"]]
        out.append(
            {
                "work_type": work_type or "не задан",
                "size": size or "не задан",
                "risk_class": risk or "не задан",
                "tasks": len(totals),
                "median_task_minutes": _minutes(statistics.median(totals)),
                "max_task_minutes": _minutes(max(totals)),
                "mutations_share": round(group["mutations"] / group["runner"], 3)
                if group["runner"]
                else None,
                "ac_collect_share": round(group["ac_collect"] / group["runner"], 3)
                if group["runner"]
                else None,
            }
        )
    return out


async def _missing_among_received(db: aiosqlite.Connection, scope: Any) -> dict:
    where, params = scope.where("a.reported_at", task_column="a.task_id")
    rows = await fetchall(
        db,
        "SELECT COUNT(*) AS received, COALESCE(SUM(CASE WHEN NOT EXISTS ("  # nosec B608
        "SELECT 1 FROM ci_attempt_timings t WHERE t.task_id = a.task_id "
        "AND t.run_id = a.run_id AND t.run_attempt = a.run_attempt) "
        "THEN 1 ELSE 0 END), 0) AS missing FROM ci_report_attempts a "
        f"WHERE a.run_id != '' AND {where}",
        params,
    )
    return {
        "received_ci_reports": rows[0]["received"],
        "without_timing": rows[0]["missing"],
        "note": RECEIVED_NOTE,
    }


async def ci_minutes_metrics(db: aiosqlite.Connection, scope: Any) -> dict[str, Any]:
    """Runner minutes per task and per class over the scope (#1700)."""
    where, params = scope.where("t.completed_at", task_column="t.task_id")
    rows = await fetchall(
        db,
        "SELECT t.task_id, t.runner_seconds, t.steps_seconds, k.work_type, "  # nosec B608
        "k.size, k.risk_class FROM ci_attempt_timings t "
        f"JOIN tasks k ON k.id = t.task_id WHERE {where} ORDER BY t.id",
        params,
    )
    tasks = _task_totals(rows)
    ranked = sorted(tasks.values(), key=lambda i: (-i["seconds"], i["task_id"]))
    unknown = sum(1 for r in rows if r["runner_seconds"] is None)
    return {
        "window_by": "completed_at (время завершения из события)",
        "attempts": {
            "received": len(rows),
            "timed": len(rows) - unknown,
            "unknown_duration": unknown,
        },
        "by_task": [
            {
                "task_id": i["task_id"],
                "attempts": i["attempts"],
                "unknown_attempts": i["unknown_attempts"],
                "runner_minutes": _minutes(i["seconds"]),
            }
            for i in ranked[:BY_TASK_LIMIT]
        ],
        "by_task_more": max(0, len(ranked) - BY_TASK_LIMIT),
        "by_class": _class_rows(rows, tasks),
        "missing_among_received_ci_reports": await _missing_among_received(db, scope),
    }
