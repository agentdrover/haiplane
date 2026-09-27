"""Канал слотов: где идёт задача и когда слот умер (#1434, F7).

С 26.09 у хаба два канала исполнения — облако Cursor и слоты-воркспейсы на
сервере владельца, — а диспетчер у них один: сессия-стюард. Канал (``cloud``
или ``slot``) и имя слота стюард называет, получая одноразовый код
implementer; при захвате задачи сессией этого кода они ложатся в карточку
одной строкой и в ``executor_slots``.

Проход поллера — по образцу ``executor_runs`` (#1410): строка слота без
сдачи и без признаков жизни дольше порога освобождается один раз, с названной
причиной; задача остаётся в работе и видна как брошенная. Признак жизни —
последняя запись исполнителя в карточке или heartbeat сессии, держащей задачу.

Чего модуль НЕ делает: не выбирает канал для задачи (правило — отдельной
задачей по данным) и не бронирует голову очереди (диспетчер один).
"""

from __future__ import annotations

import logging
from typing import Any

import aiosqlite

from hub import config
from hub import repository as repo
from hub.db import fetchall
from hub.services import project_policy

log = logging.getLogger(__name__)

CHANNEL_SLOT = "slot"
CHANNEL_CLOUD = "cloud"
#: Как занятость называет задачу в работе, чей захват канала не назвал.
UNNAMED = "не назван"

OUTCOME_OCCUPIED = "occupied"
OUTCOME_FREED = "freed"
OUTCOME_RELEASED_DEAD = "released_dead"

#: Статусы, в которых слот держит задачу. Всё прочее — задача ушла из рук
#: исполнителя (сдача, возврат в open, решение человека), слот свободен.
IN_WORK = ("claimed", "running")

POLICY_KEY = "slot_dead_minutes"
EVENT_RELEASED = "executor_slot_released"
REASON_DEAD = "слот {slot} освобождён: нет признаков жизни {minutes} мин"

_LAST_SIGN_SQL = (
    "SELECT MAX(at) AS at FROM ("
    "  SELECT MAX(created_at) AS at FROM task_updates "
    "  WHERE task_id = ? AND agent <> 'hub'"
    "  UNION ALL "
    "  SELECT MAX(last_seen_at) FROM agent_sessions WHERE current_task_id = ?"
    "  UNION ALL "
    "  SELECT ?"
    ")"
)


def label_of(channel: str, slot: str) -> str:
    """Одна строка канала: ``slot-2``, ``cloud`` или «не назван»."""
    if channel == CHANNEL_SLOT and slot:
        return slot
    return channel or UNNAMED


def dead_minutes_of(policy: dict[str, Any]) -> int:
    """Порог смерти слота: ключ политики проекта, иначе конфиг."""
    value = policy.get(POLICY_KEY) if isinstance(policy, dict) else None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return int(config.EXECUTOR_SLOT_DEAD_MINUTES)
    return value


def format_occupancy(result: dict[str, Any]) -> list[str]:
    """Занятость каналов строками — общая для CLI и MCP."""
    lines: list[str] = []
    for row in result.get("tasks") or []:
        lines.append(
            f"{row.get('label', '')}: #{row.get('task_id')} {row.get('title', '')} "
            f"[{row.get('status', '')}] с {row.get('since') or '?'}, "
            f"признак жизни {row.get('last_sign_of_life') or '?'}"
        )
    for row in result.get("abandoned") or []:
        lines.append(
            f"брошена: #{row.get('task_id')} {row.get('title', '')} — "
            f"{row.get('reason', '')}"
        )
    return lines or ["Слоты свободны, задач в работе нет."]


async def _capture_session(
    db: aiosqlite.Connection, task_id: int
) -> dict[str, Any] | None:
    """Живая сессия implementer на задаче с названным каналом, самая свежая."""
    rows = await fetchall(
        db,
        "SELECT channel, slot FROM chat_pair_sessions "
        "WHERE kind = 'implementer' AND bound_task_id = ? AND channel <> '' "
        "AND revoked_at IS NULL AND expires_at > datetime('now') "
        "ORDER BY id DESC LIMIT 1",
        (int(task_id),),
    )
    return dict(rows[0]) if rows else None


async def record_capture(db: aiosqlite.Connection, task_id: int) -> str:
    """Записать канал захвата; вернуть строку канала или пусто.

    Одна живая строка на задачу держится уникальным индексом: claim, а за ним
    pair-start той же сессией — одна строка в карточке, не две.
    """
    session = await _capture_session(db, task_id)
    if session is None:
        return ""
    channel, slot = str(session["channel"]), str(session["slot"] or "")
    cursor = await db.execute(
        "INSERT OR IGNORE INTO executor_slots (task_id, channel, slot) "
        "VALUES (?, ?, ?)",
        (int(task_id), channel, slot),
    )
    if not cursor.rowcount:
        return ""
    label = label_of(channel, slot)
    await repo.add_task_update(db, int(task_id), "hub", "status", f"канал: {label}")
    await db.commit()
    return label


async def active_slot_of(db: aiosqlite.Connection, task_id: int) -> str:
    """Имя слота, который сейчас держит задачу; пусто — не слот."""
    rows = await fetchall(
        db,
        "SELECT slot FROM executor_slots WHERE task_id = ? AND released_at IS NULL "
        "AND channel = ?",
        (int(task_id), CHANNEL_SLOT),
    )
    return str(dict(rows[0])["slot"] or "") if rows else ""


