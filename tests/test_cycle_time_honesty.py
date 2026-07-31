"""Cycle time counts only rows whose start is actually known (#518).

``ready_at`` was backfilled in bulk on 2026-07-11 onto tasks that had already
finished, so for those rows it records when someone stamped the column, not
when the work became ready. The duration comes out as exactly zero, and a zero
is not a fast task — it is a task with no measurable start.

Measured on production: counting them as zero dragged the feature median from
69.95h down to 4.01h. This is not a cosmetic correction to a metric; it is the
difference between "we ship features in an afternoon" and "in three days".
"""

from __future__ import annotations

import aiosqlite
from httpx import AsyncClient

from hub import services
from hub.models import TaskCreate


async def _completed(
    db: aiosqlite.Connection, work_type: str, *, ready: str, completed: str
) -> int:
    """A completed task with explicit ready/completed offsets from now."""
    tv = await services.create_task(
        db, TaskCreate(title=f"{work_type} {ready}", work_type=work_type)
    )
    await db.execute(
        "UPDATE tasks SET status='completed', ready_at=datetime('now', ?), "
        "completed_at=datetime('now', ?), updated_at=datetime('now', ?) WHERE id=?",
        (ready, completed, completed, tv.id),
    )
    return tv.id


async def _cycles(client: AsyncClient) -> dict[str, dict]:
    resp = await client.get("/api/metrics/practices")
    assert resp.status_code == 200, resp.text
    return {c["work_type"]: c for c in resp.json()["cycle_times"]}


async def test_a_task_with_no_measurable_start_stays_out_of_the_median(
    db: aiosqlite.Connection, client: AsyncClient
):
    """AC-1. The backfilled rows land on ready_at == completed_at."""
    await _completed(db, "bug", ready="-10 hours", completed="-1 hours")
    await _completed(db, "bug", ready="-12 hours", completed="-2 hours")
    # Backfilled: stamped ready at the moment it was already finished.
    await _completed(db, "bug", ready="-3 hours", completed="-3 hours")
    await db.commit()

    bug = (await _cycles(client))["bug"]

    assert bug["tasks"] == 2, "only rows with a real start belong in the median"
    assert bug["unmeasurable_tasks"] == 1
    assert 9.0 <= bug["median_hours"] <= 10.0, (
        "a zero folded into the median would drag it toward 9h/2"
    )


async def test_the_excluded_rows_are_reported_not_dropped(
    db: aiosqlite.Connection, client: AsyncClient
):
    """AC-2. Silently discarding them would replace one lie with a quieter
    one: the operator would see a plausible median over an unstated sample."""
    await _completed(db, "chore", ready="-5 hours", completed="-5 hours")
    await _completed(db, "chore", ready="-6 hours", completed="-6 hours")
    await _completed(db, "chore", ready="-8 hours", completed="-4 hours")
    await db.commit()

    chore = (await _cycles(client))["chore"]

    assert chore["tasks"] == 1
    assert chore["unmeasurable_tasks"] == 2


async def test_a_work_type_with_nothing_measurable_says_so(
    db: aiosqlite.Connection, client: AsyncClient
):
    """The edge where the old shape would quietly return zero again.

    If every row of a work type is unmeasurable there is no median to give.
    Reporting 0 repeats the original defect; omitting the row entirely reads
    as "no work of this type happened". Neither is true — the honest answer is
    the count plus an absent median."""
    await _completed(db, "docs", ready="-2 hours", completed="-2 hours")
    await _completed(db, "docs", ready="-9 hours", completed="-9 hours")
    await db.commit()

    docs = (await _cycles(client))["docs"]

    assert docs["median_hours"] is None
    assert docs["tasks"] == 0
    assert docs["unmeasurable_tasks"] == 2


async def test_measurable_rows_are_untouched(
    db: aiosqlite.Connection, client: AsyncClient
):
    """The constraint: rows with a real start must be unaffected by the
    exclusion — the fix must narrow the sample, not shift it."""
    for ready, completed in (("-4 hours", "-2 hours"), ("-14 hours", "-2 hours")):
        await _completed(db, "refactor", ready=ready, completed=completed)
    await db.commit()

    refactor = (await _cycles(client))["refactor"]

    assert refactor["tasks"] == 2
    assert refactor["unmeasurable_tasks"] == 0
    # durations are 2h and 12h, so the median of the pair is 7h
    assert 6.5 <= refactor["median_hours"] <= 7.5
