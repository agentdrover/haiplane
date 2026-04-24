from __future__ import annotations

import aiosqlite

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
