"""One-shot roadmap activation cannot spend an approval twice."""

import asyncio
import json
from dataclasses import replace

import pytest

from hub import repository as repo
from hub.db import connect
from hub.hub_instance import hub_base_url
from hub.services import admin, roadmap_activation, roadmap_requests
from tests import role_orders_support as support
from tests import test_executor_orders as executor_tests
from tests.test_roadmap_requests import _record_redeem_failure

world = support.world
keys = support.keys
_clean_limiter = support._clean_limiter
_no_cloud = support._no_cloud


@pytest.fixture
async def approved(world, monkeypatch):
    mayor = await admin.create_principal(
        world.db, kind="human", username="pilot-administrator", role_slug="admin"
    )
    key = await admin.create_api_key(world.db, mayor["id"], name="local fixture")
    auth = {"Authorization": f"Bearer {key['plaintext_key']}"}
    steward = await admin.create_principal(
        world.db, kind="agent", username="pilot-steward", role_slug="roadmap_steward"
    )
    roadmap = await admin.create_principal(
        world.db, kind="agent", username="pilot-roadmap", role_slug="agent"
    )
    await repo.update_project(world.db, world.pid, slug="home-infra")
    task = await support.task_in_project(world, "pilot plan", status="draft")
    ac = {
        "id": "AC-5",
        "given": "A confirmed failure on the specified pilot.",
        "when": "The owner reviews new fresh redeems.",
        "then": "Record the exact approved condition.",
        "verifiable_by": "log_check",
        "test_ref": "roadmap:successful_fresh_redeems>=2",
    }
    assert (
        await world.client.post(
            f"/api/tasks/{task}/acceptance_criteria", headers=auth, json=ac
        )
    ).status_code == 201
    approved = await world.client.post(
        f"/api/tasks/{task}/approve", headers=auth, json={"run": False, "force": True}
    )
    assert approved.status_code == 200, approved.text
    row = (
        await support.rows(
            world.db,
            "SELECT id,payload FROM events WHERE task_id=? AND kind='task_approved'",
            (task,),
        )
    )[0]
    stamp = json.loads(row["payload"])
    mandate = roadmap_activation._Mandate(
        task_id=task,
        project_id=world.pid,
        approval_id=row["id"],
        approver_id=mayor["id"],
        generation=stamp["statement_generation"],
        fingerprint=stamp["statement_fingerprint"],
        steward_id=steward["id"],
        roadmap_id=roadmap["id"],
    )
    monkeypatch.setattr(roadmap_activation, "_MANDATE", mandate)
    world.slug = "home-infra"
    monkeypatch.setattr(roadmap_activation, "_PUBLIC_HUB_URL", hub_base_url())
    return world, mandate, auth


async def test_exact_owner_approval_activates_scoped_grant(approved):
    world, mandate, auth = approved
    read = await roadmap_activation._run(world.db, apply=False)
    assert read == {
        "state": "eligible",
        "task_id": mandate.task_id,
        "approval_id": mandate.approval_id,
    }
    assert not await support.rows(world.db, "SELECT * FROM roadmap_grants")
    result = await roadmap_activation._run(world.db, apply=True)
    assert result["state"] == "active" and result["repeat"] is False
    grant = (await support.rows(world.db, "SELECT * FROM roadmap_grants"))[0]
    assert grant["id"] == result["grant_id"]
    assert (
        grant["task_id"]
        == grant["plan_id"]
        == grant["signal_task_id"]
        == mandate.task_id
    )
    assert grant["approval_id"] == mandate.approval_id
    assert (
        grant["steward_id"] == mandate.steward_id
        and grant["roadmap_id"] == mandate.roadmap_id
    )
    assert json.loads(grant["decision_checks_json"])[0]["target"] == 2
    hours = (
        await support.rows(
            world.db,
            "SELECT unixepoch(?) - unixepoch(?) AS elapsed",
            (
                grant["expires_at"],
                result["activated_at"],
            ),
        )
    )[0]["elapsed"]
    assert hours == 86400
    audit = (
        await support.rows(
            world.db, "SELECT * FROM admin_audit_log WHERE action='roadmap_granted'"
        )
    )[0]
    assert audit["actor_principal_id"] is None
    assert json.loads(audit["detail"])["approver_id"] == mandate.approver_id
    assert json.loads(audit["detail"])["actor"] == "hub.local.roadmap_activation"


@pytest.mark.parametrize("failure", ["event", "revision", "steward", "prior_offer"])
async def test_activation_rejects_stale_or_unbound_authority(
    approved, monkeypatch, failure
):
    world, mandate, auth = approved
    if failure == "event":
        monkeypatch.setattr(
            roadmap_activation,
            "_MANDATE",
            replace(mandate, approval_id=mandate.approval_id + 100),
        )
    elif failure == "revision":
        await repo.update_task(
            world.db, mandate.task_id, statement_generation=mandate.generation + 1
        )
        await world.db.commit()
    elif failure == "steward":
        await world.db.execute(
            "UPDATE principals SET status='disabled' WHERE id=?", (mandate.steward_id,)
        )
        await world.db.commit()
    else:
        route = f"/api/tasks/{mandate.task_id}/roadmap-grant"
        granted = await world.client.put(
            route,
            headers=auth,
            json={
                "plan_id": mandate.task_id,
                "steward_principal_id": mandate.steward_id,
                "roadmap_principal_id": mandate.roadmap_id,
                "signal_task_id": mandate.task_id,
                "ttl_days": 1,
            },
        )
        assert granted.status_code == 200, granted.text
        assert (await world.client.delete(route, headers=auth)).status_code == 200
    counts = await support.rows(
        world.db, "SELECT count(*) AS count FROM roadmap_grants"
    )
    with pytest.raises(roadmap_activation.ActivationRefused):
        await roadmap_activation._run(world.db, apply=True)
    assert (
        await support.rows(world.db, "SELECT count(*) AS count FROM roadmap_grants")
        == counts
    )
    assert not await support.rows(world.db, "SELECT * FROM roadmap_activation_receipts")


