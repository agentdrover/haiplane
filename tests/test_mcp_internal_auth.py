"""MCP in-process Bearer propagation for REST calls from tools."""

from __future__ import annotations

import pytest

from hub.mcp_internal_auth import (
    bearer_context_get,
    bearer_context_reset,
    bearer_context_set,
)
from hub.mcp_server import _hub_token


@pytest.fixture(autouse=True)
def _clean_prefixed_env(monkeypatch):
    """Neither prefix may leak in from the developer's shell (Task 4)."""
    monkeypatch.delenv("HAIPLANE_HUB_TOKEN", raising=False)


def test_hub_token_prefers_caller_bearer_over_env(monkeypatch):
    """#1556: a remote caller's bearer beats the service env token.

    Before, env won, so a read-only caller's write went out under the env
    token's rights and the role was a fiction.
    """
    monkeypatch.setenv("HAIPLANE_HUB_TOKEN", "env-secret")
    h = bearer_context_set("ctx-secret")
    try:
        assert _hub_token() == "ctx-secret"
    finally:
        bearer_context_reset(h)


def test_hub_token_uses_env_without_caller_bearer(monkeypatch):
    """#1556: local stdio MCP has no inbound bearer; env stays its credential."""
    monkeypatch.setenv("HAIPLANE_HUB_TOKEN", "env-secret")
    assert _hub_token() == "env-secret"


def test_hub_token_ignores_legacy_env_name(monkeypatch):
    monkeypatch.setenv("HAIPLANE_HUB_TOKEN", "new-secret")
    monkeypatch.setenv("OPEN" + "CLAW" + "_HUB_TOKEN", "old-secret")
    assert _hub_token() == "new-secret"


def test_hub_token_uses_context_when_env_empty(monkeypatch):
    h = bearer_context_set("ctx-only")
    try:
        assert _hub_token() == "ctx-only"
    finally:
        bearer_context_reset(h)


def test_context_reset_restores_previous():
    outer = bearer_context_set("first")
    try:
        inner = bearer_context_set("second")
        try:
            assert bearer_context_get() == "second"
        finally:
            bearer_context_reset(inner)
        assert bearer_context_get() == "first"
    finally:
        bearer_context_reset(outer)


# ---------------------------------------------------------------------------
# watcher over /mcp with an env HUB_TOKEN that CAN write (#1556)
# ---------------------------------------------------------------------------

_WRITER_TOKEN = "service-env-writer-token"  # pragma: allowlist secret


@pytest.fixture
async def watcher_mcp_hub(client, db, monkeypatch):
    """Auth on; service env HUB_TOKEN is an agent that may write; caller is a watcher."""
    from types import SimpleNamespace

    import httpx
    from httpx import ASGITransport

    from hub import config
    from hub.app import app
    from hub.services import admin as admin_svc
    from hub.services import mcp_telemetry

    monkeypatch.setattr(
        config,
        "HUB_TOKENS",
        {_WRITER_TOKEN: config.TokenIdentity("env-writer", "agent", principal_id=5001)},
    )
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    monkeypatch.setenv("HAIPLANE_HUB_TOKEN", _WRITER_TOKEN)

    human = await admin_svc.create_principal(
        db, kind="human", username="alice-m", role_slug="operator"
    )
    human_key = await admin_svc.create_api_key(db, human["id"], name="h")
    watcher = await admin_svc.create_principal(
        db, kind="agent", username="grok-m", role_slug="watcher"
    )
    watcher_key = await admin_svc.create_api_key(db, watcher["id"], name="w")

    real_client = httpx.AsyncClient

    def _in_process(*args, **kwargs):
        kwargs["transport"] = ASGITransport(app=app)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _in_process)
    mcp_telemetry.set_telemetry_sink(db)
    try:
        yield SimpleNamespace(
            client=client,
            db=db,
            human={"Authorization": f"Bearer {human_key['plaintext_key']}"},
            watcher_token=watcher_key["plaintext_key"],
        )
    finally:
        mcp_telemetry.set_telemetry_sink(None)


