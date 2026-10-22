"""#1994: bodies of the extra-origin CSRF tests, called from tests/test_csrf.py.

tests/test_csrf.py is at its size ceiling (the tests ledger only shrinks), so the
four AC tests there are thin wrappers and the cases live here. Helpers are
repeated on purpose: importing them from test_csrf would be a cycle.
"""

from __future__ import annotations

import logging
import re

from hub import brand, config, csrf
from hub.config import TokenIdentity
from hub.host_security import HostAllowlistMiddleware
from hub.services import admin as admin_svc

EXTRA = "https://haiplane.com"
EXTRA_ENV = brand.ENV_PREFIX + "HUB_EXTRA_ORIGINS"
META = re.compile(r'<meta name="csrf-token" content="([^"]+)"')


def _cookie(session: str) -> dict[str, str]:
    return {"Cookie": f"{config.HUB_COOKIE_NAME}={session}"}


def _refused(resp) -> bool:
    return resp.status_code == 403 and "csrf_failed" in resp.text


async def _token(world, session: str) -> str:
    resp = await world.client.get(
        "/tasks", headers={**_cookie(session), "Accept": "text/html"}
    )
    assert resp.status_code == 200, resp.text
    return META.search(resp.text).group(1)


async def _post(world, session: str, *, token: str | None = None, **headers: str):
    sent = {**_cookie(session), **headers}
    if token is not None:
        sent["X-CSRF-Token"] = token
    return await world.client.post("/__csrf_echo", headers=sent)


async def extra_origins_trusted_and_nothing_else(world, monkeypatch) -> None:
    monkeypatch.setattr(config, "CSRF_MODE", "require", raising=False)
    monkeypatch.setenv(EXTRA_ENV, EXTRA)
    own = world.own["Origin"]
    tok_a = await _token(world, world.session_a)
    tok_b = await _token(world, world.session_b)
    a = world.session_a
    allowed = [
        dict(token=tok_a, Origin=EXTRA),
        dict(token=tok_a, Origin=own),
        dict(token=tok_a, Referer=EXTRA + "/tasks/1"),
    ]
    refused = [
        dict(Origin=EXTRA),
        dict(token="forged", Origin=EXTRA),
        dict(token=tok_b, Origin=EXTRA),
        dict(token=tok_a, Origin="https://evil.example"),
        dict(token=tok_a, Origin="null", Referer=EXTRA + "/tasks/1"),
        dict(token=tok_a, Origin="https://evil.example", Referer=EXTRA + "/tasks/1"),
        dict(token=tok_a, Origin="http://haiplane.com"),
        dict(token=tok_a, Origin="https://haiplane.com:8443"),
        dict(token=tok_a, Referer="https://evil.example/tasks/1"),
    ]
    for case in allowed:
        resp = await _post(world, a, **case)
        assert resp.status_code == 200, (case, resp.text)
    for case in refused:
        resp = await _post(world, a, **case)
        assert _refused(resp), case
    form = await world.client.post(
        "/__csrf_echo",
        data={"csrf_token": tok_a},
        headers={**_cookie(a), "Origin": EXTRA},
    )
    assert form.status_code == 200, form.text


