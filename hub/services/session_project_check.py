"""Сверка проекта задачи с проектами сессии при захвате (#1674).

MCP не хранит сессию вызывающего (#1364): она известна только там, где её
передают явно, — claim (``body.session_id``) и pair-start (declared session).
Только на этих двух входах и спрашиваем. Режим — ``claim_project_check``;
проверка независима от сверки областей и стоит ДО её записывающей части, чтобы
require отказывал без единой записи.

Молчит, когда сказать нечего: сессия не передана или не зарегистрирована,
проекты не объявлены, задача своего проекта.
"""

from __future__ import annotations

from typing import Any

import aiosqlite
from fastapi import HTTPException

from hub import repository as repo
from hub.actionable_errors import project_check_refused_detail
from hub.services import project_policy
from hub.services.sessions import declared_projects


async def foreign_project_check(
    db: aiosqlite.Connection, task_id: int, session_id: str
) -> dict[str, Any] | None:
    """``{mode, task_project, session_projects}`` для warn, иначе None.

    В require чужой проект — 409 ``project_check_refused``; ничего не пишет.
    """
    session_id = (session_id or "").strip()
    if not session_id:
        return None
    session = await repo.get_agent_session(db, session_id)
    if session is None:
        return None
    declared = declared_projects(dict(session).get("projects"))
    if not declared:
        return None
    project = await repo.resolve_project_for_task(db, task_id)
    if project is None:
        return None
    mode = project_policy.claim_project_check_of(project_policy.gate_policy_of(project))
    task_project = str(project["slug"])
    if mode == project_policy.CLAIM_PROJECT_OFF or task_project.lower() in declared:
        return None
    if mode == project_policy.CLAIM_PROJECT_REQUIRE:
        row = await repo.get_task(db, task_id)
        raise HTTPException(
            409,
            detail=project_check_refused_detail(
                task_id=task_id,
                task_project=task_project,
                session_projects=declared,
                current_status=str(row["status"]) if row is not None else "",
            ),
        )
    return {"mode": mode, "task_project": task_project, "session_projects": declared}


async def record_foreign_project(
    db: aiosqlite.Connection,
    task_id: int,
    session_id: str,
    check: dict[str, Any] | None,
) -> None:
    """Одна запись alert с обоими slug; повторная встреча её не дублирует."""
    if check is None:
        return
    text = (
        f"{project_policy.CLAIM_PROJECT_MARK} (claim_project_check={check['mode']}): "
        f"задача проекта {check['task_project']}, сессия {session_id} объявила "
        f"проекты: {', '.join(check['session_projects'])}"
    )
    updates = await repo.get_task_updates(db, task_id)
    if any(str(u["content"]) == text for u in updates):
        return
    await repo.add_task_update(db, task_id, "hub", "alert", text)
    await db.commit()
