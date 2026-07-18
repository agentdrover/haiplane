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
async def test_create_task_idempotent_replay_returns_same_id(client: AsyncClient, db):
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
