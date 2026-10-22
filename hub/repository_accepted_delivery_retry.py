"""SQL автоповтора доставки принятой задачи (#1999).

Рядом с ``hub.repository``, а не в нём: файл у потолка размера. Образец -
``hub.repository_delivery_rerun`` (#1956).

Ключ строки - пара (задача, поколение сдачи). Состояния: ``armed`` (ждёт CI и
попытки) -> ``reserved`` (попытка занята ровно одним проходом) -> ``armed``
(потраченная попытка, ждём дальше) или ``closed`` (исход). КАЖДЫЙ переход - один
условный UPDATE: проигравший гонку видит rowcount 0 и сеть не трогает. Лимиты
(``attempts`` и срок от ``armed_at``) проверяет сам UPDATE брони, поэтому
рестарт, второе соединение и повторное вооружение их не сбрасывают.
Коммита здесь нет: транзакцией владеет вызывающий (``write_transaction``).
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import aiosqlite

from hub.db import fetchall

ARMED = "armed"
RESERVED = "reserved"
CLOSED = "closed"
MAX_ATTEMPTS = 6
WINDOW_HOURS = 24
#: Бронь старше этого числа секунд считается брошенной (процесс умер между
#: бронью и записью исхода): попытка остаётся потраченной, проход свободен.
STALL_SECONDS = 600


def _event(text: str, event: str) -> str:
    return json.dumps(
        {
            "at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S"),
            "actor": "hub",
            "event": event,
            "text": text,
        },
        ensure_ascii=False,
    )


async def arm(
    db: aiosqlite.Connection,
    task_id: int,
    generation: int,
    *,
    pr_number: int,
    project_id: int | None,
    via: str,
    text: str,
) -> bool:
    """Вооружить: True, если строка создана сейчас; повтор ничего не меняет.

    ``INSERT OR IGNORE``: второй base_updated того же поколения не сбрасывает
    ни счётчик, ни срок, и закрытую строку не открывает.
    """
    cur = await db.execute(
        "INSERT OR IGNORE INTO accepted_delivery_retries "
        "(task_id, submission_generation, pr_number, project_id, via, history) "
        "VALUES (?, ?, ?, ?, ?, json_array(json(?)))",
        (
            int(task_id),
            int(generation),
            int(pr_number),
            project_id,
            via,
            _event(text, "armed"),
        ),
    )
    return cur.rowcount == 1


async def latest(db: aiosqlite.Connection, task_id: int) -> dict[str, Any] | None:
    """Последняя строка задачи: старшее поколение, потом id."""
    rows = await fetchall(
        db,
        "SELECT * FROM accepted_delivery_retries WHERE task_id=? "
        "ORDER BY submission_generation DESC, id DESC LIMIT 1",
        (int(task_id),),
    )
    return dict(rows[0]) if rows else None


async def open_rows(db: aiosqlite.Connection, limit: int) -> list[dict[str, Any]]:
    """Не закрытые строки, давно не проверявшиеся первыми (курсор ``last_checked_at``).

    Возраст считает база, а не часы проверяющего. Порядок по ``id`` оставлял
    строки за пределом ``limit`` непроверенными навсегда.
    """
    rows = await fetchall(
        db,
        "SELECT *, (julianday('now') - julianday(armed_at)) * 24.0 AS age_hours, "
        "(julianday('now') - julianday(COALESCE(reserved_at, armed_at))) * 86400.0 "
        "AS reserved_seconds FROM accepted_delivery_retries "
        "WHERE state != 'closed' "
        "ORDER BY last_checked_at IS NOT NULL, last_checked_at, id LIMIT ?",
        (int(limit),),
    )
    return [dict(r) for r in rows]


async def touch(db: aiosqlite.Connection, row_id: int) -> None:
    """Строка проверена в этом проходе (в том числе "ждём"): в хвост очереди."""
    await db.execute(
        "UPDATE accepted_delivery_retries SET last_checked_at=? WHERE id=?",
        (datetime.now(UTC).isoformat(timespec="microseconds"), int(row_id)),
    )


async def reserve(db: aiosqlite.Connection, row_id: int, text: str) -> str | None:
    """Занять попытку ОДНИМ UPDATE: токен брони, или None - попытка не наша.

    Токен пишется в строку; завершающие UPDATE (``settle``, ``close``) идут с
    ``WHERE token=?``, поэтому прежний владелец, у которого бронь перехватили,
    не меняет ни состояние, ни счётчик нового.

    Условие целиком в UPDATE: строка ``armed`` либо ``reserved`` и брошенная,
    попыток меньше лимита, срок от первого вооружения не вышел. Попытка
    тратится здесь и возвращается только явным ``release``.
    """
    token = uuid.uuid4().hex
    cur = await db.execute(
        "UPDATE accepted_delivery_retries SET state='reserved', "
        "attempts=attempts+1, reserved_at=datetime('now'), token=?, "
        "history=json_insert(history, '$[#]', json(?)) "
        "WHERE id=? AND attempts < ? "
        "AND armed_at > datetime('now', ?) "
        "AND (state='armed' OR (state='reserved' "
        "AND reserved_at <= datetime('now', ?)))",
        (
            token,
            _event(text, "reserved"),
            int(row_id),
            MAX_ATTEMPTS,
            f"-{WINDOW_HOURS} hours",
            f"-{STALL_SECONDS} seconds",
        ),
    )
    return token if cur.rowcount == 1 else None


async def settle(
    db: aiosqlite.Connection, row_id: int, text: str, *, refund: bool, token: str
) -> bool:
    """Попытка кончилась без исхода: reserved -> armed; ``refund`` возвращает её."""
    cur = await db.execute(
        "UPDATE accepted_delivery_retries SET state='armed', "
        "attempts=attempts - ?, history=json_insert(history, '$[#]', json(?)) "
        "WHERE id=? AND state='reserved' AND token=?",
        (
            1 if refund else 0,
            _event(text, "refunded" if refund else "spent"),
            int(row_id),
            token,
        ),
    )
    return cur.rowcount == 1


async def close(
    db: aiosqlite.Connection,
    row_id: int,
    outcome: str,
    text: str,
    *,
    token: str = "",
) -> bool:
    """Закрыть исходом; уже закрытая не перезаписывается.

    С ``token`` закрывает только владелец своей брони: перехваченная у него
    строка остаётся тому, кто взял её после. Без токена (стоп до брони) - любую
    не закрытую.
    """
    cur = await db.execute(
        "UPDATE accepted_delivery_retries SET state='closed', outcome=?, "
        "closed_at=datetime('now'), history=json_insert(history, '$[#]', json(?)) "
        "WHERE id=? AND state != 'closed' AND (? = '' OR token = ?)",
        (outcome, _event(text, outcome), int(row_id), token, token),
    )
    return cur.rowcount == 1
