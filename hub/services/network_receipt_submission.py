"""Submit the verified fixed #2413 receipt through the ordinary pair lifecycle."""

from __future__ import annotations

from fastapi import HTTPException

from hub import repository as repo
from hub.models import TaskSubmitReview, TaskUpdateCreate
from hub.services import (
    executor_orders,
    network_receipt as pilot,
    network_receipt_verification as verifier,
    project_policy,
    roadmap_activation,
    roadmap_requests,
)


def is_pilot_task(task_id):
    return task_id == pilot.TASK_ID


def _candidate(order):
    return order["id"], order["receipt_id"], order["head_sha"], order["commit_sha"]


async def _fixed_task(db, task):
    if (
        int(task["id"]) != pilot.TASK_ID
        or task["branch"] != pilot.BRANCH
        or task["assigned_agent"] != pilot.WORKER
        or task["work_type"] != "chore"
    ):
        return False
    project = await repo.resolve_bound_project(db, pilot.TASK_ID)
    return bool(
        project
        and project["id"] == pilot.MANDATE.project_id
        and project["slug"] == "home-infra"
        and project["repo"] == pilot.REPO
        and project["status"] == "active"
    )


async def refuse_ordinary_submission(db, task_id):
    """A dedicated order cannot enter ordinary pair submission without its proof."""
    if task_id == pilot.TASK_ID and await pilot._order(db):
        raise HTTPException(409, "submit this network receipt through the steward tick")


async def verified_human_submission(db, task):
    """Only the atomically stamped pilot generation has a human-only review."""
    if not await _fixed_task(db, task):
        return False
    order = await pilot._order(db)
    return bool(
        order
        and order["submitted_generation"]
        and order["submitted_generation"] == task["submission_generation"]
        and order["commit_sha"] == task["submission_sha"]
    )


async def _require_owner_route(db, task):
    from hub.services.orchestration import machine_review_required

    project = await repo.get_project(db, pilot.MANDATE.project_id)
    if (
        project is None
        or project_policy.gate_value_of(
            project_policy.gate_policy_of(project), "verdict"
        )
        != "human"
        or machine_review_required(dict(task), project["machine_review"])
    ):
        raise HTTPException(409, "network receipt requires the human review route")


async def _require_running_worker(db, grant, task):
    from hub.services import admin

    worker = task["implementer_principal_id"] if task is not None else None
    if (
        task is None
        or not grant
        or grant["runner_id"] != worker
        or grant["profile_sha"] != pilot.PROFILE_SHA256
        or not await pilot.live_runner_grant(db, grant)
        or task["status"] != "running"
        or not await _fixed_task(db, task)
        or not worker
        or await executor_orders.host_of_principal(db, worker) != pilot.HOST
    ):
        raise HTTPException(409, "network receipt execution no longer current")
    principal = await admin.get_principal(db, worker)
    if not principal or principal["status"] != "active":
        raise HTTPException(409, "network receipt worker no longer active")
    await _require_owner_route(db, task)


async def _pending(db, steward, order_id):
    async with roadmap_requests._read_snapshot(db):
        grant = await pilot._authorized(db, steward, steward=True)
        order = await pilot._order(db)
        if not order or order["id"] != order_id:
            raise HTTPException(409, "network receipt order no longer current")
        if not order["commit_sha"]:
            return None
        task = await repo.get_task(db, pilot.TASK_ID)
        if order["submitted_generation"]:
            if not task or task["status"] != "running" or task["pr_number"]:
                return None  # Normal pair delivery handles an attached PR.
            if not await receipt_completion_allowed(db, dict(task)):
                return None
            return _candidate(order), True
        await _require_running_worker(db, grant, task)
        return _candidate(order), False


def _done_text(order_id, commit_sha):
    return f"Исполнение сетевой квитанции k8 №{order_id} подтверждено для {commit_sha}."


async def _prior_done(db, order):
    return await pilot._one(
        db,
        "SELECT 1 FROM task_updates WHERE task_id=? AND kind='done' AND content=?",
        (pilot.TASK_ID, _done_text(order["id"], order["commit_sha"])),
    )


