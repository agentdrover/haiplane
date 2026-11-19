"""Explicit roadmap grants preserve the existing automatic order route."""

from __future__ import annotations

import argparse
import asyncio
from io import StringIO
from types import SimpleNamespace

import pytest

from hub import repository as repo
from hub import roadmap_cli
from hub.services import admin
from tests import role_orders_support as support
from tests import test_executor_orders as executor_tests

world = support.world
keys = support.keys
_clean_limiter = support._clean_limiter
_no_cloud = support._no_cloud

QUESTION = "Which verification should the operator run next?"


async def _principal(world, name, role):
    principal = await admin.create_principal(
        world.db, kind="agent", username=name, role_slug=role
    )
    key = await admin.create_api_key(world.db, principal["id"], name="test")
    return principal["id"], {"Authorization": f"Bearer {key['plaintext_key']}"}


@pytest.fixture
async def scenario(world):
    _, owner_auth = await _principal(world, "roadmap-owner", "admin")
    steward_id, steward_auth = await _principal(
        world, "project-steward", "roadmap_steward"
    )
    _, other_steward = await _principal(world, "other-steward", "roadmap_steward")
    roadmap_id, roadmap_auth = await _principal(world, "roadmap-guest", "agent")
    _, other_guest = await _principal(world, "other-guest", "agent")
    task_id = await support.task_in_project(world, "active plan")
    await repo.insert_event(
        world.db, task_id=task_id, kind="task_approved", actor="human"
    )
    await world.db.commit()
    return SimpleNamespace(
        world=world,
        task_id=task_id,
        owner=owner_auth,
        steward=steward_auth,
        other_steward=other_steward,
        guest=roadmap_auth,
        other_guest=other_guest,
        grants={},
        grant_body={
            "plan_id": task_id,
            "steward_principal_id": steward_id,
            "roadmap_principal_id": roadmap_id,
        },
    )


def _grant_path(task_id):
    return f"/api/tasks/{task_id}/roadmap-grant"


async def _grant(s, task_id=None, **overrides):
    target = task_id or s.task_id
    response = await s.world.client.put(
        _grant_path(target),
        headers=s.owner,
        json={**s.grant_body, **overrides},
    )
    if response.status_code == 200:
        s.grants[target] = response.json()["grant_id"]
    return response


async def _request(s, *, auth=None, task_id=None, **overrides):
    return await s.world.client.post(
        f"/api/tasks/{task_id or s.task_id}/roadmap-requests",
        headers=auth or s.steward,
        json={
            "plan_id": s.task_id,
            "grant_id": s.grants.get(task_id or s.task_id, 1),
            "request_id": "one",
            "question": QUESTION,
            **overrides,
        },
    )


async def _question(s, order_id, auth=None, source_key=""):
    return await s.world.client.get(
        f"/api/roadmap/orders/{order_id}",
        headers={**(auth or s.guest), "X-Hub-Roadmap-Source": source_key},
    )


async def test_steward_request_and_retry(scenario):
    s = scenario
    granted = await _grant(s)
    assert granted.status_code == 200, granted.text
    replies = await asyncio.gather(_request(s), _request(s))
    assert all(r.status_code == 200 for r in replies), [r.text for r in replies]
    assert {r.json()["repeat"] for r in replies} == {True, False}
    order_id = replies[0].json()["order_id"]
    assert replies[1].json()["order_id"] == order_id
    assert (await _request(s, question="changed")).status_code == 409
    assert (await _request(s, request_id="another")).status_code == 409
    order = await support.order_row(s.world, order_id)
    assert order["source_key"].startswith("steward_request:")
    assert order["source_event_id"] is None
    assert (await repo.get_task(s.world.db, s.task_id))["status"] == "open"
    audit = await support.rows(
        s.world.db, "SELECT * FROM admin_audit_log WHERE action='roadmap_requested'"
    )
    assert len(audit) == 1
    assert audit[0]["actor_principal_id"] == s.grant_body["steward_principal_id"]
    assert QUESTION not in str(audit[0])


