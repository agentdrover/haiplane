"""One operator-attested recovery without modifying steward requests or old orders."""

from typing import NamedTuple

import aiosqlite
from fastapi import HTTPException

from hub.config import TokenIdentity
from hub.db import write_transaction
from hub.services import roadmap_requests as requests


class Attestation(NamedTuple):
    update_id: int
    evidence_sha256: str
    runtime_sha256: str


async def _require_operator(
    db: aiosqlite.Connection, who: TokenIdentity, task_id: int
) -> None:
    if who.principal_id is None or who.auth_source not in ("db_api_key", "db_session"):
        raise HTTPException(
            403, "roadmap recovery requires an auditable DB administrator"
        )
    await requests._require_task_grantor(db, who, task_id)


async def _failed_original(
    db: aiosqlite.Connection, task_id: int, order_id: int
) -> aiosqlite.Row:
    row = await requests._request(db, order_id)
    if row is None or row["task_id"] != task_id:
        raise HTTPException(404, "roadmap order not found")
    if (
        row["initial_order_id"] != order_id
        or row["source_order_id"] is None
        or row["state"] != "failed"
        or row["outcome"] != "failed"
        or row["started_at"] is None
        or row["closed_at"] is None
    ):
        requests._conflict(
            "roadmap_recovery_ineligible",
            "recovery requires a reported failure of an original started signal order",
        )
    if not await requests._request_is_current(db, row):
        requests._conflict(
            "roadmap_grant_stale", "roadmap grant or source is no longer current"
        )
    return row


async def _require_evidence(
    db: aiosqlite.Connection, original: aiosqlite.Row, attestation: Attestation
) -> None:
    if (
        await requests._one(
            db,
            "SELECT 1 FROM task_updates WHERE id = ? AND task_id = ? "
            "AND author_kind = 'principal' AND principal_id IS NOT NULL "
            "AND datetime(created_at) >= datetime(?)",
            (attestation.update_id, original["task_id"], original["closed_at"]),
        )
        is None
    ):
        requests._conflict(
            "roadmap_recovery_evidence_invalid",
            "record authenticated technical evidence on the target task after the failure",
        )


async def recover(
    db: aiosqlite.Connection,
    who: TokenIdentity,
    task_id: int,
    initial_order_id: int,
    attestation: Attestation,
) -> dict[str, int | bool]:
    async with write_transaction(db):
        await _require_operator(db, who, task_id)
        original = await _failed_original(db, task_id, initial_order_id)
        existing = await requests._one(
            db,
            "SELECT order_id, evidence_update_id, evidence_sha256, runtime_sha256 "
            "FROM roadmap_recoveries WHERE request_pk = ?",
            (original["request_pk"],),
        )
        if existing is not None:
            if tuple(existing)[1:] != attestation:
                requests._conflict(
                    "roadmap_recovery_conflict",
                    "a recovery already exists with different attestation",
                )
            return {"order_id": existing["order_id"], "repeat": True}
        await _require_evidence(db, original, attestation)
        order_id = await requests._queue_order(db, original)
        await db.execute(
            "INSERT INTO roadmap_recoveries (request_pk, order_id, operator_id, "
            "evidence_update_id, evidence_sha256, runtime_sha256) VALUES (?, ?, ?, ?, ?, ?)",
            (original["request_pk"], order_id, who.principal_id, *attestation),
        )
        await requests._audit(
            db,
            who,
            "roadmap_recovered",
            task_id,
            f"original={initial_order_id} order={order_id} grant={original['id']} "
            f"source_order={original['source_order_id']} evidence_update={attestation.update_id} "
            f"evidence_sha256={attestation.evidence_sha256} runtime_sha256={attestation.runtime_sha256}",
        )
        return {"order_id": order_id, "repeat": False}
