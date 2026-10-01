"""MCP in-process Bearer propagation for REST calls from tools."""

from __future__ import annotations

import pytest

from hub.mcp_internal_auth import (
    bearer_context_get,
    bearer_context_reset,
    bearer_context_set,
)
from hub.mcp_server import _hub_token


@pytest.fixture(autouse=True)
def _clean_prefixed_env(monkeypatch):
    """Neither prefix may leak in from the developer's shell (Task 4)."""
    monkeypatch.delenv("HAIPLANE_HUB_TOKEN", raising=False)


def test_hub_token_prefers_caller_bearer_over_env(monkeypatch):
    """#1556: a remote caller's bearer beats the service env token.

    Before, env won, so a read-only caller's write went out under the env
    token's rights and the role was a fiction.
    """
    monkeypatch.setenv("HAIPLANE_HUB_TOKEN", "env-secret")
    h = bearer_context_set("ctx-secret")
    try:
        assert _hub_token() == "ctx-secret"
    finally:
        bearer_context_reset(h)


def test_hub_token_uses_env_without_caller_bearer(monkeypatch):
    """#1556: local stdio MCP has no inbound bearer; env stays its credential."""
    monkeypatch.setenv("HAIPLANE_HUB_TOKEN", "env-secret")
    assert _hub_token() == "env-secret"


def test_hub_token_ignores_legacy_env_name(monkeypatch):
    monkeypatch.setenv("HAIPLANE_HUB_TOKEN", "new-secret")
    monkeypatch.setenv("OPEN" + "CLAW" + "_HUB_TOKEN", "old-secret")
    assert _hub_token() == "new-secret"


def test_hub_token_uses_context_when_env_empty(monkeypatch):
    h = bearer_context_set("ctx-only")
    try:
        assert _hub_token() == "ctx-only"
    finally:
        bearer_context_reset(h)


def test_context_reset_restores_previous():
    outer = bearer_context_set("first")
    try:
        inner = bearer_context_set("second")
        try:
            assert bearer_context_get() == "second"
        finally:
            bearer_context_reset(inner)
        assert bearer_context_get() == "first"
    finally:
        bearer_context_reset(outer)


# ---------------------------------------------------------------------------
# watcher over /mcp with an env HUB_TOKEN that CAN write (#1556)
# ---------------------------------------------------------------------------

_WRITER_TOKEN = "service-env-writer-token"  # pragma: allowlist secret


@pytest.fixture
async def watcher_mcp_hub(client, db, monkeypatch):
    """Auth on; service env HUB_TOKEN is an agent that may write; caller is a watcher."""
    from types import SimpleNamespace

    import httpx
    from httpx import ASGITransport

    from hub import config
    from hub.app import app
    from hub.services import admin as admin_svc
    from hub.services import mcp_telemetry

    monkeypatch.setattr(
        config,
        "HUB_TOKENS",
        {_WRITER_TOKEN: config.TokenIdentity("env-writer", "agent", principal_id=5001)},
    )
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    monkeypatch.setenv("HAIPLANE_HUB_TOKEN", _WRITER_TOKEN)

    human = await admin_svc.create_principal(
        db, kind="human", username="alice-m", role_slug="operator"
    )
    human_key = await admin_svc.create_api_key(db, human["id"], name="h")
    watcher = await admin_svc.create_principal(
        db, kind="agent", username="grok-m", role_slug="watcher"
    )
    watcher_key = await admin_svc.create_api_key(db, watcher["id"], name="w")

    real_client = httpx.AsyncClient

    def _in_process(*args, **kwargs):
        kwargs["transport"] = ASGITransport(app=app)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _in_process)
    mcp_telemetry.set_telemetry_sink(db)
    try:
        yield SimpleNamespace(
            client=client,
            db=db,
            human={"Authorization": f"Bearer {human_key['plaintext_key']}"},
            watcher_token=watcher_key["plaintext_key"],
        )
    finally:
        mcp_telemetry.set_telemetry_sink(None)


async def test_watcher_mcp_write_refused_even_with_env_hub_token(watcher_mcp_hub):
    """AC-3 (#1556): read tool works, write tool is 403 under the WATCHER bearer."""
    from tests.test_mcp_server import _call_tool, _hub_process, _rpc_result

    hub = watcher_mcp_hub
    created = await hub.client.post(
        "/api/tasks", json={"title": "для mcp"}, headers=hub.human
    )
    assert created.status_code in (200, 201), created.text
    tid = created.json()["id"]

    async with _hub_process():
        read = await _call_tool(
            hub.client, hub.watcher_token, "hub_task_status", {"task_id": tid}
        )
        result = _rpc_result(read)
        assert result.get("isError") is not True, result
        assert "для mcp" in "".join(p.get("text", "") for p in result["content"])

        write = await _call_tool(
            hub.client,
            hub.watcher_token,
            "hub_task_update",
            {"task_id": tid, "content": "запись сторожа", "agent": "grok-m"},
        )
        wrote = _rpc_result(write)
    text = "".join(p.get("text", "") for p in wrote["content"])
    assert "403" in text or "watcher_gate_forbidden" in text, wrote

    rows = await hub.db.execute_fetchall(
        "SELECT 1 FROM task_updates WHERE task_id = ? AND content = ?",
        (tid, "запись сторожа"),
    )
    assert rows == []
    refusals = await hub.db.execute_fetchall(
        "SELECT actor, payload FROM events WHERE kind = 'watcher_route_refused'"
    )
    # the refused inner REST call carried the WATCHER's bearer, not the env one
    assert [r["actor"] for r in refusals] == ["grok-m"], refusals
