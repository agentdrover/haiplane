"""The exact published receipt submits once and awaits an ordinary human verdict."""

import asyncio
from unittest.mock import AsyncMock

from hub import repository as repo
from hub.services import (
    lifecycle,
    network_receipt_verification as verifier,
    review_dispatch,
)
from tests import role_orders_support as support
from tests import test_network_receipt_mandate as scenarios

_receipt_task_type = scenarios._receipt_task_type
approved = scenarios.approved
keys = scenarios.keys
pilot = scenarios.pilot
world = scenarios.world
_clean_limiter = scenarios._clean_limiter
_no_cloud = scenarios._no_cloud

GREEN = verifier.ReceiptVerification(commit_verified=True, ci_verified=True)


async def _publish(world):
    assert (await scenarios._ready(world)).status_code == 200
    response = await scenarios._issue(world)
    assert response.status_code == 200, response.text
    order = response.json()
    assert (
        await scenarios._post_receipt(world, scenarios._receipt(order))
    ).status_code == 200
    publication = await world.client.post(
        f"{scenarios._HOST}/orders/{order['order_id']}/publication",
        headers=world.k8_auth,
        json={"commit_sha": scenarios._COMMIT},
    )
    assert publication.status_code == 200, publication.text
    return order


def _external(monkeypatch, *, proof=GREEN, final=GREEN, tip=scenarios._COMMIT):
    verify = AsyncMock(return_value=proof)
    recheck = AsyncMock(return_value=final)
    resolve_branch = AsyncMock(return_value=(tip, ""))
    monkeypatch.setattr(verifier, "verify_network_receipt", verify)
    monkeypatch.setattr(verifier, "recheck_network_receipt", recheck)
    monkeypatch.setattr(lifecycle, "resolve_branch_tip", resolve_branch)
    return verify, recheck, resolve_branch


async def test_verified_publication_submits_once_without_model_or_verdict(
    pilot, monkeypatch
):
    world, mandate, _ = pilot
    parent = await support.task_in_project(world, "parent receipt task")
    await repo.update_task(world.db, mandate.task_id, project_id=None, parent_id=parent)
    await world.db.commit()
    task = await repo.get_task(world.db, mandate.task_id)
    assert task["project_id"] is None
    order = await _publish(world)
    verify, recheck, resolve = _external(monkeypatch)
    model_gate = AsyncMock(side_effect=AssertionError("checked paid model policy"))
    monkeypatch.setattr(review_dispatch, "_policy_and_novelty_allow", model_gate)

    tick = await scenarios._issue(world)
    assert tick.status_code == 200 and tick.json() == order, tick.text
    task = await repo.get_task(world.db, mandate.task_id)
    assert task["status"] == "review"
    assert task["submission_sha"] == scenarios._COMMIT
    assert task["submission_generation"] == 1
    assert not task["review_job_id"] and not task["review_verdict"]
    assert not await support.rows(world.db, "SELECT * FROM review_dispatches")
    assert not await support.rows(world.db, "SELECT * FROM machine_reviews")
    assert not await review_dispatch.maybe_dispatch_review(world.db, mandate.task_id)
    assert not await review_dispatch.maybe_dispatch_review(
        world.db, mandate.task_id, force_profile=review_dispatch.DEEP
    )
    assert not await review_dispatch.dispatch_local_review(
        world.db, dict(task), "github", task["branch"], task["submission_generation"]
    )
    model_gate.assert_not_awaited()

    replay = await scenarios._issue(world)
    assert replay.status_code == 200 and replay.json() == order, replay.text
    assert (await repo.get_task(world.db, mandate.task_id))[
        "submission_generation"
    ] == 1
    verify.assert_awaited_once()
    recheck.assert_awaited_once()
    resolve.assert_awaited_once()


async def test_pending_ci_does_not_submit(pilot, monkeypatch):
    world, mandate, _ = pilot
    order = await _publish(world)
    verify, recheck, resolve = _external(
        monkeypatch, proof=verifier.ReceiptVerification(commit_verified=True)
    )
    assert (await scenarios._issue(world)).json() == order
    assert (await repo.get_task(world.db, mandate.task_id))["status"] == "running"
    verify.assert_awaited_once()
    recheck.assert_not_awaited()
    resolve.assert_not_awaited()


async def test_revoked_after_external_proof_cannot_submit(pilot, monkeypatch):
    world, mandate, owner = pilot
    await _publish(world)
    verify, recheck, _ = _external(monkeypatch)

    async def revoke_during_verification(db, **_kwargs):
        assert not db.in_transaction
        revoked = await world.client.delete(f"{scenarios._TASK}/mandate", headers=owner)
        assert revoked.status_code == 204
        return GREEN

    verify.side_effect = revoke_during_verification
    tick = await scenarios._issue(world)
    assert tick.status_code == 409, tick.text
    assert (await repo.get_task(world.db, mandate.task_id))["status"] == "running"
    rows = await support.rows(
        world.db, "SELECT submitted_generation FROM network_receipt_orders"
    )
    assert rows == [{"submitted_generation": None}]
    recheck.assert_not_awaited()


