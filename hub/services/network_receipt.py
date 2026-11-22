"""Single owner-approved, deterministic k8 order. Never enters the model queue."""

from __future__ import annotations

import secrets
from typing import Literal

import aiosqlite
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from hub import repository as repo
from hub.config import TokenIdentity
from hub.db import write_transaction
from hub.models import PairGitMode, TaskPairStart
from hub.services import admin, executor_orders, roadmap_activation, roadmap_requests

TASK_ID = 2413
HOST = "k8"
BRANCH = "task-2413/k8-network-receipt"
PROFILE_SHA256 = "7c07f713864c3db41b3fd1c6f0354bb1f0e4017257c048cca46b4142c13740d9"  # pragma: allowlist secret
MANDATE = roadmap_activation._Mandate(
    task_id=TASK_ID,
    project_id=14,
    approval_id=26347,
    approver_id=1,
    generation=2,
    fingerprint="8695394922fdcec02f255a9b10e07fc447500633ef3a4857f22c9441deb7f104",  # pragma: allowlist secret
    steward_id=42,
    roadmap_id=31,
)
WORKER = "k8-network-receipt"
REPO = "agentdrover/home-infra"
REF = "refs/heads/main"


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Ready(StrictBody):
    profile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class Receipt(StrictBody):
    task_id: int
    order_id: int = Field(gt=0)
    receipt_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    repository: Literal["agentdrover/home-infra"]
    ref: Literal["refs/heads/main"]


class Publication(StrictBody):
    commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")


async def _one(db, sql, args):
    return await roadmap_requests._one(db, sql, args)


async def _key_is_live(db, key_id, principal_id, role):
    key = await _one(
        db,
        "SELECT k.scopes FROM api_keys k JOIN principals p ON p.id = k.principal_id "
        "WHERE k.id=? AND k.principal_id=? AND k.revoked_at IS NULL "
        "AND (k.expires_at IS NULL OR julianday(k.expires_at) > julianday('now')) "
        "AND p.status='active' AND p.kind IN ('service','agent')",
        (key_id, principal_id),
    )
    if key is None or await admin.get_effective_role(db, principal_id) != role:
        return False
    scopes, damaged = admin.parse_key_scopes(key["scopes"])
    return not damaged and (not scopes or "home-infra" in scopes)


async def _require_live_key(db, who, role):
    """Recheck the credential, including changes since request authentication."""
    if (
        who.auth_source != "db_api_key"
        or who.role != role
        or who.scopes_damaged
        or (who.scopes and "home-infra" not in who.scopes)
        or not await _key_is_live(db, who.api_key_id, who.principal_id, role)
    ):
        raise HTTPException(403, "dedicated project credential no longer active")


async def live_runner_grant(db, grant):
    """After issuance the original host credential must remain usable."""
    return bool(
        grant
        and await _key_is_live(
            db, grant["ready_key_id"], grant["runner_id"], "slot_runner"
        )
    )


async def _authorized(db, who, *, steward=False, allow_stamped=False):
    await _require_live_key(db, who, "roadmap_steward" if steward else "slot_runner")
    if steward:
        if who.principal_id != MANDATE.steward_id:
            raise HTTPException(403, "wrong steward for approved order")
    elif await executor_orders.host_of_principal(db, who.principal_id) != HOST:
        raise HTTPException(403, "registered k8 host required")
    row = await _one(
        db,
        "SELECT * FROM network_receipt_mandates WHERE approval_id=?",
        (MANDATE.approval_id,),
    )
    if row is not None and row["revoked_at"] is not None:
        raise HTTPException(409, "network receipt mandate revoked")
    project = await repo.get_project(db, MANDATE.project_id)
    if not project or project["slug"] != "home-infra" or project["repo"] != REPO:
        raise HTTPException(409, "approved receipt repository changed")
    fresh = await _one(
        db,
        "SELECT 1 FROM events WHERE id=? AND datetime(created_at,'+7 days') > datetime('now')",
        (MANDATE.approval_id,),
    )
    if fresh is None:
        raise HTTPException(409, "network receipt mandate expired")
    submitted = (steward or allow_stamped) and await _one(
        db,
        "SELECT 1 FROM network_receipt_orders o JOIN tasks t ON t.id=o.task_id "
        "WHERE o.approval_id=? AND o.task_id=? AND o.submitted_generation=t.submission_generation "
        "AND o.commit_sha=t.submission_sha AND t.branch=? AND t.assigned_agent=?",
        (MANDATE.approval_id, TASK_ID, BRANCH, WORKER),
    )
    try:
        await roadmap_activation._verify_approval(
            db, MANDATE, include_submitted=bool(submitted)
        )
    except roadmap_activation.ActivationRefused as exc:
        raise HTTPException(409, "approved network receipt statement changed") from exc
    return row


