"""Tests for admin REST API endpoints."""

from __future__ import annotations

import pytest

from hub import config
from hub.config import TokenIdentity
from hub.services import admin as admin_svc

TEST_ADMIN_PASSWORD = "s3cur3pw!"  # pragma: allowlist secret


def _admin_tokens() -> dict[str, TokenIdentity]:
    return {"admin-token": TokenIdentity("admin-user", "admin")}


def _agent_tokens() -> dict[str, TokenIdentity]:
    return {"agent-token": TokenIdentity("bot", "agent")}


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bootstrap_in_open_mode(client, db, monkeypatch):
    monkeypatch.setattr(config, "HUB_TOKENS", {})
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)

    resp = await client.post(
        "/api/admin/bootstrap",
        json={
            "username": "first-admin",
            "password": TEST_ADMIN_PASSWORD,
            "display_name": "First Admin",
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["username"] == "first-admin"
    assert "super_admin" in data.get("roles", [])


@pytest.mark.asyncio
async def test_bootstrap_rejects_duplicate(client, db, monkeypatch):
    monkeypatch.setattr(config, "HUB_TOKENS", {})
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)

    await client.post(
        "/api/admin/bootstrap",
        json={
            "username": "admin1",
            "password": TEST_ADMIN_PASSWORD,
        },
    )
    resp = await client.post(
        "/api/admin/bootstrap",
        json={
            "username": "admin2",
            "password": TEST_ADMIN_PASSWORD,
        },
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_bootstrap_with_token(client, db, monkeypatch):
    monkeypatch.setattr(config, "HUB_TOKENS", _agent_tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    monkeypatch.setattr(config, "HUB_BOOTSTRAP_TOKEN", "boot-secret")

    resp = await client.post(
        "/api/admin/bootstrap",
        json={
            "username": "admin1",
            "password": TEST_ADMIN_PASSWORD,
        },
        headers={"Authorization": "Bearer boot-secret"},
    )
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# Admin CRUD (using env admin token)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_summary(client, db, monkeypatch):
    monkeypatch.setattr(config, "HUB_TOKENS", _admin_tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)

    resp = await client.get(
        "/api/admin/summary",
        headers={"Authorization": "Bearer admin-token"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "active_users" in data
    assert "admin_bootstrap_required" in data


@pytest.mark.asyncio
async def test_agent_cannot_access_admin(client, db, monkeypatch):
    monkeypatch.setattr(config, "HUB_TOKENS", _agent_tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)

    resp = await client.get(
        "/api/admin/summary",
        headers={"Authorization": "Bearer agent-token"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_create_and_list_principals(client, db, monkeypatch):
    monkeypatch.setattr(config, "HUB_TOKENS", _admin_tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)

    resp = await client.post(
        "/api/admin/principals",
        json={
            "kind": "human",
            "username": "bob",
            "display_name": "Bob",
            "role": "operator",
        },
        headers={"Authorization": "Bearer admin-token"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["username"] == "bob"

    resp = await client.get(
        "/api/admin/principals",
        headers={"Authorization": "Bearer admin-token"},
    )
    assert resp.status_code == 200
    assert any(p["username"] == "bob" for p in resp.json())


@pytest.mark.asyncio
async def test_create_agent_principal(client, db, monkeypatch):
    monkeypatch.setattr(config, "HUB_TOKENS", _admin_tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)

    resp = await client.post(
        "/api/admin/principals",
        json={"kind": "agent", "username": "cursor-dev", "role": "agent"},
        headers={"Authorization": "Bearer admin-token"},
    )
    assert resp.status_code == 200
    assert resp.json()["kind"] == "agent"


@pytest.mark.asyncio
async def test_disable_and_enable_principal(client, db, monkeypatch):
    monkeypatch.setattr(config, "HUB_TOKENS", _admin_tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)

    # Ensure at least one admin exists so we can disable others
    await admin_svc.bootstrap_admin(
        db,
        username="super",
        password=TEST_ADMIN_PASSWORD,
    )

    resp = await client.post(
        "/api/admin/principals",
        json={"kind": "human", "username": "bob", "role": "operator"},
        headers={"Authorization": "Bearer admin-token"},
    )
    pid = resp.json()["id"]

    resp = await client.post(
        f"/api/admin/principals/{pid}/disable",
        headers={"Authorization": "Bearer admin-token"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "disabled"

    resp = await client.post(
        f"/api/admin/principals/{pid}/enable",
        headers={"Authorization": "Bearer admin-token"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "active"


# ---------------------------------------------------------------------------
# API keys
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_and_list_api_key(client, db, monkeypatch):
    monkeypatch.setattr(config, "HUB_TOKENS", _admin_tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)

    resp = await client.post(
        "/api/admin/principals",
        json={"kind": "agent", "username": "bot", "role": "agent"},
        headers={"Authorization": "Bearer admin-token"},
    )
    pid = resp.json()["id"]

    resp = await client.post(
        f"/api/admin/principals/{pid}/api-keys",
        json={"name": "test-key", "expires_days": 30},
        headers={"Authorization": "Bearer admin-token"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "plaintext_key" in data
    assert data["key_prefix"] == data["plaintext_key"][:8]

    resp = await client.get(
        "/api/admin/api-keys",
        headers={"Authorization": "Bearer admin-token"},
    )
    assert resp.status_code == 200
    assert any(k["name"] == "test-key" for k in resp.json())


@pytest.mark.asyncio
async def test_revoke_api_key(client, db, monkeypatch):
    monkeypatch.setattr(config, "HUB_TOKENS", _admin_tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)

    resp = await client.post(
        "/api/admin/principals",
        json={"kind": "agent", "username": "bot", "role": "agent"},
        headers={"Authorization": "Bearer admin-token"},
    )
    pid = resp.json()["id"]

    resp = await client.post(
        f"/api/admin/principals/{pid}/api-keys",
        json={"name": "test-key"},
        headers={"Authorization": "Bearer admin-token"},
    )
    key_id = resp.json()["id"]

    resp = await client.post(
        f"/api/admin/api-keys/{key_id}/revoke",
        headers={"Authorization": "Bearer admin-token"},
    )
    assert resp.status_code == 200
    assert resp.json()["revoked"] is True


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_roles(client, db, monkeypatch):
    monkeypatch.setattr(config, "HUB_TOKENS", _admin_tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)

    resp = await client.get(
        "/api/admin/roles",
        headers={"Authorization": "Bearer admin-token"},
    )
    assert resp.status_code == 200
    slugs = [r["slug"] for r in resp.json()]
    assert "super_admin" in slugs
    assert "agent" in slugs


@pytest.mark.asyncio
async def test_set_principal_roles(client, db, monkeypatch):
    monkeypatch.setattr(config, "HUB_TOKENS", _admin_tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)

    resp = await client.post(
        "/api/admin/principals",
        json={"kind": "human", "username": "alice"},
        headers={"Authorization": "Bearer admin-token"},
    )
    pid = resp.json()["id"]

    resp = await client.put(
        f"/api/admin/principals/{pid}/roles",
        json={"roles": ["operator", "developer"]},
        headers={"Authorization": "Bearer admin-token"},
    )
    assert resp.status_code == 200
    assert set(resp.json()["roles"]) == {"operator", "developer"}


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_log_records_writes(client, db, monkeypatch):
    monkeypatch.setattr(config, "HUB_TOKENS", _admin_tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)

    await client.post(
        "/api/admin/principals",
        json={"kind": "human", "username": "bob", "role": "operator"},
        headers={"Authorization": "Bearer admin-token"},
    )

    resp = await client.get(
        "/api/admin/audit",
        headers={"Authorization": "Bearer admin-token"},
    )
    assert resp.status_code == 200
    actions = [e["action"] for e in resp.json()]
    assert "create_principal" in actions


# ---------------------------------------------------------------------------
# Last admin protection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cannot_disable_last_admin_via_api(client, db, monkeypatch):
    monkeypatch.setattr(config, "HUB_TOKENS", _admin_tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)

    admin = await admin_svc.bootstrap_admin(
        db,
        username="admin",
        password=TEST_ADMIN_PASSWORD,
    )
    resp = await client.post(
        f"/api/admin/principals/{admin['id']}/disable",
        headers={"Authorization": "Bearer admin-token"},
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_api_key_can_be_scoped_to_projects(client, db, monkeypatch):
    """AC-5 (#1644): the scope is stored, visible, and an unknown project is 422."""
    from hub import repository as repo

    monkeypatch.setattr(config, "HUB_TOKENS", _admin_tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    admin = {"Authorization": "Bearer admin-token"}
    await repo.create_project(
        db, slug="audit-in", name="audit-in", workspace_path="/tmp/ws"
    )
    await db.commit()
    resp = await client.post(
        "/api/admin/principals",
        json={"kind": "service", "username": "ci-bot", "role": "ci_runner"},
        headers=admin,
    )
    pid = resp.json()["id"]
    url = f"/api/admin/principals/{pid}/api-keys"

    created = await client.post(
        url, json={"name": "scoped", "projects": ["audit-in"]}, headers=admin
    )
    assert created.status_code == 200, created.text
    assert created.json()["scopes"] == ["project:audit-in"]
    listed = await client.get("/api/admin/api-keys", headers=admin)
    mine = [k for k in listed.json() if k["name"] == "scoped"]
    assert mine and mine[0]["scopes"] == ["project:audit-in"]

    missing = await client.post(
        url, json={"name": "bad", "projects": ["no-such-project"]}, headers=admin
    )
    assert missing.status_code == 422, missing.text
    listed = await client.get("/api/admin/api-keys", headers=admin)
    assert not [k for k in listed.json() if k["name"] == "bad"]


@pytest.mark.asyncio
async def test_blank_or_malformed_projects_are_refused_without_a_key(
    client, db, monkeypatch
):
    """#1644 P2/P3: a blank slug must not issue an unrestricted key; bad types 422."""
    monkeypatch.setattr(config, "HUB_TOKENS", _admin_tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    admin = {"Authorization": "Bearer admin-token"}
    resp = await client.post(
        "/api/admin/principals",
        json={"kind": "service", "username": "ci-bot2", "role": "ci_runner"},
        headers=admin,
    )
    url = f"/api/admin/principals/{resp.json()['id']}/api-keys"
    for bad in (["  "], [""], [1], "audit-in"):
        r = await client.post(
            url, json={"name": "blank", "projects": bad}, headers=admin
        )
        assert r.status_code == 422, (bad, r.status_code, r.text[:200])
    listed = await client.get("/api/admin/api-keys", headers=admin)
    assert not [k for k in listed.json() if k["name"] == "blank"]
