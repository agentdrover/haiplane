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
async def test_root_html_get_redirects_to_clean_login_url(client, monkeypatch):
    monkeypatch.setattr(config, "HUB_TOKENS", _tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)

    resp = await client.get(
        "/",
        headers={"Accept": "text/html"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["Location"] == "/login"


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