async def ready(db, who, body):
    async with write_transaction(db):
        grant = await _authorized(db, who)
        if grant and grant["ready_key_id"] != who.api_key_id and await _order(db):
            raise HTTPException(409, "issued receipt bound to earlier ready credential")
        if body.profile_sha256 != PROFILE_SHA256:
            raise HTTPException(409, "worker profile mismatch")
        await db.execute(
            "INSERT INTO network_receipt_mandates(approval_id,runner_id,ready_key_id,profile_sha,ready_at) "
            "VALUES(?,?,?,?,datetime('now')) ON CONFLICT(approval_id) DO UPDATE SET "
            "runner_id=excluded.runner_id, ready_key_id=excluded.ready_key_id, "
            "profile_sha=excluded.profile_sha, ready_at=excluded.ready_at",
            (MANDATE.approval_id, who.principal_id, who.api_key_id, PROFILE_SHA256),
        )
    return {"ready": True}


async def _order(db):
    return await _one(
        db,
        "SELECT * FROM network_receipt_orders WHERE approval_id=? AND task_id=?",
        (MANDATE.approval_id, TASK_ID),
    )


def identity(row):
    return {
        "task_id": row["task_id"],
        "order_id": row["id"],
        "receipt_id": row["receipt_id"],
    }


async def dedicated_worker_required(db, task_id):
    if task_id != TASK_ID:
        return False
    project = await repo.resolve_bound_project(db, task_id)
    return bool(
        project
        and project["id"] == MANDATE.project_id
        and project["slug"] == "home-infra"
        and project["repo"] == REPO
    )


async def _eligible(db):
    grant = await _one(
        db,
        "SELECT m.runner_id, m.ready_key_id, h.principal_id FROM network_receipt_mandates m JOIN executor_hosts h ON h.principal_id=m.runner_id "
        "JOIN principals p ON p.id=h.principal_id "
        "WHERE m.approval_id=? AND m.revoked_at IS NULL AND m.profile_sha=? AND h.name=? AND h.enabled=1 "
        "AND p.status='active' AND m.ready_at>=datetime('now','-3 minutes')",
        (MANDATE.approval_id, PROFILE_SHA256, HOST),
    )

    return grant if await live_runner_grant(db, grant) else None


async def issue(db: aiosqlite.Connection, who: TokenIdentity):
    from hub.services.lifecycle import pair_start_task

    async with roadmap_requests._read_snapshot(db):
        await _authorized(db, who, steward=True)
        old = await _order(db)
        if old:
            return identity(old)
        runner = await _eligible(db)
        if not runner:
            return None

    # Pair-start supplies all normal workflow guards. Its final write transaction
    # owns this INSERT and the task transition, including their rollback.
    async def issue_under_lock():
        await _authorized(db, who, steward=True)
        current = await _eligible(db)
        task = await repo.get_task(db, TASK_ID)
        if (
            not current
            or current["principal_id"] != runner["principal_id"]
            or task["status"] != "open"
        ):
            raise HTTPException(409, "pilot conditions changed before issuance")
        await db.execute(
            "INSERT INTO network_receipt_orders(approval_id,task_id,receipt_id,steward_key_id) "
            "VALUES(?,?,?,?)",
            (MANDATE.approval_id, TASK_ID, secrets.token_hex(16), who.api_key_id),
        )

    try:
        await pair_start_task(
            db,
            TASK_ID,
            TaskPairStart(
                assigned_agent=WORKER,
                branch_slug="k8-network-receipt",
                git_mode=PairGitMode.remote,
                session_id="k8-network-receipt-2413",
                plan="Служебный заказ k8. Проверить версию home-infra через DE и доставить квитанцию.",
            ),
            caller=WORKER,
            implementer_principal_id=runner["principal_id"],
            caller_is_agent=True,
            before_pair_transition=issue_under_lock,
        )
    except HTTPException as exc:
        if exc.status_code not in (400, 409):
            raise
        async with roadmap_requests._read_snapshot(db):
            await _authorized(db, who, steward=True)
            old = await _order(db)
            if old is None:
                raise
            return identity(old)
    return identity(await _order(db))


def _current_worker_stage(task, order):
    if order["submitted_generation"] is None:
        return task["status"] == "running"
    return (
        task["status"] in {"running", "review", "completed"}
        and order["submitted_generation"] == task["submission_generation"]
        and order["commit_sha"] == task["submission_sha"]
        and order["head_sha"] is not None
        and order["commit_sha"] is not None
    )


async def _worker_order(db, who, order_id=None):
    grant = await _authorized(db, who, allow_stamped=True)
    row = await _order(db)
    if not row and order_id is None:
        return None
    if not row or (order_id is not None and order_id != row["id"]):
        raise HTTPException(404, "unknown network receipt order")
    task = await repo.get_task(db, TASK_ID)
    if (
        not grant
        or grant["runner_id"] != who.principal_id
        or grant["ready_key_id"] != who.api_key_id
        or task["implementer_principal_id"] != who.principal_id
        or not _current_worker_stage(task, row)
        or task["branch"] != BRANCH
        or task["assigned_agent"] != WORKER
    ):
        raise HTTPException(409, "network receipt execution no longer current")
    return row


