import asyncio
from datetime import datetime, timedelta
import json
from types import SimpleNamespace

import pytest

from hub import config, repository as repo
from hub.db import fetchall
from hub.services import admin, epic_plan, epic_plan_admission, orchestrator_queue
from tests.executor_slot_support import seed_slot_skills
from tests.test_executor_orders import keys  # noqa: F401 - slot host key fixture

PLAN = "/api/tasks/2373/epic-plan"


@pytest.fixture
async def pilot(db, monkeypatch):
    if await repo.get_project_by_slug(db, "default") is None:
        await repo.create_project(db, slug="default", name="Default")
    pid = await repo.create_project(db, slug=epic_plan.PROJECT, name="Synthetic pilot")
    nodes = [
        (2373, None, "epic", "draft", pid),
        (2374, 2373, "feature", "draft", None),
        (2375, 2373, "feature", "draft", None),
        (2376, 2373, "feature", "draft", None),
        (2377, 2374, "task", "draft", None),
        (2378, 2374, "task", "draft", None),
        (2379, 2375, "task", "draft", None),
        (2380, 2376, "task", "open", None),
        (2381, 2376, "task", "draft", None),
    ]
    await db.executemany(
        "INSERT INTO tasks(id,parent_id,task_type,status,project_id,title,statement_generation,affected_areas) "
        "VALUES (?,?,?,?,?,?,?,?)",
        [(n, p, t, s, proj, f"Task {n}", 2, '["app/"]') for n, p, t, s, proj in nodes],
    )
    for source, dest in [(2378, 2377), (2379, 2378), (2380, 2378), (2381, 2379)]:
        await repo.add_task_dependency(db, source, dest)
    await repo.add_task_update(db, 2380, "seed", "status", "existing open history")

    identities = {}
    for name, kind, role in [
        ("plan-owner", "human", "operator"),
        ("other-admin", "human", "admin"),
        ("plan-agent", "agent", "agent"),
    ]:
        principal = await admin.create_principal(
            db, kind=kind, username=name, role_slug=role
        )
        key = await admin.create_api_key(db, principal["id"], name="fixture")
        identities[name] = (
            principal["id"],
            {"Authorization": f"Bearer {key['plaintext_key']}"},
        )
    owner, _ = identities["plan-owner"]
    monkeypatch.setenv("HAIPLANE_EPIC_PLAN_OWNER_PRINCIPAL_ID", str(owner))
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    monkeypatch.setattr(
        config, "HUB_TOKENS", {"fixture-auth": config.TokenIdentity("fixture")}
    )

    async def ready(db, task_id, *, snapshot):
        return SimpleNamespace(dor_passed=True, dor_checks=[])

    # Isolate the pilot from Git and use real DoR snapshots and the persisted approval gate.
    monkeypatch.setattr(
        epic_plan_admission, "calculate_readiness_with_recommendations", ready
    )
    await db.commit()
    return pid, identities


async def _approved(client, pilot):
    _, actors = pilot
    _, headers = actors["plan-owner"]
    view = await client.get(PLAN, headers=headers)
    assert view.status_code == 200, view.text
    version = view.json()["version"]
    response = await client.post(
        PLAN + "/approve", headers=headers, json={"version": version}
    )
    assert response.status_code == 200, response.text
    return headers, response.json()


