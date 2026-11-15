"""SQL долга по спецификации (#2220).

Рядом с ``hub.repository``: тот файл стоит у потолка размера. Здесь только
запросы; решение «долг ли это» - в ``hub.services.spec_archive``. Ничего не
коммитит: транзакцию ведёт вызывающий.
"""

from __future__ import annotations

from typing import Any

import aiosqlite


async def insert_debt(
    db: aiosqlite.Connection,
    *,
    project_id: int,
    task_id: int,
    requirement_id: str,
    pr_number: int,
    approved_sha: str,
    merged_sha: str,
    reason: str,
) -> bool:
    """Записать долг; ``False`` - такая строка уже есть (то же слияние виделось раньше)."""
    cur = await db.execute(
        "INSERT OR IGNORE INTO spec_archive_debt (project_id, task_id, "
        "requirement_id, pr_number, approved_sha, merged_sha, reason) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            project_id,
            task_id,
            requirement_id,
            pr_number,
            approved_sha,
            merged_sha,
            reason,
        ),
    )
    return cur.rowcount > 0


async def list_open(db: aiosqlite.Connection, project_id: int) -> list[dict[str, Any]]:
    """Незакрытые долги проекта в порядке записи."""
    cur = await db.execute(
        "SELECT requirement_id, task_id, pr_number, approved_sha, merged_sha, "
        "reason, created_at FROM spec_archive_debt "
        "WHERE project_id = ? AND closed_at IS NULL ORDER BY id",
        (project_id,),
    )
    return [dict(row) for row in await cur.fetchall()]
