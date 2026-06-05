"""Tests for multi-user auth with role-based access control.

Covers:

1. **Open mode** (no tokens configured) — every endpoint stays accessible
   and the user is reported as ``anonymous``.
2. **Bearer auth** — with tokens configured, API/JSON requests must
   present ``Authorization: Bearer <token>`` and are rejected 401 otherwise.
3. **Cookie auth + /login flow** — browsers POST the token to ``/login``,
   receive a session cookie, and can then GET HTML pages.
4. **Browser redirect** — an unauthenticated HTML GET returns 303 to
   ``/login?next=...`` (not 401).
5. **Role boundaries** — agent tokens get 403 on human-only endpoints.
6. **Startup guard** — non-loopback bind without auth is rejected.
"""

from __future__ import annotations

import pytest

from hub import config
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
    assert "OpenClaw Hub" in resp.text


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
# /login flow — submit token, receive cookie, browse with cookie
# ---------------------------------------------------------------------------


async def _get_csrf_token(client) -> str:
    """GET /login to obtain a CSRF token for form submissions."""
    resp = await client.get("/login")
    from hub.auth import CSRF_COOKIE_NAME

    csrf = resp.cookies.get(CSRF_COOKIE_NAME, "")
    client.cookies.set(CSRF_COOKIE_NAME, csrf)
    return csrf


@pytest.mark.asyncio
async def test_login_with_valid_token_sets_cookie(client, monkeypatch):
    monkeypatch.setattr(config, "HUB_TOKENS", _tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)

    csrf = await _get_csrf_token(client)
    resp = await client.post(
        "/login",
        data={"token": "secret-token", "next": "/tasks", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["Location"] == "/tasks"
    assert config.HUB_COOKIE_NAME in resp.cookies
    assert resp.cookies[config.HUB_COOKIE_NAME] == "secret-token"


@pytest.mark.asyncio
async def test_login_with_wrong_token_redirects_with_error(client, monkeypatch):
    monkeypatch.setattr(config, "HUB_TOKENS", _tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)

    csrf = await _get_csrf_token(client)
    resp = await client.post(
        "/login",
        data={"token": "WRONG", "next": "/", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    location = resp.headers["Location"]
    assert location.startswith("/login?")
    assert "error=" in location
    assert config.HUB_COOKIE_NAME not in resp.cookies


@pytest.mark.asyncio
async def test_login_without_csrf_is_rejected(client, monkeypatch):
    """POST /login without CSRF token is rejected."""
    monkeypatch.setattr(config, "HUB_TOKENS", _tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)

    resp = await client.post(
        "/login",
        data={"token": "secret-token", "next": "/tasks"},
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
# Open-mode safeguard — explicit AUTH_DISABLED keeps tokens inactive
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_disabled_overrides_tokens(client, monkeypatch):
    """``OPENCLAW_HUB_AUTH_DISABLED=1`` is the documented escape hatch."""
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


# ---------------------------------------------------------------------------
# Role boundaries — agent tokens blocked from human-only operations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_token_cannot_approve(client, monkeypatch):
    """Agent role gets 403 on /approve."""
    monkeypatch.setattr(config, "HUB_TOKENS", _tokens("agent"))
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)

    resp = await client.post(
        "/api/tasks",
        json={"title": "test task"},
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