class _Records(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


# One value, every kind of damage. "ok" and the duplicates are the only good ones.
_BAD_AND_GOOD = [
    " ",
    "haiplane.com",
    "ftp://ftp.example",
    "https://*.wild.example",
    "https://user:pw@leak.example",  # pragma: allowlist secret
    "https://path.example/path",
    "https://zero.example:0",
    "https://big.example:99999",
    "https://hub.example.test",
    "HTTPS://Hub.Example.Test",
    "https://ok.example.test:8443",
    "https://ok.example.test:8443",
]
_MUST_STAY_OUT = [
    "https://haiplane.com",
    "https://leak.example",
    "https://user:pw@leak.example",  # pragma: allowlist secret
    "https://path.example",
    "https://zero.example",
    "https://zero.example:0",
    "https://big.example",
    "https://sub.wild.example",
    "http://ftp.example",
    "https://wild.example",
]


async def bad_extra_origins_never_widen_trust(world, monkeypatch) -> None:
    monkeypatch.setattr(config, "CSRF_MODE", "require", raising=False)
    own = world.own["Origin"]
    token = await _token(world, world.session_a)
    csrf._parse_extra_origins.cache_clear()  # the warning is once per value
    seen = _Records()
    logger = logging.getLogger("hub.csrf")
    monkeypatch.setattr(logger, "handlers", [*logger.handlers, seen])

    async def health() -> dict:
        resp = await world.client.get("/health")
        assert resp.status_code == 200, resp.text
        for leaked in ("pw", "leak.example", "wild.example", "zero.example"):
            assert leaked not in resp.text, leaked
        return resp.json()

    # Unset: only the hub's own address, and nothing to warn about.
    data = await health()
    assert data["csrf_trusted_origins"] == [own]
    assert "EXTRA_ORIGINS" not in data["csrf_warning"]

    monkeypatch.setenv(EXTRA_ENV, ",".join(_BAD_AND_GOOD))
    data = await health()
    assert data["csrf_trusted_origins"] == [own, "https://ok.example.test:8443"]
    warning = data["csrf_warning"]
    assert EXTRA_ENV in warning and "8 of 12" in warning, warning

    for origin in (own, "https://ok.example.test:8443"):
        resp = await _post(world, world.session_a, token=token, Origin=origin)
        assert resp.status_code == 200, (origin, resp.text)
    for origin in _MUST_STAY_OUT:
        resp = await _post(world, world.session_a, token=token, Origin=origin)
        assert _refused(resp), origin

    # Nothing parses: trust is exactly what it was without the variable.
    monkeypatch.setenv(EXTRA_ENV, "haiplane.com,*,https://u:p@x.example")
    data = await health()
    assert data["csrf_trusted_origins"] == [own]
    assert "3 of 3" in data["csrf_warning"]
    resp = await _post(world, world.session_a, token=token, Origin=EXTRA)
    assert _refused(resp)
    # One log line per distinct bad value, naming counts only.
    lines = [line for line in seen.lines if "ignored" in line]
    assert len(lines) == 2, seen.lines
    for line in seen.lines:
        for leaked in ("pw", "leak.example", "haiplane", "wild", "zero", "x.example"):
            assert leaked not in line, line


async def _slow_body_refusal(monkeypatch, query: bytes) -> None:
    clock = {"now": 0.0}
    monkeypatch.setattr(csrf, "_monotonic", lambda: clock["now"])
    chunks = [b"a=1&", b"b=2"]

    async def receive():
        clock["now"] += config.CSRF_BODY_DEADLINE
        piece = chunks.pop(0)
        return {"type": "http.request", "body": piece, "more_body": bool(chunks)}

    async def downstream(scope, rcv, send):
        raise AssertionError("a refused request must not reach the handler")

    async def send(message):
        return None

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/slow",
        "raw_path": b"/slow",
        "query_string": query,
        "scheme": "http",
        "server": ("test", 80),
        "headers": [(b"content-type", b"application/x-www-form-urlencoded")],
        "state": {"identity": TokenIdentity("u", "human", transport="cookie")},
    }
    await csrf.CsrfMiddleware(downstream)(scope, receive, send)


async def _count_events(db) -> int:
    cur = await db.execute(
        "SELECT COUNT(*) FROM events WHERE kind = 'csrf_would_reject'"
    )
    return (await cur.fetchone())[0]


