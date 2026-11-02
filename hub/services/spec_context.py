"""Требования индекса, которые контекст задачи показывает по списку id.

Список id хранится на задаче и пока является единственным источником.
Контекст возвращает обязательство из индекса только для id из списка и
только если индекс этот id уже знает. Путь из affected_areas требование
не выбирает: без id в списке контекст молчит.
"""

from __future__ import annotations

from typing import Any

import aiosqlite

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
    project_id = task.get("project_id")
    if not ids or project_id is None:
        return []
    by_id = {req.id: req for req in await indexed_requirements(db, int(project_id))}
    return [by_id[req_id] for req_id in ids if req_id in by_id]
