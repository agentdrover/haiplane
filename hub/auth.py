"""Multi-user authentication for OpenClaw Hub.

Auth sources (checked in order):

1. ``Authorization: Bearer <token>`` — DB API key lookup, then env-token fallback.
2. Session cookie ``HUB_COOKIE_NAME`` — DB browser session lookup, then
   env-token fallback (legacy).

When :data:`hub.config.HUB_TOKENS` is empty **and** no DB principals exist,
the Hub runs in **open mode** (backward compat).

Roles: resolved from DB principal_roles or from env-token role field.
Agent tokens are restricted from human-only operations.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from typing import Any, Final

from fastapi import HTTPException, Request, status
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from hub import config
from hub.actionable_errors import (
    human_only_gate_detail,
    permission_denied_detail,
    withdraw_agent_only_detail,
)
from hub.config import TokenIdentity
from hub.mcp_internal_auth import bearer_context_reset, bearer_context_set

log = logging.getLogger("hub.auth")


# ---------------------------------------------------------------------------
# Rate limiter (in-memory, per-IP)
# ---------------------------------------------------------------------------


class LoginRateLimiter:
    """Sliding-window rate limiter for login attempts."""

    __slots__ = ("_max_attempts", "_window_seconds", "_buckets")

    def __init__(self, max_attempts: int = 10, window_seconds: int = 300) -> None:
        self._max_attempts = max_attempts
        self._window_seconds = window_seconds
        self._buckets: dict[str, list[float]] = {}

    def is_blocked(self, key: str) -> bool:
        now = time.monotonic()
        attempts = self._buckets.get(key, [])
        attempts = [t for t in attempts if now - t < self._window_seconds]
        self._buckets[key] = attempts
        return len(attempts) >= self._max_attempts

    def record(self, key: str) -> None:
        now = time.monotonic()
        bucket = self._buckets.setdefault(key, [])
        bucket.append(now)
        if len(bucket) > self._max_attempts * 2:
            cutoff = now - self._window_seconds
            self._buckets[key] = [t for t in bucket if t > cutoff]

    def _cleanup(self) -> None:
        """Remove stale entries. Called periodically from the session reaper."""
        now = time.monotonic()
        stale_keys = [
            k
            for k, v in self._buckets.items()
            if all(now - t >= self._window_seconds for t in v)
        ]
        for k in stale_keys:
            del self._buckets[k]


login_limiter = LoginRateLimiter(max_attempts=10, window_seconds=300)


# ---------------------------------------------------------------------------
# CSRF protection (double-submit cookie)
# ---------------------------------------------------------------------------

CSRF_COOKIE_NAME = "openclaw_csrf"
CSRF_FIELD_NAME = "csrf_token"
CSRF_HEADER_NAME = "X-CSRF-Token"


def generate_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def verify_csrf(request_token: str | None, cookie_token: str | None) -> bool:
    if not request_token or not cookie_token:
        return False
    return (
        hashlib.sha256(request_token.encode()).hexdigest()
        == hashlib.sha256(cookie_token.encode()).hexdigest()
    )


_PUBLIC_PATHS: Final[frozenset[str]] = frozenset(
    {
        "/login",
        "/logout",
        "/health",
        "/healthz",
        "/favicon.ico",
        "/robots.txt",
        "/api/admin/bootstrap",
    }
)

_PUBLIC_PREFIXES: Final[tuple[str, ...]] = ("/static/",)

_PROTECTED_PREFIXES: Final[tuple[str, ...]] = ("/",)

ANONYMOUS_USER: Final[str] = "anonymous"
ANONYMOUS_IDENTITY: Final[TokenIdentity] = TokenIdentity("anonymous", "human")


def _looks_public(path: str) -> bool:
    if path in _PUBLIC_PATHS:
        return True
    return any(path.startswith(prefix) for prefix in _PUBLIC_PREFIXES)


def _extract_bearer(request: Request) -> str | None:
    auth_header = request.headers.get("Authorization") or request.headers.get(
        "authorization"
    )
    if auth_header and auth_header.lower().startswith("bearer "):
        token = auth_header[len("Bearer ") :].strip()
        if token:
            return token
    return None


def _extract_cookie(request: Request) -> str | None:
    cookie_token = request.cookies.get(config.HUB_COOKIE_NAME)
    if cookie_token:
        return cookie_token.strip() or None
    return None


def _resolve_env_token(token: str | None) -> TokenIdentity | None:
    if not token:
        return None
    return config.HUB_TOKENS.get(token)


def _is_open_mode() -> bool:
    if config.HUB_AUTH_DISABLED:
        return True
    return not config.HUB_TOKENS


async def _resolve_db_bearer(request: Request, token: str) -> TokenIdentity | None:
    """Try to resolve a bearer token via DB api_keys."""
    db = getattr(getattr(request, "app", None), "state", None)
    if db is None:
        return None
    db_conn = getattr(db, "db", None)
    if db_conn is None:
        return None
    try:
        from hub.services.admin import resolve_api_key

        return await resolve_api_key(db_conn, token)
    except Exception:
        return None


async def _resolve_db_session(request: Request, token: str) -> TokenIdentity | None:
    """Try to resolve a session cookie via DB browser_sessions."""
    db = getattr(getattr(request, "app", None), "state", None)
    if db is None:
        return None
    db_conn = getattr(db, "db", None)
    if db_conn is None:
        return None
    try:
        from hub.services.admin import resolve_browser_session

        return await resolve_browser_session(db_conn, token)
    except Exception:
        return None


async def _resolve_identity(request: Request) -> TokenIdentity | None:
    """Resolve identity from bearer header or session cookie.

    Priority: bearer DB key > bearer env token > cookie DB session > cookie env token.
    """
    bearer = _extract_bearer(request)
    if bearer:
        identity = await _resolve_db_bearer(request, bearer)
        if identity:
            return identity
        identity = _resolve_env_token(bearer)
        if identity:
            return identity

    cookie = _extract_cookie(request)
    if cookie:
        identity = await _resolve_db_session(request, cookie)
        if identity:
            return identity
        identity = _resolve_env_token(cookie)
        if identity:
            return identity

    return None


class AuthMiddleware(BaseHTTPMiddleware):
    """Authenticate every request and stash identity in ``request.state``."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        path = request.url.path
        mcp_ctx: Any = None
        if path.startswith("/mcp"):
            br = _extract_bearer(request)
            if br:
                mcp_ctx = bearer_context_set(br)

        try:
            if _looks_public(path):
                identity = await _resolve_identity(request) or ANONYMOUS_IDENTITY
                request.state.user = identity.username
                request.state.identity = identity
                return await call_next(request)

            if _is_open_mode():
                request.state.user = ANONYMOUS_USER
                request.state.identity = ANONYMOUS_IDENTITY
                return await call_next(request)

            identity = await _resolve_identity(request)
            if not identity:
                return _unauthorized(request)
            request.state.user = identity.username
            request.state.identity = identity
            return await call_next(request)
        finally:
            if mcp_ctx is not None:
                bearer_context_reset(mcp_ctx)


