"""Архив задачи в работе требует причину и называет работу (#1673)."""

from __future__ import annotations

from httpx import AsyncClient

from hub import repository as repo


async def test_archive_of_active_work_needs_a_reason_and_names_the_work(
    client: AsyncClient, db
):
    from tests.archive_support import footprint, mk, principal, say

    task_id = await mk(db, "в работе", "running", claimed_by="a1", branch="task-1/x")
    agent = await principal(db, "agent", "bot")
    person = await principal(db, "human", "denis")
    service = await principal(db, "service", "ci")
    await say(db, task_id, "status", "агент", by=agent, author_kind="principal")
    await say(db, task_id, "status", "человек", by=person, author_kind="principal")
    await say(db, task_id, "status", "сервис", by=service, author_kind="principal")
    await say(db, task_id, "status", "хаб", author_kind="hub")
    await say(db, task_id, "status", "аноним", author_kind="anonymous")
    await say(db, task_id, "status", "до #559", by=agent, author_kind="legacy")
    await say(
        db, task_id, "answer", "ответ владельца", by=person, author_kind="principal"
    )
    before = await footprint(db)

    for body in ({}, {"reason": "  "}):
        resp = await client.post(f"/api/tasks/{task_id}/archive", json=body)
        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert detail["reason"] == "archive_active_work"
        assert detail["project"] == "default"
        first = detail["blocking"][0]
        assert (first["task_id"], first["status"]) == (task_id, "running")
        assert first["claimed_by"] == "a1" and first["branch"] == "task-1/x"
        assert first["updates_by_agents"] == 1 and first["answers"] == 1
        assert await footprint(db) == before, "отказ ничего не пишет"
    assert (await client.get(f"/api/tasks/{task_id}")).json()["archived"] is False


async def test_cascade_archive_names_active_descendants(
    client: AsyncClient, db, monkeypatch
):
    import json

    from tests.archive_support import archived, human, mk, principal, say

    headers = human(monkeypatch, "denis")
    epic = await mk(db, "эпик", kind="epic")
    live = await mk(db, "живой", "running", parent=epic, claimed_by="kid")
    gone = await mk(db, "архивный", "running", parent=epic, archived=1)
    bot = await principal(db, "agent", "bot")
    await say(db, epic, "status", "эпик", by=bot, author_kind="principal")
    for text in ("раз", "два"):
        await say(db, live, "status", text, by=bot, author_kind="principal")
    url = f"/api/tasks/{epic}/archive"

    refused = await client.post(url, json={}, headers=headers)
    assert refused.status_code == 409, refused.text
    blocking = refused.json()["detail"]["blocking"]
    assert [b["task_id"] for b in blocking] == [live], "архивный потомок не в счёте"
    assert blocking[0]["updates_by_agents"] == 2 and blocking[0]["claimed_by"] == "kid"
    assert not await archived(db, epic)

    done = await client.post(url, json={"reason": "эпик закрыт"}, headers=headers)
    assert done.status_code == 200, done.text
    assert all([await archived(db, i) for i in (epic, live, gone)])
    feed = [
        dict(u)
        for u in await repo.get_task_updates(db, live)
        if u["kind"] == "decision"
    ]
    assert len(feed) == 1
    for word in ("эпик закрыт", "denis", "cascade=True", str(live), str(epic)):
        assert word in feed[0]["content"], (word, feed[0]["content"])
    assert not [
        u for u in await repo.get_task_updates(db, epic) if u["kind"] == "decision"
    ]
    rows = await db.execute_fetchall(
        "SELECT detail FROM activity_log WHERE kind='task_archived'"
    )
    detail = json.loads(rows[-1][0])
    assert detail["actor"] == "denis" and detail["reason"] == "эпик закрыт"
    assert detail["cascade"] is True and live in detail["ids"] and epic in detail["ids"]


