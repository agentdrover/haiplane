"""Tests for multi-user auth with role-based access control.

Covers:

1. **Open mode** (no tokens configured) — every endpoint stays accessible
   and the user is reported as ``anonymous``.
2. **Bearer auth** — with tokens configured, API/JSON requests must
   present ``Authorization: Bearer <token>`` and are rejected 401 otherwise.
3. **Cookie auth + /login flow** — browsers POST username/password to
   ``/login``, receive a session cookie, and can then GET HTML pages. An
   env-token presented as a cookie is still resolved by the middleware.
4. **Browser redirect** — an unauthenticated HTML GET returns 303 to
   ``/login?next=...`` (not 401).
5. **Role boundaries** — agent tokens get 403 on human-only endpoints.
6. **Startup guard** — non-loopback bind without auth is rejected.
"""

from __future__ import annotations

import json

import pytest

from hub import brand, config
from hub.config import TokenIdentity

PASSWORD_WITHOUT_DIGIT = "abcdefgh!"  # pragma: allowlist secret
PASSWORD_WITHOUT_LETTER = "12345678!"  # pragma: allowlist secret
PASSWORD_WITHOUT_SPECIAL = "abcdefg1"  # pragma: allowlist secret
VALID_ADMIN_PASSWORD = "s3cur3pw!"  # pragma: allowlist secret


def _tokens(role: str = "human") -> dict[str, TokenIdentity]:
    """Helper to build a HUB_TOKENS dict with the given role."""
    return {"secret-token": TokenIdentity("alice", role)}


# ---------------------------------------------------------------------------
# Open mode (no tokens configured)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_mode_allows_anonymous_api(client, monkeypatch):
    """With no tokens configured, /api/tasks is reachable without auth."""
    monkeypatch.setattr(config, "HUB_TOKENS", {})
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)

    resp = await client.get("/api/tasks")
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_open_mode_dashboard_renders(client, monkeypatch):
    monkeypatch.setattr(config, "HUB_TOKENS", {})
    resp = await client.get("/")
    assert resp.status_code == 200
    assert "Haiplane Hub" in resp.text
    assert ("Open" + "Claw") not in resp.text


# ---------------------------------------------------------------------------
# Bearer auth (REST / MCP clients)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bearer_required_for_api(client, monkeypatch):
    monkeypatch.setattr(config, "HUB_TOKENS", _tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)

    resp = await client.get(
        "/api/tasks",
        headers={"Accept": "application/json"},
    )
    assert resp.status_code == 401
    assert resp.headers.get("WWW-Authenticate", "").startswith("Bearer")
    assert resp.json()["detail"] == "authentication required"


