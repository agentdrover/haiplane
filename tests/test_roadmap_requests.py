"""Explicit roadmap grants preserve the existing automatic order route."""

from __future__ import annotations

import argparse
import asyncio
import json
from io import StringIO
from types import SimpleNamespace

import pytest

from hub import repository as repo
from hub import roadmap_cli
from hub.services import admin, roadmap_decisions, roadmap_requests
from tests import role_orders_support as support
from tests import test_executor_orders as executor_tests
from tests.roadmap_decision_support import (
    _approve_plan_check,
    _conclusion,
    _decision_audits,
    _evaluate,
    _put_plan_check,
    _start_role,
    _structured_report,
)

world = support.world
keys = support.keys
_clean_limiter = support._clean_limiter
_no_cloud = support._no_cloud

QUESTION = "Which verification should the operator run next?"


async def _principal(world, name, role):
    principal = await admin.create_principal(
        world.db, kind="agent", username=name, role_slug=role
    )
    key = await admin.create_api_key(
        world.db,
        principal["id"],
        name="test",
        projects=None if role == "admin" else [world.slug],
    )
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
    payload = read.json()
    assert payload.pop("decision_context")["missing"] == ["signal", "plan_check"]
    assert payload == {
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


async def _start_explicit(s, source_order_id=None):
    reply = await _request(s, source_order_id=source_order_id)
    assert reply.status_code == 200, reply.text
    lease = await _start_role(s)
    assert lease["id"] == reply.json()["order_id"]
    return lease


async def test_roadmap_order_routes_structured_result(scenario):
    s = scenario
    await support.admit(s.world)
    natural_task = await support.task_in_project(s.world, "needs owner decision")
    await support.enter_needs_decision(s.world, natural_task)
    assert await support.sweep(s.world) == 1
    natural = await _start_role(s)
    assert (await _structured_report(s, natural, _conclusion(None))).status_code == 422
    assert (
        await support.report(
            s.world, "k8", natural["id"], natural["lease_id"], note="Владельцу: сверить"
        )
    ).status_code == 200
    assert (await support.order_row(s.world, natural["id"]))[
        "note"
    ] == "Владельцу: сверить"
    assert (await repo.get_task(s.world.db, natural_task))["status"] == "needs_decision"

    generation = await _approve_plan_check(s)
    source_task = await support.task_in_project(s.world, "redeem signal")
    source_order = await _record_redeem_failure(s.world, source_task)
    granted = await _grant(s, signal_task_id=source_task)
    assert granted.status_code == 200, granted.text
    explicit = await _start_explicit(s, source_order)
    result = _conclusion(source_order)
    report = await _structured_report(s, explicit, result)
    assert report.status_code == 200, report.text
    assert (await _structured_report(s, explicit, result)).json()["repeat"] is True
    changed = await _structured_report(s, explicit, _conclusion(None, "owner_required"))
    assert changed.status_code == 409
    assert (await support.order_row(s.world, explicit["id"]))[
        "note"
    ] == "Краткая заметка"
    got = await s.world.client.get(_grant_path(s.task_id), headers=s.steward)
    assert got.json()["pending_decision_order_id"] == explicit["id"]
    replies = await asyncio.gather(
        _evaluate(s, explicit["id"]), _evaluate(s, explicit["id"])
    )
    assert all(response.status_code == 200 for response in replies)
    first, repeat = sorted((r.json() for r in replies), key=lambda row: row["repeat"])
    assert first == {**repeat, "repeat": False}
    assert repeat["repeat"] is True
    assert first["status"] == "accepted" and first["recipient"] == "steward"
    assert first["reason"] == "hold_expansion"
    assert first["plan"] == {
        "task_id": s.task_id,
        "approval_id": granted.json()["approval_id"],
        "statement_generation": generation,
    }
    assert first["signal"]["order_id"] == source_order
    assert first["verification_condition"] == {
        "id": "AC-1",
        "metric": "successful_fresh_redeems",
        "operator": "gte",
        "target": 2,
        "observed": None,
    }
    got = await s.world.client.get(_grant_path(s.task_id), headers=s.steward)
    assert got.json()["pending_decision_order_id"] is None
    assert got.json()["signals"] == []
    audit = await _decision_audits(s)
    assert len(audit) == 1
    assert audit[0]["actor_principal_id"] == s.grant_body["steward_principal_id"]
    assert json.loads(audit[0]["detail"])["decision"] == first
    assert result["risk"] in audit[0]["detail"]
    assert len(await support.order_rows(s.world)) == 2
    assert len(await support.rows(s.world.db, "SELECT * FROM executor_orders")) == 1


@pytest.mark.parametrize("invalidate", ["revision", "revoke", "new_approval"])
async def test_roadmap_context_stays_within_active_grant(scenario, invalidate):
    s = scenario
    generation = await _approve_plan_check(s)
    source_task = await support.task_in_project(s.world, "assigned signal")
    source_order = await _record_redeem_failure(s.world, source_task)
    await support.admit(s.world)
    granted = await _grant(s, signal_task_id=source_task)
    assert granted.status_code == 200, granted.text
    lease = await _start_explicit(s, source_order)
    key = lease["source_key"]
    order_id = lease["id"]
    for bad in (
        await _question(s, order_id, s.other_guest, key),
        await _question(s, order_id + 100, s.guest, key),
        await _question(s, order_id, s.guest, key[:-1]),
    ):
        assert bad.status_code == 404
        assert "decision_context" not in bad.text and QUESTION not in bad.text
    good = await _question(s, order_id, s.guest, key)
    assert good.status_code == 200
    context = good.json()["decision_context"]
    assert context == {
        "plan": {
            "task_id": s.task_id,
            "approval_id": granted.json()["approval_id"],
            "statement_generation": generation,
        },
        "signal": {
            "task_id": source_task,
            "order_id": source_order,
            "outcome": "failed",
            "reason": "redeem_failed",
        },
        "checks": [
            {
                "id": "AC-1",
                "metric": "successful_fresh_redeems",
                "operator": "gte",
                "target": 2,
            }
        ],
        "missing": [],
    }
    if invalidate == "revoke":
        assert (
            await s.world.client.delete(_grant_path(s.task_id), headers=s.owner)
        ).status_code == 200
    elif invalidate == "revision":
        await _put_plan_check(s, 3)
    else:
        await repo.insert_event(
            s.world.db, kind="task_approved", actor="human", task_id=s.task_id
        )
        await s.world.db.commit()
    refused = await _question(s, order_id, s.guest, key)
    assert refused.status_code == 404
    assert "decision_context" not in refused.text


@pytest.mark.parametrize(
    ("recommendation", "expected", "reason"),
    [
        ("hold_expansion", "blocked", "missing_plan_check"),
        ("owner_required", "owner_required", "outside_delegation"),
        ("insufficient_facts", "blocked", "missing_plan_check"),
    ],
)
async def test_roadmap_recommendation_respects_decision_boundary(
    scenario, recommendation, expected, reason
):
    s = scenario
    await _approve_plan_check(s)
    await _put_plan_check(s, 3)  # An edit after approval, before an owner grants.
    source_task = await support.task_in_project(s.world, "historical redeem failure")
    source_order = await _record_redeem_failure(s.world, source_task)
    await support.admit(s.world)
    granted = await _grant(s, signal_task_id=source_task)
    assert granted.status_code == 200, granted.text
    lease = await _start_explicit(s, source_order)
    context = (await _question(s, lease["id"], source_key=lease["source_key"])).json()[
        "decision_context"
    ]
    assert context["checks"] == [] and context["missing"] == ["plan_check"]
    report = await _structured_report(
        s, lease, _conclusion(source_order, recommendation)
    )
    assert report.status_code == 200, report.text
    assert (await _evaluate(s, lease["id"], auth=s.other_steward)).status_code == 404
    assert (await _evaluate(s, lease["id"], auth=s.owner)).status_code == 403
    assert (await _evaluate(s, lease["id"], auth=s.guest)).status_code == 403
    assert (
        await _evaluate(s, lease["id"], grant_id=granted.json()["grant_id"] + 1)
    ).status_code == 409
    assert await _decision_audits(s) == []
    decision = (await _evaluate(s, lease["id"])).json()
    assert decision["status"] == expected and decision["reason"] == reason
    assert decision["verification_condition"] is None
    assert decision["recipient"] == (
        "owner" if expected == "owner_required" else "steward"
    )
    assert (await repo.get_task(s.world.db, s.task_id))["status"] == "open"
    assert len(await support.order_rows(s.world)) == 1
    assert len(await support.rows(s.world.db, "SELECT * FROM executor_orders")) == 1


async def test_historical_order_40_is_preserved_without_issuing_again(scenario):
    s = scenario
    source_task = await support.task_in_project(s.world, "saved signal")
    grant = await _grant(s, signal_task_id=source_task)
    assert grant.status_code == 200, grant.text
    run_id = await repo.create_executor_run(
        s.world.db,
        task_id=source_task,
        submission_generation=1,
        agent_id="",
        run_id="saved-37",
        model="codex",
        channel="slot",
        slot="k8",
    )
    await s.world.db.execute(
        "UPDATE executor_runs SET outcome='aborted' WHERE id=?", (run_id,)
    )
    await s.world.db.execute(
        "INSERT INTO executor_orders "
        "(id,host,task_id,generation,executor_run_id,state,expires_at,close_reason,report_outcome,report_reason_code) "
        "VALUES (37,'k8',?,1,?,'closed',datetime('now'),'redeem_failed','failed','redeem_failed')",
        (source_task, run_id),
    )
    historical = (
        "Рекомендую паузу: сообщён redeem_failed; связь с пилотом и план неизвестны."
    )
    for order_id, outcome, note in ((39, "failed", ""), (40, "done", historical)):
        await s.world.db.execute(
            "INSERT INTO role_orders "
            "(id,role,source_key,task_id,project_id,state,outcome,note,host) "
            "VALUES (?,'roadmap',?,?,?,?,?,?,'k8')",
            (
                order_id,
                f"steward_request:{order_id:032x}",
                s.task_id,
                s.world.pid,
                outcome,
                outcome,
                note,
            ),
        )
    cursor = await s.world.db.execute(
        "INSERT INTO roadmap_requests (grant_id,request_id,question,source_order_id,order_id) "
        "VALUES (?,'saved-40',?,37,39)",
        (grant.json()["grant_id"], QUESTION),
    )
    await s.world.db.execute(
        "INSERT INTO roadmap_recoveries "
        "(request_pk,order_id,operator_id,evidence_update_id,evidence_sha256,runtime_sha256) "
        "VALUES (?,40,?,22506,?,?)",
        (cursor.lastrowid, s.world.human_id, "1" * 64, "2" * 64),
    )
    await repo.update_task(s.world.db, s.task_id, status="completed")
    await s.world.db.commit()

    saved = await roadmap_requests._request(s.world.db, 40)
    assert saved is not None and saved["initial_order_id"] == 39
    assert await roadmap_requests._signal_is_current(s.world.db, saved, 37)
    context = roadmap_decisions.decision_context(saved)
    assert context["signal"]["order_id"] == 37
    assert context["checks"] == [] and context["missing"] == ["plan_check"]
    assert (await _evaluate(s, 40)).status_code == 409
    assert (
        await _question(s, 40, source_key=f"steward_request:{40:032x}")
    ).status_code == 404
    assert (await support.order_row(s.world, 40))["note"] == historical
    assert [r["id"] for r in await support.order_rows(s.world)] == [39, 40]
    assert await _decision_audits(s) == []