async def test_owner_approves_exact_plan_once(client, db, pilot, monkeypatch):
    _, actors = pilot
    owner_id, owner = actors["plan-owner"]
    get = await client.get(PLAN, headers=owner)
    assert get.status_code == 200, get.text
    plan = get.json()
    assert plan["state"] == "draft" and plan["can_decide"]
    assert len(plan["snapshot"]["nodes"]) == 9
    assert [
        n["task_id"] for n in plan["snapshot"]["nodes"] if n["allowed"] and n["leaf"]
    ] == [2377, 2378, 2379, 2380]
    assert plan["snapshot"]["terms"] == epic_plan.TERMS.model_dump()
    version = plan["version"]
    payload = {"version": version}
    for name in ("plan-agent", "other-admin"):
        _, headers = actors[name]
        other = await client.get(PLAN, headers=headers)
        assert other.status_code == 200 and not other.json()["can_decide"]
        guessed = {"version": "0" * 64} if name == "plan-agent" else payload
        refused = await client.post(PLAN + "/approve", headers=headers, json=guessed)
        assert refused.status_code == 403

    responses = await asyncio.gather(
        *(client.post(PLAN + "/approve", headers=owner, json=payload) for _ in range(2))
    )
    assert all(response.status_code == 200 for response in responses), [
        r.text for r in responses
    ]
    assert responses[0].json() == responses[1].json()
    answer = responses[0].json()
    assert answer["state"] == "active" and answer["approved_principal_id"] == owner_id
    assert datetime.fromisoformat(answer["expires_at"]) - datetime.fromisoformat(
        answer["approved_at"]
    ) == timedelta(days=14)
    events = await fetchall(
        db, "SELECT kind,actor FROM events WHERE kind LIKE 'epic_plan_%'"
    )
    assert [(e["kind"], e["actor"]) for e in events] == [
        ("epic_plan_approved", "human")
    ]
    approvals = await fetchall(
        db, "SELECT actor FROM events WHERE kind='task_approved'"
    )
    assert len(approvals) == 7
    assert {e["actor"] for e in approvals} == {"epic_plan"}
    assert (await repo.get_task(db, 2380))["status"] == "open"
    assert [r["content"] for r in await repo.get_task_updates(db, 2380)] == [
        "existing open history"
    ]
    assert (await repo.get_task(db, 2381))["status"] == "draft"
    assert not await fetchall(db, "SELECT id FROM executor_runs")
    assert not await fetchall(db, "SELECT id FROM executor_orders")

    async def paired(request, token):
        return config.TokenIdentity(
            "plan-owner",
            "operator",
            owner_id,
            frozenset({"tasks.read", "tasks.human_gate"}),
            auth_source="chat_pair",
            chat_pair_kind="intake",
        )

    monkeypatch.setattr("hub.auth._resolve_chat_pair", paired)
    refused = await client.post(PLAN + "/approve", headers=owner, json=payload)
    assert refused.status_code == 403


@pytest.mark.parametrize(
    "change",
    [
        "criterion",
        "areas",
        "project",
        "budget",
        "revision",
        "descendant",
        "graph",
        "repo",
        "forge",
        "base",
        "release_base",
        "workspace",
    ],
)
async def test_revised_work_requires_new_decision(
    client, db, pilot, monkeypatch, change
):
    headers, approved = await _approved(client, pilot)
    match change:
        case "criterion":
            await db.execute(
                "INSERT INTO acceptance_criteria(task_id,ac_id,given,when_clause,then_clause,verifiable_by) "
                "VALUES (2377,'AC-1','before','action','changed requirement','manual')"
            )
        case "areas":
            await repo.update_task(db, 2377, affected_areas='["new/"]')
        case "project":
            await db.execute("UPDATE tasks SET project_id=1 WHERE id=2373")
        case "repo" | "forge" | "base" | "release_base" | "workspace":
            project_id, _ = pilot
            changes = {
                "repo": {"repo": "other/receipt"},
                "forge": {"forge": "gitverse"},
                "base": {"default_branch": "other-base"},
                "release_base": {
                    "default_branch_policy": '{"release_base":"other-release"}'
                },
                "workspace": {"workspace_path": "/private/pilot-location"},
            }
            await repo.update_project(db, project_id, **changes[change])
        case "budget":
            monkeypatch.setattr(
                epic_plan,
                "TERMS",
                epic_plan.TERMS.model_copy(update={"total_cents": 3000}),
            )
        case "revision":
            await db.execute("UPDATE tasks SET statement_generation=3 WHERE id=2377")
        case "descendant":
            await db.execute(
                "INSERT INTO tasks(id,parent_id,title) VALUES (2399,2374,'Unapproved')"
            )
        case "graph":
            await repo.remove_task_dependency(db, 2378, 2377)
    await db.commit()
    blocker = await epic_plan.start_blocker(db, 2377)
    assert blocker["reason"] == (
        "pilot_project_moved" if change == "project" else "plan_changed"
    )
    if change == "descendant":
        assert (await epic_plan.start_blocker(db, 2399))[
            "reason"
        ] == "plan_task_excluded"
    replay = await client.post(
        PLAN + "/approve", headers=headers, json={"version": approved["version"]}
    )
    if change == "project":
        assert replay.status_code == 404
    else:
        assert replay.status_code == 200
        assert replay.json()["state"] == "stopped"
    assert len(await fetchall(db, "SELECT id FROM epic_plan_decisions")) == 1


