"""Independent request connections sharing a file SQLite/WAL database."""

import asyncio

import pytest

from hub import config

OP = "parallel-operation-2241"


@pytest.mark.parametrize("different", [False, True])
async def test_eight_concurrent_creates(client, db, monkeypatch, different):
    monkeypatch.setattr(config, "IDEMPOTENCY_ENABLED", True)
    results = await asyncio.gather(
        *(
            client.post(
                "/api/tasks",
                json={"title": f"title {n}" if different else "same"},
                headers={"Idempotency-Key": OP},
            )
            for n in range(8)
        )
    )
    statuses = [r.status_code for r in results]
    assert statuses.count(201) == 1, [r.text for r in results]
    assert statuses.count(409 if different else 200) == 7
    if not different:
        assert len({r.content for r in results}) == 1
        assert sum(r.headers.get("Idempotent-Replayed") == "true" for r in results) == 7
    else:
        assert all(
            r.json()["detail"]["conflict_kind"] == "payload_mismatch"
            for r in results
            if r.status_code == 409
        )
    for table in ("tasks", "operation_keys"):
        assert len(await db.execute_fetchall(f"SELECT * FROM {table}")) == 1
    assert (
        len(
            await db.execute_fetchall(
                "SELECT * FROM activity_log WHERE kind='task_created'"
            )
        )
        == 1
    )


@pytest.mark.parametrize("auditor", [False, True])
async def test_busy_writer_returns_retryable_503_and_same_key_can_succeed(
    client, db, db_dsn, monkeypatch, auditor
):
    from hub import db as db_module

    monkeypatch.setattr(config, "IDEMPOTENCY_ENABLED", True)
    body = {"title": "busy"}
    headers = {"Idempotency-Key": OP}
    if auditor:
        from hub import repository as tasks
        from hub.config import TokenIdentity

        await tasks.create_project(db, slug="default", name="Default")
        await db.commit()
        identity = TokenIdentity(
            "auditor", "ci_auditor", principal_id=22411, scopes=("default",)
        )
        monkeypatch.setattr(config, "HUB_TOKENS", {"audit-test": identity})
        body.update(source="agent", client_request_id="pip-audit:test")
        headers["Authorization"] = "Bearer audit-test"
    original_connect = db_module.connect
    blocker = await original_connect(db_dsn)

    async def impatient_connect(*args, **kwargs):
        connection = await original_connect(*args, **kwargs)
        await connection.execute("PRAGMA busy_timeout=1")
        return connection

    monkeypatch.setattr(db_module, "connect", impatient_connect)
    try:
        await blocker.execute("BEGIN IMMEDIATE")
        response = await client.post("/api/tasks", json=body, headers=headers)
        assert response.status_code == 503, response.text
        assert response.headers["Retry-After"] == "1"
        assert response.json()["detail"]["reason"] == "idempotency_busy"
        assert response.json()["detail"]["retry_safe"] is True
        assert not await db.execute_fetchall("SELECT * FROM operation_keys")
        await blocker.rollback()
        retry = await client.post("/api/tasks", json=body, headers=headers)
        assert retry.status_code == 201
    finally:
        await blocker.close()
