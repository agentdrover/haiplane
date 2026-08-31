"""Chat-pair: одноразовый код из хаба → короткая сессия с узкими правами (#961).

Тесты написаны до кода и покрывают все 22 AC задачи. Соответствие AC спеки
chat-pair.md rev. 4 указано в docstring каждого теста.

Фикстуры сознательно поднимают хаб С принципалами: ``_is_open_mode()`` истинен
при пустом ``HUB_TOKENS``, и на такой фикстуре каждый тест получил бы 503
вместо своего кода, а тест open mode прошёл бы вхолостую (F3').
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from hub import config
from hub.auth import CSRF_COOKIE_NAME, CSRF_HEADER_NAME, login_limiter
from hub.config import TokenIdentity
from hub.services import admin as admin_svc
from hub.services import chat_pair as cp

CHAT_PAIR_PERMS = {"tasks.read", "tasks.create", "tasks.refine", "tasks.update"}

AGENT_TOKEN = "agent-token"  # pragma: allowlist secret
CALLER_IP = "203.0.113.7"


def _ip(ip: str = CALLER_IP) -> dict[str, str]:
    """Заголовок, по которому лимитер видит клиента (как в hub/web.py)."""
    return {"x-forwarded-for": ip}


async def _rows(db, sql: str, params: tuple = ()) -> list[dict]:
    cursor = await db.execute(sql, params)
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


@pytest.fixture(autouse=True)
def _clean_limiters():
    """Лимитеры живут в памяти процесса — тесты не должны видеть чужие попытки."""
    cp.chat_pair_limiter._buckets.clear()
    login_limiter._buckets.clear()
    yield
    cp.chat_pair_limiter._buckets.clear()
    login_limiter._buckets.clear()


@pytest.fixture
async def hub(client, db, monkeypatch):
    """Хаб с включённым auth: human-принципал, admin-принципал, агентский токен."""
    monkeypatch.setattr(
        config, "HUB_TOKENS", {AGENT_TOKEN: TokenIdentity("bot", "agent")}
    )
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)

    human = await admin_svc.create_principal(
        db, kind="human", username="alice", role_slug="operator"
    )
    human_key = await admin_svc.create_api_key(db, human["id"], name="laptop")
    admin = await admin_svc.create_principal(
        db, kind="human", username="root", role_slug="admin"
    )
    admin_key = await admin_svc.create_api_key(db, admin["id"], name="admin-laptop")

    return SimpleNamespace(
        client=client,
        db=db,
        human_id=human["id"],
        human_token=human_key["plaintext_key"],
        human_auth={"Authorization": f"Bearer {human_key['plaintext_key']}"},
        admin_id=admin["id"],
        admin_token=admin_key["plaintext_key"],
        admin_auth={"Authorization": f"Bearer {admin_key['plaintext_key']}"},
        agent_auth={"Authorization": f"Bearer {AGENT_TOKEN}"},
    )


async def _start(hub, *, auth: dict[str, str] | None = None) -> str:
    """Выдать код через API и вернуть его в том виде, в каком видит оператор."""
    resp = await hub.client.post(
        "/api/auth/chat-pair/start",
        headers={**(auth or hub.human_auth), **_ip()},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["code"]


async def _redeem(hub, code: str) -> str:
    resp = await hub.client.post(
        "/api/auth/chat-pair/redeem", json={"code": code}, headers=_ip()
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


async def _session(hub, *, auth: dict[str, str] | None = None) -> dict[str, str]:
    """Полный проход start → redeem, вернуть заголовок chat-pair сессии."""
    token = await _redeem(hub, await _start(hub, auth=auth))
    return {"Authorization": f"Bearer {token}"}


async def _make_task(hub, title: str = "задача с ноутбука") -> int:
    resp = await hub.client.post(
        "/api/tasks", json={"title": title}, headers=hub.human_auth
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


# ---------------------------------------------------------------------------
# Выдача кода (start)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_rejects_agent_token(hub):
    """AC-1 (спека AC-1): агентский токен кода не получает."""
    resp = await hub.client.post(
        "/api/auth/chat-pair/start", headers={**hub.agent_auth, **_ip()}
    )

    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["reason"] == "human_only_gate"
    assert await _rows(hub.db, "SELECT * FROM chat_pair_codes") == []


@pytest.mark.asyncio
async def test_start_returns_code_stores_hash_only(hub, monkeypatch):
    """AC-2 (спека AC-2): код 8 символов, в БД только hash, TTL из конфига."""
    monkeypatch.setattr(config, "CHAT_PAIR_CODE_SECONDS", 300)

    resp = await hub.client.post(
        "/api/auth/chat-pair/start", headers={**hub.human_auth, **_ip()}
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["expires_in_sec"] == 300

    normalized = cp.normalize_pair_code(body["code"])
    assert len(normalized) == 8
    assert set(normalized) <= set(cp.CODE_ALPHABET)

    rows = await _rows(hub.db, "SELECT * FROM chat_pair_codes")
    assert len(rows) == 1
    stored = json.dumps(rows[0], ensure_ascii=False, default=str)
    assert normalized not in stored
    assert body["code"] not in stored
    assert rows[0]["code_hash"] == cp.hash_pair_code(normalized)
    assert rows[0]["principal_id"] == hub.human_id


@pytest.mark.asyncio
async def test_start_cookie_requires_csrf(hub):
    """AC-3 (спека AC-2b): cookie без валидного CSRF кода не выдаёт."""
    session_token = await admin_svc.create_browser_session(hub.db, hub.human_id)
    hub.client.cookies.set(config.HUB_COOKIE_NAME, session_token)

    no_csrf = await hub.client.post("/api/auth/chat-pair/start", headers=_ip())
    assert no_csrf.status_code == 403, no_csrf.text

    hub.client.cookies.set(CSRF_COOKIE_NAME, "csrf-value")
    wrong_csrf = await hub.client.post(
        "/api/auth/chat-pair/start",
        headers={CSRF_HEADER_NAME: "other-value", **_ip()},
    )
    assert wrong_csrf.status_code == 403, wrong_csrf.text
    assert await _rows(hub.db, "SELECT * FROM chat_pair_codes") == []

    good = await hub.client.post(
        "/api/auth/chat-pair/start",
        headers={CSRF_HEADER_NAME: "csrf-value", **_ip()},
    )
    assert good.status_code == 200, good.text


@pytest.mark.asyncio
async def test_second_start_burns_unused_code(hub):
    """AC-4 (спека AC-3): повторный start сжигает неиспользованный код."""
    code_a = await _start(hub)
    code_b = await _start(hub)

    dead = await hub.client.post(
        "/api/auth/chat-pair/redeem", json={"code": code_a}, headers=_ip()
    )
    assert dead.status_code == 401, dead.text
    assert dead.json()["detail"]["reason"] == "chat_pair_invalid"

    alive = await hub.client.post(
        "/api/auth/chat-pair/redeem", json={"code": code_b}, headers=_ip()
    )
    assert alive.status_code == 200, alive.text


# ---------------------------------------------------------------------------
# Обмен кода на сессию (redeem)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_redeem_issues_session_for_same_principal(hub):
    """AC-5 (спека AC-4): сессия того же принципала, но с узкими правами."""
    code = await _start(hub, auth=hub.admin_auth)

    resp = await hub.client.post(
        "/api/auth/chat-pair/redeem",
        json={"code": code.lower()},
        headers=_ip(),
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["username"] == "root"
    assert body["role"] == "human"
    assert body["base_url"]
    assert set(body["permissions"]) == CHAT_PAIR_PERMS
    assert body["expires_at"]

    who = await hub.client.get(
        "/api/whoami", headers={"Authorization": f"Bearer {body['token']}"}
    )
    assert who.status_code == 200, who.text
    identity = who.json()
    assert identity["principal_id"] == hub.admin_id
    assert identity["auth_source"] == "chat_pair"
    assert identity["api_key_id"] is None
    summary = set(identity["permissions_summary"])
    assert summary == CHAT_PAIR_PERMS
    assert not summary & {"tasks.human_gate", "tasks.decision", "admin.read"}

    rows = await _rows(hub.db, "SELECT * FROM chat_pair_sessions")
    assert len(rows) == 1
    assert body["token"] not in json.dumps(rows[0], ensure_ascii=False, default=str)


@pytest.mark.asyncio
async def test_redeem_accepts_display_form_with_prefix(hub):
    """AC-5 (спека AC-4): форма AH-… принимается наравне с нормализованной."""
    code = await _start(hub)
    assert code.startswith("AH-")

    resp = await hub.client.post(
        "/api/auth/chat-pair/redeem", json={"code": code}, headers=_ip()
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_redeem_invalid_cases_are_indistinguishable(hub):
    """AC-6 (спека AC-5): повтор, чужая строка и просрочка неотличимы."""
    used = await _start(hub)
    await _redeem(hub, used)

    expired = await _start(hub)
    await hub.db.execute(
        "UPDATE chat_pair_codes SET expires_at = datetime('now', '-1 second') "
        "WHERE redeemed_at IS NULL"
    )
    await hub.db.commit()

    answers = []
    for candidate in (used, "ZZZZZZZZ", expired):
        resp = await hub.client.post(
            "/api/auth/chat-pair/redeem", json={"code": candidate}, headers=_ip()
        )
        answers.append((resp.status_code, resp.json()["detail"]["reason"]))
        assert "token" not in resp.json()

    assert answers == [(401, "chat_pair_invalid")] * 3


@pytest.mark.asyncio
async def test_redeem_rate_limited_per_ip_separate_from_login(hub, monkeypatch):
    """AC-7 (спека AC-6): свой лимитер, 429, /login не задет."""
    monkeypatch.setattr(config, "CHAT_PAIR_REDEEM_MAX", 3)

    for _ in range(3):
        resp = await hub.client.post(
            "/api/auth/chat-pair/redeem", json={"code": "ZZZZZZZZ"}, headers=_ip()
        )
        assert resp.status_code == 401, resp.text

    blocked = await hub.client.post(
        "/api/auth/chat-pair/redeem", json={"code": "ZZZZZZZZ"}, headers=_ip()
    )
    assert blocked.status_code == 429, blocked.text
    assert blocked.json()["detail"]["reason"] == "chat_pair_rate_limited"

    assert not login_limiter.is_blocked(CALLER_IP)
    assert (await hub.client.get("/login", headers=_ip())).status_code == 200

    other_ip = await hub.client.post(
        "/api/auth/chat-pair/redeem",
        json={"code": "ZZZZZZZZ"},
        headers=_ip("198.51.100.4"),
    )
    assert other_ip.status_code == 401, other_ip.text

    code = await _start(hub)
    cp.chat_pair_limiter._buckets.clear()
    reopened = await hub.client.post(
        "/api/auth/chat-pair/redeem", json={"code": code}, headers=_ip()
    )
    assert reopened.status_code == 200, reopened.text


# ---------------------------------------------------------------------------
# Что сессия может и чего не может
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_pair_token_creates_open_task(hub):
    """AC-8 (спека AC-7): задача создаётся в open с атрибуцией принципала."""
    session = await _session(hub)

    resp = await hub.client.post(
        "/api/tasks", json={"title": "поставлено с телефона"}, headers=session
    )
    assert resp.status_code == 200, resp.text
    task = resp.json()
    assert task["status"] == "open"
    assert task["source"] == "human"

    first = await hub.client.post(
        "/api/tasks",
        json={"title": "идемпотентная", "client_request_id": "phone-1"},
        headers=session,
    )
    assert first.status_code == 201, first.text
    again = await hub.client.post(
        "/api/tasks",
        json={"title": "идемпотентная", "client_request_id": "phone-1"},
        headers=session,
    )
    assert again.status_code == 200, again.text
    assert again.json()["id"] == first.json()["id"]


@pytest.mark.asyncio
async def test_chat_pair_token_forbidden_on_human_gate_and_admin(hub):
    """AC-9 (спека AC-7b): даже сессия admin-принципала не проходит гейты."""
    session = await _session(hub, auth=hub.admin_auth)
    task_id = await _make_task(hub)

    approve = await hub.client.post(f"/api/tasks/{task_id}/approve", headers=session)
    assert approve.status_code == 403, approve.text
    assert approve.json()["detail"]["reason"] == "chat_pair_gate_forbidden"

    summary = await hub.client.get("/api/admin/summary", headers=session)
    assert summary.status_code == 403, summary.text
    assert summary.json()["detail"]["reason"] == "chat_pair_gate_forbidden"


@pytest.mark.asyncio
async def test_chat_pair_allowlist_blocks_human_branch_routes(hub):
    """AC-10 (спека AC-7c): ветки «не агент → значит человек» закрыты."""
    session = await _session(hub)
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
    ]
    for method, path, payload in attempts:
        resp = await getattr(hub.client, method)(path, json=payload, headers=session)
        assert resp.status_code == 403, f"{path}: {resp.text}"
        assert resp.json()["detail"]["reason"] == "chat_pair_gate_forbidden"

    after = (
        await hub.client.get(f"/api/tasks/{task_id}", headers=hub.human_auth)
    ).json()
    assert after["status"] == before["status"]
    assert after["review_verdict"] is None
    assert after["updates"] in (None, [])

    projects = await hub.client.get("/api/projects", headers=hub.human_auth)
    assert "smuggled" not in [p["slug"] for p in projects.json()]


@pytest.mark.asyncio
async def test_chat_pair_denies_unlisted_route(hub):
    """AC-11 (спека AC-7d): deny-by-default, включая /mcp."""
    session = await _session(hub)
    task_id = await _make_task(hub)

    unlisted = await hub.client.get(f"/api/tasks/{task_id}/log", headers=session)
    assert unlisted.status_code == 403, unlisted.text
    assert unlisted.json()["detail"]["reason"] == "chat_pair_gate_forbidden"

    mcp = await hub.client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "cursor", "version": "1"},
            },
        },
        headers={**session, "Accept": "application/json, text/event-stream"},
    )
    assert mcp.status_code == 403, mcp.text
    assert "tools" not in mcp.text


@pytest.mark.asyncio
async def test_chat_pair_allowlist_permits_task_authoring(hub):
    """AC-12 (спека AC-7e): минимальный рабочий набор для постановки открыт."""
    session = await _session(hub)
    task_id = await _make_task(hub)

    for path in (
        "/api/whoami",
        "/api/tasks",
        f"/api/tasks/{task_id}",
        f"/api/tasks/{task_id}/readiness",
        f"/api/tasks/{task_id}/tree",
        f"/api/tasks/{task_id}/acceptance_criteria",
    ):
        resp = await hub.client.get(path, headers=session)
        assert resp.status_code != 403, f"{path}: {resp.text}"

    refine = await hub.client.post(
        f"/api/tasks/{task_id}/refine",
        json={"business_value": "с телефона"},
        headers=session,
    )
    assert refine.status_code == 200, refine.text
    assert refine.json()["business_value"] == "с телефона"
    assert refine.json()["title"] == "задача с ноутбука"


@pytest.mark.asyncio
async def test_chat_pair_create_rejects_run_and_review_optout(hub):
    """AC-13 (спека AC-7f): канал не запускает исполнение и не снимает ревью."""
    session = await _session(hub)

    for payload in (
        {"title": "запусти немедленно", "run_immediately": True},
        {"title": "без ревью", "auto_review": False},
    ):
        resp = await hub.client.post("/api/tasks", json=payload, headers=session)
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"]["reason"] == "chat_pair_run_forbidden"

    titles = await _rows(hub.db, "SELECT title FROM tasks")
    assert [t["title"] for t in titles] == []

    ok = await hub.client.post(
        "/api/tasks", json={"title": "обычная постановка"}, headers=session
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "open"
    assert ok.json()["job_id"] is None


# ---------------------------------------------------------------------------
# Жизнь и смерть сессии
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_pair_session_expires(hub, monkeypatch):
    """AC-14 (спека AC-8): по истечении TTL сессия мертва."""
    monkeypatch.setattr(config, "CHAT_PAIR_TTL_SECONDS", 1)
    session = await _session(hub)

    rows = await _rows(hub.db, "SELECT * FROM chat_pair_sessions")
    assert len(rows) == 1

    await hub.db.execute(
        "UPDATE chat_pair_sessions SET expires_at = datetime('now', '-1 second')"
    )
    await hub.db.commit()

    who = await hub.client.get(
        "/api/whoami", headers={**session, "Accept": "application/json"}
    )
    assert who.status_code == 401, who.text

    created = await hub.client.post(
        "/api/tasks",
        json={"title": "поздно"},
        headers={**session, "Accept": "application/json"},
    )
    assert created.status_code == 401, created.text


@pytest.mark.asyncio
async def test_revoke_chat_pair_does_not_kill_other_auth(hub):
    """AC-15 (спека AC-9): отзыв гасит только chat-pair сессии."""
    session = await _session(hub)
    cookie_token = await admin_svc.create_browser_session(hub.db, hub.human_id)

    revoke = await hub.client.post(
        "/api/auth/chat-pair/revoke", headers={**hub.human_auth, **_ip()}
    )
    assert revoke.status_code == 200, revoke.text

    dead = await hub.client.get(
        "/api/whoami", headers={**session, "Accept": "application/json"}
    )
    assert dead.status_code == 401, dead.text

    by_key = await hub.client.get("/api/whoami", headers=hub.human_auth)
    assert by_key.status_code == 200, by_key.text

    hub.client.cookies.set(config.HUB_COOKIE_NAME, cookie_token)
    by_cookie = await hub.client.get("/api/whoami")
    assert by_cookie.status_code == 200, by_cookie.text


@pytest.mark.asyncio
async def test_chat_pair_token_can_revoke_itself(hub):
    """AC-16 (спека AC-9b): «закончил в метро» не требует ноутбука."""
    session = await _session(hub)

    resp = await hub.client.post("/api/auth/chat-pair/revoke", headers=session)
    assert resp.status_code == 200, resp.text

    after = await hub.client.get(
        "/api/whoami", headers={**session, "Accept": "application/json"}
    )
    assert after.status_code == 401, after.text


@pytest.mark.asyncio
async def test_audit_omits_code_and_token(hub):
    """AC-17 (спека AC-10): в аудите есть факт, но нет секретов."""
    code = await _start(hub)
    token = await _redeem(hub, code)
    await hub.client.post(
        "/api/auth/chat-pair/revoke", headers={**hub.human_auth, **_ip()}
    )

    audit = await _rows(hub.db, "SELECT * FROM admin_audit_log")
    activity = await _rows(hub.db, "SELECT * FROM activity_log")
    dump = json.dumps(audit + activity, ensure_ascii=False, default=str)

    assert cp.normalize_pair_code(code) not in dump
    assert code not in dump
    assert token not in dump

    actions = {row["action"] for row in audit}
    assert {"chat_pair_start", "chat_pair_redeem", "chat_pair_revoke"} <= actions
    assert any(row["actor_principal_id"] == hub.human_id for row in audit)


# ---------------------------------------------------------------------------
# Web UI, миграция, open mode, reaper, конверт ошибок
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_web_pair_form_issues_code(hub):
    """AC-18 (спека AC-11): кнопка в Web UI выдаёт тот же код."""
    session_token = await admin_svc.create_browser_session(hub.db, hub.human_id)
    hub.client.cookies.set(config.HUB_COOKIE_NAME, session_token)

    page = await hub.client.get("/chat-pair", headers={"Accept": "text/html"})
    assert page.status_code == 200, page.text
    csrf = page.cookies.get(CSRF_COOKIE_NAME)
    assert csrf

    hub.client.cookies.set(CSRF_COOKIE_NAME, csrf)
    issued = await hub.client.post(
        "/chat-pair/web-start",
        data={"csrf_token": csrf},
        headers={"Accept": "text/html"},
        follow_redirects=True,
    )
    assert issued.status_code == 200, issued.text
    rows = await _rows(hub.db, "SELECT * FROM chat_pair_codes")
    assert len(rows) == 1
    assert "AH-" in issued.text

    without_csrf = await hub.client.post(
        "/chat-pair/web-start",
        data={"csrf_token": "wrong"},
        headers={"Accept": "text/html"},
        follow_redirects=False,
    )
    assert without_csrf.status_code in (303, 403), without_csrf.text
    assert len(await _rows(hub.db, "SELECT * FROM chat_pair_codes")) == 1

    hub.client.cookies.clear()
    as_agent = await hub.client.get(
        "/chat-pair", headers={**hub.agent_auth, "Accept": "text/html"}
    )
    assert as_agent.status_code in (303, 403), as_agent.text


@pytest.mark.asyncio
async def test_open_mode_refuses_chat_pair(client, db, monkeypatch):
    """AC-20 (спека AC-13): без auth канала личности не существует."""
    monkeypatch.setattr(config, "HUB_TOKENS", {})
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)

    start = await client.post("/api/auth/chat-pair/start", headers=_ip())
    assert start.status_code == 503, start.text
    assert start.json()["detail"]["reason"] == "chat_pair_auth_required"

    redeem = await client.post(
        "/api/auth/chat-pair/redeem", json={"code": "ZZZZZZZZ"}, headers=_ip()
    )
    assert redeem.status_code == 503, redeem.text
    assert redeem.json()["detail"]["reason"] == "chat_pair_auth_required"

    cursor = await db.execute("SELECT COUNT(*) FROM chat_pair_codes")
    assert (await cursor.fetchone())[0] == 0


@pytest.mark.asyncio
async def test_reaper_purges_expired_pair_rows(hub):
    """AC-21 (спека AC-14): чистка убирает мёртвое и не трогает живое."""
    # Порядок важен: второй start сжигает неиспользованный код, поэтому сперва
    # тратим один код (его строка остаётся жить по retention), потом просим
    # новый — он и есть живой невыданный.
    live_token = await _redeem(hub, await _start(hub))
    live_code = await _start(hub)

    await hub.db.execute(
        "INSERT INTO chat_pair_codes (principal_id, code_hash, expires_at) "
        "VALUES (?, 'dead-1', datetime('now', '-10 minutes'))",
        (hub.human_id,),
    )
    await hub.db.execute(
        "INSERT INTO chat_pair_codes (principal_id, code_hash, expires_at, redeemed_at) "
        "VALUES (?, 'dead-2', datetime('now', '+10 minutes'), datetime('now', '-2 days'))",
        (hub.human_id,),
    )
    await hub.db.execute(
        "INSERT INTO chat_pair_sessions (principal_id, token_hash, expires_at) "
        "VALUES (?, 'dead-3', datetime('now', '-1 hour'))",
        (hub.human_id,),
    )
    await hub.db.execute(
        "INSERT INTO chat_pair_sessions (principal_id, token_hash, expires_at, revoked_at) "
        "VALUES (?, 'dead-4', datetime('now', '+1 hour'), datetime('now'))",
        (hub.human_id,),
    )
    await hub.db.commit()

    removed = await cp.purge_expired(hub.db)
    assert removed == 4

    codes = {
        c["code_hash"]
        for c in await _rows(hub.db, "SELECT code_hash FROM chat_pair_codes")
    }
    sessions = {
        s["token_hash"]
        for s in await _rows(hub.db, "SELECT token_hash FROM chat_pair_sessions")
    }
    assert not {"dead-1", "dead-2"} & codes
    assert not {"dead-3", "dead-4"} & sessions
    assert cp.hash_pair_code(cp.normalize_pair_code(live_code)) in codes
    assert cp.hash_pair_code(live_token) in sessions


@pytest.mark.asyncio
async def test_chat_pair_errors_are_actionable(hub, monkeypatch):
    """AC-22 (спека AC-15): новые reason проходят общую обвязку."""
    from hub.mcp_envelope import MUTATION_ENVELOPE_FIELDS, compute_next_action

    generic = compute_next_action("?", "none")
    session = await _session(hub)
    task_id = await _make_task(hub)

    monkeypatch.setattr(config, "CHAT_PAIR_REDEEM_MAX", 1)
    await hub.client.post(
        "/api/auth/chat-pair/redeem",
        json={"code": "ZZZZZZZZ"},
        headers=_ip("198.51.100.9"),
    )

    cases = [
        (
            "chat_pair_gate_forbidden",
            await hub.client.post(f"/api/tasks/{task_id}/approve", headers=session),
        ),
        (
            "chat_pair_invalid",
            await hub.client.post(
                "/api/auth/chat-pair/redeem", json={"code": "YYYYYYYY"}, headers=_ip()
            ),
        ),
        (
            "chat_pair_rate_limited",
            await hub.client.post(
                "/api/auth/chat-pair/redeem",
                json={"code": "YYYYYYYY"},
                headers=_ip("198.51.100.9"),
            ),
        ),
        (
            "chat_pair_run_forbidden",
            await hub.client.post(
                "/api/tasks",
                json={"title": "запусти", "run_immediately": True},
                headers=session,
            ),
        ),
    ]

    for reason, resp in cases:
        detail = resp.json()["detail"]
        assert detail["reason"] == reason, resp.text
        for field in MUTATION_ENVELOPE_FIELDS:
            assert field in detail, f"{reason}: нет поля {field}"
        assert detail["next_action"]
        assert detail["next_action"] != generic, f"{reason}: обвязка по умолчанию"
        assert detail.get("hint")


# ---------------------------------------------------------------------------
# #980 kind=implementer — sibling of intake, one open task
# ---------------------------------------------------------------------------

IMPLEMENTER_PERMS = {"tasks.read", "tasks.update", "tasks.agent_report"}


async def _start_implementer(hub, task_id: int):
    return await hub.client.post(
        "/api/auth/chat-pair/start",
        json={"kind": "implementer", "task_id": task_id},
        headers={**hub.human_auth, **_ip()},
    )


async def _implementer_session(hub, task_id: int) -> dict[str, str]:
    issued = await _start_implementer(hub, task_id)
    assert issued.status_code == 200, issued.text
    token = await _redeem(hub, issued.json()["code"])
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_implementer_cannot_reach_another_task(hub):
    """AC-1: bound to N, a different {task_id} is 403 chat_pair_gate_forbidden."""
    bound = await _make_task(hub, "bound")
    other = await _make_task(hub, "other")
    session = await _implementer_session(hub, bound)

    resp = await hub.client.get(f"/api/tasks/{other}", headers=session)
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["reason"] == "chat_pair_gate_forbidden"

    own = await hub.client.get(f"/api/tasks/{bound}", headers=session)
    assert own.status_code == 200, own.text


@pytest.mark.asyncio
async def test_implementer_code_not_issued_unless_task_is_open(hub):
    """AC-2: running/claimed task → 409, no code row."""
    task_id = await _make_task(hub)
    started = await hub.client.post(
        f"/api/tasks/{task_id}/pair-start",
        json={"assigned_agent": "bot", "plan": "Plan: go", "session_id": "s-980"},
        headers=hub.agent_auth,
    )
    assert started.status_code == 200, started.text

    before = await _rows(hub.db, "SELECT id FROM chat_pair_codes")
    resp = await _start_implementer(hub, task_id)
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["reason"] == "chat_pair_task_not_open"
    after = await _rows(hub.db, "SELECT id FROM chat_pair_codes")
    assert after == before


@pytest.mark.asyncio
async def test_implementer_self_revoke_leaves_intake_alive(hub):
    """AC-3: implementer self-revoke 401s itself; intake whoami stays 200."""
    task_id = await _make_task(hub)
    intake = await _session(hub)
    implementer = await _implementer_session(hub, task_id)

    revoked = await hub.client.post("/api/auth/chat-pair/revoke", headers=implementer)
    assert revoked.status_code == 200, revoked.text

    dead = await hub.client.get("/api/whoami", headers=implementer)
    assert dead.status_code == 401, dead.text

    live = await hub.client.get("/api/whoami", headers=intake)
    assert live.status_code == 200, live.text
    assert live.json()["role"] == "human"


@pytest.mark.asyncio
async def test_implementer_redeem_spent_code_is_indistinguishable(hub):
    """AC-4: second redeem of the same code is 401 chat_pair_invalid."""
    task_id = await _make_task(hub)
    issued = await _start_implementer(hub, task_id)
    assert issued.status_code == 200, issued.text
    code = issued.json()["code"]
    await _redeem(hub, code)

    again = await hub.client.post(
        "/api/auth/chat-pair/redeem", json={"code": code}, headers=_ip()
    )
    assert again.status_code == 401, again.text
    assert again.json()["detail"]["reason"] == "chat_pair_invalid"
    assert "token" not in again.json()


@pytest.mark.asyncio
async def test_implementer_start_without_cloud_is_503_guessed_redeem_401(hub):
    """AC-5: missing/inactive cloud → 503 on issue; guessed redeem stays 401."""
    await hub.db.execute(
        "UPDATE principals SET status = 'disabled' WHERE username = ?",
        (config.CHAT_PAIR_AGENT,),
    )
    await hub.db.commit()
    task_id = await _make_task(hub)

    issued = await _start_implementer(hub, task_id)
    assert issued.status_code == 503, issued.text
    assert issued.json()["detail"]["reason"] == "chat_pair_agent_missing"

    guessed = await hub.client.post(
        "/api/auth/chat-pair/redeem", json={"code": "ZZZZZZZZ"}, headers=_ip()
    )
    assert guessed.status_code == 401, guessed.text
    assert guessed.json()["detail"]["reason"] == "chat_pair_invalid"


@pytest.mark.asyncio
async def test_intake_start_redeem_unchanged_alongside_implementer(hub):
    """AC-6: intake still human + CHAT_PAIR_PERMS + create."""
    task_id = await _make_task(hub)
    await _implementer_session(hub, task_id)
    session = await _session(hub)

    who = await hub.client.get("/api/whoami", headers=session)
    assert who.status_code == 200, who.text
    body = who.json()
    assert body["role"] == "human"
    assert set(body["permissions_summary"]) == CHAT_PAIR_PERMS

    created = await hub.client.post(
        "/api/tasks", json={"title": "intake still creates"}, headers=session
    )
    assert created.status_code in (200, 201), created.text


# ---------------------------------------------------------------------------
# #981 button on the open task card
# ---------------------------------------------------------------------------


async def _browser_human(hub) -> None:
    session_token = await admin_svc.create_browser_session(hub.db, hub.human_id)
    hub.client.cookies.set(config.HUB_COOKIE_NAME, session_token)


@pytest.mark.asyncio
async def test_open_task_card_issues_implementer_code(hub):
    """#981 AC-1: Transfer to cloud chat on an open task shows AH- bound to that id."""
    await _browser_human(hub)
    task_id = await _make_task(hub)

    page = await hub.client.get(f"/tasks/{task_id}", headers={"Accept": "text/html"})
    assert page.status_code == 200, page.text
    assert "Передать в облачный чат" in page.text
    csrf = page.cookies.get(CSRF_COOKIE_NAME)
    assert csrf

    hub.client.cookies.set(CSRF_COOKIE_NAME, csrf)
    issued = await hub.client.post(
        f"/tasks/{task_id}/web-implementer-start",
        data={"csrf_token": csrf},
        headers={"Accept": "text/html"},
        follow_redirects=True,
    )
    assert issued.status_code == 200, issued.text
    assert "AH-" in issued.text
    assert f"#{task_id}" in issued.text or str(task_id) in issued.text
    rows = await _rows(
        hub.db,
        "SELECT kind, bound_task_id FROM chat_pair_codes WHERE redeemed_at IS NULL",
    )
    assert len(rows) == 1
    assert rows[0]["kind"] == "implementer"
    assert int(rows[0]["bound_task_id"]) == task_id

    without_csrf = await hub.client.post(
        f"/tasks/{task_id}/web-implementer-start",
        data={"csrf_token": "wrong"},
        headers={"Accept": "text/html"},
        follow_redirects=False,
    )
    assert without_csrf.status_code in (303, 403), without_csrf.text
    assert "AH-" not in without_csrf.text or without_csrf.status_code != 200
    assert len(await _rows(hub.db, "SELECT id FROM chat_pair_codes")) == 1


