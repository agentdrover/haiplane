from __future__ import annotations

import aiosqlite
import pytest

from hub import repository as repo


async def test_create_and_get_task(db: aiosqlite.Connection):
    task_id = await repo.create_task(
        db,
        title="Test task",
        description="A description",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status="open",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await db.commit()

    row = await repo.get_task(db, task_id)
    assert row is not None
    d = dict(row)
    assert d["title"] == "Test task"
    assert d["description"] == "A description"
    assert d["status"] == "open"
    assert d["runtime"] == "auto"
    assert d["task_type"] == "task"
    assert d["priority"] == "medium"


async def test_get_task_nonexistent(db: aiosqlite.Connection):
    row = await repo.get_task(db, 99999)
    assert row is None


async def test_list_tasks_filtered(db: aiosqlite.Connection):
    await repo.create_task(
        db,
        title="Open 1",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status="open",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await repo.create_task(
        db,
        title="Running 1",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status="running",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="high",
    )
    await repo.create_task(
        db,
        title="Open 2",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status="open",
        auto_review=True,
        task_type="feature",
        parent_id=None,
        priority="low",
    )
    await db.commit()

    open_tasks = await repo.list_tasks_filtered(db, status="open")
    assert len(open_tasks) == 2
    assert all(dict(r)["status"] == "open" for r in open_tasks)

    running_tasks = await repo.list_tasks_filtered(db, status="running")
    assert len(running_tasks) == 1
    assert dict(running_tasks[0])["title"] == "Running 1"

    features = await repo.list_tasks_filtered(db, task_type="feature")
    assert len(features) == 1
    assert dict(features[0])["title"] == "Open 2"

    high = await repo.list_tasks_filtered(db, priority="high")
    assert len(high) == 1
    assert dict(high[0])["title"] == "Running 1"


async def test_list_tasks_filtered_respects_archived(db: aiosqlite.Connection):
    task_id = await repo.create_task(
        db,
        title="Soon archived",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status="open",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await db.commit()
    await repo.update_task(db, task_id, archived=1)
    await db.commit()

    visible = await repo.list_tasks_filtered(db, status="open")
    assert [dict(r)["id"] for r in visible] == []

    all_rows = await repo.list_tasks_filtered(db, status="open", include_archived=True)
    assert len(all_rows) == 1
    assert dict(all_rows[0])["id"] == task_id


async def test_update_task(db: aiosqlite.Connection):
    task_id = await repo.create_task(
        db,
        title="To update",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status="open",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await db.commit()

    await repo.update_task(db, task_id, status="running", assigned_agent="dev-1")
    await db.commit()

    row = await repo.get_task(db, task_id)
    d = dict(row)
    assert d["status"] == "running"
    assert d["assigned_agent"] == "dev-1"


async def test_add_and_get_updates(db: aiosqlite.Connection):
    task_id = await repo.create_task(
        db,
        title="With updates",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status="open",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await db.commit()

    uid1 = await repo.add_task_update(db, task_id, "agent-1", "status", "Working on it")
    uid2 = await repo.add_task_update(db, task_id, "agent-1", "done", "Finished")
    await db.commit()

    assert uid1 > 0
    assert uid2 > uid1

    updates = await repo.get_task_updates(db, task_id)
    assert len(updates) == 2
    assert dict(updates[0])["kind"] == "status"
    assert dict(updates[1])["kind"] == "done"

    single = await repo.get_task_update_by_id(db, uid1)
    assert single is not None
    assert dict(single)["content"] == "Working on it"


async def test_has_done_updates(db: aiosqlite.Connection):
    task_id = await repo.create_task(
        db,
        title="Done check",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status="running",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await db.commit()

    assert not await repo.has_done_updates(db, task_id)

    await repo.add_task_update(db, task_id, "a", "status", "progress")
    await db.commit()
    assert not await repo.has_done_updates(db, task_id)

    await repo.add_task_update(db, task_id, "a", "done", "complete")
    await db.commit()
    assert await repo.has_done_updates(db, task_id)


async def test_has_plan_updates(db: aiosqlite.Connection):
    task_id = await repo.create_task(
        db,
        title="Plan check",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status="open",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await db.commit()

    assert not await repo.has_plan_updates(db, task_id)

    await repo.add_task_update(db, task_id, "a", "status", "random update")
    await db.commit()
    assert not await repo.has_plan_updates(db, task_id)

    await repo.add_task_update(db, task_id, "a", "status", "Plan: implement X")
    await db.commit()
    assert await repo.has_plan_updates(db, task_id)


async def test_list_tasks_by_statuses(db: aiosqlite.Connection):
    await repo.create_task(
        db,
        title="T1",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status="open",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await repo.create_task(
        db,
        title="T2",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status="running",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await repo.create_task(
        db,
        title="T3",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status="completed",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await db.commit()

    rows = await repo.list_tasks_by_statuses(db, ["open", "running"])
    assert len(rows) == 2
    statuses = {dict(r)["status"] for r in rows}
    assert statuses == {"open", "running"}


async def test_list_tasks_by_status_allows_known_order_by(db: aiosqlite.Connection):
    await repo.create_task(
        db,
        title="Oldest pending report",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status="pending_report",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await repo.create_task(
        db,
        title="Newest pending report",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status="pending_report",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await db.commit()

    rows = await repo.list_tasks_by_status(
        db,
        "pending_report",
        order_by="updated_at ASC",
        limit=20,
    )
    assert len(rows) == 2


async def test_list_tasks_by_status_rejects_unsafe_order_by(
    db: aiosqlite.Connection,
):
    with pytest.raises(ValueError, match="Unsupported order_by clause"):
        await repo.list_tasks_by_status(
            db,
            "open",
            order_by="id DESC; DROP TABLE tasks",
        )


async def test_list_agent_tasks(db: aiosqlite.Connection):
    await repo.create_task(
        db,
        title="Agent task",
        description="",
        runtime="auto",
        source="agent",
        assigned_agent="bot",
        rationale="need it",
        status="draft",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await repo.create_task(
        db,
        title="Human task",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status="open",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await db.commit()

    agent_rows = await repo.list_agent_tasks(db)
    assert len(agent_rows) == 1
    assert dict(agent_rows[0])["title"] == "Agent task"

    draft_rows = await repo.list_agent_tasks(db, "draft")
    assert len(draft_rows) == 1


async def test_activity_log(db: aiosqlite.Connection):
    from hub.db import log_activity

    await log_activity(db, "test_event", "Something happened", "detail info")
    await log_activity(db, "test_event", "Another thing")

    rows = await repo.list_activity(db, limit=10)
    assert len(rows) == 2
    assert dict(rows[0])["summary"] == "Another thing"
    assert dict(rows[1])["summary"] == "Something happened"


async def _make_task(db: aiosqlite.Connection, *, status: str = "running") -> int:
    return await repo.create_task(
        db,
        title="Review gen",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="dev",
        rationale="",
        status=status,
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )


async def test_bump_submission_generation_increments(db: aiosqlite.Connection):
    task_id = await _make_task(db)
    await db.commit()

    assert await repo.bump_submission_generation(db, task_id) == 1
    assert await repo.bump_submission_generation(db, task_id) == 2
    await db.commit()

    row = await repo.get_task(db, task_id)
    assert dict(row)["submission_generation"] == 2


async def test_record_review_verdict_binds_current_generation(
    db: aiosqlite.Connection,
):
    task_id = await _make_task(db)
    await repo.bump_submission_generation(db, task_id)
    await repo.record_review_verdict(db, task_id, "approved")
    await db.commit()

    d = dict(await repo.get_task(db, task_id))
    assert d["review_verdict"] == "approved"
    assert d["review_verdict_generation"] == 1

    # Resubmission bumps the generation; the stored verdict becomes stale.
    await repo.bump_submission_generation(db, task_id)
    await db.commit()
    d = dict(await repo.get_task(db, task_id))
    assert d["submission_generation"] == 2
    assert d["review_verdict_generation"] == 1


async def test_list_stale_tasks_filters_status_and_review_job(
    db: aiosqlite.Connection,
):
    stale_review = await _make_task(db, status="review")
    fresh_review = await _make_task(db, status="review")
    headless_review = await _make_task(db, status="review")
    await repo.update_task(db, headless_review, review_job_id="rev-1")
    for tid in (stale_review, headless_review):
        await db.execute(
            "UPDATE tasks SET updated_at = datetime('now', '-999 minutes') WHERE id=?",
            (tid,),
        )
    await db.commit()

    rows = await repo.list_stale_tasks(db, "review", 120, require_null_review_job=True)
    ids = [dict(r)["id"] for r in rows]
    assert stale_review in ids
    assert fresh_review not in ids
    assert headless_review not in ids
