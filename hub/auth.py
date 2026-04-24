"""Multi-user authentication for OpenClaw Hub.

This is the **MVP** auth layer (epic: feat/multi-user-auth). It does the
minimum needed to identify a user across UI, REST API and MCP, and to lock
the Hub behind a token gate when configured. It deliberately does **not**:

- store users or roles in the database (env-driven, edit & restart);
- support per-user permissions (everyone authenticated == full access);
- implement OAuth/OIDC.

Those will land in the production-grade follow-up. The contracts here are
designed so the upgrade is mechanical: replace ``parse_tokens`` lookup with
a DB query, add a role check in ``require_user``.

Recognised credentials, in order:

1. ``Authorization: Bearer <token>`` — used by REST API consumers and MCP
   clients (HTTP transport).
2. Session cookie ``HUB_COOKIE_NAME`` carrying the same token — set by the
   browser-facing ``/login`` flow.

When :data:`hub.config.HUB_TOKENS` is empty, the Hub runs in **open mode**:
no token is required and the request user defaults to ``anonymous``. This
preserves the previous single-user behaviour for existing deploys and
keeps the test suite working without setting env vars.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Final

from fastapi import HTTPException, Request, status
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from hub import config

# Public paths that must never require authentication. Anything else under
# the protected prefixes (see ``_PROTECTED_PREFIXES``) is gated.
_PUBLIC_PATHS: Final[frozenset[str]] = frozenset(
    {
        "/login",
        "/logout",
        "/health",
        "/healthz",
        "/favicon.ico",
        "/robots.txt",
    }
)

# Static assets are public by prefix (CSS, JS, images served by /static).
_PUBLIC_PREFIXES: Final[tuple[str, ...]] = ("/static/",)

# Anything matching these prefixes is gated when tokens are configured.
# Everything else (root, partials, custom paths) is also gated by default
# unless explicitly listed in _PUBLIC_PATHS — auth is opt-out, not opt-in.
_PROTECTED_PREFIXES: Final[tuple[str, ...]] = (
    "/",  # dashboard + everything else
)

ANONYMOUS_USER: Final[str] = "anonymous"


def _looks_public(path: str) -> bool:
    if path in _PUBLIC_PATHS:
        return True
    return any(path.startswith(prefix) for prefix in _PUBLIC_PREFIXES)


def _extract_token(request: Request) -> str | None:
    """Pull a token from the Authorization header or the session cookie."""
    auth_header = request.headers.get("Authorization") or request.headers.get(
        "authorization"
    )
    if auth_header and auth_header.lower().startswith("bearer "):
        token = auth_header[len("Bearer ") :].strip()
        if token:
            return token

    cookie_token = request.cookies.get(config.HUB_COOKIE_NAME)
    if cookie_token:
        return cookie_token.strip() or None

    return None


def _resolve_user(token: str | None) -> str | None:
    """Map a token to a username via :data:`hub.config.HUB_TOKENS`."""
    if not token:
        return None
    return config.HUB_TOKENS.get(token)


def _is_open_mode() -> bool:
    """No tokens configured (or auth explicitly disabled) → open mode."""
    if config.HUB_AUTH_DISABLED:
        return True
    return not config.HUB_TOKENS


class AuthMiddleware(BaseHTTPMiddleware):
    """Authenticate every request and stash the user in ``request.state``.

    The middleware is the single source of truth for "who is this request":
    handlers and dependencies must read ``request.state.user`` rather than
    parsing the header again. ``request.state.user`` is always populated
    after this middleware runs — it is :data:`ANONYMOUS_USER` in open mode.

    For browser navigation (``Accept: text/html``) we redirect to ``/login``
    instead of returning JSON 401; that gives a usable UX without requiring
    JS to handle the auth flow.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        path = request.url.path

        if _looks_public(path):
            request.state.user = _resolve_user(_extract_token(request)) or ANONYMOUS_USER
            return await call_next(request)

        if _is_open_mode():
            request.state.user = ANONYMOUS_USER
            return await call_next(request)

        token = _extract_token(request)
        user = _resolve_user(token)
        if not user:
            return _unauthorized(request)
        request.state.user = user
        return await call_next(request)


def _unauthorized(request: Request) -> Response:
    """401 for API clients, 303 redirect to /login for browsers."""
    accept = (request.headers.get("accept") or "").lower()
    wants_html = "text/html" in accept and "application/json" not in accept
    if wants_html and request.method in {"GET", "HEAD"}:
        # Preserve the originally requested path so login can bounce back.
        from urllib.parse import quote

        next_url = quote(request.url.path)
        if request.url.query:
            next_url += "?" + quote(request.url.query, safe="=&")
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


def current_user(request: Request) -> str:
    """FastAPI dependency: returns the authenticated user (or anonymous).

    Always populated because :class:`AuthMiddleware` runs first. Raises
    HTTP 500 if the middleware was not installed — that is a programming
    error, not a runtime auth failure.
    """
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "auth middleware not installed",
        )
    return user


def require_user(request: Request) -> str:
    """Like :func:`current_user` but rejects open-mode anonymous access.

    Use on endpoints that must record a real human author (e.g. decisions).
    In open mode this still returns ``anonymous`` — that is fine for the
    MVP; production-grade auth will switch this to 401.
    """
    return current_user(request)
