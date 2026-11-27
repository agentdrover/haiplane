"""Policy briefs through the real context and effective-policy routes."""

from __future__ import annotations

import json

import httpx
import pytest

from hub import auth, config, mcp_server as mcp
from hub import repository as repo
from hub.app import app
from hub.services import admin, ci_trust_warning
from hub.services import effective_policy as policy
from hub.services import (
    steward_act_readiness,
    steward_cap,
    steward_exit,
    steward_shadow,
)
from tests.working_rules_support import task_in_project

SLUG = "brief-lab"
POLICY = f"/api/projects/{SLUG}/effective-policy"
BRIEF_QUERY = b"brief=true"


class RestBridge(httpx.ASGITransport):
    """Drive actual REST from MCP, optionally with the old REST contract."""

    def __init__(self, *, legacy=False, old_context=False):
        super().__init__(app=app, raise_app_exceptions=False)
        self.legacy = legacy
        self.old_context = old_context
        self.policy_requests = []

    def install(self, monkeypatch):
        monkeypatch.setenv("HAIPLANE_HUB_URL", "http://test")
        monkeypatch.delenv("HAIPLANE_HUB_TOKEN", raising=False)
        monkeypatch.setattr(
            mcp,
            "_rest_client",
            lambda timeout: httpx.AsyncClient(transport=self, timeout=timeout),
        )

    async def handle_async_request(self, request):
        if request.url.path.endswith("/effective-policy"):
            self.policy_requests.append(request.url.raw_path)
            if self.legacy:
                request.url = request.url.copy_with(query=None)
        response = await super().handle_async_request(request)
        if self.old_context and request.url.path.endswith("/context"):
            await response.aread()
            body = response.json()
            body.pop("working_rules")
            return httpx.Response(response.status_code, json=body)
        return response


@pytest.fixture
async def scene(client, db, monkeypatch):
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", True)
    monkeypatch.setattr(steward_shadow, "configured_mode", lambda: "shadow")
    task_id = await task_in_project(client, db, slug=SLUG)
    project = await repo.get_project_by_slug(db, SLUG)
    await repo.update_project(
        db,
        project["id"],
        gate_policy=json.dumps({"verdict": "auto", "review": "off"}),
    )
    await db.commit()
    return project["id"], task_id


def _text(result):
    return json.loads(result.content[0].text)["message"]


async def _full(client):
    response = await client.get(POLICY)
    assert response.status_code == 200, response.text
    return response.json()


async def _task_layer(client, task_id, mode):
    response = await client.get(f"/api/tasks/{task_id}/context", params={"mode": mode})
    assert response.status_code == 200, response.text
    return response.json()["working_rules"]["project_policy"]


async def test_policy_brief_baseline(client, scene, monkeypatch):
    """Contract exercised on the actual branch base before changing readers."""
    _, task_id = scene
    expected = [
        f"Policy of project {SLUG}: verdict = auto [project]; "
        "review = dispatch [derived from verdict=auto] (stored off)",
        "Steward: requested shadow, effective shadow — full view: hub_effective_policy",
    ]
    text = "\n".join(expected)
    assert policy.format_policy_brief(await _full(client)) == expected
    for mode in ("summary", "full"):
        assert await _task_layer(client, task_id, mode) == {
            "state": "available",
            "text": text,
            "chars": len(text),
        }
        RestBridge().install(monkeypatch)
        for target in ({"project": SLUG}, {"task_id": task_id}):
            result = await mcp.hub_my_context(**target, mode=mode, max_chars=20_000)
            assert _text(result).count(text) == 1
    started = await client.post(
        f"/api/tasks/{task_id}/pair-start",
        json={
            "plan": "verify policy",
            "assigned_agent": "fixture",
            "git_mode": "remote",
        },
    )
    assert started.status_code == 200, started.text
    assert started.json()["working_rules"]["project_policy"]["text"] == text


