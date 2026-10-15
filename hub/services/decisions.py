"""Записи ленты задач вида ``decision`` (#1698).

Хаб сам пишет в ленту задачи запись kind=decision при decide (accept и rework),
возврате в работу и архивировании активной работы. Здесь они читаются обратно.
Это «записи ленты вида decision с происхождением», а не «решения владельца»:
kind — произвольная строка обновления, такую запись может добавить и агент, и
поэтому каждая строка несёт происхождение (agent, principal_id, author_kind).

Источник первой версии — только ``task_updates.kind='decision'``. Events
диспетчера, суждения стюарда и review-вердикты — другие журналы и сюда не
входят.

SQL лежит здесь, а не в ``repository.py``: у того бюджет размера без запаса.
"""

from __future__ import annotations

from typing import Any

import aiosqlite

from hub import repository as repo

DECISION_KIND = "decision"
DEFAULT_LIMIT = 10
MAX_LIMIT = 50
# Константа репозитория, не ввод: подзапрос по поддереву проекта.
_PROJECT_FILTER = (
    "u.task_id IN (SELECT id FROM tasks WHERE "
    + (
        repo.PROJECT_SUBTREE_CONDITION  # nosec B608
    )
    + ")"
)


def clamp_limit(limit: int | None) -> int:
    """Лимит в пределах 1..50; пустое значение — 10."""
    if limit is None:
        return DEFAULT_LIMIT
    return max(1, min(int(limit), MAX_LIMIT))


async def list_decisions(
    db: aiosqlite.Connection,
    *,
    project_id: int | None = None,
    limit: int | None = DEFAULT_LIMIT,
) -> list[dict[str, Any]]:
    """Новые записи decision: ``created_at DESC, id DESC``.

    Архивные задачи и проект отбираются В ТОМ ЖЕ запросе, до LIMIT: проект
    держит поддерево задач (потомки наследуют его от эпика). Фильтр после
    LIMIT выбросил бы строки, на которые лимит уже потрачен.
    """
    where = ["u.kind = ?", "t.archived = 0"]
    params: list[Any] = [DECISION_KIND]
    if project_id is not None:
        where.append(_PROJECT_FILTER)
        params.append(project_id)
    params.append(clamp_limit(limit))
    cur = await db.execute(
        "SELECT u.id, u.task_id, t.title AS task_title, u.content, u.agent, "  # nosec B608
        "u.principal_id, u.author_kind, u.created_at "
        "FROM task_updates u JOIN tasks t ON t.id = u.task_id "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY u.created_at DESC, u.id DESC LIMIT ?",
        tuple(params),
    )
    rows = await cur.fetchall()
    slugs: dict[int, str] = {}
    out: list[dict[str, Any]] = []
    for row in rows:
        task_id = int(row["task_id"])
        if task_id not in slugs:
            project = await repo.resolve_project_for_task(db, task_id)
            slugs[task_id] = project["slug"] if project is not None else "default"
        out.append(
            {
                "id": row["id"],
                "task_id": task_id,
                "task_title": row["task_title"],
                "project": slugs[task_id],
                "content": row["content"],
                "agent": row["agent"],
                "principal_id": row["principal_id"],
                "author_kind": row["author_kind"],
                "created_at": row["created_at"],
            }
        )
    return out
