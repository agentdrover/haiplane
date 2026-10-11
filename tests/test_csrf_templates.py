"""#1665 CSRF B: every form and HTMX action of the web UI carries the token.

#1664 made the check; this proves the pages meet it. Three views of the rule:

* static: template sources parsed with an HTML parser (not a regex). Every
  mutating declaration (a form with method post in any spelling, any element
  with hx-post/put/patch/delete, a button with formmethod=post) is listed, and
  a form without ``{{ csrf_field() }}`` fails. Jinja comments are cut first, so
  ``{# csrf_field() #}`` proves nothing;
* rendered: pages for a browser session. The effective ``hx-headers`` of each
  htmx element are computed from its ancestors (a descendant overrides a
  parent, ``hx-disinherit`` stops inheritance) and sent as they are;
* completeness: the static declarations are the denominator. Each is either
  matched by a request that was really sent, or named in ``EXCLUDED`` with a
  reason. The test prints both lists.

``browser`` is a person's tab (reads the token off the page), ``stranger`` is
a page of another site (never sets one). Handlers are replaced by a spy that
answers 204, as in ``test_csrf.py``: "reached" means the middleware let the
request through.
"""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

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
JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.S)
JINJA_EXPR = re.compile(r"\{\{.*?\}\}", re.S)
HX_MUTATING = ("hx-post", "hx-put", "hx-patch", "hx-delete")
# The login form holds the pre-session token its handler checks itself.
OWN_TOKEN_FIELD = {"login.html"}
VOID = frozenset(
    "area base br col embed hr img input link meta source track wbr".split()
)

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

# Declarations the fixture cannot show, by "<template>::<url pattern>" -> why.
EXCLUDED: dict[str, str] = {
    "login.html::form::/login": (
        "pre-session form of an exempt route: its own token is checked by the "
        "handler, see tests/test_csrf.py"
    ),
    "task_detail.html::form::/tasks/{}/web-executor-merge": (
        "offered only on a real base conflict, which the card reads from the "
        "git repository"
    ),
}


# ---------------------------------------------------------------------------
# A small DOM, for the sources and for the rendered pages
# ---------------------------------------------------------------------------


class Node:
    def __init__(self, tag: str, attrs: dict[str, str], parent, line: int) -> None:
        self.tag = tag
        self.attrs = attrs
        self.parent = parent
        self.line = line
        self.children: list[Node] = []
        self.texts: list[str] = []

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()

    def text(self) -> str:
        return "".join(self.texts) + "".join(c.text() for c in self.children)