async def test_policy_brief_matches_baseline_across_contexts(
    client, db, scene, monkeypatch
):
    project_id, task_id = scene
    assert (await client.get(f"{POLICY}?brief=true")).json().keys() == {
        "slug",
        "keys",
        "steward",
    }
    live = RestBridge()
    old = RestBridge(legacy=True)
    cases = (
        ("off", [], {}),
        ("shadow", [], {"verdict": "auto", "review": "off"}),
        ("act", [], {"verdict": "auto", "review": "off"}),
        ("act", [("fixture", "refused")], {"verdict": "auto", "review": "off"}),
    )

    for requested, refusals, gates in cases:
        monkeypatch.setattr(steward_shadow, "configured_mode", lambda: requested)

        async def read_refusals(_db):
            return refusals

        monkeypatch.setattr(steward_shadow, "act_refusals", read_refusals)
        await repo.update_project(db, project_id, gate_policy=json.dumps(gates))
        await db.commit()
        full = await _full(client)
        lines = policy.format_policy_brief(full)
        text = "\n".join(lines)
        assert ("all keys at defaults" in lines[0]) == (not gates)
        granted = "shadow" if refusals else requested
        assert f"requested {requested}, effective {granted}" in lines[1]
        short = (await client.get(f"{POLICY}?brief=true")).json()
        assert short["steward"] == {"requested": requested, "effective": granted}
        assert policy.format_policy_brief(short) == lines
        for mode in ("summary", "full"):
            layer = await _task_layer(client, task_id, mode)
            assert layer == {"state": "available", "text": text, "chars": len(text)}
            for cap in (20_000, 300):
                for target in ({"project": SLUG}, {"task_id": task_id}):
                    old.install(monkeypatch)
                    with monkeypatch.context() as mp:
                        mp.setattr(policy, "policy_brief", policy.effective_policy)
                        expected = await mcp.hub_my_context(
                            **target, mode=mode, max_chars=cap
                        )
                    live.policy_requests.clear()
                    live.install(monkeypatch)
                    actual = await mcp.hub_my_context(
                        **target, mode=mode, max_chars=cap
                    )
                    assert actual.model_dump() == expected.model_dump()
                    assert live.policy_requests == (
                        [] if "task_id" in target else [f"{POLICY}?brief=true".encode()]
                    )
                    if cap == 20_000:
                        assert _text(actual).count(text) == 1
                    else:
                        assert actual.structuredContent["bounds"]["text_truncated"]

    legacy_context = RestBridge(legacy=True, old_context=True)
    legacy_context.install(monkeypatch)
    result = await mcp.hub_my_context(task_id=task_id, mode="summary", max_chars=20_000)
    assert _text(result).count(text) == 1
    assert legacy_context.policy_requests == [f"{POLICY}?brief=true".encode()]