async def test_watcher_mcp_write_refused_even_with_env_hub_token(watcher_mcp_hub):
    """AC-3 (#1556): read tool works, write tool is 403 under the WATCHER bearer."""
    from tests.test_mcp_server import _call_tool, _hub_process, _rpc_result

    hub = watcher_mcp_hub
    created = await hub.client.post(
        "/api/tasks", json={"title": "для mcp"}, headers=hub.human
    )
    assert created.status_code in (200, 201), created.text
    tid = created.json()["id"]

    async with _hub_process():
        read = await _call_tool(
            hub.client, hub.watcher_token, "hub_task_status", {"task_id": tid}
        )
        result = _rpc_result(read)
        assert result.get("isError") is not True, result
        assert "для mcp" in "".join(p.get("text", "") for p in result["content"])

        write = await _call_tool(
            hub.client,
            hub.watcher_token,
            "hub_task_update",
            {"task_id": tid, "content": "запись сторожа", "agent": "grok-m"},
        )
        wrote = _rpc_result(write)
    text = "".join(p.get("text", "") for p in wrote["content"])
    assert "403" in text or "watcher_gate_forbidden" in text, wrote

    rows = await hub.db.execute_fetchall(
        "SELECT 1 FROM task_updates WHERE task_id = ? AND content = ?",
        (tid, "запись сторожа"),
    )
    assert rows == []
    refusals = await hub.db.execute_fetchall(
        "SELECT actor, payload FROM events WHERE kind = 'watcher_route_refused'"
    )
    # the refused inner REST call carried the WATCHER's bearer, not the env one
    assert [r["actor"] for r in refusals] == ["grok-m"], refusals


async def test_watcher_reads_a_compact_task_status(watcher_mcp_hub):
    """AC-4 (#1613): a watcher gets the compact answer and never POSTs /refresh."""
    from tests.test_mcp_server import _call_tool, _hub_process, _rpc_result

    hub = watcher_mcp_hub
    created = await hub.client.post(
        "/api/tasks", json={"title": "компактно"}, headers=hub.human
    )
    tid = created.json()["id"]
    for i in range(13):
        posted = await hub.client.post(
            f"/api/tasks/{tid}/updates",
            json={"content": f"запись-{i}", "agent": "h", "kind": "status"},
            headers=hub.human,
        )
        assert posted.status_code in (200, 201), posted.text

    async with _hub_process():
        read = await _call_tool(
            hub.client, hub.watcher_token, "hub_task_status", {"task_id": tid}
        )
    result = _rpc_result(read)
    assert result.get("isError") is not True, result
    card = result["structuredContent"]["task"]
    assert card["compact"] is True
    assert card["updates_total"] >= 13
    assert len(card["updates"]) == 10
    text = "".join(p.get("text", "") for p in result["content"])
    assert "[bounded] updates 10/" in text and "updates=-1" in text
    assert "запись-12" in text
    assert not any(u["content"] == "запись-0" for u in card["updates"])
    # A POST /refresh under the watcher bearer would leave a refusal event.
    refusals = await hub.db.execute_fetchall(
        "SELECT 1 FROM events WHERE kind = 'watcher_route_refused'"
    )
    assert refusals == []


# ---------------------------------------------------------------------------
# #1624: the agent catalog has no human-only tools
# ---------------------------------------------------------------------------

_HUMAN_ONLY = (
    "hub_approve_task",
    "hub_reject_task",
    "hub_decide_task",
    "hub_force_complete_task",
    "hub_answer_question",
    "hub_start_task",
)
_REMOVED = (
    "hub_approve_proposal",
    "hub_reject_proposal",
    "hub_submit_steward_judgement",
)
_TOKENS_1624 = {
    "agent": ("t-agent", "agent", 7001, frozenset()),
    "watcher_gate": (
        "t-watcher",
        "watcher",
        7002,
        frozenset({"tasks.read", "tasks.human_gate"}),
    ),
    "human": ("t-human", "human", 7003, frozenset()),
    "admin": ("t-admin", "admin", 7004, frozenset()),
    "super_admin": ("t-super", "super_admin", 7005, frozenset()),
    "custom_gate": (
        "t-custom",
        "agent",
        7006,
        frozenset({"tasks.read", "tasks.human_gate"}),
    ),
}


