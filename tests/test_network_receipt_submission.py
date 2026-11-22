"""The exact published receipt submits once and awaits an ordinary human verdict."""

import asyncio
import subprocess
from unittest.mock import AsyncMock

from hub import repository as repo
from hub.integrations.git_ops import GitOpsIntegration
from hub.integrations.registry import plugins
from hub.services import (
    lifecycle,
    network_receipt as receipt_service,
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


def _git(directory, *arguments):
    return subprocess.check_output(
        ["git", "-C", str(directory), *arguments], text=True
    ).strip()


def _receipt_origin(path, branch):
    origin = path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare", "--initial-branch=main")
    working = path / "working"
    _git(path, "clone", str(origin), str(working))
    _git(working, "config", "user.name", "Receipt fixture")
    _git(working, "config", "user.email", "receipt@example.invalid")
    _git(working, "commit", "--allow-empty", "-m", "Measured main")
    head = _git(working, "rev-parse", "HEAD")
    _git(working, "switch", "-c", branch)
    receipt = working / "evidence" / "k8-network-receipt-2413.json"
    receipt.parent.mkdir()
    receipt.write_text("{}\n")
    _git(working, "add", "evidence")
    _git(working, "commit", "-m", "Publish receipt")
    commit = _git(working, "rev-parse", "HEAD")
    _git(working, "switch", "main")
    _git(working, "commit", "--allow-empty", "-m", "Advance main")
    _git(working, "push", "origin", "main", branch)
    hub_clone = path / "hub"
    _git(path, "clone", str(origin), str(hub_clone))
    assert _git(hub_clone, "rev-parse", "main") != head
    assert _git(hub_clone, "merge-base", "main", f"origin/{branch}") == head
    return origin, hub_clone, head, commit


async def test_dedicated_done_preserves_verified_receipt_when_main_advances(
    pilot, monkeypatch, tmp_path
):
    world, mandate, owner = pilot
    branch = receipt_service.BRANCH
    origin, hub_clone, head, commit = _receipt_origin(tmp_path, branch)
    monkeypatch.setattr(scenarios, "_HEAD", head)
    monkeypatch.setattr(scenarios, "_COMMIT", commit)
    git_ops = GitOpsIntegration()

    async def diff_paths(observed_branch, **_context):
        return await git_ops.branch_diff_paths(
            observed_branch, base_branch="main", repo=str(hub_clone)
        )

    assert await diff_paths(branch) == ["evidence/k8-network-receipt-2413.json"]
    discover_pr = AsyncMock(return_value=None)
    monkeypatch.setattr(plugins.git_ops, "branch_diff_paths", diff_paths)
    monkeypatch.setattr(plugins.git_ops, "pr_for_branch", discover_pr)
    create_pr = AsyncMock(return_value=99)
    update_pr = AsyncMock()
    monkeypatch.setattr(plugins.git_ops, "create_pr", create_pr)
    monkeypatch.setattr(plugins.git_ops, "update_pr_branch", update_pr)
    order = await _publish(world)
    verify, recheck, _ = _external(monkeypatch, tip=commit)
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
    assert row["submission_sha"] == commit and row["pr_number"] is None
    discover_pr.assert_not_awaited()
    create_pr.assert_not_awaited()

    recheck.return_value = verifier.ReceiptVerification(True, True, True)
    target = f"/api/tasks/{mandate.task_id}/review-verdict"
    approval = {
        "verdict": "approved",
        "expected_generation": 1,
        "comments": "Owner accepted evidence",
    }
    for other in (world.agent_auth, world.human_auth):
        refused = await world.client.post(target, headers=other, json=approval)
        assert refused.status_code == 403, refused.text
    stale = await world.client.post(
        target, headers=owner, json=approval | {"expected_generation": 2}
    )
    assert stale.status_code == 409, stale.text
    accepted = await world.client.post(target, headers=owner, json=approval)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "running"
    final = verifier.ReceiptVerification(True, True, True, True)
    recheck.return_value = final

    async def live_verification(_db, **candidate):
        assert candidate["commit_sha"] == commit
        observed = _git(
            origin, "for-each-ref", "--format=%(objectname)", f"refs/heads/{branch}"
        )
        return final if observed == commit else verifier.ReceiptVerification()

    verify.side_effect = live_verification
    ref = f"refs/heads/{branch}"
    _git(origin, "update-ref", "-d", ref)
    direct = await world.client.post(
        f"/api/tasks/{mandate.task_id}/updates",
        headers=owner,
        json={"kind": "done", "content": "Attempt direct completion"},
    )
    assert direct.status_code == 409, direct.text
    assert (await scenarios._issue(world)).json() == order
    assert (await repo.get_task(world.db, mandate.task_id))["status"] == "running"
    updates = await repo.get_task_updates(world.db, mandate.task_id)
    assert not any(update["kind"] == "done" for update in updates)

    _git(origin, "update-ref", ref, commit)
    # Historical PR metadata is retained, but does not own delivery of evidence.
    await repo.update_task(world.db, mandate.task_id, pr_number=99)
    await world.db.commit()
    finish = lifecycle._finish_deferred_done
    with monkeypatch.context() as interrupted:
        interrupted.setattr(lifecycle, "_finish_deferred_done", AsyncMock())
        first = await scenarios._issue(world)
        assert first.status_code == 200 and first.json() == order
        assert (await repo.get_task(world.db, mandate.task_id))["status"] == "running"
    assert lifecycle._finish_deferred_done is finish
    tick = await scenarios._issue(world)
    assert tick.status_code == 200 and tick.json() == order, tick.text
    row = await repo.get_task(world.db, mandate.task_id)
    assert row["status"] == "completed"
    assert row["review_verdict"] == "approved" and row["submission_generation"] == 1
    assert row["submission_sha"] == commit and row["pr_number"] == 99
    discover_pr.assert_not_awaited()
    create_pr.assert_not_awaited()
    update_pr.assert_not_awaited()
    again = await scenarios._issue(world)
    assert again.status_code == 200 and again.json() == order
    updates = await repo.get_task_updates(world.db, mandate.task_id)
    assert sum(row["kind"] == "done" for row in updates) == 1


async def test_lost_worker_ack_after_submission_replays_only_the_recorded_result(
    pilot, monkeypatch
):
    world, mandate, _ = pilot
    order = await _publish(world)
    _external(monkeypatch)
    assert (await scenarios._issue(world)).status_code == 200
    publication = f"{scenarios._HOST}/orders/{order['order_id']}/publication"
    for status in ("review", "completed"):
        await repo.update_task(world.db, mandate.task_id, status=status)
        await world.db.commit()
        assert (await scenarios._order(world)).status_code == 204
        repeated = await scenarios._post_receipt(world, scenarios._receipt(order))
        assert repeated.status_code == 200 and repeated.json()["repeat"] is True
        assert (
            await world.client.post(
                publication,
                headers=world.k8_auth,
                json={"commit_sha": scenarios._COMMIT},
            )
        ).status_code == 200
        assert (
            await world.client.post(
                publication, headers=world.k8_auth, json={"commit_sha": "c" * 40}
            )
        ).status_code == 409
        assert (await repo.get_task(world.db, mandate.task_id))[
            "submission_generation"
        ] == 1


async def test_fixed_pilot_cannot_be_taken_before_steward_issues(pilot):
    world, mandate, owner = pilot
    routes = {
        "claim": {"agent": "other", "session_id": "other"},
        "start": {"plan": "other"},
        "pair-start": {"plan": "other", "git_mode": "remote"},
        "submit-review": {"expected_sha": scenarios._COMMIT},
        "force-complete": {"comment": "unsafe override"},
    }
    for method, body in routes.items():
        denied = await world.client.post(
            f"/api/tasks/{mandate.task_id}/{method}", headers=owner, json=body
        )
        assert denied.status_code == 409, (method, denied.text)
    row = await repo.get_task(world.db, mandate.task_id)
    assert row["status"] == "open" and row["submission_generation"] == 0
    assert not await support.rows(world.db, "SELECT * FROM network_receipt_orders")
    assert (await scenarios._ready(world)).status_code == 200
    assert (await scenarios._issue(world)).status_code == 200

    await repo.update_task(world.db, mandate.task_id, status="needs_decision")
    for action in ("accept", "rework"):
        denied = await world.client.post(
            f"/api/tasks/{mandate.task_id}/decide",
            headers=owner,
            json={"action": action, "decision_summary": "other"},
        )
        assert denied.status_code == 409, (action, denied.text)
    assert (await repo.get_task(world.db, mandate.task_id))[
        "status"
    ] == "needs_decision"

    ordinary = await support.task_in_project(world, "normal task")
    allowed = await world.client.post(
        f"/api/tasks/{ordinary}/pair-start",
        headers=owner,
        json={"plan": "normal workflow", "git_mode": "remote"},
    )
    assert allowed.status_code == 200 and allowed.json()["status"] == "running", (
        allowed.text
    )


async def test_issuer_revoked_after_final_check_cannot_record_done(pilot, monkeypatch):
    world, mandate, owner = pilot
    await _publish(world)
    verify, recheck, _ = _external(monkeypatch)
    result = await scenarios._issue(world)
    assert result.status_code == 200, result.text
    recheck.return_value = verifier.ReceiptVerification(True, True, True)
    verdict = await world.client.post(
        f"/api/tasks/{mandate.task_id}/review-verdict",
        headers=owner,
        json={
            "verdict": "approved",
            "expected_generation": 1,
            "comments": "Owner verified",
        },
    )
    assert verdict.status_code == 200, verdict.text
    final = verifier.ReceiptVerification(True, True, True, True)
    recheck.return_value = final

    async def revoke_after_verifying(db, **_kwargs):
        assert not db.in_transaction
        await world.db.execute(
            "UPDATE api_keys SET revoked_at=datetime('now') "
            "WHERE id=(SELECT steward_key_id FROM network_receipt_orders WHERE task_id=?)",
            (mandate.task_id,),
        )
        await world.db.commit()
        return final

    verify.side_effect = revoke_after_verifying
    denied = await scenarios._issue(world)
    assert denied.status_code == 409, denied.text
    assert (await repo.get_task(world.db, mandate.task_id))["status"] == "running"
    assert not any(
        row["kind"] == "done"
        for row in await repo.get_task_updates(world.db, mandate.task_id)
    )


async def test_project_drift_keeps_order_reserved_and_prevents_completion(
    pilot, monkeypatch
):
    from hub.services import network_receipt_submission

    world, mandate, owner = pilot
    await _publish(world)
    _external(monkeypatch)
    assert (await scenarios._issue(world)).status_code == 200
    await repo.update_project(world.db, world.pid, repo="other/repository")
    await world.db.commit()
    task = dict(await repo.get_task(world.db, mandate.task_id))
    assert not await network_receipt_submission.receipt_completion_allowed(
        world.db, task
    )
    assert await review_dispatch._automation_not_applicable_to_review(world.db, task)
    response = await world.client.post(
        f"/api/tasks/{mandate.task_id}/force-complete",
        headers=owner,
        json={"comment": "override"},
    )
    assert response.status_code == 409
    assert (await scenarios._issue(world)).status_code == 409
