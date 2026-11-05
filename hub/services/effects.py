"""Non-idempotent dispatch outbox: ambiguity always needs reconciliation."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import Any
from uuid import uuid4

from fastapi import HTTPException

from hub import config
from hub import repository as tasks
from hub import repository_idempotency as repo
from hub.db import write_transaction
from hub.idempotency import utc_timestamp

log = logging.getLogger(__name__)


class EffectPayloadConflict(Exception):
    """A natural effect key was reused with a different payload."""


def dispatch_enabled() -> bool:
    """Both switches must be on before any external dispatch (review, major 5).

    ``IDEMPOTENCY_ENABLED`` off means the feature is rolled back: effects left
    in the outbox must not fire. ``IDEMPOTENCY_DISPATCH_ENABLED`` is a separate
    sender freeze for incidents, so the create API can keep accepting work
    while no external agent is started. A disabled sender leaves rows pending
    for the poller instead of consuming or finalizing them.
    """
    return config.IDEMPOTENCY_ENABLED and config.IDEMPOTENCY_DISPATCH_ENABLED


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


async def _dispatch(db: Any, row: Any) -> tuple[str, str | None, str | None]:
    """Run one dispatch effect; return (state, resolution, last_error).

    The intent, the task state and an existing job are re-checked immediately
    before the external call (review, major 5). A stale intention is finalized
    WITHOUT dispatching: firing a completed/archived task, or a task that
    already has a job, is exactly what the old existence-only check allowed.

    Outcomes are typed (review, major 6). ``delivery=not_sent`` is evidence the
    process never started, so the effect is a terminal failure that a person
    may safely retry manually. Anything an error result cannot prove — the
    dispatch binary ran and exited non-zero, or returned no job id — is
    ``unknown``, never ``failed``.
    """
    from hub.services import lifecycle

    payload = json.loads(row["payload_json"])
    task_id = int(payload["task_id"])
    task = await tasks.get_task(db, task_id)
    if task is None:
        return "dead", None, "stale_intent:task_missing"
    if row["operation_id"] and row["principal_ref"]:
        key = await repo.get_key(db, row["principal_ref"], row["operation_id"])
        if key is None:
            return "dead", None, "stale_intent:operation_missing"
        if key["status"] != "completed":
            return "dead", None, "stale_intent:operation_expired"
    if task["job_id"]:
        return "done", f"job_id={task['job_id']}", None
    if task["archived"]:
        return "dead", None, "stale_intent:task_archived"
    if task["status"] != "running":
        return "dead", None, f"stale_intent:task_{task['status']}"
    result = await lifecycle.dispatch_task(db, task_id, dict(task))
    if result.get("job_id"):
        return "done", f"job_id={result['job_id']}", None
    delivery = str(result.get("delivery") or "")
    if delivery == "not_sent":
        return "failed", None, "dispatch_not_sent"
    if delivery == "permanent":
        return "failed", None, "dispatch_refused"
    # ``ambiguous`` and any unlabelled error: the external side may have a job.
    return "unknown", None, "dispatch_outcome_unknown"


async def run_dispatch_effect(db: Any, effect_id: str) -> None:
    if db.in_transaction:
        raise RuntimeError("dispatch must run outside the operation transaction")
    if not dispatch_enabled():
        return
    owner = uuid4().hex
    async with write_transaction(db):
        if not await repo.mark_running(db, effect_id, owner, utc_timestamp(60)):
            return
        row = await repo.get_effect(db, effect_id)
    # The adapter is outside BOTH the key transaction and the lease transaction.
    try:
        state, resolution, last_error = await _dispatch(db, row)
    except HTTPException:
        # A refusal raised by the local dispatch path happens before submit:
        # there is evidence nothing was sent, so this is a permanent failure,
        # not an ambiguous outcome (review, major 6).
        if db.in_transaction:
            await db.rollback()
        async with write_transaction(db):
            await repo.finalize_effect(
                db, effect_id, owner, "failed", last_error="dispatch_refused"
            )
        return
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
        await repo.finalize_effect(db, effect_id, owner, state, resolution, last_error)


async def process_ready_effects(db: Any) -> None:
    if not dispatch_enabled():
        return
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