async def test_policy_brief_keeps_access_and_failure_contracts(
    client, db, scene, monkeypatch
):
    project_id, task_id = scene
    member_row = await admin.create_principal(
        db, username="brief-member", kind="human", role_slug="operator"
    )
    outsider_row = await admin.create_principal(
        db, username="brief-outsider", kind="human", role_slug="operator"
    )
    member_id = member_row["id"]
    await db.execute(
        "INSERT INTO private_project_security(project_id,owner_id) VALUES (?,?)",
        (project_id, member_id),
    )
    await db.execute(
        "INSERT INTO project_memberships(project_id,principal_id,role) VALUES (?,?,'owner')",
        (project_id, member_id),
    )
    await db.commit()
    member = config.TokenIdentity("member", "operator", principal_id=member_id)
    outsider = config.TokenIdentity(
        "outsider", "operator", principal_id=outsider_row["id"]
    )
    agent = config.TokenIdentity("agent", "agent", principal_id=603)
    steward = config.TokenIdentity("steward", "steward", principal_id=604)
    tokens = {
        "member": member,
        "outsider": outsider,
        "agent": agent,
        "steward": steward,
    }
    pairs = {
        kind: config.TokenIdentity(
            kind,
            "agent",
            auth_source="chat_pair",
            chat_pair_kind=kind,
            chat_pair_task_id=task_id,
            chat_pair_generation=1,
        )
        for kind in ("implementer", "reviewer")
    }

    async def pair_identity(_request, bearer):
        return pairs.get(bearer)

    # Use the real middleware with synthetic callers; credentials are fixture data.
    monkeypatch.setattr(auth, "_resolve_chat_pair", pair_identity)
    monkeypatch.setattr(config, "HUB_TOKENS", tokens)
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    for bearer in ("", *tokens, *pairs):
        headers = {"Authorization": f"Bearer {bearer}"} if bearer else {}
        baseline = await client.get(POLICY, headers=headers)
        short = await client.get(f"{POLICY}?brief=true", headers=headers)
        assert short.status_code == baseline.status_code
        if baseline.status_code == 200:
            assert short.json().keys() == {"slug", "keys", "steward"}
            assert policy.format_policy_brief(
                short.json()
            ) == policy.format_policy_brief(baseline.json())
            for suffix in ("?brief=false", "?brief=unknown"):
                assert (
                    await client.get(POLICY + suffix, headers=headers)
                ).json() == baseline.json()
        else:
            assert short.json() == baseline.json()
    assert (await client.get(POLICY)).status_code == 401
    agent_headers = {"Authorization": "Bearer agent"}
    missing = POLICY.replace(SLUG, "missing")
    for suffix in ("", "?brief=true"):
        assert (
            await client.get(missing + suffix, headers=agent_headers)
        ).status_code == 404
    pair_headers = {"Authorization": "Bearer implementer"}
    assert (
        await client.get(f"{POLICY}?brief=true", headers=pair_headers)
    ).status_code == 403
    context = f"/api/tasks/{task_id}/context"
    own = await client.get(context, headers=pair_headers)
    assert own.status_code == 200
    assert own.json()["working_rules"]["project_policy"]["state"] == "available"
    assert (
        await client.get(f"/api/tasks/{task_id + 1}/context", headers=pair_headers)
    ).status_code == 403

    reviewer_headers = {"Authorization": "Bearer reviewer"}
    assert (await client.get(context, headers=reviewer_headers)).status_code == 403
    assert (
        await client.get(f"/api/tasks/{task_id + 1}/context", headers=reviewer_headers)
    ).status_code == 403

    for bearer, slug in (("implementer", SLUG), ("agent", "missing")):
        legacy = RestBridge(legacy=True)
        legacy.install(monkeypatch)
        monkeypatch.setenv("HAIPLANE_HUB_TOKEN", bearer)
        expected = await mcp._policy_brief_for_slug(slug)
        current = RestBridge()
        current.install(monkeypatch)
        monkeypatch.setenv("HAIPLANE_HUB_TOKEN", bearer)
        assert await mcp._policy_brief_for_slug(slug) == expected
        assert "политика проекта не прочитана:" in expected
        assert len(current.policy_requests) == 1

    async def malformed(request):
        return httpx.Response(200, json={"slug": SLUG, "keys": []}, request=request)

    monkeypatch.setattr(current, "handle_async_request", malformed)
    assert (await mcp._policy_brief_for_slug(SLUG)).strip() == (
        "политика проекта не прочитана: ответ хаба не разобран"
    )


