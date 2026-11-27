"""Transport adapters for MCP Streamable HTTP."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass

from starlette.datastructures import MutableHeaders
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from hub import config


@dataclass(frozen=True)
class MountedMcpRequest:
    rest_url: str
    external_base_url: str
    host_header: str


mounted_mcp_request: ContextVar[MountedMcpRequest | None] = ContextVar(
    "mounted_mcp_request", default=None
)


class McpRestRoutingMiddleware:
    """Route mounted MCP's REST calls to the listener serving the same Hub."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(
            config.HUB_HOST, config.HUB_HOST
        )
        if ":" in host:
            host = f"[{host}]"
        server = scope.get("server")
        port = (server[1] if server else None) or config.HUB_PORT
        token = mounted_mcp_request.set(
            MountedMcpRequest(
                rest_url=f"http://{host}:{port}",
                external_base_url=str(request.base_url).rstrip("/"),
                host_header=request.headers.get("host", ""),
            )
        )
        try:
            await self.app(scope, receive, send)
        finally:
            mounted_mcp_request.reset(token)


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
