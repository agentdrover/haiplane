"""MCP transport security must not reject public Host when hub mounts streamable HTTP."""

from __future__ import annotations

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
