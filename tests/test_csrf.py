"""#1664 CSRF A: one check for every mutating request that rides a cookie session.

The tests drive the real app through httpx and look at three observable
things only: the response, the events table and ``/health``. The CSRF token is
taken from the page the way a browser gets it (``<meta name="csrf-token">``),
never computed in the test, so a token that the product cannot hand out fails
here too.

"Handler reached" is observed by swapping ``route.app`` of every route for a
spy that answers 204: it proves the request got past the middleware without
running any of the ~120 handlers.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route

from hub import brand, config
from hub.config import TokenIdentity
from hub.services import admin as admin_svc

HUB_URL = "https://hub.example.test"
ENV_TOKEN = "env-human-token"  # pragma: allowlist secret
MUTATING = {"POST", "PUT", "PATCH", "DELETE"}
# Written out on purpose: the list in the product must not be the oracle.
EXEMPT = {
    ("POST", "/login"),
    ("POST", "/api/admin/bootstrap"),
    ("POST", "/api/auth/chat-pair/redeem"),
}
META = re.compile(r'<meta name="csrf-token" content="([^"]+)"')


def _cookie(session: str) -> dict[str, str]:
    return {"Cookie": f"{config.HUB_COOKIE_NAME}={session}"}


def _is_csrf_refusal(resp) -> bool:
    return resp.status_code == 403 and "csrf_failed" in resp.text


def _set_mode(monkeypatch, mode: str) -> None:
    monkeypatch.setattr(config, "CSRF_MODE", mode, raising=False)


async def _echo(request: Request) -> Response:
    """Plays a handler: what it reads is what the middleware must not change."""
    ctype = request.headers.get("content-type", "")
    out: dict = {"method": request.method, "ctype": ctype}
    if "form" in ctype:
        form = await request.form()
        fields: dict = {}
        for key in form.keys():
            values = []
            for value in form.getlist(key):
                if isinstance(value, str):
                    values.append(value)
                else:
                    values.append([value.filename, (await value.read()).decode()])
            fields[key] = values
        out["form"] = fields
        out["form_sizes"] = {k: [len(str(v)) for v in vs] for k, vs in fields.items()}
    else:
        body = await request.body()
        out["body"] = body.decode()
    return JSONResponse(out)


@pytest.fixture
async def world(client, db, monkeypatch):
    """Auth on, two real browser sessions of one human, echo routes mounted."""
    from hub.app import app

    monkeypatch.setenv(brand.ENV_PREFIX + "HUB_URL", HUB_URL)
    monkeypatch.setattr(
        config, "HUB_TOKENS", {ENV_TOKEN: TokenIdentity("env-human", "human")}
    )
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    human = await admin_svc.create_principal(
        db, kind="human", username="alice-csrf", role_slug="operator"
    )
    human_key = await admin_svc.create_api_key(db, human["id"], name="laptop")
    session_a = await admin_svc.create_browser_session(db, human["id"])
    session_b = await admin_svc.create_browser_session(db, human["id"])
    await db.commit()
    methods = sorted(MUTATING)
    echoes = [
        Route("/__csrf_echo", _echo, methods=methods),
        Route("/api/__csrf_echo", _echo, methods=methods),
    ]
    for route in echoes:
        app.router.routes.insert(0, route)
    try:
        yield SimpleNamespace(
            client=client,
            db=db,
            human=human,
            human_bearer={"Authorization": f"Bearer {human_key['plaintext_key']}"},
            session_a=session_a,
            session_b=session_b,
            own={"Origin": HUB_URL},
        )
    finally:
        for route in echoes:
            app.router.routes.remove(route)


async def _page_token(world, session: str) -> str:
    resp = await world.client.get(
        "/tasks", headers={**_cookie(session), "Accept": "text/html"}
    )
    assert resp.status_code == 200, resp.text
    found = META.search(resp.text)
    assert found, "the page must carry the session CSRF token in a meta tag"
    return found.group(1)


async def _count_events(db, kind: str = "csrf_would_reject") -> int:
    cur = await db.execute("SELECT COUNT(*) FROM events WHERE kind = ?", (kind,))
    return (await cur.fetchone())[0]


# ---------------------------------------------------------------------------
# AC-1: every mutating route
# ---------------------------------------------------------------------------


def _inventory(app) -> list[tuple[str, str, Route]]:
    found: list[tuple[str, str, Route]] = []

    def walk(routes, prefix: str) -> None:
        for route in routes:
            if isinstance(route, Mount):
                walk(route.routes, prefix + route.path)
                continue
            methods = set(getattr(route, "methods", None) or MUTATING)
            path = re.sub(r"\{[^}]+\}", "1", prefix + route.path)
            for method in sorted(methods & MUTATING):
                found.append((method, path, route))

    walk(app.routes, "")
    return found


@pytest.mark.asyncio
async def test_every_mutating_route_requires_a_token_for_cookie_sessions(
    world, monkeypatch
):
    from hub.app import app

    _set_mode(monkeypatch, "require")
    reached: list[tuple[str, str]] = []

    async def spy(scope, receive, send):
        reached.append((scope["method"], scope["path"]))
        await Response(status_code=204)(scope, receive, send)

    inventory = _inventory(app)
    keys = {(m, p) for m, p, _ in inventory}
    assert len(keys) >= 100, "the runtime inventory must see the REST and web routes"
    assert ("POST", "/mcp") in keys, "mounts must be walked recursively"
    assert EXEMPT <= keys
    for _, _, route in inventory:
        monkeypatch.setattr(route, "app", spy)

    token_a = await _page_token(world, world.session_a)
    token_b = await _page_token(world, world.session_b)
    assert token_a != token_b
    headers = {**_cookie(world.session_a), **world.own}
    uncovered: list[str] = []
    for method, path in sorted(keys):
        is_mcp = path.startswith("/mcp")

        reached.clear()
        bare = await world.client.request(method, path, headers=headers)
        if (method, path) in EXEMPT:
            if (method, path) not in reached:
                uncovered.append(f"{method} {path}: exempt but refused ({bare.text})")
            continue
        if reached:
            uncovered.append(f"{method} {path}: handler ran without a token")
        if is_mcp:
            assert bare.status_code in (401, 403), (method, path, bare.status_code)
            continue
        if not _is_csrf_refusal(bare):
            uncovered.append(f"{method} {path}: {bare.status_code} {bare.text[:80]}")

        reached.clear()
        foreign = await world.client.request(
            method, path, headers={**headers, "X-CSRF-Token": token_b}
        )
        if reached or not _is_csrf_refusal(foreign):
            uncovered.append(f"{method} {path}: another session's token accepted")

        reached.clear()
        good = await world.client.request(
            method, path, headers={**headers, "X-CSRF-Token": token_a}
        )
        if (method, path) not in reached:
            uncovered.append(f"{method} {path}: valid token did not reach it")
        assert good.status_code == 204, (method, path, good.status_code)
    assert not uncovered, "\n".join(uncovered)


# ---------------------------------------------------------------------------
# AC-2: Bearer clients
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bearer_clients_are_exempt_and_never_fall_back_to_cookie(
    world, scoped_ci_key, monkeypatch
):
    _set_mode(monkeypatch, "require")
    # scoped_ci_key swaps the env tokens for its own; put ours back.
    monkeypatch.setattr(
        config, "HUB_TOKENS", {ENV_TOKEN: TokenIdentity("env-human", "human")}
    )
    client = world.client
    db = world.db

    # env token, DB key of a human, DB key of an agent: no CSRF needed.
    env_resp = await client.post(
        "/api/tasks",
        json={"title": "bearer env"},
        headers={"Authorization": f"Bearer {ENV_TOKEN}"},
    )
    assert env_resp.status_code in (200, 201), env_resp.text
    key_resp = await client.post(
        "/api/tasks", json={"title": "bearer key"}, headers=world.human_bearer
    )
    assert key_resp.status_code in (200, 201), key_resp.text
    agent = await admin_svc.create_principal(
        db, kind="agent", username="bot-csrf", role_slug="agent"
    )
    agent_key = await admin_svc.create_api_key(db, agent["id"], name="agent")
    await db.commit()
    agent_resp = await client.post(
        "/api/sessions/register",
        json={"session_id": "csrf-probe"},
        headers={"Authorization": f"Bearer {agent_key['plaintext_key']}"},
    )
    assert not _is_csrf_refusal(agent_resp) and agent_resp.status_code != 401

    # scoped CI key
    ci_headers = await scoped_ci_key('["default"]', name="csrf-ci")
    ci_resp = await client.post(
        "/api/tasks/1/ci-run-report", json={}, headers=ci_headers
    )
    assert not _is_csrf_refusal(ci_resp) and ci_resp.status_code != 401

    # chat-pair: start with a human Bearer, redeem (exempt), act with the session
    start = await client.post("/api/auth/chat-pair/start", headers=world.human_bearer)
    assert start.status_code == 200, start.text
    redeem = await client.post(
        "/api/auth/chat-pair/redeem", json={"code": start.json()["code"]}
    )
    assert redeem.status_code == 200, redeem.text
    pair_resp = await client.post(
        "/api/tasks",
        json={"title": "from chat"},
        headers={"Authorization": f"Bearer {redeem.json()['token']}"},
    )
    assert pair_resp.status_code in (200, 201), pair_resp.text

    # An invalid or empty Bearer next to a valid cookie and a valid token: 401.
    token = await _page_token(world, world.session_a)
    cookie_headers = {**_cookie(world.session_a), **world.own, "X-CSRF-Token": token}
    control = await client.post("/__csrf_echo", headers=cookie_headers)
    assert control.status_code == 200, control.text
    for bad in ("Bearer not-a-real-token", "Bearer", "Bearer "):
        resp = await client.post(
            "/api/tasks",
            json={"title": "must not exist"},
            headers={**cookie_headers, "Authorization": bad},
        )
        assert resp.status_code == 401, (bad, resp.status_code, resp.text)
    for method in ("PUT", "PATCH", "DELETE"):
        resp = await client.request(
            method,
            "/__csrf_echo",
            headers={**cookie_headers, "Authorization": "Bearer not-a-real-token"},
        )
        assert resp.status_code == 401, (method, resp.status_code, resp.text)
    titles = await db.execute("SELECT title FROM tasks WHERE title = 'must not exist'")
    assert await titles.fetchall() == []

    # MCP: a cookie never opens it; a valid Bearer does.
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
    mcp_headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    by_cookie = await client.post(
        "/mcp", headers={**mcp_headers, **cookie_headers}, json=initialize
    )
    assert by_cookie.status_code in (401, 403), by_cookie.text
    from hub.app import _mcp_streamable_app

    async with _mcp_streamable_app.router.lifespan_context(_mcp_streamable_app):
        by_bearer = await client.post(
            "/mcp",
            headers={**mcp_headers, "Authorization": f"Bearer {ENV_TOKEN}"},
            json=initialize,
        )
        assert by_bearer.status_code == 200, by_bearer.text
        # A cookie GET must be refused at once; if it were let in it would
        # open an event stream that never ends.
        cookie_get = await asyncio.wait_for(
            client.get(
                "/mcp",
                headers={"Accept": "text/event-stream", **_cookie(world.session_a)},
            ),
            timeout=10,
        )
        assert cookie_get.status_code in (401, 403)


# ---------------------------------------------------------------------------
# AC-3: the token belongs to its session and lives as long as it does
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_token_is_bound_to_the_session_and_lives_with_it(world, monkeypatch):
    _set_mode(monkeypatch, "require")
    client = world.client
    token_a = await _page_token(world, world.session_a)
    token_b = await _page_token(world, world.session_b)

    mine = {**_cookie(world.session_a), **world.own}
    foreign = await client.post(
        "/__csrf_echo", headers={**mine, "X-CSRF-Token": token_b}
    )
    assert foreign.status_code == 403 and "csrf_failed" in foreign.text
    ok = await client.post("/__csrf_echo", headers={**mine, "X-CSRF-Token": token_a})
    assert ok.status_code == 200, ok.text

    # Older than the 10 minutes of the cookie the old scheme used: still valid.
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + 3600)
    later = await client.post("/__csrf_echo", headers={**mine, "X-CSRF-Token": token_a})
    assert later.status_code == 200, later.text
    monkeypatch.setattr(time, "time", real_time)

    # Logout is protected too, and ends the token's life with the session.
    refused = await client.post("/logout", headers=mine)
    assert refused.status_code == 403 and "csrf_failed" in refused.text
    out = await client.post(
        "/logout", headers={**mine, "X-CSRF-Token": token_a}, follow_redirects=False
    )
    assert out.status_code == 303, out.text
    after = await client.post("/__csrf_echo", headers={**mine, "X-CSRF-Token": token_a})
    assert after.status_code == 401, after.text
    other = await client.post(
        "/__csrf_echo",
        headers={**_cookie(world.session_b), **world.own, "X-CSRF-Token": token_b},
    )
    assert other.status_code == 200, "logout must not end the other session"


# ---------------------------------------------------------------------------
# AC-4: bodies
# ---------------------------------------------------------------------------


async def _chunks(count: int, size: int, seen: list[int]) -> AsyncIterator[bytes]:
    """``count`` fields of ``size`` bytes: each under the parser's own field limit."""
    for i in range(count):
        seen.append(i)
        yield f"f{i}=".encode() + b"x" * size + b"&"


