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
from hub.models import (
    AWAITING_HUMAN_STATUSES,
    FINAL_STATUSES,
    IN_FLIGHT_STATUSES,
    QUEUED_STATUSES,
)

# One spelling of "this row is finished", derived from the model instead of
# retyped in SQL (#571). list_live_epics had the three values written out by
# hand, and #569 shipped that list WRONG — it omitted `failed` until spec review
# caught it. A second hand-typed copy for orphan tasks would make three places
# to keep in step, which is the same defect as the permission list in #614.
FINAL_STATUS_VALUES = tuple(sorted(s.value for s in FINAL_STATUSES))
_FINAL_PLACEHOLDERS = ",".join("?" * len(FINAL_STATUS_VALUES))

# Same treatment for the two sets #567 needs: values come from the model, and
# the only thing interpolated into SQL is a run of "?".
AWAITING_HUMAN_STATUS_VALUES = tuple(sorted(s.value for s in AWAITING_HUMAN_STATUSES))
IN_FLIGHT_STATUS_VALUES = tuple(sorted(s.value for s in IN_FLIGHT_STATUSES))
QUEUED_STATUS_VALUES = tuple(sorted(s.value for s in QUEUED_STATUSES))
_AWAITING_PLACEHOLDERS = ",".join("?" * len(AWAITING_HUMAN_STATUS_VALUES))
_QUEUED_PLACEHOLDERS = ",".join("?" * len(QUEUED_STATUS_VALUES))
_IN_FLIGHT_PLACEHOLDERS = ",".join("?" * len(IN_FLIGHT_STATUS_VALUES))


class UnknownTaskStateError(ValueError):
    """Raised for a ``state`` filter value that is not one of the named modes.

    An unrecognised value must NOT silently mean "no filter": a parameter that
    quietly does nothing is worse than one that does not exist, because the
    caller believes it was applied. The API layer turns this into a 400.
    """


# The named status-set modes (#617). A counter and the list behind its link have
# to agree, and "live" is the NEGATION of a set — impossible to express through
# the single-valued ``status`` filter, which is exactly why "Без эпика: 19 живых"
# opened a list of 51.
#
# Named values only, taken from the model's own sets. Accepting arbitrary status
# lists in a query string would turn this into a small query language nobody
# validates; a fourth NAMED mode is the way to grow, per the task's revisit
# condition.
#
# ``awaiting`` excludes a headless review — a review with ``review_job_id`` set
# belongs to the poller conveyor, not to a person. Same rule as
# ``list_stale_by_status(require_null_review_job=True)`` and the card counter in
# #567; disagreeing with it here would recreate the very mismatch this fixes.
TASK_STATE_FILTERS: dict[str, tuple[str, tuple[str, ...]]] = {
    "live": (f"status NOT IN ({_FINAL_PLACEHOLDERS})", FINAL_STATUS_VALUES),
    "awaiting": (
        f"status IN ({_AWAITING_PLACEHOLDERS}) "
        "AND NOT (status='review' AND review_job_id IS NOT NULL)",
        AWAITING_HUMAN_STATUS_VALUES,
    ),
    "inflight": (f"status IN ({_IN_FLIGHT_PLACEHOLDERS})", IN_FLIGHT_STATUS_VALUES),
    # A fourth NAMED mode, added exactly the way #617's revisit condition said to
    # grow this: approved and untouched is a queue, and it deserves its own link
    # rather than an arbitrary status list in the query string (#619).
    "queued": (f"status IN ({_QUEUED_PLACEHOLDERS})", QUEUED_STATUS_VALUES),
}


def task_state_condition(state: str) -> tuple[str, tuple[str, ...]]:
    """(sql, params) for one named state mode, or raise for anything else."""
    try:
        return TASK_STATE_FILTERS[state]
    except KeyError as exc:
        known = ", ".join(sorted(TASK_STATE_FILTERS))
        raise UnknownTaskStateError(f"unknown state {state!r}; known: {known}") from exc


# A task no epic-shaped view can reach: no parent, and not an epic itself. The
# project sits on the epic and descendants inherit it, so these rows belong to
# no project either — by construction, not by mistake. Archival is deliberately
# NOT part of this: callers control that through include_archived.
ORPHAN_CONDITION = "parent_id IS NULL AND task_type != 'epic'"

# Whether an epic is alive. Written once and used by BOTH the live list and the
# done list, so the two are exact complements: spell the condition twice and an
# epic can fall out of both at once, which is the silent loss #569 refused when
# it dropped its LIMIT.
_EPIC_IS_LIVE = """
        EXISTS (
            SELECT 1 FROM sub JOIN tasks c ON c.id = sub.id
            WHERE sub.root = e.id AND sub.id != sub.root
              AND c.archived = 0
              AND c.status NOT IN ({finals})
        )
        OR (
            NOT EXISTS (SELECT 1 FROM tasks ch WHERE ch.parent_id = e.id)
            AND e.status NOT IN ({finals})
        )
"""

_SUBTREE_CTE = """
    WITH RECURSIVE sub(root, id) AS (
        SELECT id, id FROM tasks WHERE task_type='epic' AND archived=0
        UNION ALL
        SELECT sub.root, t.id FROM tasks t JOIN sub ON t.parent_id = sub.id
    )"""