async def test_archive_of_idle_task_is_unchanged(client: AsyncClient, db, monkeypatch):
    from tests.archive_support import archived, jobs, mk, slot

    jobs(monkeypatch, {"live": "running", "done": "completed", "bad": "failed"})

    async def archive(task_id, body=None):
        return await client.post(f"/api/tasks/{task_id}/archive", json=body or {})

    for status, cols in (
        ("open", {}),
        ("completed", {}),
        ("completed", {"job_id": "done"}),
        ("open", {"job_id": "bad"}),
        ("open", {"review_job_id": "ghost"}),
    ):
        task_id = await mk(db, "тихая", status, **cols)
        assert (await archive(task_id)).status_code == 200, (status, cols)
        assert await archived(db, task_id)
    closed = await mk(db, "слот закрыт", "open")
    await slot(db, closed, "slot", closed=True)
    assert (await archive(closed)).status_code == 200

    epic = await mk(db, "эпик", kind="epic")
    kid = await mk(db, "живой", "running", parent=epic)
    assert (await archive(epic, {"cascade": False})).status_code == 200
    assert await archived(db, epic) and not await archived(db, kid)

    for status in ("open", "completed"):
        for cols in ({"job_id": "live"}, {"review_job_id": "live"}):
            task_id = await mk(db, "с job", status, **cols)
            resp = await archive(task_id)
            assert resp.status_code == 409, (status, cols, resp.text)
            assert resp.json()["detail"]["blocking"][0]["active_job"]
            assert not await archived(db, task_id)
    for status in (
        "claimed",
        "running",
        "review",
        "fix_requested",
        "ci_check",
        "needs_info",
        "pending_report",
        "needs_decision",
    ):
        task_id = await mk(db, f"статус {status}", status)
        resp = await archive(task_id)
        assert resp.status_code == 409, (status, resp.text)
        assert resp.json()["detail"]["blocking"][0]["status"] == status
    for channel in ("slot", "cloud"):
        task_id = await mk(db, f"слот {channel}", "open")
        await slot(db, task_id, channel)
        resp = await archive(task_id)
        assert resp.status_code == 409, (channel, resp.text)
        assert resp.json()["detail"]["blocking"][0]["open_slot"]
        assert not await archived(db, task_id)


async def test_archive_rechecks_work_inside_the_write(
    client: AsyncClient, db, db_dsn, monkeypatch
):
    import asyncio
    import contextlib

    from hub import db as db_module
    from hub.services import lifecycle
    from tests.archive_support import archived, footprint, mk

    epic = await mk(db, "эпик", kind="epic")
    kid = await mk(db, "потомок", "open", parent=epic)
    real = db_module.write_transaction
    committed = asyncio.Event()

    @contextlib.asynccontextmanager
    async def gate(conn):
        # Между внешним чтением и защищённым: чужое соединение ставит running.
        writer = await db_module.connect(db_dsn)
        try:
            await writer.execute("UPDATE tasks SET status='running' WHERE id=?", (kid,))
            await writer.commit()
        finally:
            await writer.close()
        committed.set()
        async with real(conn):
            yield

    monkeypatch.setattr(db_module, "write_transaction", gate)
    url = f"/api/tasks/{epic}/archive"
    before = await footprint(db)

    resp = await client.post(url, json={})
    assert committed.is_set()
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["blocking"][0]["task_id"] == kid
    assert (await repo.get_task(db, kid))["status"] == "running", "чужая запись цела"
    assert not await archived(db, epic) and await footprint(db) == before

    resp = await client.post(url, json={"reason": "знаю"})
    assert resp.status_code == 200, resp.text
    rows = [u for u in await repo.get_task_updates(db, kid) if u["kind"] == "decision"]
    assert len(rows) == 1 and "знаю" in rows[0]["content"], "аудит по свежим признакам"

    monkeypatch.setattr(db_module, "write_transaction", real)
    other = await mk(db, "ещё", "running")

    real_log = lifecycle.log_activity

    async def broken(*a, **kw):
        # Настоящая запись (с тем commit, что передал сервис), затем сбой.
        await real_log(*a, **kw)
        raise RuntimeError("activity недоступна")

    monkeypatch.setattr(lifecycle, "log_activity", broken)
    before = await footprint(db)
    try:
        resp = await client.post(f"/api/tasks/{other}/archive", json={"reason": "p"})
        assert resp.status_code >= 500, resp.text
    except RuntimeError:
        pass
    assert not await archived(db, other) and await footprint(db) == before