@pytest.mark.asyncio
async def test_running_task_card_does_not_issue_implementer_code(hub):
    """#981 AC-2: running task — no button, POST is 409 and inserts nothing."""
    await _browser_human(hub)
    task_id = await _make_task(hub)
    started = await hub.client.post(
        f"/api/tasks/{task_id}/pair-start",
        json={"assigned_agent": "bot", "plan": "Plan: go", "session_id": "s-981"},
        headers=hub.agent_auth,
    )
    assert started.status_code == 200, started.text

    page = await hub.client.get(f"/tasks/{task_id}", headers={"Accept": "text/html"})
    assert page.status_code == 200, page.text
    assert "Передать в облачный чат" not in page.text

    csrf = page.cookies.get(CSRF_COOKIE_NAME) or "missing"
    hub.client.cookies.set(CSRF_COOKIE_NAME, csrf)
    before = await _rows(hub.db, "SELECT id FROM chat_pair_codes")
    resp = await hub.client.post(
        f"/tasks/{task_id}/web-implementer-start",
        data={"csrf_token": csrf},
        headers={"Accept": "text/html"},
        follow_redirects=False,
    )
    assert resp.status_code == 409, resp.text
    assert "AH-" not in resp.text
    after = await _rows(hub.db, "SELECT id FROM chat_pair_codes")
    assert after == before


