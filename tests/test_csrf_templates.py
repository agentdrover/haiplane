"""#1665 CSRF B: every form and HTMX action of the web UI carries the token.

#1664 made the check; this proves the pages meet it. Two views of the same
rule:

* static: the template sources. A new ``<form method="post">`` without
  ``{{ csrf_field() }}`` fails here, before any page is rendered;
* dynamic: pages rendered for a browser session, forms parsed from the HTML
  and sent the way the browser would send them, with ``CSRF_MODE=require``.

Two clients: ``browser`` takes the token from the page by itself (a person with
a tab open); ``stranger`` never sets one (a forged cross-site request).
Handlers are replaced by a spy that answers 204, as in ``test_csrf.py``:
"reached" means the middleware let the request through.
"""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from starlette.responses import Response
from starlette.routing import Mount

from hub import brand, config
from hub.config import TokenIdentity
from hub.models import TaskStatus
from hub.services import admin as admin_svc

HUB_URL = "https://hub.example.test"
TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "hub" / "templates"
META = re.compile(r'<meta name="csrf-token" content="([^"]+)"')
FORM_BLOCK = re.compile(r"<form\b([^>]*)>(.*?)</form>", re.S | re.I)
POSTING = re.compile(r'method="post"|hx-post', re.I)
# The login form holds the pre-session token its handler checks itself.
OWN_TOKEN_FIELD = {"login.html"}

PAGES = [
    "/",
    "/tasks",
    "/tasks/list",
    "/projects",
    "/skills",
    "/review-queue",
    "/findings",
    "/chat-pair",
    "/admin/users",
    "/admin/agents",
    "/admin/keys",
    "/partials/inbox",
    "/partials/kanban",
    "/partials/epics",
]


# ---------------------------------------------------------------------------
# Static: template sources
# ---------------------------------------------------------------------------


def _sources() -> dict[str, str]:
    return {
        str(p.relative_to(TEMPLATES_DIR)): p.read_text()
        for p in sorted(TEMPLATES_DIR.rglob("*.html"))
    }


def static_template_problems(sources: dict[str, str]) -> list[str]:
    """What is wrong with the CSRF wiring of the given templates, as sentences."""
    problems: list[str] = []
    for name, text in sources.items():
        for match in FORM_BLOCK.finditer(text):
            attrs, body = match.group(1), match.group(2)
            if not POSTING.search(attrs):
                continue
            ok = "csrf_field()" in body
            if Path(name).name in OWN_TOKEN_FIELD:
                ok = ok or 'name="csrf_token"' in body
            if not ok:
                line = text.count("\n", 0, match.start()) + 1
                problems.append(f"{name}:{line}: POST form without csrf_field()")
        if name != "login.html" and re.search(r'<input[^>]*name="csrf_token"', text):
            problems.append(f"{name}: hand-written csrf_token input, use csrf_field()")
        if name != "base.html" and "hx-headers" in text:
            problems.append(f"{name}: hx-headers would override the token header")
        is_page = "{% extends" in text or name in {
            "base.html",
            "login.html",
            "landing.html",
        }
        if (
            not is_page
            and not name.startswith("partials/")
            and re.search(r"hx-post|method=\"post\"", text)
        ):
            problems.append(f"{name}: posting markup outside a page or a partial")
    base = sources["base.html"]
    if not re.search(
        r"<body[^>]*hx-headers='\{\"X-CSRF-Token\": \"\{\{ csrf_token \}\}\"\}'", base
    ):
        problems.append("base.html: <body> must carry hx-headers with X-CSRF-Token")
    for name in ("login.html", "landing.html"):
        if "hx-post" in sources.get(name, ""):
            problems.append(
                f"{name}: standalone page cannot rely on base.html hx-headers"
            )
    return problems


# ---------------------------------------------------------------------------
# Dynamic: parse what the page serves
# ---------------------------------------------------------------------------