@pytest.mark.asyncio
async def test_valid_bearer_unlocks_api(client, monkeypatch):
    monkeypatch.setattr(config, "HUB_TOKENS", _tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)

    resp = await client.get(
        "/api/tasks",
        headers={"Authorization": "Bearer secret-token"},
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_invalid_bearer_rejected(client, monkeypatch):
    monkeypatch.setattr(config, "HUB_TOKENS", _tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)

    resp = await client.get(
        "/api/tasks",
        headers={
            "Authorization": "Bearer wrong-token",
            "Accept": "application/json",
        },
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Browser redirect — unauthenticated HTML GET → /login
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_html_get_redirects_to_login(client, monkeypatch):
    monkeypatch.setattr(config, "HUB_TOKENS", _tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)

    resp = await client.get(
        "/tasks",
        headers={"Accept": "text/html"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    location = resp.headers["Location"]
    assert location.startswith("/login")
    assert "next=" in location


@pytest.mark.asyncio
async def test_root_html_get_shows_landing_not_login_redirect(client, monkeypatch):
    """#955: корень для анонима — визитка продукта.

    Раньше здесь был 303 на чистый /login (без ?next=/): проверялось, что
    возврат на корень не тащит бессмысленный параметр. Теперь редиректа нет
    вовсе — аноним получает публичную страницу со ссылкой на вход, а данные
    задач ему по-прежнему недоступны (см. тест дашборда ниже).
    """
    monkeypatch.setattr(config, "HUB_TOKENS", _tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)

    resp = await client.get(
        "/",
        headers={"Accept": "text/html"},
        follow_redirects=False,
    )
    assert resp.status_code == 200
    assert 'href="/login"' in resp.text
    assert "docker compose up" in resp.text


@pytest.mark.asyncio
async def test_login_page_is_public(client, monkeypatch):
    """/login itself must be reachable even without a session."""
    monkeypatch.setattr(config, "HUB_TOKENS", _tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)

    resp = await client.get("/login")
    assert resp.status_code == 200
    assert "Sign in" in resp.text


@pytest.mark.asyncio
async def test_healthz_is_public(client, monkeypatch):
    """Liveness probe stays public so VPN / LB checks always work."""
    monkeypatch.setattr(config, "HUB_TOKENS", _tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)

    resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.text == "ok"


# ---------------------------------------------------------------------------
# /login flow — submit username/password, receive cookie, browse with cookie
# ---------------------------------------------------------------------------


async def _get_csrf_token(client) -> str:
    """GET /login to obtain a CSRF token for form submissions."""
    resp = await client.get("/login")
    from hub.auth import CSRF_COOKIE_NAME

    csrf = resp.cookies.get(CSRF_COOKIE_NAME, "")
    client.cookies.set(CSRF_COOKIE_NAME, csrf)
    return csrf


@pytest.mark.asyncio
async def test_login_without_csrf_is_rejected(client, monkeypatch):
    """POST /login without a CSRF token is rejected before credential checks."""
    monkeypatch.setattr(config, "HUB_TOKENS", _tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)

    resp = await client.post(
        "/login",
        data={
            "username": "alice",
            "password": VALID_ADMIN_PASSWORD,
            "next": "/tasks",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "error=" in resp.headers["Location"]
    assert config.HUB_COOKIE_NAME not in resp.cookies


@pytest.mark.asyncio
async def test_cookie_session_unlocks_html(client, monkeypatch):
    monkeypatch.setattr(config, "HUB_TOKENS", _tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)

    client.cookies.set(config.HUB_COOKIE_NAME, "secret-token")
    resp = await client.get("/", headers={"Accept": "text/html"})
    assert resp.status_code == 200
    assert "alice" in resp.text


@pytest.mark.asyncio
async def test_logout_clears_cookie(client, monkeypatch):
    monkeypatch.setattr(config, "HUB_TOKENS", _tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)

    client.cookies.set(config.HUB_COOKIE_NAME, "secret-token")
    resp = await client.post("/logout", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["Location"] == "/login"
    set_cookie = resp.headers.get("set-cookie", "")
    assert config.HUB_COOKIE_NAME in set_cookie


# ---------------------------------------------------------------------------
# Cookie rename (Haiplane rebrand, Wave 3) — dual-accept, dual-delete
# ---------------------------------------------------------------------------


async def _password_user(db, username: str) -> None:
    """A DB principal that can pass the real /login credential check."""
    from hub.services import admin as admin_svc

    await admin_svc.create_principal(
        db,
        kind="human",
        username=username,
        password=VALID_ADMIN_PASSWORD,
        role_slug="operator",
    )


def test_cookie_default_is_the_new_name():
    """With no HAIPLANE_HUB_COOKIE override, the default session-cookie
    name is the Haiplane one."""
    assert config.HUB_COOKIE_NAME == brand.COOKIE_NAME
    assert config.HUB_COOKIE_NAME_EXPLICIT is False


@pytest.mark.asyncio
async def test_legacy_session_cookie_no_longer_authenticates(client, monkeypatch):
    """Wave 5: a browser holding only the pre-rename session cookie is
    anonymous — dual-accept is gone."""
    monkeypatch.setattr(config, "HUB_TOKENS", _tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)

    legacy_cookie = "open" + "claw" + "_hub_session"
    client.cookies.set(legacy_cookie, "secret-token")
    # Корень с #955 публичен, поэтому проверяем защищённую страницу: важно не
    # то, каким кодом отвечает витрина, а что легаси-кука никуда не пускает.
    resp = await client.get("/tasks", headers={"Accept": "text/html"})
    assert resp.status_code in (303, 401), resp.status_code
    root = await client.get("/", headers={"Accept": "text/html"})
    assert "docker compose up" in root.text, "аноним должен видеть визитку"


@pytest.mark.asyncio
async def test_login_rejects_legacy_csrf_cookie(client, db, monkeypatch):
    """Wave 5: a form token matching only the pre-rename CSRF cookie is
    an invalid submission — verify-both is gone."""
    monkeypatch.setattr(config, "HUB_TOKENS", _tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    await _password_user(db, "dana")

    legacy_csrf = "open" + "claw" + "_csrf"
    client.cookies.set(legacy_csrf, "legacy-form-token")
    resp = await client.post(
        "/login",
        data={
            "username": "dana",
            "password": VALID_ADMIN_PASSWORD,
            "csrf_token": "legacy-form-token",
            "next": "/tasks",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "error=" in resp.headers["Location"], resp.headers["Location"]
    assert config.HUB_COOKIE_NAME not in resp.cookies


@pytest.mark.asyncio
async def test_login_and_logout_delete_canonical_csrf_cookie(client, db, monkeypatch):
    """haiplane_csrf is expired on login success and on logout."""
    monkeypatch.setattr(config, "HUB_TOKENS", _tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    await _password_user(db, "fred")

    client.cookies.set(brand.CSRF_COOKIE_NAME, "fresh-token")
    login = await client.post(
        "/login",
        data={
            "username": "fred",
            "password": VALID_ADMIN_PASSWORD,
            "csrf_token": "fresh-token",
            "next": "/",
        },
        follow_redirects=False,
    )
    assert login.status_code == 303
    assert "error=" not in login.headers["Location"]
    deleted = login.headers.get_list("set-cookie")
    assert any(c.startswith(f"{brand.CSRF_COOKIE_NAME}=") for c in deleted), deleted

    logout = await client.post("/logout", follow_redirects=False)
    deleted = logout.headers.get_list("set-cookie")
    assert any(c.startswith(f"{brand.CSRF_COOKIE_NAME}=") for c in deleted), deleted


# ---------------------------------------------------------------------------
# Open-mode safeguard — explicit AUTH_DISABLED keeps tokens inactive
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_disabled_overrides_tokens(client, monkeypatch):
    """``HAIPLANE_HUB_AUTH_DISABLED=1`` is the documented escape hatch."""
    monkeypatch.setattr(config, "HUB_TOKENS", _tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", True)

    resp = await client.get("/api/tasks")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Token parser — env format edge cases
# ---------------------------------------------------------------------------


def test_parse_tokens_basic():
    out = config.parse_tokens("alice:s3cret,bob:hunter2")
    assert out["s3cret"].username == "alice"
    assert out["s3cret"].role == "human"
    assert out["hunter2"].username == "bob"


def test_parse_tokens_with_roles():
    out = config.parse_tokens("alice:tok1:human,bot:tok2:agent,admin:tok3:admin")
    assert out["tok1"].username == "alice"
    assert out["tok1"].role == "human"
    assert out["tok2"].username == "bot"
    assert out["tok2"].role == "agent"
    assert out["tok3"].username == "admin"
    assert out["tok3"].role == "admin"


def test_parse_tokens_invalid_role_defaults_to_human():
    out = config.parse_tokens("alice:tok1:superuser")
    assert out["tok1"].role == "human"


def test_parse_tokens_tolerates_whitespace_and_blanks():
    out = config.parse_tokens(" alice : a , , bob:b ,broken,:onlyvalue,name:")
    assert out["a"].username == "alice"
    assert out["b"].username == "bob"
    assert len(out) == 2


def test_parse_tokens_empty_returns_empty_dict():
    assert config.parse_tokens("") == {}
    assert config.parse_tokens("   ") == {}


def test_parse_tokens_multiple_agent_identities():
    """Reviewer identity provisioning (#432): several tokens may share the
    ``agent`` role while remaining distinct principals by name."""
    out = config.parse_tokens("cursor:tok-a:agent,cursor-reviewer:tok-b:agent")
    assert out["tok-a"].username == "cursor"
    assert out["tok-a"].role == "agent"
    assert out["tok-b"].username == "cursor-reviewer"
    assert out["tok-b"].role == "agent"
    assert out["tok-a"].username != out["tok-b"].username


# ---------------------------------------------------------------------------
# Role boundaries — agent tokens blocked from human-only operations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_token_cannot_approve(client, monkeypatch):
    """Agent role gets 403 on /approve."""
    monkeypatch.setattr(config, "HUB_TOKENS", _tokens("agent"))
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)

    # source=agent because that is all an agent may create (#360); the subject
    # here is the approve gate, and any task will do.
    resp = await client.post(
        "/api/tasks",
        json={"title": "test task", "source": "agent"},
        headers={"Authorization": "Bearer secret-token"},
    )
    task_id = resp.json()["id"]

    resp = await client.post(
        f"/api/tasks/{task_id}/approve",
        json={},
        headers={"Authorization": "Bearer secret-token"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_agent_token_cannot_start(client, monkeypatch):
    monkeypatch.setattr(config, "HUB_TOKENS", _tokens("agent"))
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)

    resp = await client.post(
        "/api/tasks/{0}/start".format(999),
        json={},
        headers={"Authorization": "Bearer secret-token"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_agent_token_cannot_decide(client, monkeypatch):
    monkeypatch.setattr(config, "HUB_TOKENS", _tokens("agent"))
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)

    resp = await client.post(
        "/api/tasks/999/decide",
        json={"action": "accept"},
        headers={"Authorization": "Bearer secret-token"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_agent_token_cannot_force_complete(client, monkeypatch):
    monkeypatch.setattr(config, "HUB_TOKENS", _tokens("agent"))
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)

    resp = await client.post(
        "/api/tasks/999/force-complete",
        json={},
        headers={"Authorization": "Bearer secret-token"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_agent_token_archive_actionable_error(client, monkeypatch):
    monkeypatch.setattr(config, "HUB_TOKENS", _tokens("agent"))
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)

    create = await client.post(
        "/api/tasks",
        json={"title": "drafty", "source": "agent"},
        headers={"Authorization": "Bearer secret-token"},
    )
    task_id = create.json()["id"]

    resp = await client.post(
        f"/api/tasks/{task_id}/archive",
        json={"cascade": False},
        headers={"Authorization": "Bearer secret-token"},
    )
    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert detail["reason"] == "permission_denied"
    assert detail["required_role"] == "human"
    assert detail["suggested_tool"] == "hub_withdraw_own_draft"
    assert detail["hint"]


@pytest.mark.asyncio
async def test_agent_token_can_withdraw_own_draft(client, monkeypatch):
    monkeypatch.setattr(
        config,
        "HUB_TOKENS",
        {
            "agent-token": TokenIdentity("bot", "agent"),
            "other-agent": TokenIdentity("other", "agent"),
        },
    )
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)

    create = await client.post(
        "/api/tasks",
        json={"title": "my draft", "source": "agent", "agent": "bot"},
        headers={"Authorization": "Bearer agent-token"},
    )
    task_id = create.json()["id"]

    resp = await client.post(
        f"/api/tasks/{task_id}/withdraw",
        headers={"Authorization": "Bearer agent-token"},
    )
    assert resp.status_code == 200
    assert resp.json()["archived"] is True


@pytest.mark.asyncio
async def test_agent_token_cannot_withdraw_foreign_draft(client, monkeypatch):
    monkeypatch.setattr(
        config,
        "HUB_TOKENS",
        {
            "agent-token": TokenIdentity("bot", "agent"),
            "other-agent": TokenIdentity("other", "agent"),
        },
    )
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)

    create = await client.post(
        "/api/tasks",
        json={"title": "bot draft", "source": "agent", "agent": "bot"},
        headers={"Authorization": "Bearer agent-token"},
    )
    task_id = create.json()["id"]

    resp = await client.post(
        f"/api/tasks/{task_id}/withdraw",
        headers={"Authorization": "Bearer other-agent"},
    )
    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert detail["reason"] == "not_task_owner"
    assert detail["required_role"] == "agent"
    assert detail["hint"]
    assert detail["suggested_tool"] == "hub_withdraw_own_draft"


@pytest.mark.asyncio
async def test_agent_token_withdraw_empty_assigned_agent(client, monkeypatch):
    monkeypatch.setattr(config, "HUB_TOKENS", _tokens("agent"))
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)

    create = await client.post(
        "/api/tasks",
        json={"title": "orphan draft", "source": "agent"},
        headers={"Authorization": "Bearer secret-token"},
    )
    task_id = create.json()["id"]

    resp = await client.post(
        f"/api/tasks/{task_id}/withdraw",
        headers={"Authorization": "Bearer secret-token"},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"]["reason"] == "not_task_owner"


@pytest.mark.asyncio
async def test_agent_token_can_pair_start(client, monkeypatch):
    monkeypatch.setattr(
        config,
        "HUB_TOKENS",
        {
            "human-token": TokenIdentity("denis", "human"),
            "agent-token": TokenIdentity("bot", "agent"),
        },
    )
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)

    resp = await client.post(
        "/api/tasks",
        json={"title": "pair target"},
        headers={"Authorization": "Bearer human-token"},
    )
    assert resp.status_code == 200
    task_id = resp.json()["id"]

    resp = await client.post(
        f"/api/tasks/{task_id}/pair-start",
        # #852: an agent declares the session that takes the task; what this
        # test holds — an agent token may pair-start at all — is unchanged.
        json={"plan": "Plan: work in Cursor", "session_id": "s-bot"},
        headers={"Authorization": "Bearer agent-token"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "running"
    assert data["assigned_agent"] == "bot"
    assert data["job_id"] is None


@pytest.mark.asyncio
async def test_agent_token_can_create_and_update(client, monkeypatch):
    """Agent role can still create tasks and post updates."""
    monkeypatch.setattr(config, "HUB_TOKENS", _tokens("agent"))
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)

    resp = await client.post(
        "/api/tasks",
        json={"title": "agent work", "source": "agent"},
        headers={"Authorization": "Bearer secret-token"},
    )
    assert resp.status_code == 200
    task_id = resp.json()["id"]

    resp = await client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": "bot", "kind": "status", "content": "working..."},
        headers={"Authorization": "Bearer secret-token"},
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_human_token_can_approve(client, monkeypatch):
    """Human role can approve draft tasks."""
    monkeypatch.setattr(config, "HUB_TOKENS", _tokens("human"))
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)

    resp = await client.post(
        "/api/tasks",
        json={"title": "test task", "source": "agent", "agent": "bot"},
        headers={"Authorization": "Bearer secret-token"},
    )
    task_id = resp.json()["id"]
    assert resp.json()["status"] == "draft"

    resp = await client.post(
        f"/api/tasks/{task_id}/approve",
        json={"force": True},
        headers={"Authorization": "Bearer secret-token"},
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Startup guard — non-loopback bind without auth
# ---------------------------------------------------------------------------


def test_startup_guard_rejects_open_network(monkeypatch):
    monkeypatch.setattr(config, "HUB_HOST", "0.0.0.0")
    monkeypatch.setattr(config, "HUB_TOKENS", {})
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    monkeypatch.setattr(config, "HUB_ALLOW_UNAUTH_NETWORK", False)

    with pytest.raises(RuntimeError, match="Refusing to bind"):
        config.validate_network_auth()


def test_startup_guard_allows_localhost(monkeypatch):
    monkeypatch.setattr(config, "HUB_HOST", "127.0.0.1")
    monkeypatch.setattr(config, "HUB_TOKENS", {})
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    monkeypatch.setattr(config, "HUB_ALLOW_UNAUTH_NETWORK", False)

    config.validate_network_auth()  # no exception


def test_startup_guard_allows_network_with_tokens(monkeypatch):
    monkeypatch.setattr(config, "HUB_HOST", "0.0.0.0")
    monkeypatch.setattr(config, "HUB_TOKENS", _tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    monkeypatch.setattr(config, "HUB_ALLOW_UNAUTH_NETWORK", False)

    config.validate_network_auth()  # no exception


def test_startup_guard_allows_explicit_override(monkeypatch):
    monkeypatch.setattr(config, "HUB_HOST", "0.0.0.0")
    monkeypatch.setattr(config, "HUB_TOKENS", {})
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    monkeypatch.setattr(config, "HUB_ALLOW_UNAUTH_NETWORK", True)

    config.validate_network_auth()  # no exception


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


def test_rate_limiter_blocks_after_threshold():
    from hub.auth import LoginRateLimiter

    rl = LoginRateLimiter(max_attempts=3, window_seconds=60)
    assert not rl.is_blocked("1.2.3.4")
    rl.record("1.2.3.4")
    rl.record("1.2.3.4")
    rl.record("1.2.3.4")
    assert rl.is_blocked("1.2.3.4")
    assert not rl.is_blocked("5.6.7.8")


def test_rate_limiter_cleanup():
    from hub.auth import LoginRateLimiter

    rl = LoginRateLimiter(max_attempts=3, window_seconds=0)
    rl.record("1.2.3.4")
    rl._cleanup()
    assert not rl.is_blocked("1.2.3.4")


# ---------------------------------------------------------------------------
# Password complexity
# ---------------------------------------------------------------------------


def test_password_complexity_rejects_no_digit():
    from pydantic import ValidationError

    from hub.models import AdminBootstrap

    with pytest.raises(ValidationError, match="digit"):
        AdminBootstrap(username="admin", password=PASSWORD_WITHOUT_DIGIT)


def test_password_complexity_rejects_no_letter():
    from pydantic import ValidationError

    from hub.models import AdminBootstrap

    with pytest.raises(ValidationError, match="letter"):
        AdminBootstrap(username="admin", password=PASSWORD_WITHOUT_LETTER)


def test_password_complexity_rejects_no_special():
    from pydantic import ValidationError

    from hub.models import AdminBootstrap

    with pytest.raises(ValidationError, match="special"):
        AdminBootstrap(username="admin", password=PASSWORD_WITHOUT_SPECIAL)


def test_password_complexity_accepts_valid():
    from hub.models import AdminBootstrap

    b = AdminBootstrap(
        username="admin",
        password=VALID_ADMIN_PASSWORD,
    )
    assert b.password == VALID_ADMIN_PASSWORD


# ---- a permission list that tells the truth about what it gates (#614) ----
#
# Handing a permission out in a role, showing it in the admin UI, and checking it
# in code are three different things, and only the first two were visible. Nine
# of eighteen permissions were consulted by nothing — so a role looked narrow
# while its narrowness was decorative. Human gates were never open (they are held
# by require_human_or_admin, _reject_agent_authored_source and the review gate),
# but the list promised granularity that did not exist: in #613 the ci_runner
# role was described to the owner as unable to do anything but report, and the CI
# token could in fact file drafts, because tasks.create is asked by nobody.
#
# The classification lives in hub/db.py; these tests derive the real answer FROM
# THE CODE, so the two cannot drift apart quietly.

_SOURCE_FILES = ("hub/app.py", "hub/web.py", "hub/mcp_server.py", "hub/cli.py")
# Permissions consulted without require_permission: config.py reads these two
# directly to answer is_admin / is_human, and require_human_or_admin is built on
# is_human. Listed explicitly because a regex over require_permission would
# otherwise file tasks.human_gate as decorative — which would be wrong.
_INDIRECT_SOURCES = {"hub/config.py": ("admin.read", "tasks.human_gate")}


def _permissions_enforced_in_code() -> set[str]:
    """Every permission the code actually consults, read from the source.

    Parsed as text rather than introspected: the gates live in decorators and in
    Depends(...) defaults, where inspect cannot see them.
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    found: set[str] = set()
    for name in _SOURCE_FILES:
        text = (root / name).read_text()
        found |= set(re.findall(r'require_permission\(\s*"([a-z._]+)"', text))
    for name, perms in _INDIRECT_SOURCES.items():
        text = (root / name).read_text()
        for perm in perms:
            if f'"{perm}"' in text:
                found.add(perm)
    return found


def test_every_permission_is_classified_as_enforced_or_decorative():
    # AC-1 (#614): the split covers the whole list, without overlap, and matches
    # what the code does. Derived from source on purpose — two hand-written lists
    # agreeing with each other and both wrong is exactly the defect being fixed.
    from hub.db import (
        ALL_PERMISSIONS,
        DECLARED_ONLY_PERMISSIONS,
        ENFORCED_PERMISSIONS,
    )

    declared = set(ALL_PERMISSIONS)
    enforced = set(ENFORCED_PERMISSIONS)
    decorative = set(DECLARED_ONLY_PERMISSIONS)

    assert enforced | decorative == declared, (
        "every declared permission must be classified: "
        f"unclassified={sorted(declared - enforced - decorative)}, "
        f"invented={sorted((enforced | decorative) - declared)}"
    )
    assert not (enforced & decorative), sorted(enforced & decorative)

    in_code = _permissions_enforced_in_code()
    assert in_code, "the parser found nothing — it would then agree with anything"
    assert in_code == enforced, (
        "the classification disagrees with the code: "
        f"gating but called decorative={sorted(in_code - enforced)}, "
        f"listed as enforced but gating nothing={sorted(enforced - in_code)}"
    )


def test_a_new_permission_must_be_classified():
    # AC-2 (#614): a permission added to the list and to neither bucket has to
    # fail loudly. Otherwise the classification rots the moment someone adds the
    # nineteenth permission — which is how the original defect arrived.
    from hub.db import DECLARED_ONLY_PERMISSIONS, ENFORCED_PERMISSIONS

    declared = set(ALL_PERMISSIONS_WITH("tasks.brand_new"))
    classified = set(ENFORCED_PERMISSIONS) | set(DECLARED_ONLY_PERMISSIONS)
    assert declared - classified == {"tasks.brand_new"}, (
        "the check must notice an unclassified permission, and name it"
    )


def ALL_PERMISSIONS_WITH(extra: str) -> tuple[str, ...]:
    """The declared list plus one hypothetical permission (test helper)."""
    from hub.db import ALL_PERMISSIONS

    return (*ALL_PERMISSIONS, extra)


def test_a_decorative_permission_that_starts_gating_fails_the_test():
    # AC-3 (#614): the other direction, and the one that rots silently. If a
    # permission listed as decorative appears in a gate, the label is stale — the
    # same way #610's comment claimed for months that the score looked at
    # mitigation while the code never did.
    from hub.db import DECLARED_ONLY_PERMISSIONS

    in_code = _permissions_enforced_in_code()
    now_gating = in_code & set(DECLARED_ONLY_PERMISSIONS)
    assert not now_gating, (
        "these are marked as gating nothing but appear in a permission check — "
        f"move them to ENFORCED_PERMISSIONS: {sorted(now_gating)}"
    )


def test_the_classification_names_the_permissions_from_the_incident():
    # The two facts that made this task: tasks.create gates nothing (so the CI
    # token could file drafts, contrary to what #613's report claimed), and
    # tasks.ci_report does gate (the intake added in #546 is real).
    from hub.db import DECLARED_ONLY_PERMISSIONS, ENFORCED_PERMISSIONS

    assert "tasks.create" in DECLARED_ONLY_PERMISSIONS
    assert "tasks.ci_report" in ENFORCED_PERMISSIONS
    assert "tasks.human_gate" in ENFORCED_PERMISSIONS, (
        "it gates through is_human, one hop away — decorative would be wrong"
    )


# ---------------------------------------------------------------------------
# watcher: read-only role (#1556)
# ---------------------------------------------------------------------------


@pytest.fixture
async def watcher_hub(client, db, monkeypatch):
    """Auth on; a human who makes a task and a DB principal with role watcher."""
    from types import SimpleNamespace

    from hub.services import admin as admin_svc

    # Auth is on only when some token exists: an empty map means open mode.
    monkeypatch.setattr(
        config, "HUB_TOKENS", {"unused-env-token": TokenIdentity("env-x", "human")}
    )
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    human = await admin_svc.create_principal(
        db, kind="human", username="alice-w", role_slug="operator"
    )
    human_key = await admin_svc.create_api_key(db, human["id"], name="laptop")
    watcher = await admin_svc.create_principal(
        db, kind="agent", username="grok-w", role_slug="watcher"
    )
    watcher_key = await admin_svc.create_api_key(db, watcher["id"], name="w")
    return SimpleNamespace(
        client=client,
        db=db,
        human={"Authorization": f"Bearer {human_key['plaintext_key']}"},
        watcher={"Authorization": f"Bearer {watcher_key['plaintext_key']}"},
        watcher_key=watcher_key["plaintext_key"],
    )


async def _task_state(hub, task_id: int) -> tuple:
    rows = await hub.db.execute_fetchall(
        "SELECT title, status, assigned_agent, claimed_by FROM tasks WHERE id = ?",
        (task_id,),
    )
    n = await hub.db.execute_fetchall(
        "SELECT COUNT(*) FROM task_updates WHERE task_id = ?", (task_id,)
    )
    return (tuple(rows[0]), n[0][0])


@pytest.mark.asyncio
async def test_watcher_reads_api_and_every_write_is_refused(watcher_hub):
    """AC-1 (#1556): GET /api/* works; everything else, even unknown, is 403."""
    hub = watcher_hub
    created = await hub.client.post(
        "/api/tasks", json={"title": "для сторожа"}, headers=hub.human
    )
    assert created.status_code in (200, 201), created.text
    tid = created.json()["id"]
    before = await _task_state(hub, tid)

    for path in (f"/api/tasks/{tid}", "/api/metrics/practices"):
        ok = await hub.client.get(path, headers=hub.watcher)
        assert ok.status_code == 200, (path, ok.status_code, ok.text)
    head = await hub.client.head(f"/api/tasks/{tid}", headers=hub.watcher)
    assert head.status_code != 403

    refused = [
        ("POST", f"/api/tasks/{tid}/claim", {}),
        ("PATCH", f"/api/tasks/{tid}", {"title": "взломано"}),
        ("POST", f"/api/tasks/{tid}/updates", {"content": "x", "agent": "w"}),
        ("POST", "/api/tasks", {"title": "новая"}),
        ("DELETE", f"/api/tasks/{tid}", None),
        ("PUT", f"/api/tasks/{tid}", {}),
        ("GET", "/admin", None),
        ("GET", "/admin/anything", None),
        ("GET", "/api/no-such-route-write", None),
        ("POST", "/api/no-such-route-write", {}),
        ("POST", "/login", {}),
    ]
    for method, path, body in refused:
        kwargs = {"headers": hub.watcher}
        if body is not None:
            kwargs["json"] = body
        resp = await hub.client.request(method, path, **kwargs)
        expect = 403
        # An unknown GET under /api is allowed by the list and then 404s in routing;
        # the list is by method+path shape, never by "does the route exist".
        if (method, path) == ("GET", "/api/no-such-route-write"):
            expect = 404
        assert resp.status_code == expect, (method, path, resp.status_code, resp.text)
        if expect == 403:
            detail = resp.json()["detail"]
            assert detail["reason"] == "watcher_gate_forbidden", detail
            assert method in detail["message"] and path in detail["message"]

    assert await _task_state(hub, tid) == before
    events = await hub.db.execute_fetchall(
        "SELECT actor, payload FROM events WHERE kind='watcher_route_refused'"
    )
    paths = {json.loads(r["payload"])["path"] for r in events}
    assert f"/api/tasks/{tid}/claim" in paths and "/admin" in paths
    assert {r["actor"] for r in events} == {"grok-w"}


@pytest.mark.asyncio
async def test_watcher_refused_on_public_looking_paths_too(watcher_hub):
    """AC-1 (#1556): the _looks_public branch of the middleware refuses as well."""
    hub = watcher_hub
    for method, path in (("POST", "/login"), ("POST", "/api/admin/bootstrap")):
        resp = await hub.client.request(method, path, headers=hub.watcher, json={})
        assert resp.status_code == 403, (method, path, resp.status_code)


def test_watcher_is_neither_agent_nor_human():
    """AC-2 (#1556): is_watcher only; no 'not an agent = human' branch fires."""
    ident = TokenIdentity("w", "watcher", principal_id=7)
    assert ident.is_watcher is True
    assert ident.is_agent is False
    assert ident.is_human is False
    assert ident.is_admin is False
    assert ident.is_steward is False
    for perm in (
        "tasks.create",
        "tasks.refine",
        "tasks.update",
        "tasks.agent_report",
        "tasks.human_gate",
        "tasks.decision",
        "tasks.archive",
        "tasks.delete",
        "admin.read",
        "admin.users.write",
    ):
        assert ident.has_permission(perm) is False, perm
    # even when the DB role hands it a permission set
    seeded = TokenIdentity(
        "w", "watcher", principal_id=7, permissions=frozenset({"tasks.read"})
    )
    assert seeded.has_permission("tasks.read") is True
    assert seeded.has_permission("tasks.update") is False
    # a stored permission set that names a human gate does not make it a human
    loaded = TokenIdentity(
        "w",
        "watcher",
        principal_id=7,
        permissions=frozenset({"tasks.read", "tasks.human_gate", "tasks.update"}),
    )
    assert (loaded.is_human, loaded.is_agent) == (False, False)
    assert loaded.has_permission("tasks.human_gate") is False
    assert loaded.has_permission("tasks.update") is False
    # a plain agent / human are untouched
    assert TokenIdentity("a", "agent", principal_id=1).is_watcher is False


@pytest.mark.asyncio
async def test_db_watcher_principal_does_not_resolve_as_human(watcher_hub):
    """AC-2 (#1556): without the role in the priority list it fell through to human."""
    from hub.services import admin as admin_svc

    ident = await admin_svc.resolve_api_key(watcher_hub.db, watcher_hub.watcher_key)
    assert ident is not None
    assert ident.role == "watcher"
    assert (ident.is_watcher, ident.is_human, ident.is_agent) == (True, False, False)


# ---------------------------------------------------------------------------
# ci_runner is a machine, not a human (#1639)
# ---------------------------------------------------------------------------


def _human_gate_routes() -> list[tuple[str, str]]:
    """Every (method, path) guarded by ``require_human_or_admin``, from the app.

    Two shapes carry the guard: ``Depends(require_human_or_admin)`` (found by
    walking each route's dependant tree) and a direct call inside the handler
    (found in the handler source). Derived, not listed by hand: a gate added
    next month is covered without anyone remembering to extend a list.
    """
    import inspect

    from fastapi.routing import APIRoute

    from hub.app import app
    from hub.auth import require_human_or_admin

    def _depends_on_gate(dependant) -> bool:
        return any(
            sub.call is require_human_or_admin or _depends_on_gate(sub)
            for sub in dependant.dependencies
        )

    found: set[tuple[str, str]] = set()
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        guarded = _depends_on_gate(route.dependant)
        if not guarded:
            try:
                guarded = "require_human_or_admin(" in inspect.getsource(route.endpoint)
            except (OSError, TypeError):
                guarded = False
        if guarded:
            for method in route.methods - {"HEAD", "OPTIONS"}:
                found.add((method, route.path))
    return sorted(found)


def _fill_path(path: str) -> str:
    import re

    return re.sub(r"\{[^}:]+(:path)?\}", "1", path)


# Every table a human-gate operation could write to: tasks and their feed,
# criteria, dependencies, projects and the scheduled policy, skills, delivery
# and dispositions, executor state, pairing, messages. No table is skipped when
# it is missing: a renamed table must fail this test, not drop out of it.
_GATE_TABLES = (
    "tasks",
    "task_updates",
    "acceptance_criteria",
    "task_dependencies",
    "projects",
    "scheduled_policy_changes",
    "skills",
    "releases",
    "ci_run_reports",
    "finding_dispositions",
    "outcome_answers",
    "live_checks",
    "steward_false_approvals",
    "executor_runs",
    "executor_slots",
    "chat_pair_codes",
    "chat_pair_sessions",
    "agent_messages",
)


async def _business_state(db) -> str:
    out = {}
    for table in _GATE_TABLES:
        rows = await db.execute_fetchall(f"SELECT * FROM {table} ORDER BY 1")
        out[table] = [tuple(r) for r in rows]
    return json.dumps(out, default=str, sort_keys=True)


def test_the_human_gate_route_list_is_derived_and_not_trivial():
    routes = _human_gate_routes()
    paths = {p for _m, p in routes}
    assert len(routes) >= 25, routes
    for needle in ("/approve", "/reject", "/decide", "/force-complete", "/start"):
        assert any(p.endswith(needle) for p in paths), needle


async def test_ci_runner_key_is_refused_by_every_human_route(ci_runner_hub):
    """AC-1 (#1639): a REAL ci_runner DB key, closed mode, every human gate."""
    hub = ci_runner_hub
    created = await hub.client.post(
        "/api/tasks", json={"title": "gate probe"}, headers=hub.human
    )
    assert created.status_code in (200, 201), created.text
    # Slug "1" and id 1 are what the path filler puts into every route, so the
    # routes act on real rows and a leaked call WOULD change something.
    proj = await hub.client.post(
        "/api/projects", json={"slug": "1", "name": "One"}, headers=hub.human
    )
    assert proj.status_code == 200, proj.text
    before = await _business_state(hub.db)
    routes = _human_gate_routes()
    assert routes
    leaked: list[tuple[str, str, int, str]] = []
    for method, path in routes:
        resp = await hub.client.request(
            method,
            _fill_path(path),
            headers=hub.ci,
            json={},
            follow_redirects=False,
        )
        refused = resp.status_code == 403 and "human_only_gate" in resp.text
        if not refused:
            leaked.append((method, path, resp.status_code, resp.text[:120]))
        # After EVERY request, not once at the end: a leak that a later route
        # undoes (or that a later failure hides) must still be seen.
        if await _business_state(hub.db) != before:
            leaked.append((method, path, resp.status_code, "state changed"))
            before = await _business_state(hub.db)
    assert not leaked, leaked
    who = await hub.client.get("/api/whoami", headers=hub.ci)
    assert who.status_code == 200, who.text
    assert who.json()["role"] == "agent", who.json()


async def test_ci_runner_browser_session_is_not_human(ci_runner_hub):
    """AC-1 (#1639): the cookie door (resolve_browser_session) gives no human either."""
    hub = ci_runner_hub
    task = await hub.client.post(
        "/api/tasks", json={"title": "cookie probe"}, headers=hub.human
    )
    tid = task.json()["id"]
    who = await hub.client.get("/api/whoami", headers=hub.ci_cookie)
    assert who.status_code == 200, who.text
    assert who.json()["auth_source"] == "db_session", who.json()
    assert who.json()["role"] == "agent", who.json()

    from hub.services import admin as admin_svc

    token = hub.ci_cookie["Cookie"].split("=", 1)[1]
    identity = await admin_svc.resolve_browser_session(hub.db, token)
    assert identity is not None
    assert identity.is_human is False and identity.is_agent is True

    before = await _business_state(hub.db)
    rest = await hub.client.post(
        f"/api/tasks/{tid}/approve", json={}, headers=hub.ci_cookie
    )
    assert rest.status_code == 403, rest.text
    web = await hub.client.get("/chat-pair", headers=hub.ci_cookie)
    assert web.status_code == 403, web.status_code
    assert await _business_state(hub.db) == before


async def test_ci_runner_may_withdraw_its_own_draft(ci_runner_hub):
    """Accepted consequence (#1639): ci_runner is an agent, so /withdraw is open."""
    hub = ci_runner_hub
    made = await hub.client.post(
        "/api/tasks",
        json={"title": "ci draft", "source": "agent", "agent": "ci-1639"},
        headers=hub.ci,
    )
    assert made.status_code in (200, 201), made.text
    tid = made.json()["id"]
    resp = await hub.client.post(f"/api/tasks/{tid}/withdraw", headers=hub.ci)
    assert resp.status_code == 200, resp.text
    assert resp.json()["archived"] is True


# ---------------------------------------------------------------------------
# agent-bootstrap: матрица ролей (#1631)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_bootstrap_role_matrix(client, db, monkeypatch):
    """AC-4: anonymous 401, агент и watcher 200, implementer и steward 403, нет slug 404."""
    from hub import repository as repo
    from hub.services import admin as admin_svc

    monkeypatch.setattr(
        config,
        "HUB_TOKENS",
        {
            "agent-env": TokenIdentity("bot", "agent"),
            "steward-env": TokenIdentity("stew", "steward"),
        },
    )
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    await repo.create_project(db, slug="matrix", name="Matrix")
    await db.commit()
    human = await admin_svc.create_principal(
        db, kind="human", username="alice-m", role_slug="operator"
    )
    human_key = await admin_svc.create_api_key(db, human["id"], name="laptop")
    watcher = await admin_svc.create_principal(
        db, kind="agent", username="grok-m", role_slug="watcher"
    )
    watcher_key = await admin_svc.create_api_key(db, watcher["id"], name="w")
    human_auth = {"Authorization": f"Bearer {human_key['plaintext_key']}"}
    ip = {"x-forwarded-for": "203.0.113.9"}
    task = await client.post(
        "/api/tasks", json={"title": "для сессии"}, headers=human_auth
    )
    assert task.status_code in (200, 201), task.text
    issued = await client.post(
        "/api/auth/chat-pair/start",
        json={"kind": "implementer", "task_id": task.json()["id"]},
        headers={**human_auth, **ip},
    )
    assert issued.status_code == 200, issued.text
    redeemed = await client.post(
        "/api/auth/chat-pair/redeem", json={"code": issued.json()["code"]}, headers=ip
    )
    assert redeemed.status_code == 200, redeemed.text

    def bearer(token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    url = "/api/projects/matrix/agent-bootstrap"
    got = {
        "anonymous": (await client.get(url, headers={"Accept": "application/json"})),
        "agent": await client.get(url, headers=bearer("agent-env")),
        "watcher": await client.get(url, headers=bearer(watcher_key["plaintext_key"])),
        "implementer": await client.get(url, headers=bearer(redeemed.json()["token"])),
        "steward": await client.get(url, headers=bearer("steward-env")),
        "unknown": await client.get(
            "/api/projects/no-such/agent-bootstrap", headers=bearer("agent-env")
        ),
    }
    assert {k: v.status_code for k, v in got.items()} == {
        "anonymous": 401,
        "agent": 200,
        "watcher": 200,
        "implementer": 403,
        "steward": 403,
        "unknown": 404,
    }


# ---------------------------------------------------------------------------
# run-validation / run-ac-tests: исполнение на хосте только для людей (#1646)
# ---------------------------------------------------------------------------

_RUN_ROUTES = ("run-validation", "run-ac-tests")


@pytest.fixture
async def run_routes_hub(client, db, monkeypatch):
    """Closed mode, REAL DB keys of every kind, a task with prior results.

    Both default runners are replaced by recorders: the test never executes a
    command or a test, and ``calls`` shows whether a route reached the runner.
    """
    from types import SimpleNamespace

    from hub import repository as repo
    from hub.models import AcceptanceCriterion
    from hub.services import ac_tests, validation_run
    from hub.services import admin as admin_svc

    monkeypatch.setattr(
        config,
        "HUB_TOKENS",
        {"steward-env": TokenIdentity("stew", "steward")},
    )
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    monkeypatch.delenv("HAIPLANE_HUB_TOKEN", raising=False)
    calls: list[str] = []

    async def fake_tests(nodeids, repo_path):
        calls.append("ac-tests")
        return {n: True for n in nodeids}

    async def fake_validation(commands, repo_path):
        calls.append("validation")
        return (0, "ran")

    monkeypatch.setattr(ac_tests, "default_test_runner", fake_tests)
    monkeypatch.setattr(validation_run, "default_validation_runner", fake_validation)

    keys: dict[str, dict[str, str]] = {}
    for label, kind, role in (
        ("human", "human", "operator"),
        ("agent", "agent", "agent"),
        ("ci_runner", "service", "ci_runner"),
        ("watcher", "agent", "watcher"),
    ):
        principal = await admin_svc.create_principal(
            db, kind=kind, username=f"{label}-1646", role_slug=role
        )
        key = await admin_svc.create_api_key(db, principal["id"], name=label)
        keys[label] = {"Authorization": f"Bearer {key['plaintext_key']}"}
    keys["steward"] = {"Authorization": "Bearer steward-env"}

    task_id = await repo.create_task(
        db,
        title="прежние результаты",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="dev",
        rationale="",
        status="running",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await repo.bump_submission_generation(db, task_id)
    await repo.replace_acceptance_criteria(
        db,
        task_id,
        [
            AcceptanceCriterion(
                id="AC-1",
                given="g",
                when="w",
                then="t",
                verifiable_by="test",
                test_ref="tests/test_x.py::test_a",
            )
        ],
    )
    await repo.update_task(
        db,
        task_id,
        validation_commands=json.dumps(["echo old"]),
        validation_generation=1,
        validation_status="fail",
        validation_log="old log",
    )
    await repo.upsert_ac_test_result(db, task_id, "AC-1", 1, "fail")
    await db.commit()

    ip = {"x-forwarded-for": "203.0.113.46"}
    # Pairing is issued only for an open task, so it gets its own.
    open_task = await client.post(
        "/api/tasks", json={"title": "для сессии"}, headers=keys["human"]
    )
    assert open_task.status_code in (200, 201), open_task.text
    issued = await client.post(
        "/api/auth/chat-pair/start",
        json={"kind": "implementer", "task_id": open_task.json()["id"]},
        headers={**keys["human"], **ip},
    )
    assert issued.status_code == 200, issued.text
    redeemed = await client.post(
        "/api/auth/chat-pair/redeem", json={"code": issued.json()["code"]}, headers=ip
    )
    assert redeemed.status_code == 200, redeemed.text
    keys["chat_pair"] = {"Authorization": f"Bearer {redeemed.json()['token']}"}
    return SimpleNamespace(
        client=client, db=db, task_id=task_id, keys=keys, calls=calls
    )


async def _run_routes_state(hub) -> str:
    rows = await hub.db.execute_fetchall(
        "SELECT validation_commands, validation_generation, validation_status, "
        "validation_log FROM tasks WHERE id = ?",
        (hub.task_id,),
    )
    results = await hub.db.execute_fetchall(
        "SELECT ac_id, submission_generation, status, created_at "
        "FROM ac_test_results WHERE task_id = ? ORDER BY ac_id",
        (hub.task_id,),
    )
    return json.dumps([[tuple(r) for r in rows], [tuple(r) for r in results]])


async def test_run_validation_and_ac_tests_refuse_machine_keys(run_routes_hub):
    """AC-1 (#1646): agent и ci_runner с настоящими ключами получают 403."""
    hub = run_routes_hub
    before = await _run_routes_state(hub)
    for who in ("agent", "ci_runner"):
        for route in _RUN_ROUTES:
            resp = await hub.client.post(
                f"/api/tasks/{hub.task_id}/{route}", headers=hub.keys[who]
            )
            assert resp.status_code == 403, (who, route, resp.text)
            assert "human_only_gate" in resp.text, (who, route, resp.text)
            # Отказ раньше поиска задачи: чужой/несуществующий id тоже 403, не 404.
            ghost = await hub.client.post(
                f"/api/tasks/999999/{route}", headers=hub.keys[who]
            )
            assert ghost.status_code == 403, (who, route, ghost.text)
    assert hub.calls == []
    assert await _run_routes_state(hub) == before


async def test_run_validation_and_ac_tests_keep_middleware_refusals(run_routes_hub):
    """AC-2 (#1646): steward, watcher, chat-pair режутся своими кодами middleware."""
    hub = run_routes_hub
    before = await _run_routes_state(hub)
    expected = {
        "steward": "steward_gate_forbidden",
        "watcher": "watcher_gate_forbidden",
        "chat_pair": "chat_pair_gate_forbidden",
    }
    for who, reason in expected.items():
        for route in _RUN_ROUTES:
            resp = await hub.client.post(
                f"/api/tasks/{hub.task_id}/{route}", headers=hub.keys[who]
            )
            assert resp.status_code == 403, (who, route, resp.text)
            assert resp.json()["detail"]["reason"] == reason, (who, route, resp.text)
    assert hub.calls == []
    assert await _run_routes_state(hub) == before


async def test_run_validation_and_ac_tests_still_work_for_humans(run_routes_hub):
    """AC-3 (#1646): человеческий ключ — маршруты работают, runner вызван, результат записан."""
    hub = run_routes_hub
    tests = await hub.client.post(
        f"/api/tasks/{hub.task_id}/run-ac-tests", headers=hub.keys["human"]
    )
    assert tests.status_code == 200, tests.text
    assert [r["status"] for r in tests.json()["results"]] == ["pass"]
    validation = await hub.client.post(
        f"/api/tasks/{hub.task_id}/run-validation", headers=hub.keys["human"]
    )
    assert validation.status_code == 200, validation.text
    assert validation.json()["status"] == "pass"
    assert hub.calls == ["ac-tests", "validation"]
    state = json.loads(await _run_routes_state(hub))
    assert tuple(state[0][0][1:]) == (1, "pass", "ran")
    assert state[1][0][2] == "pass"