@pytest.mark.asyncio
async def test_chat_pair_page_copy_stays_intake(hub):
    """#981 AC-3: /chat-pair still describes intake create/refine, not implementer."""
    await _browser_human(hub)
    page = await hub.client.get("/chat-pair", headers={"Accept": "text/html"})
    assert page.status_code == 200, page.text
    assert "постановку и уточнение" in page.text
    assert "Передать в облачный чат" not in page.text
    assert "implementer" not in page.text.lower()


# ---------------------------------------------------------------------------
# #990: task-card GET must not rotate the shared CSRF cookie
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_second_task_card_get_does_not_invalidate_implementer_form(hub):
    """#990 AC-1: a later task-card GET must not 403 the original form token."""
    await _browser_human(hub)
    first_id = await _make_task(hub)
    other_id = await _make_task(hub)

    page = await hub.client.get(f"/tasks/{first_id}", headers={"Accept": "text/html"})
    assert page.status_code == 200, page.text
    csrf = page.cookies.get(CSRF_COOKIE_NAME)
    assert csrf

    later = await hub.client.get(f"/tasks/{other_id}", headers={"Accept": "text/html"})
    assert later.status_code == 200, later.text
    assert later.cookies.get(CSRF_COOKIE_NAME) == csrf

    hub.client.cookies.set(CSRF_COOKIE_NAME, csrf)
    issued = await hub.client.post(
        f"/tasks/{first_id}/web-implementer-start",
        data={"csrf_token": csrf},
        headers={"Accept": "text/html"},
        follow_redirects=True,
    )
    assert issued.status_code == 200, issued.text
    assert "Форма устарела" not in issued.text
    assert "AH-" in issued.text


