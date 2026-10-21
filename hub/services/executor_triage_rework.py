"""Разбор в тени, второй вход (#1947): задача вернулась с ревью на доработку.

После CHANGES_REQUESTED и return-to-work ВМ k8, сдавшая задачу, припаркована, а
будит её только новый заказ по кнопке владельца. Хаб знает: последний host-заказ
задачи закрыт сдачей на поколении N, ``latest_review`` - changes_requested на N,
задача открыта и свободна. Жива ли ВМ, хаб не знает. Рекомендация
``rework_on_host`` («разбудить ВМ на доработку по находкам») пишется в ту же
таблицу, что и #1942, с тем же followup и замером; совпадение - ``new_order``.

Один вход - один снимок (``executor_triage._read_snapshot``): заказ, задача, живые
сессии и прогоны читаются одним SELECT. Хаб ничего не запускает, не выпускает
код и не меняет статус. Рекомендация одна на заказ (UNIQUE(order_id) таблицы), а
заказ закрыт сдачей одного поколения, поэтому - одна на (задача, поколение
сдачи). Заказ, закрытый до границы активации разбора, - история.
"""

from __future__ import annotations

import json
from typing import Any, Final

import aiosqlite

from hub import repository as repo
from hub.db import fetchall
from hub.services import executor_triage as triage

CLASS_REWORK: Final[int] = 5
CLASS_REWORK_NAME: Final[str] = "rework"
_SAVEPOINT: Final[str] = "executor_triage_rework"
CLOSED_BY_SUBMISSION: Final[str] = "submitted"
CHANGES_REQUESTED: Final[str] = "changes_requested"


def findings_count(raw: Any) -> int | None:
    """Число находок вердикта; ``None`` - не прочиталось (не ноль)."""
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return None
    return len(data) if isinstance(data, list) else None


def applies(snap: dict[str, Any]) -> bool:
    """Задача вернулась с ревью на доработку; чистая функция по снимку."""
    generation = int(snap["submission_generation"] or 0)
    return (
        triage.idle(snap)
        and snap["state"] == "closed"
        and snap["close_reason"] == CLOSED_BY_SUBMISSION
        and snap["review_verdict"] == CHANGES_REQUESTED
        and int(snap["generation"]) == generation
        and int(snap["review_verdict_generation"] or 0) == generation
    )


def _entry_text(snap: dict[str, Any], count: int | None) -> str:
    found = "неизвестно" if count is None else str(count)
    return (
        f"Разбор прогона k8 (тень, #1947): задача вернулась с ревью (вердикт "
        f"changes_requested, версия {int(snap['review_verdict_revision'] or 0)}, "
        f"находок: {found}); последний заказ #{snap['order_id']} закрыт сдачей "
        f"{snap['generation']}. Рекомендация: "
        f"{triage.ACTION_TEXT[triage.ACTION_REWORK_ON_HOST]}. "
        "Хаб ничего не запустил; решение за человеком."
    )


async def recommend_for_task(
    db: aiosqlite.Connection, task_id: int
) -> dict[str, Any] | None:
    """Записать рекомендацию «доработка на ВМ»; ``None`` - её нет или она уже есть.

    Без commit: транзакция - вызывающего. Читает свежий снимок задачи.
    """
    snap = await triage._read_snapshot(db, int(task_id))
    if snap is None or not applies(snap):
        return None
    if str(snap["closed_at"] or "") < await triage.activated_at(db):
        return None
    count = findings_count(snap["review_findings"])
    revision = int(snap["review_verdict_revision"] or 0)
    cursor = await db.execute(
        "INSERT OR IGNORE INTO executor_triage_recommendations "
        "(order_id, task_id, triage_class, class_name, action, reason, child_floor, "
        "verdict_revision, findings_count) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)",
        (
            int(snap["order_id"]),
            int(task_id),
            CLASS_REWORK,
            CLASS_REWORK_NAME,
            triage.ACTION_REWORK_ON_HOST,
            "задача вернулась с ревью на доработку",
            revision,
            count,
        ),
    )
    if not cursor.rowcount:
        return None  # вторая вставка: событие и запись ленты уже есть у первой
    project_row = await repo.resolve_project_for_task(db, int(task_id))
    await repo.insert_event(
        db,
        kind=triage.EVENT_RECOMMENDED,
        task_id=int(task_id),
        project_id=int(project_row["id"]) if project_row else None,
        actor="hub",
        payload={
            "order_id": int(snap["order_id"]),
            "action": triage.ACTION_REWORK_ON_HOST,
            "verdict_revision": revision,
            "findings": count,
        },
    )
    await repo.add_task_update(
        db, int(task_id), "hub", "status", _entry_text(snap, count)
    )
    return {"order_id": int(snap["order_id"]), "action": triage.ACTION_REWORK_ON_HOST}


async def recommend_safely(
    db: aiosqlite.Connection, task_id: int
) -> dict[str, Any] | None:
    """``recommend_for_task`` под SAVEPOINT: сбой не мешает return-to-work."""
    return await triage.guarded(
        db, _SAVEPOINT, recommend_for_task(db, task_id), f"rework #{task_id}"
    )


async def recover_missing(db: aiosqlite.Connection) -> int:
    """Вернувшиеся с ревью задачи без рекомендации (возврат другим путём, сбой)."""
    rows = await fetchall(
        db,
        "SELECT o.task_id FROM executor_orders o JOIN tasks t ON t.id = o.task_id "
        "WHERE o.state = 'closed' AND o.close_reason = ? AND o.closed_at >= ? "
        "AND t.status = 'open' AND t.archived = 0 AND t.review_verdict = ? "
        "AND NOT EXISTS (SELECT 1 FROM executor_triage_recommendations r "
        "                WHERE r.order_id = o.id) "
        "AND o.id = (SELECT MAX(n.id) FROM executor_orders n "
        "            WHERE n.task_id = o.task_id)",
        (CLOSED_BY_SUBMISSION, await triage.activated_at(db), CHANGES_REQUESTED),
    )
    made = 0
    for row in rows:
        if await recommend_safely(db, int(row["task_id"])):
            made += 1
    return made