class _Tree(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#root", {}, None, 0)
        self.cur = self.root

    def _open(self, tag, attrs) -> Node:
        node = Node(
            tag,
            {k.lower(): (v if v is not None else "") for k, v in attrs},
            self.cur,
            self.getpos()[0],
        )
        self.cur.children.append(node)
        return node

    def handle_starttag(self, tag, attrs):
        node = self._open(tag, attrs)
        if tag not in VOID:
            self.cur = node

    def handle_startendtag(self, tag, attrs):
        self._open(tag, attrs)

    def handle_endtag(self, tag):
        node = self.cur
        while node is not None and node.tag != tag:
            node = node.parent
        if node is not None and node.parent is not None:
            self.cur = node.parent

    def handle_data(self, data):
        self.cur.texts.append(data)


def parse_html(html: str) -> Node:
    tree = _Tree()
    tree.feed(html)
    tree.close()
    return tree.root


def without_jinja_comments(text: str) -> str:
    """Comments go, their lines stay: line numbers keep pointing at the file."""
    return JINJA_COMMENT.sub(lambda m: "\n" * m.group(0).count("\n"), text)


def _is_post(value: str) -> bool:
    return value.strip().lower() == "post"


def declarations(root: Node) -> list[dict]:
    """Every place that sends a mutating request, once each."""
    found: list[dict] = []
    for node in root.walk():
        a = node.attrs
        hx = [name for name in HX_MUTATING if name in a]
        form_post = node.tag == "form" and _is_post(a.get("method", ""))
        button_post = node.tag in {"button", "input"} and _is_post(
            a.get("formmethod", "")
        )
        if hx:
            kind, url = hx[0], a[hx[0]]
        elif form_post:
            kind, url = "form", a.get("action", "")
        elif button_post:
            owner = _closest(node, "form")
            kind = "formmethod"
            url = a.get("formaction") or (
                owner.attrs.get("action", "") if owner else ""
            )
        else:
            continue
        found.append({"node": node, "kind": kind, "url": url, "line": node.line})
    return found


def _closest(node: Node, tag: str) -> Node | None:
    cur = node.parent
    while cur is not None and cur.tag != tag:
        cur = cur.parent
    return cur


def _form_needing_field(decl: dict) -> Node | None:
    """The form that must carry a hidden token field for this declaration."""
    node = decl["node"]
    if node.tag == "form":
        return node
    if decl["kind"] == "formmethod":
        return _closest(node, "form")
    return None


# ---------------------------------------------------------------------------
# Static: template sources
# ---------------------------------------------------------------------------


def _sources() -> dict[str, str]:
    return {
        str(p.relative_to(TEMPLATES_DIR)): p.read_text()
        for p in sorted(TEMPLATES_DIR.rglob("*.html"))
    }


def static_declarations(sources: dict[str, str]) -> list[dict]:
    out: list[dict] = []
    for name, text in sources.items():
        for decl in declarations(parse_html(without_jinja_comments(text))):
            out.append({**decl, "file": name})
    return out


def static_template_problems(sources: dict[str, str]) -> list[str]:
    """What is wrong with the CSRF wiring of the given templates, as sentences."""
    problems: list[str] = []
    for name, text in sources.items():
        root = parse_html(without_jinja_comments(text))
        for decl in declarations(root):
            form = _form_needing_field(decl)
            if form is not None:
                body = form.text()
                ok = "csrf_field()" in body
                if Path(name).name in OWN_TOKEN_FIELD:
                    ok = ok or any(
                        n.tag == "input" and n.attrs.get("name") == "csrf_token"
                        for n in form.walk()
                    )
                if not ok:
                    problems.append(
                        f"{name}:{decl['line']}: POST form without csrf_field()"
                    )
            elif decl["kind"] == "formmethod":
                problems.append(
                    f"{name}:{decl['line']}: formmethod=post outside a form"
                )
        for node in root.walk():
            if (
                node.tag == "input"
                and node.attrs.get("name") == "csrf_token"
                and name != "login.html"
            ):
                problems.append(
                    f"{name}:{node.line}: hand-written csrf_token input, use csrf_field()"
                )
            if name != "base.html":
                for attr in ("hx-headers", "hx-disinherit"):
                    if attr in node.attrs:
                        problems.append(
                            f"{name}:{node.line}: {attr} would break the token header"
                        )
        is_page = "{% extends" in text or name in {
            "base.html",
            "login.html",
            "landing.html",
        }
        if not is_page and not name.startswith("partials/") and declarations(root):
            problems.append(f"{name}: posting markup outside a page or a partial")
        if name in {"login.html", "landing.html"} and any(
            d["kind"].startswith("hx-") for d in declarations(root)
        ):
            problems.append(
                f"{name}: standalone page cannot rely on base.html hx-headers"
            )
    body_tag = re.search(
        r"<body\b[^>]*>", without_jinja_comments(sources["base.html"]), re.S
    )
    # The attribute sits inside {% if %}, which no HTML parser reads as markup.
    if not body_tag or not re.search(
        r"""hx-headers='\{"X-CSRF-Token": "\{\{ csrf_token \}\}"\}'""",
        body_tag.group(0),
    ):
        problems.append("base.html: <body> must carry hx-headers with X-CSRF-Token")
    return problems


# ---------------------------------------------------------------------------
# Rendered pages
# ---------------------------------------------------------------------------


def effective_headers(node: Node) -> dict[str, str]:
    """What htmx sends for ``node``: its own hx-headers over its ancestors'.

    ``hx-disinherit`` on an element (``hx-headers`` or ``*``) cuts the chain
    above it; the element's own ``hx-headers`` still count.
    """
    out: dict[str, str] = {}
    cur: Node | None = node
    while cur is not None and cur.tag != "#root":
        raw = cur.attrs.get("hx-headers", "").strip()
        if raw:
            try:
                parsed = json.loads(raw)
            except ValueError:
                parsed = {}
            for key, value in parsed.items():
                out.setdefault(str(key).lower(), str(value))
        cut = cur.attrs.get("hx-disinherit", "").split()
        if "*" in cut or "hx-headers" in cut:
            break
        cur = cur.parent
    return out


def _with_page_body(html: str, body_headers: str) -> str:
    """A fragment lands inside the page body: give it the body's hx-headers."""
    if "<body" in html:
        return html
    return f"<body hx-headers='{body_headers}'>{html}</body>"


def rendered_problems(page: str, html: str, body_headers: str, token: str) -> list[str]:
    """Rendered declarations whose request would miss the page token."""
    root = parse_html(_with_page_body(html, body_headers))
    problems: list[str] = []
    for decl in declarations(root):
        node = decl["node"]
        where = f"{page}:{decl['line']} {decl['kind']} {decl['url']}"
        form = _form_needing_field(decl)
        if form is not None:
            fields = [
                n.attrs.get("value", "")
                for n in form.walk()
                if n.tag == "input" and n.attrs.get("name") == "csrf_token"
            ]
            if token not in fields:
                problems.append(f"{where}: form carries no token")
        else:
            sent = effective_headers(node).get("x-csrf-token", "")
            if sent != token:
                problems.append(f"{where}: htmx header is {sent!r}, not the token")
    return problems


def _controls(scope: Node) -> dict[str, str]:
    """Values a browser would submit for the controls under ``scope``."""
    data: dict[str, str] = {}
    for n in scope.walk():
        name = n.attrs.get("name")
        if not name or "disabled" in n.attrs:
            continue
        if n.tag == "input":
            kind = n.attrs.get("type", "text").lower()
            if kind in {"submit", "button", "file", "image", "reset"}:
                continue
            if kind in {"checkbox", "radio"} and "checked" not in n.attrs:
                continue
            data[name] = n.attrs.get("value", "on" if kind == "checkbox" else "")
        elif n.tag == "select":
            options = [c for c in n.walk() if c.tag == "option"]
            chosen = next((o for o in options if "selected" in o.attrs), None)
            chosen = chosen or (options[0] if options else None)
            data[name] = (
                chosen.attrs.get("value", chosen.text().strip()) if chosen else ""
            )
        elif n.tag == "textarea":
            data[name] = n.text()
    return data


def _by_id(root: Node, ident: str) -> Node | None:
    return next((n for n in root.walk() if n.attrs.get("id") == ident), None)


def requests_of(html: str, body_headers: str, page: str) -> list[dict]:
    """Every request the page can send, with the headers and data it would carry."""
    root = parse_html(_with_page_body(html, body_headers))
    sent: list[dict] = []
    for decl in declarations(root):
        node = decl["node"]
        if decl["kind"] in {"form", "formmethod"}:
            form = _form_needing_field(decl)
            sent.append(
                {
                    "url": decl["url"] or page,
                    "data": _controls(form) if form else {},
                    "headers": {},
                    "htmx": False,
                    "node": node,
                    "kind": decl["kind"],
                }
            )
            continue
        data: dict[str, str] = {}
        if form := _closest(node, "form"):
            data.update(_controls(form))
        for ref in node.attrs.get("hx-include", "").split(","):
            ref = ref.strip()
            if ref.startswith("#") and (target := _by_id(root, ref[1:])):
                data.update(_controls(target))
        try:
            vals = json.loads(node.attrs.get("hx-vals", "") or "{}")
        except ValueError:
            vals = {}
        data.update({str(k): str(v) for k, v in vals.items()})
        sent.append(
            {
                "url": decl["url"],
                "data": data,
                "headers": effective_headers(node),
                "htmx": True,
                "node": node,
                "kind": decl["kind"],
            }
        )
    return sent


# ---------------------------------------------------------------------------
# Completeness: static declarations against what was sent
# ---------------------------------------------------------------------------


def _pattern(value: str) -> re.Pattern[str] | None:
    """A regex for an attribute written with Jinja, or None when it is dynamic."""
    if "{%" in value:
        return None
    parts = JINJA_EXPR.split(value)
    return re.compile(".*?".join(re.escape(p) for p in parts), re.S)


def declaration_key(file: str, kind: str, url: str) -> str:
    return f"{file}::{kind}::{JINJA_EXPR.sub('{}', url)}"


def static_matches(decl: dict, node: Node) -> bool:
    """Is the rendered ``node`` an instance of the template declaration ``decl``?"""
    static = decl["node"]
    if static.tag != node.tag:
        return False
    for name in (
        "action",
        "method",
        "class",
        "id",
        "hx-target",
        "hx-include",
        "hx-vals",
        *HX_MUTATING,
    ):
        if name not in static.attrs:
            continue
        pattern = _pattern(static.attrs[name])
        if pattern is None:
            continue
        if name == "method":
            if not _is_post(node.attrs.get(name, "")):
                return False
            continue
        if not pattern.fullmatch(node.attrs.get(name, "")):
            return False
    return True


def coverage(
    static: list[dict], sent: list[dict]
) -> tuple[list[str], list[str], list[str]]:
    """(covered, excluded, missing) declarations, as readable lines."""
    covered: list[str] = []
    excluded: list[str] = []
    missing: list[str] = []
    for decl in static:
        key = declaration_key(decl["file"], decl["kind"], decl["url"])
        line = f"{decl['file']}:{decl['line']} {decl['kind']} {decl['url']}"
        if any(
            static_matches(decl, node)
            for s in sent
            for node in s.get("twins", [s["node"]])
        ):
            covered.append(line)
        elif key in EXCLUDED:
            excluded.append(f"{line}  [{EXCLUDED[key]}]")
        else:
            missing.append(f"{line}  (key {key})")
    return covered, excluded, missing


# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------


class BrowserClient:
    """A person's tab: reads the token off the page, sends what the page says."""

    def __init__(self, client, session: str) -> None:
        self.client = client
        self.cookie = {"Cookie": f"{config.HUB_COOKIE_NAME}={session}"}
        self.token = ""
        self.body_headers = ""

    async def get(self, url: str):
        resp = await self.client.get(
            url, headers={**self.cookie, "Accept": "text/html"}
        )
        if found := META.search(resp.text):
            self.token = found.group(1)
        if "<body" in resp.text:
            body = next(
                (n for n in parse_html(resp.text).walk() if n.tag == "body"), None
            )
            if body is not None and body.attrs.get("hx-headers"):
                self.body_headers = body.attrs["hx-headers"]
        return resp

    async def send(self, req: dict):
        """Exactly the headers and fields the page's markup produced."""
        headers = {**self.cookie, "Origin": HUB_URL, **req["headers"]}
        if req["htmx"]:
            headers["HX-Request"] = "true"
        return await self.client.post(req["url"], data=req["data"], headers=headers)

    async def act(self, url: str, data: dict | None = None):
        """A scripted action that sets the token itself (not a page's markup)."""
        headers = {**self.cookie, "Origin": HUB_URL, "X-CSRF-Token": self.token}
        return await self.client.post(url, data=data or {}, headers=headers)


class StrangerClient(BrowserClient):
    """A page on another site riding the session cookie: never sets a token."""

    async def send(self, req: dict):
        bare = {
            **req,
            "data": {k: v for k, v in req["data"].items() if k != "csrf_token"},
            "headers": {
                k: v for k, v in req["headers"].items() if k.lower() != "x-csrf-token"
            },
        }
        return await super().send(bare)

    async def act(self, url: str, data: dict | None = None):
        headers = {**self.cookie, "Origin": HUB_URL}
        return await self.client.post(url, data=data or {}, headers=headers)


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
    extra = await _cheap_state(client, db, bearer, ids)
    pages = [*PAGES, *extra, *(f"/tasks/{i}" for i in ids.values())]
    return SimpleNamespace(
        client=client,
        db=db,
        bearer=bearer,
        pages=pages,
        browser=BrowserClient(client, session),
        stranger=StrangerClient(client, session),
    )


async def _cheap_state(client, db, bearer, ids: dict[str, int]) -> list[str]:
    """What it costs little to show: projects, a skill, principals, findings, a run."""
    import json as _json

    from hub import repository as repo_module

    pages: list[str] = []
    made = await client.post(
        "/api/projects",
        json={
            "slug": "csrf-live",
            "name": "Csrf live",
            "repo": "owner/csrf-live",
            "workspace_path": "/tmp/csrf-live",
        },
        headers=bearer,
    )
    assert made.status_code in (200, 201), made.text
    await db.execute(
        "UPDATE projects SET status = 'pending', gate_policy = ? WHERE id = ?",
        (_json.dumps({"executor_launch": "manual"}), made.json()["id"]),
    )
    # Tasks outside an epic fall back to the project 'default'.
    base = await client.post(
        "/api/projects", json={"slug": "default", "name": "Default"}, headers=bearer
    )
    assert base.status_code in (200, 201), base.text
    await db.execute(
        "UPDATE projects SET gate_policy = ? WHERE id = ?",
        (_json.dumps({"executor_launch": "manual"}), base.json()["id"]),
    )
    for name in ("csrf-disabled", "csrf-locked"):
        for kind in ("human", "agent"):
            row = await admin_svc.create_principal(
                db, kind=kind, username=f"{name}-{kind}", role_slug=None
            )
            await db.execute(
                "UPDATE principals SET status = ? WHERE id = ?",
                ("disabled" if name == "csrf-disabled" else "locked", row["id"]),
            )
    for version in (1, 2):
        sk = await client.post(
            "/api/skills",
            json={"name": "csrf-skill", "content": f"v{version}", "kind": "prompt"},
            headers=bearer,
        )
        assert sk.status_code == 200, sk.text
    pages.append("/skills/csrf-skill")
    await db.execute(
        "UPDATE skills SET status = CASE version WHEN 1 THEN 'active' ELSE 'draft' END "
        "WHERE name = 'csrf-skill'"
    )

    async def extra_task(title: str, status: str) -> int:
        resp = await client.post("/api/tasks", json={"title": title}, headers=bearer)
        task_id = resp.json()["id"]
        await db.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))
        pages.append(f"/tasks/{task_id}")
        return task_id

    finding = {
        "title": "probe finding",
        "severity": "high",
        "category": "logic",
        "file": "hub/probe.py",
        "line": 1,
        "start_line": 1,
        "end_line": 1,
        "locator": "lines",
        "detail": "",
    }
    # A review with a finding to judge, a run to stop, and a repair to offer.
    for task_id in (ids["review"], ids["open"]):
        await db.execute(
            "UPDATE tasks SET submission_generation = 1 WHERE id = ?",
            (task_id,),
        )
        await repo_module.insert_machine_review(
            db,
            task_id=task_id,
            submission_generation=1,
            harness_skill="deep-review",
            model="probe",
            raw_count=1,
            findings_confirmed=_json.dumps([finding]),
            unresolved="[]",
            submitted_by="probe",
        )
    for task_id, outcome in ((ids["running"], "running"), (ids["open"], "failed")):
        await db.execute(
            "INSERT INTO executor_runs (task_id, agent_id, run_id, outcome) "
            "VALUES (?, 'agent-x', 'run-x', ?)",
            (task_id, outcome),
        )
    # A review nobody has asked a machine about yet.
    await extra_task("csrf review without a machine report", "review")
    delivered = await extra_task("csrf accepted, not delivered", "completed")
    await db.execute(
        "UPDATE tasks SET branch = 'task-1/probe', pr_number = 7 WHERE id = ?",
        (delivered,),
    )
    await db.execute(
        "INSERT OR REPLACE INTO delivery_discrepancies (task_id, pr_number, state) "
        "VALUES (?, 7, 'pr_open')",
        (delivered,),
    )
    await db.commit()
    return pages


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


