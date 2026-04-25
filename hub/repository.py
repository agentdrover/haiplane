"""OpenClaw Hub — Data access layer (repository).

All SQL queries live here. Functions take ``aiosqlite.Connection`` as the
first argument and return raw rows (``aiosqlite.Row``) or primitives.
No Pydantic models, no business logic.
"""

from __future__ import annotations

import json
from typing import Any

import aiosqlite

from hub.db import (
    STRUCTURED_TASK_FIELDS,
    ac_to_row_kwargs,
    structured_fields_to_db,
)

# ---------------------------------------------------------------------------
# Tasks — Read
# ---------------------------------------------------------------------------


async def get_task(
    db: aiosqlite.Connection,
    task_id: int,
) -> aiosqlite.Row | None:
    rows = await db.execute_fetchall(
        "SELECT * FROM tasks WHERE id=?",
        (task_id,),
    )
    return rows[0] if rows else None


async def list_tasks_filtered(
    db: aiosqlite.Connection,
    *,
    status: str | None = None,
    task_type: str | None = None,
    priority: str | None = None,
    source: str | None = None,
    parent_id: int | None = None,
    human_owner: str | None = None,
    human_reviewer: str | None = None,
    limit: int = 50,
) -> list[aiosqlite.Row]:
    conditions: list[str] = []
    params: list[Any] = []

    if status:
        conditions.append("status=?")
        params.append(status)
    if task_type:
        conditions.append("task_type=?")
        params.append(task_type)
    if priority:
        conditions.append("priority=?")
        params.append(priority)
    if source:
        conditions.append("source=?")
        params.append(source)
    if parent_id is not None:
        conditions.append("parent_id=?")
        params.append(parent_id)
    if human_owner:
        conditions.append("human_owner=?")
        params.append(human_owner)
    if human_reviewer:
        conditions.append("human_reviewer=?")
        params.append(human_reviewer)

    where = " AND ".join(conditions) if conditions else "1=1"
    params.append(limit)
    return await db.execute_fetchall(
        f"SELECT * FROM tasks WHERE {where} ORDER BY position ASC, id DESC LIMIT ?",
        tuple(params),
    )


async def list_tasks_by_statuses(
    db: aiosqlite.Connection,
    statuses: list[str],
    *,
    limit: int = 20,
) -> list[aiosqlite.Row]:
    placeholders = ",".join("?" for _ in statuses)
    return await db.execute_fetchall(
        f"SELECT * FROM tasks WHERE status IN ({placeholders}) ORDER BY id DESC LIMIT ?",
        (*statuses, limit),
    )


async def list_tasks_by_status(
    db: aiosqlite.Connection,
    status: str,
    *,
    order_by: str = "id DESC",
    limit: int = 20,
) -> list[aiosqlite.Row]:
    return await db.execute_fetchall(
        f"SELECT * FROM tasks WHERE status=? ORDER BY {order_by} LIMIT ?",
        (status, limit),
    )


async def list_running_dispatchable(
    db: aiosqlite.Connection,
) -> list[aiosqlite.Row]:
    return await db.execute_fetchall(
        "SELECT * FROM tasks WHERE status IN ('running', 'fix_requested') "
        "AND job_id IS NOT NULL",
    )


async def list_review_tasks(
    db: aiosqlite.Connection,
) -> list[aiosqlite.Row]:
    return await db.execute_fetchall(
        "SELECT * FROM tasks WHERE status='review' AND review_job_id IS NOT NULL",
    )


async def list_ci_check_tasks(
    db: aiosqlite.Connection,
) -> list[aiosqlite.Row]:
    return await db.execute_fetchall(
        "SELECT * FROM tasks WHERE status='ci_check'",
    )


async def list_stale_running(
    db: aiosqlite.Connection,
    threshold_minutes: int,
) -> list[aiosqlite.Row]:
    return await db.execute_fetchall(
        "SELECT * FROM tasks WHERE status='running' "
        "AND updated_at < datetime('now', ?)",
        (f"-{threshold_minutes} minutes",),
    )