@pytest.fixture
async def catalog_hub(client, db, monkeypatch):
    """Real app, auth on, one env token per identity under test."""
    import httpx
    from httpx import ASGITransport

    from hub import config
    from hub.app import app
    from hub.services import mcp_telemetry

    monkeypatch.setattr(
        config,
        "HUB_TOKENS",
        {
            tok: config.TokenIdentity(name, role, principal_id=pid, permissions=perms)
            for tok, (name, role, pid, perms) in (
                (v[0], v) for v in _TOKENS_1624.values()
            )
        },
    )
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    monkeypatch.delenv("HAIPLANE_HUB_TOKEN", raising=False)

    seen: list[str] = []
    real_client = httpx.AsyncClient

    class _Counting(ASGITransport):
        async def handle_async_request(self, request):  # type: ignore[override]
            seen.append(f"{request.method} {request.url.path}")
            return await super().handle_async_request(request)

    def _in_process(*args, **kwargs):
        kwargs["transport"] = _Counting(app=app)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _in_process)
    mcp_telemetry.set_telemetry_sink(db)
    try:
        from types import SimpleNamespace

        yield SimpleNamespace(client=client, db=db, rest_calls=seen, config=config)
    finally:
        mcp_telemetry.set_telemetry_sink(None)


async def _tools_list(client, token: str | None) -> set[str]:
    from tests.test_mcp_server import _MCP_HEADERS, _rpc_result

    headers = dict(_MCP_HEADERS)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = await client.post(
        "/mcp",
        headers=headers,
        json={"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}},
    )
    return {t["name"] for t in _rpc_result(resp)["tools"]}


async def test_tools_list_hides_human_only_tools_from_non_humans(catalog_hub):
    """AC-1 (#1624): who sees what comes from the PROTECTED identity, per call."""
    from tests.test_mcp_server import _hub_process

    hub = catalog_hub
    hidden = set(_HUMAN_ONLY) | set(_REMOVED)
    async with _hub_process():
        for key in ("agent", "watcher_gate"):
            names = await _tools_list(hub.client, _TOKENS_1624[key][0])
            assert names, key
            assert not (names & hidden), (key, sorted(names & hidden))
        for key in ("human", "admin", "super_admin", "custom_gate"):
            names = await _tools_list(hub.client, _TOKENS_1624[key][0])
            assert set(_HUMAN_ONLY) <= names, (key, set(_HUMAN_ONLY) - names)
            assert not (names & set(_REMOVED)), key
        # interleaving: nothing leaks from one caller to the next
        order = ["human", "agent", "human", "watcher_gate", "admin", "agent"]
        for key in order:
            names = await _tools_list(hub.client, _TOKENS_1624[key][0])
            sees = set(_HUMAN_ONLY) <= names
            assert sees == (key in ("human", "admin")), key
        # concurrent human / agent requests
        import asyncio

        results = await asyncio.gather(
            *[
                _tools_list(hub.client, _TOKENS_1624[k][0])
                for k in ("human", "agent", "super_admin", "watcher_gate") * 3
            ]
        )
        for k, names in zip(
            ("human", "agent", "super_admin", "watcher_gate") * 3, results
        ):
            assert (set(_HUMAN_ONLY) <= names) == (k in ("human", "super_admin")), k

    # open mode: no identity, agent view
    hub.config.HUB_AUTH_DISABLED = True
    async with _hub_process():
        names = await _tools_list(hub.client, None)
    assert not (names & set(_HUMAN_ONLY)), sorted(names & set(_HUMAN_ONLY))

    # stdio: no inbound request at all, agent view
    from hub.mcp_server import mcp

    stdio_names = {t.name for t in await mcp.list_tools()}
    assert not (stdio_names & hidden)
    assert "hub_pair_start" in stdio_names


async def test_a_hidden_tool_call_from_an_agent_is_refused_with_the_human_path(
    catalog_hub,
):
    """AC-2 (#1624): the refusal is inside the measured path, nothing runs."""
    import json

    from tests.test_mcp_server import _call_tool, _hub_process, _rpc_result

    hub = catalog_hub
    created = await hub.client.post(
        "/api/tasks",
        json={"title": "gate probe"},
        headers={"Authorization": f"Bearer {_TOKENS_1624['human'][0]}"},
    )
    tid = created.json()["id"]
    before = (
        await hub.db.execute_fetchall("SELECT status FROM tasks WHERE id = ?", (tid,))
    )[0]["status"]
    route_markers = {
        "hub_approve_task": "/approve",
        "hub_reject_task": "/reject",
        "hub_decide_task": "/decide",
        "hub_force_complete_task": "/force-complete",
        "hub_answer_question": "/answer",
        "hub_start_task": "/start",
    }
    good_args = {"task_id": tid, "answer": "x", "comment": "x", "decision": "accept"}
    agent = _TOKENS_1624["agent"][0]
    async with _hub_process():
        # (c) a human fills the schema cache first
        assert set(_HUMAN_ONLY) <= await _tools_list(
            hub.client, _TOKENS_1624["human"][0]
        )
        for tool in _HUMAN_ONLY:
            for args in (good_args, {"bogus": 1}):
                hub.rest_calls.clear()
                await hub.db.execute("DELETE FROM mcp_call_events")
                await hub.db.commit()
                result = _rpc_result(await _call_tool(hub.client, agent, tool, args))
                assert result["isError"] is True, (tool, result)
                text = "".join(p.get("text", "") for p in result["content"])
                body = json.loads(text)
                assert body["reason"] == "human_only_gate", (tool, body)
                assert body["actor_hint"] == "human", (tool, body)
                assert route_markers[tool] in body["next_action"], (tool, body)
                assert "/api/tasks/" in body["next_action"], (tool, body)
                assert not [
                    c for c in hub.rest_calls if c.startswith("POST /api/tasks")
                ], (
                    tool,
                    hub.rest_calls,
                )
                rows = await hub.db.execute_fetchall(
                    "SELECT tool, status, principal_role FROM mcp_call_events"
                )
                assert [(r["tool"], r["status"]) for r in rows] == [(tool, "error")], (
                    tool,
                    [dict(r) for r in rows],
                )
    after = (
        await hub.db.execute_fetchall("SELECT status FROM tasks WHERE id = ?", (tid,))
    )[0]["status"]
    assert after == before
    # the human still gets through the same funnel (not refused by the gate)
    async with _hub_process():
        ok = _rpc_result(
            await _call_tool(
                hub.client,
                _TOKENS_1624["human"][0],
                "hub_reject_task",
                {"task_id": tid},
            )
        )
        assert "human_only_gate" not in "".join(
            p.get("text", "") for p in ok["content"]
        )
    # REST regression: the same call by an agent token is 403
    for path in ("approve", "reject", "start"):
        resp = await hub.client.post(
            f"/api/tasks/{tid}/{path}",
            json={},
            headers={"Authorization": f"Bearer {agent}"},
        )
        assert resp.status_code == 403, (path, resp.status_code)


# ---------------------------------------------------------------------------
# #1639: a ci_runner DB key is an agent everywhere a human gate is asked
# ---------------------------------------------------------------------------


async def test_ci_runner_key_gets_the_agent_catalog_and_is_refused_human_tools(
    catalog_hub,
):
    """AC-2 (#1639): REAL ci_runner DB principal, closed mode, every other door."""
    import json

    from hub.services import admin as admin_svc
    from tests.test_mcp_server import _call_tool, _hub_process, _rpc_result

    hub = catalog_hub
    db = hub.db
    human = await admin_svc.create_principal(
        db, kind="human", username="alice-1639", role_slug="operator"
    )
    human_key = (await admin_svc.create_api_key(db, human["id"], name="h"))[
        "plaintext_key"
    ]
    ci = await admin_svc.create_principal(
        db, kind="service", username="ci-1639", role_slug="ci_runner"
    )
    ci_key = (await admin_svc.create_api_key(db, ci["id"], name="ci"))["plaintext_key"]
    await db.commit()
    h_ci = {"Authorization": f"Bearer {ci_key}"}
    h_human = {"Authorization": f"Bearer {human_key}"}
    client = hub.client

    # REST doors that ask "is this a human?" outside require_human_or_admin.
    # 1. _human_door: chat-pair code issuing.
    r = await client.post("/api/auth/chat-pair/start", headers=h_ci)
    assert r.status_code == 403 and "human_only_gate" in r.text, r.text
    # 2. _reject_agent_authored_source: a task labelled source=human.
    r = await client.post(
        "/api/tasks", json={"title": "from ci", "source": "human"}, headers=h_ci
    )
    assert r.status_code == 403, r.text
    assert "agent_create_forbidden" in r.text, r.text
    # 3. an active project is a human privilege: the agent's one is pending.
    r = await client.post(
        "/api/projects", json={"slug": "ci-proj", "name": "CI"}, headers=h_ci
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "pending", r.json()
    # 4. skill publication: a draft, never an active version.
    r = await client.post(
        "/api/skills", json={"name": "ci-skill", "content": "x"}, headers=h_ci
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "draft", r.json()
    # 5. somebody else's thread.
    hub_session = await client.post(
        "/api/sessions/register",
        json={"session_id": "s-human", "model": ""},
        headers=h_human,
    )
    assert hub_session.status_code == 200, hub_session.text
    peer = await client.post(
        "/api/sessions/register",
        json={"session_id": "s-peer", "model": ""},
        headers={"Authorization": f"Bearer {_TOKENS_1624['agent'][0]}"},
    )
    assert peer.status_code == 200, peer.text
    sent = await client.post(
        "/api/messages",
        json={
            "to_kind": "session",
            "to_ref": "s-peer",
            "body": "private",
            "session_id": "s-human",
        },
        headers=h_human,
    )
    assert sent.status_code == 200, sent.text
    thread_id = sent.json()["message"]["thread_id"]
    peek = await client.get(f"/api/messages?thread_id={thread_id}", headers=h_ci)
    assert peek.status_code == 403 and "foreign_thread" in peek.text, peek.text
    assert "private" not in peek.text
    # 6. a web route behind _require_human_web.
    page = await client.get("/chat-pair", headers=h_ci)
    assert page.status_code == 403, page.status_code
    # Identity as the hub resolves it.
    who = await client.get("/api/whoami", headers=h_ci)
    assert who.json()["role"] == "agent", who.json()

    # MCP: the agent catalog, and a refused human-only call that runs nothing.
    created = await client.post(
        "/api/tasks", json={"title": "mcp probe"}, headers=h_human
    )
    tid = created.json()["id"]
    before = (
        await db.execute_fetchall("SELECT status FROM tasks WHERE id = ?", (tid,))
    )[0]["status"]
    async with _hub_process():
        names = await _tools_list(client, ci_key)
        assert names and not (names & (set(_HUMAN_ONLY) | set(_REMOVED))), sorted(
            names & set(_HUMAN_ONLY)
        )
        assert "hub_pair_start" in names
        assert set(_HUMAN_ONLY) <= await _tools_list(client, human_key)
        hub.rest_calls.clear()
        result = _rpc_result(
            await _call_tool(
                client,
                ci_key,
                "hub_decide_task",
                {"task_id": tid, "decision": "accept"},
            )
        )
    assert result["isError"] is True, result
    body = json.loads("".join(p.get("text", "") for p in result["content"]))
    assert body["reason"] == "human_only_gate", body
    assert not [c for c in hub.rest_calls if c.startswith("POST /api/tasks")]
    after = (
        await db.execute_fetchall("SELECT status FROM tasks WHERE id = ?", (tid,))
    )[0]["status"]
    assert after == before