# When work last HAPPENED in this epic's subtree — from the feed, not from
# ``updated_at`` (#570).
#
# ``updated_at`` is bumped by any write at all (#616 spelled that out), so it
# answers "when was this row last touched". Checked against production: ordering
# by it floated #182, #192, #209, #371 and #394 to the top — the five epics whose
# PROJECT had just been reassigned. An administrative touch would have looked
# exactly like work.
#
# Falling back to ``updated_at`` was the first design and a TEST KILLED IT: with
# a per-row COALESCE the epic's own touch entered the maximum, so a rename
# outranked a week of real work — the exact noise this key exists to avoid. An
# epic with no feed entry anywhere in its subtree has no activity, full stop; it
# sorts last (SQLite ranks NULL below every value, so DESC puts it at the end)
# and stays in the list, which is the honest answer rather than a borrowed date.
_EPIC_ACTIVITY = """
        SELECT MAX(u.created_at)
        FROM sub JOIN task_updates u ON u.task_id = sub.id
        WHERE sub.root = e.id
"""

# Built once, from the placeholders above: the only thing interpolated is a run
# of "?" — the status values themselves travel as parameters.
_LIVE_EPICS_SQL = f"""
    {_SUBTREE_CTE}
    SELECT e.*, ({_EPIC_ACTIVITY}) AS last_activity_at
    FROM tasks e
    WHERE e.task_type='epic' AND e.archived=0
      AND ({_EPIC_IS_LIVE})
    ORDER BY last_activity_at DESC, e.id DESC
""".format(finals=_FINAL_PLACEHOLDERS)  # nosec B608 - placeholders only, values are params

# The complement, from the same condition: an epic is here exactly when it is not
# in the live list. Done epics were not collapsed before this — they were gone
# (#569 excluded them), so the count is what returns access to them.
_DONE_EPICS_SQL = f"""
    {_SUBTREE_CTE}
    SELECT e.*, ({_EPIC_ACTIVITY}) AS last_activity_at
    FROM tasks e
    WHERE e.task_type='epic' AND e.archived=0
      AND NOT ({_EPIC_IS_LIVE})
    ORDER BY last_activity_at DESC, e.id DESC
""".format(finals=_FINAL_PLACEHOLDERS)  # nosec B608 - placeholders only, values are params

_LIVE_ORPHANS_COUNT_SQL = (
    f"SELECT COUNT(*) AS n FROM tasks "  # nosec B608 - placeholders only
    f"WHERE {ORPHAN_CONDITION} AND archived=0 "
    f"AND status NOT IN ({_FINAL_PLACEHOLDERS})"
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
    no_epic: bool = False,
    state: str | None = None,
    human_owner: str | None = None,
    human_reviewer: str | None = None,
    claimed_by: str | None = None,
    mine: str | None = None,
    limit: int = 50,
    include_archived: bool = False,
    after_id: int | None = None,
    project_id: int | None = None,
) -> list[aiosqlite.Row]:
    conditions: list[str] = []
    params: list[Any] = []

    if project_id is not None:
        # The project filter has to run BEFORE the LIMIT. Applied afterwards in
        # Python it discarded rows the page had already spent, so a page whose
        # top limit+1 rows belonged to other projects came back empty with
        # next_cursor=null — indistinguishable from "this project has no
        # tasks" (#370). Same subtree rule as list_task_ids_for_project: the
        # project sits on the epic and descendants inherit it.
        conditions.append(
            """id IN (
                WITH RECURSIVE subtree(id) AS (
                    SELECT id FROM tasks WHERE project_id = ?
                    UNION ALL
                    SELECT t.id FROM tasks t JOIN subtree s ON t.parent_id = s.id
                )
                SELECT id FROM subtree
            )"""
        )
        params.append(project_id)

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
    if no_epic:
        # A SEPARATE flag, never a special value of parent_id: a blank parent_id
        # means "no filter" and a test pins that (#571 AC-3). Overloading it
        # would silently change every existing link that passes an empty box.
        conditions.append(ORPHAN_CONDITION)
    if state:
        # #617: a third independent parameter. It composes with status and
        # no_epic rather than replacing either — the counter needs "live", the
        # hierarchy flag needs "no parent", and they are different questions.
        sql, values = task_state_condition(state)
        conditions.append(sql)
        params.extend(values)
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