async def current(db, who, *, publication=False):
    async with roadmap_requests._read_snapshot(db):
        row = await _worker_order(db, who)
        if not row or row["submitted_generation"] is not None:
            return None
        if not publication:
            return identity(row)
        if not row["head_sha"] or row["commit_sha"]:
            return None
        return {
            **identity(row),
            "head_sha": row["head_sha"],
            "repository": REPO,
            "ref": REF,
        }


async def measure(db, who, order_id, body):
    async with write_transaction(db):
        row = await _worker_order(db, who, order_id)
        if identity(row) != body.model_dump(
            include={"task_id", "order_id", "receipt_id"}
        ) or row["head_sha"] not in (None, body.head_sha):
            raise HTTPException(409, "receipt contradicts order or recorded result")
        if row["head_sha"] is None:
            await db.execute(
                "UPDATE network_receipt_orders SET head_sha=?,measured_at=datetime('now') WHERE id=?",
                (body.head_sha, order_id),
            )
            await repo.add_task_update(
                db,
                TASK_ID,
                WORKER,
                "status",
                f"Получена квитанция заказа {order_id}, исходный SHA {body.head_sha}.",
                principal_id=who.principal_id,
                author_kind="principal",
            )
        return {**identity(row), "repeat": row["head_sha"] is not None}


async def publish(db, who, order_id, body):
    async with write_transaction(db):
        row = await _worker_order(db, who, order_id)
        if not row["head_sha"] or row["commit_sha"] not in (None, body.commit_sha):
            raise HTTPException(409, "publication contradicts order or recorded result")
        if not row["commit_sha"]:
            await db.execute(
                "UPDATE network_receipt_orders SET commit_sha=?,published_at=datetime('now') WHERE id=?",
                (body.commit_sha, order_id),
            )
            await repo.add_task_update(
                db,
                TASK_ID,
                WORKER,
                "status",
                f"Публикация заказа {order_id} заявлена на {body.commit_sha}; ожидается независимая проверка.",
                principal_id=who.principal_id,
                author_kind="principal",
            )
        return {"order_id": order_id, "commit_sha": body.commit_sha}


async def revoke(db):
    async with write_transaction(db):
        await db.execute(
            "INSERT INTO network_receipt_mandates(approval_id,revoked_at) VALUES(?,datetime('now')) "
            "ON CONFLICT(approval_id) DO UPDATE SET revoked_at=COALESCE(revoked_at,excluded.revoked_at)",
            (MANDATE.approval_id,),
        )


def _submission_stage(task, proof):
    if proof.accepted:
        return "accepted", ""
    if task["archived"] or task["status"] in ("rejected", "failed"):
        return "stopped", "receipt_task_inactive"
    if (
        task["review_verdict_closed_generation"] == task["submission_generation"]
        or task["behavior_approval_void"]
    ):
        return "verdict_withdrawn", "receipt_verdict_withdrawn"
    if (
        task["review_verdict_generation"] == task["submission_generation"]
        and task["review_verdict"] == "changes_requested"
    ):
        return "changes_requested", "receipt_changes_requested"
    if not proof.ci_verified:
        return "awaiting_ci", proof.reason
    return "awaiting_verdict", proof.reason


async def _card_stage(db, order):
    from hub.services.network_receipt_submission import (
        receipt_completion_allowed,
        verified_human_submission,
    )
    from hub.services.network_receipt_verification import recheck_network_receipt

    stage = "issued"
    if order["measured_at"]:
        stage = "measured"
    if order["published_at"]:
        stage = "published"
    result = dict(
        stage=stage,
        ci_verified=False,
        verdict_verified=False,
        accepted=False,
        reason="",
    )
    if order["submitted_generation"] is not None:
        task = await repo.get_task(db, TASK_ID)
        if task is None or not await verified_human_submission(db, task):
            result.update(
                stage="submission_changed",
                reason="receipt_submission_no_longer_current",
            )
        else:
            proof = await recheck_network_receipt(
                db, commit_sha=order["commit_sha"], commit_verified=True
            )
            result.update(
                ci_verified=proof.ci_verified,
                verdict_verified=proof.verdict_verified,
                accepted=proof.accepted,
                reason=proof.reason,
            )
            result["stage"], result["reason"] = _submission_stage(task, proof)
            if proof.accepted and not await receipt_completion_allowed(db, task):
                result.update(
                    stage="held",
                    accepted=False,
                    reason="receipt_completion_authority_changed",
                )
    grant = await _one(
        db,
        "SELECT revoked_at FROM network_receipt_mandates WHERE approval_id=?",
        (order["approval_id"],),
    )
    if grant is None or grant["revoked_at"] is not None:
        result.update(stage="revoked", accepted=False, reason="receipt_mandate_revoked")
    return result


async def card_view(db, task_id):
    if task_id != TASK_ID:
        return None
    async with roadmap_requests._read_snapshot(db):
        row = await _order(db)
        if row is None:
            return None
        return {
            "order_id": row["id"],
            "host": HOST,
            "created_at": row["created_at"],
            "measured_at": row["measured_at"],
            "head_sha": row["head_sha"],
            "published_at": row["published_at"],
            "commit_sha": row["commit_sha"],
            "submitted_generation": row["submitted_generation"],
            **await _card_stage(db, row),
        }