async def require_mode_logs_the_refusal_reason(world, monkeypatch) -> None:
    own = world.own["Origin"]
    session = world.session_a
    token = await _token(world, session)
    handler = _Records()
    logger = logging.getLogger("hub.csrf")
    logger.addHandler(handler)
    monkeypatch.setattr(logger, "level", logging.WARNING)
    secrets_to_hide = ("secret=1", session, token)

    async def refusal(reason: str, send) -> None:
        handler.lines.clear()
        await send()
        assert len(handler.lines) == 1, (reason, handler.lines)
        line = handler.lines[0]
        assert reason in line and "POST" in line, line
        for hidden in secrets_to_hide:
            assert hidden not in line, (reason, hidden)

    async def post(path: str = "/__csrf_echo?secret=1", **kw):
        return await world.client.post(path, **kw)

    def go(**kw):
        headers = {**_cookie(session), **kw.pop("headers", {})}
        return lambda: post(headers=headers, **kw)

    try:
        monkeypatch.setattr(config, "CSRF_MODE", "require", raising=False)
        await refusal(
            "origin_mismatch",
            go(headers={"Origin": "https://evil.example", "X-CSRF-Token": token}),
        )
        await refusal(
            "referer_mismatch",
            go(headers={"Referer": "https://evil.example/x", "X-CSRF-Token": token}),
        )
        await refusal("token_missing", go(headers={"Origin": own}))
        await refusal(
            "token_invalid", go(headers={"Origin": own, "X-CSRF-Token": "forged"})
        )
        monkeypatch.setattr(config, "CSRF_BODY_LIMIT", 10)
        big = {"Origin": own, "Content-Type": "application/x-www-form-urlencoded"}
        await refusal("body_too_large", go(headers=big, content=b"a=" + b"x" * 64))
        monkeypatch.setattr(config, "CSRF_BODY_LIMIT", 1024 * 1024)
        await refusal(
            "body_timeout", lambda: _slow_body_refusal(monkeypatch, b"secret=1")
        )
        handler.lines.clear()
        ok = await post(
            headers={**_cookie(session), "Origin": own, "X-CSRF-Token": token}
        )
        assert ok.status_code == 200 and handler.lines == []
        assert await _count_events(world.db) == 0, "require never writes events"

        for mode, events in (("warn", 1), ("off", 1)):
            monkeypatch.setattr(config, "CSRF_MODE", mode, raising=False)
            handler.lines.clear()
            resp = await post(
                headers={**_cookie(session), "Origin": "https://evil.example"}
            )
            assert resp.status_code == 200, (mode, resp.text)
            assert handler.lines == [], (mode, handler.lines)
            assert await _count_events(world.db) == events, mode
    finally:
        logger.removeHandler(handler)


async def second_domain_full_browser_flow(world, monkeypatch) -> None:
    from hub.app import app

    first, second = "hub.example.test", "second.example.test"
    monkeypatch.setattr(config, "CSRF_MODE", "require", raising=False)
    monkeypatch.setenv(EXTRA_ENV, f"https://{second}")
    await world.client.get("/healthz")  # builds the stack that serves requests
    layer = app.middleware_stack
    while layer is not None and not isinstance(layer, HostAllowlistMiddleware):
        layer = getattr(layer, "app", None)
    assert layer is not None, "the host allowlist must be in the stack"
    monkeypatch.setattr(layer, "allowed_hosts", frozenset({first, second}))
    await admin_svc.create_principal(
        world.db,
        kind="human",
        username="bob-second",
        password="correct horse battery 9",  # pragma: allowlist secret
        role_slug="operator",
    )
    await world.db.commit()
    client = world.client
    client.cookies.clear()
    origin = {"Origin": f"https://{second}", "Host": second}

    third = await client.get("/health", headers={"Host": "third.example.test"})
    assert third.status_code == 421, "an unlisted host must stay refused"

    page = await client.get("/login", headers={**origin, "Accept": "text/html"})
    assert page.status_code == 200, page.text
    form_token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    login = await client.post(
        "/login",
        data={
            "username": "bob-second",
            "password": "correct horse battery 9",  # pragma: allowlist secret
            "csrf_token": form_token,
        },
        headers=origin,
        follow_redirects=False,
    )
    assert login.status_code == 303, login.text
    cookies = login.headers.get_list("set-cookie")
    session_cookie = [c for c in cookies if c.startswith(config.HUB_COOKIE_NAME + "=")]
    assert session_cookie and "domain" not in session_cookie[0].lower(), cookies

    task_page = await client.get("/tasks", headers={**origin, "Accept": "text/html"})
    assert task_page.status_code == 200, task_page.text
    token = META.search(task_page.text).group(1)
    by_form = await client.post(
        "/__csrf_echo", data={"csrf_token": token, "a": "1"}, headers=origin
    )
    assert by_form.status_code == 200, by_form.text
    by_header = await client.post(
        "/__csrf_echo", headers={**origin, "X-CSRF-Token": token}
    )
    assert by_header.status_code == 200, by_header.text
    out = await client.post(
        "/logout", headers={**origin, "X-CSRF-Token": token}, follow_redirects=False
    )
    assert out.status_code == 303 and out.headers["location"] == "/login", out.text