async def list_unmerged_branch_tasks(
    db: aiosqlite.Connection,
    *,
    exclude_task_id: int,
    statuses: list[str],
) -> list[aiosqlite.Row]:
    """Active tasks (other than ``exclude_task_id``) that own a branch (#438)."""
    placeholders = ",".join("?" for _ in statuses)
    return await db.execute_fetchall(
        f"SELECT id, title, status, branch FROM tasks "  # nosec B608
        f"WHERE archived=0 AND id != ? AND status IN ({placeholders}) "
        "AND branch IS NOT NULL AND TRIM(branch) != '' ORDER BY id",
        (exclude_task_id, *statuses),
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


async def record_pipeline_merge(
    db: aiosqlite.Connection,
    *,
    pr_number: int,
    merge_sha: str = "",
    project_id: int | None = None,
    task_id: int | None = None,
) -> None:
    """Remember a merge the hub performed itself, by the commit it produced.

    ``merge_sha`` is the evidence. The pull-request number is kept for
    context only: it lives in the commit subject, which the person pushing
    controls, so matching on it left the guard bypassable by typing a number
    that had been merged before (#534, review of submission #2).
    """
    await db.execute(
        "INSERT OR IGNORE INTO pipeline_merges "
        "(project_id, pr_number, task_id, merge_sha) VALUES (?, ?, ?, ?)",
        (project_id, int(pr_number), task_id, merge_sha or ""),
    )
    await db.commit()


async def pipeline_merge_recorded(
    db: aiosqlite.Connection, task_id: int, pr_number: int
) -> bool:
    """Was this task's PR already delivered by the pipeline (#605)?

    The completion gate must not merge twice: the headless conveyor merges
    inside the poller and then walks the same shared transition, and a second
    merge_pr on an already-merged PR reads as a refusal — which would flip a
    delivered task into needs_decision. Caught by #363's merges-exactly-once
    guard the first time the gate ran.
    """
    rows = await db.execute_fetchall(
        "SELECT 1 FROM pipeline_merges WHERE task_id = ? AND pr_number = ? LIMIT 1",
        (task_id, int(pr_number)),
    )
    return bool(rows)


async def known_pipeline_shas(db: aiosqlite.Connection, project_id: int) -> set[str]:
    """Merge commits this project's pipeline produced.

    Scoped to the project on purpose. An earlier version also pulled in rows
    with a NULL project_id, so a merge recorded for a task without a project
    counted as legitimate everywhere (#534, review of submission #2).
    """
    rows = await db.execute_fetchall(
        "SELECT merge_sha FROM pipeline_merges "
        "WHERE project_id = ? AND COALESCE(merge_sha, '') != ''",
        (project_id,),
    )
    return {dict(r)["merge_sha"] for r in rows}


async def set_drift_baseline(
    db: aiosqlite.Connection, project_id: int, sha: str
) -> None:
    await db.execute(
        "UPDATE projects SET drift_baseline_sha=? WHERE id=?", (sha, project_id)
    )
    await db.commit()


async def record_drift_commit(
    db: aiosqlite.Connection,
    *,
    project_id: int,
    sha: str,
    branch: str,
    subject: str = "",
    author: str = "",
) -> bool:
    """Record a drift commit; True when it was not already known (#534).

    INSERT OR IGNORE on the (project_id, sha) key: the second sighting of the
    same commit returns False and nothing is written, so a periodic check
    cannot turn one violation into a stream of alerts.
    """
    cur = await db.execute(
        "INSERT OR IGNORE INTO base_branch_drift "
        "(project_id, sha, branch, subject, author) VALUES (?, ?, ?, ?, ?)",
        (project_id, sha, branch, subject, author),
    )
    await db.commit()
    return bool(cur.rowcount)


async def list_drift_commits(
    db: aiosqlite.Connection, project_id: int | None = None
) -> list[aiosqlite.Row]:
    if project_id is None:
        return await db.execute_fetchall(
            "SELECT * FROM base_branch_drift ORDER BY detected_at DESC, id DESC"
        )
    return await db.execute_fetchall(
        "SELECT * FROM base_branch_drift WHERE project_id=? "
        "ORDER BY detected_at DESC, id DESC",
        (project_id,),
    )


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


# --- skills library (#380) --------------------------------------------------


async def create_skill_version(
    db: aiosqlite.Connection,
    *,
    name: str,
    kind: str = "prompt",
    content: str,
    tags: str = "[]",
    project_id: int | None = None,
    status: str = "draft",
    created_by: str = "",
) -> tuple[int, int]:
    """Insert the next version for ``name``. Returns (id, version)."""
    rows = await db.execute_fetchall(
        "SELECT COALESCE(MAX(version), 0) AS v FROM skills WHERE name=?", (name,)
    )
    version = (rows[0]["v"] or 0) + 1
    cur = await db.execute(
        "INSERT INTO skills (name, kind, version, content, tags, project_id, "
        "status, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (name, kind, version, content, tags, project_id, status, created_by),
    )
    return cur.lastrowid, version  # type: ignore[return-value]


async def get_active_skill(db: aiosqlite.Connection, name: str) -> aiosqlite.Row | None:
    rows = await db.execute_fetchall(
        "SELECT * FROM skills WHERE name=? AND status='active' "
        "ORDER BY version DESC LIMIT 1",
        (name,),
    )
    return rows[0] if rows else None


async def list_skills(db: aiosqlite.Connection) -> list[aiosqlite.Row]:
    """Latest version per name (active preferred, else newest draft)."""
    return await db.execute_fetchall(
        """
        SELECT s.* FROM skills s
        JOIN (
            SELECT name,
                   COALESCE(
                       MAX(CASE WHEN status='active' THEN version END),
                       MAX(version)
                   ) AS v
            FROM skills GROUP BY name
        ) latest ON latest.name = s.name AND latest.v = s.version
        ORDER BY s.name ASC
        """
    )


async def list_skill_versions(
    db: aiosqlite.Connection, name: str
) -> list[aiosqlite.Row]:
    return await db.execute_fetchall(
        "SELECT * FROM skills WHERE name=? ORDER BY version DESC", (name,)
    )


async def get_skill_version(
    db: aiosqlite.Connection, name: str, version: int
) -> aiosqlite.Row | None:
    rows = await db.execute_fetchall(
        "SELECT * FROM skills WHERE name=? AND version=?", (name, version)
    )
    return rows[0] if rows else None


async def activate_skill_version(
    db: aiosqlite.Connection, name: str, version: int
) -> None:
    await db.execute(
        "UPDATE skills SET status='active' WHERE name=? AND version=?",
        (name, version),
    )


# --- machine reviews (#381) -------------------------------------------------


async def insert_machine_review(
    db: aiosqlite.Connection,
    *,
    task_id: int,
    submission_generation: int,
    harness_skill: str = "",
    harness_version: int | None = None,
    agent_count: int | None = None,
    tokens_spent: int | None = None,
    duration_ms: int | None = None,
    orchestrator: str = "",
    model: str = "",
    raw_count: int = 0,
    findings_confirmed: str = "[]",
    findings_rejected: str = "[]",
    submitted_by: str = "",
    incomplete: bool | None = None,
    unresolved: str = "[]",
    lost_dimensions: str = "[]",
) -> int:
    cur = await db.execute(
        "INSERT INTO machine_reviews (task_id, submission_generation, "
        "harness_skill, harness_version, agent_count, tokens_spent, "
        "duration_ms, orchestrator, model, raw_count, findings_confirmed, "
        "findings_rejected, submitted_by, incomplete, unresolved, "
        "lost_dimensions) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            task_id,
            submission_generation,
            harness_skill,
            harness_version,
            agent_count,
            tokens_spent,
            duration_ms,
            orchestrator,
            model,
            raw_count,
            findings_confirmed,
            findings_rejected,
            submitted_by,
            None if incomplete is None else int(incomplete),
            unresolved,
            lost_dimensions,
        ),
    )
    return cur.lastrowid  # type: ignore[return-value]


