"""SQL индекса поведенческой спецификации (#2214).

Рядом с ``hub.repository``, а не в нём: тот файл стоит у потолка размера
(docs/agent-context/file-size-budget.json). Здесь только запросы; разбор файла и
решение «индексировать или отказать» - в ``hub.services.spec_index``.

Ни одна функция не коммитит: ``replace_index`` вызывается внутри одной
write-транзакции вызывающего, и прежний индекс либо заменён целиком, либо цел.
"""

from __future__ import annotations

import json
from collections.abc import Collection
from typing import Any

import aiosqlite

from hub.models import IndexedRequirement, SpecScenario


async def replace_index(
    db: aiosqlite.Connection,
    project_id: int,
    *,
    ref: str,
    commit_sha: str,
    files: list[dict[str, str]],
    requirements: list[dict[str, Any]],
) -> None:
    """Заменить индекс проекта целиком: файлы, требования и состояние."""
    await db.execute(
        "DELETE FROM spec_requirements WHERE project_id = ?", (project_id,)
    )
    await db.execute("DELETE FROM spec_files WHERE project_id = ?", (project_id,))
    for f in files:
        await db.execute(
            "INSERT INTO spec_files (project_id, path, capability, revision) "
            "VALUES (?, ?, ?, ?)",
            (project_id, f["path"], f["capability"], f["revision"]),
        )
    for position, req in enumerate(requirements):
        await insert_requirement(db, project_id, position, req)
    await db.execute(
        "INSERT INTO spec_index_state (project_id, ref, commit_sha, indexed_at) "
        "VALUES (?, ?, ?, datetime('now')) "
        "ON CONFLICT(project_id) DO UPDATE SET ref = excluded.ref, "
        "commit_sha = excluded.commit_sha, indexed_at = excluded.indexed_at",
        (project_id, ref, commit_sha),
    )


async def insert_requirement(
    db: aiosqlite.Connection, project_id: int, position: int, req: dict[str, Any]
) -> None:
    await db.execute(
        "INSERT INTO spec_requirements (project_id, req_id, path, position, "
        "obligation, scenarios, revision) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            project_id,
            req["id"],
            req["path"],
            position,
            req["obligation"],
            req["scenarios"],
            req["revision"],
        ),
    )


async def get_state(db: aiosqlite.Connection, project_id: int) -> dict[str, Any] | None:
    cur = await db.execute(
        "SELECT ref, commit_sha, indexed_at FROM spec_index_state WHERE project_id = ?",
        (project_id,),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def list_files(db: aiosqlite.Connection, project_id: int) -> list[dict[str, Any]]:
    cur = await db.execute(
        "SELECT path, capability, revision FROM spec_files "
        "WHERE project_id = ? ORDER BY path",
        (project_id,),
    )
    return [dict(r) for r in await cur.fetchall()]


async def list_requirements(
    db: aiosqlite.Connection, project_id: int
) -> list[IndexedRequirement]:
    """Требования проекта в порядке файлов и строк; ревизия файла - из ``spec_files``."""
    cur = await db.execute(
        "SELECT r.req_id, r.path, r.obligation, r.scenarios, r.revision, "
        "f.capability, f.revision AS file_revision "
        "FROM spec_requirements r "
        "JOIN spec_files f ON f.project_id = r.project_id AND f.path = r.path "
        "WHERE r.project_id = ? ORDER BY r.path, r.position",
        (project_id,),
    )
    return [
        IndexedRequirement(
            id=row["req_id"],
            obligation=row["obligation"],
            scenarios=[SpecScenario(**s) for s in json.loads(row["scenarios"])],
            path=row["path"],
            capability=row["capability"],
            file_revision=row["file_revision"],
            revision=row["revision"],
        )
        for row in await cur.fetchall()
    ]


async def approved_snapshots(
    db: aiosqlite.Connection, project_id: int, merge_shas: Collection[str]
) -> list[tuple[str, str]]:
    """``(слитый коммит, снимок дельты)`` одобренных сдач из ``merge_shas`` (#2223).

    Порядок здесь не задан: его даёт история git, вызывающий сортирует по ней.

    Слияние - факт хаба: ``pipeline_merges`` (по проекту слияния, как у остальных
    чтений, не по ``tasks.project_id``). Снимок берётся у поколения, которое
    одобрил вердикт, и только пока одобрение не сброшено расхождением черновика.
    Текст на этом коммите, у которого такой строки нет, одобрения не имеет.
    """
    if not merge_shas:
        return []
    cur = await db.execute(
        "SELECT m.merge_sha, s.behavior_snapshot FROM pipeline_merges m "
        "JOIN tasks t ON t.id = m.task_id "
        "JOIN submissions s ON s.task_id = t.id "
        "AND s.generation = t.review_verdict_generation "
        "WHERE m.project_id = ? AND COALESCE(m.merge_sha, '') <> '' "
        "AND t.review_verdict = 'approved' "
        "AND COALESCE(t.behavior_approval_void, 0) = 0 "
        "AND COALESCE(s.behavior_snapshot, '') <> ''",
        (project_id,),
    )
    wanted = set(merge_shas)
    return [
        (str(r["merge_sha"]), str(r["behavior_snapshot"]))
        for r in await cur.fetchall()
        if r["merge_sha"] in wanted
    ]