async def test_dependency_delivery_unknown_is_preserved(client, db, pilot):
    pid, _ = pilot
    await repo.update_task(db, 2380, dor_passed=True)
    await db.commit()
    headers, _ = await _approved(client, pilot)
    cause = "Реестр недоступен для проверки доставки"
    await repo.record_delivery_discrepancy(
        db,
        task_id=2378,
        state="unknown",
        reason=cause,
        pr_number=24,
        delivery_path="unknown",
    )

    expected = {
        "reason": "dependency_delivery_unknown",
        "task_id": 2380,
        "blockers": [{"task_id": 2378, "delivered": None, "reason": cause}],
    }
    start = await client.post(
        "/api/tasks/2380/start", headers=headers, json={"plan": "not funded"}
    )
    assert start.status_code == 409, start.text
    assert start.json()["detail"] == expected
    queue = await orchestrator_queue.next_task(db, await repo.get_project(db, pid))
    skip = next(row for row in queue["skipped"] if row["task_id"] == 2380)
    assert {key: skip[key] for key in expected} == expected
    assert cause in skip["detail"]
    assert await orchestrator_queue.announce_once(db, queue)
    assert (await repo.get_task(db, 2380))["status"] == "open"


async def test_slot_launch_reports_pilot_gate_for_open_task(
    client,
    db,
    pilot,
    keys,  # noqa: F811 - imported pytest fixture
):
    pid, actors = pilot
    _, headers = actors["plan-owner"]
    await seed_slot_skills(db)
    await repo.update_project(
        db, pid, gate_policy=json.dumps({"executor_launch": "manual"})
    )
    await db.commit()
    host = await client.post(
        "/api/admin/executor-hosts",
        json={"name": "k8", "public_key_pem": keys.k8.public_pem},
        headers=headers,
    )
    assert host.status_code == 200, host.text

    result = await client.post(
        f"/api/projects/{epic_plan.PROJECT}/executor-launch",
        headers=headers,
        json={"target": "slot:k8", "task_id": 2380},
    )
    assert result.status_code == 409, result.text
    assert result.json()["detail"] == {"reason": "plan_not_approved", "task_id": 2380}
    assert (await repo.get_task(db, 2380))["status"] == "open"
    assert not await fetchall(db, "SELECT id FROM executor_runs WHERE task_id=2380")
    assert not await fetchall(db, "SELECT id FROM executor_orders WHERE task_id=2380")
    assert not await fetchall(
        db, "SELECT id FROM chat_pair_codes WHERE bound_task_id=2380"
    )


async def test_revoke_or_undelivered_dependency_blocks_start(client, db, pilot):
    pid, _ = pilot
    headers, approved = await _approved(client, pilot)
    reason = await epic_plan.start_blocker(db, 2380)
    assert reason["reason"] == "dependency_undelivered"
    assert reason["task_id"] == 2380
    assert [(row["task_id"], row["delivered"]) for row in reason["blockers"]] == [
        (2378, False)
    ]
    skip = await orchestrator_queue._skip_reason(db, {"id": 2380}, [])
    assert skip["reason"] == "dependency_undelivered"
    assert (await epic_plan.start_blocker(db, 2374))["reason"] == "plan_task_excluded"
    assert await epic_plan.start_blocker(db, 1) is None

    await repo.record_pipeline_merge(
        db, pr_number=24, merge_sha="a" * 40, project_id=pid, task_id=2378
    )
    await db.commit()
    assert await epic_plan.start_blocker(db, 2380) is None
    await repo.update_task(db, 2379, status="running")
    await db.commit()
    assert (await epic_plan.start_blocker(db, 2380))["reason"] == "plan_wip_full"
    await repo.update_task(db, 2379, status="open")
    await db.commit()
    dispatch = await client.post(
        "/api/tasks/2380/start", headers=headers, json={"plan": "not funded"}
    )
    assert dispatch.status_code == 409
    assert dispatch.json()["detail"]["reason"] == "plan_budget_unverified"

    payload = {
        "state": "revoked",
        "version": approved["version"],
        "reason": "Owner revoked",
    }
    for _ in range(2):
        revoked = await client.post(PLAN + "/terminate", headers=headers, json=payload)
        assert revoked.status_code == 200 and revoked.json()["state"] == "revoked"
    assert (await epic_plan.start_blocker(db, 2380))["reason"] == "plan_revoked"
    records = await fetchall(
        db, "SELECT actor FROM events WHERE kind='epic_plan_revoked'"
    )
    assert [r["actor"] for r in records] == ["human"]