class _Parser(HTMLParser):
    """Forms with their fields, hx-post elements, and the body's hx-headers."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[dict] = []
        self.hx_posts: list[dict] = []
        self.body_headers: dict[str, str] | None = None
        self._stack: list[dict] = []

    def handle_starttag(self, tag, attrs):
        a = {k: (v if v is not None else "") for k, v in attrs}
        if tag == "body" and "hx-headers" in a:
            self.body_headers = json.loads(a["hx-headers"])
        if tag == "form":
            form = {"attrs": a, "fields": [], "hx_posts": []}
            self.forms.append(form)
            self._stack.append(form)
        owner = self._stack[-1] if self._stack else None
        if "hx-post" in a:
            item = {"url": a["hx-post"], "tag": tag, "attrs": a, "form": owner}
            self.hx_posts.append(item)
        if (
            owner is not None
            and tag in {"input", "select", "textarea"}
            and a.get("name")
        ):
            kind = a.get("type", "text").lower()
            if kind in {"submit", "button", "file", "checkbox", "radio"}:
                return
            owner["fields"].append((a["name"], a.get("value", "")))

    def handle_endtag(self, tag):
        if tag == "form" and self._stack:
            self._stack.pop()


def _parse(html: str) -> _Parser:
    parser = _Parser()
    parser.feed(html)
    return parser


def rendered_problems(page: str, html: str) -> list[str]:
    """Forms and hx-post elements of one rendered page that miss the token."""
    parsed = _parse(html)
    problems: list[str] = []
    for form in parsed.forms:
        attrs = form["attrs"]
        posts = attrs.get("method", "").lower() == "post" or "hx-post" in attrs
        if not posts:
            continue
        names = [n for n, v in form["fields"] if n == "csrf_token" and v]
        if not names:
            problems.append(
                f"{page}: form {attrs.get('action') or attrs.get('hx-post')} has no token"
            )
    if parsed.hx_posts and not (parsed.body_headers or {}).get("X-CSRF-Token"):
        # A partial is a fragment of a page: the header comes from the page body.
        if "<body" in html:
            problems.append(f"{page}: hx-post without X-CSRF-Token on the body")
    return problems


def _submissions(page_html: str, page_url: str) -> list[dict]:
    """Every POST the page can send, as the browser or htmx would send it."""
    parsed = _parse(page_html)
    sent: list[dict] = []
    for form in parsed.forms:
        attrs = form["attrs"]
        data = dict(form["fields"])
        if attrs.get("method", "").lower() == "post" and "hx-post" not in attrs:
            url = attrs.get("action") or page_url
            sent.append({"url": url, "data": data, "htmx": False})
    for item in parsed.hx_posts:
        data = dict(item["form"]["fields"]) if item["form"] else {}
        sent.append({"url": item["url"], "data": data, "htmx": True})
    return sent


# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------


class BrowserClient:
    """A person's browser: reads the token off the page, sends it like htmx."""

    def __init__(self, client, session: str) -> None:
        self.client = client
        self.cookie = {"Cookie": f"{config.HUB_COOKIE_NAME}={session}"}
        self.token = ""

    async def get(self, url: str):
        resp = await self.client.get(
            url, headers={**self.cookie, "Accept": "text/html"}
        )
        if found := META.search(resp.text):
            self.token = found.group(1)
        return resp

    async def post(self, url: str, data: dict | None = None, *, htmx: bool = False):
        headers = {**self.cookie, "Origin": HUB_URL}
        if htmx:
            headers["HX-Request"] = "true"
            headers["X-CSRF-Token"] = self.token
        return await self.client.post(url, data=data or {}, headers=headers)


class StrangerClient(BrowserClient):
    """A page on another site riding the session cookie: never sets a token."""

    async def post(self, url: str, data: dict | None = None, *, htmx: bool = False):
        headers = {**self.cookie, "Origin": HUB_URL}
        if htmx:
            headers["HX-Request"] = "true"
        form = {k: v for k, v in (data or {}).items() if k != "csrf_token"}
        return await self.client.post(url, data=form, headers=headers)


def _refused(resp) -> bool:
    return resp.status_code == 403 and "csrf_failed" in resp.text


@pytest.fixture
async def web(client, db, monkeypatch):
    monkeypatch.setenv(brand.ENV_PREFIX + "HUB_URL", HUB_URL)
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    monkeypatch.setattr(
        config, "HUB_TOKENS", {"env-tok": TokenIdentity("env", "human")}
    )
    monkeypatch.setattr(config, "CSRF_MODE", "require", raising=False)
    admin = await admin_svc.create_principal(
        db, kind="human", username="boss-csrf", role_slug="admin"
    )
    key = await admin_svc.create_api_key(db, admin["id"], name="k")
    session = await admin_svc.create_browser_session(db, admin["id"])
    await db.commit()
    bearer = {"Authorization": f"Bearer {key['plaintext_key']}"}
    ids: dict[str, int] = {}
    for status in TaskStatus:
        resp = await client.post(
            "/api/tasks", json={"title": f"csrf page {status.value}"}, headers=bearer
        )
        assert resp.status_code in (200, 201), resp.text
        task_id = resp.json()["id"]
        await db.execute(
            "UPDATE tasks SET status = ? WHERE id = ?", (status.value, task_id)
        )
        ids[status.value] = task_id
    archived = ids["completed"]
    await db.execute("UPDATE tasks SET archived = 1 WHERE id = ?", (archived,))
    await db.commit()
    pages = [*PAGES, *(f"/tasks/{i}" for i in ids.values())]
    return SimpleNamespace(
        client=client,
        db=db,
        bearer=bearer,
        pages=pages,
        browser=BrowserClient(client, session),
        stranger=StrangerClient(client, session),
    )


async def _render_all(web) -> dict[str, str]:
    out: dict[str, str] = {}
    for page in web.pages:
        resp = await web.browser.get(page)
        assert resp.status_code == 200, (page, resp.status_code, resp.text[:200])
        out[page] = resp.text
    return out


