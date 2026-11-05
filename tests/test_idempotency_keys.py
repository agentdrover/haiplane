"""Hash/identity vectors, operation validation and bounded retention."""

import pytest
from fastapi import HTTPException

from hub import config, idempotency as idem, repository as tasks
from hub import repository_idempotency as repo
from hub.config import TokenIdentity
from hub.models import TaskCreate
from hub.services.idempotency_maintenance import run_sweep
from tests.idempotency_support import OP, PRINCIPAL, seed_effect, seed_key


def test_contract_hash_vectors():
    body = TaskCreate(title="idem vector", description="d", client_request_id="vec-1")
    assert (
        idem.hash_args_v1(body)
        == "e0103f447ae15259db50ebbcb52863341e58e56ae4376fb83101426b7c063a3b"  # pragma: allowlist secret
    )
    assert (
        idem.hash_args_v2(
            "POST /api/tasks/{task_id}/refine",
            dict(task_id=42, title="vector", list=["a", "b"], flag=False),
        )
        == "1c1a21236ff977c858e794eb3886247ac6240c16fd0d09d2c2ccec869236ba0f"  # pragma: allowlist secret
    )
    with pytest.raises(ValueError):
        idem.hash_args_v2("op", {"value": float("nan")})


@pytest.mark.parametrize(
    "value",
    ["short", "a" * 129, "a long key with spaces", "_" + ("a" * 16), "a" * 16 + "\x00"],
)
def test_invalid_ids(value):
    with pytest.raises(HTTPException) as exc:
        idem.validate_operation_id(value)
    assert exc.value.status_code == 400
    assert exc.value.detail["reason"] == "invalid_operation_id"


@pytest.mark.parametrize(
    "value", ["a" * 16, "a" * 128, "a._~:-0123456789012", "srv_" + "f" * 32]
)
def test_valid_ids(value):
    assert idem.validate_operation_id(" " + value + " ") == value


def test_principals_and_header_aliases():
    assert idem.principal_ref(TokenIdentity("db", principal_id=42)) == "p:42"
    assert idem.principal_ref(TokenIdentity("env")) == "t:env"
    assert (
        idem.principal_ref(TokenIdentity("anonymous", transport="open"))
        == "t:anonymous"
    )
    assert idem.principal_ref(TokenIdentity("zero", principal_id=0)) == "p:0"
    assert (
        idem.resolve_operation_id(
            {"Idempotency-Key": " " + OP + " ", "X-Operation-Id": OP}
        )
        == OP
    )
    assert idem.resolve_operation_id({"Idempotency-Key": "  "}) is None
    assert idem.validate_alias("req-1") == "req-1"


async def test_unknown_hash_version_fails_closed(client, db, monkeypatch):
    monkeypatch.setattr(config, "IDEMPOTENCY_ENABLED", True)
    first = await client.post(
        "/api/tasks", json={"title": "hash version"}, headers={"Idempotency-Key": OP}
    )
    assert first.status_code == 201
    await db.execute("UPDATE operation_keys SET args_hash_version=99")
    await db.commit()
    replay = await client.post(
        "/api/tasks", json={"title": "hash version"}, headers={"Idempotency-Key": OP}
    )
    assert replay.status_code == 500
    assert replay.json()["detail"]["reason"] == "idempotency_hash_version_unsupported"
    assert len(await db.execute_fetchall("SELECT * FROM tasks")) == 1


async def test_expire_result_then_delete_key_and_alias(db):
    await seed_key(db)
    await repo.insert_alias(db, PRINCIPAL, "alias", OP, "POST /api/tasks")
    await db.execute("UPDATE operation_keys SET expires_at='2000-01-01 00:00:00'")
    await db.commit()
    await run_sweep(db)
    row = await repo.get_key(db, PRINCIPAL, OP)
    assert row["status"] == "expired" and row["response_body"] is None
    assert row["response_headers"] is None and row["response_status"] is None
    assert row["response_bytes"] == 0
    await db.execute("UPDATE operation_keys SET key_expires_at='2000-01-01 00:00:00'")
    await db.commit()
    await run_sweep(db)
    assert await repo.get_key(db, PRINCIPAL, OP) is None
    assert await repo.get_alias(db, PRINCIPAL, "alias") is None


