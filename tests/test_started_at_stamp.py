"""``tasks.started_at`` is written by the status primitives, once (#1790).

Until then the column was never written: 0 of 335 tasks completed in 30 days
had it, so "how long did the work take after it started" had no source and the
queue was folded into one cycle-time number. The moment a task FIRST enters
``running`` is now stamped in the very statement that writes the status, so
every entry point is covered without being listed, a lost race writes nothing,
and work that was already running before the stamp existed is never given
today's date (#810).
"""

from __future__ import annotations

import aiosqlite

from hub import db as db_module
from hub import repository as repo
from hub import services
from hub.models import TaskCreate


async def _started(db: aiosqlite.Connection, task_id: int) -> str | None:
    rows = await db_module.fetchall(
        db, "SELECT started_at FROM tasks WHERE id=?", (task_id,)
    )
    return rows[0]["started_at"]


async def _row(db: aiosqlite.Connection, task_id: int) -> dict:
    rows = await db_module.fetchall(db, "SELECT * FROM tasks WHERE id=?", (task_id,))
    return dict(rows[0])


async def _open_task(db: aiosqlite.Connection, title: str) -> int:
    return await repo.create_task_full(db, TaskCreate(title=title), status="open")


async def _age(db: aiosqlite.Connection, task_id: int, stamp: str) -> None:
    """Move the stamp into the past so a rewrite is visible."""
    await db.execute("UPDATE tasks SET started_at=? WHERE id=?", (stamp, task_id))