async def test_policy_brief_avoids_full_policy_reads(client, db, scene, monkeypatch):
    _, task_id = scene
    monkeypatch.setattr(steward_shadow, "configured_mode", lambda: "act")
    project = await repo.get_project_by_slug(db, SLUG)
    sql = []
    await db.set_trace_callback(sql.append)
    changes = db.total_changes
    try:
        full = await policy.effective_policy(db, project)
        full_count = len(sql)
        sql.clear()
        brief = await policy.policy_brief(db, project)
        brief_count = len(sql)
    finally:
        await db.set_trace_callback(None)
    assert 0 < brief_count < full_count, (brief_count, full_count)
    assert "effective shadow" in policy.format_policy_brief(brief)[1]
    assert policy.format_policy_brief(brief) == policy.format_policy_brief(full)
    assert db.total_changes == changes

    async def unexpected(*_args, **_kwargs):
        pytest.fail("the brief path consulted a full report or a writing reader")

    for module, fname in (
        (repo, "list_scheduled_policy_changes"),
        (ci_trust_warning, "ci_trust_block"),
        (steward_cap, "project_report"),
        (steward_act_readiness, "readiness_report"),
        (steward_exit, "contour_report"),
        (steward_shadow, "effective_mode"),
        (steward_shadow, "_announce_refusal_once"),
        (policy, "_locks_block"),
        (policy, "_steward_verdicts_block"),
        (policy, "_last_change"),
    ):
        monkeypatch.setattr(module, fname, unexpected)
    bridge = RestBridge()
    bridge.install(monkeypatch)
    result = await mcp.hub_my_context(project=SLUG, mode="full")
    assert "effective shadow" in _text(result)
    assert bridge.policy_requests == [f"{POLICY}?brief=true".encode()]
    bridge.policy_requests.clear()
    result = await mcp.hub_my_context(task_id=task_id, mode="full")
    assert "effective shadow" in _text(result)
    assert bridge.policy_requests == []


async def test_policy_brief_isolates_detailed_report_failures(
    client, scene, monkeypatch
):
    _, task_id = scene
    monkeypatch.setattr(steward_shadow, "configured_mode", lambda: "act")
    lines = "\n".join(policy.format_policy_brief(await _full(client)))
    bridge = RestBridge()
    bridge.install(monkeypatch)

    async def unreadable(*_args, **_kwargs):
        raise RuntimeError("fixture read failure")

    for module, attr in (
        (repo, "list_scheduled_policy_changes"),
        (ci_trust_warning, "ci_trust_block"),
        (steward_cap, "project_report"),
        (steward_act_readiness, "readiness_report"),
        (steward_exit, "contour_report"),
        (policy, "_locks_block"),
        (policy, "_steward_verdicts_block"),
        (policy, "_last_change"),
    ):
        with monkeypatch.context() as mp:
            mp.setattr(module, attr, unreadable)
            async with httpx.AsyncClient(
                transport=bridge, base_url="http://test"
            ) as api:
                response = await api.get(POLICY)
            if attr == "readiness_report":
                assert response.status_code == 200
                assert response.json()["steward"]["readiness"]["status"] == "unknown"
            else:
                assert response.status_code == 500
            for target in ({"project": SLUG}, {"task_id": task_id}):
                result = await mcp.hub_my_context(**target, mode="full")
                assert lines in _text(result)

    monkeypatch.setattr(steward_exit, "contour_counts", unreadable)
    result = await mcp.hub_my_context(project=SLUG, mode="full")
    assert "политика проекта не прочитана:" in _text(result)
    layer = await _task_layer(client, task_id, "full")
    assert layer == {"state": "unavailable", "text": "", "chars": 0}
    bridge.policy_requests.clear()
    result = await mcp.hub_my_context(task_id=task_id, mode="full")
    assert "политика проекта не собрана" in _text(result)
    assert bridge.policy_requests == []

    for error, message in (
        (httpx.ReadTimeout, "не дождался ответа хаба"),
        (httpx.ReadError, "не смог поговорить с хабом"),
    ):

        async def failed_get(request):
            assert request.url.query == BRIEF_QUERY
            raise error("fixture failure", request=request)

        monkeypatch.setattr(bridge, "handle_async_request", failed_get)
        text = await mcp._policy_brief_for_slug(SLUG)
        assert f"GET {POLICY} {message}" in text
        assert "brief=true" not in text
