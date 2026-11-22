"""#2414 — live authority and owner-visible evidence for the current order."""

import json

import pytest
from fastapi import HTTPException

from hub import repository as repo
from hub.db import write_transaction
from hub.services import admin, ci_contract, network_receipt as pilot_service
from hub.services import network_receipt_verification as verifier
from tests import role_orders_support as support
from tests import test_network_receipt_mandate as scenarios
from tests import test_network_receipt_submission as submission

_receipt_task_type = scenarios._receipt_task_type
approved = scenarios.approved
keys = scenarios.keys
pilot = scenarios.pilot
world = scenarios.world
_clean_limiter = scenarios._clean_limiter
_no_cloud = scenarios._no_cloud


async def _identity(world, headers):
    return await admin.resolve_api_key(
        world.db, headers["Authorization"].removeprefix("Bearer ")
    )


@pytest.mark.parametrize(
    "change", ["revoke", "expire", "scope", "disable", "role", "kind"]
)
async def test_steward_authority_is_reread_at_the_submission_write(
    pilot, monkeypatch, change
):
    world, mandate, _ = pilot
    await submission._publish(world)
    identity = await _identity(world, world.steward_auth)
    ver, _, _ = submission._external(monkeypatch)

    async def withdraw(db, **_kwargs):
        # The middleware already authenticated this request. A later check
        # using the same object alone would still pass.
        assert not db.in_transaction
        if change == "revoke":
            await admin.revoke_api_key(world.db, identity.api_key_id)
        elif change == "expire":
            await world.db.execute(
                "UPDATE api_keys SET expires_at=datetime('now','-1 hour') WHERE id=?",
                (identity.api_key_id,),
            )
        elif change == "scope":
            await world.db.execute(
                "UPDATE api_keys SET scopes=? WHERE id=?",
                ('["project:other"]', identity.api_key_id),
            )
        elif change == "disable":
            await world.db.execute(
                "UPDATE principals SET status='disabled' WHERE id=?",
                (identity.principal_id,),
            )
        elif change == "kind":
            await world.db.execute(
                "UPDATE principals SET kind='human' WHERE id=?",
                (identity.principal_id,),
            )
        else:
            await world.db.execute(
                "DELETE FROM principal_roles WHERE principal_id=?",
                (identity.principal_id,),
            )
        await world.db.commit()
        return submission.GREEN

    ver.side_effect = withdraw
    response = await scenarios._issue(world)
    assert response.status_code == 403, response.text
    assert (await repo.get_task(world.db, mandate.task_id))["status"] == "running"
    assert await support.rows(
        world.db, "SELECT submitted_generation FROM network_receipt_orders"
    ) == [{"submitted_generation": None}]


async def test_host_key_can_only_represent_its_live_principal(pilot):
    world, _, _ = pilot
    who = await _identity(world, world.k8_auth)
    other = await _identity(world, world.other_steward_auth)
    who.api_key_id = other.api_key_id
    async with write_transaction(world.db):
        with pytest.raises(HTTPException) as blocked:
            await pilot_service._require_live_key(world.db, who, "slot_runner")
        assert blocked.value.status_code == 403