@pytest.mark.asyncio
async def test_task_card_get_does_not_invalidate_chat_pair_form(hub):
    """#990 AC-2: viewing a task card must not 403 the original /chat-pair form."""
    await _browser_human(hub)
    task_id = await _make_task(hub)

    pair = await hub.client.get("/chat-pair", headers={"Accept": "text/html"})
    assert pair.status_code == 200, pair.text
    csrf = pair.cookies.get(CSRF_COOKIE_NAME)
    assert csrf

    card = await hub.client.get(f"/tasks/{task_id}", headers={"Accept": "text/html"})
    assert card.status_code == 200, card.text
    assert card.cookies.get(CSRF_COOKIE_NAME) == csrf

    hub.client.cookies.set(CSRF_COOKIE_NAME, csrf)
    issued = await hub.client.post(
        "/chat-pair/web-start",
        data={"csrf_token": csrf},
        headers={"Accept": "text/html"},
        follow_redirects=True,
    )
    assert issued.status_code == 200, issued.text
    assert "Форма устарела" not in issued.text
    assert "AH-" in issued.text


@pytest.mark.asyncio
async def test_implementer_start_wrong_csrf_is_403_and_inserts_nothing(hub):
    """#990 AC-3: a mismatched CSRF token is 403 and creates no AH- row."""
    await _browser_human(hub)
    task_id = await _make_task(hub)
    page = await hub.client.get(f"/tasks/{task_id}", headers={"Accept": "text/html"})
    assert page.status_code == 200, page.text
    csrf = page.cookies.get(CSRF_COOKIE_NAME)
    assert csrf
    hub.client.cookies.set(CSRF_COOKIE_NAME, csrf)

    before = await _rows(hub.db, "SELECT id FROM chat_pair_codes")
    resp = await hub.client.post(
        f"/tasks/{task_id}/web-implementer-start",
        data={"csrf_token": "wrong"},
        headers={"Accept": "text/html"},
        follow_redirects=False,
    )
    assert resp.status_code == 403, resp.text
    assert "AH-" not in resp.text
    after = await _rows(hub.db, "SELECT id FROM chat_pair_codes")
    assert after == before