async def _last_sign(db: aiosqlite.Connection, task_id: int, floor: str) -> str:
    rows = await fetchall(db, _LAST_SIGN_SQL, (int(task_id), int(task_id), floor))
    return str(dict(rows[0])["at"] or floor) if rows else floor


async def _silence_minutes(db: aiosqlite.Connection, at: str) -> int:
    rows = await fetchall(
        db,
        "SELECT CAST((julianday('now') - julianday(?)) * 1440 AS INTEGER) AS m",
        (at,),
    )
    value = dict(rows[0])["m"] if rows else None
    return int(value) if value is not None else 0


async def _entry(db: aiosqlite.Connection, row: dict[str, Any]) -> dict[str, Any]:
    since = str(row.get("since") or "")
    last = await _last_sign(db, int(row["task_id"]), since)
    channel, slot = str(row.get("channel") or ""), str(row.get("slot") or "")
    return {
        "task_id": int(row["task_id"]),
        "title": row.get("title") or "",
        "status": row.get("status") or "",
        "channel": channel,
        "slot": slot,
        "label": label_of(channel, slot),
        "since": since,
        "last_sign_of_life": last,
        "silence_minutes": await _silence_minutes(db, last) if last else None,
    }


async def occupancy(db: aiosqlite.Connection) -> dict[str, Any]:
    """Занятость: слоты, все задачи в работе с каналом и брошенные слотом."""
    rows = await fetchall(
        db,
        "SELECT t.id AS task_id, t.title, t.status, "
        "COALESCE(s.channel, '') AS channel, COALESCE(s.slot, '') AS slot, "
        "COALESCE(s.captured_at, t.claimed_at, t.started_at, t.updated_at) AS since "
        "FROM tasks t LEFT JOIN executor_slots s "
        "ON s.task_id = t.id AND s.released_at IS NULL "
        "WHERE t.status IN (?, ?) AND COALESCE(t.archived, 0) = 0 ORDER BY t.id",
        IN_WORK,
    )
    tasks = [await _entry(db, dict(r)) for r in rows]
    dead = await fetchall(
        db,
        "SELECT s.task_id, s.slot, s.released_at, s.reason, t.title, t.status "
        "FROM executor_slots s JOIN tasks t ON t.id = s.task_id "
        "WHERE s.outcome = ? AND t.status IN (?, ?) AND NOT EXISTS ("
        "  SELECT 1 FROM executor_slots live "
        "  WHERE live.task_id = s.task_id AND live.released_at IS NULL"
        ") ORDER BY s.id",
        (OUTCOME_RELEASED_DEAD, *IN_WORK),
    )
    return {
        "slots": [t for t in tasks if t["channel"] == CHANNEL_SLOT],
        "tasks": tasks,
        "abandoned": [dict(r) for r in dead],
    }


async def _close(
    db: aiosqlite.Connection, row_id: int, outcome: str, reason: str
) -> bool:
    cursor = await db.execute(
        "UPDATE executor_slots SET released_at = datetime('now'), outcome = ?, "
        "reason = ? WHERE id = ? AND released_at IS NULL",
        (outcome, reason, int(row_id)),
    )
    return bool(cursor.rowcount)


async def _release_if_dead(db: aiosqlite.Connection, row: dict[str, Any]) -> bool:
    task_id = int(row["task_id"])
    last = await _last_sign(db, task_id, str(row["captured_at"]))
    silence = await _silence_minutes(db, last)
    policy = await project_policy.gate_policy_for_task(db, task_id)
    if silence < dead_minutes_of(policy):
        return False
    reason = REASON_DEAD.format(slot=row["slot"], minutes=silence)
    if not await _close(db, int(row["id"]), OUTCOME_RELEASED_DEAD, reason):
        return False
    await repo.add_task_update(db, task_id, "hub", "alert", reason)
    await repo.insert_event(
        db,
        kind=EVENT_RELEASED,
        task_id=task_id,
        actor="hub",
        payload={"slot": row["slot"], "silence_minutes": silence, "last": last},
    )
    log.warning("executor slot %s released from task #%d", row["slot"], task_id)
    return True


async def sweep_executor_slots(db: aiosqlite.Connection) -> int:
    """Проход поллера: закрыть ушедшие из работы, освободить умершие слоты.

    Возвращает число освобождённых по сроку. Задача, ушедшая из работы
    (сдача, возврат), закрывает строку молча — записи об освобождении нет.
    """
    rows = await fetchall(
        db,
        "SELECT s.id, s.task_id, s.channel, s.slot, s.captured_at, "
        "t.status AS task_status FROM executor_slots s "
        "JOIN tasks t ON t.id = s.task_id WHERE s.released_at IS NULL",
    )
    released = 0
    for raw in rows:
        row = dict(raw)
        status = str(row["task_status"] or "")
        if status not in IN_WORK:
            await _close(db, int(row["id"]), OUTCOME_FREED, f"задача ушла в {status}")
        elif row["channel"] == CHANNEL_SLOT and await _release_if_dead(db, row):
            released += 1
    if rows:
        await db.commit()
    return released
