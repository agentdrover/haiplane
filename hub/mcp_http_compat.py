"""Compatibility shims for MCP streamable HTTP behind diverse clients."""

from __future__ import annotations

from starlette.datastructures import MutableHeaders
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request


def _merge_streamable_mcp_accept(accept: str) -> str:
    """MCP streamable HTTP expects JSON + SSE in Accept for POST; GET needs SSE.

    Some clients (e.g. Cursor) omit ``text/event-stream`` on GET, which yields
    406 / JSON-RPC ``Not Acceptable`` from the MCP SDK.
    """
    current = accept.strip()
    if not current:
        return "application/json, text/event-stream"
    lower = current.lower()
    extra: list[str] = []
    if "application/json" not in lower:
        extra.append("application/json")
    if "text/event-stream" not in lower:
        extra.append("text/event-stream")
    if not extra:
        return current
    return f"{current}, {', '.join(extra)}"


class McpStreamableAcceptCompatMiddleware(BaseHTTPMiddleware):
    """Normalize ``Accept`` for mounted ``/mcp`` streamable HTTP."""

    async def dispatch(self, request: Request, call_next):
        if request.url.path.startswith("/mcp") and request.method in (
            "GET",
            "POST",
        ):
            headers = MutableHeaders(scope=request.scope)
            before = headers.get("accept", "")
            merged = _merge_streamable_mcp_accept(before)
            if merged != before:
                headers["accept"] = merged
        return await call_next(request)