async def test_roadmap_read_is_bound_to_started_order(scenario):
    s = scenario
    assert (await _grant(s)).status_code == 200
    reply = await _request(s)
    assert reply.status_code == 200, reply.text
    order_id = reply.json()["order_id"]
    assert (await _question(s, order_id)).status_code == 404
    await support.admit(s.world)
    order = await support.leased(s.world)
    assert set(order) == {
        "id",
        "role",
        "task_id",
        "source_key",
        "lease_id",
        "attempt",
        "lease_deadline",
        "run_max_minutes",
    }
    assert order["id"] == order_id
    assert (await _question(s, order_id)).status_code == 404
    assert (
        await support.started(s.world, "k8", order_id, order["lease_id"])
    ).status_code == 200
    assert (await _question(s, order_id)).status_code == 404
    assert (
        await _question(s, order_id, source_key="steward_request:" + "0" * 32)
    ).status_code == 404
    read = await _question(s, order_id, source_key=order["source_key"])
    assert read.status_code == 200, read.text
    assert read.headers["cache-control"] == "no-store"
    card = await s.world.client.get(f"/api/tasks/{s.task_id}", headers=s.guest)
    assert order["source_key"] not in card.text
    assert QUESTION not in card.text
    assert read.json() == {
        "order_id": order_id,
        "task_id": s.task_id,
        "project": s.world.slug,
        "source_kind": "steward_request",
        "question": QUESTION,
    }
    assert (
        await _question(s, order_id, s.other_guest, order["source_key"])
    ).status_code == 404
    assert (
        await _question(s, order_id, s.owner, order["source_key"])
    ).status_code == 404
    assert (await _question(s, order_id + 1)).status_code == 404
    assert (
        await support.report(s.world, "k8", order_id, order["lease_id"])
    ).status_code == 200
    assert (await _question(s, order_id)).status_code == 404
    genuine_task = await support.task_in_project(s.world, "needs a decision")
    event = await support.enter_needs_decision(s.world, genuine_task)
    assert await support.sweep(s.world) == 1
    natural = await support.leased(s.world)
    assert natural["source_key"] == f"needs_decision:{event}"


async def test_scope_and_stale_leased_order(scenario):
    s = scenario
    another = await support.task_in_project(s.world, "outside the plan")
    assert (await _grant(s, task_id=another)).status_code == 409
    assert (await _grant(s, plan_id=another)).status_code == 409
    assert (await _grant(s)).status_code == 200
    assert (await _request(s, auth=s.other_steward)).status_code == 404
    assert (await _request(s, auth=s.owner)).status_code == 403
    assert (await _request(s, task_id=another)).status_code == 404
    forbidden = await s.world.client.get(f"/api/tasks/{s.task_id}", headers=s.steward)
    assert forbidden.status_code == 403
    assert (await _request(s)).status_code == 200
    await support.admit(s.world)
    lease = await support.leased(s.world)
    await repo.update_task(s.world.db, s.task_id, statement_generation=1)
    await s.world.db.commit()
    assert (await _request(s)).status_code == 409
    refused = await support.started(s.world, "k8", lease["id"], lease["lease_id"])
    assert refused.status_code == 409
    assert refused.json()["detail"]["reason"] == "roadmap_grant_stale"
    assert (await _question(s, lease["id"])).status_code == 404


