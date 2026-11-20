"""End-to-end checks of the scoped pilot using existing REST role identities."""

import asyncio
from uuid import uuid4

import pytest

from hub import repository as repo
from hub.services import (
    admin,
    role_orders,
    roadmap_observation_pilot as pilot,
    roadmap_pilot_activation,
)
from tests import role_orders_support as support
from tests.test_roadmap_activation import approved as approval_fixture

approved = approval_fixture
world = support.world
keys = support.keys
_clean_limiter = support._clean_limiter
_no_cloud = support._no_cloud
_HOST = "/api/executor-hosts/k8/roadmap-pilot/2406"


@pytest.fixture
async def scenario(approved, monkeypatch):
    world, mandate, owner = approved
    monkeypatch.setattr(pilot, "TASK_ID", mandate.task_id)
    monkeypatch.setattr(roadmap_pilot_activation, "_MANDATE", mandate)
    return world, mandate, owner


async def _auth(world, principal_id):
    key = await admin.create_api_key(
        world.db, principal_id, name="test", projects=[world.slug]
    )
    return {"Authorization": f"Bearer {key['plaintext_key']}"}


async def _post_sample(world, grant_id, **overrides):
    payload = dict(
        activation_id=grant_id,
        observation_id=str(uuid4()),
        codex_ready=True,
        openai_ready=True,
        repository_read=True,
        **{},
    )
    payload.update(overrides)
    return await world.client.post(
        f"{_HOST}/observations", headers=world.k8_auth, json=payload
    )


async def _move_observation(world, shift):
    await world.db.execute(
        "UPDATE roadmap_pilot_observations SET observed_at=datetime('now',?)", (shift,)
    )
    await world.db.commit()


async def test_ac_1(scenario):
    world, mandate, owner = scenario
    url = f"/api/tasks/{mandate.task_id}/roadmap-grant"
    denied = await world.client.put(
        url,
        headers=owner,
        json={
            "plan_id": mandate.task_id,
            "steward_principal_id": mandate.steward_id,
            "roadmap_principal_id": mandate.roadmap_id,
            "ttl_days": 1,
        },
    )
    assert denied.status_code == 409
    absent = await world.client.get(f"{_HOST}/observations", headers=world.k8_auth)
    assert absent.json() == {"activation_id": None, "observation_permitted": False}
    assert (
        await world.client.get(f"{_HOST}/observations", headers=world.k9_auth)
    ).status_code == 403
    started_at = (await support.rows(world.db, "SELECT datetime('now') AS utc"))[0][
        "utc"
    ]
    created = await roadmap_pilot_activation._run(world.db)
    finished_at = (await support.rows(world.db, "SELECT datetime('now') AS utc"))[0][
        "utc"
    ]
    assert started_at <= created["activated_at"] <= finished_at
    again = await roadmap_pilot_activation._run(world.db)
    assert again == {"state": "active", "grant_id": created["grant_id"], "repeat": True}
    lifetime = await support.rows(
        world.db,
        "SELECT unixepoch(g.expires_at)-unixepoch(p.activated_at) AS seconds "
        "FROM roadmap_grants g JOIN roadmap_observation_pilots p ON p.grant_id=g.id",
    )
    assert lifetime == [{"seconds": 86400}]
    await world.db.execute(
        "UPDATE roadmap_grants SET expires_at=datetime('now','-1 second')"
    )
    await world.db.commit()
    assert (await _post_sample(world, created["grant_id"])).status_code == 409
    assert (await roadmap_pilot_activation._run(world.db))["state"] == "inactive"
    await world.client.delete(url, headers=owner)
    assert (await roadmap_pilot_activation._run(world.db))["repeat"] is True
    assert (
        len(await support.rows(world.db, "SELECT * FROM roadmap_observation_pilots"))
        == 1
    )


async def test_ac_2(scenario):
    world, _, _ = scenario
    grant = (await roadmap_pilot_activation._run(world.db))["grant_id"]
    assert (await _post_sample(world, grant + 1)).status_code == 409
    token = str(uuid4())
    accepted = await _post_sample(world, grant, observation_id=token)
    assert accepted.json() == {"sequence": 1, "repeat": False}
    repeated = await _post_sample(world, grant, observation_id=token)
    assert repeated.json() == {"sequence": 1, "repeat": True}
    assert (
        await _post_sample(world, grant, observation_id=token, codex_ready=False)
    ).status_code == 409
    assert (await _post_sample(world, grant)).status_code == 409
    await _move_observation(world, "-60 seconds")
    results = await asyncio.gather(
        _post_sample(world, grant), _post_sample(world, grant)
    )
    assert sorted(r.status_code for r in results) == [200, 409]
    rows = await support.rows(
        world.db,
        "SELECT unixepoch(MAX(observed_at))-unixepoch(MIN(observed_at)) AS seconds "
        "FROM roadmap_pilot_observations",
    )
    assert rows[0]["seconds"] >= 60


