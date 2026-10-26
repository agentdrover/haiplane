"""Возврат на доработку молчащему исполнителю: сказать вслух, ничего не двигая (#2053).

Вердикт «на доработку» оставляет задачу у того, кто её держит. Если он давно не
подаёт признаков жизни, человеку нужно знать об этом в карточке: решить,
отпускать ли задачу, может только он. Автоматическое освобождение три раунда
ревью отнимало задачи у живых агентов (имена, окна, гонки), поэтому здесь
сигнал ТОЛЬКО информирует: статус и захват не меняются, а ошибка проверки
вердикт не блокирует. Сигнал может ошибаться - от этого он и не действует.
"""

from __future__ import annotations

import logging

import aiosqlite

from hub import repository as repo
from hub.db import fetchall

log = logging.getLogger(__name__)

EVENT_SILENT = "rework_executor_silent"


async def silence_minutes(db: aiosqlite.Connection, task: dict) -> int | None:
    """Порог молчания в минутах, если исполнитель его превысил; иначе ``None``.

    Молчит: нет идущего прогона и открытого слота, а последняя запись исполнителя
    в карточке и последний признак его сессии старше порога ``slot_dead_minutes``.
    """
    from hub.services import executor_slots, project_policy

    task_id = int(task["id"])
    names = [
        str(task.get(key) or "").strip() for key in ("claimed_by", "assigned_agent")
    ]
    names = [n for n in names if n]
    session = str(task.get("claim_session_id") or "").strip()
    if not names and not session:
        return None
    live = await fetchall(
        db,
        "SELECT 1 FROM executor_runs WHERE task_id = ? AND outcome = 'running' "
        "AND cost_wait_since IS NULL "
        "UNION ALL SELECT 1 FROM executor_slots WHERE task_id = ? "
        "AND released_at IS NULL",
        (task_id, task_id),
    )
    if live:
        return None
    marks = ",".join("?" for _ in names) or "NULL"
    rows = await fetchall(
        db,
        "SELECT MAX(at) AS at FROM ("  # nosec B608 - placeholders only
        "  SELECT MAX(created_at) AS at FROM task_updates "
        f"  WHERE task_id = ? AND agent IN ({marks}) "
        "  UNION ALL SELECT last_seen_at FROM agent_sessions WHERE session_id = ?"
        ")",
        (task_id, *names, session),
    )
    seen = dict(rows[0])["at"] if rows else None
    policy = await project_policy.gate_policy_for_task(db, task_id)
    limit = executor_slots.dead_minutes_of(policy)
    if not seen:
        return limit
    silent = await executor_slots._silence_minutes(db, str(seen))
    return limit if silent >= limit else None


async def note_if_silent(db: aiosqlite.Connection, task_id: int) -> bool:
    """Одна запись в карточке на поколение сдачи; ``True`` - записана этим вызовом.

    Вызывается в транзакции вердикта. Любая ошибка проверки глотается: вердикт
    важнее подсказки.
    """
    try:
        found = await repo.get_task(db, int(task_id))
        if found is None:
            return False
        task = dict(found)
        generation = int(task.get("submission_generation") or 0)
        minutes = await silence_minutes(db, task)
        if minutes is None:
            return False
        if await fetchall(
            db,
            "SELECT 1 FROM events WHERE task_id = ? AND kind = ? "
            "AND json_extract(payload, '$.generation') = ?",
            (int(task_id), EVENT_SILENT, generation),
        ):
            return False
        await repo.add_task_update(
            db,
            int(task_id),
            "hub",
            "alert",
            f"Возврат на доработку, исполнитель молчит дольше {minutes} мин - "
            "задача остаётся у него. Решите, отпустить ли её.",
        )
        await repo.insert_event(
            db,
            kind=EVENT_SILENT,
            task_id=int(task_id),
            actor="hub",
            payload={"generation": generation, "limit_minutes": minutes},
        )
        return True
    except Exception:  # noqa: BLE001 - подсказка не блокирует вердикт
        log.warning("rework silence check failed for #%s", task_id, exc_info=True)
        return False
