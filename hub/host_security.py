"""Host header allowlist enforcement for remote Hub/MCP deployments."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


def _normalize_host(host: str) -> str:
    return host.strip().lower().rstrip(".")


def _host_without_port(host: str) -> str:
    if host.startswith("["):
        end = host.find("]")
        if end != -1:
            return host[: end + 1]
        return host
    if host.count(":") == 1:
        return host.split(":", 1)[0]
    return host


def is_host_allowed(host: str, allowed_hosts: frozenset[str]) -> bool:
    """Return whether a Host header is allowed.

    Exact ``host:port`` entries match only the same port. Plain host entries
    match any port for that hostname.
    """
    if not allowed_hosts:
        return True
    normalized = _normalize_host(host)
    if normalized in allowed_hosts:
        return True
    return _host_without_port(normalized) in allowed_hosts


class HostAllowlistMiddleware(BaseHTTPMiddleware):
    """Reject requests whose Host header is not explicitly allowed."""

    def __init__(self, app, allowed_hosts: frozenset[str]) -> None:
        super().__init__(app)
        self.allowed_hosts = allowed_hosts

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        host = request.headers.get("host", "")
        if not is_host_allowed(host, self.allowed_hosts):
            return Response(
                status_code=421,
                content='{"detail":"host not allowed"}',
                media_type="application/json",
            )
        return await call_next(request)
