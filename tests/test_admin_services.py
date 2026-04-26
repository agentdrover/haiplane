"""Tests for hub/services/admin.py — admin service layer."""

from __future__ import annotations

import pytest

from hub.services import admin as admin_svc
from hub.services.admin import LastAdminError


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------


def test_hash_and_verify_password():
    pw = "correct-horse-battery-staple"
    h = admin_svc.hash_password(pw)
    assert h != pw
    assert admin_svc.verify_password(h, pw) is True
    assert admin_svc.verify_password(h, "wrong") is False


# ---------------------------------------------------------------------------
# API key generation
# ---------------------------------------------------------------------------


def test_generate_api_key():
    plaintext, prefix, key_hash = admin_svc.generate_api_key()
    assert plaintext.startswith("ochk_")
    assert len(prefix) == 8
    assert admin_svc.hash_api_key(plaintext) == key_hash


# ---------------------------------------------------------------------------
# Session tokens
# ---------------------------------------------------------------------------


def test_generate_session_token():
    token, session_hash = admin_svc.generate_session_token()
    assert len(token) > 20
    assert admin_svc.hash_session_token(token) == session_hash


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bootstrap_creates_first_admin(db):
    p = await admin_svc.bootstrap_admin(
        db, username="admin1", password="s3cur3pw!", display_name="Admin One"
    )
    assert p["username"] == "admin1"
    assert p["kind"] == "human"
    assert "super_admin" in p.get("roles", [])


@pytest.mark.asyncio
async def test_bootstrap_fails_if_admin_exists(db):
    await admin_svc.bootstrap_admin(db, username="admin1", password="s3cur3pw!")
    with pytest.raises(ValueError, match="admin already exists"):
        await admin_svc.bootstrap_admin(db, username="admin2", password="s3cur3pw2")


# ---------------------------------------------------------------------------
# Principals CRUD
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_and_get_principal(db):
    p = await admin_svc.create_principal(
        db, kind="human", username="bob", display_name="Bob Smith", email="bob@ex.com"
    )
    assert p["username"] == "bob"
    assert p["kind"] == "human"

    fetched = await admin_svc.get_principal(db, p["id"])
    assert fetched is not None
    assert fetched["display_name"] == "Bob Smith"


@pytest.mark.asyncio
async def test_create_agent_principal(db):
    p = await admin_svc.create_principal(
        db, kind="agent", username="cursor-dev", role_slug="agent"
    )
    assert p["kind"] == "agent"
    assert "agent" in p.get("roles", [])


@pytest.mark.asyncio
async def test_list_principals_by_kind(db):
    await admin_svc.create_principal(db, kind="human", username="user1")
    await admin_svc.create_principal(db, kind="agent", username="bot1")
    humans = await admin_svc.list_principals(db, kind="human")
    assert all(p["kind"] == "human" for p in humans)


@pytest.mark.asyncio
async def test_update_principal(db):
    p = await admin_svc.create_principal(db, kind="human", username="alice")
    updated = await admin_svc.update_principal(
        db, p["id"], display_name="Alice Updated", email="alice@new.com"
    )
    assert updated["display_name"] == "Alice Updated"
    assert updated["email"] == "alice@new.com"


@pytest.mark.asyncio
async def test_disable_and_enable_principal(db):
    await admin_svc.bootstrap_admin(db, username="admin", password="s3cur3pw!")
    p = await admin_svc.create_principal(
        db, kind="human", username="bob", role_slug="operator"
    )

    disabled = await admin_svc.disable_principal(db, p["id"])
    assert disabled["status"] == "disabled"

    enabled = await admin_svc.enable_principal(db, p["id"])
    assert enabled["status"] == "active"


@pytest.mark.asyncio
async def test_cannot_disable_last_admin(db):
    admin = await admin_svc.bootstrap_admin(db, username="admin", password="s3cur3pw!")
    with pytest.raises(LastAdminError):
        await admin_svc.disable_principal(db, admin["id"])


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_principal_roles(db):
    p = await admin_svc.create_principal(db, kind="human", username="alice")
    slugs = await admin_svc.set_principal_roles(db, p["id"], ["operator", "developer"])
    assert set(slugs) == {"operator", "developer"}


@pytest.mark.asyncio
async def test_cannot_remove_last_admin_role(db):
    admin = await admin_svc.bootstrap_admin(db, username="admin", password="s3cur3pw!")
    with pytest.raises(LastAdminError):
        await admin_svc.set_principal_roles(db, admin["id"], ["viewer"])


