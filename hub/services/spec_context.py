"""Требования индекса, которые контекст задачи показывает по списку id.

Список id хранится на задаче и пока является единственным источником.
Контекст возвращает обязательство из индекса только для id из списка и
только если индекс этот id уже знает. Путь из affected_areas требование
не выбирает: без id в списке контекст молчит.
"""

from __future__ import annotations

from typing import Any

import aiosqlite

from hub import repository as repo
from hub.db import deserialize_str_list
from hub.models import IndexedRequirement
from hub.services.spec_index import indexed_requirements


def requirement_lines(reqs: list[IndexedRequirement]) -> list[str]:
    """Строки дайджеста. Пустой список не добавляет раздел: молчание — не «нет REQ»."""
    if not reqs:
        return []
    lines = ["Requirements:"]
    lines.extend(f"- {req.id}: {req.obligation}" for req in reqs)
    return lines


async def linked_requirements(
    db: aiosqlite.Connection, task: dict[str, Any]
) -> list[IndexedRequirement]:
    """Требования индекса в порядке списка id. Неизвестного id в ответе нет."""
    ids = deserialize_str_list(task.get("requirement_ids"))
    if not ids:
        return []
    # Проект живёт на эпике, ребёнок наследует: колонка tasks.project_id у
    # ребёнка NULL, ключ индекса берём тем же путём, что и остальной /context.
    project = await repo.resolve_project_for_task(db, int(task["id"]))
    if project is None:
        return []
    by_id = {req.id: req for req in await indexed_requirements(db, int(project["id"]))}
    return [by_id[req_id] for req_id in ids if req_id in by_id]