# ---------------------------------------------------------------------------
# #983 TTL: refuse renew; release bound pair task when implementer session dies
# ---------------------------------------------------------------------------


async def _pair_start_implementer(hub, task_id: int, session: dict[str, str]) -> None:
    started = await hub.client.post(
        f"/api/tasks/{task_id}/pair-start",
        json={
            "assigned_agent": config.CHAT_PAIR_AGENT,
            "plan": "Plan: go",
            "session_id": "s-983",
            "git_mode": "remote",
        },
        headers=session,
    )
    assert started.status_code == 200, started.text


async def _expire_kind(hub, kind: str) -> None:
    await hub.db.execute(
        "UPDATE chat_pair_sessions SET expires_at = datetime('now', '-1 second') "
        "WHERE kind = ?",
        (kind,),
    )
    await hub.db.commit()


@pytest.mark.asyncio
async def test_expired_implementer_session_releases_running_task(hub):
    """#983 AC-1: dead implementer session returns the bound running task to open."""
    task_id = await _make_task(hub)
    session = await _implementer_session(hub, task_id)
    await _pair_start_implementer(hub, task_id, session)
    await _expire_kind(hub, "implementer")

    await cp.purge_expired(hub.db)

    row = dict(
        (await _rows(hub.db, "SELECT status FROM tasks WHERE id = ?", (task_id,)))[0]
    )
    assert row["status"] == "open"
    updates = await _rows(
        hub.db,
        "SELECT kind, content FROM task_updates WHERE task_id = ? ORDER BY id",
        (task_id,),
    )
    assert any(
        u["kind"] == "status" and "pairing session expired" in u["content"]
        for u in updates
    )
    who = await hub.client.get("/api/whoami", headers=session)
    assert who.status_code == 401, who.text