async def test_revocation_and_project_boundary(scenario):
    s = scenario
    child = await support.task_in_project(s.world, "child")
    await repo.update_task(s.world.db, child, parent_id=s.task_id, project_id=None)
    await s.world.db.commit()
    assert (await _grant(s, task_id=child)).status_code == 200
    assert (await _request(s, task_id=child)).status_code == 200
    await support.admit(s.world)
    order = await support.leased(s.world)
    assert (
        await support.started(s.world, "k8", order["id"], order["lease_id"])
    ).status_code == 200
    revoked = await s.world.client.delete(_grant_path(child), headers=s.owner)
    assert revoked.json() == {"revoked": True}
    assert (
        await _question(s, order["id"], source_key=order["source_key"])
    ).status_code == 404
    assert (
        await support.report(s.world, "k8", order["id"], order["lease_id"])
    ).status_code == 409
    assert (
        await support.report(s.world, "k8", order["id"], order["lease_id"], "failed")
    ).status_code == 200
    another_project = await repo.create_project(
        s.world.db, slug="outside", name="outside"
    )
    await repo.update_task(s.world.db, child, project_id=another_project)
    await s.world.db.commit()
    assert (await _grant(s, task_id=child)).status_code == 409


async def _record_redeem_failure(world, source_task):
    launch = await executor_tests._launch(world, source_task)
    assert launch.status_code == 200, launch.text
    lease = await executor_tests._orders(world)
    order = lease.json()["orders"][0]
    order_id = order["id"]
    failed = await executor_tests._ack(world, order_id, "redeem_failed")
    assert failed.status_code == 200, failed.text
    report = await world.client.post(
        f"/api/executor-hosts/k8/orders/{order_id}/report",
        headers=world.k8_auth,
        json={
            "outcome": "failed",
            "reason_code": "redeem_failed",
            "note": "local test",
        },
    )
    assert report.status_code == 200, report.text
    return order_id


async def test_grant_discovers_two_subsequent_signals_without_another_approval(
    scenario,
):
    s = scenario
    source = await support.task_in_project(s.world, "executor signal source")
    assert (await _grant(s, signal_task_id=source)).status_code == 200

    async def signals():
        got = await s.world.client.get(_grant_path(s.task_id), headers=s.steward)
        assert got.status_code == 200, got.text
        return got.json()["signals"]

    assert await signals() == []
    fail_one = await _record_redeem_failure(s.world, source)
    fail_two = await _record_redeem_failure(s.world, source)
    foreign_task = await support.task_in_project(
        s.world, "not the selected signal task"
    )
    foreign = await _record_redeem_failure(s.world, foreign_task)
    assert (await _request(s, source_order_id=foreign)).status_code == 409
    assert (await _request(s)).status_code == 409
    assert [item["executor_order_id"] for item in await signals()] == [
        fail_one,
        fail_two,
    ]

    await support.admit(s.world)
    for number, source_order_id in enumerate((fail_one, fail_two), 1):
        first = await _request(
            s, source_order_id=source_order_id, request_id=f"once-{number}"
        )
        assert first.status_code == 200, first.text
        order_id = first.json()["order_id"]
        retry = await _request(
            s, source_order_id=source_order_id, request_id=f"once-{number}"
        )
        assert retry.json() == {"order_id": order_id, "repeat": True}
        assert (
            await _request(s, source_order_id=source_order_id, request_id="duplicate")
        ).status_code == 409
        order = await support.leased(s.world)
        assert (
            await support.started(s.world, "k8", order_id, order["lease_id"])
        ).status_code == 200
        assert (
            await support.report(s.world, "k8", order_id, order["lease_id"])
        ).status_code == 200
    assert await signals() == []


async def test_signal_grant_rejects_other_project_and_survives_event_retention(
    scenario,
):
    s = scenario
    project = await repo.create_project(
        s.world.db, slug="other-signal", name="other-signal"
    )
    source = await support.task_in_project(s.world, "outside")
    await repo.update_task(s.world.db, source, project_id=project)
    await s.world.db.commit()
    assert (await _grant(s, signal_task_id=source)).status_code == 422
    granted = await _grant(s)
    assert granted.status_code == 200
    await s.world.db.execute(
        "DELETE FROM events WHERE id = ?", (granted.json()["approval_id"],)
    )
    await s.world.db.commit()
    assert (await _request(s)).status_code == 200
    await repo.insert_event(
        s.world.db, task_id=s.task_id, kind="task_approved", actor="human"
    )
    await s.world.db.commit()
    assert (await _request(s)).status_code == 409