async def get_latest_machine_review(
    db: aiosqlite.Connection, task_id: int
) -> aiosqlite.Row | None:
    rows = await db.execute_fetchall(
        "SELECT * FROM machine_reviews WHERE task_id=? ORDER BY id DESC LIMIT 1",
        (task_id,),
    )
    return rows[0] if rows else None


# --- AC test results (#507) ------------------------------------------------


async def upsert_ac_test_result(
    db: aiosqlite.Connection,
    task_id: int,
    ac_id: str,
    generation: int,
    status: str,
) -> None:
    """Record the latest test result for one AC, stamped with its generation."""
    await db.execute(
        "INSERT INTO ac_test_results (task_id, ac_id, submission_generation, "
        "status, created_at) VALUES (?, ?, ?, ?, datetime('now')) "
        "ON CONFLICT(task_id, ac_id) DO UPDATE SET "
        "submission_generation=excluded.submission_generation, "
        "status=excluded.status, created_at=excluded.created_at",
        (task_id, ac_id, generation, status),
    )


async def list_ac_test_results(
    db: aiosqlite.Connection, task_id: int
) -> list[aiosqlite.Row]:
    """All recorded AC test results for a task (any generation)."""
    return await db.execute_fetchall(
        "SELECT * FROM ac_test_results WHERE task_id=?",
        (task_id,),
    )


# --- CI run reports (#546) -------------------------------------------------


async def upsert_ci_run_report(
    db: aiosqlite.Connection,
    *,
    task_id: int,
    head_sha: str,
    ac_results: str,
    validation_status: str,
    validation_log: str,
    reason: str,
    reported_by: str,
) -> None:
    """Store what a CI run reported for one commit (idempotent per commit).

    Re-running CI on the same commit updates the row rather than adding a
    second opinion — the same rule the merge ledger follows (#605).
    """
    await db.execute(
        "INSERT INTO ci_run_reports (task_id, head_sha, ac_results, "
        "validation_status, validation_log, reason, reported_by, reported_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now')) "
        "ON CONFLICT(task_id, head_sha) DO UPDATE SET "
        "ac_results=excluded.ac_results, "
        "validation_status=excluded.validation_status, "
        "validation_log=excluded.validation_log, "
        "reason=excluded.reason, reported_by=excluded.reported_by, "
        "reported_at=excluded.reported_at",
        (
            task_id,
            head_sha,
            ac_results,
            validation_status,
            validation_log,
            reason,
            reported_by,
        ),
    )


async def get_ci_run_report(
    db: aiosqlite.Connection, task_id: int, head_sha: str
) -> aiosqlite.Row | None:
    """The report for one commit, or None when that commit was never reported."""
    rows = await db.execute_fetchall(
        "SELECT * FROM ci_run_reports WHERE task_id=? AND head_sha=?",
        (task_id, head_sha),
    )
    return rows[0] if rows else None


async def latest_ci_run_report(
    db: aiosqlite.Connection, task_id: int
) -> aiosqlite.Row | None:
    """The most recently reported run for a task, whatever commit it covered.

    Used only to explain a mismatch ("a run was reported, but for another
    commit") — never to satisfy a gate, which always keys on the pinned SHA.
    """
    rows = await db.execute_fetchall(
        "SELECT * FROM ci_run_reports WHERE task_id=? "
        "ORDER BY reported_at DESC, id DESC LIMIT 1",
        (task_id,),
    )
    return rows[0] if rows else None


# --- events feed (#349) ----------------------------------------------------


async def insert_event(
    db: aiosqlite.Connection,
    *,
    kind: str,
    task_id: int | None = None,
    project_id: int | None = None,
    actor: str = "",
    payload: dict[str, Any] | None = None,
) -> int:
    """Append a typed event. Deliberately NO commit: the caller emits it
    inside the same transaction as the transition it describes, so a
    rollback removes both (#349 AC-1)."""
    cur = await db.execute(
        "INSERT INTO events (kind, task_id, project_id, actor, payload) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            kind,
            task_id,
            project_id,
            actor,
            json.dumps(payload or {}, ensure_ascii=False),
        ),
    )
    return cur.lastrowid  # type: ignore[return-value]


async def list_events(
    db: aiosqlite.Connection,
    *,
    since: int = 0,
    kinds: list[str] | None = None,
    limit: int = 200,
) -> list[aiosqlite.Row]:
    """Events with id > since, oldest first. ``kinds`` narrows the feed."""
    conditions = ["id > ?"]
    params: list[Any] = [since]
    if kinds:
        placeholders = ",".join("?" for _ in kinds)
        conditions.append(f"kind IN ({placeholders})")
        params.extend(kinds)
    params.append(min(limit, 200))
    return await db.execute_fetchall(
        f"SELECT * FROM events WHERE {' AND '.join(conditions)} "  # nosec B608
        "ORDER BY id ASC LIMIT ?",
        tuple(params),
    )