@pytest.mark.asyncio
async def test_expired_implementer_session_releases_claimed_task(hub):
    """#983: claim without pair-start is also unstuck when the session dies."""
    task_id = await _make_task(hub)
    session = await _implementer_session(hub, task_id)
    claimed = await hub.client.post(
        f"/api/tasks/{task_id}/claim",
        json={"agent": config.CHAT_PAIR_AGENT, "session_id": "s-983-claim"},
        headers=session,
    )
    assert claimed.status_code == 200, claimed.text
    await _expire_kind(hub, "implementer")

    await cp.purge_expired(hub.db)

    row = dict(
        (await _rows(hub.db, "SELECT status FROM tasks WHERE id = ?", (task_id,)))[0]
    )
    assert row["status"] == "open"


@pytest.mark.asyncio
async def test_expired_implementer_session_leaves_review_task(hub):
    """#983: work already in review is not yanked back to open."""
    task_id = await _make_task(hub)
    session = await _implementer_session(hub, task_id)
    await _pair_start_implementer(hub, task_id, session)
    await hub.db.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (task_id,))
    await hub.db.commit()
    await _expire_kind(hub, "implementer")

    await cp.purge_expired(hub.db)

    row = dict(
        (await _rows(hub.db, "SELECT status FROM tasks WHERE id = ?", (task_id,)))[0]
    )
    assert row["status"] == "review"


