"""Durable, version-bound waits for verified subscription quotas."""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256

import aiosqlite

from hub import repository as repo
from hub.db import fetchall
from hub.epic_plan_view import PlanDecision
from hub.services import epic_plan_spend as spend
from hub.services.epic_plan_authority import SpendingGrant, WorkBinding

WAITING = "pilot_wait_for_quota_reset"


def _key(binding: WorkBinding) -> str:
    return sha256(binding.model_dump_json().encode()).hexdigest()


async def waiting(
    db: aiosqlite.Connection, binding: WorkBinding, *, before_reset: bool = False
) -> bool:
    rows = await fetchall(
        db,
        "SELECT reset_at FROM epic_plan_quota_waits WHERE binding_key=?",
        (_key(binding),),
    )
    if not rows:
        return False
    if not before_reset:
        return True
    reset = rows[0]["reset_at"]
    return bool(reset and datetime.fromisoformat(reset) > datetime.now(UTC))


async def _note(
    db: aiosqlite.Connection,
    binding: WorkBinding,
    *,
    resumed: bool,
    reset_at: str | None = None,
) -> None:
    if resumed:
        message = f"Квота {binding.phase} подтверждена. Ожидание снято."
        kind = "epic_plan_quota_resumed"
    else:
        when = f" Следующая проверка после {reset_at}." if reset_at else ""
        message = f"Квота {binding.phase} исчерпана. Hub ждёт её обновления.{when}"
        kind = "epic_plan_quota_waiting"
    await repo.insert_event(
        db,
        kind=kind,
        task_id=binding.task_id,
        actor="hub",
        payload={**binding.model_dump(), "reset_at": reset_at},
    )
    await repo.add_task_update(db, binding.task_id, "hub", "status", message)


async def refusal(
    db: aiosqlite.Connection, decision: PlanDecision, grant: SpendingGrant
) -> str:
    """Check and record a provider wait inside the caller's write transaction."""
    binding = grant.binding
    if not grant.covers(binding) or binding.plan_version != decision.version:
        return "spending_grant_stale"
    reason = spend.subscription_refusal(decision, grant)
    if reason != WAITING:
        return reason or (
            WAITING if await waiting(db, binding, before_reset=True) else ""
        )
    reset = grant.reset_at.astimezone(UTC).isoformat() if grant.reset_at else None
    key = _key(binding)
    inserted = await db.execute(
        """INSERT OR IGNORE INTO epic_plan_quota_waits (binding_key,task_id,reset_at)
           VALUES (?,?,?)""",
        (key, binding.task_id, reset),
    )
    if inserted.rowcount == 1:
        await _note(db, binding, resumed=False, reset_at=reset)
    else:
        await db.execute(
            "UPDATE epic_plan_quota_waits SET reset_at=? WHERE binding_key=?",
            (reset, key),
        )
    return WAITING


async def resume(db: aiosqlite.Connection, binding: WorkBinding) -> None:
    """Remove a wait when the same operation acquires its durable reservation."""
    removed = await db.execute(
        "DELETE FROM epic_plan_quota_waits WHERE binding_key=?", (_key(binding),)
    )
    if removed.rowcount == 1:
        await _note(db, binding, resumed=True)
