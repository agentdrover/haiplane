"""Живой блокер уводит отчёт о готовности к человеку (#948, #952, #1679).

Вынесено из ``lifecycle.add_update``: у функции нет запаса сложности, а у
файла — запаса размера. Поведение то же, что было на месте, плюс событие
needs_decision через общую обёртку — стюард проекта получает уведомление.
"""

from __future__ import annotations

import aiosqlite

from hub import repository as repo
from hub.db import log_activity
from hub.hub_instance import mutation_activity_detail
from hub.services import watch_notices


async def hold_done_report(db: aiosqlite.Connection, task_id: int, note: str) -> None:
    """Статус needs_decision, алерт, событие и запись в журнал; коммитит вызывающий."""
    await repo.update_task(db, task_id, status="needs_decision")
    await repo.add_task_update(
        db,
        task_id,
        "hub",
        "alert",
        # #952: same rule as the merge-gate alert — the task is entering
        # needs_decision, where a fresh done report is refused, so the hint
        # must route through the decision, not around it.
        f"Отчёт о готовности не пошёл в доставку: после "
        f"последней сдачи записан блокер — {note}. Решение "
        "за человеком (REST POST /api/tasks/<id>/decide): rework вернёт "
        "задачу в running — снимите препятствие и "
        "пересдайте done; accept завершит без доставки.",
    )
    await watch_notices.emit_needs_decision(
        db,
        task_id=task_id,
        actor="hub",
        payload={"reason": "done_held_by_blocker", "blocker": note[:200]},
    )
    await log_activity(
        db,
        "task_needs_decision",
        f"Task #{task_id} → needs_decision ({note})",
        detail=mutation_activity_detail(),
    )
