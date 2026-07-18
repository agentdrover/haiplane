"""Tests for whoami and health diagnostics."""

from __future__ import annotations

import pytest

from hub import config
from hub.config import TokenIdentity
from hub.services import admin as admin_svc
from hub.services.diagnostics import build_health, build_whoami
from hub.version import get_app_version

TEST_ADMIN_PASSWORD = "s3cur3pw!"  # pragma: allowlist secret


def _tokens(role: str = "human") -> dict[str, TokenIdentity]:
    return {"secret-token": TokenIdentity("alice", role)}


@pytest.mark.asyncio
async def test_whoami_env_token_role(client, monkeypatch):
    monkeypatch.setattr(config, "HUB_TOKENS", _tokens("agent"))
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)

    resp = await client.get(
        "/api/whoami",
        headers={"Authorization": "Bearer secret-token"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["username"] == "alice"
    assert data["role"] == "agent"
    assert data["auth_source"] == "env"
    assert data["api_key_id"] is None
    assert data["permissions_count"] > 0
    assert "tasks.agent_report" in data["permissions_summary"]


@pytest.mark.asyncio
async def test_whoami_db_api_key_source(client, db, monkeypatch):
    monkeypatch.setattr(config, "HUB_TOKENS", _tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)

    principal = await admin_svc.create_principal(
        db, kind="agent", username="bot", role_slug="agent"
    )
    key_data = await admin_svc.create_api_key(db, principal["id"], name="diag-key")

    resp = await client.get(
        "/api/whoami",
        headers={"Authorization": f"Bearer {key_data['plaintext_key']}"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["username"] == "bot"
    assert data["auth_source"] == "db"
    assert data["api_key_id"] == key_data["id"]
    assert "plaintext" not in resp.text.lower()


@pytest.mark.asyncio
async def test_health_matches_config(client, monkeypatch):
    monkeypatch.setattr(config, "HUB_HOST", "0.0.0.0")
    monkeypatch.setattr(config, "HUB_PORT", 9090)
    monkeypatch.setattr(config, "HUB_TOKENS", _tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    monkeypatch.setattr(config, "VAST_ENABLED", True)

    resp = await client.get("/health")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "ok"
    assert data["bind_host"] == "0.0.0.0"
    assert data["bind_port"] == 9090
    assert data["auth_required"] is True
    assert data["auth_disabled"] is False
    assert data["env_tokens_configured"] is True
    assert data["vast_enabled"] is True
    assert data["app_version"] == get_app_version()
    assert "secret" not in resp.text.lower()


@pytest.mark.asyncio
async def test_health_is_public_without_auth(client, monkeypatch):
    monkeypatch.setattr(config, "HUB_TOKENS", _tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)

    resp = await client.get("/health")
    assert resp.status_code == 200


def test_build_whoami_open_mode_identity():
    view = build_whoami(
        TokenIdentity("anonymous", "human", auth_source="open_mode"),
    )
    assert view.auth_source == "open_mode"
    assert view.role == "human"


def test_build_health_open_mode(monkeypatch):
    monkeypatch.setattr(config, "HUB_TOKENS", {})
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    monkeypatch.setattr(config, "VAST_ENABLED", False)

    view = build_health()
    assert view.auth_required is False
    assert view.env_tokens_configured is False
    assert view.vast_enabled is False


# --- identity diagnostics: honest instance + workspace (#452) ---


async def test_identity_diagnostics_flags_config_mismatch(monkeypatch):
    from unittest.mock import AsyncMock

    from hub.integrations.registry import plugins
    from hub.services.diagnostics import build_identity_diagnostics

    monkeypatch.setenv("OPENCLAW_HUB_URL", "http://127.0.0.1:8080")
    plugins.git_ops.current_branch = AsyncMock(return_value="develop")

    view = await build_identity_diagnostics(
        TokenIdentity("alice", "agent"), connected_via="https://agenthai.ru"
    )
    assert view.instance == "local"
    assert view.base_url == "http://127.0.0.1:8080"
    assert view.connected_via == "https://agenthai.ru"
    assert view.config_mismatch is True
    assert view.workspace_branch == "develop"
    assert view.workspace_path
    assert view.server_id


async def test_identity_diagnostics_no_mismatch_when_hosts_match(monkeypatch):
    from unittest.mock import AsyncMock

    from hub.integrations.registry import plugins
    from hub.services.diagnostics import build_identity_diagnostics

    monkeypatch.setenv("OPENCLAW_HUB_URL", "https://agenthai.ru/mcp")
    plugins.git_ops.current_branch = AsyncMock(return_value="")

    view = await build_identity_diagnostics(
        TokenIdentity("bot", "agent"), connected_via="https://agenthai.ru/x"
    )
    assert view.instance == "prod"
    assert view.config_mismatch is False


async def test_identity_diagnostics_no_connection_never_mismatches(monkeypatch):
    from unittest.mock import AsyncMock

    from hub.integrations.registry import plugins
    from hub.services.diagnostics import build_identity_diagnostics

    monkeypatch.setenv("OPENCLAW_HUB_URL", "http://127.0.0.1:8080")
    plugins.git_ops.current_branch = AsyncMock(return_value="")

    view = await build_identity_diagnostics(
        TokenIdentity("bot", "agent"), connected_via=""
    )
    assert view.config_mismatch is False


@pytest.mark.asyncio
async def test_diagnostics_identity_endpoint(client, monkeypatch):
    # AC-1/AC-2/AC-3 (#452): one call returns identity + honest instance + workspace.
    monkeypatch.setattr(config, "HUB_TOKENS", _tokens("agent"))
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    monkeypatch.setenv("OPENCLAW_HUB_URL", "http://127.0.0.1:8080")

    resp = await client.get(
        "/api/diagnostics/identity",
        headers={"Authorization": "Bearer secret-token"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["username"] == "alice"
    assert data["role"] == "agent"
    assert data["instance"] == "local"
    assert data["base_url"] == "http://127.0.0.1:8080"
    assert data["server_id"]
    assert "workspace_path" in data
    assert data["connected_via"]
    # The test client reaches the app as 'testserver', not 127.0.0.1 → mismatch.
    assert data["config_mismatch"] is True
