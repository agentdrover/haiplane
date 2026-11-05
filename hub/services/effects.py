"""Non-idempotent dispatch outbox: ambiguity always needs reconciliation."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import Any
from uuid import uuid4

from hub import repository as tasks
from hub import repository_idempotency as repo
from hub.db import write_transaction
from hub.idempotency import utc_timestamp

log = logging.getLogger(__name__)


class EffectPayloadConflict(Exception):
    """A natural effect key was reused with a different payload."""


async def enqueue_effect(
    db: Any,
    *,
    operation_id: str,
    principal_ref: str,
    effect_kind: str,
    effect_key: str,
    payload: Any,
    ordering_key: str = "",
) -> str:
    if not db.in_transaction:
        raise RuntimeError("enqueue_effect requires the operation transaction")
    if effect_kind != "task.dispatch":
        raise ValueError("effect adapter not registered")
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    row, matches = await repo.insert_effect(
        db,
        effect_id=uuid4().hex,
        operation_id=operation_id,
        principal_ref=principal_ref,
        effect_kind=effect_kind,
        effect_key=effect_key,
        ordering_key=ordering_key,
        payload_json=encoded,
        payload_hash=hashlib.sha256(encoded.encode()).hexdigest(),
    )
    if not matches:
        log.error(
            "effect payload conflict: effect_id=%s operation_id=%s",
            row["effect_id"],
            operation_id,
        )
        raise EffectPayloadConflict("effect_payload_mismatch")
    return row["effect_id"]


async def _dispatch(db: Any, row: Any) -> tuple[str, str | None]:
    from hub.services import lifecycle

    payload = json.loads(row["payload_json"])
    task_id = int(payload["task_id"])
    task = await tasks.get_task(db, task_id)
    if task is None:
        raise ValueError("dispatch_task_not_found")
    result = await lifecycle.dispatch_task(db, task_id, dict(task))
    if result.get("error"):
        return "failed", None
    if result.get("job_id"):
        return "done", f"job_id={result['job_id']}"
    return "unknown", None


async def run_dispatch_effect(db: Any, effect_id: str) -> None:
    if db.in_transaction:
        raise RuntimeError("dispatch must run outside the operation transaction")
    owner = uuid4().hex
    async with write_transaction(db):
        if not await repo.mark_running(db, effect_id, owner, utc_timestamp(60)):
            return
        row = await repo.get_effect(db, effect_id)
    # The adapter is outside BOTH the key transaction and the lease transaction.
    try:
        state, resolution = await _dispatch(db, row)
    except (Exception, asyncio.CancelledError) as exc:
        if db.in_transaction:
            await db.rollback()
        # Never store exception text: providers can include credentials/results.
        async with write_transaction(db):
            await repo.finalize_effect(
                db, effect_id, owner, "unknown", last_error="dispatch_outcome_unknown"
            )
        if isinstance(exc, asyncio.CancelledError):
            raise
        return
    async with write_transaction(db):
        await repo.finalize_effect(
            db,
            effect_id,
            owner,
            state,
            resolution,
            "dispatch_failed" if state == "failed" else None,
        )


async def process_ready_effects(db: Any) -> None:
    rows = await repo.pick_ready_effects(db)
    for row in rows:
        if row["attempts"] > 0:
            async with write_transaction(db):
                await repo.mark_ambiguous(db, row["effect_id"])
        else:
            await run_dispatch_effect(db, row["effect_id"])


async def resolve_effect(
    db: Any, effect_id: str, state: str, resolution: str, resolved_by: str
) -> None:
    if state not in {"done", "failed", "dead"} or not resolution or not resolved_by:
        raise ValueError("reconciliation requires terminal state, evidence and actor")
    async with write_transaction(db):
        if not await repo.resolve_effect(db, effect_id, state, resolution, resolved_by):
            raise ValueError("effect is not unresolved")
