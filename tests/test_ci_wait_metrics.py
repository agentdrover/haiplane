"""#1767: the wait of an attempt in practice_metrics, apart from runner minutes.

Kept out of tests/test_practice_metrics.py, which is at its size ceiling.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import aiosqlite

from hub import repository as repo
from hub.services.orchestration import practice_metrics


def _job(name: str, start: str, end: str | None, **steps: int) -> dict:
    return {
        "name": name,
        "started_at": f"2026-10-15T{start}Z",
        "completed_at": None if end is None else f"2026-10-15T{end}Z",
        "steps": [
            {"key": key, "seconds": float(value)} for key, value in steps.items()
        ],
    }


async def test_the_wait_of_an_attempt_is_first_start_to_last_end_not_the_sum(
    db: aiosqlite.Connection,
):
    """AC-5 (#1767): parallel jobs count once in the wait and fully in the minutes.

    The suite runs as three parts side by side. The runner minutes add them up
    (what the attempt cost); the wait is the interval from the first job to the
    last one (what the attempt delayed). An attempt with a job whose end is
    unknown has no wait: not a shorter one, and never zero.
    """
    from hub.services.ci_timing import CIRunTimingSubmit, store_ci_run_timing

    task = await repo.create_task(
        db,
        title="parts",
        description="",
        runtime="auto",
        source="agent",
        assigned_agent="",
        rationale="",
        status="draft",
        auto_review=False,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    whole = [
        _job("Detect tree already tested", "00:00:00", "00:00:30"),
        _job("Tests part 1/3", "00:00:40", "00:04:40", test=240),
        _job("Tests part 2/3", "00:00:40", "00:05:00", test=260),
        _job("Tests part 3/3", "00:00:40", "00:04:00", test=200),
        _job("Ruff and pytest", "00:05:10", "00:07:10", ac_collect=60),
    ]
    cut = [*whole[:-1], _job("Ruff and pytest", "00:05:10", None)]
    for run_id, jobs, state in (
        ("1", whole, "ok"),
        ("2", cut, "ok"),
        ("3", [], "timing_unavailable"),
    ):
        body = CIRunTimingSubmit(
            run_id=run_id,
            run_attempt="1",
            completed_at=(datetime.now(UTC) - timedelta(hours=1)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            state=state,
            jobs=jobs,
        )
        await store_ci_run_timing(db, task, body, reported_by="ci")
    await db.commit()

    section = (await practice_metrics(db, since_days=30))["ci_minutes"]
    wait = section["attempt_wait"]
    assert wait["measured_attempts"] == 1
    assert wait["unknown_attempts"] == 2
    # 00:00:00 -> 00:07:10 is 430 s; the runner sum of the same attempt is 850 s.
    assert wait["median_minutes"] == 7.17
    assert wait["max_minutes"] == 7.17
    rows = {r["task_id"]: r for r in section["by_task"]}
    assert rows[task]["runner_minutes"] > wait["max_minutes"]

    # Nothing to measure: null, never zero.
    from hub.services.ci_timing import CIJobTiming, wait_seconds

    assert wait_seconds([]) is None
    assert wait_seconds([CIJobTiming(**cut[-1])]) is None
    assert wait_seconds([CIJobTiming(**j) for j in whole]) == 430.0
