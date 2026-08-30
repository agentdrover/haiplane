"""Принципал steward: закрытый allowlist из двух операций (#1021).

Спека: docs/issues/steward-agent.md §8. CHAT_PAIR_PERMS — образец ПРИЁМА
(deny-by-default allowlist), не набор прав: create/refine/update туда не входят.

AC-1: любой маршрут кроме двух разрешённых — 403, перебором, не выборкой.
AC-2: is_human ложь; ветки «не агент → человек» его человеком не считают.
AC-3: новый маршрут недоступен, пока его явно не внесли в allowlist.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute

from hub import config
from hub.app import _reject_agent_authored_source
from hub.auth import require_human_or_admin
from hub.config import TokenIdentity
from hub.models import TaskSource
from hub.services import admin as admin_svc
from hub.web import _require_human_web

STEWARD_TOKEN = "steward-env-token"  # pragma: allowlist secret

# The two operations named in the task. Paths are the contract this test pins;
# the implementation must match them, not the other way around.
STEWARD_OPS = (
    ("GET", "/api/tasks/{task_id}/steward-evidence"),
    ("POST", "/api/tasks/{task_id}/steward-judgement"),
)

FORBIDDEN_PERMS = frozenset(
    {
        "tasks.human_gate",
        "tasks.decision",
        "tasks.agent_report",
        "tasks.create",
        "tasks.refine",
        "tasks.update",
    }
)

_PUBLIC_EXACT = frozenset(
    {
        "/login",
        "/logout",
        "/health",
        "/healthz",
        "/favicon.ico",
        "/robots.txt",
        "/api/admin/bootstrap",
        "/api/auth/chat-pair/redeem",
    }
)

_FILL_PATH = re.compile(r"\{[^}/]+\}")


def _fill(path: str) -> str:
    return _FILL_PATH.sub("1", path)


@pytest.fixture
async def hub(client, db, monkeypatch):
    """Хаб с включённым auth и принципалом steward (роль может ещё не существовать)."""
    monkeypatch.setattr(
        config,
        "HUB_TOKENS",
        {STEWARD_TOKEN: TokenIdentity("steward-env", "steward")},
    )
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)

    human = await admin_svc.create_principal(
        db, kind="human", username="alice", role_slug="operator"
    )
    human_key = await admin_svc.create_api_key(db, human["id"], name="laptop")
    steward = await admin_svc.create_principal(
        db, kind="agent", username="steward-bot", role_slug="steward"
    )
    steward_key = await admin_svc.create_api_key(db, steward["id"], name="steward-key")

    return SimpleNamespace(
        client=client,
        db=db,
        human_id=human["id"],
        human_auth={"Authorization": f"Bearer {human_key['plaintext_key']}"},
        steward_id=steward["id"],
        steward_auth={"Authorization": f"Bearer {steward_key['plaintext_key']}"},
        env_auth={"Authorization": f"Bearer {STEWARD_TOKEN}"},
    )


async def _make_task(hub) -> int:
    resp = await hub.client.post(
        "/api/tasks",
        json={"title": "черновик для гейтов"},
        headers=hub.human_auth,
    )
    assert resp.status_code in {200, 201}, resp.text
    return resp.json()["id"]


def _named_api_routes():
    from hub.app import app

    out: list[tuple[str, str]] = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        path = route.path
        if path in _PUBLIC_EXACT or path.startswith("/static"):
            continue
        for method in sorted((route.methods or set()) - {"HEAD", "OPTIONS"}):
            out.append((method, path))
    return out


# ---------------------------------------------------------------------------
# AC-1 — перебор маршрутов
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_steward_principal_denied_everything_else(hub):
    """AC-1: 403 на каждом маршруте вне allowlist, включая human/agent write."""
    task_id = await _make_task(hub)
    named = [
        ("POST", "/api/tasks"),
        ("POST", f"/api/tasks/{task_id}/refine"),
        ("POST", f"/api/tasks/{task_id}/updates"),
        ("POST", f"/api/tasks/{task_id}/approve"),
        ("POST", f"/api/tasks/{task_id}/decide"),
        ("POST", f"/api/tasks/{task_id}/claim"),
        ("GET", "/api/whoami"),
        ("GET", f"/api/tasks/{task_id}"),
        ("POST", "/mcp"),
    ]
    walked = []
    for method, path in _named_api_routes():
        if (method, path) in STEWARD_OPS:
            continue
        walked.append((method, _fill(path)))

    probes = named + walked
    seen: set[tuple[str, str]] = set()
    failures: list[str] = []
    for method, path in probes:
        key = (method, path)
        if key in seen:
            continue
        seen.add(key)
        kwargs: dict = {"headers": hub.steward_auth}
        if method in {"POST", "PUT", "PATCH"}:
            kwargs["json"] = {}
        if path == "/mcp":
            kwargs["headers"] = {
                **hub.steward_auth,
                "Accept": "application/json, text/event-stream",
            }
            kwargs["json"] = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "t", "version": "1"},
                },
            }
        resp = await getattr(hub.client, method.lower())(path, **kwargs)
        if resp.status_code != 403:
            failures.append(f"{method} {path} → {resp.status_code}")
    assert not failures, (
        "steward must get 403 on every route except the two allowed; "
        f"missed {len(failures)}: " + "; ".join(failures[:20])
    )
    assert len(seen) >= 20, "the walk must actually enumerate routes, not a handful"


def _gate_refused(resp) -> bool:
    """Отказал ли ИМЕННО allowlist маршрутов (#1021), а не гейт за дверью.

    С #1075 за разрешённой дверью стоит второй, содержательный гейт: пакет
    выдаётся только под открытый заказ прогона. Его 403 несёт другую причину,
    и путать их нельзя — «маршрута нет в списке» и «читать сейчас нечего» это
    разные утверждения, и только первое отменяло бы allowlist.
    """
    if resp.status_code != 403:
        return False
    try:
        detail = resp.json().get("detail")
    except ValueError:
        return True
    reason = detail.get("reason") if isinstance(detail, dict) else None
    return reason == "steward_gate_forbidden"


@pytest.mark.asyncio
async def test_steward_may_reach_the_two_allowed_ops(hub):
    """AC-1 complementary: the two named ops are not refused BY THE ALLOWLIST."""
    task_id = await _make_task(hub)
    get_path = f"/api/tasks/{task_id}/steward-evidence"
    post_path = f"/api/tasks/{task_id}/steward-judgement"
    got = await hub.client.get(get_path, headers=hub.steward_auth)
    assert not _gate_refused(got), got.text
    posted = await hub.client.post(post_path, json={}, headers=hub.steward_auth)
    assert not _gate_refused(posted), posted.text


# ---------------------------------------------------------------------------
# AC-2 — не человек
# ---------------------------------------------------------------------------


def test_steward_is_never_read_as_human():
    """AC-2: identity flags AND the helpers that used to read 'not agent' as human."""
    ident = TokenIdentity("s", "steward", principal_id=9)
    assert ident.is_human is False
    assert ident.is_agent is False
    assert ident.is_steward is True
    request = SimpleNamespace(state=SimpleNamespace(identity=ident))
    with pytest.raises(HTTPException) as web_gate:
        _require_human_web(request)
    assert web_gate.value.status_code == 403
    with pytest.raises(HTTPException) as rest_gate:
        require_human_or_admin(request)
    assert rest_gate.value.status_code == 403
    with pytest.raises(HTTPException) as create_gate:
        _reject_agent_authored_source(request, TaskSource.human)
    assert create_gate.value.status_code == 403
    # The web create form uses the same predicate, not _require_human_web
    # (different error body). Steward must not fall through as a human author.
    assert not ident.is_human


def test_require_human_or_admin_rejects_steward_env_token():
    """Env-token without principal_id used to pass this gate: is_agent was false."""
    ident = TokenIdentity("s", "steward")
    request = SimpleNamespace(state=SimpleNamespace(identity=ident))
    with pytest.raises(HTTPException) as caught:
        require_human_or_admin(request)
    assert caught.value.status_code == 403


@pytest.mark.asyncio
async def test_db_steward_principal_does_not_resolve_as_human(hub):
    """get_effective_role without a steward slug used to fall through to human."""
    ident = await admin_svc.resolve_api_key(
        hub.db, hub.steward_auth["Authorization"].split(" ", 1)[1]
    )
    assert ident is not None
    assert ident.role == "steward"
    assert ident.is_human is False
    assert ident.is_agent is False
    assert ident.is_steward is True
    assert ident.permissions.isdisjoint(FORBIDDEN_PERMS)


@pytest.mark.asyncio
async def test_not_agent_human_branches_do_not_treat_steward_as_human(hub):
    """AC-2: the #961 class — review-verdict, pair-start, projects, done."""
    task_id = await _make_task(hub)
    before = (
        await hub.client.get(f"/api/tasks/{task_id}", headers=hub.human_auth)
    ).json()

    attempts = [
        (
            "post",
            f"/api/tasks/{task_id}/review-verdict",
            {"verdict": "approved", "summary": "ok"},
        ),
        ("post", f"/api/tasks/{task_id}/pair-start", {"assigned_agent": "bot"}),
        ("post", "/api/projects", {"slug": "smuggled", "name": "Smuggled"}),
        (
            "post",
            f"/api/tasks/{task_id}/updates",
            {"content": "готово", "kind": "done"},
        ),
        ("post", f"/api/tasks/{task_id}/approve", {"force": True}),
        ("post", f"/api/tasks/{task_id}/decide", {"action": "accept"}),
        ("post", "/api/tasks/batch-approve", {"task_ids": [task_id]}),
    ]
    for method, path, payload in attempts:
        resp = await getattr(hub.client, method)(
            path, json=payload, headers=hub.steward_auth
        )
        assert resp.status_code == 403, f"{path}: {resp.text}"

    after = (
        await hub.client.get(f"/api/tasks/{task_id}", headers=hub.human_auth)
    ).json()
    assert after["status"] == before["status"]
    assert after.get("review_verdict") in (None, before.get("review_verdict"))

    projects = await hub.client.get("/api/projects", headers=hub.human_auth)
    assert "smuggled" not in [p["slug"] for p in projects.json()]


@pytest.mark.asyncio
async def test_env_steward_token_cannot_use_human_gates(hub):
    """The env-token shape has no principal_id; is_agent stays false."""
    task_id = await _make_task(hub)
    resp = await hub.client.post(
        f"/api/tasks/{task_id}/approve",
        json={},
        headers=hub.env_auth,
    )
    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# AC-3 — deny-by-default
# ---------------------------------------------------------------------------


def test_steward_allowlist_is_exactly_two_explicit_routes():
    from hub.auth import STEWARD_ALLOWLIST

    assert STEWARD_ALLOWLIST == STEWARD_OPS
    assert len(STEWARD_ALLOWLIST) == 2


def test_steward_perms_are_the_two_ops_and_nothing_forbidden():
    from hub.config import STEWARD_PERMS

    assert len(STEWARD_PERMS) == 2
    assert STEWARD_PERMS.isdisjoint(FORBIDDEN_PERMS)
    ident = TokenIdentity("s", "steward", permissions=STEWARD_PERMS)
    for perm in FORBIDDEN_PERMS:
        assert ident.has_permission(perm) is False


def test_new_routes_are_denied_by_default():
    """AC-3: a route that appears later is closed until explicitly listed."""
    from hub.auth import steward_route_allowed

    ident = TokenIdentity("s", "steward", principal_id=3)
    assert steward_route_allowed("GET", "/api/tasks/1/brand-new", ident) is False
    assert steward_route_allowed("GET", "/api/tasks/1/steward-evidence", ident) is True
    assert (
        steward_route_allowed("POST", "/api/tasks/1/steward-judgement", ident) is True
    )


def test_system_role_seed_grants_only_the_two_ops():
    from hub.db import SYSTEM_ROLES

    slugs = {row[0]: row for row in SYSTEM_ROLES}
    assert "steward" in slugs
    _slug, _name, _desc, perms = slugs["steward"]
    assert set(perms).isdisjoint(FORBIDDEN_PERMS)
    assert len(perms) == 2


# ---------------------------------------------------------------------------
# #1075 — пакет как единственный вход
# ---------------------------------------------------------------------------


_LEAK_MARKERS = ("submission_sha", "findings", "acceptance_criteria", "content")


async def test_evidence_packet_is_only_input(hub):
    """#1075 AC-1: каналы вне пакета не отдают стюарду данные.

    Перебор всех маршрутов уже есть выше (#1021 AC-1); здесь названы именно те
    источники, ради закрытия которых пакет и объявлен единственным входом:
    карточка задачи, её лента, чат пары, инбокс и список сессий. Проверяется не
    только код ответа, но и то, что тело отказа не несёт данных: 403 с
    выдержкой из задачи внутри был бы той же утечкой, только вежливой.
    """
    task_id = await _make_task(hub)
    channels = [
        ("GET", f"/api/tasks/{task_id}"),
        ("GET", f"/api/tasks/{task_id}/updates"),
        ("GET", f"/api/tasks/{task_id}/review-brief"),
        ("GET", "/api/inbox"),
        ("GET", "/api/sessions"),
        ("POST", "/api/auth/chat-pair/issue"),
    ]

    for method, path in channels:
        kwargs: dict = {"headers": hub.steward_auth}
        if method == "POST":
            kwargs["json"] = {}
        resp = await hub.client.request(method, path, **kwargs)
        assert resp.status_code == 403, f"{method} {path} → {resp.status_code}"
        body = resp.text
        for marker in _LEAK_MARKERS:
            assert marker not in body, f"{method} {path} протёк через {marker}"


async def test_packet_requires_an_open_run(hub):
    """#1075 AC-2: без открытого заказа прогона пакет не выдаётся.

    Заказы размещает хаб (#1073), и пока их нет — читать нечего. Судья,
    который берёт доказательства когда захочет, — судья, которого никто не
    звал; поэтому дверь закрыта по умолчанию, а не открыта до появления
    диспетчера.
    """
    task_id = await _make_task(hub)

    resp = await hub.client.get(
        f"/api/tasks/{task_id}/steward-evidence", headers=hub.steward_auth
    )

    assert resp.status_code == 403, resp.text
    detail = resp.json()["detail"]
    assert detail["reason"] == "steward_run_required"
    # Отказ не рассказывает о задаче — включая то, существует ли она вообще.
    missing = await hub.client.get(
        "/api/tasks/999999/steward-evidence", headers=hub.steward_auth
    )
    assert missing.status_code == 403, missing.text
    assert missing.json()["detail"]["reason"] == "steward_run_required"


async def test_allowlist_is_closed(hub):
    """#1075 AC-3: отказ называет операцию и попадает в аудит.

    Молчаливый 403 защищает не хуже, но разобрать по нему нечего: у стюарда
    ровно две двери, поэтому запрос в третью — это либо ошибка прогона, либо
    проба границы, и различить их можно только по записи.
    """
    from hub.db import fetchall

    resp = await hub.client.get(
        f"/api/tasks/{await _make_task(hub)}", headers=hub.steward_auth
    )

    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert detail["reason"] == "steward_gate_forbidden"
    assert "/api/tasks/" in detail["message"] and "GET" in detail["message"]

    events = await fetchall(
        hub.db,
        "SELECT kind, payload FROM events WHERE kind='steward_route_refused'",
    )
    assert events, "отказ стюарду не дошёл до аудита"
    assert any("/api/tasks/" in (dict(e)["payload"] or "") for e in events)