async def prune_events(db: aiosqlite.Connection, *, keep_days: int = 14) -> int:
    """Delete events older than ``keep_days``. Returns rows removed."""
    cur = await db.execute(
        "DELETE FROM events WHERE created_at < datetime('now', ?)",
        (f"-{keep_days} days",),
    )
    return cur.rowcount or 0


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


async def list_live_epics(db: aiosqlite.Connection) -> list[aiosqlite.Row]:
    """Epics where work is actually happening (#569).

    Liveness is judged by DESCENDANTS, not by the epic's own status: a
    completed epic with an open task underneath is live work someone would
    otherwise lose (#501 and #449 are real examples), and an open epic whose
    children are all final is finished noise. An epic with no children at
    all stays live until itself final — a freshly approved epic must not
    vanish before its first task exists.

    "Final" is the model's own set — completed, failed, rejected — not the
    completed/rejected pair the task prose named: failed is terminal
    everywhere else in the lifecycle, and a criterion that disagrees with
    FINAL_STATUSES would fork the model (spec review, finding 1).

    No LIMIT: silently dropping a live epic is exactly the failure this
    replaces (#569 AC-4). The list is bounded by reality — an epic leaves it
    by finishing, not by being the 21st row.

    Ordered by when work last happened in the subtree (#570), newest first —
    not by ``position``, which is 0 on every epic, and not by id, which ranks
    by age of creation and put a June-dead epic above yesterday's work.
    """
    return await db.execute_fetchall(_LIVE_EPICS_SQL, FINAL_STATUS_VALUES * 2)


async def list_done_epics(db: aiosqlite.Connection) -> list[aiosqlite.Row]:
    """Epics whose work is finished — the exact complement of the live list (#570).

    Not merely "not shown": before this they were unreachable from the epic
    views, because #569 filtered them out and nothing counted them. A collapsed
    block with a number gives the access back without putting 14 finished epics
    back into the working list.

    Complement by construction, sharing ``_EPIC_IS_LIVE`` with
    ``list_live_epics``: two hand-written conditions would eventually disagree,
    and an epic matching neither would vanish from both lists — the silent loss
    #569 refused when it dropped its LIMIT.
    """
    return await db.execute_fetchall(_DONE_EPICS_SQL, FINAL_STATUS_VALUES * 2)


async def project_work_summary(db: aiosqlite.Connection) -> dict[int, dict[str, Any]]:
    """Per project: what waits for a human, what is in flight, when work last happened.

    ONE aggregate query for every project rather than a loop of per-project
    queries — the N+1 risk named in #567's own statement.

    Two rules are honoured here rather than restated:

    * the project sits on the EPIC and descendants inherit it (the same subtree
      walk as ``list_task_ids_for_project``), so a task outside every epic
      belongs to no project — it is counted by ``count_live_orphan_tasks``
      instead (#571), and dropping it here is by construction, not by accident;
    * a ``review`` with ``review_job_id`` set is a headless review owned by the
      poller, not something a person is holding — the same exclusion
      ``list_stale_by_status(require_null_review_job=True)`` makes.

    Activity is a SEPARATE query on purpose: joining ``task_updates`` into the
    counting query would multiply every row by its number of feed entries and
    silently inflate all three numbers.

    Epics count as awaiting a human but NOT as work in flight, and the asymmetry
    is deliberate: a draft epic really is waiting for the same approval gate as a
    draft task, while an epic sits in ``open`` for its entire life as a
    container — counting it would add a permanent "+1 in flight" to every
    project, including projects where nothing at all is happening. Caught by a
    test that expected 2 and got 3.
    """
    counts_sql = f"""
        WITH RECURSIVE tree(project_id, id) AS (
            SELECT project_id, id FROM tasks
            WHERE task_type='epic' AND project_id IS NOT NULL AND archived=0
            UNION ALL
            SELECT tree.project_id, t.id FROM tasks t JOIN tree ON t.parent_id = tree.id
        )
        SELECT tree.project_id AS pid,
               SUM(CASE WHEN t.status IN ({_AWAITING_PLACEHOLDERS})
                         AND NOT (t.status='review' AND t.review_job_id IS NOT NULL)
                        THEN 1 ELSE 0 END) AS awaiting,
               SUM(CASE WHEN t.status='draft' THEN 1 ELSE 0 END) AS drafts,
               SUM(CASE WHEN t.status IN ({_IN_FLIGHT_PLACEHOLDERS})
                         AND t.task_type != 'epic'
                        THEN 1 ELSE 0 END) AS in_flight,
               SUM(CASE WHEN t.status IN ({_QUEUED_PLACEHOLDERS})
                         AND t.task_type != 'epic'
                        THEN 1 ELSE 0 END) AS queued
        FROM tree JOIN tasks t ON t.id = tree.id
        WHERE t.archived=0
        GROUP BY tree.project_id
    """  # nosec B608 - placeholders only, values are params
    activity_sql = """
        WITH RECURSIVE tree(project_id, id) AS (
            SELECT project_id, id FROM tasks
            WHERE task_type='epic' AND project_id IS NOT NULL AND archived=0
            UNION ALL
            SELECT tree.project_id, t.id FROM tasks t JOIN tree ON t.parent_id = tree.id
        )
        SELECT tree.project_id AS pid, MAX(u.created_at) AS last_activity_at
        FROM tree JOIN task_updates u ON u.task_id = tree.id
        GROUP BY tree.project_id
    """

    summary: dict[int, dict[str, Any]] = {}
    for row in await db.execute_fetchall(
        counts_sql,
        AWAITING_HUMAN_STATUS_VALUES + IN_FLIGHT_STATUS_VALUES + QUEUED_STATUS_VALUES,
    ):
        d = dict(row)
        summary[int(d["pid"])] = {
            "awaiting_human": int(d["awaiting"] or 0),
            "drafts": int(d["drafts"] or 0),
            "in_flight": int(d["in_flight"] or 0),
            # Kept beside "in flight" rather than folded into it: the queue is
            # worth seeing, it is just a different question (#619).
            "queued": int(d["queued"] or 0),
            "last_activity_at": None,
        }
    for row in await db.execute_fetchall(activity_sql):
        d = dict(row)
        pid = int(d["pid"])
        if pid in summary:
            summary[pid]["last_activity_at"] = d["last_activity_at"]
    return summary