@pytest.mark.parametrize("state", ["pending", "running", "unknown"])
async def test_unfinished_effect_blocks_result_and_key_cleanup(db, state):
    await seed_key(db)
    effect_id = await seed_effect(db)
    await db.execute(
        "UPDATE effect_outbox SET state=? WHERE effect_id=?", (state, effect_id)
    )
    await db.execute(
        "UPDATE operation_keys SET expires_at='2000-01-01 00:00:00',key_expires_at='2000-01-01 00:00:00'"
    )
    await db.commit()
    await run_sweep(db)
    row = await repo.get_key(db, PRINCIPAL, OP)
    assert row["status"] == "completed" and row["response_body"] == '{"saved":true}'


async def test_done_effect_retention(db):
    effect_id = await seed_effect(db)
    await db.execute(
        "UPDATE effect_outbox SET state='done',finished_at='2000-01-01 00:00:00'"
    )
    await db.commit()
    await run_sweep(db)
    assert await repo.get_effect(db, effect_id) is None


async def test_new_schema_is_additive_and_reentrant(db):
    from hub.migrations.steps.m202611051200_2241_operation_keys import (
        MIGRATIONS as BASE,
    )
    from hub.migrations.steps.m202611051400_2241_operation_principal_resource import (
        MIGRATIONS as REVIEW_FIXES,
    )

    assert sum("CREATE TABLE " in sql for _, sql in BASE) == 3
    assert sum("CREATE INDEX " in sql for _, sql in BASE) == 6
    assert all("IF NOT EXISTS" in sql for _, sql in BASE)
    for _, sql in BASE:
        await db.execute(sql)
    await db.commit()
    assert (await db.execute_fetchall("PRAGMA integrity_check"))[0][0] == "ok"
    assert not await db.execute_fetchall("PRAGMA foreign_key_check")
    fks = await db.execute_fetchall("PRAGMA foreign_key_list(operation_aliases)")
    assert len(fks) == 2
    assert all(
        row["table"] == "operation_keys" and row["on_delete"] == "CASCADE"
        for row in fks
    )
    applied = await db.execute_fetchall(
        "SELECT name FROM _migrations WHERE name LIKE '2241_%'"
    )
    assert len(applied) == len(BASE) + len(REVIEW_FIXES)
    key_columns = {
        row["name"]
        for row in await db.execute_fetchall("PRAGMA table_info(operation_keys)")
    }
    assert "resource_id" in key_columns
    legacy_columns = {
        row["name"]
        for row in await db.execute_fetchall("PRAGMA table_info(task_idempotency_keys)")
    }
    assert "principal_ref" in legacy_columns


async def test_review_fix_columns_are_backfilled(db):
    """Pre-fix rows: recover the owner and the resource id from what exists."""
    import json

    from hub import repository_idempotency as repo
    from hub.migrations.steps.m202611051400_2241_operation_principal_resource import (
        MIGRATIONS as REVIEW_FIXES,
    )

    task_id = await tasks.create_task_full(
        db, TaskCreate(title="backfill"), status="open"
    )
    await db.commit()
    snapshot = json.dumps({"id": task_id, "title": "backfill"})
    await repo.insert_key(
        db,
        principal_ref=PRINCIPAL,
        operation_id=OP,
        operation="POST /api/tasks",
        args_hash="hash",
        args_hash_version=1,
        response_status=201,
        response_body=snapshot,
        response_headers="{}",
        response_bytes=len(snapshot),
        expires_at="2999-01-01 00:00:00",
        key_expires_at="2999-01-01 00:00:00",
    )
    await repo.insert_alias(db, PRINCIPAL, "old-dual-write", OP, "POST /api/tasks")
    await tasks.insert_task_idempotency_key(
        db,
        client_request_id="old-dual-write",
        task_id=task_id,
        request_hash="hash",
    )
    await db.commit()
    steps = dict(REVIEW_FIXES)
    for name in (
        "2241_operation_keys_resource_backfill_0",
        "2241_task_idempotency_keys_principal_backfill_0",
    ):
        await db.execute(steps[name])
    await db.commit()
    key = await repo.get_key(db, PRINCIPAL, OP)
    assert key["resource_id"] == task_id
    legacy = await tasks.get_task_idempotency_key(db, "old-dual-write")
    assert legacy["principal_ref"] == PRINCIPAL