async def list_active_epics(
    db: aiosqlite.Connection,
    *,
    limit: int = 20,
) -> list[aiosqlite.Row]:
    return await db.execute_fetchall(
        "SELECT * FROM tasks WHERE task_type='epic' "
        "AND status NOT IN ('completed','failed','rejected') "
        "ORDER BY position ASC, id DESC LIMIT ?",
        (limit,),
    )


async def list_agent_tasks(
    db: aiosqlite.Connection,
    status: str | None = None,
    *,
    limit: int = 50,
) -> list[aiosqlite.Row]:
    if status:
        return await db.execute_fetchall(
            "SELECT * FROM tasks WHERE source='agent' AND status=? "
            "ORDER BY id DESC LIMIT ?",
            (status, limit),
        )
    return await db.execute_fetchall(
        "SELECT * FROM tasks WHERE source='agent' ORDER BY id DESC LIMIT ?",
        (limit,),
    )


async def get_siblings(
    db: aiosqlite.Connection,
    parent_id: int,
    exclude_id: int,
) -> list[aiosqlite.Row]:
    return await db.execute_fetchall(
        "SELECT id, title, task_type, status FROM tasks "
        "WHERE parent_id=? AND id!=? ORDER BY position ASC, id ASC",
        (parent_id, exclude_id),
    )


# ---------------------------------------------------------------------------
# Tasks — Write
# ---------------------------------------------------------------------------


async def create_task(
    db: aiosqlite.Connection,
    *,
    title: str,
    description: str,
    runtime: str,
    source: str,
    assigned_agent: str,
    rationale: str,
    status: str,
    auto_review: bool,
    task_type: str,
    parent_id: int | None,
    priority: str,
    position: int = 0,
) -> int:
    cur = await db.execute(
        "INSERT INTO tasks (title, description, runtime, source, assigned_agent, "
        "rationale, status, auto_review, task_type, parent_id, priority, position) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            title,
            description,
            runtime,
            source,
            assigned_agent,
            rationale,
            status,
            int(auto_review),
            task_type,
            parent_id,
            priority,
            position,
        ),
    )
    return cur.lastrowid  # type: ignore[return-value]


async def update_task(
    db: aiosqlite.Connection,
    task_id: int,
    **fields: Any,
) -> None:
    """Update arbitrary task columns and always bump ``updated_at``."""
    sets = [f"{k}=?" for k in fields]
    sets.append("updated_at=datetime('now')")
    values = list(fields.values())
    values.append(task_id)
    await db.execute(
        f"UPDATE tasks SET {', '.join(sets)} WHERE id=?",
        tuple(values),
    )


async def transition_status_if(
    db: aiosqlite.Connection,
    task_id: int,
    *,
    expected_from: str,
    new_status: str,
) -> bool:
    """Atomically transition ``task.status`` only when the current value
    matches ``expected_from``. Returns ``True`` if the row was updated.

    Used to close the read/write race in approve/reject/start: a second
    concurrent caller will see ``rowcount == 0`` and can be rejected with
    409 Conflict instead of double-processing the task. Review I5.
    """
    cur = await db.execute(
        "UPDATE tasks SET status=?, updated_at=datetime('now') WHERE id=? AND status=?",
        (new_status, task_id, expected_from),
    )
    return (cur.rowcount or 0) > 0


