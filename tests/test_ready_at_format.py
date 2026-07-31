"""ready_at is stored in the same shape as every other timestamp (#594).

It was the only timestamp column on ``tasks`` written by Python:
``2026-07-11T08:58:30+00:00`` against ``2026-07-11 08:58:30`` for
created_at/updated_at/completed_at. Nothing was broken by it —
``julianday()`` reads both and returns an identical number — so these tests
cannot prove the fix by asserting that values are unchanged. They have to
assert the thing that WAS wrong: ordering.

'T' is 0x54 and a space is 0x20, so the old shape compared as later than any
same-day value written by SQLite. On production:

    SELECT '2026-07-11T08:58:30+00:00' > '2026-07-11 23:00:00';  -- 1

08:58 counted as later than 23:00. The first ``WHERE ready_at >=
datetime('now', ...)`` would have returned the wrong rows without failing.
"""

from __future__ import annotations

import aiosqlite

from hub import repository as repo
from hub.db import _MIGRATIONS
from hub import services
from hub.models import TaskCreate, TaskRefine


async def _task_that_passes_dor(db: aiosqlite.Connection) -> int:
    """A task complete enough for DoR to pass, which is what stamps ready_at."""
    tv = await services.create_task(db, TaskCreate(title="ready"))
    await db.commit()
    await services.refine_task(
        db,
        tv.id,
        TaskRefine(
            work_type="chore",
            size="S",
            scope_in=["something"],
            validation_commands=["uv run pytest -q"],
        ),
    )
    await db.commit()
    return tv.id


async def _run_the_real_migration(db: aiosqlite.Connection) -> None:
    """Execute the migration as it is written in hub/db.py.

    Deliberately looked up by name instead of pasting its SQL here. A test
    holding its own copy of the statement passes no matter what the migration
    actually says — verified by mutating db.py and watching these tests stay
    green, which is exactly the tautology this whole session kept finding in
    other people's tests (#594).
    """
    sql = dict(_MIGRATIONS)["normalize_ready_at_format"]
    await db.execute(sql)
    await db.commit()


async def test_ready_at_is_written_in_the_shape_of_its_neighbours(
    db: aiosqlite.Connection,
):
    """AC-1. The old value carried a 'T' and an offset; the columns beside it
    never did."""
    task_id = await _task_that_passes_dor(db)

    row = dict(await repo.get_task(db, task_id))

    assert row["ready_at"], "DoR passed, so the column must be stamped"
    assert "T" not in row["ready_at"]
    assert "+" not in row["ready_at"]
    assert len(row["ready_at"]) == len(row["created_at"]), (
        "same shape as the timestamp written by SQLite next to it"
    )


async def test_ready_at_compares_correctly_as_a_string(db: aiosqlite.Connection):
    """AC-3, and the only assertion here that can fail on the old format.

    Values and julianday() were always right, so asserting on those proves
    nothing. Ordering is what was broken — and only WITHIN a day.

    The first version of this test compared against datetime('now', '-1 day')
    and '+1 day'. Both passed on the old format and it could not have failed:
    a one-day offset changes the date, which differs before the comparison
    ever reaches character 11 where 'T' sits. Caught in review of submission
    #1 — a test written to catch tautological assertions that was itself one.

    The comparison has to stay inside the same day for 'T' (0x54) versus a
    space (0x20) to decide the answer.
    """
    task_id = await _task_that_passes_dor(db)

    rows = await db.execute_fetchall(
        "SELECT ready_at <= datetime('now', '+5 seconds') AS not_future, "
        "ready_at >= datetime('now', '-5 seconds') AS just_now "
        "FROM tasks WHERE id=?",
        (task_id,),
    )
    row = dict(rows[0])

    assert row["not_future"] == 1, (
        "stamped a moment ago, so it cannot sort after five seconds from now "
        "— on the old format 'T' pushed it above every same-day value and "
        "this returned 0"
    )
    assert row["just_now"] == 1


async def test_the_migration_converts_old_rows_without_moving_them(
    db: aiosqlite.Connection,
):
    """AC-2. The value must not change — only its representation."""
    task_id = await _task_that_passes_dor(db)
    # Put the row back into the old shape, as it exists on production.
    await db.execute(
        "UPDATE tasks SET ready_at = '2026-07-11T08:58:30+00:00' WHERE id=?",
        (task_id,),
    )
    await db.commit()
    before = dict(
        (
            await db.execute_fetchall(
                "SELECT julianday(ready_at) AS j FROM tasks WHERE id=?", (task_id,)
            )
        )[0]
    )["j"]

    await _run_the_real_migration(db)

    row = dict(await repo.get_task(db, task_id))
    after = dict(
        (
            await db.execute_fetchall(
                "SELECT julianday(ready_at) AS j FROM tasks WHERE id=?", (task_id,)
            )
        )[0]
    )["j"]

    assert row["ready_at"] == "2026-07-11 08:58:30"
    assert before == after, "the instant in time is the same, only its spelling moved"


async def test_the_migration_can_run_twice(db: aiosqlite.Connection):
    """A migration that damages already-converted rows on a rerun is worse than
    no migration."""
    task_id = await _task_that_passes_dor(db)
    await db.execute(
        "UPDATE tasks SET ready_at = '2026-07-11T08:58:30+00:00' WHERE id=?",
        (task_id,),
    )
    await db.commit()

    for _ in range(2):
        await _run_the_real_migration(db)

    assert dict(await repo.get_task(db, task_id))["ready_at"] == "2026-07-11 08:58:30"


async def test_a_non_utc_offset_would_convert_rather_than_shift(
    db: aiosqlite.Connection,
):
    """Every row on production carries +00:00 — measured, not assumed. This
    pins the reason the migration uses datetime() instead of trimming the
    string: string surgery would keep 08:58 for a +03:00 value, which is
    05:58 UTC, and quietly move the row three hours."""
    task_id = await _task_that_passes_dor(db)
    await db.execute(
        "UPDATE tasks SET ready_at = '2026-07-11T08:58:30+03:00' WHERE id=?",
        (task_id,),
    )
    await db.commit()

    await _run_the_real_migration(db)

    assert dict(await repo.get_task(db, task_id))["ready_at"] == "2026-07-11 05:58:30"


async def test_dor_failure_still_clears_the_stamp(db: aiosqlite.Connection):
    """The constraint: only the spelling changes. A task that stops passing
    DoR must still lose its ready_at."""
    task_id = await _task_that_passes_dor(db)
    assert dict(await repo.get_task(db, task_id))["ready_at"]

    await services.refine_task(db, task_id, TaskRefine(scope_in=[]))
    await db.commit()

    assert dict(await repo.get_task(db, task_id))["ready_at"] is None