async def receipt_completion_allowed(db, task):
    """Protect normal completion paths with the current stamped receipt and verdict."""
    if task["id"] != pilot.TASK_ID:
        return True
    order = await pilot._order(db)
    if not order:
        return True
    if not await verified_human_submission(db, task):
        return False
    grant = await pilot._one(
        db,
        "SELECT m.* FROM network_receipt_mandates m JOIN events e ON e.id=m.approval_id "
        "WHERE m.approval_id=? AND datetime(e.created_at,'+7 days') > datetime('now')",
        (pilot.MANDATE.approval_id,),
    )
    if not grant or grant["revoked_at"] or not await pilot.live_runner_grant(db, grant):
        return False
    try:
        await roadmap_activation._verify_approval(
            db, pilot.MANDATE, include_submitted=True
        )
        await _require_owner_route(db, task)
    except (roadmap_activation.ActivationRefused, HTTPException):
        return False
    proof = await verifier.recheck_network_receipt(
        db, commit_sha=order["commit_sha"], commit_verified=True
    )
    return proof.accepted


async def require_receipt_done(db, task, body):
    if not await receipt_completion_allowed(db, task):
        raise HTTPException(
            409, "network receipt must be verified and approved before done"
        )
    if task["id"] == pilot.TASK_ID:
        order = await pilot._order(db)
        if order and body.content == _done_text(order["id"], order["commit_sha"]):
            if await _prior_done(db, order):
                raise HTTPException(409, "network receipt done already recorded")


async def _verify_candidate(db, candidate):
    order_id, receipt_id, head_sha, commit_sha = candidate
    return await verifier.verify_network_receipt(
        db,
        order_id=order_id,
        receipt_id=receipt_id,
        head_sha=head_sha,
        commit_sha=commit_sha,
    )


async def submit_verified_publication(db, steward, order_id):
    """Advance a verified publication through submission, then normal completion."""
    pending = await _pending(db, steward, order_id)
    if pending is None:
        return
    candidate, submitted = pending
    proof = await _verify_candidate(db, candidate)
    if submitted:
        if proof.accepted:
            await _finish_without_pr(db, steward, candidate)
    elif proof.commit_verified and proof.ci_verified:
        await _submit(db, steward, candidate)


async def _finish_without_pr(db, steward, candidate):
    from hub.services.lifecycle import add_update

    order_id, _, _, commit_sha = candidate
    order = await pilot._order(db)
    if await _prior_done(db, order):
        return
    await add_update(
        db,
        pilot.TASK_ID,
        TaskUpdateCreate(
            agent=pilot.WORKER, kind="done", content=_done_text(order_id, commit_sha)
        ),
        principal_id=steward.principal_id,
    )


async def _submit(db, steward, candidate):
    from hub.services.lifecycle import submit_for_review

    order_id, _, _, commit_sha = candidate

    async def guard(live):
        grant = await pilot._authorized(db, steward, steward=True)
        order = await pilot._order(db)
        if not order or _candidate(order) != candidate or order["submitted_generation"]:
            raise HTTPException(409, "published receipt changed before submission")
        await _require_running_worker(db, grant, live)
        current = await verifier.recheck_network_receipt(
            db, commit_sha=commit_sha, commit_verified=True
        )
        if not (current.commit_verified and current.ci_verified):
            raise HTTPException(409, "network receipt CI no longer verified")
        await db.execute(
            "UPDATE network_receipt_orders SET submitted_generation=? WHERE id=?",
            (live["submission_generation"] + 1, order_id),
        )

    await submit_for_review(
        db,
        pilot.TASK_ID,
        TaskSubmitReview(
            agent=pilot.WORKER,
            branch=pilot.BRANCH,
            expected_sha=commit_sha,
            summary=(
                f"Квитанция k8 №{order_id}. Независимо подтверждены файл "
                "квитанции на GitHub и CI закреплённого коммита."
            ),
        ),
        before_submit_transition=guard,
    )