async def test_scoped_admin_cannot_grant_or_revoke_outside_scope(scenario):
    s = scenario
    other = await repo.create_project(
        s.world.db, slug="outside-admin", name="outside-admin"
    )
    assert other != s.world.pid
    principal = await admin.create_principal(
        s.world.db, kind="human", username="scoped-admin", role_slug="admin"
    )
    key = await admin.create_api_key(
        s.world.db, principal["id"], name="other-project", projects=["outside-admin"]
    )
    scoped = {"Authorization": f"Bearer {key['plaintext_key']}"}
    path = _grant_path(s.task_id)
    denied = await s.world.client.put(path, headers=scoped, json=s.grant_body)
    assert denied.status_code == 403
    assert (await _grant(s)).status_code == 200
    assert (await s.world.client.delete(path, headers=scoped)).status_code == 403
    assert (await _request(s)).status_code == 200


async def test_deleted_signal_task_cannot_expand_grant(scenario):
    s = scenario
    source = await support.task_in_project(s.world, "source")
    assert (await _grant(s, signal_task_id=source)).status_code == 200
    await s.world.db.execute("DELETE FROM tasks WHERE id = ?", (source,))
    await s.world.db.commit()
    refused = await _request(s)
    assert refused.status_code == 409
    assert refused.json()["detail"]["reason"] == "roadmap_source_invalid"
    assert await support.order_rows(s.world) == []


async def test_replacement_grant_rejects_old_intent_and_does_not_relist_source(
    scenario,
):
    s = scenario
    source = await support.task_in_project(s.world, "source")
    first_grant = (await _grant(s, signal_task_id=source)).json()["grant_id"]
    order_a = await _record_redeem_failure(s.world, source)
    order_b = await _record_redeem_failure(s.world, source)
    first = await _request(s, source_order_id=order_a)
    assert first.status_code == 200, first.text
    revoked = await s.world.client.delete(_grant_path(s.task_id), headers=s.owner)
    assert revoked.json() == {"revoked": True}
    assert (await _grant(s, signal_task_id=source)).status_code == 200
    outdated = await _request(s, grant_id=first_grant, source_order_id=order_b)
    assert outdated.status_code == 409
    assert outdated.json()["detail"]["reason"] == "roadmap_grant_stale"
    replay = await _request(s, source_order_id=order_a)
    assert replay.status_code == 409
    assert replay.json()["detail"]["reason"] == "roadmap_source_already_requested"
    current = await s.world.client.get(_grant_path(s.task_id), headers=s.steward)
    assert current.status_code == 200, current.text
    assert [item["executor_order_id"] for item in current.json()["signals"]] == [
        order_b
    ]
    assert len(await support.order_rows(s.world)) == 1


def test_cli_reads_question_from_stdin(monkeypatch):
    parser = argparse.ArgumentParser()
    operations = parser.add_subparsers(required=True)
    calls = []
    roadmap_cli.add_parser(
        operations, lambda *args: calls.append(args), lambda result: None
    )
    monkeypatch.setattr("sys.stdin", StringIO(QUESTION))
    args = parser.parse_args(
        [
            "roadmap",
            "request",
            "2386",
            "--plan-id",
            "2386",
            "--grant-id",
            "4",
            "--source-order-id",
            "37",
            "--request-id",
            "one",
            "--question-file",
            "-",
        ]
    )
    assert args.func(args) == 0
    assert calls == [
        (
            "POST",
            "/api/tasks/2386/roadmap-requests",
            {
                "plan_id": 2386,
                "grant_id": 4,
                "request_id": "one",
                "source_order_id": 37,
                "question": QUESTION,
            },
        )
    ]