def _spy_every_route(monkeypatch) -> list[tuple[str, str]]:
    from hub.app import app

    reached: list[tuple[str, str]] = []

    async def spy(scope, receive, send):
        reached.append((scope["method"], scope["path"]))
        await Response(status_code=204)(scope, receive, send)

    def walk(routes):
        for route in routes:
            if isinstance(route, Mount):
                walk(route.routes)
            elif hasattr(route, "app"):
                monkeypatch.setattr(route, "app", spy)

    walk(app.routes)
    return reached


# ---------------------------------------------------------------------------
# AC-1
# ---------------------------------------------------------------------------


async def test_every_form_and_hx_post_carries_the_token(web):
    static = static_template_problems(_sources())
    assert static == []

    pages = await _render_all(web)
    problems: list[str] = []
    forms_seen = 0
    hx_seen = 0
    for page, html in pages.items():
        problems += rendered_problems(page, html)
        parsed = _parse(html)
        forms_seen += len(parsed.forms)
        hx_seen += len(parsed.hx_posts)
    assert problems == []
    assert forms_seen >= 10 and hx_seen >= 10, "the pages must show forms and hx-post"
    token = web.browser.token
    assert token and all(
        (_parse(h).body_headers or {}).get("X-CSRF-Token") == token
        for h in pages.values()
        if "<body" in h and "hx-headers" in h
    )


def test_static_check_catches_a_form_without_the_macro():
    sources = _sources()
    sources["probe.html"] = (
        '{% extends "base.html" %}<form method="post" action="/x"><button>go</button></form>'
    )
    assert any("probe.html" in p for p in static_template_problems(sources))
    sources["probe.html"] = (
        '{% extends "base.html" %}<form method="post" action="/x">{{ csrf_field() }}</form>'
    )
    assert not any("probe.html" in p for p in static_template_problems(sources))
    sources["probe.html"] = "{% extends \"base.html\" %}<div hx-headers='{}'></div>"
    assert any("hx-headers" in p for p in static_template_problems(sources))


def test_rendered_check_catches_a_form_without_the_token():
    html = '<body hx-headers=\'{"X-CSRF-Token": "t"}\'><form method="post" action="/x"></form></body>'
    assert rendered_problems("/p", html)


# ---------------------------------------------------------------------------
# AC-2
# ---------------------------------------------------------------------------


async def test_rendered_forms_pass_the_check_in_require_mode(web, monkeypatch):
    pages = await _render_all(web)
    reached = _spy_every_route(monkeypatch)

    sent = 0
    failures: list[str] = []
    seen: set[tuple[str, str]] = set()
    for page, html in pages.items():
        for item in _submissions(html, page):
            path = urlsplit(item["url"]).path
            resp = await web.browser.post(item["url"], item["data"], htmx=item["htmx"])
            sent += 1
            seen.add(("POST", path))
            if _refused(resp) or (("POST", path)) not in reached:
                failures.append(f"{page} -> {item['url']}: {resp.status_code}")
    assert failures == []
    assert sent >= 25 and len(seen) >= 15, (sent, len(seen))


async def test_the_same_forms_without_a_token_are_refused(web, monkeypatch):
    pages = await _render_all(web)
    reached = _spy_every_route(monkeypatch)
    sent = 0
    leaked: list[str] = []
    for page, html in pages.items():
        for item in _submissions(html, page):
            resp = await web.stranger.post(item["url"], item["data"], htmx=item["htmx"])
            sent += 1
            if not _refused(resp):
                leaked.append(f"{page} -> {item['url']}: {resp.status_code}")
    assert leaked == []
    assert sent >= 25
    assert reached == []


async def test_warn_mode_lets_a_stranger_through_but_writes_the_event(web, monkeypatch):
    monkeypatch.setattr(config, "CSRF_MODE", "warn", raising=False)
    pages = await _render_all(web)
    reached = _spy_every_route(monkeypatch)
    html = pages["/projects"]
    item = next(i for i in _submissions(html, "/projects") if not i["htmx"])
    resp = await web.stranger.post(item["url"], item["data"])
    assert resp.status_code == 204 and reached
    cur = await web.db.execute(
        "SELECT COUNT(*) FROM events WHERE kind = 'csrf_would_reject'"
    )
    assert (await cur.fetchone())[0] >= 1


# ---------------------------------------------------------------------------
# AC-3
# ---------------------------------------------------------------------------


async def test_htmx_refusal_is_visible(web, monkeypatch):
    pages = await _render_all(web)
    reached = _spy_every_route(monkeypatch)
    headers = {
        **web.browser.cookie,
        "Origin": HUB_URL,
        "HX-Request": "true",
        "X-CSRF-Token": "forged",
    }
    resp = await web.client.post("/tasks/1/web-approve", data={}, headers=headers)
    assert resp.status_code == 403
    assert resp.headers["HX-Retarget"] == "body"
    assert resp.headers["HX-Reswap"] == "beforeend"
    assert "Обновите страницу" in resp.text and 'role="alert"' in resp.text
    assert reached == []
    # htmx drops 4xx bodies unless the page says otherwise: the handler is there.
    base = pages["/"]
    assert "htmx:beforeSwap" in base and 'getResponseHeader("HX-Retarget")' in base
