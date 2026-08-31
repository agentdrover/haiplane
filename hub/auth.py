"""Multi-user authentication for Haiplane Hub.

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
import json
import logging
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from typing import Any, Final

from fastapi import HTTPException, Request, status
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from hub import brand, config
from hub.actionable_errors import (
    chat_pair_gate_forbidden_detail,
    human_only_gate_detail,
    permission_denied_detail,
    steward_gate_forbidden_detail,
    withdraw_agent_only_detail,
)
from hub.config import TokenIdentity
from hub.mcp_internal_auth import (
    bearer_context_reset,
    bearer_context_set,
    identity_context_reset,
    identity_context_set,
)

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

CSRF_COOKIE_NAME = brand.CSRF_COOKIE_NAME
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
        # Chat-pair redeem (#961): the caller has no identity yet — the code IS
        # the credential. Guarded by its own per-IP limiter and by a code that
        # works once and lives minutes.
        "/api/auth/chat-pair/redeem",
    }
)


# ---------------------------------------------------------------------------
# Chat-pair route allowlist (#961)
# ---------------------------------------------------------------------------
#
# Identity in the hub is binary — human or agent — and chat-pair is a third
# state. Every branch shaped ``if identity.is_agent: ... else: <human path>``
# hands such a session the human path by default, and several of those
# branches have no gate at all (review-verdict, pair-start, project creation,
# message threads). Listing them to close them would mean catching the next one
# too, forever; so access is a positive list and everything else is refused,
# including routes that do not exist yet.
#
# Method AND path, never a prefix: ``/api/tasks/`` as a prefix would have let
# through ``approve`` and ``decide``.
CHAT_PAIR_ALLOWLIST: Final[tuple[tuple[str, str], ...]] = (
    ("GET", "/api/whoami"),
    ("GET", "/api/diagnostics/identity"),
    ("GET", "/api/tasks"),
    ("GET", "/api/tasks/{task_id}"),
    ("GET", "/api/tasks/{task_id}/tree"),
    ("GET", "/api/tasks/{task_id}/context"),
    ("GET", "/api/tasks/{task_id}/readiness"),
    ("POST", "/api/tasks"),
    ("POST", "/api/tasks/{task_id}/refine"),
    ("GET", "/api/tasks/{task_id}/acceptance_criteria"),
    ("POST", "/api/tasks/{task_id}/acceptance_criteria"),
    # Replace-by-list and delete are here on purpose: this is the same
    # acceptance-criteria authoring the channel exists for, and neither touches
    # anything outside the draft of a task.
    ("PUT", "/api/tasks/{task_id}/acceptance_criteria"),
    ("PUT", "/api/tasks/{task_id}/acceptance_criteria/{ac_id}"),
    ("DELETE", "/api/tasks/{task_id}/acceptance_criteria/{ac_id}"),
    ("POST", "/api/tasks/{task_id}/risks"),
    ("POST", "/api/auth/chat-pair/redeem"),
    # The one route a session calls about itself: finishing the channel from
    # the phone must not require going back to the laptop.
    ("POST", "/api/auth/chat-pair/revoke"),
)

# Implementer allowlist is a sibling of intake, not a widening of it (#980).
# {task_id} is captured and compared to the session's bound_task_id.
CHAT_PAIR_IMPLEMENTER_ALLOWLIST: Final[tuple[tuple[str, str], ...]] = (
    ("GET", "/api/whoami"),
    ("GET", "/api/diagnostics/identity"),
    ("GET", "/api/tasks/{task_id}"),
    ("GET", "/api/tasks/{task_id}/tree"),
    ("GET", "/api/tasks/{task_id}/context"),
    ("GET", "/api/tasks/{task_id}/readiness"),
    ("GET", "/api/tasks/{task_id}/review-brief"),
    ("GET", "/api/tasks/{task_id}/acceptance_criteria"),
    ("GET", "/api/tasks/{task_id}/updates"),
    ("POST", "/api/tasks/{task_id}/updates"),
    ("POST", "/api/tasks/{task_id}/question"),
    ("POST", "/api/tasks/{task_id}/claim"),
    ("POST", "/api/tasks/{task_id}/pair-start"),
    ("POST", "/api/tasks/{task_id}/submit-review"),
    ("POST", "/api/tasks/{task_id}/declare-wait"),
    ("POST", "/api/sessions/register"),
    ("POST", "/api/sessions/{session_id}/heartbeat"),
    ("POST", "/api/auth/chat-pair/redeem"),
    ("POST", "/api/auth/chat-pair/revoke"),
)

# #1084: the cloud reviewer. TWO routes — read the brief, file the report —
# and nothing else, by the same reasoning as STEWARD_ALLOWLIST below: a list
# built by subtracting from the implementer's would still carry claim,
# pair-start and submit-review, and the reviewer must never be able to take
# the task or sign a verdict on it. /api/whoami is left out too: the redeem
# response already tells the run who it is, so the route would buy nothing
# and widen the surface.
CHAT_PAIR_REVIEWER_ALLOWLIST: Final[tuple[tuple[str, str], ...]] = (
    ("GET", "/api/tasks/{task_id}/review-brief"),
    ("POST", "/api/tasks/{task_id}/machine-review"),
)


# ---------------------------------------------------------------------------
# Steward route allowlist (#1021)
# ---------------------------------------------------------------------------
#
# Same shape as chat-pair: identity is otherwise binary (human or agent), and
# several branches read "not an agent" as "a human". Listing those branches
# forever is how #961 arrived. Access is a positive list of two operations;
# everything else is refused, including routes that do not exist yet.
#
# Method AND path, never a prefix.
STEWARD_ALLOWLIST: Final[tuple[tuple[str, str], ...]] = (
    ("GET", "/api/tasks/{task_id}/steward-evidence"),
    ("POST", "/api/tasks/{task_id}/steward-judgement"),
)


def _template_to_regex(template: str) -> re.Pattern[str]:
    """``/api/tasks/{task_id}/refine`` → an anchored regex over path segments.

    ``AuthMiddleware.dispatch`` runs before routing, so FastAPI's route
    template is not known there — only the raw path. Compiling the templates
    once at import keeps the check a match against a known shape rather than
    string surgery on every request.

    ``{task_id}`` is a named group so implementer sessions can refuse a
    different task without a second parse (#980). Other placeholders stay
    anonymous — ``{session_id}`` is the caller's own UUID, not the bound task.
    """
    parts: list[str] = []
    for seg in template.split("/"):
        if seg.startswith("{") and seg.endswith("}"):
            name = seg[1:-1]
            if name == "task_id":
                parts.append("(?P<task_id>[^/]+)")
            else:
                parts.append("[^/]+")
        else:
            parts.append(re.escape(seg))
    return re.compile("^" + "/".join(parts) + "$")


_CHAT_PAIR_ALLOWED: Final[tuple[tuple[str, re.Pattern[str]], ...]] = tuple(
    (method, _template_to_regex(template)) for method, template in CHAT_PAIR_ALLOWLIST
)
_CHAT_PAIR_IMPLEMENTER_ALLOWED: Final[tuple[tuple[str, re.Pattern[str]], ...]] = tuple(
    (method, _template_to_regex(template))
    for method, template in CHAT_PAIR_IMPLEMENTER_ALLOWLIST
)
_CHAT_PAIR_REVIEWER_ALLOWED: Final[tuple[tuple[str, re.Pattern[str]], ...]] = tuple(
    (method, _template_to_regex(template))
    for method, template in CHAT_PAIR_REVIEWER_ALLOWLIST
)
_ALLOW_BY_KIND: Final[dict[str, tuple[tuple[str, re.Pattern[str]], ...]]] = {
    "implementer": _CHAT_PAIR_IMPLEMENTER_ALLOWED,
    "reviewer": _CHAT_PAIR_REVIEWER_ALLOWED,
}
_STEWARD_ALLOWED: Final[tuple[tuple[str, re.Pattern[str]], ...]] = tuple(
    (method, _template_to_regex(template)) for method, template in STEWARD_ALLOWLIST
)


def chat_pair_route_allowed(
    method: str, path: str, identity: TokenIdentity | None = None
) -> bool:
    """Whether a chat-pair session may reach ``(method, path)``.

    Intake uses :data:`CHAT_PAIR_ALLOWLIST` unchanged. Implementer uses its
    own list and, when the path carries ``{task_id}``, requires that segment
    to equal the bound task. A non-numeric segment is a refusal, not a 500.
    """
    kind = (
        (getattr(identity, "chat_pair_kind", None) or "intake")
        if identity
        else "intake"
    )
    allow = _ALLOW_BY_KIND.get(kind, _CHAT_PAIR_ALLOWED)
    bound = getattr(identity, "chat_pair_task_id", None) if identity else None
    probe = "GET" if method == "HEAD" else method
    for allowed_method, pattern in allow:
        if probe != allowed_method:
            continue
        matched = pattern.match(path)
        if not matched:
            continue
        captured = matched.groupdict().get("task_id")
        if kind in config.CHAT_PAIR_TASK_BOUND_KINDS and captured is not None:
            try:
                got = int(captured)
            except ValueError:
                return False
            if bound is None or got != int(bound):
                return False
        return True
    return False


def steward_route_allowed(
    method: str, path: str, identity: TokenIdentity | None = None
) -> bool:
    """Whether a steward principal may reach ``(method, path)`` (#1021)."""
    if identity is not None and not identity.is_steward:
        return False
    probe = "GET" if method == "HEAD" else method
    for allowed_method, pattern in _STEWARD_ALLOWED:
        if probe == allowed_method and pattern.match(path):
            return True
    return False


_PUBLIC_PREFIXES: Final[tuple[str, ...]] = ("/static/",)

_PROTECTED_PREFIXES: Final[tuple[str, ...]] = ("/",)

ANONYMOUS_USER: Final[str] = "anonymous"
ANONYMOUS_IDENTITY: Final[TokenIdentity] = TokenIdentity(
    "anonymous", "human", auth_source="anonymous"
)
OPEN_MODE_IDENTITY: Final[TokenIdentity] = TokenIdentity(
    "anonymous", "human", auth_source="open_mode"
)


def _with_auth_source(
    identity: TokenIdentity,
    auth_source: str,
    *,
    api_key_id: int | None = None,
) -> TokenIdentity:
    return TokenIdentity(
        identity.username,
        identity.role,
        identity.principal_id,
        identity.permissions,
        auth_source=auth_source,
        api_key_id=api_key_id,
        chat_pair_kind=identity.chat_pair_kind,
        chat_pair_task_id=identity.chat_pair_task_id,
        chat_pair_generation=identity.chat_pair_generation,
    )


def client_ip(request: Request) -> str:
    """The address a per-IP limiter counts against.

    One reader for the proxy header: the web login, the pairing redeem and
    anything added later must agree on who the caller is, or a limit measured
    in one place will be spent in another.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


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
    """The session token the browser presented.

    An explicit HAIPLANE_HUB_COOKIE override names the cookie read;
    otherwise it is the canonical ``haiplane_hub_session``.
    """
    cookie_token = request.cookies.get(config.HUB_COOKIE_NAME)
    if cookie_token and cookie_token.strip():
        return cookie_token.strip()
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


async def _resolve_chat_pair(request: Request, token: str) -> TokenIdentity | None:
    """Try to resolve a bearer token as a chat-pair session (#961)."""
    db_conn = getattr(getattr(getattr(request, "app", None), "state", None), "db", None)
    if db_conn is None:
        return None
    try:
        from hub.services.chat_pair import resolve_session

        return await resolve_session(db_conn, token)
    except Exception:
        return None


async def _resolve_identity(request: Request) -> TokenIdentity | None:
    """Resolve identity from bearer header or session cookie.

    Priority: bearer chat-pair session > bearer DB key > bearer env token >
    cookie DB session > cookie env token. Chat-pair goes first because its
    tokens carry their own prefix — the lookup is one indexed hash, and the
    session must never be mistaken for the API key of the same principal.
    """
    bearer = _extract_bearer(request)
    if bearer:
        identity = await _resolve_chat_pair(request, bearer)
        if identity:
            return identity
        identity = await _resolve_db_bearer(request, bearer)
        if identity:
            return identity
        identity = _resolve_env_token(bearer)
        if identity:
            return _with_auth_source(identity, "env")

    cookie = _extract_cookie(request)
    if cookie:
        identity = await _resolve_db_session(request, cookie)
        if identity:
            return identity
        identity = _resolve_env_token(cookie)
        if identity:
            return _with_auth_source(identity, "env")

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
        identity_ctx: Any = None
        if path.startswith("/mcp"):
            br = _extract_bearer(request)
            if br:
                mcp_ctx = bearer_context_set(br)

        try:
            if _looks_public(path):
                identity = await _resolve_identity(request) or ANONYMOUS_IDENTITY
                if _chat_pair_refused(identity, request.method, path):
                    return _chat_pair_forbidden(request.method, path)
                if _steward_refused(identity, request.method, path):
                    await _record_steward_refusal(request, request.method, path)
                    return _steward_forbidden(request.method, path)
                request.state.user = identity.username
                request.state.identity = identity
                return await call_next(request)

            if _is_open_mode():
                request.state.user = ANONYMOUS_USER
                request.state.identity = OPEN_MODE_IDENTITY
                return await call_next(request)

            resolved = await _resolve_identity(request)
            if not resolved:
                # #955: корень для анонима — визитка продукта, а не редирект на
                # форму входа: ссылка из статьи или каталога обязана объяснять,
                # куда человек попал. Пропускаем с анонимной идентичностью —
                # обработчик по ней отдаёт статическую страницу и к данным
                # задач не обращается. Все прочие пути защищены как прежде.
                if path == "/" and request.method in {"GET", "HEAD"}:
                    request.state.user = ANONYMOUS_USER
                    request.state.identity = ANONYMOUS_IDENTITY
                    return await call_next(request)
                return _unauthorized(request)
            identity = resolved
            # The allowlist is checked HERE, before the route runs, so a branch
            # that reads "not an agent" as "a human" never executes for a
            # chat-pair session in the first place (#961).
            if _chat_pair_refused(identity, request.method, path):
                return _chat_pair_forbidden(request.method, path)
            if _steward_refused(identity, request.method, path):
                await _record_steward_refusal(request, request.method, path)
                return _steward_forbidden(request.method, path)
            request.state.user = identity.username
            request.state.identity = identity
            if path.startswith("/mcp"):
                # Usage telemetry (#780) names the caller by principal and
                # role. It is set here, where the identity is already
                # resolved, rather than re-resolved inside the MCP layer —
                # two places deciding who the caller is eventually disagree.
                identity_ctx = identity_context_set(
                    identity.principal_id, identity.role
                )
            return await call_next(request)
        finally:
            if mcp_ctx is not None:
                bearer_context_reset(mcp_ctx)
            if identity_ctx is not None:
                identity_context_reset(identity_ctx)


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
        headers={"WWW-Authenticate": 'Bearer realm="haiplane-hub"'},
    )


def _chat_pair_refused(identity: TokenIdentity, method: str, path: str) -> bool:
    return identity.auth_source == "chat_pair" and not chat_pair_route_allowed(
        method, path, identity
    )


def _steward_refused(identity: TokenIdentity, method: str, path: str) -> bool:
    return identity.is_steward and not steward_route_allowed(method, path, identity)


def _chat_pair_forbidden(method: str, path: str) -> Response:
    """403 with the same actionable payload the REST handlers raise (#961)."""
    return Response(
        status_code=status.HTTP_403_FORBIDDEN,
        content=json.dumps(
            {"detail": chat_pair_gate_forbidden_detail(method, path)},
            ensure_ascii=False,
        ),
        media_type="application/json",
    )


async def _record_steward_refusal(request: Request, method: str, path: str) -> None:
    """A refused steward reaches the audit, not just the caller (#1075).

    The refusal itself is the interesting event: the steward has exactly two
    doors, so a request at a third one is either a bug in the run or someone
    probing the boundary, and both are only visible if the attempt is written
    down. Best effort by contract — a failure to record must never turn a 403
    into a 500, because refusing is the part that protects anything.
    """
    db = getattr(request.app.state, "db", None)
    if db is None:
        return
    try:
        from hub import repository as repo
        from hub.services.gate_events import STEWARD_ROUTE_REFUSED

        identity = getattr(request.state, "identity", None)
        await repo.insert_event(
            db,
            kind=STEWARD_ROUTE_REFUSED,
            actor=getattr(identity, "username", "") or "steward",
            payload={"method": method, "path": path},
        )
        await db.commit()
    except Exception:  # noqa: BLE001 — the refusal stands regardless
        log.warning("steward refusal not recorded: %s %s", method, path)


def _steward_forbidden(method: str, path: str) -> Response:
    """403 with the same actionable payload the REST handlers raise (#1021)."""
    return Response(
        status_code=status.HTTP_403_FORBIDDEN,
        content=json.dumps(
            {"detail": steward_gate_forbidden_detail(method, path)},
            ensure_ascii=False,
        ),
        media_type="application/json",
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
    """Rejects anyone who is not a human (agents, steward, other third states)."""
    identity = current_identity(request)
    if not identity.is_human:
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
