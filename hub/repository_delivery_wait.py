"""SQL срока ожидания доставки (#1992).

Рядом с ``hub.repository``, а не в нём: файл у потолка размера. Нигде здесь нет
коммита: транзакцией владеет вызывающий. Время считает база (``datetime('now')``,
UTC), чтобы срок не зависел от часов процесса и не разбирал строки в Python.
"""

from __future__ import annotations

import aiosqlite

from hub.db import fetchall

OPEN = "open"
CLOSED = "closed"
NOTIFIED = "notified"


async def open_wait(
    db: aiosqlite.Connection, task_id: int, generation: int, revision: int, kind: str
) -> None:
    """Начать (или продолжить) ожидание; закрытое - заново, эскалированное - нет.

    Открытая строка не трогается: ``started_at`` не двигается ни повтором, ни
    рестартом. ``notified`` терминально для своего ключа: владелец уже уведомлен, и
    повторный отказ fetch с тем же ключом второго уведомления не даёт. Открытые строки той же задачи с другим ключом (прежнее поколение
    или ревизия вердикта) закрываются: новая сдача начинает отсчёт с нуля.
    """
    await db.execute(
        "UPDATE delivery_wait_deadlines SET state=?, closed_at=datetime('now') "
        "WHERE task_id=? AND kind=? AND state=? "
        "AND NOT (submission_generation=? AND verdict_revision=?)",
        (CLOSED, task_id, kind, OPEN, generation, revision),
    )
    await db.execute(
        "INSERT INTO delivery_wait_deadlines "
        "(task_id, submission_generation, verdict_revision, kind) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT (task_id, submission_generation, verdict_revision, kind) "
        "DO UPDATE SET state=?, started_at=datetime('now'), closed_at=NULL "
        "WHERE delivery_wait_deadlines.state = ?",
        (task_id, generation, revision, kind, OPEN, CLOSED),
    )


async def is_overdue(
    db: aiosqlite.Connection,
    task_id: int,
    generation: int,
    revision: int,
    kind: str,
    hours: int,
) -> bool:
    """Открытое ожидание длится не меньше ``hours`` часов."""
    rows = await fetchall(
        db,
        "SELECT 1 FROM delivery_wait_deadlines WHERE task_id=? "
        "AND submission_generation=? AND verdict_revision=? AND kind=? "
        "AND state=? AND julianday(started_at) <= julianday('now', ?)",
        (task_id, generation, revision, kind, OPEN, f"-{int(hours)} hours"),
    )
    return bool(rows)


async def notify_once(
    db: aiosqlite.Connection, task_id: int, generation: int, revision: int, kind: str
) -> bool:
    """open -> notified одним условным UPDATE: True - уведомляет этот вызов."""
    cur = await db.execute(
        "UPDATE delivery_wait_deadlines SET state=?, closed_at=datetime('now') "
        "WHERE task_id=? AND submission_generation=? AND verdict_revision=? "
        "AND kind=? AND state=?",
        (NOTIFIED, task_id, generation, revision, kind, OPEN),
    )
    return cur.rowcount == 1


async def close_open(
    db: aiosqlite.Connection, task_id: int, generation: int, revision: int, kind: str
) -> int:
    """Закрыть открытое ожидание ЭТОГО ключа (проход того же поколения и ревизии)."""
    cur = await db.execute(
        "UPDATE delivery_wait_deadlines SET state=?, closed_at=datetime('now') "
        "WHERE task_id=? AND submission_generation=? AND verdict_revision=? "
        "AND kind=? AND state=?",
        (CLOSED, task_id, generation, revision, kind, OPEN),
    )
    return int(cur.rowcount)


async def has_open(
    db: aiosqlite.Connection, task_id: int, generation: int, revision: int, kind: str
) -> bool:
    """Есть ли открытое ожидание этого ключа: чтение, write-лока не берёт."""
    rows = await fetchall(
        db,
        "SELECT 1 FROM delivery_wait_deadlines WHERE task_id=? "
        "AND submission_generation=? AND verdict_revision=? AND kind=? AND state=?",
        (task_id, generation, revision, kind, OPEN),
    )
    return bool(rows)