@pytest.mark.asyncio
async def test_expired_intake_session_does_not_release_running_task(hub):
    """#983 out of scope: intake TTL does not move a running pair task."""
    task_id = await _make_task(hub)
    started = await hub.client.post(
        f"/api/tasks/{task_id}/pair-start",
        json={"assigned_agent": "bot", "plan": "Plan: go", "session_id": "s-intake"},
        headers=hub.agent_auth,
    )
    assert started.status_code == 200, started.text
    await _session(hub)
    await _expire_kind(hub, "intake")

    await cp.purge_expired(hub.db)

    row = dict(
        (await _rows(hub.db, "SELECT status FROM tasks WHERE id = ?", (task_id,)))[0]
    )
    assert row["status"] == "running"


@pytest.mark.asyncio
async def test_live_implementer_session_does_not_release_running_task(hub):
    """#983: a still-valid implementer token must not trip the reaper."""
    task_id = await _make_task(hub)
    session = await _implementer_session(hub, task_id)
    await _pair_start_implementer(hub, task_id, session)

    await cp.purge_expired(hub.db)

    row = dict(
        (await _rows(hub.db, "SELECT status FROM tasks WHERE id = ?", (task_id,)))[0]
    )
    assert row["status"] == "running"
    who = await hub.client.get("/api/whoami", headers=session)
    assert who.status_code == 200, who.text


