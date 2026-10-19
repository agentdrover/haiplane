"""SQL журнала минут владельца (#1874).

Рядом с ``hub.repository``, а не в нём: тот файл стоит у потолка размера
(docs/agent-context/file-size-budget.json). Здесь только запросы; нормативы,
решение «кого и за что считать» и сборка ответа - в
``hub.services.owner_minutes``.

Ни одна функция не коммитит: пас оборачивает пакет в одну write-транзакцию, и
начисления вместе с продвижением курсора либо есть, либо нет.
"""

from __future__ import annotations

from typing import Any

import aiosqlite


async def get_state(db: aiosqlite.Connection) -> dict[str, Any] | None:
    cur = await db.execute(
        "SELECT tracking_since, cursor_event_id FROM owner_minutes_state WHERE id = 1"
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def init_state(db: aiosqlite.Connection) -> None:
    """Однократно: когда начался учёт и с какого события. Повтор ничего не меняет."""
    await db.execute(
        "INSERT OR IGNORE INTO owner_minutes_state (id, tracking_since, cursor_event_id) "
        "SELECT 1, datetime('now'), COALESCE(MAX(id), 0) FROM events"
    )


async def max_event_id(db: aiosqlite.Connection) -> int:
    cur = await db.execute("SELECT COALESCE(MAX(id), 0) FROM events")
    row = await cur.fetchone()
    return int(row[0]) if row else 0


async def events_after(
    db: aiosqlite.Connection, cursor: int, limit: int
) -> list[dict[str, Any]]:
    """Следующий пакет событий ВСЕХ видов: курсор идёт и по неучитываемым."""
    cur = await db.execute(
        "SELECT id, kind, task_id, payload, created_at FROM events "
        "WHERE id > ? ORDER BY id ASC LIMIT ?",
        (cursor, limit),
    )
    return [dict(r) for r in await cur.fetchall()]


async def human_principal_ids(db: aiosqlite.Connection, ids: set[int]) -> set[int]:
    """Из названных принципалов - те, что в таблице principals имеют kind='human'."""
    if not ids:
        return set()
    marks = ",".join("?" * len(ids))
    cur = await db.execute(
        f"SELECT id FROM principals WHERE kind = 'human' AND id IN ({marks})",  # nosec B608
        tuple(sorted(ids)),
    )
    return {int(r[0]) for r in await cur.fetchall()}


async def insert_accrual(
    db: aiosqlite.Connection,
    *,
    task_id: int,
    principal_id: int,
    kind: str,
    occurred_at: str,
    seconds: int,
    norms_version: int,
    source_event_id: int,
) -> None:
    await db.execute(
        "INSERT OR IGNORE INTO owner_minute_accruals (task_id, principal_id, kind, "
        "occurred_at, seconds, norms_version, source_event_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            task_id,
            principal_id,
            kind,
            occurred_at,
            seconds,
            norms_version,
            source_event_id,
        ),
    )


async def advance_cursor(db: aiosqlite.Connection, cursor: int) -> None:
    await db.execute(
        "UPDATE owner_minutes_state SET cursor_event_id = ? WHERE id = 1", (cursor,)
    )


async def task_split(db: aiosqlite.Connection, task_id: int) -> list[dict[str, Any]]:
    """Сумма и число начислений по задаче в разбивке по принципалам."""
    cur = await db.execute(
        "SELECT a.principal_id AS principal_id, COALESCE(p.username, '') AS username, "
        "SUM(a.seconds) AS seconds, COUNT(*) AS accruals "
        "FROM owner_minute_accruals a LEFT JOIN principals p ON p.id = a.principal_id "
        "WHERE a.task_id = ? GROUP BY a.principal_id ORDER BY a.principal_id",
        (task_id,),
    )
    return [dict(r) for r in await cur.fetchall()]


async def principal_window(
    db: aiosqlite.Connection, principal_id: int, start_utc: str, end_utc: str
) -> tuple[int, int]:
    """(секунды, число начислений) принципала за [start_utc, end_utc) по UTC."""
    cur = await db.execute(
        "SELECT COALESCE(SUM(seconds), 0), COUNT(*) FROM owner_minute_accruals "
        "WHERE principal_id = ? AND occurred_at >= ? AND occurred_at < ?",
        (principal_id, start_utc, end_utc),
    )
    row = await cur.fetchone()
    return (int(row[0]), int(row[1])) if row else (0, 0)
