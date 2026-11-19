"""An attested recovery is a new role order under the original request."""

import argparse
import asyncio

from hub import mcp_task_card, repository as repo, roadmap_cli
from hub.services import role_orders
from tests import test_roadmap_requests as roadmap

scenario = roadmap.scenario
world = roadmap.world
keys = roadmap.keys
_clean_limiter = roadmap._clean_limiter
_no_cloud = roadmap._no_cloud


async def _failed(s):
    support = roadmap.support
    source = await support.task_in_project(s.world, "executor signal")
    assert (await roadmap._grant(s, signal_task_id=source)).status_code == 200
    signal = await roadmap._record_redeem_failure(s.world, source)
    response = await roadmap._request(s, source_order_id=signal)
    initial = response.json()["order_id"]
    await support.admit(s.world)
    lease = await support.leased(s.world)
    assert (
        await support.started(s.world, "k8", initial, lease["lease_id"])
    ).status_code == 200
    assert (
        await support.report(s.world, "k8", initial, lease["lease_id"], "failed")
    ).status_code == 200
    evidence = await repo.add_task_update(
        s.world.db,
        s.task_id,
        "operator",
        "status",
        "Host diagnostics and fixed runtime smoke",
        principal_id=s.world.human_id,
        author_kind="principal",
    )
    await s.world.db.commit()
    return (
        initial,
        signal,
        {
            "evidence_update_id": evidence,
            "evidence_sha256": "a" * 64,
            "runtime_sha256": "b" * 64,
            "model_did_not_start": True,
            "runtime_smoke_passed": True,
        },
    )


async def _recover(s, order_id, body, *, auth=None):
    return await s.world.client.post(
        f"/api/tasks/{s.task_id}/roadmap-orders/{order_id}/recover",
        headers=s.owner if auth is None else auth,
        json=body,
    )


async def test_concurrent_recovery_preserves_original(scenario):
    s = scenario
    first, _, evidence = await _failed(s)
    original = await roadmap.support.order_row(s.world, first)
    replies = await asyncio.gather(*(_recover(s, first, evidence) for _ in range(2)))
    assert all(reply.status_code == 200 for reply in replies), [r.text for r in replies]
    order_ids = {reply.json()["order_id"] for reply in replies}
    assert {reply.json()["repeat"] for reply in replies} == {True, False}
    (recovered,) = order_ids
    assert recovered != first
    assert await roadmap.support.order_row(s.world, first) == original
    lease = await roadmap.support.leased(s.world)
    assert lease["id"] == recovered and lease["source_key"] != original["source_key"]
    assert (
        await roadmap._question(s, recovered, source_key=lease["source_key"])
    ).status_code == 404
    assert (
        await roadmap.support.started(s.world, "k8", recovered, lease["lease_id"])
    ).status_code == 200
    assert (
        await roadmap._question(s, recovered, source_key=original["source_key"])
    ).status_code == 404
    read = await roadmap._question(s, recovered, source_key=lease["source_key"])
    assert read.status_code == 200, read.text
    assert read.json()["question"] == roadmap.QUESTION
    orders = await role_orders.card_view(s.world.db, s.task_id)
    assert orders[0]["recovery_of"] == first
    assert "recovery_of" not in orders[1]
    lines = mcp_task_card.role_order_lines({"role_orders": orders})
    assert f"восстановление #{first}" in lines[0]
    assert (
        await roadmap._question(
            s, recovered, auth=s.other_guest, source_key=lease["source_key"]
        )
    ).status_code == 404
    audit = await roadmap.support.rows(
        s.world.db,
        "SELECT action, actor_principal_id FROM admin_audit_log "
        "WHERE action IN ('roadmap_requested', 'roadmap_recovered') ORDER BY id",
    )
    (owner,) = await roadmap.support.rows(
        s.world.db, "SELECT id FROM principals WHERE username = 'roadmap-owner'"
    )
    assert [(row["action"], row["actor_principal_id"]) for row in audit] == [
        ("roadmap_requested", s.grant_body["steward_principal_id"]),
        ("roadmap_recovered", owner["id"]),
    ]


async def test_initial_request_remains_idempotent(scenario):
    s = scenario
    first, source, evidence = await _failed(s)
    recovery = await _recover(s, first, evidence)
    assert recovery.status_code == 200
    assert (await _recover(s, recovery.json()["order_id"], evidence)).status_code == 409
    assert (
        await _recover(s, first, dict(evidence, runtime_sha256="c" * 64))
    ).status_code == 409
    replay = await roadmap._request(s, source_order_id=source)
    assert replay.json() == {"order_id": first, "repeat": True}
    assert (
        await roadmap._request(s, source_order_id=source, request_id="new")
    ).status_code == 409
    rows = await roadmap.support.order_rows(s.world)
    assert len(rows) == 2


async def test_recovery_refuses_unsafe_requests(scenario):
    s = scenario
    first, _, evidence = await _failed(s)
    assert (await _recover(s, first, evidence, auth=s.steward)).status_code == 403
    invalid = dict(evidence, model_did_not_start=False)
    assert (await _recover(s, first, invalid)).status_code == 422
    unknown = dict(evidence, evidence_update_id=999999)
    assert (await _recover(s, first, unknown)).status_code == 409
    hub_update = await repo.add_task_update(
        s.world.db, s.task_id, "hub", "status", "Untrusted role result"
    )
    await s.world.db.commit()
    assert (
        await _recover(s, first, dict(evidence, evidence_update_id=hub_update))
    ).status_code == 409
    await s.world.client.delete(roadmap._grant_path(s.task_id), headers=s.owner)
    assert (await _recover(s, first, evidence)).status_code == 409
    assert len(await roadmap.support.order_rows(s.world)) == 1


def test_recovery_cli_calls_operator_route():
    parser = argparse.ArgumentParser()
    calls = []
    roadmap_cli.add_parser(
        parser.add_subparsers(), lambda *args: calls.append(args), lambda _: None
    )
    args = parser.parse_args(
        [
            "roadmap",
            "recover",
            "2386",
            "39",
            "--evidence-update-id",
            "42",
            "--evidence-sha256",
            "a" * 64,
            "--runtime-sha256",
            "b" * 64,
            "--model-did-not-start",
            "--runtime-smoke-passed",
        ]
    )
    assert args.func(args) == 0
    assert calls == [
        (
            "POST",
            "/api/tasks/2386/roadmap-orders/39/recover",
            {
                "evidence_update_id": 42,
                "evidence_sha256": "a" * 64,
                "runtime_sha256": "b" * 64,
                "model_did_not_start": True,
                "runtime_smoke_passed": True,
            },
        )
    ]


async def test_active_explicit_order_blocks_recovery(scenario):
    s = scenario
    first, _, evidence = await _failed(s)
    (grant,) = await roadmap.support.rows(
        s.world.db,
        "SELECT signal_task_id FROM roadmap_grants WHERE id = ?",
        (s.grants[s.task_id],),
    )
    later = await roadmap._record_redeem_failure(s.world, grant["signal_task_id"])
    assert (
        await roadmap._request(s, source_order_id=later, request_id="another-signal")
    ).status_code == 200
    refused = await _recover(s, first, evidence)
    assert refused.status_code == 409
    assert refused.json()["detail"]["reason"] == "roadmap_target_busy"
    assert len(await roadmap.support.order_rows(s.world)) == 2
