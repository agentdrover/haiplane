"""SQL брони автоперезапуска упавших job CI на свежей базе (#1956).

Рядом с ``hub.repository``, а не в нём: файл у потолка размера
(docs/agent-context/file-size-budget.json).

Ключ строки - пара (задача, поколение сдачи). Состояния идут только вперёд
(armed -> reserved -> requested -> closed), и КАЖДЫЙ переход - один условный
UPDATE с ``WHERE state = <прежнее>``: резерв, который займёт ровно один из двух
соединений, и поздний ответ POST, который не перепишет уже закрытый исход.
Нигде здесь нет коммита: транзакцией владеет вызывающий (``write_transaction``).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import aiosqlite

from hub.db import fetchall

ARMED = "armed"
RESERVED = "reserved"
REQUESTED = "requested"
CLOSED = "closed"
#: Бронь, по которой POST возможен или уже ушёл и результат ещё не закрыт.
OPEN_STATES = (RESERVED, REQUESTED)


#: «Имена не относятся к исходу» (superseded, зелёный исход): в отличие от None,
#: который значит «список не прочитан».
NOT_APPLICABLE: Any = object()


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
    base_from_sha: str,
) -> None:
    """Записать происхождение: гейт слил базу в ветку в этом поколении (#1956).

    ``INSERT OR IGNORE``: второе слияние базы в том же поколении (другой PR,
    другая голова) ничего не меняет - лимит один на поколение, и уже
    потраченную бронь ``armed`` не возвращает.
    """
    await db.execute(
        "INSERT OR IGNORE INTO delivery_reruns "
        "(task_id, submission_generation, pr_number, base_from_sha) "
        "VALUES (?, ?, ?, ?)",
        (int(task_id), int(generation), int(pr_number), base_from_sha),
    )


async def get(
    db: aiosqlite.Connection, task_id: int, generation: int
) -> dict[str, Any] | None:
    rows = await fetchall(
        db,
        "SELECT * FROM delivery_reruns WHERE task_id=? AND submission_generation=?",
        (int(task_id), int(generation)),
    )
    return dict(rows[0]) if rows else None


async def latest(db: aiosqlite.Connection, task_id: int) -> dict[str, Any] | None:
    """Последняя бронь задачи (для карточки): старшее поколение, потом id."""
    rows = await fetchall(
        db,
        "SELECT * FROM delivery_reruns WHERE task_id=? "
        "ORDER BY submission_generation DESC, id DESC LIMIT 1",
        (int(task_id),),
    )
    return dict(rows[0]) if rows else None


async def open_rows(db: aiosqlite.Connection, task_id: int) -> list[dict[str, Any]]:
    rows = await fetchall(
        db,
        "SELECT * FROM delivery_reruns WHERE task_id=? "
        "AND state IN ('reserved', 'requested')",
        (int(task_id),),
    )
    return [dict(r) for r in rows]


async def reserve(
    db: aiosqlite.Connection,
    task_id: int,
    generation: int,
    *,
    pr_number: int,
    base_from_sha: str,
    repo: str,
    workflow: str,
    head_sha: str,
    run_id: str,
    attempt_before: int,
    text: str,
) -> int | None:
    """Занять единственный POST поколения: id строки или None, если занят/потрачен.

    Строка создаётся здесь, если происхождения не записали заранее (политика
    была выключена в момент слияния базы, а признак есть в ленте событий).
    Переход ``armed -> reserved`` - один UPDATE: проигравший гонку видит
    rowcount 0 и POST не делает.
    """
    await arm(db, task_id, generation, pr_number=pr_number, base_from_sha=base_from_sha)
    cur = await db.execute(
        "UPDATE delivery_reruns SET state='reserved', repo=?, workflow=?, "
        "head_sha=?, run_id=?, attempt_before=?, pr_number=?, "
        "requested_at=datetime('now'), "
        "history=json_insert(history, '$[#]', json(?)) "
        "WHERE task_id=? AND submission_generation=? AND state='armed'",
        (
            repo,
            workflow,
            head_sha,
            run_id,
            int(attempt_before),
            int(pr_number),
            _event(text, "reserved"),
            int(task_id),
            int(generation),
        ),
    )
    if cur.rowcount != 1:
        return None
    rows = await fetchall(
        db,
        "SELECT id FROM delivery_reruns WHERE task_id=? AND submission_generation=?",
        (int(task_id), int(generation)),
    )
    return int(rows[0][0])


async def mark_requested(db: aiosqlite.Connection, row_id: int, text: str) -> bool:
    """POST принят: reserved -> requested. Закрытую бронь не трогает."""
    cur = await db.execute(
        "UPDATE delivery_reruns SET state='requested', "
        "history=json_insert(history, '$[#]', json(?)) "
        "WHERE id=? AND state='reserved'",
        (_event(text, "requested"), int(row_id)),
    )
    return cur.rowcount == 1


async def close(
    db: aiosqlite.Connection,
    row_id: int,
    outcome: str,
    text: str,
    *,
    failed_jobs: Any = NOT_APPLICABLE,
    from_states: tuple[str, ...] = OPEN_STATES,
) -> bool:
    """Закрыть открытую бронь исходом; уже закрытая не перезаписывается."""
    marks = ", ".join("?" for _ in from_states)
    cur = await db.execute(
        "UPDATE delivery_reruns SET state='closed', outcome=?, "
        "closed_at=datetime('now'), failed_jobs=?, "
        "history=json_insert(history, '$[#]', json(?)) "
        f"WHERE id=? AND state IN ({marks})",
        (
            outcome,
            "[]"
            if failed_jobs is NOT_APPLICABLE
            else json.dumps(failed_jobs, ensure_ascii=False),  # None -> 'null'
            _event(text, outcome),
            int(row_id),
            *from_states,
        ),
    )
    return cur.rowcount == 1