@pytest.mark.asyncio
async def test_form_body_is_replayed_unchanged_to_the_handler(world, monkeypatch):
    client = world.client
    token = await _page_token(world, world.session_a)
    base = {**_cookie(world.session_a), **world.own}
    multipart_files = {"upload": ("note.txt", b"line one\nline two", "text/plain")}
    variants = {
        "urlencoded": dict(
            data={"title": "t", "csrf_token": token, "tag": ["x", "y", "x"]}
        ),
        "repeated": dict(
            content=f"tag=1&csrf_token={token}&tag=2&tag=3&empty=",
            headers={"content-type": "application/x-www-form-urlencoded"},
        ),
        "multipart": dict(
            data={"title": "ü", "csrf_token": token, "tag": ["p", "q"]},
            files=multipart_files,
        ),
        "json": dict(json={"a": [1, 2], "b": "ü"}, headers={"X-CSRF-Token": token}),
        "empty": dict(headers={"X-CSRF-Token": token}),
    }
    for name, kwargs in variants.items():
        kwargs = dict(kwargs)
        extra = kwargs.pop("headers", {})
        _set_mode(monkeypatch, "off")
        baseline = await client.post(
            "/__csrf_echo", headers={**base, **extra}, **kwargs
        )
        assert baseline.status_code == 200, (name, baseline.text)
        _set_mode(monkeypatch, "require")
        guarded = await client.post("/__csrf_echo", headers={**base, **extra}, **kwargs)
        assert guarded.status_code == 200, (name, guarded.text)
        got, want = guarded.json(), baseline.json()
        # multipart boundaries differ per request; compare the parsed values
        got.pop("ctype"), want.pop("ctype")
        assert got == want, name
    assert baseline.json().get("body", "") == ""

    # Over the limit and no token in a header: refused without reading it all.
    chunk = 64 * 1024
    count = 40
    seen: list[int] = []
    _set_mode(monkeypatch, "require")
    big = await client.post(
        "/__csrf_echo",
        headers={**base, "content-type": "application/x-www-form-urlencoded"},
        content=_chunks(count, chunk, seen),
    )
    assert big.status_code == 403 and "csrf_failed" in big.text
    assert len(seen) < count, "the whole body was read before refusing"

    # A body that cannot carry a form field is not read at all.
    seen.clear()
    unread = await client.post(
        "/__csrf_echo",
        headers={**base, "content-type": "application/json"},
        content=_chunks(count, chunk, seen),
    )
    assert unread.status_code == 403 and "csrf_failed" in unread.text
    assert seen == [], "a JSON body was read to look for a form field"

    # In warn mode the same request goes through and the handler still gets
    # every byte: what was buffered plus what had not been read yet.
    _set_mode(monkeypatch, "warn")
    seen.clear()
    through = await client.post(
        "/__csrf_echo",
        headers={**base, "content-type": "application/x-www-form-urlencoded"},
        content=_chunks(count, chunk, seen),
    )
    assert through.status_code == 200, through.text
    sizes = through.json()["form_sizes"]
    assert [sizes[f"f{i}"] for i in range(count)] == [[chunk]] * count


