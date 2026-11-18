"""Read models for the recorded release pipeline and its deployment feed."""

from collections import defaultdict
from typing import Any

import aiosqlite

from hub.db import fetchall


# A legacy merge's project_id can be empty even when the task inherits an
# explicit project. Visibility follows the task as well as the merge's scope.
_TASK_SCOPE = """
    task_lineage(task_id, parent_id, project_id, depth) AS (
        SELECT t.id, t.parent_id, t.project_id, 0 FROM tasks t
        WHERE t.id IN (SELECT task_id FROM candidates)
        UNION ALL
        SELECT lineage.task_id, parent.parent_id, parent.project_id, lineage.depth + 1
        FROM task_lineage lineage JOIN tasks parent ON parent.id = lineage.parent_id
        WHERE lineage.project_id IS NULL AND lineage.depth < 19
    ),
    task_scope AS (
        SELECT task_id FROM task_lineage
        WHERE project_id = ? OR (? AND project_id IS NULL AND parent_id IS NULL)
    )
"""


async def unreleased_completed_count(
    db: aiosqlite.Connection, project_id: int, include_legacy: bool
) -> int:
    rows = await fetchall(
        db,
        f"""WITH RECURSIVE latest AS (
                SELECT task_id, MAX(id) AS merge_id FROM pipeline_merges
                WHERE project_id = ? OR (? AND project_id IS NULL)
                GROUP BY task_id
            ),
            candidates AS (
                SELECT latest.task_id FROM latest
                JOIN pipeline_merges m ON m.id = latest.merge_id
                JOIN tasks t ON t.id = latest.task_id
                WHERE t.status = 'completed' AND COALESCE(TRIM(m.released_sha), '') = ''
            ),
            {_TASK_SCOPE}
            SELECT COUNT(*) AS total FROM task_scope""",  # nosec B608 - constant CTE
        (project_id, include_legacy, project_id, include_legacy),
    )
    return int(rows[0]["total"])


async def release_merges_since(
    db: aiosqlite.Connection, project_id: int, deployed_sha: str
) -> int | None:
    """Count only when the deployed commit has an exact recorded merge."""
    rows = await fetchall(
        db,
        """WITH deployed_merge AS (
               SELECT merged_at, id
               FROM release_merges
               WHERE project_id = ? AND LOWER(TRIM(release_sha)) = ?
               ORDER BY merged_at DESC, id DESC LIMIT 1
           )
           SELECT (
               SELECT COUNT(*) FROM release_merges m
               WHERE m.project_id = ?
                 AND (m.merged_at, m.id) > (deployed_merge.merged_at, deployed_merge.id)
           ) AS total FROM deployed_merge""",
        (project_id, deployed_sha, project_id),
    )
    return int(rows[0]["total"]) if rows else None


async def release_feed(
    db: aiosqlite.Connection, project_id: int, include_legacy: bool, limit: int
) -> list[dict[str, Any]]:
    rows = await fetchall(
        db,
        """WITH recent AS (
               SELECT * FROM releases WHERE project_id = ? OR (? AND project_id IS NULL)
               ORDER BY deployed_at DESC, id DESC LIMIT ?
           )
           SELECT r.id, r.deployed_sha AS sha, r.ref, r.status, r.source,
                  r.deployed_at, r.backup, m.release_pr, m.merged_at AS release_merged_at,
                  ROUND((JULIANDAY(r.deployed_at) - JULIANDAY(m.merged_at)) * 1440, 2)
                      AS lag_minutes
           FROM recent r
           LEFT JOIN release_merges m ON m.id = (
               SELECT id FROM release_merges
               WHERE project_id = ? AND LOWER(TRIM(release_sha)) = LOWER(TRIM(r.deployed_sha))
               ORDER BY merged_at DESC, id DESC LIMIT 1
           )
           ORDER BY r.deployed_at DESC, r.id DESC""",
        (project_id, include_legacy, limit, project_id),
    )
    return [dict(row) for row in rows]


async def release_tasks(
    db: aiosqlite.Connection, project_id: int, include_legacy: bool, shas: set[str]
) -> dict[str, list[int]]:
    if not shas:
        return {}
    marks = ", ".join("?" for _ in shas)
    rows = await fetchall(
        db,
        f"""WITH RECURSIVE candidates AS (
                SELECT DISTINCT LOWER(TRIM(released_sha)) AS sha, task_id
                FROM pipeline_merges
                WHERE (project_id = ? OR (? AND project_id IS NULL))
                  AND task_id IS NOT NULL AND LOWER(TRIM(released_sha)) IN ({marks})
            ),
            {_TASK_SCOPE}
            SELECT c.sha, c.task_id FROM candidates c
            JOIN task_scope s ON s.task_id = c.task_id
            ORDER BY c.task_id""",  # nosec B608 - constant CTE and placeholders
        (project_id, include_legacy, *sorted(shas), project_id, include_legacy),
    )
    result: dict[str, list[int]] = defaultdict(list)
    for row in rows:
        result[row["sha"]].append(int(row["task_id"]))
    return dict(result)


async def open_deploy_reasons(
    db: aiosqlite.Connection,
    project_id: int,
    alert_kinds: tuple[str, str],
    recovery_kind: str,
) -> list[str]:
    rows = await fetchall(
        db,
        """SELECT JSON_EXTRACT(e.payload, '$.reason') AS reason
           FROM events e
           WHERE e.project_id = ? AND e.kind IN (?, ?)
             AND NOT EXISTS (
                 SELECT 1 FROM events r
                 WHERE r.project_id = e.project_id AND r.kind = ? AND r.id > e.id
                   AND JSON_EXTRACT(r.payload, '$.release_merge_id') =
                       JSON_EXTRACT(e.payload, '$.release_merge_id')
             )
           ORDER BY e.id""",
        (project_id, *alert_kinds, recovery_kind),
    )
    return [str(row["reason"]) for row in rows if row["reason"]]
