"""CSRF for mutating requests that ride a cookie session (#1664).

One check for the whole app instead of one call per handler: a handler that
forgets it is how 41 of 49 web POST routes ended up unprotected.

The token is ``HMAC-SHA256(secret, sha256(session cookie))``. It is bound to
the session without a migration (the hash of the cookie is what
``browser_sessions`` stores anyway), lives exactly as long as the session, and
is the same for every page of that session, so a second tab cannot invalidate
the first.

Who is checked is decided by ``identity.transport`` (set by the resolver in
``hub.auth``): only ``cookie`` is forgeable from another site. A Bearer client
sends its credential on purpose and is never asked for a token.

``CSRF_MODE``: ``off`` checks nothing, ``warn`` (the default) lets the request
through and writes ``csrf_would_reject``, ``require`` answers 403.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import secrets
import stat
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.types import Message, Receive, Scope, Send

from hub import config
from hub.hub_instance import hub_base_url

log = logging.getLogger("hub.csrf")

MUTATING_METHODS: Final[frozenset[str]] = frozenset({"POST", "PUT", "PATCH", "DELETE"})
CSRF_FIELD_NAME: Final[str] = "csrf_token"
CSRF_HEADER_NAME: Final[str] = "X-CSRF-Token"
CSRF_WOULD_REJECT: Final[str] = "csrf_would_reject"

# Method + path, never a prefix. Everything here is reachable without a
# session by design (the caller has no identity to forge), or carries its own
# credential. /logout is NOT here: a page that can log the operator out can
# also keep them from seeing a stale-form message.
EXEMPT_ROUTES: Final[frozenset[tuple[str, str]]] = frozenset(
    {
        # The pre-session token of the login form is verified by the handler.
        ("POST", "/login"),
        ("POST", "/api/admin/bootstrap"),
        ("POST", "/api/auth/chat-pair/redeem"),
    }
)

_FORM_TYPES: Final[tuple[str, ...]] = (
    "application/x-www-form-urlencoded",
    "multipart/form-data",
)
_DEFAULT_PORTS: Final[dict[str, int]] = {"http": 80, "https": 443}


# ---------------------------------------------------------------------------
# Secret
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SecretState:
    value: bytes
    source: str  # env | file | unusable
    problem: str = ""


_KEY_HEX_LEN = 64
_READ_ATTEMPTS = 10
_READ_PAUSE = 0.05
_UNUSABLE_RETRY_SECONDS = 30.0

_secret_cache: SecretState | None = None
_secret_cached_at = 0.0


def reset_secret_cache() -> None:
    global _secret_cache
    _secret_cache = None


def _secret_path() -> Path:
    return config.HUB_DB_PATH.parent / "csrf_secret"


def _unusable(problem: str) -> SecretState:
    log.warning("csrf key file unusable: %s", problem)
    return SecretState(b"", "unusable", problem)


def _read_key_file(path: Path) -> SecretState | None:
    """The published key, ``None`` while the file is empty or short, else unusable.

    No symlink is followed, and the file must be a regular file of this user
    that nobody else can read or write: a key anyone can read is not a key.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        return _unusable(f"cannot open ({exc.strerror})")
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return _unusable("not a regular file")
        if info.st_uid != os.getuid():
            return _unusable("owned by another user")
        if info.st_mode & 0o077:
            return _unusable("readable or writable by group or others")
        with os.fdopen(fd, "r", closefd=False) as handle:
            text = handle.read().strip()
    finally:
        os.close(fd)
    if len(text) < _KEY_HEX_LEN:
        return None
    return SecretState(text.encode(), "file")