# ---------------------------------------------------------------------------
# AC-5: Origin
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_foreign_origin_is_refused_even_with_a_valid_token(world, monkeypatch):
    _set_mode(monkeypatch, "require")
    token = await _page_token(world, world.session_a)
    base = {**_cookie(world.session_a), "X-CSRF-Token": token}

    async def post(**extra: str):
        return await world.client.post("/__csrf_echo", headers={**base, **extra})

    for origin in (
        HUB_URL,
        HUB_URL + ":443",
        HUB_URL.upper().replace("HTTPS", "https"),
    ):
        resp = await post(Origin=origin)
        assert resp.status_code == 200, (origin, resp.text)
    for origin in (
        "https://evil.example",
        "https://hub.example.test.evil.example",
        "http://hub.example.test",
        "http://hub.example.test:443",
        "https://hub.example.test:8443",
        "null",
        "not a url",
        "",
    ):
        resp = await post(Origin=origin)
        assert resp.status_code == 403 and "csrf_failed" in resp.text, origin
    # No Origin: the Referer decides.
    own_ref = await post(Referer=HUB_URL + "/tasks?x=1")
    assert own_ref.status_code == 200, own_ref.text
    foreign_ref = await post(Referer="https://evil.example/page")
    assert foreign_ref.status_code == 403 and "csrf_failed" in foreign_ref.text