async def _requests(web, pages: dict[str, str]) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for page, html in pages.items():
        for req in requests_of(html, web.browser.body_headers, page):
            key = json.dumps(
                [req["url"], req["data"], req["headers"], req["htmx"]], sort_keys=True
            )
            if key not in seen:
                seen.add(key)
                req["twins"] = [req["node"]]
                out.append(req)
            else:
                # Same request from another place: it stays a witness for it.
                next(
                    r
                    for r in out
                    if json.dumps(
                        [r["url"], r["data"], r["headers"], r["htmx"]], sort_keys=True
                    )
                    == key
                )["twins"].append(req["node"])
    return out


# ---------------------------------------------------------------------------
# AC-1
# ---------------------------------------------------------------------------


async def test_every_form_and_hx_post_carries_the_token(web):
    static = static_template_problems(_sources())
    assert static == []

    pages = await _render_all(web)
    token, body = web.browser.token, web.browser.body_headers
    assert token and token in body
    problems: list[str] = []
    seen = 0
    for page, html in pages.items():
        problems += rendered_problems(page, html, body, token)
        seen += len(declarations(parse_html(_with_page_body(html, body))))
    assert problems == []
    assert seen >= 20, "the pages must show forms and htmx actions"


STATIC_BAD = {
    "method POST upper": '<form method="POST" action="/x"><button>go</button></form>',
    "method POST unquoted": "<form method=POST action=/x><button>go</button></form>",
    "method with spaces": '<form method = "post" action="/x"><button>go</button></form>',
    "method single quotes": "<form method='Post' action='/x'></form>",
    "formmethod on a button": (
        '<form method="get" action="/x"><button formmethod="post" formaction="/y">'
        "go</button></form>"
    ),
    "hx-put on a form": '<form hx-put="/x"><button>go</button></form>',
    "hx-delete on a form": '<form hx-delete="/x"><button>go</button></form>',
    "hx-patch on a form": '<form hx-patch="/x"></form>',
    "macro inside a jinja comment": (
        '<form method="post" action="/x">{# {{ csrf_field() }} #}</form>'
    ),
    "macro in a multi-line comment": (
        '<form method="post" action="/x">{#\n {{ csrf_field() }}\n #}</form>'
    ),
    "hx-headers on a descendant": (
        '<button hx-delete="/x" hx-headers=\'{"X-CSRF-Token": ""}\'>go</button>'
    ),
    "hx-disinherit": '<div hx-disinherit="hx-headers"><button hx-patch="/x"></button></div>',
}
STATIC_GOOD = {
    "macro": '<form method="post" action="/x">{{ csrf_field() }}</form>',
    "macro after a comment": (
        '<form method="POST" action="/x">{# note #}{{ csrf_field() }}</form>'
    ),
    "get form": '<form method="get" action="/x"></form>',
    "htmx button": '<button hx-delete="/x">go</button>',
}