def _publish_key_file(path: Path) -> None:
    """Write a private temp file, then link it into place; losing the race is fine."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(secrets.token_hex(_KEY_HEX_LEN // 2))
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(tmp, path)
        except FileExistsError:
            pass
    finally:
        tmp.unlink(missing_ok=True)


def _load_or_create_secret_file() -> SecretState:
    path = _secret_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not os.path.lexists(path):
            _publish_key_file(path)
    except OSError as exc:
        return _unusable(f"cannot create ({exc.strerror})")
    for attempt in range(_READ_ATTEMPTS):
        state = _read_key_file(path)
        if state is not None:
            return state
        if attempt + 1 < _READ_ATTEMPTS:
            time.sleep(_READ_PAUSE)
    return _unusable("empty or short")


def secret_state() -> SecretState:
    """The key and where it came from: ``env``, else a private 0600 file.

    When neither works the state is ``unusable`` with an empty key: no token is
    issued or accepted, so ``require`` refuses cookie mutations and ``/health``
    says why. A silent per-process key would differ between workers.
    """
    global _secret_cache, _secret_cached_at
    if config.CSRF_SECRET:
        return SecretState(config.CSRF_SECRET.encode(), "env")
    now = time.monotonic()
    stale = (
        _secret_cache is not None
        and _secret_cache.source == "unusable"
        and now - _secret_cached_at > _UNUSABLE_RETRY_SECONDS
    )
    if _secret_cache is None or stale:
        _secret_cache = _load_or_create_secret_file()
        _secret_cached_at = now
    return _secret_cache


def secret_warning(state: SecretState | None = None) -> str:
    """Text for the public /health: it must not carry the word "secret"."""
    state = state or secret_state()
    if state.source == "env":
        return ""
    if state.source == "file":
        return (
            "CSRF key is read from a file next to the database; "
            "set it in the service environment"
        )
    return (
        f"CSRF key file is unusable ({state.problem}); cookie sessions get no "
        "CSRF token until it is fixed or the key is set in the service environment"
    )


# ---------------------------------------------------------------------------
# Token
# ---------------------------------------------------------------------------


def token_for_session_cookie(cookie_value: str) -> str:
    session_hash = hashlib.sha256(cookie_value.encode()).hexdigest()
    return hmac.new(
        secret_state().value, session_hash.encode(), hashlib.sha256
    ).hexdigest()


def session_token_ok(presented: str | None, cookie_value: str | None) -> bool:
    if not presented or not cookie_value:
        return False
    if secret_state().source == "unusable":
        return False
    expected = token_for_session_cookie(cookie_value)
    return hmac.compare_digest(presented.encode(), expected.encode())


def _cookie_value(request: Request) -> str:
    return (request.cookies.get(config.HUB_COOKIE_NAME) or "").strip()


def request_session_token_ok(request: Request, presented: str | None) -> bool:
    return session_token_ok(presented, _cookie_value(request))


def token_for_request(request: Request) -> str:
    """The token to put in a page, or empty when the caller has no cookie session."""
    identity = getattr(request.state, "identity", None)
    cookie = _cookie_value(request)
    if getattr(identity, "transport", "") != "cookie" or not cookie:
        return ""
    if secret_state().source == "unusable":
        return ""
    return token_for_session_cookie(cookie)


# ---------------------------------------------------------------------------
# Origin
# ---------------------------------------------------------------------------


def _origin_triple(value: str) -> tuple[str, str, int] | None:
    try:
        parts = urlsplit(value.strip())
        scheme = parts.scheme.lower()
        host = (parts.hostname or "").lower()
        port = parts.port or _DEFAULT_PORTS.get(scheme)
    except ValueError:
        return None
    if scheme not in _DEFAULT_PORTS or not host or port is None:
        return None
    return scheme, host, port


def _origin_problem(request: Request) -> str:
    """Origin, else Referer, against the public address of the hub."""
    own = _origin_triple(hub_base_url())
    origin = request.headers.get("origin")
    if origin is not None:
        return "" if own and _origin_triple(origin) == own else "origin_mismatch"
    referer = request.headers.get("referer")
    if referer:
        return "" if own and _origin_triple(referer) == own else "referer_mismatch"
    return ""


# ---------------------------------------------------------------------------
# Body replay
# ---------------------------------------------------------------------------


def _replayer(buffered: list[Message], receive: Receive) -> Receive:
    """A ``receive`` that hands the buffered messages out first, then the rest."""
    queue = deque(buffered)

    async def replay() -> Message:
        if queue:
            return queue.popleft()
        return await receive()

    return replay


_monotonic = time.monotonic


async def _buffer_body(
    receive: Receive, limit: int, deadline_seconds: float
) -> tuple[list[Message], str]:
    """Read the body into ONE message, up to ``limit`` bytes and a deadline.

    Returns (messages to replay, "" | "body_too_large" | "body_timeout").
    Fragments are joined into a single ``bytearray``: a dict per ASGI message
    would cost about 190 times the body for a body sent byte by byte.
    """
    body = bytearray()
    tail: list[Message] = []
    started = _monotonic()
    reason = ""
    more = True
    while more:
        remaining = deadline_seconds - (_monotonic() - started)
        if remaining <= 0:
            reason = "body_timeout"
            break
        try:
            message = await asyncio.wait_for(receive(), timeout=remaining)
        except asyncio.TimeoutError:
            reason = "body_timeout"
            break
        if message["type"] != "http.request":
            tail.append(message)
            reason = "body_too_large"  # a disconnect: nothing complete to judge
            break
        body.extend(message.get("body", b""))
        more = message.get("more_body", False)
        if len(body) > limit:
            reason = "body_too_large"
            break
    unfinished = bool(reason) or more
    head: Message = {
        "type": "http.request",
        "body": bytes(body),
        "more_body": unfinished and not tail,
    }
    return [head, *tail], reason


async def _form_token(scope: Scope, replay: Receive) -> str | None:
    request = Request(scope, replay)
    try:
        form = await request.form()
    except Exception:  # noqa: BLE001 — a body that does not parse has no token
        return None
    try:
        value = form.get(CSRF_FIELD_NAME)
        return value if isinstance(value, str) else None
    finally:
        await form.close()


async def _judge_form(
    request: Request, scope: Scope, receive: Receive
) -> tuple[str, Receive]:
    ctype = (request.headers.get("content-type") or "").lower()
    if not ctype.startswith(_FORM_TYPES):
        return "token_missing", receive
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > config.CSRF_BODY_LIMIT:
        return "body_too_large", receive
    buffered, reason = await _buffer_body(
        receive, config.CSRF_BODY_LIMIT, config.CSRF_BODY_DEADLINE
    )
    passed_on = _replayer(buffered, receive)
    if reason:
        return reason, passed_on
    token = await _form_token(scope, _replayer(buffered, receive))
    if request_session_token_ok(request, token):
        return "", passed_on
    return ("token_invalid" if token else "token_missing"), passed_on


async def _judge(scope: Scope, receive: Receive) -> tuple[str, Receive]:
    """Empty reason means the request carries a valid token and a fitting origin."""
    request = Request(scope, receive)
    problem = _origin_problem(request)
    if problem:
        return problem, receive
    presented = request.headers.get(CSRF_HEADER_NAME)
    if presented:
        ok = request_session_token_ok(request, presented)
        return ("" if ok else "token_invalid"), receive
    return await _judge_form(request, scope, receive)


# ---------------------------------------------------------------------------
# Answers
# ---------------------------------------------------------------------------

_STALE_TEXT = "Форма устарела. Обновите страницу и повторите действие."


def _refusal(request: Request, cause: str) -> Response:
    path = request.url.path
    if path.startswith(("/api", "/mcp")):
        return JSONResponse(
            {
                "detail": {
                    "reason": "csrf_failed",
                    "cause": cause,
                    "actor_hint": "human",
                    "message": (
                        "Cookie-сессия требует CSRF-токен: заголовок "
                        f"{CSRF_HEADER_NAME} из страницы хаба. Клиентам "
                        "без браузера нужен Authorization: Bearer."
                    ),
                }
            },
            status_code=403,
        )
    if request.headers.get("hx-request", "").lower() == "true":
        return HTMLResponse(
            f'<div class="flash error" role="alert" data-reason="csrf_failed">{_STALE_TEXT}</div>',
            status_code=403,
            headers={"HX-Retarget": "body", "HX-Reswap": "beforeend"},
        )
    return HTMLResponse(
        '<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">'
        "<title>Форма устарела</title></head><body>"
        f'<p role="alert" data-reason="csrf_failed">{_STALE_TEXT}</p>'
        '<p><a href="/">На главную</a></p></body></html>',
        status_code=403,
    )


def _mcp_cookie_refusal() -> Response:
    return JSONResponse(
        {
            "detail": {
                "reason": "mcp_cookie_forbidden",
                "actor_hint": "agent",
                "message": "MCP принимает только Authorization: Bearer, не cookie.",
            }
        },
        status_code=403,
    )


async def _record_would_reject(scope: Scope, request: Request, reason: str) -> None:
    """Best effort: a failure to write the event must never refuse the request."""
    db = getattr(getattr(scope.get("app"), "state", None), "db", None)
    if db is None:
        return
    try:
        from hub import repository as repo

        identity = getattr(request.state, "identity", None)
        await repo.insert_event(
            db,
            kind=CSRF_WOULD_REJECT,
            actor=getattr(identity, "username", "") or "",
            payload={
                "method": request.method,
                "path": request.url.path,
                "reason": reason,
            },
        )
        await db.commit()
    except Exception:  # noqa: BLE001
        log.warning("csrf_would_reject not recorded: %s", request.url.path)


async def would_reject_count_24h(db: Any) -> int:
    try:
        cur = await db.execute(
            "SELECT COUNT(*) FROM events WHERE kind = ? "
            "AND created_at >= datetime('now', '-1 day')",
            (CSRF_WOULD_REJECT,),
        )
        row = await cur.fetchone()
        return int(row[0]) if row else 0
    except Exception:  # noqa: BLE001
        return 0


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


class CsrfMiddleware:
    """Pure ASGI, placed after authentication: it reads ``identity`` from the scope."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        identity = (scope.get("state") or {}).get("identity")
        if getattr(identity, "transport", "") != "cookie":
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        path = request.url.path
        # MCP is a machine door: a cookie never opens it, whatever the method.
        if path.startswith("/mcp"):
            await _mcp_cookie_refusal()(scope, receive, send)
            return
        mode = config.CSRF_MODE
        if (
            scope["method"] not in MUTATING_METHODS
            or mode == "off"
            or (scope["method"], path) in EXEMPT_ROUTES
        ):
            await self.app(scope, receive, send)
            return
        reason, downstream = await _judge(scope, receive)
        if reason and mode == "require":
            await _refusal(request, reason)(scope, receive, send)
            return
        if reason:
            await _record_would_reject(scope, request, reason)
        await self.app(scope, downstream, send)


def health_fields() -> dict[str, Any]:
    state = secret_state()
    return {
        "csrf_mode": config.CSRF_MODE,
        "csrf_key_source": state.source,
        "csrf_warning": secret_warning(state),
    }
