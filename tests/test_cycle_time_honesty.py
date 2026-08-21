"""Cycle time counts only rows whose start and completion are both known.

#518 excluded the rows with no measurable START. #810 excludes the rows with
no measurable COMPLETION: until then a task finished before ``completed_at``
existed had its completion filled in from ``updated_at``, and that is a
different quantity, not a rougher version of the same one. On production the
measured rows have a median of 0.83h (bug) and 1.70h (feature) while the
filled-in rows sit at 254h and 550h — so the published median tracked the
share of filled-in rows per work type, and announced that bugs take eight
times longer than features while measured bugs were the fastest rows there.

The original #518 note follows, because the two exclusions are separate and
both must keep holding.

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


async def _completed_without_stamp(
    db: aiosqlite.Connection, work_type: str, *, ready: str, touched: str
) -> int:
    """A completed task from before ``completed_at`` existed.

    Its only later timestamp is ``updated_at`` — the moment someone last
    touched the card, which is what used to stand in for a completion.
    """
    tv = await services.create_task(
        db, TaskCreate(title=f"{work_type} unstamped {ready}", work_type=work_type)
    )
    await db.execute(
        "UPDATE tasks SET status='completed', ready_at=datetime('now', ?), "
        "completed_at=NULL, updated_at=datetime('now', ?) WHERE id=?",
        (ready, touched, tv.id),
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


async def test_median_uses_only_measured_completions(
    db: aiosqlite.Connection, client: AsyncClient
):
    """AC-1 (#810). Two short measured tasks and two ancient unstamped ones.

    The unstamped rows would drag the median from ~2h into the hundreds if
    ``updated_at`` were still allowed to stand in for a completion.
    """
    await _completed(db, "bug", ready="-3 hours", completed="-1 hours")
    await _completed(db, "bug", ready="-4 hours", completed="-1 hours")
    await _completed_without_stamp(db, "bug", ready="-40 days", touched="-2 days")
    await _completed_without_stamp(db, "bug", ready="-50 days", touched="-3 days")
    await db.commit()

    bug = (await _cycles(client))["bug"]

    assert bug["tasks"] == 2, "only rows with a real completion belong in the median"
    assert 1.5 <= bug["median_hours"] <= 3.5, (
        "a completion filled in from updated_at would push the median past 100h"
    )


async def test_rows_without_completion_are_counted_not_estimated(
    db: aiosqlite.Connection, client: AsyncClient
):
    """AC-2 (#810). Dropping them silently would repeat the defect quietly:
    a plausible median over an unstated sample. ``estimated_tasks`` goes with
    the estimate itself — there is nothing left for it to report."""
    await _completed(db, "chore", ready="-5 hours", completed="-1 hours")
    await _completed_without_stamp(db, "chore", ready="-30 days", touched="-1 days")
    await _completed_without_stamp(db, "chore", ready="-31 days", touched="-2 days")
    await db.commit()

    chore = (await _cycles(client))["chore"]

    assert chore["tasks"] == 1
    assert chore["no_completion_tasks"] == 2
    assert "estimated_tasks" not in chore


async def test_window_filters_on_the_same_clock_as_numerator(
    db: aiosqlite.Connection, client: AsyncClient
):
    """AC-3 (#810). A task completed long before the window but touched
    yesterday must not re-enter it carrying a year-long duration. #518 fixed
    exactly this for the numerator; the window has to agree."""
    await _completed(db, "spike", ready="-6 hours", completed="-2 hours")
    stale = await _completed(db, "spike", ready="-400 days", completed="-300 days")
    await db.execute(
        "UPDATE tasks SET updated_at=datetime('now', '-1 hours') WHERE id=?", (stale,)
    )
    await db.commit()

    spike = (await _cycles(client))["spike"]

    assert spike["tasks"] == 1, "the stale row was pulled in by its updated_at"
    assert spike["no_completion_tasks"] == 0
    assert 3.5 <= spike["median_hours"] <= 4.5


async def test_work_type_without_measured_rows_still_reported(
    db: aiosqlite.Connection, client: AsyncClient
):
    """AC-4 (#810). Same rule as #518: omitting the row reads as "no work of
    this type happened", which is not what the data says."""
    await _completed_without_stamp(db, "docs", ready="-20 days", touched="-1 days")
    await _completed_without_stamp(db, "docs", ready="-25 days", touched="-2 days")
    await db.commit()

    docs = (await _cycles(client))["docs"]

    assert docs["median_hours"] is None
    assert docs["tasks"] == 0
    assert docs["no_completion_tasks"] == 2


async def test_metrics_page_shows_uncompleted_counter(
    db: aiosqlite.Connection, client: AsyncClient
):
    """AC-5 (#810). The page carried a column named «Из них оценка» and a
    paragraph explaining why the estimate was acceptable. Both describe a
    behaviour that no longer exists."""
    await _completed(db, "bug", ready="-4 hours", completed="-1 hours")
    await _completed_without_stamp(db, "bug", ready="-30 days", touched="-1 days")
    await db.commit()

    resp = await client.get("/metrics")

    assert resp.status_code == 200, resp.text
    assert "Без отметки завершения" in resp.text
    assert "Из них оценка" not in resp.text
    # The «старт неизвестен» column now reads 0 because those rows moved to the
    # new counter, not because the 11.07 backfill stopped mattering. The page
    # has to say which, or the zero becomes the next plausible-looking number.
    assert "завершение раньше старта" in resp.text


async def test_a_row_missing_both_stamps_is_counted_once(
    db: aiosqlite.Connection, client: AsyncClient
):
    """The two exclusions overlap, and a row must not be counted twice.

    Production has no row that is unmeasurable without also lacking a
    completion, so this pins the ordering rather than a live case: no
    completion wins, because without one there is no duration to test the
    start against.
    """
    await _completed_without_stamp(db, "incident", ready="-1 days", touched="-2 days")
    await db.commit()

    incident = (await _cycles(client))["incident"]

    assert incident["no_completion_tasks"] == 1
    assert incident["unmeasurable_tasks"] == 0
    assert incident["tasks"] == 0
    assert incident["median_hours"] is None
