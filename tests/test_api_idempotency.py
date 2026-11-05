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


@pytest.fixture
def idempotency_enabled(monkeypatch):
    from hub import config

    monkeypatch.setattr(config, "IDEMPOTENCY_ENABLED", True)


OP = "op-2241-0000000001"


@pytest.mark.usefixtures("idempotency_enabled")
class TestOperationCreate:
    async def test_snapshot_replay_and_dispatch(self, client, db, monkeypatch):
        from hub.services import lifecycle
        from unittest.mock import AsyncMock

        dispatch = AsyncMock(return_value={"job_id": "job-2241"})
        monkeypatch.setattr(lifecycle, "dispatch_task", dispatch)
        body = {"title": "snapshot", "run_immediately": True}
        first = await client.post(
            "/api/tasks", json=body, headers={"Idempotency-Key": OP}
        )
        assert first.status_code == 201, first.text
        assert first.headers["Idempotency-Key"] == OP
        assert first.json()["job_id"] in ("", None)
        await db.execute(
            "UPDATE tasks SET title='changed after commit' WHERE id=?",
            (first.json()["id"],),
        )
        await db.commit()
        again = await client.post(
            "/api/tasks", json=body, headers={"Idempotency-Key": OP}
        )
        assert again.status_code == 200, again.text
        assert again.content == first.content
        assert again.headers["Idempotent-Replayed"] == "true"
        assert again.headers["X-Original-Status"] == "201"
        assert again.headers["Cache-Control"] == "no-store"
        assert dispatch.await_count == 1
        rows = await db.execute_fetchall(
            "SELECT * FROM activity_log WHERE kind='task_created'"
        )
        assert len(rows) == 1
        effects = await db.execute_fetchall("SELECT * FROM effect_outbox")
        assert len(effects) == 1 and effects[0]["state"] == "done"

    async def test_conflict_and_independent_intentions(self, client, db):
        h = {"Idempotency-Key": OP}
        first = await client.post("/api/tasks", json={"title": "one"}, headers=h)
        assert first.status_code == 201
        conflict = await client.post("/api/tasks", json={"title": "two"}, headers=h)
        assert conflict.status_code == 409
        assert conflict.json()["detail"]["conflict_kind"] == "payload_mismatch"
        second = await client.post(
            "/api/tasks", json={"title": "one"}, headers={"Idempotency-Key": OP + "2"}
        )
        assert second.status_code == 201
        assert second.json()["id"] != first.json()["id"]
        assert len(await db.execute_fetchall("SELECT * FROM tasks")) == 2

    async def test_alias_rules(self, client, db):
        body = {"title": "alias", "client_request_id": "req-1"}
        h = {"Idempotency-Key": OP}
        first = await client.post("/api/tasks", json=body, headers=h)
        assert first.status_code == 201, first.text
        for headers, payload in ((h, body), ({}, body), (h, {"title": "alias"})):
            replay = await client.post("/api/tasks", json=payload, headers=headers)
            assert replay.status_code == 200 and replay.content == first.content
        unbound = await client.post(
            "/api/tasks", json=dict(body, client_request_id="free"), headers=h
        )
        assert unbound.status_code == 409
        assert unbound.json()["detail"]["conflict_kind"] == "alias_not_bound"
        mismatch = await client.post(
            "/api/tasks", json=body, headers={"Idempotency-Key": OP + "2"}
        )
        assert mismatch.status_code == 409
        assert mismatch.json()["detail"]["conflict_kind"] == "operation_mismatch"
        other = await client.post(
            "/api/tasks",
            json={"title": "other", "client_request_id": "req-2"},
            headers={"Idempotency-Key": OP + "2"},
        )
        assert other.status_code == 201
        taken = await client.post(
            "/api/tasks", json=dict(body, client_request_id="req-2"), headers=h
        )
        assert taken.status_code == 409
        assert taken.json()["detail"]["conflict_kind"] == "alias_taken"
        assert len(await db.execute_fetchall("SELECT * FROM operation_keys")) == 2

    async def test_alias_only_generates_bound_server_id(self, client):
        body = {"title": "alias only", "client_request_id": "short"}
        first = await client.post("/api/tasks", json=body)
        assert first.status_code == 201
        assert first.headers["Idempotency-Key"].startswith("srv_")
        assert len(first.headers["Idempotency-Key"]) == 36
        second = await client.post("/api/tasks", json=body)
        assert second.content == first.content and second.status_code == 200

    @pytest.mark.parametrize("key", ["short", "invalid operation key"])
    async def test_invalid_operation_id(self, client, key):
        response = await client.post(
            "/api/tasks", json={"title": "bad"}, headers={"Idempotency-Key": key}
        )
        assert response.status_code == 400
        assert response.json()["detail"]["reason"] == "invalid_operation_id"

    async def test_mismatched_headers_and_alias_body(self, client):
        for headers, payload in (
            (
                {"Idempotency-Key": OP, "X-Operation-Id": OP + "2"},
                {"title": "mismatch"},
            ),
            (
                {"X-Client-Request-Id": "req-1"},
                {"title": "mismatch", "client_request_id": "req-2"},
            ),
        ):
            response = await client.post("/api/tasks", headers=headers, json=payload)
            assert response.status_code == 400
            assert response.json()["detail"]["reason"] == "idempotency_key_mismatch"

    async def test_subtasks_not_admitted(self, client):
        response = await client.post(
            "/api/tasks/1/subtasks",
            json={"items": [{"title": "child"}]},
            headers={"Idempotency-Key": OP},
        )
        assert response.status_code == 400
        assert response.json()["detail"]["reason"] == "operation_not_idempotent"

    async def test_business_refusal_never_reserves_key(self, client, db, monkeypatch):
        from hub.services import task_create
        from unittest.mock import AsyncMock

        check = AsyncMock(wraps=task_create._refuse_bad_shape)
        monkeypatch.setattr(task_create, "_refuse_bad_shape", check)
        for _ in range(2):
            response = await client.post(
                "/api/tasks",
                json={
                    "title": "bad parent",
                    "task_type": "subtask",
                    "parent_id": 999999,
                },
                headers={"Idempotency-Key": OP},
            )
            assert response.status_code == 400
        assert check.await_count == 2
        assert not await db.execute_fetchall("SELECT * FROM operation_keys")

    async def test_status_expiry_and_owner_result(self, client, db):
        first = await client.post(
            "/api/tasks", json={"title": "status"}, headers={"X-Operation-Id": OP}
        )
        assert first.status_code == 201
        status = await client.get(f"/api/operations/{OP}?include=result")
        assert status.status_code == 200
        assert status.json()["result"] == first.json()
        assert status.json()["status"] == "completed"
        assert status.json()["response_status"] == 201
        assert (await client.get(f"/api/operations/{OP}")).json().get("result") is None
        await db.execute("UPDATE operation_keys SET expires_at='2000-01-01 00:00:00'")
        await db.commit()
        replay = await client.post(
            "/api/tasks", json={"title": "status"}, headers={"Idempotency-Key": OP}
        )
        assert replay.status_code == 410
        status = await client.get(f"/api/operations/{OP}?include=result")
        assert status.json()["status"] == "expired" and "result" not in status.json()
        assert (await client.get("/api/operations/not-known")).status_code == 400
        assert (await client.get("/api/operations/" + "x" * 32)).status_code == 404

    async def test_alias_is_dual_written_for_rollback(self, client, db):
        first = await client.post(
            "/api/tasks",
            json={"title": "dual write", "client_request_id": "dual-1"},
            headers={"Idempotency-Key": OP},
        )
        assert first.status_code == 201
        legacy = await db.execute_fetchall("SELECT * FROM task_idempotency_keys")
        assert len(legacy) == 1 and legacy[0]["client_request_id"] == "dual-1"
        aliases = await db.execute_fetchall("SELECT * FROM operation_aliases")
        assert len(aliases) == 1 and aliases[0]["alias"] == "dual-1"

    async def test_principal_isolation(self, client, db, monkeypatch):
        from hub import config
        from hub.config import TokenIdentity
        from hub import repository_idempotency as repo

        monkeypatch.setattr(
            config,
            "HUB_TOKENS",
            {
                "test-a": TokenIdentity("a", "admin", principal_id=22411),
                "test-b": TokenIdentity("b", "admin", principal_id=22412),
            },
        )
        a = {"Authorization": "Bearer test-a", "Idempotency-Key": OP}
        b = {"Authorization": "Bearer test-b", "Idempotency-Key": OP}
        first = await client.post("/api/tasks", json={"title": "private"}, headers=a)
        assert first.status_code == 201, first.text
        assert (
            await client.post("/api/tasks", json={"title": "private"}, headers=b)
        ).status_code == 404
        assert (
            await client.get(f"/api/operations/{OP}?include=result", headers=b)
        ).status_code == 404
        row = dict(await repo.get_key(db, "p:22411", OP))
        row["principal_ref"] = "p:22412"
        await repo.insert_key(db, **row)
        await db.commit()
        own = await client.post("/api/tasks", json={"title": "private"}, headers=b)
        assert own.status_code == 200 and own.content == first.content

    async def test_legacy_hit_does_not_bind_new_operation(self, client, db):
        from hub.models import TaskCreate
        from hub.services.task_idempotency import hash_task_create_payload
        from hub import repository as repo

        first = await client.post("/api/tasks", json={"title": "legacy"})
        await repo.insert_task_idempotency_key(
            db,
            client_request_id="old-key",
            task_id=first.json()["id"],
            request_hash=hash_task_create_payload(TaskCreate(title="legacy")),
        )
        await db.commit()
        replay = await client.post(
            "/api/tasks",
            json={"title": "legacy", "client_request_id": "old-key"},
            headers={"Idempotency-Key": OP},
        )
        assert replay.status_code == 200, replay.text
        assert replay.headers["X-Idempotency-Legacy"] == "true"
        assert not await db.execute_fetchall("SELECT * FROM operation_keys")

    async def test_oversized_snapshot_rolls_back_everything(
        self, client, db, monkeypatch
    ):
        from hub import config

        monkeypatch.setattr(config, "IDEMPOTENCY_MAX_RESULT_BYTES", 200)
        response = await client.post(
            "/api/tasks",
            json={"title": "large", "client_request_id": "size"},
            headers={"Idempotency-Key": OP},
        )
        assert response.status_code == 500
        assert response.json()["detail"]["reason"] == "idempotency_result_too_large"
        for table in (
            "tasks",
            "operation_keys",
            "operation_aliases",
            "task_idempotency_keys",
            "effect_outbox",
            "activity_log",
        ):
            assert not await db.execute_fetchall(f"SELECT * FROM {table}")

    async def test_replay_precedes_business_preconditions(self, client, monkeypatch):
        from hub.services import task_create
        from unittest.mock import AsyncMock

        first = await client.post(
            "/api/tasks",
            json={"title": "preconditions"},
            headers={"Idempotency-Key": OP},
        )
        assert first.status_code == 201
        monkeypatch.setattr(
            task_create,
            "_refuse_bad_shape",
            AsyncMock(side_effect=AssertionError("business replay")),
        )
        replay = await client.post(
            "/api/tasks",
            json={"title": "preconditions"},
            headers={"Idempotency-Key": OP},
        )
        assert replay.status_code == 200 and replay.content == first.content


