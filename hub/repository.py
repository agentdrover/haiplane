"""Haiplane Hub — Data access layer (repository).

All SQL queries live here. Functions take ``aiosqlite.Connection`` as the
first argument and return raw rows (``aiosqlite.Row``) or primitives.
No Pydantic models, no business logic.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode

import aiosqlite

from hub import config
from hub.db import (
    inserted_id,
    STRUCTURED_TASK_FIELDS,
    ac_to_row_kwargs,
    fetchall,
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
    #
    # Epics are excluded, and the reason is the one ``project_work_summary``
    # already gives for the same exclusion: an epic sits in ``open`` for its
    # entire life as a container, so it is not "approved and nobody started it".
    # #619 put that rule in the counter and forgot it here, which is how
    # notesforllm came to promise 4 and open 5 (#621). The membership rule now
    # lives on BOTH sides of the same door.
    "queued": (
        f"status IN ({_QUEUED_PLACEHOLDERS}) AND task_type != 'epic'",
        QUEUED_STATUS_VALUES,
    ),
}


def task_state_condition(state: str) -> tuple[str, tuple[str, ...]]:
    """(sql, params) for one named state mode, or raise for anything else."""
    try:
        return TASK_STATE_FILTERS[state]
    except KeyError as exc:
        known = ", ".join(sorted(TASK_STATE_FILTERS))
        raise UnknownTaskStateError(f"unknown state {state!r}; known: {known}") from exc


# Membership in a project, written ONCE (#627).
#
# The project sits on the epic and descendants inherit it, so membership is a
# subtree walk. Every list that narrows by project must apply this INSIDE its
# query, before its LIMIT: applied afterwards in Python it discards rows the
# query already spent, and the caller gets an empty list that is
# indistinguishable from "this project has no work". That defect has now been
# fixed three times — #370 in the API, #621 on the tasks page, #627 across the
# dashboard and inbox — which is why the rule is a constant instead of a habit.
PROJECT_SUBTREE_CONDITION = """id IN (
    WITH RECURSIVE subtree(id) AS (
        SELECT id FROM tasks WHERE project_id = ?
        UNION ALL
        SELECT t.id FROM tasks t JOIN subtree s ON t.parent_id = s.id
    )
    SELECT id FROM subtree
)"""


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


async def count_tasks_by_state(
    db: aiosqlite.Connection, project_id: int | None = None
) -> dict[str, int]:
    """How many live tasks sit in each named mode — ONE query, ONE definition.

    Built by folding TASK_STATE_FILTERS into a single SELECT of SUM(CASE ...),
    so a counter cannot drift from the list its link opens: both read the same
    condition string. Spelling the counts out with "similar" WHERE clauses is
    exactly how #571, #619 and #621 each shipped a number that disagreed with
    the page behind it — three times is enough to make the shared definition
    structural rather than a habit (#630).
    """
    parts: list[str] = []
    params: list[Any] = []
    for name in ("live", "awaiting", "inflight", "queued"):
        sql, values = task_state_condition(name)
        parts.append(f"SUM(CASE WHEN {sql} THEN 1 ELSE 0 END) AS {name}")
        params.extend(values)
    where = ["archived=0"]
    if project_id is not None:
        where.append(PROJECT_SUBTREE_CONDITION)
        params.append(project_id)
    rows = await fetchall(
        db,
        f"SELECT {', '.join(parts)} FROM tasks WHERE {' AND '.join(where)}",  # nosec B608
        tuple(params),
    )
    row = dict(rows[0]) if rows else {}
    return {
        name: int(row.get(name) or 0)
        for name in ("live", "awaiting", "inflight", "queued")
    }


async def task_ids_with_update_marker(
    db: aiosqlite.Connection, task_ids: list[int], marker: str
) -> set[int]:
    """Which of these tasks carry an update containing ``marker`` — ONE query.

    Written for #629: the caller used to ask this per task, inside a loop over
    a rendered list, so drawing one badge on 100 rows cost 100 scans of
    task_updates. The question is the same; asking it once is the whole change.
    """
    if not task_ids:
        return set()
    placeholders = ",".join("?" for _ in task_ids)
    rows = await fetchall(
        db,
        f"SELECT DISTINCT task_id FROM task_updates "  # nosec B608 - placeholders only
        f"WHERE task_id IN ({placeholders}) AND instr(content, ?) > 0",
        (*task_ids, marker),
    )
    return {int(dict(r)["task_id"]) for r in rows}


def inbox_query_string(
    *,
    human_owner: str | None = None,
    claimed_by: str | None = None,
    mine: str | None = None,
    project: str | None = None,
) -> str:
    """The URL the inbox fragment re-fetches ITSELF with, every 30 seconds.

    ``project`` belongs here for the same reason the person filters do: the
    fragment overwrites its container's hx-get with this string, so anything
    missing is dropped on the first self-refresh. Measured on production
    (#628): a scoped inbox kept ``mine`` and lost ``project``, silently widening
    back to every project half a minute after it was narrowed.
    """
    params: dict[str, str] = {}
    if mine:
        params["mine"] = mine
    if human_owner:
        params["human_owner"] = human_owner
    if claimed_by:
        params["claimed_by"] = claimed_by
    if project:
        params["project"] = project
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
    rows = await fetchall(
        db,
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
        conditions.append(PROJECT_SUBTREE_CONDITION)
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
    return await fetchall(
        db,
        f"SELECT * FROM tasks WHERE {where} ORDER BY {order} LIMIT ?",  # nosec B608
        tuple(params),
    )


async def list_tasks_by_statuses(
    db: aiosqlite.Connection,
    statuses: list[str],
    *,
    limit: int = 20,
    include_archived: bool = False,
    project_id: int | None = None,
) -> list[aiosqlite.Row]:
    placeholders = ",".join("?" for _ in statuses)
    archived_sql = "" if include_archived else " AND archived=0"
    # #627: optional, and INSIDE the query — see PROJECT_SUBTREE_CONDITION for
    # why narrowing a limited list afterwards is the defect, not the fix.
    project_sql = f" AND {PROJECT_SUBTREE_CONDITION}" if project_id is not None else ""
    project_params = () if project_id is None else (project_id,)
    return await fetchall(
        db,
        f"SELECT * FROM tasks WHERE status IN ({placeholders}){archived_sql}"  # nosec B608
        f"{project_sql} ORDER BY id DESC LIMIT ?",
        (*statuses, *project_params, limit),
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
    project_id: int | None = None,
) -> list[aiosqlite.Row]:
    if order_by not in ALLOWED_TASKS_ORDER_BY:
        raise ValueError(f"Unsupported order_by clause: {order_by!r}")
    conditions = ["status=?"]
    params: list[Any] = [status]
    if not include_archived:
        conditions.append("archived=0")
    if project_id is not None:
        # #627: inside the query, before the LIMIT — see
        # PROJECT_SUBTREE_CONDITION.
        conditions.append(PROJECT_SUBTREE_CONDITION)
        params.append(project_id)
    _append_person_filters(
        conditions,
        params,
        human_owner=human_owner,
        claimed_by=claimed_by,
        mine=mine,
    )
    where = " AND ".join(conditions)
    return await fetchall(
        db,
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
    return await fetchall(
        db,
        f"SELECT id, title, status, branch FROM tasks "  # nosec B608
        f"WHERE archived=0 AND id != ? AND status IN ({placeholders}) "
        "AND branch IS NOT NULL AND TRIM(branch) != '' ORDER BY id",
        (exclude_task_id, *statuses),
    )


async def list_running_dispatchable(
    db: aiosqlite.Connection,
) -> list[aiosqlite.Row]:
    return await fetchall(
        db,
        "SELECT * FROM tasks WHERE archived=0 AND status IN ('running', 'fix_requested') "
        "AND job_id IS NOT NULL",
    )


async def list_review_tasks(
    db: aiosqlite.Connection,
) -> list[aiosqlite.Row]:
    return await fetchall(
        db,
        "SELECT * FROM tasks WHERE archived=0 AND status='review' "
        "AND review_job_id IS NOT NULL",
    )


async def list_ci_check_tasks(
    db: aiosqlite.Connection,
) -> list[aiosqlite.Row]:
    return await fetchall(
        db,
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
        # #957: a declared, still-current wait is not staleness — the task
        # said what it waits for and until when. Past the deadline it is
        # back on the list: a wait never buys silence forever.
        "(waiting_for='' OR waiting_until IS NULL OR waiting_until=''"
        " OR waiting_until < datetime('now'))",
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
    return await fetchall(
        db,
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
    default_branch: str = config.PAIR_BASE_BRANCH,
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
    rows = await fetchall(db, "SELECT * FROM projects WHERE id=?", (project_id,))
    return rows[0] if rows else None


async def get_project_by_slug(
    db: aiosqlite.Connection, slug: str
) -> aiosqlite.Row | None:
    rows = await fetchall(db, "SELECT * FROM projects WHERE slug=?", (slug,))
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


async def mark_merges_released(
    db: aiosqlite.Connection,
    *,
    project_id: int | None,
    release_pr: int,
    release_sha: str,
) -> int:
    """Stamp every unreleased merge of this project with the release that took it.

    Called at release-merge time (#950). A release carries the base branch
    whole (#812), so the set is exact: everything merged before this moment
    and not yet released went out with this release. The stamp survives what
    git history does not — squashes and recreated branches both cut ancestry,
    and this row is then the only fact tying a merge to a deploy.
    """
    cur = await db.execute(
        "UPDATE pipeline_merges SET released_pr = ?, released_sha = ? "
        "WHERE (released_sha IS NULL OR released_sha = '') "
        "AND ((? IS NULL AND project_id IS NULL) OR project_id = ?)",
        (int(release_pr), release_sha.strip(), project_id, project_id),
    )
    await db.commit()
    return int(cur.rowcount or 0)


async def release_fact_for_task(
    db: aiosqlite.Connection, task_id: int
) -> dict[str, Any] | None:
    """The recorded release that carried this task's merge, or None (#950)."""
    rows = await fetchall(
        db,
        "SELECT released_pr, released_sha FROM pipeline_merges "
        "WHERE task_id = ? AND released_sha IS NOT NULL AND released_sha != '' "
        "ORDER BY id DESC LIMIT 1",
        (task_id,),
    )
    return dict(rows[0]) if rows else None


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
    rows = await fetchall(
        db,
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
    rows = await fetchall(
        db,
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
        return await fetchall(
            db, "SELECT * FROM base_branch_drift ORDER BY detected_at DESC, id DESC"
        )
    return await fetchall(
        db,
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
    return await fetchall(
        db,
        f"SELECT * FROM projects {where} ORDER BY slug ASC",  # nosec B608
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
    rows = await fetchall(
        db,
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
        rows = await fetchall(
            db,
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
    rows = await fetchall(
        db, "SELECT COALESCE(MAX(version), 0) AS v FROM skills WHERE name=?", (name,)
    )
    version = (rows[0]["v"] or 0) + 1
    cur = await db.execute(
        "INSERT INTO skills (name, kind, version, content, tags, project_id, "
        "status, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (name, kind, version, content, tags, project_id, status, created_by),
    )
    return cur.lastrowid, version  # type: ignore[return-value]


async def get_active_skill(db: aiosqlite.Connection, name: str) -> aiosqlite.Row | None:
    rows = await fetchall(
        db,
        "SELECT * FROM skills WHERE name=? AND status='active' "
        "ORDER BY version DESC LIMIT 1",
        (name,),
    )
    return rows[0] if rows else None


async def list_skills(db: aiosqlite.Connection) -> list[aiosqlite.Row]:
    """Latest version per name (active preferred, else newest draft)."""
    return await fetchall(
        db,
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
        """,
    )


async def list_skill_versions(
    db: aiosqlite.Connection, name: str
) -> list[aiosqlite.Row]:
    return await fetchall(
        db, "SELECT * FROM skills WHERE name=? ORDER BY version DESC", (name,)
    )


async def get_skill_version(
    db: aiosqlite.Connection, name: str, version: int
) -> aiosqlite.Row | None:
    rows = await fetchall(
        db, "SELECT * FROM skills WHERE name=? AND version=?", (name, version)
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
    profile: str = "",
) -> int:
    cur = await db.execute(
        "INSERT INTO machine_reviews (task_id, submission_generation, "
        "harness_skill, harness_version, agent_count, tokens_spent, "
        "duration_ms, orchestrator, model, raw_count, findings_confirmed, "
        "findings_rejected, submitted_by, incomplete, unresolved, "
        "lost_dimensions, profile) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
            profile,
        ),
    )
    return cur.lastrowid  # type: ignore[return-value]


async def set_machine_review_provider_tokens(
    db: aiosqlite.Connection, task_id: int, generation: int, tokens: int
) -> None:
    """Record what the provider billed for this generation's review (#828).

    Written by the sweep that already fetches usage for the mismatch check.
    Only the latest report of the generation is stamped: a resubmission gets
    its own dispatch and its own run.
    """
    await db.execute(
        "UPDATE machine_reviews SET provider_tokens = ? WHERE id = ("
        "SELECT id FROM machine_reviews WHERE task_id = ? "
        "AND submission_generation = ? ORDER BY id DESC LIMIT 1)",
        (tokens, task_id, generation),
    )


async def get_latest_machine_review(
    db: aiosqlite.Connection, task_id: int
) -> aiosqlite.Row | None:
    rows = await fetchall(
        db,
        "SELECT * FROM machine_reviews WHERE task_id=? ORDER BY id DESC LIMIT 1",
        (task_id,),
    )
    return rows[0] if rows else None


async def machine_reviews_of_generation(
    db: aiosqlite.Connection, task_id: int, generation: int
) -> list[aiosqlite.Row]:
    """Every report written about one submission, oldest first (#880).

    Plural because the ladder (#879) can produce two for one generation.
    """
    return list(
        await fetchall(
            db,
            "SELECT * FROM machine_reviews WHERE task_id=? "
            "AND submission_generation=? ORDER BY id",
            (task_id, generation),
        )
    )


async def list_machine_reviews(
    db: aiosqlite.Connection, task_id: int
) -> list[aiosqlite.Row]:
    """Every review RUN this task has cost, oldest first (#893).

    The unit that tracks spend is the run, not the token: measured across
    eleven billed runs, none came in under 777k tokens and the size of the
    diff explained none of the variance. A task reviewed five times cost five
    times the entry price, and that is only visible when the runs are counted
    rather than summed.
    """
    return list(
        await fetchall(
            db,
            "SELECT id, submission_generation, profile, model, agent_count, "
            "tokens_spent, provider_tokens, incomplete, created_at "
            "FROM machine_reviews WHERE task_id=? ORDER BY id",
            (task_id,),
        )
    )


# --- Finding dispositions (#876) -------------------------------------------


async def upsert_finding_disposition(
    db: aiosqlite.Connection,
    *,
    review_id: int,
    task_id: int,
    submission_generation: int,
    finding_index: int,
    finding_title: str,
    disposition: str,
    note: str,
    decided_by: str,
) -> None:
    """Record what one confirmed finding turned out to be.

    Upsert on (review_id, finding_index): a gate revisiting its own judgement
    corrects it instead of leaving two contradictory rows for the metrics to
    average.
    """
    await db.execute(
        "INSERT INTO finding_dispositions (review_id, task_id, "
        "submission_generation, finding_index, finding_title, disposition, "
        "note, decided_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(review_id, finding_index) DO UPDATE SET "
        "disposition=excluded.disposition, note=excluded.note, "
        "decided_by=excluded.decided_by, decided_at=datetime('now')",
        (
            review_id,
            task_id,
            submission_generation,
            finding_index,
            finding_title,
            disposition,
            note,
            decided_by,
        ),
    )


async def list_finding_dispositions(
    db: aiosqlite.Connection, review_id: int
) -> list[aiosqlite.Row]:
    return await fetchall(
        db,
        "SELECT * FROM finding_dispositions WHERE review_id=? "
        "ORDER BY finding_index ASC",
        (review_id,),
    )


# --- Category checks (#878) ------------------------------------------------


async def upsert_category_check(
    db: aiosqlite.Connection,
    *,
    category: str,
    check_ref: str,
    note: str = "",
    recorded_by: str = "",
) -> None:
    """Record the deterministic check that covers a finding category."""
    await db.execute(
        "INSERT INTO category_checks (category, check_ref, note, recorded_by) "
        "VALUES (?, ?, ?, ?) ON CONFLICT(category) DO UPDATE SET "
        "check_ref=excluded.check_ref, note=excluded.note, "
        "recorded_by=excluded.recorded_by, recorded_at=datetime('now')",
        (category, check_ref, note, recorded_by),
    )


async def list_category_checks(db: aiosqlite.Connection) -> list[aiosqlite.Row]:
    return await fetchall(db, "SELECT * FROM category_checks ORDER BY category ASC")


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
    return await fetchall(
        db,
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
    checks: str = "{}",
) -> None:
    """Store what a CI run reported for one commit (idempotent per commit).

    Re-running CI on the same commit updates the row rather than adding a
    second opinion — the same rule the merge ledger follows (#605).
    """
    await db.execute(
        "INSERT INTO ci_run_reports (task_id, head_sha, ac_results, "
        "validation_status, validation_log, reason, reported_by, checks, "
        "reported_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now')) "
        "ON CONFLICT(task_id, head_sha) DO UPDATE SET "
        "ac_results=excluded.ac_results, "
        "validation_status=excluded.validation_status, "
        "validation_log=excluded.validation_log, "
        "reason=excluded.reason, reported_by=excluded.reported_by, "
        "checks=excluded.checks, "
        "reported_at=excluded.reported_at",
        (
            task_id,
            head_sha,
            ac_results,
            validation_status,
            validation_log,
            reason,
            reported_by,
            checks,
        ),
    )


async def get_ci_run_report(
    db: aiosqlite.Connection, task_id: int, head_sha: str
) -> aiosqlite.Row | None:
    """The report for one commit, or None when that commit was never reported."""
    rows = await fetchall(
        db,
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
    rows = await fetchall(
        db,
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
    return await fetchall(
        db,
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
        # #957: same rule as list_stale_running — see the comment there.
        "(waiting_for='' OR waiting_until IS NULL OR waiting_until=''"
        " OR waiting_until < datetime('now'))",
    ]
    params: list[Any] = [status, f"-{threshold_minutes} minutes"]
    if require_null_review_job:
        conditions.append("review_job_id IS NULL")
    where = " AND ".join(conditions)
    return await fetchall(
        db,
        f"SELECT * FROM tasks WHERE {where}",  # nosec B608
        tuple(params),
    )


async def list_outcome_debt(db: aiosqlite.Connection) -> list[aiosqlite.Row]:
    """Completed tasks that stated an outcome nobody has come back to.

    DoR refuses a task without ``outcome_metric``, and nothing has ever read it
    since. This is the read that makes the debt visible.

    Deliberately not filtered by ``outcome_deadline``: that column is free text
    (models.py, max_length 64) and real values are event descriptions - "within
    the first 30 captures", "on the next reviewed submission" - not dates.
    Filtering on it would hide tasks behind a value that cannot be parsed.

    Oldest first: the ones that have waited longest are the ones whose answer is
    most likely already knowable.
    """
    return await fetchall(
        db,
        "SELECT id, title, task_type, outcome_metric, outcome_indicator, "
        "outcome_deadline, outcome_revisit_condition, completed_at, updated_at "
        "FROM tasks "
        "WHERE archived=0 AND status='completed' AND TRIM(outcome_metric) != '' "
        "ORDER BY COALESCE(completed_at, updated_at) ASC",
    )


async def record_outcome_answer(
    db: aiosqlite.Connection,
    *,
    task_id: int,
    verdict: str,
    measured_value: str,
    note: str = "",
    answered_by: str = "",
    hypothesis_snapshot: str | None = None,
) -> int:
    """Append one answer to a task's outcome (#819).

    Append-only on purpose: an outcome_deadline routinely names more than one
    moment, and an update-in-place would erase the evidence that anyone came
    back a second time.

    ``hypothesis_snapshot`` is the ``outcome_metric`` as it stood when the
    answer was written (#576). Empty/NULL means the row predates the column
    and must still read as an answer, not as a revision.
    """
    cur = await db.execute(
        "INSERT INTO outcome_answers "
        "(task_id, verdict, measured_value, note, answered_by, "
        "hypothesis_snapshot) VALUES (?, ?, ?, ?, ?, ?)",
        (task_id, verdict, measured_value, note, answered_by, hypothesis_snapshot),
    )
    await db.commit()
    return int(cur.lastrowid or 0)


async def list_outcome_answers(db: aiosqlite.Connection) -> list[aiosqlite.Row]:
    """Every recorded answer, oldest first, for grouping by task (#819)."""
    return await fetchall(
        db,
        "SELECT id, task_id, verdict, measured_value, note, answered_by, "
        "answered_at, hypothesis_snapshot FROM outcome_answers "
        "ORDER BY answered_at ASC, id ASC",
    )


async def list_outcome_answers_for_task(
    db: aiosqlite.Connection, task_id: int
) -> list[aiosqlite.Row]:
    """Answers for one task, oldest first (#576)."""
    return await fetchall(
        db,
        "SELECT id, task_id, verdict, measured_value, note, answered_by, "
        "answered_at, hypothesis_snapshot FROM outcome_answers "
        "WHERE task_id=? ORDER BY answered_at ASC, id ASC",
        (task_id,),
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
    return await fetchall(db, _LIVE_EPICS_SQL, FINAL_STATUS_VALUES * 2)


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
    return await fetchall(db, _DONE_EPICS_SQL, FINAL_STATUS_VALUES * 2)


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
    for row in await fetchall(
        db,
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
    for row in await fetchall(db, activity_sql):
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
    rows = await fetchall(db, _LIVE_ORPHANS_COUNT_SQL, FINAL_STATUS_VALUES)
    return int(dict(rows[0])["n"]) if rows else 0


async def list_agent_tasks(
    db: aiosqlite.Connection,
    status: str | None = None,
    *,
    limit: int = 50,
) -> list[aiosqlite.Row]:
    if status:
        return await fetchall(
            db,
            "SELECT * FROM tasks WHERE archived=0 AND source='agent' AND status=? "
            "ORDER BY id DESC LIMIT ?",
            (status, limit),
        )
    return await fetchall(
        db,
        "SELECT * FROM tasks WHERE archived=0 AND source='agent' ORDER BY id DESC LIMIT ?",
        (limit,),
    )


async def get_siblings(
    db: aiosqlite.Connection,
    parent_id: int,
    exclude_id: int,
) -> list[aiosqlite.Row]:
    return await fetchall(
        db,
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
    ``HAIPLANE_REVIEW_SELF_APPROVE=allow`` solo opt-out (#434); it belongs
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


# ---------------------------------------------------------------------------
# Cross-model review dispatches (#757)
# ---------------------------------------------------------------------------


async def create_review_dispatch(
    db: aiosqlite.Connection,
    *,
    task_id: int,
    submission_generation: int,
    agent_id: str,
    run_id: str,
    model: str,
    profile: str = "",
) -> int:
    cur = await db.execute(
        "INSERT INTO review_dispatches "
        "(task_id, submission_generation, agent_id, run_id, model, profile) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (task_id, submission_generation, agent_id, run_id, model, profile),
    )
    return inserted_id(cur)


async def list_active_review_dispatches(
    db: aiosqlite.Connection,
) -> list[aiosqlite.Row]:
    return list(
        await fetchall(
            db, "SELECT * FROM review_dispatches WHERE status='active' ORDER BY id ASC"
        )
    )


async def get_settled_review_dispatch(
    db: aiosqlite.Connection, task_id: int, generation: int
) -> aiosqlite.Row | None:
    """The latest DONE dispatch of this submission generation (#769).

    Only 'done' counts: the sweep sets it after the report of the same
    generation arrived — a failed or still-active dispatch proves nothing
    about the emptiness of a review.
    """
    rows = await fetchall(
        db,
        "SELECT * FROM review_dispatches "
        "WHERE task_id=? AND submission_generation=? AND status='done' "
        "ORDER BY id DESC LIMIT 1",
        (task_id, generation),
    )
    rows = list(rows)
    return rows[0] if rows else None


async def get_review_dispatch_for_generation(
    db: aiosqlite.Connection, task_id: int, generation: int
) -> aiosqlite.Row | None:
    """The latest dispatch of this generation, whatever its status (#807).

    Deliberately status-agnostic, unlike get_settled_review_dispatch: the
    profile is decided when the run is launched, and the report normally
    arrives while the dispatch is still 'active'.
    """
    rows = list(
        await fetchall(
            db,
            "SELECT * FROM review_dispatches "
            "WHERE task_id=? AND submission_generation=? "
            "ORDER BY id DESC LIMIT 1",
            (task_id, generation),
        )
    )
    return rows[0] if rows else None


async def record_submission(
    db: aiosqlite.Connection,
    *,
    task_id: int,
    generation: int,
    sha: str,
    base_branch: str,
) -> None:
    """Remember which commit a submission pinned (#880)."""
    await db.execute(
        "INSERT INTO submissions (task_id, generation, sha, base_branch) "
        "VALUES (?, ?, ?, ?) ON CONFLICT(task_id, generation) DO UPDATE SET "
        "sha=excluded.sha, base_branch=excluded.base_branch, "
        "submitted_at=datetime('now')",
        (task_id, generation, sha, base_branch),
    )


async def previous_submission(
    db: aiosqlite.Connection, task_id: int, generation: int
) -> aiosqlite.Row | None:
    """The newest submission BEFORE this generation, or None (#880).

    "Newest below", not "generation - 1": the ledger starts mid-history for
    tasks that were already submitted before it existed, and skipping straight
    to the previous number would silently claim a delta against a commit
    nobody recorded.
    """
    rows = await fetchall(
        db,
        "SELECT * FROM submissions WHERE task_id=? AND generation<? "
        "ORDER BY generation DESC LIMIT 1",
        (task_id, generation),
    )
    return rows[0] if rows else None


async def count_review_dispatches(
    db: aiosqlite.Connection, task_id: int, generation: int
) -> int:
    """How many cloud runs this submission has already bought (#879).

    Counted from the rows, not from a flag: the ladder's ceiling has to be the
    same fact the bill is, and a flag would drift from it.
    """
    rows = await fetchall(
        db,
        "SELECT COUNT(*) AS n FROM review_dispatches "
        "WHERE task_id=? AND submission_generation=?",
        (task_id, generation),
    )
    return int(dict(rows[0])["n"]) if rows else 0


async def set_review_dispatch_status(
    db: aiosqlite.Connection, dispatch_id: int, status: str
) -> None:
    await db.execute(
        "UPDATE review_dispatches SET status=? WHERE id=?", (status, dispatch_id)
    )


# ---------------------------------------------------------------------------
# Task dependencies (#483, epic #478)
# ---------------------------------------------------------------------------


class DependencyCycleError(ValueError):
    """Raised when an edge would close a cycle in the dependency graph (#483).

    The message names the path that would close it. "Cycle detected" tells the
    caller that something is wrong; naming A → B → C → A tells them which edge
    to reconsider, and that is the difference between an error somebody acts
    on and one they work around.
    """


class SelfDependencyError(ValueError):
    """Raised for an edge from a task to itself (#483).

    The schema also refuses it (#482), but an IntegrityError from SQLite says
    "CHECK constraint failed" and leaves the reader to work out which one.
    """


async def add_task_dependency(
    db: aiosqlite.Connection, task_id: int, depends_on_task_id: int
) -> bool:
    """Record that ``task_id`` waits for ``depends_on_task_id`` (#483).

    Returns True when an edge was created, False when it already existed —
    adding the same edge twice is not an error, it is a no-op, because the
    caller's intent ("this must wait for that") is already satisfied.

    Deliberately does NOT commit: the cycle walk and the insert belong to the
    same transaction as the caller's other writes, so two concurrent callers
    cannot each pass the check separately and close a cycle between them.
    """
    if task_id == depends_on_task_id:
        raise SelfDependencyError(f"задача #{task_id} не может зависеть от самой себя")
    path = await _dependency_path(db, depends_on_task_id, task_id)
    if path is not None:
        chain = " → ".join(f"#{t}" for t in [task_id, *path])
        raise DependencyCycleError(
            f"ребро #{task_id} → #{depends_on_task_id} замкнуло бы цикл: {chain}"
        )
    cur = await db.execute(
        "INSERT OR IGNORE INTO task_dependencies (task_id, depends_on_task_id) "
        "VALUES (?, ?)",
        (task_id, depends_on_task_id),
    )
    return cur.rowcount > 0


async def remove_task_dependency(
    db: aiosqlite.Connection, task_id: int, depends_on_task_id: int
) -> bool:
    """Drop the edge; True when one was there. Removing a missing edge is a
    no-op for the same reason adding a present one is (#483)."""
    cur = await db.execute(
        "DELETE FROM task_dependencies WHERE task_id = ? AND depends_on_task_id = ?",
        (task_id, depends_on_task_id),
    )
    return cur.rowcount > 0


async def list_task_dependencies(
    db: aiosqlite.Connection, task_id: int
) -> dict[str, list[dict[str, Any]]]:
    """Both sides of the task's edges, with the other end's status (#483).

    ``blocked_by`` is what this task waits for, ``unblocks`` is what waits for
    it. Both are needed and neither is derivable from the other cheaply: the
    first is read when work is about to start, the second when it finishes.
    Statuses travel along because every consumer would otherwise ask for them
    immediately — "blocked by #818" means nothing without knowing where #818
    stands.
    """
    # #485: delivery travels beside the status. A closed task whose PR is not
    # merged blocks exactly as much as an open one — that is what #830 learned
    # the expensive way, and a reader given only the status would repeat it.
    blocked_by = await fetchall(
        db,
        "SELECT t.id AS task_id, t.title, t.status, t.pr_number, "
        "(SELECT COUNT(*) FROM pipeline_merges m WHERE m.task_id = t.id) AS merges "
        "FROM task_dependencies d JOIN tasks t ON t.id = d.depends_on_task_id "
        "WHERE d.task_id = ? ORDER BY t.id",
        (task_id,),
    )
    unblocks = await fetchall(
        db,
        "SELECT t.id AS task_id, t.title, t.status "
        "FROM task_dependencies d JOIN tasks t ON t.id = d.task_id "
        "WHERE d.depends_on_task_id = ? ORDER BY t.id",
        (task_id,),
    )
    return {
        "blocked_by": [_blocker_entry(dict(r)) for r in blocked_by],
        "unblocks": [dict(r) for r in unblocks],
    }


def _blocker_entry(row: dict[str, Any]) -> dict[str, Any]:
    """One blocker with its delivery state (#485).

    ``delivered`` answers "is the code in the base branch", which is the
    question the reader actually has; ``reason`` says why not, because "PR
    not merged" and "no PR declared" call for different next moves.
    """
    delivered = bool(row.pop("merges", 0))
    pr_number = row.pop("pr_number", None)
    reason = ""
    if not delivered:
        reason = f"PR #{pr_number} не смержен гейтом" if pr_number else "PR не заявлен"
    return {**row, "delivered": delivered, "reason": reason}


async def undelivered_blockers(
    db: aiosqlite.Connection, task_id: int
) -> list[dict[str, Any]]:
    """Blockers of ``task_id`` whose CODE has not been delivered (#484).

    Readiness is judged by delivery, not by status — the owner's decision of
    21.08.2026, taken after five cases in which the blocker was undelivered
    code rather than an unfinished task. #830 stopped after pair-start
    because the module it needed sat in an unmerged PR while its task was in
    review; and even ``completed`` would not have saved it, since the window
    between a done report and the gate's merge is real and a PR can still go
    back for rework.

    Delivery is read from ``pipeline_merges`` (#534) — merges the hub
    performed itself, keyed by task and carrying the merge SHA. A merge made
    outside the gate leaves no row here, so its task reads as undelivered:
    for the hub's own project that is a property (manual merges into the base
    branch are forbidden and the drift guard catches them), and the gate this
    feeds is advisory anyway.

    Each entry names WHY it is not delivered — "PR not declared" and "PR not
    merged" are different situations, and collapsing them would leave the
    reader unable to act on either.
    """
    rows = await fetchall(
        db,
        "SELECT t.id AS task_id, t.title, t.status, t.pr_number, "
        "(SELECT COUNT(*) FROM pipeline_merges m WHERE m.task_id = t.id) AS merges "
        "FROM task_dependencies d JOIN tasks t ON t.id = d.depends_on_task_id "
        "WHERE d.task_id = ? ORDER BY t.id",
        (task_id,),
    )
    blockers: list[dict[str, Any]] = []
    for row in rows:
        if row["merges"]:
            continue
        pr_number = row["pr_number"]
        reason = f"PR #{pr_number} не смержен гейтом" if pr_number else "PR не заявлен"
        blockers.append(
            {
                "task_id": row["task_id"],
                "title": row["title"],
                "status": row["status"],
                "pr_number": pr_number,
                "reason": reason,
            }
        )
    return blockers


async def _dependency_path(
    db: aiosqlite.Connection, start: int, target: int
) -> list[int] | None:
    """The chain of dependencies from ``start`` to ``target``, or None (#483).

    Walks "what does this wait for" breadth-first, carrying the path so the
    error can name it. The visited set is what keeps a DIAMOND from reading as
    a cycle: two tasks may legitimately wait for the same third one, and a
    walk that only tracked depth would meet it twice and call that a loop.
    """
    if start == target:
        return [start]
    seen: set[int] = {start}
    queue: list[tuple[int, list[int]]] = [(start, [start])]
    while queue:
        node, path = queue.pop(0)
        rows = await fetchall(
            db,
            "SELECT depends_on_task_id FROM task_dependencies WHERE task_id = ?",
            (node,),
        )
        for row in rows:
            nxt = row["depends_on_task_id"]
            if nxt == target:
                return [*path, nxt]
            if nxt not in seen:
                seen.add(nxt)
                queue.append((nxt, [*path, nxt]))
    return None


# ---------------------------------------------------------------------------
# Autopilot digests (#739)
# ---------------------------------------------------------------------------


async def create_digest(
    db: aiosqlite.Connection,
    *,
    project_id: int,
    digest_date: str,
    payload: str,
) -> int | None:
    """Insert a digest row; None when one already exists for that day."""
    try:
        cur = await db.execute(
            "INSERT INTO autopilot_digests (project_id, digest_date, payload) "
            "VALUES (?, ?, ?)",
            (project_id, digest_date, payload),
        )
    except aiosqlite.IntegrityError:
        return None
    return cur.lastrowid


async def get_digest(db: aiosqlite.Connection, digest_id: int) -> aiosqlite.Row | None:
    rows = await fetchall(
        db,
        "SELECT d.*, p.slug AS project_slug FROM autopilot_digests d "
        "JOIN projects p ON p.id = d.project_id WHERE d.id=?",
        (digest_id,),
    )
    return rows[0] if rows else None


async def list_digests(
    db: aiosqlite.Connection, *, limit: int = 30
) -> list[aiosqlite.Row]:
    return list(
        await fetchall(
            db,
            "SELECT d.*, p.slug AS project_slug FROM autopilot_digests d "
            "JOIN projects p ON p.id = d.project_id "
            "ORDER BY d.digest_date DESC, d.id DESC LIMIT ?",
            (limit,),
        )
    )


async def update_digest_payload(
    db: aiosqlite.Connection, digest_id: int, payload: str
) -> None:
    await db.execute(
        "UPDATE autopilot_digests SET payload=? WHERE id=?", (payload, digest_id)
    )


async def list_expired_claims(
    db: aiosqlite.Connection,
    threshold_minutes: int,
) -> list[aiosqlite.Row]:
    """Claimed tasks whose lease passed without a pair start (#417)."""
    return await fetchall(
        db,
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
    return await fetchall(
        db,
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
    return await fetchall(
        db,
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


class DefectPassportError(ValueError):
    """Raised when a defect passport write would store an unusable fact (#909).

    Two cases: a ``found_in`` outside the stage list, and a causal link that
    does not resolve. Both are refused here rather than upstream because every
    surface (REST, MCP, CLI) writes through this function — a check in one of
    them is a check the other two do not have.
    """


async def set_defect_passport(
    db: aiosqlite.Connection,
    task_id: int,
    *,
    found_in: str | None = None,
    caused_by_task_id: int | None = None,
    detected_at: str | None = None,
    resolved_at: str | None = None,
    clear_caused_by: bool = False,
) -> dict[str, Any]:
    """Write the defect passport for one task and return the applied columns.

    PATCH semantics: ``None`` means "leave as is", so a caller that only knows
    the stage does not have to restate the rest. Dropping an attribution is a
    deliberate act with its own flag (``clear_caused_by``) instead of an
    overloaded ``None`` — otherwise "I don't know who broke it" and "nobody
    broke it, remove the link" would be the same call.

    Raises ``DefectPassportError`` when the stage is not a known one or the
    causal link does not resolve; nothing is written in that case.
    """
    from hub.db import validate_caused_by
    from hub.models import DefectFoundIn

    updates: dict[str, Any] = {}

    if found_in is not None:
        try:
            updates["found_in"] = DefectFoundIn(found_in).value
        except ValueError as exc:
            allowed = ", ".join(stage.value for stage in DefectFoundIn)
            raise DefectPassportError(
                f"unknown found_in {found_in!r}; allowed: {allowed}"
            ) from exc

    if clear_caused_by:
        updates["caused_by_task_id"] = None
    elif caused_by_task_id is not None:
        problem = await validate_caused_by(db, task_id, caused_by_task_id)
        if problem:
            raise DefectPassportError(problem)
        updates["caused_by_task_id"] = caused_by_task_id

    if detected_at is not None:
        updates["detected_at"] = detected_at
    if resolved_at is not None:
        updates["resolved_at"] = resolved_at

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
    return await fetchall(
        db,
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
        rows = await fetchall(
            db,
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
    rows = await fetchall(
        db,
        "SELECT 1 FROM acceptance_criteria WHERE task_id=? AND ac_id=?",
        (task_id, ac.id),
    )
    existed = bool(rows)

    pos_rows = await fetchall(
        db,
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
        rows = await fetchall(
            db,
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
    rows = await fetchall(
        db,
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


# #948: сдача на ревью не пишет собственного события, и её единственный след —
# апдейт вот такой формы. Читателей у него уже двое: метрики человеческих гейтов
# (SQL LIKE) и правило свежести блокера (скан по истории), — а форму задавал
# f-строка в одном из них. Константа здесь, в слое данных, потому что оба слоя
# сюда уже смотрят и цикла импортов не будет.
SUBMISSION_UPDATE_PREFIX = "Submitted for review (submission #"


async def get_task_updates(
    db: aiosqlite.Connection,
    task_id: int,
) -> list[aiosqlite.Row]:
    return await fetchall(
        db,
        "SELECT * FROM task_updates WHERE task_id=? ORDER BY id ASC",
        (task_id,),
    )


async def get_task_update_by_id(
    db: aiosqlite.Connection,
    update_id: int,
) -> aiosqlite.Row | None:
    rows = await fetchall(
        db,
        "SELECT * FROM task_updates WHERE id=?",
        (update_id,),
    )
    return rows[0] if rows else None


async def has_done_updates(
    db: aiosqlite.Connection,
    task_id: int,
) -> bool:
    rows = await fetchall(
        db,
        "SELECT id FROM task_updates WHERE task_id=? AND kind='done'",
        (task_id,),
    )
    return bool(rows)


async def has_plan_updates(
    db: aiosqlite.Connection,
    task_id: int,
) -> bool:
    rows = await fetchall(
        db,
        "SELECT id FROM task_updates WHERE task_id=? "
        "AND kind='status' AND content LIKE 'Plan:%'",
        (task_id,),
    )
    return bool(rows)


async def declare_wait(
    db: aiosqlite.Connection,
    task_id: int,
    *,
    waiting_for: str,
    waiting_until: str,
    agent: str,
) -> None:
    """Record what this task is waiting for, until when, and who says so (#957).

    A declared wait silences the stale watchdog UNTIL its deadline and never
    past it — that asymmetry is the whole design. Both the claim and its
    author land in the feed too, so "declare a wait and vanish" is a visible
    act with a name on it, not a quiet toggle. An empty ``waiting_for``
    clears the declaration.
    """
    waiting_for = (waiting_for or "").strip()
    waiting_until = (waiting_until or "").strip()
    await db.execute(
        "UPDATE tasks SET waiting_for=?, waiting_until=?, waiting_declared_by=? "
        "WHERE id=?",
        (
            waiting_for,
            waiting_until if waiting_for else "",
            (agent or "").strip() if waiting_for else "",
            task_id,
        ),
    )
    feed_line = (
        f"Объявлено ожидание: {waiting_for} — до {waiting_until} (объявил {agent})."
        if waiting_for
        else f"Ожидание снято ({agent})."
    )
    await add_task_update(db, task_id, agent or "hub", "status", feed_line)
    await db.commit()


async def last_activity_at(db: aiosqlite.Connection, task_id: int) -> str:
    """When the task last saw a real update — stale alerts do not count (#957).

    The watchdog's own alerts bump ``updated_at``, so measuring silence by
    that column would let the alarm feed itself. Silence is the age of the
    last entry a PERSON or an agent wrote.
    """
    rows = await fetchall(
        db,
        "SELECT COALESCE(MAX(created_at), '') AS at FROM task_updates "
        "WHERE task_id=? AND NOT (kind='alert' AND content LIKE '%stale in %')",
        (task_id,),
    )
    return str(rows[0]["at"]) if rows else ""


async def stale_rung_raised(
    db: aiosqlite.Connection, task_id: int, status: str, rung: str
) -> bool:
    """Was the escalation rung ``rung`` already alerted for this task+status (#957)?

    The ladder is monotonic on purpose: a rung, once raised, is never raised
    again — an honest feed entry must not reopen the first rung (that is how
    #927 collected an alert per report), and a task that stays silent climbs
    to the NEXT rung instead of hiding behind the only alert it ever got
    (that is how #443 lay quiet for a week). The rung label is embedded in
    the alert text next to the parseable ``stale in {status}`` key.
    """
    rows = await fetchall(
        db,
        "SELECT 1 FROM task_updates WHERE task_id=? AND kind='alert' "
        "AND content LIKE ? AND content LIKE ? LIMIT 1",
        (task_id, f"%stale in {status}%", f"%[рубеж {rung}]%"),
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
    boundary_rows = await fetchall(
        db,
        "SELECT COALESCE(MAX(id), 0) AS boundary FROM task_updates "
        "WHERE task_id=? AND NOT (kind='alert' AND content LIKE '%stale%')",
        (task_id,),
    )
    boundary = boundary_rows[0]["boundary"] if boundary_rows else 0
    rows = await fetchall(
        db,
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
    return await fetchall(
        db,
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
    rows = await fetchall(
        db,
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


# ---------------------------------------------------------------------------
# Agent session registry (#771)
# ---------------------------------------------------------------------------


async def upsert_agent_session(
    db: aiosqlite.Connection,
    *,
    session_id: str,
    principal_id: int | None,
    agent: str,
    model: str = "",
    host: str = "",
    workspace: str = "",
) -> None:
    """Register a session, or refresh the one already under this id.

    Re-registration keeps ``started_at`` — the session did not restart just
    because it said hello twice — and keeps a previously declared model, host
    or workspace when the new call omits it: a heartbeat-shaped register must
    not quietly erase what the registry knows.
    """
    await db.execute(
        "INSERT INTO agent_sessions "
        "(session_id, principal_id, agent, model, host, workspace, "
        " started_at, last_seen_at) "
        "VALUES (?, ?, ?, ?, ?, ?, datetime('now'), datetime('now')) "
        "ON CONFLICT(session_id) DO UPDATE SET "
        "  principal_id = excluded.principal_id, "
        "  agent        = excluded.agent, "
        "  model        = CASE WHEN excluded.model = '' "
        "                 THEN agent_sessions.model ELSE excluded.model END, "
        "  host         = CASE WHEN excluded.host = '' "
        "                 THEN agent_sessions.host ELSE excluded.host END, "
        "  workspace    = CASE WHEN excluded.workspace = '' "
        "                 THEN agent_sessions.workspace ELSE excluded.workspace END, "
        "  last_seen_at = datetime('now')",
        (session_id, principal_id, agent, model, host, workspace),
    )


async def touch_agent_session(db: aiosqlite.Connection, session_id: str) -> bool:
    """Record a sign of life. False when no such session is registered."""
    cur = await db.execute(
        "UPDATE agent_sessions SET last_seen_at = datetime('now') WHERE session_id = ?",
        (session_id,),
    )
    return bool(cur.rowcount)


async def set_agent_session_task(
    db: aiosqlite.Connection, session_id: str, task_id: int | None
) -> bool:
    """Point a session at the task it now holds (None clears it).

    Deliberately silent when the session is not registered: the registry is
    optional, and claiming a task must not start failing because an agent
    never said hello (#771 AC-5). NO commit here — the caller writes this
    inside the same transaction that records claim_session_id, so the two
    can never disagree.
    """
    cur = await db.execute(
        "UPDATE agent_sessions SET current_task_id = ?, "
        "last_seen_at = datetime('now') WHERE session_id = ?",
        (task_id, session_id),
    )
    return bool(cur.rowcount)


async def get_agent_session(
    db: aiosqlite.Connection, session_id: str
) -> aiosqlite.Row | None:
    rows = await fetchall(
        db,
        "SELECT * FROM agent_sessions WHERE session_id = ? LIMIT 1",
        (session_id,),
    )
    rows = list(rows)
    return rows[0] if rows else None


async def list_agent_sessions(
    db: aiosqlite.Connection,
    *,
    agent: str = "",
    limit: int = 200,
) -> list[aiosqlite.Row]:
    """Registered sessions, freshest sign of life first."""
    conditions: list[str] = []
    params: list[Any] = []
    if agent:
        conditions.append("agent = ?")
        params.append(agent)
    where = f"WHERE {' AND '.join(conditions)} " if conditions else ""
    params.append(min(limit, 500))
    return list(
        await fetchall(
            db,
            f"SELECT * FROM agent_sessions {where}"  # nosec B608
            "ORDER BY last_seen_at DESC, id DESC LIMIT ?",
            tuple(params),
        )
    )


async def list_unaddressable_tasks(
    db: aiosqlite.Connection, *, limit: int = 200
) -> list[aiosqlite.Row]:
    """Pair tasks in flight whose claim carries no session (#852).

    Work is happening and nobody can be asked about it: claimed or running,
    no ``claim_session_id``, and no ``job_id`` — headless tasks are excluded
    on purpose, their executor is the dispatch job, not a session.
    """
    return list(
        await fetchall(
            db,
            "SELECT id, title, status, claimed_by, claimed_at, branch "
            "FROM tasks WHERE status IN ('claimed', 'running') "
            "AND (claim_session_id IS NULL OR claim_session_id = '') "
            "AND (job_id IS NULL OR job_id = '') "
            "AND COALESCE(archived, 0) = 0 "
            "ORDER BY claimed_at ASC, id ASC LIMIT ?",
            (min(limit, 500),),
        )
    )


async def prune_agent_sessions(db: aiosqlite.Connection, *, keep_days: int = 14) -> int:
    """Drop sessions with no sign of life for ``keep_days``. Rows removed."""
    cur = await db.execute(
        "DELETE FROM agent_sessions WHERE last_seen_at < datetime('now', ?)",
        (f"-{keep_days} days",),
    )
    return cur.rowcount or 0


# ---------------------------------------------------------------------------
# Agent messages (#773)
# ---------------------------------------------------------------------------


async def insert_agent_message(
    db: aiosqlite.Connection,
    *,
    from_principal_id: int | None,
    from_session_id: str,
    from_agent: str,
    from_model: str,
    to_kind: str,
    to_ref: str,
    kind: str,
    body: str,
    related_task_id: int | None = None,
    thread_id: str = "",
    for_session: str = "",
) -> int:
    """Append a message. Deliberately NO commit: the caller writes the
    ``message_posted`` event in the same transaction, so a rollback removes
    both and a notification can never outlive the message it announces
    (the rule events have followed since #349)."""
    cur = await db.execute(
        "INSERT INTO agent_messages "
        "(thread_id, from_principal_id, from_session_id, from_agent, from_model, "
        " to_kind, to_ref, kind, body, related_task_id, for_session) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            thread_id,
            from_principal_id,
            from_session_id,
            from_agent,
            from_model,
            to_kind,
            to_ref,
            kind,
            body,
            related_task_id,
            for_session or "",
        ),
    )
    message_id = cur.lastrowid
    if not thread_id:
        # A message that starts a thread is its own thread: one write, no
        # second identifier to keep in sync.
        await db.execute(
            "UPDATE agent_messages SET thread_id = CAST(id AS TEXT) WHERE id = ?",
            (message_id,),
        )
    return message_id  # type: ignore[return-value]


async def get_agent_message(
    db: aiosqlite.Connection, message_id: int
) -> aiosqlite.Row | None:
    rows = list(
        await fetchall(
            db, "SELECT * FROM agent_messages WHERE id = ? LIMIT 1", (message_id,)
        )
    )
    return rows[0] if rows else None


# The four ways a message can be addressed to the caller, as one predicate.
# Task channels resolve through the tasks table rather than through a copy of
# the claim kept here: the task stays the only place that knows who holds it.
_INBOX_PREDICATE = (
    "("
    "  (to_kind = 'session' AND to_ref = :session AND :session <> '')"
    "  OR (to_kind = 'agent' AND to_ref = :agent AND :agent <> '')"
    "  OR (to_kind = 'task' AND to_ref IN ("
    "        SELECT CAST(id AS TEXT) FROM tasks"
    "        WHERE (claimed_by = :agent AND :agent <> '')"
    "           OR (assigned_agent = :agent AND :agent <> '')"
    "           OR (claim_session_id = :session AND :session <> '')))"
    "  OR to_kind = 'project'"
    ")"
)


async def list_inbox_messages(
    db: aiosqlite.Connection,
    *,
    session_id: str = "",
    agent: str = "",
    after_id: int = 0,
    limit: int = 100,
) -> list[aiosqlite.Row]:
    """Messages addressed to this caller with id > ``after_id``, oldest first.

    A cursor rather than a read flag on the row: several readers (the session,
    its agent, the owner in the UI) look at the same message, and a single
    "read" bit would let the first of them hide it from the rest.
    """
    return list(
        await fetchall(
            db,
            f"SELECT * FROM agent_messages WHERE id > :after AND {_INBOX_PREDICATE} "  # nosec B608
            "ORDER BY id ASC LIMIT :limit",
            {
                "after": after_id,
                "session": session_id,
                "agent": agent,
                "limit": min(limit, 200),
            },
        )
    )


async def list_thread_messages(
    db: aiosqlite.Connection, thread_id: str, *, limit: int = 200
) -> list[aiosqlite.Row]:
    return list(
        await fetchall(
            db,
            "SELECT * FROM agent_messages WHERE thread_id = ? ORDER BY id ASC LIMIT ?",
            (thread_id, min(limit, 500)),
        )
    )


async def list_addressable_task_ids(
    db: aiosqlite.Connection,
    *,
    agent: str = "",
    session_ids: list[str] | None = None,
) -> list[str]:
    """Task channels this caller may read, as strings (#774).

    The same three ways the inbox counts a task as yours — you claimed it, it
    is assigned to you, or your session holds it. Returned as text because
    ``to_ref`` is text: an address, not a foreign key.
    """
    sessions = [s for s in (session_ids or []) if s]
    placeholders = ",".join("?" for _ in sessions)
    conditions = []
    params: list[Any] = []
    if agent:
        conditions.append("(claimed_by = ? OR assigned_agent = ?)")
        params.extend([agent, agent])
    if sessions:
        conditions.append(f"claim_session_id IN ({placeholders})")  # nosec B608
        params.extend(sessions)
    if not conditions:
        return []
    rows = await fetchall(
        db,
        f"SELECT id FROM tasks WHERE {' OR '.join(conditions)}",  # nosec B608
        tuple(params),
    )
    return [str(dict(r)["id"]) for r in rows]


async def count_visible_in_thread(
    db: aiosqlite.Connection,
    thread_id: str,
    *,
    session_id: str = "",
    agent: str = "",
) -> int:
    """How many messages of this thread the caller may see (#801).

    The same predicate the inbox is bounded by, plus "or I wrote it": a sender
    reading back their own thread is a participant too. Reusing the predicate is
    the point of this helper — the bug it fixes existed because the rule lived
    in one branch of the endpoint and the other branch never asked for it.
    """
    rows = list(
        await fetchall(
            db,
            "SELECT COUNT(*) AS n FROM agent_messages "  # nosec B608
            f"WHERE thread_id = :thread AND ({_INBOX_PREDICATE} "
            "  OR (from_agent = :agent AND :agent <> ''))",
            {"thread": thread_id, "session": session_id, "agent": agent},
        )
    )
    return int(rows[0]["n"]) if rows else 0


async def count_recent_messages(
    db: aiosqlite.Connection,
    *,
    session_id: str,
    agent: str,
    within_minutes: int = 1,
) -> int:
    """How many messages this sender wrote inside the window (rate limiting).

    Keyed by session when there is one and by agent otherwise, so a sender
    without a registered session cannot dodge the limit by staying anonymous.
    """
    if session_id:
        where, param = "from_session_id = ?", session_id
    else:
        where, param = "from_agent = ?", agent
    rows = list(
        await fetchall(
            db,
            f"SELECT COUNT(*) AS n FROM agent_messages WHERE {where} "  # nosec B608
            "AND created_at >= datetime('now', ?)",
            (param, f"-{within_minutes} minutes"),
        )
    )
    return int(rows[0]["n"]) if rows else 0


async def prune_agent_messages(db: aiosqlite.Connection, *, keep_days: int = 14) -> int:
    """Delete messages older than ``keep_days``. Returns rows removed."""
    cur = await db.execute(
        "DELETE FROM agent_messages WHERE created_at < datetime('now', ?)",
        (f"-{keep_days} days",),
    )
    return cur.rowcount or 0


async def list_task_messages(
    db: aiosqlite.Connection, task_id: int, *, limit: int = 200
) -> list[aiosqlite.Row]:
    """The conversation about one task, oldest first (#775).

    Two ways in, on purpose: a message addressed to the task channel, and a
    message addressed to someone in particular that names this task. Both are
    talk about this task, and an owner reading the card wants the conversation,
    not the addressing scheme.
    """
    return list(
        await fetchall(
            db,
            "SELECT * FROM agent_messages "
            "WHERE (to_kind = 'task' AND to_ref = ?) OR related_task_id = ? "
            "ORDER BY id ASC LIMIT ?",
            (str(task_id), task_id, min(limit, 500)),
        )
    )


async def list_recent_threads(
    db: aiosqlite.Connection, *, limit: int = 10
) -> list[aiosqlite.Row]:
    """Last message of each thread plus its size, newest thread first (#775).

    Every thread, whatever it is addressed to — a conversation between two
    sessions with no task attached is exactly the kind the owner must not have
    to go looking for.
    """
    return list(
        await fetchall(
            db,
            "SELECT m.*, t.messages AS messages FROM agent_messages m "
            "JOIN (SELECT thread_id, COUNT(*) AS messages, MAX(id) AS last_id "
            "      FROM agent_messages GROUP BY thread_id) t ON m.id = t.last_id "
            "ORDER BY m.id DESC LIMIT ?",
            (min(limit, 50),),
        )
    )


# ---------------------------------------------------------------------------
# MCP usage telemetry (#780)
# ---------------------------------------------------------------------------

# The full write surface of the telemetry table. Both the INSERT below and the
# redaction test read this tuple, so a column added to the schema without a
# deliberate decision here writes nothing, and a column added here without a
# schema change fails loudly at the first call.
MCP_CALL_EVENT_COLUMNS: tuple[str, ...] = (
    "tool",
    "profile",
    "principal_id",
    "principal_role",
    "status",
    "error_reason",
    "latency_ms",
    "response_chars",
    "task_id",
)


async def insert_mcp_call_event(
    db: aiosqlite.Connection,
    *,
    tool: str,
    profile: str,
    principal_id: int | None,
    principal_role: str,
    status: str,
    error_reason: str,
    latency_ms: int,
    response_chars: int,
    task_id: int | None,
) -> int:
    """Append one call record. One INSERT, no read-back, no aggregation.

    This runs inside every MCP call, so it stays the cheapest write in the
    codebase: the moment measuring the Agent API makes the Agent API slower,
    the measurement starts changing what it measures.
    """
    cur = await db.execute(
        "INSERT INTO mcp_call_events "
        "(tool, profile, principal_id, principal_role, status, error_reason, "
        " latency_ms, response_chars, task_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            tool,
            profile,
            principal_id,
            principal_role,
            status,
            error_reason,
            latency_ms,
            response_chars,
            task_id,
        ),
    )
    return cur.lastrowid  # type: ignore[return-value]


# Percentiles use the nearest-rank method: index = ceil(p/100 * n), which
# integer division writes as (n*p + 99)/100. No interpolation, so every value
# reported is a latency that actually happened — an averaged p95 nobody
# observed is a worse answer than a real one from a small sample.
_MCP_USAGE_SQL = """
WITH win AS (
    SELECT tool, profile, principal_id, status, latency_ms, response_chars
    FROM mcp_call_events
    WHERE created_at >= datetime('now', :since)
),
agg AS (
    SELECT tool,
           COUNT(*)                                             AS calls,
           COUNT(DISTINCT COALESCE(principal_id, -1))           AS principals,
           SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END)       AS ok_calls,
           SUM(CASE WHEN status <> 'ok' THEN 1 ELSE 0 END)      AS error_calls,
           SUM(response_chars)                                  AS total_chars
    FROM win GROUP BY tool
),
ranked AS (
    SELECT tool,
           latency_ms,
           response_chars,
           ROW_NUMBER() OVER (PARTITION BY tool ORDER BY latency_ms)     AS lat_rank,
           ROW_NUMBER() OVER (PARTITION BY tool ORDER BY response_chars) AS size_rank,
           COUNT(*)    OVER (PARTITION BY tool)                          AS n
    FROM win
),
pct AS (
    SELECT tool,
           MAX(CASE WHEN lat_rank  = (n * 50 + 99) / 100 THEN latency_ms     END) AS p50_latency_ms,
           MAX(CASE WHEN lat_rank  = (n * 95 + 99) / 100 THEN latency_ms     END) AS p95_latency_ms,
           MAX(CASE WHEN size_rank = (n * 50 + 99) / 100 THEN response_chars END) AS p50_response_chars,
           MAX(CASE WHEN size_rank = (n * 95 + 99) / 100 THEN response_chars END) AS p95_response_chars
    FROM ranked GROUP BY tool
)
SELECT agg.tool, agg.calls, agg.principals, agg.ok_calls, agg.error_calls,
       agg.total_chars, pct.p50_latency_ms, pct.p95_latency_ms,
       pct.p50_response_chars, pct.p95_response_chars
FROM agg JOIN pct ON pct.tool = agg.tool
ORDER BY agg.calls DESC, agg.tool ASC
"""


async def mcp_usage_by_tool(
    db: aiosqlite.Connection, *, window_days: int
) -> list[dict[str, Any]]:
    """Per-tool usage, error and cost rows for the window. Read path only."""
    rows = await fetchall(db, _MCP_USAGE_SQL, {"since": f"-{int(window_days)} days"})
    return [dict(row) for row in rows]


async def mcp_usage_by_profile(
    db: aiosqlite.Connection, *, window_days: int
) -> list[dict[str, Any]]:
    """Per-profile totals for the window: the cost of a surface, not a tool."""
    rows = await fetchall(
        db,
        "SELECT profile, COUNT(*) AS calls, "
        "COUNT(DISTINCT tool) AS tools, "
        "COUNT(DISTINCT COALESCE(principal_id, -1)) AS principals, "
        "SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END) AS ok_calls, "
        "SUM(CASE WHEN status <> 'ok' THEN 1 ELSE 0 END) AS error_calls, "
        "SUM(response_chars) AS total_chars "
        "FROM mcp_call_events WHERE created_at >= datetime('now', ?) "
        "GROUP BY profile ORDER BY calls DESC",
        (f"-{int(window_days)} days",),
    )
    return [dict(row) for row in rows]


async def mcp_usage_by_role(
    db: aiosqlite.Connection, *, window_days: int
) -> list[dict[str, Any]]:
    """Per-role totals for the window: who the Agent API is actually for.

    The role is already on every record (#780); until it was grouped, a report
    could say which tools were called but not whether reviewers and analysts
    call any at all. That distinction decides what "nobody called this tool"
    means — genuinely unused, or used from somewhere this table cannot see
    (#816).
    """
    rows = await fetchall(
        db,
        "SELECT principal_role, COUNT(*) AS calls, "
        "COUNT(DISTINCT tool) AS tools, "
        "COUNT(DISTINCT COALESCE(principal_id, -1)) AS principals, "
        "SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END) AS ok_calls, "
        "SUM(CASE WHEN status <> 'ok' THEN 1 ELSE 0 END) AS error_calls, "
        "SUM(response_chars) AS total_chars "
        "FROM mcp_call_events WHERE created_at >= datetime('now', ?) "
        "GROUP BY principal_role ORDER BY calls DESC",
        (f"-{int(window_days)} days",),
    )
    return [dict(row) for row in rows]


async def mcp_usage_errors(
    db: aiosqlite.Connection, *, window_days: int, limit: int = 20
) -> list[dict[str, Any]]:
    """Top error reasons in the window, by tool. Slugs only — never messages."""
    rows = await fetchall(
        db,
        "SELECT tool, error_reason, COUNT(*) AS calls "
        "FROM mcp_call_events "
        "WHERE created_at >= datetime('now', ?) AND status <> 'ok' "
        "GROUP BY tool, error_reason ORDER BY calls DESC, tool ASC LIMIT ?",
        (f"-{int(window_days)} days", int(limit)),
    )
    return [dict(row) for row in rows]


async def prune_mcp_call_events(
    db: aiosqlite.Connection, *, keep_days: int = 120
) -> int:
    """Delete call records older than ``keep_days``. Returns rows removed."""
    cur = await db.execute(
        "DELETE FROM mcp_call_events WHERE created_at < datetime('now', ?)",
        (f"-{keep_days} days",),
    )
    return cur.rowcount or 0


# ---------------------------------------------------------------------------
# Live-check evidence (#813)
# ---------------------------------------------------------------------------


async def insert_live_check(
    db: aiosqlite.Connection,
    *,
    task_id: int,
    sha: str,
    outcome: str,
    probe: str = "",
    observation: str = "",
    reason: str = "",
    recorded_by: int | None = None,
    recorded_agent: str = "",
    deploy_state: str = "",
) -> int:
    """Append one live-check record. Rows accumulate — never overwrite.

    A second check of the same task is a second observation, not a correction
    of the first: they may have looked at different deployments, and the older
    one stays true about the sha it names.
    """
    cur = await db.execute(
        "INSERT INTO live_checks "
        "(task_id, sha, outcome, probe, observation, reason, recorded_by, "
        " recorded_agent, deploy_state) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            task_id,
            sha or "",
            outcome,
            probe or "",
            observation or "",
            reason or "",
            recorded_by,
            recorded_agent or "",
            deploy_state or "",
        ),
    )
    await db.commit()
    return cur.lastrowid  # type: ignore[return-value]


async def list_live_checks(
    db: aiosqlite.Connection, task_id: int, *, limit: int = 50
) -> list[aiosqlite.Row]:
    """Evidence for one task, newest first."""
    return list(
        await fetchall(
            db,
            "SELECT * FROM live_checks WHERE task_id = ? ORDER BY id DESC LIMIT ?",
            (task_id, min(limit, 200)),
        )
    )


async def merge_sha_for_task(db: aiosqlite.Connection, task_id: int) -> str:
    """The merge commit the delivery gate recorded for this task, or "".

    Used as the default subject of a live check: what shipped is what should
    be observed. Empty means the hub does not know — and an unknown sha is
    recorded as unknown rather than guessed (#725).
    """
    rows = list(
        await fetchall(
            db,
            "SELECT merge_sha FROM pipeline_merges WHERE task_id = ? "
            "AND COALESCE(merge_sha, '') != '' ORDER BY id DESC LIMIT 1",
            (task_id,),
        )
    )
    return str(dict(rows[0])["merge_sha"]) if rows else ""


# --- Completed, but is the work actually delivered? (#897) ------------------


async def completed_tasks_awaiting_delivery(
    db: aiosqlite.Connection, *, lookback_days: int = 30, limit: int = 100
) -> list[Any]:
    """Completed tasks whose pinned PR the hub has no merge for — candidates only.

    Cheap by construction, the way #885 made ``merged_into_base`` cheap: a task
    the gate merged is delivered by definition and never reaches here, and a
    task already answered ``delivered`` or ``pr_closed`` is never asked twice —
    both are terminal (code in the base branch stays there, and a closed PR is
    a decision, not a transient). What is left is the small set that can still
    be a discrepancy, so a sweep normally costs zero network calls.

    ``pr_number IS NOT NULL`` is a deliberate boundary: a completed task with
    no PR at all is work that never started delivery, which is #498's warning,
    not this list. Mixing them would make "the discrepancy list" mean two
    different things and stop being trustworthy as either.
    """
    return list(
        await fetchall(
            db,
            """
            SELECT t.* FROM tasks t
            WHERE t.status = 'completed'
              AND t.archived = 0
              AND t.pr_number IS NOT NULL
              AND NOT EXISTS (
                    SELECT 1 FROM pipeline_merges m WHERE m.task_id = t.id
              )
              AND NOT EXISTS (
                    SELECT 1 FROM delivery_discrepancies d
                    WHERE d.task_id = t.id
                      AND d.state IN ('delivered', 'pr_closed')
              )
              AND (
                    COALESCE(NULLIF(t.completed_at, ''), t.updated_at)
                    >= datetime('now', ?)
              )
            ORDER BY COALESCE(NULLIF(t.completed_at, ''), t.updated_at) DESC
            LIMIT ?
            """,
            (f"-{max(int(lookback_days), 1)} days", max(int(limit), 1)),
        )
    )


async def record_delivery_discrepancy(
    db: aiosqlite.Connection,
    *,
    task_id: int,
    state: str,
    reason: str,
    pr_number: int | None = None,
    delivery_path: str = "",
    disposition: str | None = None,
    accepted_via: str | None = None,
    alerted_state: str | None = None,
) -> None:
    """Store the latest answer about one task's delivery.

    ``first_seen_at`` survives updates on purpose — it is the age of the
    discrepancy, and re-checking a row every quarter of an hour must not keep
    resetting the clock that tells the owner how long this has been true.

    ``disposition``/``accepted_via``/``alerted_state`` are ``None`` for "leave
    what is there": the sweep refreshes the facts without erasing what the
    owner declared at acceptance, and the acceptance path records a
    declaration without pretending to have re-run the alert.
    """
    await db.execute(
        """
        INSERT INTO delivery_discrepancies
            (task_id, pr_number, state, reason, delivery_path,
             disposition, accepted_via, alerted_state)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(task_id) DO UPDATE SET
            pr_number     = excluded.pr_number,
            state         = excluded.state,
            reason        = excluded.reason,
            delivery_path = excluded.delivery_path,
            disposition   = COALESCE(?, delivery_discrepancies.disposition),
            accepted_via  = COALESCE(?, delivery_discrepancies.accepted_via),
            alerted_state = COALESCE(?, delivery_discrepancies.alerted_state),
            checked_at    = datetime('now')
        """,
        (
            task_id,
            pr_number,
            state,
            reason,
            delivery_path,
            disposition or "",
            accepted_via or "",
            alerted_state or "",
            disposition,
            accepted_via,
            alerted_state,
        ),
    )
    await db.commit()


async def get_delivery_discrepancy(
    db: aiosqlite.Connection, task_id: int
) -> dict[str, Any] | None:
    """The stored answer for one task, or ``None`` if it was never checked."""
    rows = list(
        await fetchall(
            db,
            "SELECT * FROM delivery_discrepancies WHERE task_id = ?",
            (task_id,),
        )
    )
    return dict(rows[0]) if rows else None


async def list_delivery_discrepancies(
    db: aiosqlite.Connection,
    *,
    states: tuple[str, ...] = ("pr_open",),
    project_id: int | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """The discrepancy list: reads stored answers, never asks a provider (#897).

    Defaults to ``pr_open`` — the completed tasks whose PR is neither merged
    nor closed, which is the one class AC-2 names. ``unknown`` is available by
    asking for it and is reported apart: an answer the hub could not get is
    not evidence of a discrepancy, and folding it in would make the list cry
    wolf every time GitHub is unreachable.
    """
    if not states:
        return []
    placeholders = ",".join("?" for _ in states)
    params: list[Any] = list(states)
    project_clause = ""
    if project_id is not None:
        project_clause = "AND t.project_id = ? "
        params.append(project_id)
    params.append(max(int(limit), 1))
    # The two interpolations are a run of "?" and a fixed clause — every value
    # travels as a bound parameter, the same shape as the task queries above.
    query = f"""
        SELECT
            d.task_id, d.pr_number, d.state, d.reason, d.delivery_path,
            d.disposition, d.accepted_via, d.first_seen_at, d.checked_at,
            t.title, t.status, t.completed_at, t.human_owner, t.assigned_agent,
            CAST(
                (julianday('now') - julianday(
                    COALESCE(NULLIF(t.completed_at, ''), d.first_seen_at)
                )) * 24 AS INTEGER
            ) AS age_hours
        FROM delivery_discrepancies d
        JOIN tasks t ON t.id = d.task_id
        WHERE d.state IN ({placeholders})
          AND t.archived = 0
          {project_clause}
        ORDER BY age_hours DESC, d.task_id ASC
        LIMIT ?
    """  # nosec B608 - placeholders only, values are params
    rows = await fetchall(db, query, tuple(params))
    return [dict(r) for r in rows]


# --- Releases: what is actually running in production (#839) ----------------

RELEASE_SUCCESS = "success"
RELEASE_FAILED = "failed"


async def record_release(
    db: aiosqlite.Connection,
    *,
    deployed_sha: str,
    project_id: int | None = None,
    ref: str = "",
    status: str = RELEASE_SUCCESS,
    source: str = "",
) -> int:
    """Record one deploy attempt and return its id.

    Failures are recorded too, on purpose: a deploy that fell over is evidence
    about the pipeline, and dropping it would make the failure look like it
    never happened. Readers ask for the last SUCCESSFUL release, so a failure
    never becomes the state of production.
    """
    sha, status = deployed_sha.strip(), status.strip()
    # #495: CI runs get re-run, and a re-run redelivers the same callback. A
    # second row for the same (project, sha, status) would say the commit was
    # deployed twice, turning the release history into noise — so the existing
    # record is returned instead. Different STATUS for the same sha is a real
    # second event (a retry that succeeded) and is stored.
    existing = list(
        await fetchall(
            db,
            "SELECT id FROM releases WHERE deployed_sha = ? AND status = ? "
            "AND ((? IS NULL AND project_id IS NULL) OR project_id = ?) "
            "ORDER BY id DESC LIMIT 1",
            (sha, status, project_id, project_id),
        )
    )
    if existing:
        return int(dict(existing[0])["id"])

    cursor = await db.execute(
        "INSERT INTO releases (project_id, deployed_sha, ref, status, source) "
        "VALUES (?, ?, ?, ?, ?)",
        (project_id, sha, ref.strip(), status, source.strip()),
    )
    await db.commit()
    return int(cursor.lastrowid or 0)


async def latest_successful_release(
    db: aiosqlite.Connection, project_id: int | None = None
) -> dict[str, Any] | None:
    """The newest successful deploy for a project, or ``None`` for UNKNOWN.

    ``None`` means the hub has no record — NOT that nothing is deployed. The
    distinction is the whole point of this table: an installation that
    predates it, or one whose CI does not report yet, must read as "we do not
    know", never as "not in production". Collapsing the two would turn silence
    into a denial, which is the failure this epic exists to remove (#725).
    """
    rows = list(
        await fetchall(
            db,
            "SELECT * FROM releases WHERE status = ? "
            "AND (? IS NULL OR project_id = ?) ORDER BY id DESC LIMIT 1",
            (RELEASE_SUCCESS, project_id, project_id),
        )
    )
    return dict(rows[0]) if rows else None


async def release_by_id(
    db: aiosqlite.Connection, release_id: int
) -> dict[str, Any] | None:
    """One release row by id, or None."""
    rows = list(
        await fetchall(db, "SELECT * FROM releases WHERE id = ?", (release_id,))
    )
    return dict(rows[0]) if rows else None