async def test_withdrawn_ci_or_moved_branch_never_stamps_submission(pilot, monkeypatch):
    world, mandate, _ = pilot
    await _publish(world)
    _, recheck, resolve = _external(
        monkeypatch, final=verifier.ReceiptVerification(commit_verified=True)
    )
    lost_ci = await scenarios._issue(world)
    assert lost_ci.status_code == 409, lost_ci.text
    recheck.assert_awaited_once()
    recheck.reset_mock()
    resolve.return_value = ("d" * 40, "")
    moved = await scenarios._issue(world)
    assert moved.status_code == 409 and moved.json()["detail"]["reason"] == "head_moved"
    recheck.assert_not_awaited()
    assert (await repo.get_task(world.db, mandate.task_id))["status"] == "running"
    rows = await support.rows(
        world.db, "SELECT submitted_generation FROM network_receipt_orders"
    )
    assert rows == [{"submitted_generation": None}]


async def test_concurrent_steward_ticks_bind_one_generation(pilot, monkeypatch):
    world, mandate, _ = pilot
    order = await _publish(world)
    verify, recheck, _ = _external(monkeypatch)
    both_verified = asyncio.Event()
    verification_calls = 0

    async def verify_concurrently(db, **_kwargs):
        assert not db.in_transaction
        nonlocal verification_calls
        verification_calls += 1
        if verification_calls == 2:
            both_verified.set()
        await asyncio.wait_for(both_verified.wait(), timeout=5)
        return GREEN

    verify.side_effect = verify_concurrently
    ticks = await asyncio.gather(scenarios._issue(world), scenarios._issue(world))
    for tick in ticks:
        assert tick.status_code == 200 and tick.json() == order, tick.text
    assert (await repo.get_task(world.db, mandate.task_id))[
        "submission_generation"
    ] == 1
    rows = await support.rows(
        world.db, "SELECT submitted_generation FROM network_receipt_orders"
    )
    assert rows == [{"submitted_generation": 1}]
    recheck.assert_awaited_once()


async def test_ordinary_submit_cannot_bypass_independent_proof(pilot, monkeypatch):
    world, mandate, owner = pilot
    await _publish(world)
    verify, recheck, resolve = _external(monkeypatch)
    for query in ("", "?dry_run=1"):
        response = await world.client.post(
            f"/api/tasks/{mandate.task_id}/submit-review{query}",
            headers=owner,
            json={"expected_sha": scenarios._COMMIT},
        )
        assert response.status_code == 409, response.text
        assert (
            response.json()["detail"]
            == "submit this network receipt through the steward tick"
        )
    verify.assert_not_awaited()
    recheck.assert_not_awaited()
    resolve.assert_not_awaited()
    assert (await repo.get_task(world.db, mandate.task_id))[
        "submission_generation"
    ] == 0


async def test_dedicated_done_waits_for_human_verdict_and_completes_normally(
    pilot, monkeypatch
):
    world, mandate, owner = pilot
    order = await _publish(world)
    verify, recheck, _ = _external(monkeypatch)
    response = await world.client.post(
        f"/api/tasks/{mandate.task_id}/updates",
        headers=owner,
        json={"kind": "done", "content": "Premature completion"},
    )
    assert response.status_code == 409, response.text
    assert (await scenarios._issue(world)).json() == order
    waiting = await scenarios._issue(world)
    assert waiting.status_code == 200 and waiting.json() == order
    row = await repo.get_task(world.db, mandate.task_id)
    assert row["status"] == "review" and not row["review_verdict"]

    accepted = await world.client.post(
        f"/api/tasks/{mandate.task_id}/review-verdict",
        headers=owner,
        json={
            "verdict": "approved",
            "expected_generation": 1,
            "comments": "Owner accepted evidence",
        },
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "running"
    final = verifier.ReceiptVerification(True, True, True, True)
    verify.return_value = recheck.return_value = final
    tick = await scenarios._issue(world)
    assert tick.status_code == 200 and tick.json() == order, tick.text
    row = await repo.get_task(world.db, mandate.task_id)
    assert row["status"] == "completed"
    assert row["review_verdict"] == "approved" and row["submission_generation"] == 1
    again = await scenarios._issue(world)
    assert again.status_code == 200 and again.json() == order
    updates = await repo.get_task_updates(world.db, mandate.task_id)
    assert sum(row["kind"] == "done" for row in updates) == 1
