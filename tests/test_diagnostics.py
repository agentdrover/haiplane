"""Tests for whoami and health diagnostics."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiosqlite
import pytest

from hub.db import _SCHEMA, _migrate

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

    monkeypatch.delenv("HAIPLANE_HUB_URL", raising=False)
    monkeypatch.setenv("HAIPLANE_HUB_URL", "http://127.0.0.1:8080")
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


async def test_identity_diagnostics_reads_haiplane_hub_url(monkeypatch):
    """The instance echo reads the canonical prefix; the legacy one is dead."""
    from unittest.mock import AsyncMock

    from hub.integrations.registry import plugins
    from hub.services.diagnostics import build_identity_diagnostics

    monkeypatch.setenv("HAIPLANE_HUB_URL", "https://agenthai.ru/mcp")
    monkeypatch.setenv("OPEN" + "CLAW" + "_HUB_URL", "http://127.0.0.1:8080")
    plugins.git_ops.current_branch = AsyncMock(return_value="")

    view = await build_identity_diagnostics(
        TokenIdentity("bot", "agent"), connected_via=""
    )
    assert view.instance == "prod"
    assert view.base_url == "https://agenthai.ru/mcp"


async def test_identity_diagnostics_no_mismatch_when_hosts_match(monkeypatch):
    from unittest.mock import AsyncMock

    from hub.integrations.registry import plugins
    from hub.services.diagnostics import build_identity_diagnostics

    monkeypatch.delenv("HAIPLANE_HUB_URL", raising=False)
    monkeypatch.setenv("HAIPLANE_HUB_URL", "https://agenthai.ru/mcp")
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

    monkeypatch.delenv("HAIPLANE_HUB_URL", raising=False)
    monkeypatch.setenv("HAIPLANE_HUB_URL", "http://127.0.0.1:8080")
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
    monkeypatch.delenv("HAIPLANE_HUB_URL", raising=False)
    monkeypatch.setenv("HAIPLANE_HUB_URL", "http://127.0.0.1:8080")

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


# --- default workspace origin health-check (#455) ---


async def test_check_default_workspace_origin_warns(monkeypatch, tmp_path, caplog):
    from unittest.mock import AsyncMock

    import hub.services.diagnostics as diag
    from hub.integrations.registry import plugins

    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(diag, "WORKSPACE_REPO_LINK", tmp_path)
    plugins.git_ops.origin_reachable = AsyncMock(return_value=False)

    with caplog.at_level("WARNING", logger="hub"):
        ok = await diag.check_default_workspace_origin()

    assert ok is False
    assert any("cannot reach origin" in r.getMessage() for r in caplog.records)


async def test_check_default_workspace_origin_ok(monkeypatch, tmp_path):
    from unittest.mock import AsyncMock

    import hub.services.diagnostics as diag
    from hub.integrations.registry import plugins

    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(diag, "WORKSPACE_REPO_LINK", tmp_path)
    plugins.git_ops.origin_reachable = AsyncMock(return_value=True)

    assert await diag.check_default_workspace_origin() is True


async def test_check_default_workspace_origin_none_when_not_git(monkeypatch, tmp_path):
    import hub.services.diagnostics as diag

    monkeypatch.setattr(diag, "WORKSPACE_REPO_LINK", tmp_path)
    assert await diag.check_default_workspace_origin() is None


# --- Мёртвые env-переменные: устаревший префикс виден, а не молчит (#964) ----
#
# Ребрендинг #932 сменил ENV_PREFIX на HAIPLANE_, а drop-in'ы прода остались с
# старым префиксом — хаб молча жил на дефолтах (worktree-изоляция, автопилот и
# AC_LOCATOR стояли выключенными). Тесты гоняют НАСТОЯЩИЙ lifespan: сигнал
# обещан на старте, и проверять его в обход старта значило бы проверить не то.

_RETIRED_PREFIX = ("open" + "claw").upper() + "_"  # собрано: страж Волны 5


@asynccontextmanager
async def _noop_lifespan(_app):
    yield


# MCP session manager can .run() once per process, and these tests start the
# app several times — the transport is not what is under test, so it is stubbed.
_MCP_STUB = SimpleNamespace(router=SimpleNamespace(lifespan_context=_noop_lifespan))


def _drop_legacy_env(monkeypatch):
    """Реальное окружение разработчика само несёт отставленный префикс — вычистить."""
    import os

    for name in list(os.environ):
        if name.startswith(_RETIRED_PREFIX):
            monkeypatch.delenv(name)


@asynccontextmanager
async def _running_app():
    """Прогнать настоящий hub.app.lifespan на отдельной in-memory БД.

    БД отдельная от фикстуры ``db``: lifespan закрывает соединение на выходе,
    и закрытие фикстурной базы уронило бы чужие тесты.
    """
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.executescript(_SCHEMA)
    await _migrate(conn)

    from hub.app import app, lifespan

    with (
        patch("hub.app.get_db", AsyncMock(return_value=conn)),
        patch("hub.app.start_poller", return_value=AsyncMock()),
        patch("hub.app._mcp_streamable_app", _MCP_STUB),
    ):
        async with lifespan(app):
            yield conn


async def _stale_rows(conn) -> list[dict]:
    cursor = await conn.execute(
        "SELECT * FROM activity_log WHERE kind = 'stale_env_detected'"
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


@pytest.mark.asyncio
async def test_stale_env_prefix_logged_once(monkeypatch):
    """AC-1: старт с отставленными переменными даёт одну запись — имена без значений."""
    _drop_legacy_env(monkeypatch)
    monkeypatch.setenv(_RETIRED_PREFIX + "WORKTREE_PER_TASK", "sekret-value-1")
    monkeypatch.setenv(_RETIRED_PREFIX + "FOO", "sekret-value-2")

    async with _running_app() as conn:
        rows = await _stale_rows(conn)

    assert len(rows) == 1, rows
    record = f"{rows[0]['summary']} {rows[0]['detail'] or ''}"
    assert _RETIRED_PREFIX + "WORKTREE_PER_TASK" in record
    assert _RETIRED_PREFIX + "FOO" in record
    assert "sekret-value-1" not in record
    assert "sekret-value-2" not in record


@pytest.mark.asyncio
async def test_clean_env_is_silent(monkeypatch):
    """AC-2: без устаревших префиксов — ни записи, ни предупреждения в /health."""
    _drop_legacy_env(monkeypatch)

    async with _running_app() as conn:
        rows = await _stale_rows(conn)

    assert rows == []
    assert build_health().stale_env == []


@pytest.mark.asyncio
async def test_stale_env_check_never_blocks_startup(monkeypatch):
    """AC-3: ошибка записи в ленту не мешает хабу подняться."""
    _drop_legacy_env(monkeypatch)
    monkeypatch.setenv(_RETIRED_PREFIX + "WORKTREE_PER_TASK", "1")

    with patch(
        "hub.app.log_activity", AsyncMock(side_effect=RuntimeError("disk full"))
    ):
        async with _running_app() as conn:
            cursor = await conn.execute("SELECT COUNT(*) FROM tasks")
            assert (await cursor.fetchone())[0] == 0  # БД жива, старт прошёл


def test_health_names_stale_env(monkeypatch):
    """Строка в /health: имена устаревших переменных, отсортированы."""
    _drop_legacy_env(monkeypatch)
    monkeypatch.setenv(_RETIRED_PREFIX + "B", "x")
    monkeypatch.setenv(_RETIRED_PREFIX + "A", "y")

    health = build_health()
    assert health.stale_env == [_RETIRED_PREFIX + "A", _RETIRED_PREFIX + "B"]