# ---------------------------------------------------------------------------
# AC-6: modes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mode_off_warn_require(world, monkeypatch):
    client = world.client
    assert config.CSRF_MODE == "warn", "the default must be warn"
    headers = {**_cookie(world.session_a), **world.own}

    _set_mode(monkeypatch, "off")
    off = await client.post("/__csrf_echo", headers=headers)
    assert off.status_code == 200
    assert await _count_events(world.db) == 0

    _set_mode(monkeypatch, "warn")
    warn = await client.post("/__csrf_echo", headers=headers)
    assert warn.status_code == 200
    assert await _count_events(world.db) == 1
    cur = await world.db.execute(
        "SELECT payload FROM events WHERE kind = 'csrf_would_reject'"
    )
    payload = json.loads((await cur.fetchone())[0])
    assert payload["method"] == "POST" and payload["path"] == "/__csrf_echo"
    assert payload["reason"]
    health = (await client.get("/health")).json()
    assert health["csrf_mode"] == "warn"
    assert health["csrf_would_reject_24h"] == 1

    _set_mode(monkeypatch, "require")
    req = await client.post("/__csrf_echo", headers=headers)
    assert req.status_code == 403 and "csrf_failed" in req.text
    assert await _count_events(world.db) == 1, "require refuses, it does not warn"

    # A valid request in warn mode leaves no event.
    token = await _page_token(world, world.session_a)
    _set_mode(monkeypatch, "warn")
    good = await client.post("/__csrf_echo", headers={**headers, "X-CSRF-Token": token})
    assert good.status_code == 200
    assert await _count_events(world.db) == 1


