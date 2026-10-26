"""Who holds an outcome promise (#2090): an explicit link, judged on every read.

The old debt counted every completed task with a metric, so one result promised
by an epic, its features and their tasks was counted once per level. A parent's
metric does NOT prove it promises the same thing, so nothing is excluded for
that: a task leaves the actionable debt only through its own link, "my promise
is held by #N", and only while the link holds. The nearest ancestor with a
metric is a hint for triage (``suggested_owner_task_id``), never coverage.

Coverage, the answer (#819) and the due date (#1568/#1572) are separate
dimensions. Nothing here reads or writes ``outcome_answers``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import aiosqlite

from hub import repository
from hub.db import write_transaction
from hub.models import OutcomeOwnershipView

Row = Mapping[str, Any]


def _ancestors(task: Row, by_id: Mapping[int, Row]) -> list[Row]:
    """Parents from the nearest up; stops on a gap or a cycle."""
    chain: list[Row] = []
    seen: set[int] = {int(task["id"])}
    parent_id = task.get("parent_id")
    while parent_id is not None and int(parent_id) not in seen:
        parent = by_id.get(int(parent_id))
        if parent is None:
            break
        seen.add(int(parent_id))
        chain.append(parent)
        parent_id = parent.get("parent_id")
    return chain


def _invalid_reason(
    owner_id: int, chain: Sequence[Row], by_id: Mapping[int, Row]
) -> str:
    """Why the link does not hold, or "" when it does."""
    owner = by_id.get(owner_id)
    if owner is None:
        return "owner_missing"
    if all(int(a["id"]) != owner_id for a in chain):
        return "owner_not_ancestor"
    if owner.get("archived"):
        return "owner_archived"
    if owner.get("status") != "completed":
        return "owner_not_completed"
    if not str(owner.get("outcome_metric") or "").strip():
        return "owner_no_metric"
    if owner.get("outcome_owner_task_id") is not None:
        return "owner_delegates"
    return ""


def classify(task: Row, by_id: Mapping[int, Row]) -> dict[str, Any]:
    """The one place a task's ownership is decided; every surface renders this."""
    chain = _ancestors(task, by_id)
    suggested = next(
        (int(a["id"]) for a in chain if str(a.get("outcome_metric") or "").strip()),
        None,
    )
    owner_id = task.get("outcome_owner_task_id")
    reason = "" if owner_id is None else _invalid_reason(int(owner_id), chain, by_id)
    if owner_id is None:
        state = "independent"
    else:
        state = "invalid_owner" if reason else "covered"
    view = OutcomeOwnershipView(
        state=state,
        owner_task_id=int(owner_id) if owner_id is not None else None,
        reason=reason,
        suggested_owner_task_id=suggested,
    )
    return view.model_dump()


async def classify_ownerships(
    db: aiosqlite.Connection, tasks: Sequence[Row]
) -> dict[int, dict[str, Any]]:
    """Ownership of many tasks with ONE ancestor read, whatever their number."""
    context = await repository.list_ownership_context(
        db,
        [int(t["id"]) for t in tasks if t.get("parent_id") is not None],
        [
            int(t["outcome_owner_task_id"])
            for t in tasks
            if t.get("outcome_owner_task_id") is not None
        ],
    )
    by_id: dict[int, Row] = {int(r["id"]): dict(r) for r in context}
    return {int(t["id"]): classify(t, by_id) for t in tasks}


async def ownership_for_task(
    db: aiosqlite.Connection, task: Row
) -> OutcomeOwnershipView | None:
    """Ownership for the card and the brief; None when the task promises nothing."""
    if (
        not str(task.get("outcome_metric") or "").strip()
        and task.get("outcome_owner_task_id") is None
    ):
        return None
    view = (await classify_ownerships(db, [task]))[int(task["id"])]
    return OutcomeOwnershipView(**view)


async def set_outcome_owner(
    db: aiosqlite.Connection,
    *,
    task_id: int,
    owner_task_id: int | None,
    set_by: str = "",
) -> dict[str, Any]:
    """Delegate the task's promise to an ancestor, or take it back with None.

    Only the chain is checked on write. Whether the owner is finished and states
    a metric is judged at read time: a child may delegate to an epic that is
    still open, and the link then shows as invalid until the owner completes -
    the answer carries that verdict (``ownership``), so a caller is never told
    "held" about a link that does not hold. Check, write and feed entry are one
    transaction.
    """
    async with write_transaction(db):
        row = await repository.get_task(db, task_id)
        if row is None:
            raise LookupError(f"task {task_id} not found")
        task = dict(row)
        if owner_task_id is not None:
            await _require_ancestor(db, task, owner_task_id)
        previous = task.get("outcome_owner_task_id")
        await _write_link(db, task_id, previous, owner_task_id, set_by)
        view = await ownership_for_task(
            db, {**task, "outcome_owner_task_id": owner_task_id}
        )
    return {
        "task_id": task_id,
        "owner_task_id": owner_task_id,
        "previous": previous,
        "ownership": view.model_dump() if view else None,
    }


async def _write_link(
    db: aiosqlite.Connection,
    task_id: int,
    previous: int | None,
    owner_task_id: int | None,
    set_by: str,
) -> None:
    """Column and feed entry under one SAVEPOINT: a nested caller keeps neither half."""
    await db.execute("SAVEPOINT outcome_owner_write")
    try:
        await repository.set_outcome_owner_column(db, task_id, owner_task_id)
        if previous != owner_task_id:
            await repository.add_task_update(
                db,
                task_id,
                set_by,
                "status",
                (
                    f"Outcome promise delegated to #{owner_task_id}"
                    if owner_task_id is not None
                    else "Outcome promise taken back (independent)"
                ),
            )
    except BaseException:
        await db.execute("ROLLBACK TO outcome_owner_write")
        await db.execute("RELEASE outcome_owner_write")
        raise
    await db.execute("RELEASE outcome_owner_write")


async def _require_ancestor(
    db: aiosqlite.Connection, task: Row, owner_task_id: int
) -> None:
    task_id = int(task["id"])
    if owner_task_id == task_id:
        raise ValueError(f"task #{task_id} cannot hold its own promise by a link")
    if await repository.get_task(db, owner_task_id) is None:
        raise LookupError(f"task {owner_task_id} not found")
    context = await repository.list_ownership_context(db, [task_id], [])
    chain = _ancestors(task, {int(r["id"]): dict(r) for r in context})
    if all(int(a["id"]) != owner_task_id for a in chain):
        raise ValueError(
            f"task #{owner_task_id} is not an ancestor of task #{task_id}: "
            "only a parent, a grandparent and so on can hold its promise"
        )