async def test_unassigned_expired_and_stopped_are_distinct(
    client, db, pilot, monkeypatch
):
    with monkeypatch.context() as unassigned:
        unassigned.delenv("HAIPLANE_EPIC_PLAN_OWNER_PRINCIPAL_ID")
        _, actors = pilot
        _, headers = actors["plan-owner"]
        draft = await client.get(PLAN, headers=headers)
        assert draft.json()["reason"] == "owner_unassigned"
        assert not draft.json()["can_decide"]
        refused = await client.post(
            PLAN + "/approve",
            headers=headers,
            json={"version": draft.json()["version"]},
        )
        assert refused.status_code == 403
    headers, approved = await _approved(client, pilot)
    with monkeypatch.context() as retained:
        retained.setattr(
            epic_plan, "_now", lambda: datetime.fromisoformat(approved["expires_at"])
        )
        assert (await epic_plan.start_blocker(db, 2377))["reason"] == "plan_expired"
    paused = await client.post(
        PLAN + "/terminate",
        headers=headers,
        json={
            "state": "stopped",
            "version": approved["version"],
            "reason": "Owner suspended",
        },
    )
    assert paused.status_code == 200 and paused.json()["state"] == "stopped"
    assert (await epic_plan.start_blocker(db, 2377))["reason"] == "plan_stopped"


async def test_failed_dor_rolls_back_plan_and_all_openings(
    client, db, pilot, monkeypatch
):
    real = epic_plan_admission.calculate_readiness_with_recommendations

    async def blocked(db, task_id, *, snapshot):
        if task_id == 2377:
            return SimpleNamespace(
                dor_passed=False,
                missing_required=["scope"],
                score=0,
                recommendations=[],
            )
        return await real(db, task_id, snapshot=snapshot)

    monkeypatch.setattr(
        epic_plan_admission, "calculate_readiness_with_recommendations", blocked
    )
    _, actors = pilot
    _, owner = actors["plan-owner"]
    plan = (await client.get(PLAN, headers=owner)).json()
    response = await client.post(
        PLAN + "/approve", headers=owner, json={"version": plan["version"]}
    )
    assert response.status_code == 422, response.text
    assert (await repo.get_task(db, 2373))["status"] == "draft"
    assert not await fetchall(db, "SELECT id FROM epic_plan_decisions")
    assert not await fetchall(db, "SELECT id FROM events WHERE kind='task_approved'")


async def test_plan_read_surfaces_and_permissions(client, pilot, monkeypatch, capsys):
    import sys
    from hub import cli, mcp_server

    _, actors = pilot
    _, owner = actors["plan-owner"]
    _, agent = actors["plan-agent"]
    view = (await client.get(PLAN, headers=owner)).json()
    url = "/tasks/2373/epic-plan"
    owner_page = await client.get(url, headers=owner)
    agent_page = await client.get(url, headers=agent)
    assert owner_page.status_code == agent_page.status_code == 200
    assert view["version"] in owner_page.text
    assert (
        "Утвердить версию" in owner_page.text
        and "Утвердить версию" not in agent_page.text
    )

    async def mcp_read(path):
        assert path == PLAN
        return (await client.get(path, headers=agent)).json()

    monkeypatch.setattr(mcp_server, "_api_get", mcp_read)
    result = (await mcp_server.hub_epic_plan(2373)).structuredContent
    assert result["version"] == view["version"] and not result["can_decide"]
    monkeypatch.setattr(cli, "_api", lambda method, path: view)
    monkeypatch.setattr(sys, "argv", ["hp-hub", "epic-plan", "show", "2373"])
    assert cli.main() == 0
    assert json.loads(capsys.readouterr().out)["can_decide"]