async def _process_consultation(scenario, mode, reason):
    world, mandate, _ = scenario
    steward = await _auth(world, mandate.steward_id)
    guest = await _auth(world, mandate.roadmap_id)
    grant = (await roadmap_pilot_activation._run(world.db))["grant_id"]
    task = f"/api/tasks/{mandate.task_id}"
    assert (await world.client.get(f"{task}/roadmap-grant", headers=steward)).json()[
        "observation_batch_id"
    ] is None
    if mode == "incomplete":
        await world.db.execute(
            "UPDATE roadmap_observation_pilots SET activated_at=datetime('now','-11 minutes')"
        )
        await world.db.commit()
    else:
        assert (
            await _post_sample(world, grant, codex_ready=mode != "hold")
        ).status_code == 200
        await _move_observation(world, "-61 seconds")
        assert (await _post_sample(world, grant)).status_code == 200
    body = {
        "plan_id": mandate.task_id,
        "grant_id": grant,
        "observation_batch_id": grant,
        "request_id": "one",
        "question": "Which bounded step follows from the observations?",
    }
    issue = f"{task}/roadmap-requests"
    obsolete_signal = {**body, "source_order_id": 40}
    assert (
        await world.client.post(issue, headers=steward, json=obsolete_signal)
    ).status_code == 409
    created = await world.client.post(issue, headers=steward, json=body)
    assert created.status_code == 200, created.text
    order_id = created.json()["order_id"]
    assert (await world.client.post(issue, headers=steward, json=body)).json()[
        "repeat"
    ] is True
    assert (
        await world.client.post(
            issue, headers=steward, json={**body, "request_id": "two"}
        )
    ).status_code == 409
    decision = f"{task}/roadmap-orders/{order_id}/decision"
    assert (
        await world.client.post(decision, headers=steward, json={"grant_id": grant})
    ).status_code == 409
    if mode == "timeout":
        await _move_observation(world, "-16 minutes")
    else:
        await support.admit(world, host="k9")
        assert (await support.poll(world, host="k9")).json()["orders"] == []
        await support.admit(world)
        lease = await support.leased(world)
        assert (
            await support.started(world, "k8", order_id, lease["lease_id"])
        ).status_code == 200
        view = await world.client.get(
            f"/api/roadmap/orders/{order_id}",
            headers={**guest, "X-Hub-Roadmap-Source": lease["source_key"]},
        )
        assert (
            view.json()["decision_context"]["observation_batch"]["observation_batch_id"]
            == grant
        )
        recommendation = {
            "read": "verify_version_read",
            "hold": "hold_pilot",
            "incomplete": "insufficient_facts",
            "invalid": "verify_version_read",
            "failed": "verify_version_read",
        }[mode]
        report = {
            "lease_id": lease["lease_id"],
            "outcome": "failed" if mode == "failed" else "done",
        }
        if mode != "failed":
            report["roadmap_result"] = {
                "recommendation": recommendation,
                "risk": "unknown",
                "basis": {"observation_batch_id": grant + (mode == "invalid")},
            }
        response = await world.client.post(
            f"/api/executor-hosts/k8/role-orders/{order_id}/report",
            headers=world.k8_auth,
            json=report,
        )
        assert response.status_code == 200, response.text
    pending = await world.client.get(f"{task}/roadmap-grant", headers=steward)
    assert pending.json()["pending_decision_order_id"] == order_id
    done = await world.client.post(decision, headers=steward, json={"grant_id": grant})
    assert done.status_code == 200, done.text
    assert done.json()["reason"] == reason
    assert done.json()["decision_id"] == (
        None if reason in ("consultation_failed", "consultation_invalid") else order_id
    )
    assert (
        await world.client.post(decision, headers=steward, json={"grant_id": grant})
    ).json()["repeat"] is True
    return world, done.json()["effect_id"]


@pytest.mark.parametrize(
    ("mode", "reason"),
    [
        ("read", "verify_version_read"),
        ("hold", "hold_pilot"),
        ("incomplete", "insufficient_facts"),
        ("failed", "consultation_failed"),
        ("invalid", "consultation_invalid"),
        ("timeout", "consultation_failed"),
    ],
)
async def test_ac_3(scenario, mode, reason):
    _, effect_id = await _process_consultation(scenario, mode, reason)
    assert (effect_id is not None) == (mode == "read")