def _unauthorized(request: Request) -> Response:
    accept = (request.headers.get("accept") or "").lower()
    wants_html = "text/html" in accept and "application/json" not in accept
    if wants_html and request.method in {"GET", "HEAD"}:
        from urllib.parse import quote

        next_url = quote(request.url.path)
        if request.url.query:
            next_url += "?" + quote(request.url.query, safe="=&")
        if next_url == "/":
            return Response(
                status_code=status.HTTP_303_SEE_OTHER,
                headers={"Location": "/login"},
            )
        return Response(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": f"/login?next={next_url}"},
        )
    return Response(
        status_code=status.HTTP_401_UNAUTHORIZED,
        content='{"detail":"authentication required"}',
        media_type="application/json",
        headers={"WWW-Authenticate": 'Bearer realm="openclaw-hub"'},
    )


def _forbidden(detail: str = "insufficient permissions") -> Response:
    return Response(
        status_code=status.HTTP_403_FORBIDDEN,
        content=f'{{"detail":"{detail}"}}',
        media_type="application/json",
    )


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------


def current_user(request: Request) -> str:
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "auth middleware not installed",
        )
    return user


def current_identity(request: Request) -> TokenIdentity:
    identity = getattr(request.state, "identity", None)
    if identity is None:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "auth middleware not installed",
        )
    return identity


def require_human_or_admin(request: Request) -> TokenIdentity:
    """Rejects agent tokens with 403."""
    identity = current_identity(request)
    if identity.is_agent:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail=human_only_gate_detail(),
        )
    return identity


def require_agent_caller(request: Request) -> TokenIdentity:
    """Agent-only dependency for hub_withdraw_own_draft / POST .../withdraw."""
    identity = current_identity(request)
    if not identity.is_agent:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail=withdraw_agent_only_detail(),
        )
    return identity


def require_admin(request: Request) -> TokenIdentity:
    """Allows only admin tokens."""
    identity = current_identity(request)
    if not identity.is_admin:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "this operation requires admin role",
        )
    return identity


def require_permission(perm: str):
    """Factory: returns a FastAPI dependency that checks a specific permission."""

    def _check(request: Request) -> TokenIdentity:
        identity = current_identity(request)
        if not identity.has_permission(perm):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                detail=permission_denied_detail(perm),
            )
        return identity

    return _check


def require_user(request: Request) -> str:
    return current_user(request)