# ---------------------------------------------------------------------------
# Refusal shapes, token delivery, secret
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refusal_shape_follows_the_caller(world, monkeypatch):
    _set_mode(monkeypatch, "require")
    headers = {**_cookie(world.session_a), **world.own}

    api = await world.client.post("/api/__csrf_echo", headers=headers)
    assert api.status_code == 403
    assert api.headers["content-type"].startswith("application/json")
    assert "csrf_failed" in json.dumps(api.json())

    web = await world.client.post(
        "/__csrf_echo", headers={**headers, "Accept": "text/html"}
    )
    assert web.status_code == 403
    assert web.headers["content-type"].startswith("text/html")
    assert "обновите" in web.text.lower()

    htmx = await world.client.post(
        "/__csrf_echo", headers={**headers, "HX-Request": "true"}
    )
    assert htmx.status_code == 403
    assert htmx.headers.get("HX-Retarget") and htmx.headers.get("HX-Reswap")
    assert "обновите" in htmx.text.lower()


@pytest.mark.asyncio
async def test_pages_carry_the_session_token_for_forms_and_htmx(world):
    resp = await world.client.get(
        "/tasks", headers={**_cookie(world.session_a), "Accept": "text/html"}
    )
    token = META.search(resp.text).group(1)
    assert f'"X-CSRF-Token": "{token}"' in resp.text, "hx-headers on <body>"