# ---------------------------------------------------------------------------
# API keys
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_and_resolve_api_key(db):
    p = await admin_svc.create_principal(
        db, kind="agent", username="bot", role_slug="agent"
    )
    key_data = await admin_svc.create_api_key(db, p["id"], name="test-key")
    assert "plaintext_key" in key_data
    assert key_data["key_prefix"] == key_data["plaintext_key"][:8]

    identity = await admin_svc.resolve_api_key(db, key_data["plaintext_key"])
    assert identity is not None
    assert identity.username == "bot"
    assert identity.principal_id == p["id"]


@pytest.mark.asyncio
async def test_revoked_key_not_resolved(db):
    p = await admin_svc.create_principal(
        db, kind="agent", username="bot", role_slug="agent"
    )
    key_data = await admin_svc.create_api_key(db, p["id"], name="test-key")

    await admin_svc.revoke_api_key(db, key_data["id"])
    identity = await admin_svc.resolve_api_key(db, key_data["plaintext_key"])
    assert identity is None


@pytest.mark.asyncio
async def test_disabled_principal_key_not_resolved(db):
    await admin_svc.bootstrap_admin(db, username="admin", password="s3cur3pw!")
    p = await admin_svc.create_principal(
        db, kind="agent", username="bot", role_slug="agent"
    )
    key_data = await admin_svc.create_api_key(db, p["id"], name="test-key")

    await admin_svc.disable_principal(db, p["id"])
    identity = await admin_svc.resolve_api_key(db, key_data["plaintext_key"])
    assert identity is None


# ---------------------------------------------------------------------------
# Browser sessions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_and_resolve_session(db):
    p = await admin_svc.create_principal(
        db, kind="human", username="alice", password="s3cur3pw!", role_slug="operator"
    )
    session_token = await admin_svc.create_browser_session(db, p["id"])
    assert len(session_token) > 20

    identity = await admin_svc.resolve_browser_session(db, session_token)
    assert identity is not None
    assert identity.username == "alice"


@pytest.mark.asyncio
async def test_invalid_session_returns_none(db):
    identity = await admin_svc.resolve_browser_session(db, "nonexistent-token")
    assert identity is None


# ---------------------------------------------------------------------------
# Password auth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_authenticate_password(db):
    await admin_svc.create_principal(
        db, kind="human", username="alice", password="s3cur3pw!", role_slug="operator"
    )
    pid = await admin_svc.authenticate_password(db, "alice", "s3cur3pw!")
    assert pid is not None

    pid_wrong = await admin_svc.authenticate_password(db, "alice", "wrong")
    assert pid_wrong is None


@pytest.mark.asyncio
async def test_set_password(db):
    p = await admin_svc.create_principal(db, kind="human", username="alice")
    await admin_svc.set_password(db, p["id"], "new-password123")
    pid = await admin_svc.authenticate_password(db, "alice", "new-password123")
    assert pid == p["id"]


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_and_list_audit(db):
    await admin_svc.write_audit(
        db,
        actor_id=None,
        action="test_action",
        target_type="test",
        summary="test audit entry",
    )
    entries = await admin_svc.list_audit(db)
    assert len(entries) >= 1
    assert entries[0]["action"] == "test_action"


# ---------------------------------------------------------------------------
# Permission boundaries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_key_lacks_human_gate_permission(db):
    p = await admin_svc.create_principal(
        db, kind="agent", username="bot", role_slug="agent"
    )
    key_data = await admin_svc.create_api_key(db, p["id"], name="bot-key")
    identity = await admin_svc.resolve_api_key(db, key_data["plaintext_key"])
    assert identity is not None
    assert not identity.has_permission("tasks.human_gate")
    assert not identity.has_permission("admin.read")
    assert identity.has_permission("tasks.read")
    assert identity.has_permission("tasks.agent_report")


@pytest.mark.asyncio
async def test_admin_key_has_admin_permissions(db):
    p = await admin_svc.bootstrap_admin(db, username="admin", password="s3cur3pw!")
    key_data = await admin_svc.create_api_key(db, p["id"], name="admin-key")
    identity = await admin_svc.resolve_api_key(db, key_data["plaintext_key"])
    assert identity is not None
    assert identity.has_permission("admin.read")
    assert identity.has_permission("tasks.human_gate")
    assert identity.is_admin


@pytest.mark.asyncio
async def test_operator_can_human_gate_but_not_admin(db):
    await admin_svc.bootstrap_admin(db, username="admin", password="s3cur3pw!")
    p = await admin_svc.create_principal(
        db, kind="human", username="operator1", role_slug="operator"
    )
    key_data = await admin_svc.create_api_key(db, p["id"], name="op-key")
    identity = await admin_svc.resolve_api_key(db, key_data["plaintext_key"])
    assert identity is not None
    assert identity.has_permission("tasks.human_gate")
    assert not identity.has_permission("admin.users.write")