async def count_live_orphan_tasks(db: aiosqlite.Connection) -> int:
    """How many live tasks belong to no epic (#571).

    The epic-shaped views — the dashboard and the epic list — are built on
    ``list_live_epics``, so a task with no parent appears in none of them. It is
    findable in the flat ``/tasks`` list, which is why the claim "invisible
    everywhere" was wrong; what was missing is anything that SAYS how many there
    are. On 10.08.2026 that was 51 rows, 13 of them live.

    Counted here, next to the epic criterion, because two consumers ask
    (the epic list and the projects page) and two copies of the condition
    would drift — the class of defect fixed in #609, #614 and #616.
    """
    rows = await db.execute_fetchall(_LIVE_ORPHANS_COUNT_SQL, FINAL_STATUS_VALUES)
    return int(dict(rows[0])["n"]) if rows else 0


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


async def find_task_id_by_description_marker(
    db: aiosqlite.Connection,
    marker: str,
) -> int | None:
    """Return the oldest non-archived task whose description contains ``marker``.

    Used by the out-of-scope auto-draft flow (#436) to keep draft creation
    idempotent across verdict resubmissions: the marker encodes the source
    task + finding id, so an existing draft is reused instead of duplicated.
    ``instr`` is a literal substring match — no LIKE wildcard escaping needed.
    """
    cur = await db.execute(
        "SELECT id FROM tasks WHERE instr(description, ?) > 0 AND archived=0 "
        "ORDER BY id ASC LIMIT 1",
        (marker,),
    )
    row = await cur.fetchone()
    return int(row[0]) if row else None


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
        "rationale, status, auto_review, task_type, parent_id, priority, position, "
        "status_entered_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))",
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
    """Update arbitrary task columns and always bump ``updated_at``.

    When ``status`` is among the updated fields, ``status_entered_at`` advances
    only if the status actually changes (#416). SQLite evaluates SET
    right-hand sides against the pre-update row, so ``CASE WHEN status != ?``
    compares the stored status to the new one — re-writing the same status
    leaves the clock untouched, and a plain field PATCH never advances it.

    ``completed_at`` is stamped by the same rule, but only on the way into
    ``completed`` (#517). It lives here rather than in the callers because four
    separate paths complete a task; stamping in the primitive is what makes the
    column impossible to forget. Any other field PATCH leaves it alone — a
    completion moment that moved with every later edit would just be a second
    ``updated_at``, which is the defect this task removes.
    """
    sets = [f"{k}=?" for k in fields]
    sets.append("updated_at=datetime('now')")
    values = list(fields.values())
    if "status" in fields:
        sets.append(
            "status_entered_at = CASE WHEN status != ? "
            "THEN datetime('now') ELSE status_entered_at END"
        )
        values.append(fields["status"])
        sets.append(
            "completed_at = CASE WHEN ? = 'completed' AND status != ? "
            "THEN datetime('now') ELSE completed_at END"
        )
        values.extend([fields["status"], fields["status"]])
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
    self_approved: bool = False,
) -> None:
    """Persist a review verdict bound to the CURRENT submission generation.

    The binding is done in SQL (``review_verdict_generation = submission_generation``)
    so the verdict can never be attached to a generation the caller read
    before a concurrent resubmission bumped it. ``findings_json`` replaces
    the stored findings wholesale: findings belong to their verdict, so a
    verdict without findings clears the previous list (#308).
    ``self_approved`` marks verdicts accepted only via the
    ``OPENCLAW_REVIEW_SELF_APPROVE=allow`` solo opt-out (#434); it belongs
    to the verdict, so every new verdict overwrites the flag.
    """
    await db.execute(
        "UPDATE tasks SET review_verdict=?, "
        "review_verdict_generation=submission_generation, "
        "review_findings=?, "
        "review_self_approved=?, "
        "updated_at=datetime('now') WHERE id=?",
        (verdict, findings_json, 1 if self_approved else 0, task_id),
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
        "UPDATE tasks SET status=?, status_entered_at=datetime('now'), "
        "updated_at=datetime('now'), "
        # Same rule as update_task (#517): stamp the completion moment on the
        # way into `completed` and never on any other transition.
        "completed_at = CASE WHEN ? = 'completed' AND status != ? "
        "THEN datetime('now') ELSE completed_at END "
        "WHERE id=? AND status=?",
        (new_status, new_status, new_status, task_id, expected_from),
    )
    return (cur.rowcount or 0) > 0