async def test_started_at_is_stamped_once_by_the_status_primitives(
    db: aiosqlite.Connection,
):
    old = "2026-01-02 03:04:05"

    # --- the two INSERTs: straight into running, and into anything else ----
    plain = dict(
        description="",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    created_running = await repo.create_task(db, title="cr", status="running", **plain)
    created_open = await repo.create_task(db, title="co", status="open", **plain)
    full_running = await repo.create_task_full(
        db, TaskCreate(title="fr"), status="running"
    )
    full_open = await repo.create_task_full(db, TaskCreate(title="fo"), status="open")
    assert await _started(db, created_running) is not None
    assert await _started(db, full_running) is not None
    assert await _started(db, created_open) is None
    assert await _started(db, full_open) is None

    # --- public create with run_immediately and its idempotent replay ------
    body = TaskCreate(title="now", run_immediately=True, client_request_id="k-1790")
    first = await services.create_task(db, body)
    assert first.is_new
    stamp = await _started(db, first.task.id)
    assert stamp is not None
    await _age(db, first.task.id, old)
    await db.commit()
    replay = await services.create_task(db, body)
    assert not replay.is_new and replay.task.id == first.task.id
    assert await _started(db, first.task.id) == old, "a replay must not restamp"

    # --- update_task: stamps on the first entry into running only ----------
    t = await _open_task(db, "upd")
    await repo.update_task(db, t, title="renamed")  # PATCH without a status
    assert await _started(db, t) is None
    await repo.update_task(db, t, status="review")
    assert await _started(db, t) is None, "a status other than running"
    await repo.update_task(db, t, status="running")
    assert await _started(db, t) is not None
    await _age(db, t, old)
    await repo.update_task(db, t, status="running")  # same status again
    await repo.update_task(db, t, status="review")
    await repo.update_task(db, t, status="running")  # a second entry
    await repo.update_task(db, t, title="renamed again")
    assert await _started(db, t) == old

    # --- transition_status_if: success stamps, a lost race and others not --
    t = await _open_task(db, "cond")
    assert not await repo.transition_status_if(
        db, t, expected_from="review", new_status="running"
    ), "the race is lost: the task is open, not in review"
    assert await _started(db, t) is None
    assert await repo.transition_status_if(
        db, t, expected_from="open", new_status="review"
    )
    assert await _started(db, t) is None, "a transition that is not into running"
    assert await repo.transition_status_if(
        db, t, expected_from="review", new_status="running"
    )
    assert await _started(db, t) is not None
    await _age(db, t, old)
    assert await repo.transition_status_if(
        db, t, expected_from="running", new_status="review"
    )
    assert await repo.transition_status_if(
        db, t, expected_from="review", new_status="running"
    )
    assert await _started(db, t) == old, "a repeat entry keeps the first moment"
    assert not await repo.transition_status_if(
        db, t, expected_from="review", new_status="running"
    )
    assert await _started(db, t) == old, "a lost race changes nothing"

    # --- record_state_decision: a refusal sends the task back to running ---
    async def state_task(title: str) -> int:
        tid = await _open_task(db, title)
        await db.execute(
            "UPDATE tasks SET status='review', result_kind='state', "
            "submission_generation=1 WHERE id=?",
            (tid,),
        )
        return tid

    t = await state_task("state back")
    assert await repo.record_state_decision(
        db,
        t,
        verdict="changes_requested",
        generation=1,
        findings_json="[]",
        new_status="running",
    )
    assert await _started(db, t) is not None
    t = await state_task("state done")
    assert await repo.record_state_decision(
        db,
        t,
        verdict="approved",
        generation=1,
        findings_json="[]",
        new_status="completed",
    )
    assert await _started(db, t) is None, "completion is not a start"
    t = await state_task("state lost")
    assert not await repo.record_state_decision(
        db,
        t,
        verdict="changes_requested",
        generation=2,  # another generation: the conditional write loses
        findings_json="[]",
        new_status="running",
    )
    assert await _started(db, t) is None
    t = await state_task("state keeps")
    await db.execute("UPDATE tasks SET started_at=? WHERE id=?", (old, t))
    assert await repo.record_state_decision(
        db,
        t,
        verdict="changes_requested",
        generation=1,
        findings_json="[]",
        new_status="running",
    )
    assert await _started(db, t) == old


async def test_work_started_before_tracking_is_not_dated_today(
    db: aiosqlite.Connection,
):
    from hub.models import TaskReturnToWork

    # The state of the base before the stamp existed: every status, no stamp,
    # and the flag column at its default.
    ids: dict[str, int] = {}
    for status in (
        "open",
        "running",
        "review",
        "needs_info",
        "ci_check",
        "pending_report",
        "needs_decision",
        "draft",
    ):
        ids[status] = await _open_task(db, f"old {status}")
        await db.execute("UPDATE tasks SET status=? WHERE id=?", (status, ids[status]))
    ids["archived"] = await _open_task(db, "old archived")
    await db.execute("UPDATE tasks SET archived=1 WHERE id=?", (ids["archived"],))
    ids["state_done"] = await _open_task(db, "old state completed")
    await db.execute(
        "UPDATE tasks SET status='completed', result_kind='state' WHERE id=?",
        (ids["state_done"],),
    )
    await db.execute("UPDATE tasks SET started_at=NULL, start_history_unknown=0")
    await db.execute(
        "DELETE FROM _migrations WHERE name='backfill_tasks_start_history_unknown'"
    )
    await db.commit()

    async def flags() -> dict[str, int]:
        return {k: (await _row(db, i))["start_history_unknown"] for k, i in ids.items()}

    await db_module._migrate(db)
    after_first = await flags()
    assert after_first == {k: (0 if k == "draft" else 1) for k in ids}

    # A task created after the migration is not marked by a second run.
    fresh = await _open_task(db, "fresh")
    await db_module._migrate(db)
    assert await flags() == after_first
    assert (await _row(db, fresh))["start_history_unknown"] == 0
    # The statement itself is idempotent too, were the record of it ever lost.
    backfill = dict(db_module._MIGRATIONS)["backfill_tasks_start_history_unknown"]
    await db.execute(backfill)
    assert await flags() == after_first

    # Back into work: the old ones stay undated, the draft is stamped.
    await services.unarchive_task(db, ids["archived"])
    await services.return_to_work(
        db,
        ids["state_done"],
        TaskReturnToWork(reason="rework of a completed state task"),
        actor="human",
    )
    for key, task_id in ids.items():
        await repo.update_task(db, task_id, status="running")
    for key, task_id in ids.items():
        row = await _row(db, task_id)
        if key == "draft":
            assert row["started_at"] is not None and not row["start_history_unknown"]
        else:
            assert row["started_at"] is None, f"{key} was dated by today's start"
            assert row["start_history_unknown"] == 1
    assert (await _row(db, ids["archived"]))["archived"] == 0
    assert (await _row(db, ids["state_done"]))["status"] == "running"