@pytest.mark.parametrize("label", sorted(STATIC_BAD))
def test_static_check_catches(label):
    sources = _sources()
    sources["probe.html"] = '{% extends "base.html" %}' + STATIC_BAD[label]
    assert any("probe.html" in p for p in static_template_problems(sources)), label


@pytest.mark.parametrize("label", sorted(STATIC_GOOD))
def test_static_check_lets_a_proper_declaration_pass(label):
    sources = _sources()
    sources["probe.html"] = '{% extends "base.html" %}' + STATIC_GOOD[label]
    assert not any("probe.html" in p for p in static_template_problems(sources))


RENDERED_BODY = '{"X-CSRF-Token": "tok"}'


@pytest.mark.parametrize(
    "markup",
    [
        '<button hx-post="/x" hx-headers=\'{"X-CSRF-Token": ""}\'>go</button>',
        '<div hx-headers=\'{"X-CSRF-Token": ""}\'><button hx-delete="/x">go</button></div>',
        '<div hx-disinherit="hx-headers"><button hx-patch="/x">go</button></div>',
        '<div hx-disinherit="*"><button hx-put="/x">go</button></div>',
        '<button hx-delete="/x" hx-headers=\'{"x-csrf-token": "stale"}\'>go</button>',
        '<form method="post" action="/x"><input type="hidden" name="csrf_token" value=""></form>',
        '<form method="post" action="/x"></form>',
    ],
)
def test_rendered_check_catches_a_broken_declaration(markup):
    page = f"<body hx-headers='{RENDERED_BODY}'>{markup}</body>"
    assert rendered_problems("/p", page, RENDERED_BODY, "tok")