async def test_authorization_retains_inactive_project_binding(db):
    from hub import repository as tasks
    from hub.services.operation_access import task_scope, require_scope

    project_id = await tasks.create_project(
        db, slug="inactive", name="Inactive", status="paused"
    )
    task_id = await tasks.create_task_full(
        db, TaskCreate(title="bound", task_type="epic"), status="open"
    )
    await tasks.update_task(db, task_id, project_id=project_id)
    await db.commit()
    assert await task_scope(db, task_id) == "project:inactive"
    with pytest.raises(HTTPException) as caught:
        await require_scope(
            db,
            TokenIdentity("scoped", scopes=("default",)),
            await task_scope(db, task_id),
        )
    assert caught.value.status_code == 403


async def test_review_limit_note_is_in_the_key_transaction(client, db, monkeypatch):
    from unittest.mock import AsyncMock
    from hub.services import review_limit, task_create, lifecycle

    monkeypatch.setattr(config, "IDEMPOTENCY_ENABLED", True)
    check = review_limit.ReviewQueueCheck(review_limit.HELD, 1, (999,), "default")
    monkeypatch.setattr(
        task_create, "_plan_created_run", AsyncMock(return_value=(check, True))
    )
    dispatch = AsyncMock()
    monkeypatch.setattr(lifecycle, "dispatch_task", dispatch)
    monkeypatch.setattr(config, "IDEMPOTENCY_MAX_RESULT_BYTES", 200)
    body = {"title": "held creation", "run_immediately": True}
    response = await client.post(
        "/api/tasks", json=body, headers={"Idempotency-Key": OP}
    )
    assert response.status_code == 500
    for table in ("tasks", "task_updates", "operation_keys", "activity_log"):
        assert not await db.execute_fetchall(f"SELECT * FROM {table}")
    monkeypatch.setattr(config, "IDEMPOTENCY_MAX_RESULT_BYTES", 1048576)
    created = await client.post(
        "/api/tasks", json=body, headers={"Idempotency-Key": OP}
    )
    assert created.status_code == 201 and created.json()["status"] == "open"
    assert len(await db.execute_fetchall("SELECT * FROM task_updates")) == 1
    assert not await db.execute_fetchall("SELECT * FROM effect_outbox")
    dispatch.assert_not_awaited()


async def test_canonical_operation_mismatch_fails_without_execution(
    client, db, monkeypatch
):
    monkeypatch.setattr(config, "IDEMPOTENCY_ENABLED", True)
    first = await client.post(
        "/api/tasks",
        json={"title": "operation mismatch"},
        headers={"Idempotency-Key": OP},
    )
    assert first.status_code == 201
    await db.execute(
        "UPDATE operation_keys SET operation='POST /api/tasks/{task_id}/refine'"
    )
    await db.commit()
    response = await client.post(
        "/api/tasks",
        json={"title": "operation mismatch"},
        headers={"Idempotency-Key": OP},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["conflict_kind"] == "operation_mismatch"
    assert len(await db.execute_fetchall("SELECT * FROM tasks")) == 1


async def test_inaccessible_legacy_alias_is_not_disclosed(client, db, monkeypatch):
    from hub import repository as tasks
    from hub.services.task_idempotency import hash_task_create_payload

    monkeypatch.setattr(config, "IDEMPOTENCY_ENABLED", True)
    body = TaskCreate(title="private legacy")
    task_id = await tasks.create_task_full(db, body, status="open")
    await tasks.insert_task_idempotency_key(
        db,
        client_request_id="private-alias",
        task_id=task_id,
        request_hash=hash_task_create_payload(body),
    )
    await db.commit()
    identity = TokenIdentity(
        "other-scope", "admin", principal_id=22412, scopes=("other",)
    )
    monkeypatch.setattr(config, "HUB_TOKENS", {"scope-test": identity})
    response = await client.post(
        "/api/tasks",
        json={"title": "private legacy", "client_request_id": "private-alias"},
        headers={"Authorization": "Bearer scope-test", "Idempotency-Key": OP},
    )
    assert response.status_code == 404
    assert response.json()["detail"]["reason"] == "operation_not_found"
    assert not await db.execute_fetchall("SELECT * FROM operation_keys")