@pytest.mark.parametrize("close", ["revoked", "expired"])
async def test_activation_replay_never_issues_second_grant(approved, close):
    world, mandate, auth = approved
    second = await connect(world.dsn)
    try:
        a, b = await asyncio.gather(
            roadmap_activation._run(world.db, apply=True),
            roadmap_activation._run(second, apply=True),
        )
    finally:
        await second.close()
    assert a["grant_id"] == b["grant_id"]
    assert {a["repeat"], b["repeat"]} == {True, False}
    if close == "revoked":
        route = f"/api/tasks/{mandate.task_id}/roadmap-grant"
        assert (await world.client.delete(route, headers=auth)).status_code == 200
    else:
        await world.db.execute(
            "UPDATE roadmap_grants SET expires_at='2000-01-01' WHERE id=?",
            (a["grant_id"],),
        )
        await world.db.commit()
    again = await roadmap_activation._run(world.db, apply=True)
    assert again["repeat"] is True and again["state"] == close
    assert again["grant_id"] == a["grant_id"]
    assert len(await support.rows(world.db, "SELECT * FROM roadmap_grants")) == 1


async def test_one_system_slot_order_fences_historical_signals(approved):
    world, mandate, _ = approved
    historical = await _record_redeem_failure(world, mandate.task_id)
    activated = await roadmap_activation._run(world.db, apply=True)
    assert activated["executor_high_water"] == historical
    grant = await roadmap_requests._active_grant(world.db, mandate.task_id)
    assert grant is not None
    assert await roadmap_requests._signals(world.db, grant, 0) == []
    assert not await roadmap_requests._signal_is_current(world.db, grant, historical)

    second = await connect(world.dsn)
    try:
        replies = await asyncio.gather(
            roadmap_activation._issue_once(world.db),
            roadmap_activation._issue_once(second),
        )
    finally:
        await second.close()
    assert {r["repeat"] for r in replies} == {True, False}
    order = replies[0]["executor_order_id"]
    assert replies[1]["executor_order_id"] == order > historical
    runs = await support.rows(
        world.db, "SELECT launched_by FROM executor_runs ORDER BY id"
    )
    assert runs[0]["launched_by"] is not None and runs[1]["launched_by"] is None
    feed = await repo.get_task_updates(world.db, mandate.task_id)
    messages = [row["content"] for row in feed if f"заказ #{order}," in row["content"]]
    assert len(messages) == 1
    assert "локальной службой по одобренному мандату" in messages[0]
    assert "по нажатию человека" not in messages[0]

    leased = await executor_tests._orders(world)
    assert leased.json()["orders"][0]["id"] == order
    assert (await executor_tests._ack(world, order, "redeem_failed")).status_code == 200
    signal = await world.client.post(
        f"/api/executor-hosts/k8/orders/{order}/report",
        headers=world.k8_auth,
        json={
            "outcome": "failed",
            "reason_code": "redeem_failed",
            "note": "isolated fixture",
        },
    )
    assert signal.status_code == 200
    assert await roadmap_requests._signal_is_current(world.db, grant, order)
    assert [
        r["executor_order_id"]
        for r in await roadmap_requests._signals(world.db, grant, 0)
    ] == [order]
    assert (await roadmap_activation._issue_once(world.db))[
        "executor_order_id"
    ] == order
    assert len(await support.rows(world.db, "SELECT * FROM executor_orders")) == 2
    audit = await support.rows(
        world.db,
        "SELECT * FROM admin_audit_log WHERE action='roadmap_pilot_order_issued'",
    )
    assert len(audit) == 1 and audit[0]["actor_principal_id"] is None


@pytest.mark.parametrize(
    "block", ["no_grant", "expired_grant", "out_of_band", "wrong_hub"]
)
async def test_one_system_slot_order_stops_when_preconditions_fail(
    approved, monkeypatch, block
):
    world, mandate, auth = approved
    if block != "no_grant":
        receipt = await roadmap_activation._run(world.db, apply=True)
        if block == "expired_grant":
            await world.db.execute(
                "UPDATE roadmap_grants SET expires_at='2000-01-01' WHERE id=?",
                (receipt["grant_id"],),
            )
            await world.db.commit()
        elif block == "wrong_hub":
            monkeypatch.setattr(
                roadmap_activation, "_PUBLIC_HUB_URL", "https://wrong.example.test"
            )
        else:
            issued = await world.client.post(
                "/api/projects/home-infra/executor-launch",
                headers=auth,
                json={"target": "slot:k8", "task_id": mandate.task_id},
            )
            assert issued.status_code == 200, issued.text
    count = len(await support.rows(world.db, "SELECT id FROM executor_orders"))
    with pytest.raises(roadmap_activation.ActivationRefused):
        await roadmap_activation._issue_once(world.db)
    assert len(await support.rows(world.db, "SELECT id FROM executor_orders")) == count
    assert not await support.rows(
        world.db,
        "SELECT * FROM admin_audit_log WHERE action='roadmap_pilot_order_issued'",
    )
