"""Durability and exact CI for the scoped accepted-delivery recovery (#2393)."""

from collections.abc import Awaitable, Callable
from enum import Enum
from typing import Any

import aiosqlite

from hub.db import committed_if_own, write_transaction
from hub.integrations.protocols import CIPrTarget, CIProbeOutcome, CIProbeResult
from hub.services import delivery_ci


class Reservation(Enum):
    reserved = "reserved"
    approval_unproved = "approval_unproved"
    already_reserved = "already_reserved"


def _key(task: dict[str, Any]) -> tuple[int, int, int]:
    return (
        int(task["id"]),
        int(task.get("submission_generation") or 0),
        int(task.get("pr_number") or 0),
    )


async def reserve(
    db: aiosqlite.Connection,
    task: dict[str, Any],
    ctx: dict[str, Any],
    target: CIPrTarget,
    *,
    approval_stands: Callable[[], Awaitable[bool]],
) -> Reservation:
    """Commit the lease before the network. A lost reply leaves it reserved."""
    if db.in_transaction:
        return Reservation.approval_unproved
    async with write_transaction(db):
        if not await approval_stands():
            return Reservation.approval_unproved
        inserted = await db.execute(
            "INSERT INTO stale_base_update_intents "
            "(task_id, generation, pr_number, from_sha, repo_name, head_ref, base_ref) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(task_id, generation, pr_number, from_sha) DO NOTHING",
            (
                *_key(task),
                target.head_sha,
                ctx["gh_repo"],
                target.head_ref,
                target.base_ref,
            ),
        )
        return (
            Reservation.reserved if inserted.rowcount else Reservation.already_reserved
        )


async def release(
    db: aiosqlite.Connection, task: dict[str, Any], target: CIPrTarget
) -> None:
    """Release only when GitHub confirmed it did not accept the update."""
    async with committed_if_own(db):
        await db.execute(
            "DELETE FROM stale_base_update_intents WHERE task_id=? AND generation=? "
            "AND pr_number=? AND from_sha=? AND confirmed=0",
            (*_key(task), target.head_sha),
        )


async def confirm(
    db: aiosqlite.Connection, task: dict[str, Any], target: CIPrTarget
) -> None:
    """Confirm in the caller's transaction with the ordinary branch-update audit."""
    await db.execute(
        "UPDATE stale_base_update_intents SET confirmed=1 "
        "WHERE task_id=? AND generation=? AND pr_number=? AND from_sha=?",
        (*_key(task), target.head_sha),
    )


def pending_note(pr: int, target: CIPrTarget) -> str:
    return (
        f"base_update_pending: для PR #{pr} с головы {target.head_sha[:12]} "
        "зарезервировано обновление базы. Подтверждённого результата пока нет. "
        "Хаб не повторит запрос с этой головы. Проверьте состояние PR в GitHub."
    )


async def probe_ci(
    db: aiosqlite.Connection,
    task: dict[str, Any],
    ctx: dict[str, Any],
    *,
    checked_head: str | None = None,
) -> CIProbeResult | None:
    """Wait on a reserved head and require exact evidence for any later head."""
    if task.get("status") != "completed":
        return None
    async with db.execute(
        "SELECT from_sha, repo_name, head_ref, base_ref, confirmed "
        "FROM stale_base_update_intents WHERE task_id=? AND generation=? AND pr_number=? "
        "ORDER BY id DESC LIMIT 1",
        _key(task),
    ) as cursor:
        intent = await cursor.fetchone()
    if intent is None:
        return None
    if (intent["repo_name"], intent["head_ref"], intent["base_ref"]) != (
        ctx.get("gh_repo"),
        task.get("branch"),
        ctx.get("base_branch"),
    ):
        return CIProbeResult(
            CIProbeOutcome.unavailable, "delivery_pr_refs_changed_or_unreadable"
        )
    task_id, pr = task["id"], task["pr_number"]
    if checked_head is None:
        from hub.services.lifecycle import resolve_branch_tip

        checked_head, _ = await resolve_branch_tip(db, task_id, task["branch"])
    if not checked_head or intent["from_sha"] == checked_head:
        reason = (
            "waiting_for_updated_head"
            if intent["confirmed"]
            else "waiting_for_branch_update_confirmation"
        )
        return CIProbeResult(CIProbeOutcome.pending, reason)
    target = CIPrTarget(checked_head, intent["head_ref"], intent["base_ref"])
    return await delivery_ci.check_task(db, task_id, pr, ctx, target=target)