async def test_owner_browser_form_requires_csrf_and_signs_read_version(
    client, db, pilot, monkeypatch
):
    import re

    from hub import brand

    _, actors = pilot
    owner_id, _ = actors["plan-owner"]
    session = await admin.create_browser_session(db, owner_id)
    client.cookies.set(brand.COOKIE_NAME, session)
    monkeypatch.setattr(config, "CSRF_MODE", "warn")
    monkeypatch.setenv("HAIPLANE_HUB_URL", str(client.base_url).rstrip("/"))
    url = "/tasks/2373/epic-plan"
    view = await client.get(url)
    assert view.status_code == 200
    version = re.search(r'name="version" value="([0-9a-f]{64})"', view.text)
    token = re.search(r'name="csrf_token" value="([^"]+)"', view.text)
    assert version and token
    form = {"action": "approve", "version": version[1]}
    origin = {"Origin": str(client.base_url).rstrip("/")}
    forged_api = await client.post(
        PLAN + "/approve", json={"version": version[1]}, headers=origin
    )
    assert forged_api.status_code == 403
    forged = await client.post(url, data=form, headers=origin)
    assert forged.status_code == 403
    confirmed = await client.post(
        url, data={**form, "csrf_token": token[1]}, headers=origin
    )
    assert confirmed.status_code == 303, confirmed.text
    assert (await client.get(PLAN)).json()["state"] == "active"


async def test_explicit_new_version_keeps_completed_history(client, db, pilot):
    owner, first = await _approved(client, pilot)
    await repo.update_task(db, 2377, status="completed")
    await repo.update_task(db, 2378, affected_areas='["new/ui/"]')
    await db.commit()
    latest = (await client.get(PLAN, headers=owner)).json()
    assert latest["state"] == "stopped" and latest["version"] != first["version"]
    confirmed = await client.post(
        PLAN + "/approve", headers=owner, json={"version": latest["version"]}
    )
    assert confirmed.status_code == 200 and confirmed.json()["state"] == "active"
    assert (await repo.get_task(db, 2377))["status"] == "completed"
    original = await client.post(
        PLAN + "/approve", headers=owner, json={"version": first["version"]}
    )
    assert original.json()["reason"] == "plan_superseded"
    assert len(await fetchall(db, "SELECT id FROM epic_plan_decisions")) == 2
    assert (
        len(await fetchall(db, "SELECT id FROM events WHERE kind='epic_plan_approved'"))
        == 2
    )


async def test_preview_and_signed_snapshot_keep_product_scope(client, db, pilot):
    await repo.update_task(
        db, 2377, scope_in='["Approved scope"]', scope_out='["Excluded scope"]'
    )
    await db.execute(
        "INSERT INTO acceptance_criteria(task_id,ac_id,given,when_clause,then_clause,verifiable_by) "
        "VALUES (2377,'AC-1','before','action','Original criterion','manual')"
    )
    await db.commit()
    owner, approved = await _approved(client, pilot)
    await db.execute(
        "UPDATE acceptance_criteria SET then_clause='Changed criterion' WHERE task_id=2377"
    )
    await db.commit()
    view = (await client.get(PLAN, headers=owner)).json()
    assert view["version"] != approved["version"]
    before = next(n for n in approved["snapshot"]["nodes"] if n["task_id"] == 2377)
    current = next(n for n in view["snapshot"]["nodes"] if n["task_id"] == 2377)
    assert before["areas"] == current["areas"] and before["title"] == current["title"]
    assert before["acceptance_criteria"][0]["then"] == "Original criterion"
    assert current["acceptance_criteria"][0]["then"] == "Changed criterion"
    html = (await client.get("/tasks/2373/epic-plan", headers=owner)).text
    for snippet in (
        "Approved scope",
        "Excluded scope",
        "Original criterion",
        "Changed criterion",
    ):
        assert snippet in html


async def test_plan_does_not_expose_foreign_project_descendants_or_edges(
    client, db, pilot
):
    _, actors = pilot
    _, owner = actors["plan-owner"]
    other = await repo.get_project_by_slug(db, "default")
    await db.execute(
        "INSERT INTO tasks(id,title,task_type,project_id) VALUES (2399,'CANARY from other project','epic',?)",
        (other["id"],),
    )
    await repo.add_task_dependency(db, 2377, 2399)
    await db.commit()
    response = await client.get(PLAN, headers=owner)
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "plan_cross_project"
    assert "CANARY" not in response.text
    await repo.remove_task_dependency(db, 2377, 2399)
    await db.execute("UPDATE tasks SET parent_id=2376 WHERE id=2399")
    await db.commit()
    response = await client.get(PLAN, headers=owner)
    assert response.status_code == 409
    assert "CANARY" not in response.text
