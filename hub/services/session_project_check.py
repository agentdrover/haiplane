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
from hub.db import write_transaction
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
    """Одна запись alert с обоими slug; повторная встреча её не дублирует.

    Наличие и вставка — одна write-транзакция: два перекрывающихся захвата
    иначе оба прочитали бы пустую ленту и оба вставили (находка ревью #1674).
    Вызванная внутри транзакции захвата, не открывает и не закрывает свою.
    """
    if check is None:
        return
    text = (
        f"{project_policy.CLAIM_PROJECT_MARK} (claim_project_check={check['mode']}): "
        f"задача проекта {check['task_project']}, сессия {session_id} объявила "
        f"проекты: {', '.join(check['session_projects'])}"
    )
    async with write_transaction(db):
        updates = await repo.get_task_updates(db, task_id)
        if any(str(u["content"]) == text for u in updates):
            return
        await repo.add_task_update(db, task_id, "hub", "alert", text)


class SessionProjectGuard:
    """Сверка проекта под write-локом захвата (#1674, находка ревью).

    Читать сессию и проект до транзакции захвата нельзя: смена ``projects``
    сессии или проекта задачи между проверкой и записью обходила бы require.
    ``enter`` берёт write-лок (BEGIN IMMEDIATE), перечитывает сессию и проект,
    отказывает в require и в warn пишет alert — всё до записывающей части
    area-check и без промежуточного commit. Транзакцию закрывает запись
    захвата; при исключении ``abort`` откатывает её.
    """

    def __init__(self, session_id: str) -> None:
        self.session_id = (session_id or "").strip()
        self.check: dict[str, Any] | None = None
        self.opened = False

    async def enter(self, db: aiosqlite.Connection, task_id: int) -> None:
        if not self.session_id:
            return
        if not db.in_transaction:
            await db.execute("BEGIN IMMEDIATE")
            self.opened = True
        self.check = await foreign_project_check(db, task_id, self.session_id)
        await record_foreign_project(db, task_id, self.session_id, self.check)

    async def abort(self, db: aiosqlite.Connection) -> None:
        if self.opened and db.in_transaction:
            await db.rollback()