def test_secret_comes_from_env_else_a_private_file(tmp_path, monkeypatch):
    from hub import csrf

    monkeypatch.setattr(config, "CSRF_SECRET", "", raising=False)
    monkeypatch.setattr(config, "HUB_DB_PATH", tmp_path / "hub.db")
    csrf.reset_secret_cache()
    first = csrf.secret_state()
    assert first.source == "file"
    path = tmp_path / "csrf_secret"
    assert path.exists() and (path.stat().st_mode & 0o777) == 0o600
    csrf.reset_secret_cache()
    assert csrf.secret_state().value == first.value, "the file is reused"

    monkeypatch.setattr(config, "CSRF_SECRET", "from-env-value", raising=False)
    csrf.reset_secret_cache()
    assert csrf.secret_state().source == "env"
    csrf.reset_secret_cache()


@pytest.mark.asyncio
async def test_health_warns_when_the_secret_is_not_from_env(client, monkeypatch):
    from hub import csrf

    monkeypatch.setattr(config, "CSRF_SECRET", "", raising=False)
    csrf.reset_secret_cache()
    health = (await client.get("/health")).json()
    assert health["csrf_key_source"] in {"file", "ephemeral"}
    assert health["csrf_warning"]
    monkeypatch.setattr(config, "CSRF_SECRET", "from-env-value", raising=False)
    csrf.reset_secret_cache()
    health = (await client.get("/health")).json()
    assert health["csrf_key_source"] == "env"
    assert not health["csrf_warning"]
    csrf.reset_secret_cache()


@pytest.mark.asyncio
async def test_handlers_that_check_the_token_themselves_accept_the_session_token(
    world, monkeypatch
):
    """``_human_door`` and ``_check_web_csrf`` read the transport and the token.

    Mode ``off`` switches the middleware away, so only the handler decides.
    """
    _set_mode(monkeypatch, "off")
    client = world.client
    token = await _page_token(world, world.session_a)
    cookie = _cookie(world.session_a)

    start = "/api/auth/chat-pair/start"
    assert (await client.post(start, headers=cookie)).status_code == 403
    wrong = await client.post(start, headers={**cookie, "X-CSRF-Token": "nope"})
    assert wrong.status_code == 403
    ok = await client.post(start, headers={**cookie, "X-CSRF-Token": token})
    assert ok.status_code == 200, ok.text
    both = {
        "Cookie": f"{config.HUB_COOKIE_NAME}={world.session_a}; {brand.CSRF_COOKIE_NAME}=legacy-value"
    }
    legacy = await client.post(start, headers={**both, "X-CSRF-Token": "legacy-value"})
    assert legacy.status_code == 200, "the double-submit value stays valid until CSRF B"

    form = "/chat-pair/web-start"
    stale = await client.post(form, headers=cookie, data={"csrf_token": "nope"})
    assert stale.status_code == 403
    fresh = await client.post(form, headers=cookie, data={"csrf_token": token})
    assert fresh.status_code == 200, fresh.text

    # A Bearer caller is never asked.
    bearer = await client.post(start, headers=world.human_bearer)
    assert bearer.status_code == 200, bearer.text
