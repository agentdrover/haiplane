"""Idempotency for POST /api/tasks."""

from __future__ import annotations

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_create_task_with_idempotency_key_returns_201(client: AsyncClient):
    payload = {"title": "idem once", "client_request_id": "req-001"}
    resp = await client.post("/api/tasks", json=payload)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["id"] > 0
    assert data["title"] == "idem once"


@pytest.mark.asyncio
async def test_create_task_idempotent_replay_returns_same_id(
    client: AsyncClient, db, monkeypatch
):
    from hub.services import lifecycle

    dispatched: list[int] = []
    real_dispatch = lifecycle.dispatch_task

    async def counting_dispatch(db_, task_id, row):
        dispatched.append(task_id)
        return await real_dispatch(db_, task_id, row)

    monkeypatch.setattr(lifecycle, "dispatch_task", counting_dispatch)
    payload = {"title": "idem replay", "client_request_id": "req-replay"}
    first = await client.post("/api/tasks", json=payload)
    assert first.status_code == 201
    task_id = first.json()["id"]

    second = await client.post("/api/tasks", json=payload)
    assert second.status_code == 200, second.text
    assert second.json()["id"] == task_id

    rows = await db.execute_fetchall(
        "SELECT id FROM tasks WHERE title = ?", ("idem replay",)
    )
    assert len(rows) == 1
    assert rows[0]["id"] == task_id

    # #1692: replay не повторяет эффекты создания — ни событие, ни dispatch.
    run = {"title": "idem run", "client_request_id": "req-run", "run_immediately": True}
    created = await client.post("/api/tasks", json=run)
    assert created.status_code == 201, created.text
    again = await client.post("/api/tasks", json=run)
    assert again.status_code == 200, again.text
    assert again.json()["id"] == created.json()["id"]
    assert dispatched == [created.json()["id"]], "replay не запускает dispatch снова"
    events = await db.execute_fetchall(
        "SELECT summary FROM activity_log WHERE kind = 'task_created'"
    )
    assert len(events) == 2, "по одному task_created на каждую новую задачу"


@pytest.mark.asyncio
async def test_create_task_idempotent_replay_via_header(client: AsyncClient, db):
    payload = {"title": "header idem"}
    first = await client.post(
        "/api/tasks",
        json=payload,
        headers={"X-Client-Request-Id": "hdr-001"},
    )
    assert first.status_code == 201
    task_id = first.json()["id"]

    second = await client.post(
        "/api/tasks",
        json=payload,
        headers={"X-Client-Request-Id": "hdr-001"},
    )
    assert second.status_code == 200
    assert second.json()["id"] == task_id

    count = await db.execute_fetchall(
        "SELECT COUNT(*) AS c FROM tasks WHERE title = ?",
        ("header idem",),
    )
    assert count[0]["c"] == 1


@pytest.mark.asyncio
async def test_create_task_idempotency_conflict_on_payload_change(
    client: AsyncClient,
):
    key = "req-conflict"
    first = await client.post(
        "/api/tasks",
        json={"title": "original", "client_request_id": key},
    )
    assert first.status_code == 201
    existing_id = first.json()["id"]

    conflict = await client.post(
        "/api/tasks",
        json={"title": "changed", "client_request_id": key},
    )
    assert conflict.status_code == 409, conflict.text
    detail = conflict.json()["detail"]
    assert detail["reason"] == "idempotency_conflict"
    assert detail["existing_task_id"] == existing_id
    assert detail["client_request_id"] == key


@pytest.mark.asyncio
async def test_create_task_without_key_keeps_legacy_status(client: AsyncClient):
    resp = await client.post("/api/tasks", json={"title": "no key"})
    assert resp.status_code == 200


async def test_replay_of_a_request_created_before_freeze_rationale_is_not_a_conflict(
    client, db
):
    # #1594: хеш, посчитанный кодом до появления freeze_rationale, остаётся
    # хешем того же запроса; пустое умолчание нового поля его не меняет
    # (то же для plain_summary, #1811).
    import hashlib
    import json

    from hub.models import TaskCreate

    body = {"title": "legacy replay", "client_request_id": "legacy-1594"}
    first = await client.post("/api/tasks", json=body)
    assert first.status_code == 201, first.text
    payload = TaskCreate(**body).model_dump(
        mode="json",
        exclude={"client_request_id", "freeze_rationale", "plain_summary"},
    )
    legacy = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    await db.execute(
        "UPDATE task_idempotency_keys SET request_hash=? WHERE client_request_id=?",
        (legacy, "legacy-1594"),
    )
    await db.commit()

    replay = await client.post("/api/tasks", json=body)
    assert replay.status_code == 200, replay.text
    assert replay.json()["id"] == first.json()["id"]
    changed = await client.post(
        "/api/tasks", json=dict(body, freeze_rationale="новое обоснование")
    )
    assert changed.status_code == 409, "непустое обоснование - другой запрос"


async def test_create_task_refusal_under_the_lock_releases_it(db):
    """#1692: отказ 409 уже после BEGIN IMMEDIATE не оставляет write-лок занятым."""
    import pytest as _pytest
    from fastapi import HTTPException

    from hub.models import TaskCreate
    from hub.services import lifecycle

    first = await lifecycle.create_task(
        db, TaskCreate(title="lock a", client_request_id="req-lock")
    )
    assert first.is_new and not db.in_transaction
    with _pytest.raises(HTTPException) as refused:
        await lifecycle.create_task(
            db, TaskCreate(title="lock b", client_request_id="req-lock")
        )
    assert refused.value.status_code == 409
    assert not db.in_transaction, "write-лок отпущен после отказа"


async def test_create_task_key_race_rolls_back_and_replays_the_winner(db, monkeypatch):
    """#1692: проигранная гонка за ключ — rollback и replay победителя, без второй строки."""
    from hub import repository as repo
    from hub.models import TaskCreate
    from hub.services import lifecycle

    body = TaskCreate(title="race", client_request_id="req-race")
    winner = await lifecycle.create_task(db, body)
    real_lookup = repo.get_task_idempotency_key
    calls = {"n": 0}

    async def blind_first_lookup(conn, key):
        calls["n"] += 1
        return None if calls["n"] == 1 else await real_lookup(conn, key)

    monkeypatch.setattr(repo, "get_task_idempotency_key", blind_first_lookup)
    loser = await lifecycle.create_task(db, body)
    assert calls["n"] >= 2, "гонку надо было сыграть через IntegrityError"
    assert not loser.is_new and loser.task.id == winner.task.id
    assert not db.in_transaction, "rollback до replay: лок отпущен"
    rows = await db.execute_fetchall("SELECT id FROM tasks WHERE title = 'race'")
    assert len(rows) == 1