def test_rendered_check_accepts_inheritance_and_a_matching_override():
    page = (
        f"<body hx-headers='{RENDERED_BODY}'>"
        '<button hx-post="/a">a</button>'
        '<button hx-delete="/b" hx-headers=\'{"X-CSRF-Token": "tok"}\'>b</button>'
        '<form method="post" action="/c"><input type="hidden" name="csrf_token" value="tok"></form>'
        "</body>"
    )
    assert rendered_problems("/p", page, RENDERED_BODY, "tok") == []
    fragment = '<button hx-patch="/d">d</button>'
    assert rendered_problems("/p", fragment, RENDERED_BODY, "tok") == []


# ---------------------------------------------------------------------------
# AC-2
# ---------------------------------------------------------------------------


async def test_rendered_forms_pass_the_check_in_require_mode(web, monkeypatch):
    pages = await _render_all(web)
    requests = await _requests(web, pages)
    reached = _spy_every_route(monkeypatch)

    failures: list[str] = []
    for req in requests:
        before = len(reached)
        resp = await web.browser.send(req)
        if _refused(resp) or len(reached) == before:
            failures.append(f"{req['kind']} {req['url']}: {resp.status_code}")
    assert failures == []

    static = static_declarations(_sources())
    covered, excluded, missing = coverage(static, requests)
    print(f"\nCOVERED {len(covered)} of {len(static)} declarations")
    print("\n".join(f"  ok   {line}" for line in covered))
    print(f"EXCLUDED {len(excluded)}")
    print("\n".join(f"  skip {line}" for line in excluded))
    assert missing == [], "declarations no page of the fixture showed:\n" + "\n".join(
        missing
    )


async def test_the_same_forms_without_a_token_are_refused(web, monkeypatch):
    pages = await _render_all(web)
    requests = await _requests(web, pages)
    reached = _spy_every_route(monkeypatch)
    leaked = [
        f"{req['kind']} {req['url']}: {resp.status_code}"
        for req in requests
        if not _refused(resp := await web.stranger.send(req))
    ]
    assert leaked == []
    assert len(requests) >= 25
    assert reached == []


async def test_the_token_client_acts_and_the_stranger_does_not(web, monkeypatch):
    await _render_all(web)
    reached = _spy_every_route(monkeypatch)
    ok = await web.browser.act("/tasks/1/web-approve")
    assert ok.status_code == 204 and reached
    reached.clear()
    refused = await web.stranger.act("/tasks/1/web-approve")
    assert _refused(refused) and reached == []


async def test_warn_mode_lets_a_stranger_through_but_writes_the_event(web, monkeypatch):
    monkeypatch.setattr(config, "CSRF_MODE", "warn", raising=False)
    pages = await _render_all(web)
    requests = await _requests(web, pages)
    reached = _spy_every_route(monkeypatch)
    req = next(r for r in requests if not r["htmx"])
    resp = await web.stranger.send(req)
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
