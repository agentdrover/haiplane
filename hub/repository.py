"""OpenClaw Hub — Data access layer (repository).

All SQL queries live here. Functions take ``aiosqlite.Connection`` as the
first argument and return raw rows (``aiosqlite.Row``) or primitives.
No Pydantic models, no business logic.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode

import aiosqlite

from hub.db import (
    STRUCTURED_TASK_FIELDS,
    ac_to_row_kwargs,
    structured_fields_to_db,
)

# Draft queue ranking (#253): deterministic, no weights — DoR-ready first,
# then higher readiness, then older drafts (age = FIFO fairness).
DRAFT_QUEUE_ORDER_BY = "dor_passed DESC, readiness_score DESC, created_at ASC, id ASC"

ALLOWED_TASKS_ORDER_BY = frozenset(
    {
        "id DESC",
        "updated_at ASC",
        DRAFT_QUEUE_ORDER_BY,
    }
)


def _append_person_filters(
    conditions: list[str],
    params: list[Any],
    *,
    human_owner: str | None = None,
    claimed_by: str | None = None,
    mine: str | None = None,
) -> None:
    """Apply inbox/list person filters: mine = owner OR claim holder."""
    if mine:
        conditions.append("(human_owner=? OR claimed_by=?)")
        params.extend([mine, mine])
        return
    if human_owner:
        conditions.append("human_owner=?")
        params.append(human_owner)
    if claimed_by:
        conditions.append("claimed_by=?")
        params.append(claimed_by)


def inbox_query_string(
    *,
    human_owner: str | None = None,
    claimed_by: str | None = None,
    mine: str | None = None,
) -> str:
    params: dict[str, str] = {}
    if mine:
        params["mine"] = mine
    if human_owner:
        params["human_owner"] = human_owner
    if claimed_by:
        params["claimed_by"] = claimed_by
    if not params:
        return ""
    return f"?{urlencode(params)}"


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
    claimed_by: str | None = None,
    mine: str | None = None,
    limit: int = 50,
    include_archived: bool = False,
    after_id: int | None = None,
) -> list[aiosqlite.Row]:
    conditions: list[str] = []
    params: list[Any] = []

    if not include_archived:
        conditions.append("archived=0")

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
    if human_reviewer:
        conditions.append("human_reviewer=?")
        params.append(human_reviewer)
    _append_person_filters(
        conditions,
        params,
        human_owner=human_owner,
        claimed_by=claimed_by,
        mine=mine,
    )

    # Cursor pagination (#254): a paged walk orders by id DESC only, so the
    # cursor (last returned id) is stable across pages; after_id=0 starts
    # the walk. Non-paged calls keep the board ordering.
    if after_id is not None:
        if after_id > 0:
            conditions.append("id < ?")
            params.append(after_id)
        order = "id DESC"
    else:
        order = "position ASC, id DESC"
    where = " AND ".join(conditions) if conditions else "1=1"
    params.append(limit)
    return await db.execute_fetchall(
        f"SELECT * FROM tasks WHERE {where} ORDER BY {order} LIMIT ?",  # nosec B608
        tuple(params),
    )


async def list_tasks_by_statuses(
    db: aiosqlite.Connection,
    statuses: list[str],
    *,
    limit: int = 20,
    include_archived: bool = False,
) -> list[aiosqlite.Row]:
    placeholders = ",".join("?" for _ in statuses)
    archived_sql = "" if include_archived else " AND archived=0"
    return await db.execute_fetchall(
        f"SELECT * FROM tasks WHERE status IN ({placeholders}){archived_sql} "  # nosec B608
        "ORDER BY id DESC LIMIT ?",
        (*statuses, limit),
    )


async def list_tasks_by_status(
    db: aiosqlite.Connection,
    status: str,
    *,
    order_by: str = "id DESC",
    limit: int = 20,
    include_archived: bool = False,
    human_owner: str | None = None,
    claimed_by: str | None = None,
    mine: str | None = None,
) -> list[aiosqlite.Row]:
    if order_by not in ALLOWED_TASKS_ORDER_BY:
        raise ValueError(f"Unsupported order_by clause: {order_by!r}")
    conditions = ["status=?"]
    params: list[Any] = [status]
    if not include_archived:
        conditions.append("archived=0")
    _append_person_filters(
        conditions,
        params,
        human_owner=human_owner,
        claimed_by=claimed_by,
        mine=mine,
    )
    where = " AND ".join(conditions)
    return await db.execute_fetchall(
        f"SELECT * FROM tasks WHERE {where} ORDER BY {order_by} LIMIT ?",  # nosec B608
        (*params, limit),
    )


async def list_running_dispatchable(
    db: aiosqlite.Connection,
) -> list[aiosqlite.Row]:
    return await db.execute_fetchall(
        "SELECT * FROM tasks WHERE archived=0 AND status IN ('running', 'fix_requested') "
        "AND job_id IS NOT NULL",
    )


async def list_review_tasks(
    db: aiosqlite.Connection,
) -> list[aiosqlite.Row]:
    return await db.execute_fetchall(
        "SELECT * FROM tasks WHERE archived=0 AND status='review' "
        "AND review_job_id IS NOT NULL",
    )


async def list_ci_check_tasks(
    db: aiosqlite.Connection,
) -> list[aiosqlite.Row]:
    return await db.execute_fetchall(
        "SELECT * FROM tasks WHERE archived=0 AND status='ci_check'",
    )


async def list_stale_running(
    db: aiosqlite.Connection,
    threshold_minutes: int,
    *,
    human_owner: str | None = None,
    claimed_by: str | None = None,
    mine: str | None = None,
) -> list[aiosqlite.Row]:
    conditions = [
        "archived=0",
        "status='running'",
        "updated_at < datetime('now', ?)",
    ]
    params: list[Any] = [f"-{threshold_minutes} minutes"]
    _append_person_filters(
        conditions,
        params,
        human_owner=human_owner,
        claimed_by=claimed_by,
        mine=mine,
    )
    where = " AND ".join(conditions)
    return await db.execute_fetchall(
        f"SELECT * FROM tasks WHERE {where}",  # nosec B608
        tuple(params),
    )


# ---------------------------------------------------------------------------
# Projects (#335)
# ---------------------------------------------------------------------------


async def create_project(
    db: aiosqlite.Connection,
    *,
    slug: str,
    name: str,
    repo_name: str = "",
    workspace_path: str = "",
    default_branch: str = "develop",
    default_branch_policy: str = "{}",
    status: str = "active",
) -> int:
    cur = await db.execute(
        "INSERT INTO projects (slug, name, repo, workspace_path, "
        "default_branch, default_branch_policy, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            slug,
            name,
            repo_name,
            workspace_path,
            default_branch,
            default_branch_policy,
            status,
        ),
    )
    return cur.lastrowid  # type: ignore[return-value]


async def get_project(
    db: aiosqlite.Connection, project_id: int
) -> aiosqlite.Row | None:
    rows = await db.execute_fetchall("SELECT * FROM projects WHERE id=?", (project_id,))
    return rows[0] if rows else None


async def get_project_by_slug(
    db: aiosqlite.Connection, slug: str
) -> aiosqlite.Row | None:
    rows = await db.execute_fetchall("SELECT * FROM projects WHERE slug=?", (slug,))
    return rows[0] if rows else None


async def list_projects(
    db: aiosqlite.Connection,
    *,
    include_archived: bool = False,
    only_active: bool = False,
) -> list[aiosqlite.Row]:
    conditions = []
    if not include_archived:
        conditions.append("archived=0")
    if only_active:
        # Pending proposals (#345) stay out of routing and selectors.
        conditions.append("status='active'")
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    return await db.execute_fetchall(
        f"SELECT * FROM projects {where} ORDER BY slug ASC"  # nosec B608
    )


async def update_project(
    db: aiosqlite.Connection, project_id: int, **fields: Any
) -> None:
    sets = [f"{k}=?" for k in fields]
    sets.append("updated_at=datetime('now')")
    values = [*fields.values(), project_id]
    await db.execute(
        f"UPDATE projects SET {', '.join(sets)} WHERE id=?",  # nosec B608
        tuple(values),
    )


async def list_task_ids_for_project(
    db: aiosqlite.Connection, project_id: int
) -> set[int]:
    """All task ids whose root epic belongs to the project (#336)."""
    rows = await db.execute_fetchall(
        """
        WITH RECURSIVE subtree(id) AS (
            SELECT id FROM tasks WHERE project_id = ?
            UNION ALL
            SELECT t.id FROM tasks t JOIN subtree s ON t.parent_id = s.id
        )
        SELECT id FROM subtree
        """,
        (project_id,),
    )
    return {r["id"] for r in rows}


async def resolve_project_for_task(
    db: aiosqlite.Connection, task_id: int
) -> aiosqlite.Row | None:
    """Resolve a task's project by walking up to its root epic (#335).

    Projects live on epics only; descendants inherit. Tasks outside any
    epic (or epics without an assignment) fall back to the seeded
    'default' project so legacy behavior never breaks.
    """
    current_id: int | None = task_id
    for _ in range(20):  # hierarchy depth guard
        if current_id is None:
            break
        rows = await db.execute_fetchall(
            "SELECT id, parent_id, project_id FROM tasks WHERE id=?",
            (current_id,),
        )
        if not rows:
            break
        row = rows[0]
        if row["project_id"] is not None:
            project = await get_project(db, row["project_id"])
            # A pending proposal (#345) must not affect routing yet.
            if project is not None and project["status"] != "active":
                return await get_project_by_slug(db, "default")
            return project
        current_id = row["parent_id"]
    return await get_project_by_slug(db, "default")


async def list_stale_tasks(
    db: aiosqlite.Connection,
    status: str,
    threshold_minutes: int,
    *,
    require_null_review_job: bool = False,
) -> list[aiosqlite.Row]:
    """Tasks stuck in ``status`` with no updates for ``threshold_minutes``.

    Generalizes stale detection beyond running (#319): review, claimed, and
    needs_info can silently dead-end otherwise. ``require_null_review_job``
    restricts review staleness to client-driven reviews — headless reviews
    (review_job_id set) are owned by the poller conveyor.
    """
    conditions = [
        "archived=0",
        "status=?",
        "updated_at < datetime('now', ?)",
    ]
    params: list[Any] = [status, f"-{threshold_minutes} minutes"]
    if require_null_review_job:
        conditions.append("review_job_id IS NULL")
    where = " AND ".join(conditions)
    return await db.execute_fetchall(
        f"SELECT * FROM tasks WHERE {where}",  # nosec B608
        tuple(params),
    )


async def list_active_epics(
    db: aiosqlite.Connection,
    *,
    limit: int = 20,
) -> list[aiosqlite.Row]:
    return await db.execute_fetchall(
        "SELECT * FROM tasks WHERE archived=0 AND task_type='epic' "
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
            "SELECT * FROM tasks WHERE archived=0 AND source='agent' AND status=? "
            "ORDER BY id DESC LIMIT ?",
            (status, limit),
        )
    return await db.execute_fetchall(
        "SELECT * FROM tasks WHERE archived=0 AND source='agent' ORDER BY id DESC LIMIT ?",
        (limit,),
    )


async def get_siblings(
    db: aiosqlite.Connection,
    parent_id: int,
    exclude_id: int,
) -> list[aiosqlite.Row]:
    return await db.execute_fetchall(
        "SELECT id, title, task_type, status FROM tasks "
        "WHERE parent_id=? AND id!=? AND archived=0 ORDER BY position ASC, id ASC",
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
        f"UPDATE tasks SET {', '.join(sets)} WHERE id=?",  # nosec B608
        tuple(values),
    )


async def bump_submission_generation(
    db: aiosqlite.Connection,
    task_id: int,
) -> int:
    """Increment the review submission generation and return the new value.

    The increment happens in SQL (not read-modify-write in Python) so two
    concurrent submissions cannot both claim the same generation. Bumping
    the generation is what invalidates earlier APPROVED verdicts: a verdict
    only counts while ``review_verdict_generation == submission_generation``.
    """
    await db.execute(
        "UPDATE tasks SET submission_generation = submission_generation + 1, "
        "updated_at=datetime('now') WHERE id=?",
        (task_id,),
    )
    cur = await db.execute(
        "SELECT submission_generation FROM tasks WHERE id=?", (task_id,)
    )
    row = await cur.fetchone()
    return int(row[0]) if row else 0


async def record_review_verdict(
    db: aiosqlite.Connection,
    task_id: int,
    verdict: str,
    findings_json: str = "[]",
) -> None:
    """Persist a review verdict bound to the CURRENT submission generation.

    The binding is done in SQL (``review_verdict_generation = submission_generation``)
    so the verdict can never be attached to a generation the caller read
    before a concurrent resubmission bumped it. ``findings_json`` replaces
    the stored findings wholesale: findings belong to their verdict, so a
    verdict without findings clears the previous list (#308).
    """
    await db.execute(
        "UPDATE tasks SET review_verdict=?, "
        "review_verdict_generation=submission_generation, "
        "review_findings=?, "
        "updated_at=datetime('now') WHERE id=?",
        (verdict, findings_json, task_id),
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
        f"INSERT INTO tasks ({', '.join(columns)}) VALUES ({placeholders})",  # nosec B608
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


async def upsert_acceptance_criterion(
    db: aiosqlite.Connection,
    task_id: int,
    ac: Any,
) -> bool:
    """Insert an AC or update it in place when ``(task_id, ac_id)`` exists.

    Idempotent by ``ac_id``: re-sending the same payload is a no-op write,
    and a changed payload overwrites the row without a 409. New rows get the
    next position; existing rows keep their position. Returns ``True`` when a
    new row was inserted, ``False`` when an existing one was updated.
    Caller owns the commit.
    """
    rows = await db.execute_fetchall(
        "SELECT 1 FROM acceptance_criteria WHERE task_id=? AND ac_id=?",
        (task_id, ac.id),
    )
    existed = bool(rows)

    pos_rows = await db.execute_fetchall(
        "SELECT COALESCE(MAX(position), -1) + 1 AS next_pos "
        "FROM acceptance_criteria WHERE task_id=?",
        (task_id,),
    )
    next_pos = int(pos_rows[0]["next_pos"]) if pos_rows else 0

    kwargs = ac_to_row_kwargs(ac)
    await db.execute(
        "INSERT INTO acceptance_criteria "
        "(task_id, ac_id, given, when_clause, then_clause, verifiable_by, "
        "test_ref, position) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(task_id, ac_id) DO UPDATE SET "
        "given=excluded.given, when_clause=excluded.when_clause, "
        "then_clause=excluded.then_clause, verifiable_by=excluded.verifiable_by, "
        "test_ref=excluded.test_ref",
        (
            task_id,
            kwargs["ac_id"],
            kwargs["given"],
            kwargs["when_clause"],
            kwargs["then_clause"],
            kwargs["verifiable_by"],
            kwargs["test_ref"],
            next_pos,
        ),
    )
    return not existed


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


async def collect_subtree_ids(
    db: aiosqlite.Connection,
    root_id: int,
) -> list[int]:
    """All task ids in the subtree rooted at ``root_id`` (root first, BFS)."""
    out: list[int] = []
    queue: list[int] = [root_id]
    seen: set[int] = set()
    while queue:
        tid = queue.pop(0)
        if tid in seen:
            continue
        seen.add(tid)
        out.append(tid)
        rows = await db.execute_fetchall(
            "SELECT id FROM tasks WHERE parent_id=? ORDER BY id ASC",
            (tid,),
        )
        for r in rows:
            queue.append(r[0])
    return out


async def subtree_ids_deepest_first(
    db: aiosqlite.Connection,
    root_id: int,
) -> list[int]:
    """Subtree ids ordered so children always precede their ancestors."""
    rows = await db.execute_fetchall(
        """
        WITH RECURSIVE sub(id, depth) AS (
          SELECT id, 0 FROM tasks WHERE id = ?
          UNION ALL
          SELECT t.id, sub.depth + 1 FROM tasks t
          INNER JOIN sub ON t.parent_id = sub.id
        )
        SELECT id FROM sub ORDER BY depth DESC, id DESC
        """,
        (root_id,),
    )
    return [r[0] for r in rows]


async def set_tasks_archived(
    db: aiosqlite.Connection,
    task_ids: list[int],
    archived: int,
) -> None:
    if not task_ids:
        return
    ph = ",".join("?" * len(task_ids))
    await db.execute(
        f"UPDATE tasks SET archived=?, updated_at=datetime('now') WHERE id IN ({ph})",  # nosec B608
        (archived, *task_ids),
    )


async def delete_task_subtree(db: aiosqlite.Connection, root_id: int) -> int:
    """Delete ``root_id`` and all descendants. Returns number of ``tasks`` rows removed."""
    ids = await subtree_ids_deepest_first(db, root_id)
    if not ids:
        return 0
    ph = ",".join("?" * len(ids))
    await db.execute(f"DELETE FROM task_updates WHERE task_id IN ({ph})", ids)  # nosec B608
    for tid in ids:
        await db.execute("DELETE FROM tasks WHERE id=?", (tid,))
    return len(ids)


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