async def mark_ci_check_started(
    db: aiosqlite.Connection,
    task_id: int,
) -> None:
    """Stamp the CI push time durably (#416).

    Replaces the in-memory ``_ci_pushed_at`` clock so the grace period is
    measured from the real push time and survives a hub restart. Deliberately
    does not touch ``updated_at`` — CI conveyor bookkeeping must not reset the
    stale watchdog.
    """
    await db.execute(
        "UPDATE tasks SET ci_check_started_at=datetime('now') WHERE id=?",
        (task_id,),
    )


async def increment_ci_no_pr_attempts(
    db: aiosqlite.Connection,
    task_id: int,
) -> int:
    """Atomically bump the no-PR retry counter and return the new value (#416).

    The increment is a single ``x = x + 1`` UPDATE so concurrent polls cannot
    lose a count, replacing the in-memory ``_ci_no_pr_retries`` dict.
    """
    await db.execute(
        "UPDATE tasks SET ci_no_pr_attempts = ci_no_pr_attempts + 1 WHERE id=?",
        (task_id,),
    )
    cur = await db.execute("SELECT ci_no_pr_attempts FROM tasks WHERE id=?", (task_id,))
    row = await cur.fetchone()
    return int(row[0]) if row else 0


async def reset_ci_check_state(
    db: aiosqlite.Connection,
    task_id: int,
) -> None:
    """Clear durable CI state when a task leaves the ci_check conveyor (#416)."""
    await db.execute(
        "UPDATE tasks SET ci_check_started_at=NULL, ci_no_pr_attempts=0 WHERE id=?",
        (task_id,),
    )


async def mark_job_missing(
    db: aiosqlite.Connection,
    task_id: int,
) -> None:
    """Stamp when a headless job was first observed missing (#417).

    The ``IS NULL`` guard means the clock is set once and never overwritten, so
    the grace window is measured from the first miss and survives a restart.
    """
    await db.execute(
        "UPDATE tasks SET job_missing_since=datetime('now') "
        "WHERE id=? AND job_missing_since IS NULL",
        (task_id,),
    )


async def clear_job_missing(
    db: aiosqlite.Connection,
    task_id: int,
) -> None:
    """Clear the missing-job clock when the job reappears or is escalated (#417)."""
    await db.execute(
        "UPDATE tasks SET job_missing_since=NULL WHERE id=?",
        (task_id,),
    )


async def list_expired_claims(
    db: aiosqlite.Connection,
    threshold_minutes: int,
) -> list[aiosqlite.Row]:
    """Claimed tasks whose lease passed without a pair start (#417)."""
    return await db.execute_fetchall(
        "SELECT * FROM tasks WHERE archived=0 AND status='claimed' "
        "AND claimed_at IS NOT NULL AND claimed_at < datetime('now', ?)",
        (f"-{threshold_minutes} minutes",),
    )


async def claim_arbiter_dispatch(
    db: aiosqlite.Connection,
    task_id: int,
    generation: int,
) -> bool:
    """Conditionally claim an arbiter dispatch for a submission generation (#421).

    Returns ``True`` only if no active marker already exists for this
    generation — the claim is a single atomic UPDATE that sets state
    ``dispatching`` and the dispatch clock BEFORE any external submit, so a
    repeat poll or a restart finds the marker and does not dispatch again.
    A newer submission generation does not match the old marker, so it opens a
    fresh window. The caller must commit before the external side effect.
    """
    # Positive form so SQL three-valued logic on NULL columns still matches on
    # the first claim: succeed when there is no ACTIVE marker for this exact
    # generation (different generation, or a non-active/NULL state).
    cur = await db.execute(
        "UPDATE tasks SET arbiter_state='dispatching', arbiter_generation=?, "
        "arbiter_job_id=NULL, arbiter_dispatch_at=datetime('now') "
        "WHERE id=? AND ("
        "arbiter_generation IS NULL OR arbiter_generation != ? "
        "OR arbiter_state IS NULL "
        "OR arbiter_state NOT IN ('dispatching', 'running', 'finished'))",
        (generation, task_id, generation),
    )
    return (cur.rowcount or 0) > 0


async def mark_arbiter_running(
    db: aiosqlite.Connection,
    task_id: int,
    arbiter_job_id: str,
) -> None:
    """Record the arbiter job id and move the marker to ``running`` (#421)."""
    await db.execute(
        "UPDATE tasks SET arbiter_state='running', arbiter_job_id=? WHERE id=?",
        (arbiter_job_id, task_id),
    )


async def mark_arbiter_finished(
    db: aiosqlite.Connection,
    task_id: int,
) -> None:
    """Close the arbiter marker once the Hub ends the arbiter phase (#422)."""
    await db.execute(
        "UPDATE tasks SET arbiter_state='finished' WHERE id=?",
        (task_id,),
    )


async def reset_arbiter_state(
    db: aiosqlite.Connection,
    task_id: int,
) -> None:
    """Clear the arbiter marker so a reworked submission starts clean (#422)."""
    await db.execute(
        "UPDATE tasks SET arbiter_state=NULL, arbiter_job_id=NULL, "
        "arbiter_generation=NULL, arbiter_dispatch_at=NULL WHERE id=?",
        (task_id,),
    )


async def list_stale_arbiter_dispatching(
    db: aiosqlite.Connection,
    threshold_minutes: int,
) -> list[aiosqlite.Row]:
    """Tasks stuck mid-dispatch (submit started, no job id) past the grace (#421)."""
    return await db.execute_fetchall(
        "SELECT * FROM tasks WHERE archived=0 AND arbiter_state='dispatching' "
        "AND arbiter_job_id IS NULL AND arbiter_dispatch_at IS NOT NULL "
        "AND arbiter_dispatch_at < datetime('now', ?)",
        (f"-{threshold_minutes} minutes",),
    )


