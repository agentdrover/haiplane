"""T11 review fixes: principal scope, current binding, alias compatibility."""

from __future__ import annotations

import pytest

OP = "op-2241-0000000001"


@pytest.fixture
def idempotency_enabled(monkeypatch):
    from hub import config

    monkeypatch.setattr(config, "IDEMPOTENCY_ENABLED", True)


@pytest.mark.usefixtures("idempotency_enabled")
class TestPrincipalAndBinding:
    async def test_principal_isolation(self, client, db, monkeypatch):
        """#2241 review, blocker 1: keys belong to the effective principal.

        Another principal cannot read an operation (404), but a POST with the
        same operation_id is a NEW intention in its own namespace and must be
        created — the old global lookup refused it. The legacy alias dual-write
        stays a global namespace during the rollback window, so the SAME alias
        from another principal is a 409 and never a replay of someone else's
        task.
        """
        from hub import config
        from hub.config import TokenIdentity

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
        first = await client.post("/api/tasks", json={"title": "private a"}, headers=a)
        assert first.status_code == 201, first.text
        assert (
            await client.get(f"/api/operations/{OP}?include=result", headers=b)
        ).status_code == 404
        # Same id, other principal: an independent operation, not a replay.
        second = await client.post("/api/tasks", json={"title": "private b"}, headers=b)
        assert second.status_code == 201, second.text
        assert second.json()["id"] != first.json()["id"]
        own_a = await client.get(f"/api/operations/{OP}?include=result", headers=a)
        own_b = await client.get(f"/api/operations/{OP}?include=result", headers=b)
        assert own_a.json()["result"]["id"] == first.json()["id"]
        assert own_b.json()["result"]["id"] == second.json()["id"]
        # Dual-write records the owner; a foreign alias is not replayed.
        shared = "shared-alias-2241"
        alias_op = OP + "2"
        created = await client.post(
            "/api/tasks",
            json={"title": "aliased a", "client_request_id": shared},
            headers={
                "Authorization": "Bearer test-a",
                "Idempotency-Key": alias_op,
            },
        )
        assert created.status_code == 201, created.text
        legacy = await db.execute_fetchall(
            "SELECT * FROM task_idempotency_keys WHERE client_request_id=?",
            (shared,),
        )
        assert len(legacy) == 1 and legacy[0]["principal_ref"] == "p:22411"
        takeover = await client.post(
            "/api/tasks",
            json={"title": "aliased b", "client_request_id": shared},
            headers={
                "Authorization": "Bearer test-b",
                "Idempotency-Key": OP + "3",
            },
        )
        assert takeover.status_code == 409, takeover.text
        assert takeover.json()["detail"]["conflict_kind"] == "alias_taken"
        assert str(created.json()["id"]) not in takeover.text
        titles = await db.execute_fetchall("SELECT title FROM tasks ORDER BY id")
        assert [row["title"] for row in titles] == [
            "private a",
            "private b",
            "aliased a",
        ]

    async def test_replay_rechecks_current_project_binding(
        self, client, db, monkeypatch
    ):
        """#2241 review, major 4: a moved epic is re-authorized on replay."""
        from hub import config, repository as repo
        from hub.config import TokenIdentity

        await repo.create_project(db, slug="openproj", name="Open")
        closed_id = await repo.create_project(db, slug="closedproj", name="Closed")
        await db.commit()
        identity = TokenIdentity(
            "scoped",
            "admin",
            principal_id=22411,
            scopes=("openproj", "closedproj"),
        )
        monkeypatch.setattr(config, "HUB_TOKENS", {"scoped-test": identity})
        headers = {"Authorization": "Bearer scoped-test", "Idempotency-Key": OP}
        body = {"title": "moved epic", "task_type": "epic", "project": "openproj"}
        first = await client.post("/api/tasks", json=body, headers=headers)
        assert first.status_code == 201, first.text
        epic_id = first.json()["id"]
        # The epic moves to a project that is closed afterwards. Its saved
        # scope_ref still says "openproj", so only the CURRENT binding check
        # can stop the replay.
        await repo.update_project(db, closed_id, status="paused")
        await repo.update_task(db, epic_id, project_id=closed_id)
        await db.commit()
        replay = await client.post("/api/tasks", json=body, headers=headers)
        assert replay.status_code == 403, replay.text
        assert replay.json()["detail"]["reason"] == "operation_forbidden"
        status = await client.get(
            f"/api/operations/{OP}?include=result", headers=headers
        )
        assert status.status_code == 403
        assert status.json()["detail"]["reason"] == "operation_forbidden"

    async def test_deleted_resource_is_not_served(self, client, db):
        """#2241 review, major 4: the deleted-object policy fails closed."""
        first = await client.post(
            "/api/tasks", json={"title": "delete me"}, headers={"Idempotency-Key": OP}
        )
        assert first.status_code == 201, first.text
        await db.execute("DELETE FROM tasks WHERE id=?", (first.json()["id"],))
        await db.commit()
        replay = await client.post(
            "/api/tasks", json={"title": "delete me"}, headers={"Idempotency-Key": OP}
        )
        assert replay.status_code == 410, replay.text
        assert replay.json()["detail"]["reason"] == "operation_resource_gone"
        status = await client.get(f"/api/operations/{OP}?include=result")
        assert status.status_code == 410
        assert status.json()["detail"]["reason"] == "operation_resource_gone"

    async def test_invalid_alias_is_rejected_only_while_enabled(self, client):
        response = await client.post(
            "/api/tasks",
            json={"title": "bad alias", "client_request_id": "request/123"},
        )
        assert response.status_code == 400
        assert response.json()["detail"]["reason"] == "invalid_client_request_id"