async def create_task_full(
    db: aiosqlite.Connection,
    payload: Any,
    *,
    status: str,
    position: int = 0,
) -> int:
    """Insert a task from a TaskCreate-like model with all structured fields.

    Used by the Hub API and CLI when the new structured form is enabled.
    The legacy ``create_task`` stays untouched for callers that still pass
    individual columns.
    """
    structured = structured_fields_to_db(payload)
    base_kwargs = {
        "title": payload.title,
        "description": payload.description,
        "runtime": payload.runtime.value
        if hasattr(payload.runtime, "value")
        else payload.runtime,
        "source": payload.source.value
        if hasattr(payload.source, "value")
        else payload.source,
        "assigned_agent": payload.agent,
        "rationale": payload.rationale,
        "human_owner": payload.human_owner,
        "human_reviewer": payload.human_reviewer,
        "status": status,
        "auto_review": int(bool(payload.auto_review)),
        "task_type": payload.task_type.value
        if hasattr(payload.task_type, "value")
        else payload.task_type,
        "parent_id": payload.parent_id,
        "priority": payload.priority.value
        if hasattr(payload.priority, "value")
        else payload.priority,
        "position": position,
    }
    columns = list(base_kwargs) + [k for k in STRUCTURED_TASK_FIELDS if k in structured]
    values = [base_kwargs[k] for k in base_kwargs] + [
        structured[k] for k in STRUCTURED_TASK_FIELDS if k in structured
    ]
    placeholders = ", ".join("?" for _ in columns)
    cur = await db.execute(
        f"INSERT INTO tasks ({', '.join(columns)}) VALUES ({placeholders})",
        tuple(values),
    )
    return cur.lastrowid  # type: ignore[return-value]


async def update_task_structured(
    db: aiosqlite.Connection,
    task_id: int,
    refine: Any,
) -> dict[str, Any]:
    """Apply a TaskRefine PATCH-style payload to ``tasks``.

    Only fields explicitly set on the model are written. Returns the dict
    of column updates actually applied (useful for tests and audit). ACs
    are NOT handled here — they live in their own table.
    """
    updates = structured_fields_to_db(refine, exclude_unset=True)
    if not updates:
        return {}
    await update_task(db, task_id, **updates)
    return updates


async def append_task_risk(
    db: aiosqlite.Connection,
    task_id: int,
    risk: Any,
) -> bool:
    """Atomically append one TaskRisk payload to the JSON risks column.

    This intentionally avoids the old read-modify-write pattern used by
    clients through ``/refine``. SQLite's JSON append happens inside one
    UPDATE statement, so concurrent callers do not overwrite each other.
    """
    payload = risk.model_dump(mode="json")
    cur = await db.execute(
        "UPDATE tasks "
        "SET risks=json_insert(risks, '$[#]', json(?)), "
        "updated_at=datetime('now') "
        "WHERE id=?",
        (json.dumps(payload, ensure_ascii=False), task_id),
    )
    return (cur.rowcount or 0) > 0


# ---------------------------------------------------------------------------
# Acceptance criteria — CRUD
# ---------------------------------------------------------------------------


async def list_acceptance_criteria(
    db: aiosqlite.Connection,
    task_id: int,
) -> list[aiosqlite.Row]:
    return await db.execute_fetchall(
        "SELECT * FROM acceptance_criteria WHERE task_id=? "
        "ORDER BY position ASC, id ASC",
        (task_id,),
    )


async def add_acceptance_criterion(
    db: aiosqlite.Connection,
    task_id: int,
    ac: Any,
    *,
    position: int | None = None,
) -> int:
    """Insert a single AC. Raises ``aiosqlite.IntegrityError`` if the
    ``(task_id, ac_id)`` pair already exists — callers translate to 409.
    """
    if position is None:
        rows = await db.execute_fetchall(
            "SELECT COALESCE(MAX(position), -1) + 1 AS next_pos "
            "FROM acceptance_criteria WHERE task_id=?",
            (task_id,),
        )
        position = int(rows[0]["next_pos"]) if rows else 0
    kwargs = ac_to_row_kwargs(ac)
    cur = await db.execute(
        "INSERT INTO acceptance_criteria "
        "(task_id, ac_id, given, when_clause, then_clause, verifiable_by, "
        "test_ref, position) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            task_id,
            kwargs["ac_id"],
            kwargs["given"],
            kwargs["when_clause"],
            kwargs["then_clause"],
            kwargs["verifiable_by"],
            kwargs["test_ref"],
            position,
        ),
    )
    return cur.lastrowid  # type: ignore[return-value]