@pytest.mark.asyncio
async def test_expired_sibling_implementer_session_does_not_release_live_task(hub):
    """#983: a dead sibling session must not reopen work a live session still holds."""
    task_id = await _make_task(hub)
    stale = await _implementer_session(hub, task_id)
    live = await _implementer_session(hub, task_id)
    await _pair_start_implementer(hub, task_id, live)

    stale_token = stale["Authorization"].removeprefix("Bearer ")
    await hub.db.execute(
        "UPDATE chat_pair_sessions SET expires_at = datetime('now', '-1 second') "
        "WHERE token_hash = ?",
        (cp.hash_pair_code(stale_token),),
    )
    await hub.db.commit()

    await cp.purge_expired(hub.db)

    row = dict(
        (await _rows(hub.db, "SELECT status FROM tasks WHERE id = ?", (task_id,)))[0]
    )
    assert row["status"] == "running"
    who = await hub.client.get("/api/whoami", headers=live)
    assert who.status_code == 200, who.text


# ---------------------------------------------------------------------------
# kind=reviewer (#1084): облачный ревьюер, у которого нет MCP
# ---------------------------------------------------------------------------
#
# Код этого вида чеканит ДИСПАТЧ, а не человек: маршрута выдачи для reviewer
# нет и не должно быть — иначе появился бы способ выписать себе право сдать
# отчёт от имени ревьюера. Поэтому тесты зовут issue_code напрямую, ровно как
# это делает диспатч.


async def _reviewer_session(hub, task_id: int) -> dict[str, str]:
    code, _ttl = await cp.issue_code(
        hub.db, hub.human_id, kind="reviewer", bound_task_id=task_id
    )
    resp = await hub.client.post(
        "/api/auth/chat-pair/redeem", json={"code": code}, headers=_ip()
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


async def _in_review(hub, title: str) -> int:
    task_id = await _make_task(hub, title)
    # Поколение сдачи обязательно: отчёт ревью привязывается к нему, и без
    # сдачи приёмник отвечает 400 — это его правило, а не отказ доступа.
    await hub.db.execute(
        "UPDATE tasks SET status='review', submission_generation=1 WHERE id=?",
        (task_id,),
    )
    await hub.db.commit()
    return task_id


@pytest.mark.asyncio
async def test_reviewer_session_reaches_two_routes_and_nothing_else(hub):
    """AC-2 (#1084): два маршрута по своей задаче — и больше ничего.

    Отказы проверяются ПОШТУЧНО и исполнением. «Их нет в списке» — не
    проверка: список читает человек, а отказ выдаёт код.
    """
    bound = await _in_review(hub, "своя")
    other = await _in_review(hub, "чужая")
    session = await _reviewer_session(hub, bound)

    brief = await hub.client.get(f"/api/tasks/{bound}/review-brief", headers=session)
    assert brief.status_code == 200, brief.text

    filed = await hub.client.post(
        f"/api/tasks/{bound}/machine-review",
        json={
            "raw_count": 1,
            "incomplete": False,
            "findings_confirmed": [],
            "findings_rejected": [],
        },
        headers=session,
    )
    assert filed.status_code == 200, filed.text

    # Всё остальное по СВОЕЙ задаче — отказ. Особенно submit-review: вердикт
    # проверяемой стороне не принадлежит ни при каких обстоятельствах.
    for method, path in (
        ("POST", f"/api/tasks/{bound}/submit-review"),
        ("POST", f"/api/tasks/{bound}/claim"),
        ("POST", f"/api/tasks/{bound}/pair-start"),
        ("POST", f"/api/tasks/{bound}/updates"),
        ("GET", f"/api/tasks/{bound}"),
        ("GET", "/api/whoami"),
        ("POST", "/api/tasks"),
    ):
        resp = await hub.client.request(method, path, json={}, headers=session)
        assert resp.status_code == 403, f"{method} {path} → {resp.status_code}"

    # Чужая задача — отказ даже по разрешённому маршруту.
    foreign = await hub.client.get(f"/api/tasks/{other}/review-brief", headers=session)
    assert foreign.status_code == 403, foreign.text
    foreign_report = await hub.client.post(
        f"/api/tasks/{other}/machine-review",
        json={"raw_count": 0, "incomplete": False},
        headers=session,
    )
    assert foreign_report.status_code == 403, foreign_report.text


@pytest.mark.asyncio
async def test_reviewer_code_dies_with_its_submission(hub):
    """AC-2 (#1084): код годен, только пока задача на ревью.

    Собственный сторож, а не следствие: без него код, выписанный на сдачу,
    пережил бы её и позволил бы сдать отчёт по работе, которая уже ушла
    дальше — в running после доработки или в completed.
    """
    task_id = await _in_review(hub, "уехала дальше")
    code, _ttl = await cp.issue_code(
        hub.db, hub.human_id, kind="reviewer", bound_task_id=task_id
    )
    await hub.db.execute("UPDATE tasks SET status='running' WHERE id=?", (task_id,))
    await hub.db.commit()

    resp = await hub.client.post(
        "/api/auth/chat-pair/redeem", json={"code": code}, headers=_ip()
    )
    assert resp.status_code == 401, resp.text
    assert resp.json()["detail"]["reason"] == "chat_pair_invalid"
