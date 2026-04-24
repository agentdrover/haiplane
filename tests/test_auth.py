"""Tests for the multi-user MVP auth (epic: feat/multi-user-auth).

Covers the four behaviours that matter operationally:

1. **Open mode** (no tokens configured) — every endpoint stays accessible
   and the user is reported as ``anonymous``. This is the default after
   ``OPENCLAW_HUB_TOKENS`` is unset and preserves single-user deploys.
2. **Bearer auth** — with tokens configured, API/JSON requests must
   present ``Authorization: Bearer <token>`` and are rejected 401 otherwise.
3. **Cookie auth + /login flow** — browsers POST the token to ``/login``,
   receive a session cookie, and can then GET HTML pages.
4. **Browser redirect** — an unauthenticated HTML GET returns 303 to
   ``/login?next=...`` (not 401), so the UX is usable without JS.
"""

from __future__ import annotations

import pytest

from hub import config


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
    monkeypatch.setattr(config, "HUB_TOKENS", {"secret-token": "alice"})
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
    monkeypatch.setattr(config, "HUB_TOKENS", {"secret-token": "alice"})
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)

    resp = await client.get(
        "/api/tasks",
        headers={"Authorization": "Bearer secret-token"},
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_invalid_bearer_rejected(client, monkeypatch):
    monkeypatch.setattr(config, "HUB_TOKENS", {"secret-token": "alice"})
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
    monkeypatch.setattr(config, "HUB_TOKENS", {"secret-token": "alice"})
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
    monkeypatch.setattr(config, "HUB_TOKENS", {"secret-token": "alice"})
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)

    resp = await client.get("/login")
    assert resp.status_code == 200
    assert "Sign in" in resp.text


@pytest.mark.asyncio
async def test_healthz_is_public(client, monkeypatch):
    """Liveness probe stays public so VPN / LB checks always work."""
    monkeypatch.setattr(config, "HUB_TOKENS", {"secret-token": "alice"})
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)

    resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.text == "ok"


# ---------------------------------------------------------------------------
# /login flow — submit token, receive cookie, browse with cookie
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_login_with_valid_token_sets_cookie(client, monkeypatch):
    monkeypatch.setattr(config, "HUB_TOKENS", {"secret-token": "alice"})
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)

    resp = await client.post(
        "/login",
        data={"token": "secret-token", "next": "/tasks"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["Location"] == "/tasks"
    assert config.HUB_COOKIE_NAME in resp.cookies
    assert resp.cookies[config.HUB_COOKIE_NAME] == "secret-token"


@pytest.mark.asyncio
async def test_login_with_wrong_token_redirects_with_error(client, monkeypatch):
    monkeypatch.setattr(config, "HUB_TOKENS", {"secret-token": "alice"})
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)

    resp = await client.post(
        "/login",
        data={"token": "WRONG", "next": "/"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    location = resp.headers["Location"]
    assert location.startswith("/login?")
    assert "error=" in location
    assert config.HUB_COOKIE_NAME not in resp.cookies


@pytest.mark.asyncio
async def test_cookie_session_unlocks_html(client, monkeypatch):
    monkeypatch.setattr(config, "HUB_TOKENS", {"secret-token": "alice"})
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)

    client.cookies.set(config.HUB_COOKIE_NAME, "secret-token")
    resp = await client.get("/", headers={"Accept": "text/html"})
    assert resp.status_code == 200
    # Identity chip rendered for non-anonymous user.
    assert "alice" in resp.text


@pytest.mark.asyncio
async def test_logout_clears_cookie(client, monkeypatch):
    monkeypatch.setattr(config, "HUB_TOKENS", {"secret-token": "alice"})
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)

    client.cookies.set(config.HUB_COOKIE_NAME, "secret-token")
    resp = await client.post("/logout", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["Location"] == "/login"
    # Starlette signals deletion with an empty value + Max-Age=0
    set_cookie = resp.headers.get("set-cookie", "")
    assert config.HUB_COOKIE_NAME in set_cookie


# ---------------------------------------------------------------------------
# Open-mode safeguard — explicit AUTH_DISABLED keeps tokens inactive
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_disabled_overrides_tokens(client, monkeypatch):
    """``OPENCLAW_HUB_AUTH_DISABLED=1`` is the documented escape hatch."""
    monkeypatch.setattr(config, "HUB_TOKENS", {"secret-token": "alice"})
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", True)

    resp = await client.get("/api/tasks")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Token parser — env format edge cases
# ---------------------------------------------------------------------------


def test_parse_tokens_basic():
    out = config.parse_tokens("alice:s3cret,bob:hunter2")
    assert out == {"s3cret": "alice", "hunter2": "bob"}


def test_parse_tokens_tolerates_whitespace_and_blanks():
    out = config.parse_tokens(" alice : a , , bob:b ,broken,:onlyvalue,name:")
    assert out == {"a": "alice", "b": "bob"}


def test_parse_tokens_empty_returns_empty_dict():
    assert config.parse_tokens("") == {}
    assert config.parse_tokens("   ") == {}