async def replace_acceptance_criteria(
    db: aiosqlite.Connection,
    task_id: int,
    items: list[Any],
) -> int:
    """Atomic replace: validate uniqueness in payload, DELETE all, INSERT new.

    Returns the number of inserted ACs. Caller is responsible for
    ``commit()`` (consistent with the rest of repository).
    """
    seen: set[str] = set()
    for ac in items:
        if ac.id in seen:
            raise ValueError(f"duplicate ac_id in payload: {ac.id}")
        seen.add(ac.id)

    await db.execute("DELETE FROM acceptance_criteria WHERE task_id=?", (task_id,))
    for position, ac in enumerate(items):
        kwargs = ac_to_row_kwargs(ac)
        await db.execute(
            "INSERT INTO acceptance_criteria "
            "(task_id, ac_id, given, when_clause, then_clause, "
            "verifiable_by, test_ref, position) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                kwargs["ac_id"],
                kwargs["given"],
                kwargs["when_clause"],
                kwargs["then_clause"],
                kwargs["verifiable_by"],
                kwargs["test_ref"],
                position,
            ),
        )
    return len(items)


async def delete_acceptance_criterion(
    db: aiosqlite.Connection,
    task_id: int,
    ac_id: str,
) -> bool:
    """Delete a single AC by its task-scoped id. Returns True if removed."""
    cur = await db.execute(
        "DELETE FROM acceptance_criteria WHERE task_id=? AND ac_id=?",
        (task_id, ac_id),
    )
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Task Updates — Read
# ---------------------------------------------------------------------------


async def get_task_updates(
    db: aiosqlite.Connection,
    task_id: int,
) -> list[aiosqlite.Row]:
    return await db.execute_fetchall(
        "SELECT * FROM task_updates WHERE task_id=? ORDER BY id ASC",
        (task_id,),
    )


async def get_task_update_by_id(
    db: aiosqlite.Connection,
    update_id: int,
) -> aiosqlite.Row | None:
    rows = await db.execute_fetchall(
        "SELECT * FROM task_updates WHERE id=?",
        (update_id,),
    )
    return rows[0] if rows else None


async def has_done_updates(
    db: aiosqlite.Connection,
    task_id: int,
) -> bool:
    rows = await db.execute_fetchall(
        "SELECT id FROM task_updates WHERE task_id=? AND kind='done'",
        (task_id,),
    )
    return bool(rows)


async def has_plan_updates(
    db: aiosqlite.Connection,
    task_id: int,
) -> bool:
    rows = await db.execute_fetchall(
        "SELECT id FROM task_updates WHERE task_id=? "
        "AND kind='status' AND content LIKE 'Plan:%'",
        (task_id,),
    )
    return bool(rows)


async def has_stale_alert(
    db: aiosqlite.Connection,
    task_id: int,
) -> bool:
    rows = await db.execute_fetchall(
        "SELECT id FROM task_updates WHERE task_id=? AND kind='alert' "
        "AND content LIKE '%stale%' ORDER BY id DESC LIMIT 1",
        (task_id,),
    )
    return bool(rows)


# ---------------------------------------------------------------------------
# Task Updates — Write
# ---------------------------------------------------------------------------


async def add_task_update(
    db: aiosqlite.Connection,
    task_id: int,
    agent: str,
    kind: str,
    content: str,
) -> int:
    cur = await db.execute(
        "INSERT INTO task_updates (task_id, agent, kind, content) VALUES (?, ?, ?, ?)",
        (task_id, agent, kind, content),
    )
    return cur.lastrowid  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Activity — Read
# ---------------------------------------------------------------------------


async def list_activity(
    db: aiosqlite.Connection,
    limit: int = 30,
) -> list[aiosqlite.Row]:
    return await db.execute_fetchall(
        "SELECT * FROM activity_log ORDER BY id DESC LIMIT ?",
        (limit,),
    )
