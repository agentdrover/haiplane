"""MCP streamable HTTP transport and Host allowlist behavior."""

from __future__ import annotations

import pytest

from hub import config
from hub.host_security import is_host_allowed
from hub.mcp_http_compat import _merge_streamable_mcp_accept
from hub.mcp_server import mcp


def test_mcp_dns_rebinding_disabled_for_embedded_mount():
    """FastMCP defaults (localhost-only allowlist) caused 421 for real Host headers."""
    assert mcp.settings.transport_security is not None
    assert mcp.settings.transport_security.enable_dns_rebinding_protection is False


def test_merge_accept_adds_json_and_sse():
    assert _merge_streamable_mcp_accept("") == "application/json, text/event-stream"
    assert "text/event-stream" in _merge_streamable_mcp_accept("application/json")
    assert "application/json" in _merge_streamable_mcp_accept("text/event-stream")
    assert _merge_streamable_mcp_accept("application/json, text/event-stream") == (
        "application/json, text/event-stream"
    )


def test_host_allowlist_matches_exact_host_or_hostname_without_port():
    allowed = config.parse_allowed_hosts("hub.tailnet.ts.net,127.0.0.1:8080")

    assert is_host_allowed("hub.tailnet.ts.net", allowed)
    assert is_host_allowed("hub.tailnet.ts.net:8080", allowed)
    assert is_host_allowed("127.0.0.1:8080", allowed)
    assert not is_host_allowed("127.0.0.1:9090", allowed)
    assert not is_host_allowed("attacker.example", allowed)


@pytest.mark.asyncio
async def test_mcp_endpoint_requires_bearer_at_public_path(client, monkeypatch):
    monkeypatch.setattr(
        config,
        "HUB_TOKENS",
        {"secret-token": config.TokenIdentity("alice", "human")},
    )
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)

    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "0.0"},
        },
    }
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }

    unauthenticated = await client.post("/mcp", headers=headers, json=initialize)
    assert unauthenticated.status_code == 401

    # The old nested endpoint was an accidental result of mounting FastMCP at
    # /mcp even though the child app already exposes /mcp.
    old_nested = await client.post(
        "/mcp/mcp",
        headers={**headers, "Authorization": "Bearer secret-token"},
        json=initialize,
    )
    assert old_nested.status_code == 404

    from hub.app import _mcp_streamable_app

    async with _mcp_streamable_app.router.lifespan_context(_mcp_streamable_app):
        authenticated = await client.post(
            "/mcp",
            headers={**headers, "Authorization": "Bearer secret-token"},
            json=initialize,
        )
    assert authenticated.status_code == 200
    assert "serverInfo" in authenticated.text


def test_server_builds():
    """AC-3 (#895): сигнатуры инструментов принимаются SDK при регистрации.

    Эта проверка существует потому, что mypy её не делает: в #848 попытка
    объявить возврат как ``CallToolResult | str`` прошла проверку типов и
    упала уже на регистрации — SDK запрещает CallToolResult в Union, и сервер
    просто не собирался. Зелёный mypy тут ничего не гарантирует.
    """
    import importlib

    import hub.mcp_server as mcp_server

    importlib.reload(mcp_server)
    tools = mcp_server.mcp._tool_manager.list_tools()
    assert tools, "сервер должен зарегистрировать хотя бы один инструмент"