async def test_ac_4(scenario):
    world, expected_effect = await _process_consultation(
        scenario, "read", "verify_version_read"
    )
    effect_id = (
        await world.client.get(f"{_HOST}/effect", headers=world.k8_auth)
    ).json()["effect_id"]
    assert effect_id == expected_effect
    resource = f"{_HOST}/effects/{effect_id}"
    claims = await asyncio.gather(
        *[
            world.client.post(f"{resource}/claim", headers=world.k8_auth, json={})
            for _ in range(2)
        ]
    )
    assert sorted(c.json()["execute"] for c in claims) == [False, True]
    result = {"outcome": "read", "version": "a" * 40}
    assert not (
        await world.client.post(
            f"{resource}/report", headers=world.k8_auth, json=result
        )
    ).json()["repeat"]
    assert (
        await world.client.post(
            f"{resource}/report", headers=world.k8_auth, json=result
        )
    ).json()["repeat"]
    assert (
        await world.client.post(
            f"{resource}/report",
            headers=world.k8_auth,
            json={**result, "version": "b" * 40},
        )
    ).status_code == 409
    assert len(await support.rows(world.db, "SELECT * FROM role_orders")) == 1
    assert not await support.rows(world.db, "SELECT * FROM executor_orders")


async def test_quota_refusal_becomes_terminal_without_a_role_order(
    scenario, monkeypatch
):
    world, mandate, _ = scenario
    steward = await _auth(world, mandate.steward_id)
    activation = await roadmap_pilot_activation._run(world.db)
    grant = activation["grant_id"]
    assert (await _post_sample(world, grant)).status_code == 200
    await _move_observation(world, "-61 seconds")
    assert (await _post_sample(world, grant)).status_code == 200
    task = f"/api/tasks/{mandate.task_id}"
    payload = {
        "plan_id": mandate.task_id,
        "grant_id": grant,
        "observation_batch_id": grant,
        "request_id": "stable",
        "question": "Which bounded step follows the observations?",
    }
    issue = f"{task}/roadmap-requests"
    monkeypatch.setattr(role_orders, "ROLE_ORDERS_PER_HOUR", 0)
    refused = await world.client.post(issue, headers=steward, json=payload)
    assert refused.status_code == 429, refused.text
    assert await support.order_rows(world) == []
    await _move_observation(world, "-16 minutes")
    attempts = await asyncio.gather(
        *[world.client.post(issue, headers=steward, json=payload) for _ in range(2)]
    )
    assert all(r.status_code == 200 for r in attempts)
    assert sorted(reply.json()["repeat"] for reply in attempts) == [False, True]
    assert all(reply.json()["reason"] == "consultation_failed" for reply in attempts)
    current = (await world.client.get(f"{task}/roadmap-grant", headers=steward)).json()
    assert current["observation_batch_id"] is None
    assert current["decision_id"] is None
    assert current["consultation_order_id"] is None
    assert current["consultation_block_reason"] == "consultation_failed"
    assert current["consultation_blocked_at"]
    assert await support.order_rows(world) == []
    audits = await support.rows(
        world.db,
        "SELECT * FROM admin_audit_log WHERE action='roadmap_pilot_consultation_blocked'",
    )
    assert len(audits) == 1
    assert audits[0]["actor_principal_id"] == mandate.steward_id


async def _unrelated_host_auth(world):
    await repo.create_project(world.db, slug="unrelated-pilot", name="Unrelated pilot")
    await world.db.commit()
    host = (
        await support.rows(
            world.db, "SELECT principal_id FROM executor_hosts WHERE name='k8'"
        )
    )[0]
    key = await admin.create_api_key(
        world.db, host["principal_id"], name="unrelated", projects=["unrelated-pilot"]
    )
    return {"Authorization": f"Bearer {key['plaintext_key']}"}


async def test_same_host_with_unrelated_project_scope_is_denied(scenario):
    world, mandate, _ = scenario
    grant = (await roadmap_pilot_activation._run(world.db))["grant_id"]
    auth = await _unrelated_host_auth(world)
    for path in ("observations", "effect"):
        denied = await world.client.get(f"{_HOST}/{path}", headers=auth)
        assert denied.status_code == 403
    permitted = await world.client.get(f"{_HOST}/observations", headers=world.k8_auth)
    assert permitted.json()["observation_permitted"] is True
    assert (await _post_sample(world, grant)).status_code == 200
    await _move_observation(world, "-61 seconds")
    assert (await _post_sample(world, grant)).status_code == 200
    steward = await _auth(world, mandate.steward_id)
    issued = await world.client.post(
        f"/api/tasks/{mandate.task_id}/roadmap-requests",
        headers=steward,
        json={
            "grant_id": grant,
            "plan_id": mandate.task_id,
            "observation_batch_id": grant,
            "request_id": "scope",
            "question": "Which step is allowed?",
        },
    )
    assert issued.status_code == 200
    order_id = issued.json()["order_id"]
    await support.admit(world)
    assert (await support.poll(world, auth=auth)).json()["orders"] == []
    lease = await support.leased(world)
    assert (await support.poll(world, auth=auth)).json()["orders"] == []
    lease_id = lease["lease_id"]
    assert (
        await support.started(world, "k8", order_id, lease_id, auth=auth)
    ).status_code == 404
    assert (await support.started(world, "k8", order_id, lease_id)).status_code == 200
    assert (
        await support.report(world, "k8", order_id, lease_id, auth=auth)
    ).status_code == 404
    assert (await support.report(world, "k8", order_id, lease_id)).status_code == 200