async def list_past_status_deadline(
    db: aiosqlite.Connection,
    status: str,
    threshold_minutes: int,
    *,
    require_job_id: bool = False,
    require_review_job_id: bool = False,
) -> list[aiosqlite.Row]:
    """Tasks that entered ``status`` longer than ``threshold_minutes`` ago (#418).

    Uses the durable ``status_entered_at`` clock (#416), so the deadline
    measures time-in-status and survives a restart. The discriminator flags
    keep headless running/review distinct from their pair/client variants.
    """
    conditions = [
        "archived=0",
        "status=?",
        "status_entered_at IS NOT NULL",
        "status_entered_at < datetime('now', ?)",
    ]
    params: list[Any] = [status, f"-{threshold_minutes} minutes"]
    if require_job_id:
        conditions.append("job_id IS NOT NULL")
    if require_review_job_id:
        conditions.append("review_job_id IS NOT NULL")
    where = " AND ".join(conditions)
    return await db.execute_fetchall(
        f"SELECT * FROM tasks WHERE {where}",  # nosec B608
        tuple(params),
    )


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
        # status_entered_at is stamped in SQL so it shares datetime('now')
        # format with created_at/updated_at (#416).
        f"INSERT INTO tasks ({', '.join(columns)}, status_entered_at) "  # nosec B608
        f"VALUES ({placeholders}, datetime('now'))",
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
        "test_ref, expectation_source, position) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            task_id,
            kwargs["ac_id"],
            kwargs["given"],
            kwargs["when_clause"],
            kwargs["then_clause"],
            kwargs["verifiable_by"],
            kwargs["test_ref"],
            kwargs["expectation_source"],
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
        "test_ref, expectation_source, position) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(task_id, ac_id) DO UPDATE SET "
        "given=excluded.given, when_clause=excluded.when_clause, "
        "then_clause=excluded.then_clause, verifiable_by=excluded.verifiable_by, "
        "test_ref=excluded.test_ref, "
        "expectation_source=excluded.expectation_source",
        (
            task_id,
            kwargs["ac_id"],
            kwargs["given"],
            kwargs["when_clause"],
            kwargs["then_clause"],
            kwargs["verifiable_by"],
            kwargs["test_ref"],
            kwargs["expectation_source"],
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
            "verifiable_by, test_ref, expectation_source, position) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                kwargs["ac_id"],
                kwargs["given"],
                kwargs["when_clause"],
                kwargs["then_clause"],
                kwargs["verifiable_by"],
                kwargs["test_ref"],
                kwargs["expectation_source"],
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
    status: str,
) -> bool:
    """Whether ``task_id`` already has a stale alert for the current window.

    Dedup is scoped two ways so a single lifetime alert can't silence the
    watchdog forever (#393): by ``status`` (an alert raised in ``running``
    must not suppress one in ``ci_check``) and by window. The window boundary
    is the id of the latest update that is NOT itself a stale alert — any real
    activity opens a fresh window. A stale alert counts only when it names
    ``status`` and is newer than that boundary, so re-entering the same status
    after real work alerts again. Until F2 persists a status-entered clock this
    boundary is a heuristic: any non-alert update resets the window, not only a
    status transition. All stale alerts embed ``stale in {status}`` so the
    scope key is parseable from content.
    """
    boundary_rows = await db.execute_fetchall(
        "SELECT COALESCE(MAX(id), 0) AS boundary FROM task_updates "
        "WHERE task_id=? AND NOT (kind='alert' AND content LIKE '%stale%')",
        (task_id,),
    )
    boundary = boundary_rows[0]["boundary"] if boundary_rows else 0
    rows = await db.execute_fetchall(
        "SELECT id FROM task_updates WHERE task_id=? AND kind='alert' "
        "AND content LIKE ? AND id > ? ORDER BY id DESC LIMIT 1",
        (task_id, f"%stale in {status}%", boundary),
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
    *,
    principal_id: int | None = None,
    author_kind: str = "hub",
) -> int:
    """Append an update. ``agent`` is display-only; authorship is the pair
    (principal_id, author_kind) (#559).

    ``author_kind`` defaults to "hub" rather than to the column default
    "legacy": the column default exists to stamp rows written before the field
    existed, and new rows must never claim to be history. The default is
    truthful for the callers that dominate this function — the poller, the
    conveyor and the timers, none of which have a principal by nature. Request
    handlers pass "principal" or "anonymous" explicitly.
    """
    cur = await db.execute(
        "INSERT INTO task_updates (task_id, agent, kind, content, principal_id, "
        "author_kind) VALUES (?, ?, ?, ?, ?, ?)",
        (task_id, agent, kind, content, principal_id, author_kind),
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


# ---------------------------------------------------------------------------
# Task create idempotency
# ---------------------------------------------------------------------------


async def get_task_idempotency_key(
    db: aiosqlite.Connection,
    client_request_id: str,
) -> dict[str, Any] | None:
    rows = await db.execute_fetchall(
        "SELECT client_request_id, task_id, request_hash "
        "FROM task_idempotency_keys WHERE client_request_id = ?",
        (client_request_id,),
    )
    if not rows:
        return None
    return dict(rows[0])


async def insert_task_idempotency_key(
    db: aiosqlite.Connection,
    *,
    client_request_id: str,
    task_id: int,
    request_hash: str,
) -> None:
    await db.execute(
        "INSERT INTO task_idempotency_keys "
        "(client_request_id, task_id, request_hash) VALUES (?, ?, ?)",
        (client_request_id, task_id, request_hash),
    )