async def test_card_tracks_current_stamp_ci_and_separate_verdict(pilot, monkeypatch):
    world, mandate, _ = pilot
    task_id = mandate.task_id
    order = await submission._publish(world)
    monkeypatch.setattr(verifier, "TASK_ID", task_id)
    monkeypatch.setattr(verifier, "BRANCH", pilot_service.BRANCH)
    workflow = ".github/workflows/tests.yml"
    policy = {"ci_contract": {"workflow_path": workflow, "required_checks": ["tests"]}}
    await repo.update_project(world.db, world.pid, gate_policy=json.dumps(policy))
    await world.db.execute(
        "INSERT INTO ci_run_reports(task_id,head_sha,checks,validation_status,trust,contract_fp,provenance) "
        "VALUES (?,?,?,'unknown','verified',?,?)",
        (
            task_id,
            scenarios._COMMIT,
            '{"tests":"pass"}',
            ci_contract.fingerprint_of(workflow, ["tests"]),
            json.dumps(
                dict(
                    head_repository=pilot_service.REPO,
                    workflow_path=workflow,
                    run_conclusion="success",
                )
            ),
        ),
    )

    async def read():
        await world.db.commit()
        view = await pilot_service.card_view(world.db, task_id)
        assert view["order_id"] == order["order_id"]
        return view

    # Green CI may arrive before the first submission; the publication remains a claim.
    published = await read()
    assert published["stage"] == "published" and not published["accepted"]
    await world.db.execute("UPDATE network_receipt_orders SET submitted_generation=1")
    await repo.update_task(
        world.db,
        task_id,
        status="review",
        submission_generation=1,
        submission_sha=scenarios._COMMIT,
    )
    pending = await read()
    assert (
        pending["stage"] == "awaiting_verdict"
        and pending["ci_verified"]
        and not pending["accepted"]
    )
    await repo.update_task(
        world.db, task_id, review_verdict_generation=1, review_verdict="approved"
    )
    withdrawn = await read()
    assert withdrawn["stage"] == "held" and not withdrawn["accepted"]
    await repo.insert_event(
        world.db,
        kind="review_verdict_recorded",
        task_id=task_id,
        actor="pilot-administrator",
        payload={
            "verdict": "approved",
            "submission_generation": 1,
            "principal_id": mandate.approver_id,
            "human": True,
        },
    )
    accepted = await read()
    assert accepted["stage"] == "accepted" and accepted["accepted"]
    # Ordinary pair approval without an attached PR returns to running before report_done.
    await repo.update_task(world.db, task_id, status="running")
    assert (await read())["accepted"]
    await world.db.execute(
        "UPDATE api_keys SET revoked_at=datetime('now') WHERE id=(SELECT ready_key_id FROM network_receipt_mandates)"
    )
    held = await read()
    assert held["stage"] == "held" and held["verdict_verified"] and not held["accepted"]
    await world.db.execute(
        "UPDATE api_keys SET revoked_at=NULL WHERE id=(SELECT ready_key_id FROM network_receipt_mandates)"
    )
    await world.db.execute("UPDATE ci_run_reports SET trust='unverified'")
    withdrawn = await read()
    assert withdrawn["stage"] == "awaiting_ci" and not withdrawn["accepted"]
    await world.db.execute("UPDATE ci_run_reports SET trust='verified'")
    await repo.update_task(world.db, task_id, review_verdict_closed_generation=1)
    withdrawn = await read()
    assert (
        withdrawn["stage"] == "verdict_withdrawn" and not withdrawn["verdict_verified"]
    )
    await repo.update_task(
        world.db,
        task_id,
        review_verdict_closed_generation=None,
        review_verdict="changes_requested",
    )
    feedback = await read()
    assert feedback["stage"] == "changes_requested" and not feedback["accepted"]
    await repo.update_task(
        world.db, task_id, submission_generation=2, review_verdict_generation=2
    )
    changed = await read()
    assert changed["stage"] == "submission_changed" and not changed["accepted"]
    await pilot_service.revoke(world.db)
    revoked = await read()
    assert revoked["stage"] == "revoked" and not revoked["accepted"]
    assert revoked["commit_sha"] == scenarios._COMMIT


async def test_revoked_ready_key_holds_issuance_until_fresh_readiness(pilot):
    world, _, _ = pilot
    assert (await scenarios._ready(world)).status_code == 200
    old = await _identity(world, world.k8_auth)
    replacement = await scenarios._key(world, old.principal_id)
    await admin.revoke_api_key(world.db, old.api_key_id)
    assert (await scenarios._issue(world)).status_code == 204
    assert not await support.rows(world.db, "SELECT id FROM network_receipt_orders")
    assert (await scenarios._ready(world, auth=replacement)).status_code == 200
    assert (await scenarios._issue(world)).status_code == 200


async def test_already_issued_order_stays_bound_to_the_ready_credential(pilot):
    world, _, _ = pilot
    assert (await scenarios._ready(world)).status_code == 200
    assert (await scenarios._issue(world)).status_code == 200
    original = await _identity(world, world.k8_auth)
    replacement = await scenarios._key(world, original.principal_id)
    assert (await scenarios._ready(world, auth=replacement)).status_code == 409
    assert (await scenarios._order(world, auth=replacement)).status_code == 409
    assert (await scenarios._order(world)).status_code == 200