async def test_disabled_operation_headers_refused_and_short_alias_compatible(
    client, db
):
    for header in ("Idempotency-Key", "X-Operation-Id"):
        response = await client.post(
            "/api/tasks", json={"title": "flag off"}, headers={header: OP}
        )
        assert response.status_code == 400
        assert response.json()["detail"]["reason"] == "operation_not_idempotent"
    first = await client.post(
        "/api/tasks", json={"title": "legacy short", "client_request_id": "req-1"}
    )
    assert first.status_code == 201
    assert (
        await client.post(
            "/api/tasks", json={"title": "legacy short", "client_request_id": "req-1"}
        )
    ).status_code == 200
    assert not await db.execute_fetchall("SELECT * FROM operation_keys")


@pytest.mark.usefixtures("idempotency_enabled")
class TestOperationFaultsAndRights:
    async def test_insert_race_reloads_winner_after_rollback(
        self, client, db, monkeypatch
    ):
        from hub import repository_idempotency as repo

        first = await client.post(
            "/api/tasks",
            json={"title": "race new key"},
            headers={"Idempotency-Key": OP},
        )
        real_lookup = repo.get_key
        calls = 0

        async def miss_first(*args):
            nonlocal calls
            calls += 1
            return None if calls == 1 else await real_lookup(*args)

        monkeypatch.setattr(repo, "get_key", miss_first)
        replay = await client.post(
            "/api/tasks",
            json={"title": "race new key"},
            headers={"Idempotency-Key": OP},
        )
        assert replay.status_code == 200 and replay.content == first.content
        assert calls >= 2
        assert len(await db.execute_fetchall("SELECT * FROM tasks")) == 1
        assert (
            len(
                await db.execute_fetchall(
                    "SELECT * FROM activity_log WHERE kind='task_created'"
                )
            )
            == 1
        )

    async def test_failure_after_outbox_insertion_rolls_back_all_writes(
        self, client, db, monkeypatch
    ):
        from hub.services import effects

        original = effects.enqueue_effect

        async def fail_after_enqueue(*args, **kwargs):
            await original(*args, **kwargs)
            raise RuntimeError("injected before commit")

        monkeypatch.setattr(effects, "enqueue_effect", fail_after_enqueue)
        with pytest.raises(RuntimeError, match="injected before commit"):
            await client.post(
                "/api/tasks",
                json={
                    "title": "rollback",
                    "run_immediately": True,
                    "client_request_id": "fault",
                },
                headers={"Idempotency-Key": OP},
            )
        for table in (
            "tasks",
            "operation_keys",
            "operation_aliases",
            "effect_outbox",
            "task_idempotency_keys",
            "activity_log",
        ):
            assert not await db.execute_fetchall(f"SELECT * FROM {table}")

    async def test_response_lost_after_commit_is_recoverable(
        self, client, db, monkeypatch
    ):
        from hub import idempotency_api
        from unittest.mock import Mock

        with monkeypatch.context() as patch:
            patch.setattr(
                idempotency_api,
                "Response",
                Mock(side_effect=RuntimeError("lost response")),
            )
            with pytest.raises(RuntimeError, match="lost response"):
                await client.post(
                    "/api/tasks",
                    json={"title": "response lost"},
                    headers={"Idempotency-Key": OP},
                )
        row = (await db.execute_fetchall("SELECT * FROM operation_keys"))[0]
        replay = await client.post(
            "/api/tasks",
            json={"title": "response lost"},
            headers={"Idempotency-Key": OP},
        )
        assert replay.status_code == 200
        assert replay.content == row["response_body"].encode()
        assert len(await db.execute_fetchall("SELECT * FROM tasks")) == 1

    async def test_scope_and_create_permission_rechecked_on_replay(
        self, client, db, monkeypatch
    ):
        from hub import config
        from hub.config import TokenIdentity

        from hub import repository as repo

        await repo.create_project(db, slug="default", name="Default")
        await db.commit()
        identity = TokenIdentity(
            "scoped", "admin", principal_id=22411, scopes=("default",)
        )
        monkeypatch.setattr(config, "HUB_TOKENS", {"scoped-test": identity})
        headers = {"Authorization": "Bearer scoped-test", "Idempotency-Key": OP}
        first = await client.post(
            "/api/tasks", json={"title": "scoped"}, headers=headers
        )
        assert first.status_code == 201, first.text
        identity.scopes = ("other",)
        for response in (
            await client.post("/api/tasks", json={"title": "scoped"}, headers=headers),
            await client.get(f"/api/operations/{OP}?include=result", headers=headers),
        ):
            assert response.status_code == 403
            assert response.json()["detail"]["reason"] == "operation_forbidden"
        identity.scopes = ("default",)
        identity.permissions = frozenset({"tasks.read"})
        assert (
            await client.post("/api/tasks", json={"title": "scoped"}, headers=headers)
        ).status_code == 403
        assert (
            await client.get(f"/api/operations/{OP}?include=result", headers=headers)
        ).status_code == 403

    async def test_source_permission_rechecked_before_replay(self, client, monkeypatch):
        from hub import config
        from hub.config import TokenIdentity

        identity = TokenIdentity("human", "human", principal_id=22411)
        monkeypatch.setattr(config, "HUB_TOKENS", {"role-test": identity})
        headers = {"Authorization": "Bearer role-test", "Idempotency-Key": OP}
        assert (
            await client.post(
                "/api/tasks", json={"title": "human only"}, headers=headers
            )
        ).status_code == 201
        identity.role = "agent"
        assert (
            await client.post(
                "/api/tasks", json={"title": "human only"}, headers=headers
            )
        ).status_code == 403
        assert (
            await client.get(f"/api/operations/{OP}?include=result", headers=headers)
        ).status_code == 403

    async def test_unknown_effect_keeps_historical_result_until_resolution(
        self, client, db, monkeypatch
    ):
        from unittest.mock import AsyncMock
        from hub.services import effects, lifecycle

        dispatch = AsyncMock(side_effect=TimeoutError("lost dispatch result"))
        monkeypatch.setattr(lifecycle, "dispatch_task", dispatch)
        body = {"title": "unknown effect", "run_immediately": True}
        first = await client.post(
            "/api/tasks", json=body, headers={"Idempotency-Key": OP}
        )
        assert first.status_code == 201
        await db.execute("UPDATE operation_keys SET expires_at='2000-01-01 00:00:00'")
        await db.commit()
        status = (await client.get(f"/api/operations/{OP}?include=result")).json()
        assert status["status"] == "completed" and status["result"] == first.json()
        assert status["effects"][0]["state"] == "unknown"
        assert "payload_json" not in status["effects"][0]
        replay = await client.post(
            "/api/tasks", json=body, headers={"Idempotency-Key": OP}
        )
        assert replay.status_code == 200 and replay.content == first.content
        await effects.resolve_effect(
            db, status["effects"][0]["effect_id"], "done", "job found manually", "human"
        )
        row = (await db.execute_fetchall("SELECT * FROM operation_keys"))[0]
        assert row["response_body"].encode() == first.content
        assert dispatch.await_count == 1
        assert (
            await client.post("/api/tasks", json=body, headers={"Idempotency-Key": OP})
        ).status_code == 410

    async def test_opt_in_server_id_for_keyless_request(self, client, monkeypatch):
        from hub import config

        monkeypatch.setattr(config, "IDEMPOTENCY_SERVER_ID", True)
        first = await client.post("/api/tasks", json={"title": "server id"})
        assert first.status_code == 201
        key = first.headers["Idempotency-Key"]
        assert key.startswith("srv_")
        replay = await client.post(
            "/api/tasks", json={"title": "server id"}, headers={"Idempotency-Key": key}
        )
        assert replay.status_code == 200 and replay.content == first.content

    async def test_nonadmitted_route_never_ignores_operation_key(self, client, db):
        response = await client.post(
            "/api/prod-defects", json={}, headers={"Idempotency-Key": OP}
        )
        assert response.status_code == 400
        assert response.json()["detail"]["reason"] == "operation_not_idempotent"
        assert not await db.execute_fetchall("SELECT * FROM operation_keys")
