"""Снятие захвата задачи, который держит прогон хаба (#1783).

Захват снимает ТОЛЬКО тот, кто доказал, что его держит именно этот прогон:
``tasks.executor_run_id`` (пишется при claim/pair-start сессией implementer с
заказом; NULL у ручных захватов). Пустой ``claimed_by`` и принадлежность
acting-агенту доказательством не считаются: 16–17.10 (my-home #1732) захват
без сессий висел на задаче в running, пока владелец не поправил базу вручную.

Один путь на все освобождения — после сдачи (облако и слот), по вердикту
changes_requested и вторым проходом reaper'а. Он работает в транзакции
вызывающего (коммит — за ним), берёт write-лок ПЕРВЫМ и перечитывает всё под
ним: задачу, прогон, признаки работы. Снятие — условный UPDATE по
``executor_run_id`` и ожидаемому статусу: чужой держатель, взявший задачу
между чтением и записью, строку не совпадёт, и ничего не будет тронуто.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

import aiosqlite

from hub import repository as repo
from hub.db import fetchall
from hub.services.return_running import work_signs

log = logging.getLogger(__name__)

EVENT_CLAIM_RELEASED = "executor_claim_released"
#: Прогон кончился, пока у него были сессии или слот: что закрыто вместе с ним.
SLOT_REASON = "захват снят: прогон хаба #{run} закончен"


async def _lock_for_write(db: aiosqlite.Connection) -> None:
    """Write-лок до чтений: оператор, не совпавший ни с одной строкой, берёт его."""
    await db.execute("UPDATE tasks SET updated_at = updated_at WHERE id = -1")


async def finished_run_of(
    db: aiosqlite.Connection, task: dict[str, Any]
) -> dict[str, Any] | None:
    """Прогон, чей захват держит задача, если он ЗАКОНЧЕН; иначе None.

    Не закончен, чужой по задаче или строки нет — доказательства нет.
    """
    run_id = task.get("executor_run_id")
    if run_id is None:
        return None
    run = await repo.get_executor_run(db, int(run_id))
    if run is None:
        return None
    row = dict(run)
    if int(row["task_id"]) != int(task["id"]) or row["outcome"] == "running":
        return None
    return row


async def drop_run_holdings(
    db: aiosqlite.Connection, task_id: int, run_id: int, *, reason: str = ""
) -> None:
    """Сессии прогона отозвать, слот задачи закрыть; без коммита (#1783).

    Захват и всё, что к нему привязано, уходят вместе: слот без захвата
    блокировал бы повторный запуск (``open_slot`` — признак работы), а живая
    сессия — возврат в работу.
    """
    await db.execute(
        "UPDATE chat_pair_sessions SET revoked_at = datetime('now') "
        "WHERE executor_run_id = ? AND bound_task_id = ? AND revoked_at IS NULL",
        (int(run_id), int(task_id)),
    )
    await db.execute(
        "UPDATE executor_slots SET released_at = datetime('now'), "
        "outcome = 'freed', reason = ? WHERE task_id = ? AND released_at IS NULL",
        (reason or SLOT_REASON.format(run=run_id), int(task_id)),
    )


async def release_run_claim(
    db: aiosqlite.Connection,
    task_id: int,
    *,
    run_id: int,
    statuses: Iterable[str],
    note: str,
    close_own_sessions: bool = False,
    reason: str = "",
) -> bool:
    """Снять захват задачи, доказанно принадлежащий закончившемуся прогону.

    True — снят этим вызовом. Без коммита. ``close_own_sessions`` — отозвать
    живые сессии implementer ЭТОГО прогона и закрыть слот задачи в той же
    транзакции (снятие после сдачи); без него живая сессия — признак работы и
    освобождение отказывает.
    """
    await _lock_for_write(db)
    found = await repo.get_task(db, int(task_id))
    if found is None:
        return False
    task = dict(found)
    states = tuple(statuses)
    if task["status"] not in states or task.get("executor_run_id") != int(run_id):
        return False
    run = await finished_run_of(db, task)
    if run is None or int(run["id"]) != int(run_id):
        return False
    if await work_signs(
        db, task, own_run_id=int(run_id), closing_own_sessions=close_own_sessions
    ):
        return False
    marks = ", ".join("?" for _ in states)
    cursor = await db.execute(
        "UPDATE tasks SET claimed_by = NULL, claim_session_id = NULL, "
        "claimed_at = NULL, executor_run_id = NULL, updated_at = datetime('now') "
        f"WHERE id = ? AND executor_run_id = ? AND status IN ({marks})",  # nosec B608 - placeholders only
        (int(task_id), int(run_id), *states),
    )
    if not cursor.rowcount:
        return False
    await drop_run_holdings(db, int(task_id), int(run_id))
    session = (task.get("claim_session_id") or "").strip()
    if session:
        await db.execute(
            "UPDATE agent_sessions SET current_task_id = NULL "
            "WHERE session_id = ? AND current_task_id = ?",
            (session, int(task_id)),
        )
    await repo.add_task_update(db, int(task_id), "hub", "status", note)
    await repo.insert_event(
        db,
        kind=EVENT_CLAIM_RELEASED,
        task_id=int(task_id),
        actor="hub",
        payload={
            "executor_run_id": int(run_id),
            "run_outcome": run["outcome"],
            "from_status": task["status"],
            "reason": reason,
        },
    )
    return True


async def release_after_run_closed(
    db: aiosqlite.Connection, run_id: int, *, why: str
) -> bool:
    """Исполнение закрыто сдачей (облако: taken_down/finished; слот: сдача принята).

    Статус задачи не меняется — это review (или дальше); снимается только захват.
    Прогон закрыт ТОЛЬКО что в этой же транзакции, поэтому доказательство —
    строка прогона, а не слово вызывающего. Позднее закрытие старого прогона
    условный UPDATE по ``executor_run_id`` не пропустит к захвату нового.
    """
    run = await repo.get_executor_run(db, int(run_id))
    if run is None:
        return False
    return await release_run_claim(
        db,
        int(dict(run)["task_id"]),
        run_id=int(run_id),
        statuses=("claimed", "running", "review", "fix_requested", "needs_decision"),
        close_own_sessions=True,
        note=f"Захват снят: исполнение прогона хаба #{run_id} закрыто ({why}); "
        "сессии прогона отозваны, слот закрыт (#1783).",
        reason=why,
    )


async def release_for_rework(db: aiosqlite.Connection, task_id: int) -> bool:
    """changes_requested вернул задачу в running: захват завершённого прогона снять.

    Задача остаётся в running без держателя — круг починки её видит.
    """
    found = await repo.get_task(db, int(task_id))
    if found is None:
        return False
    task = dict(found)
    run = await finished_run_of(db, task)
    if run is None:
        return False
    return await release_run_claim(
        db,
        int(task_id),
        run_id=int(run["id"]),
        statuses=("running",),
        note=f"Захват снят: вердикт «на доработку» пришёл по задаче, чей захват "
        f"принадлежал закончившемуся прогону хаба #{run['id']} (#1783).",
        reason="changes_requested",
    )


async def stuck_candidates(db: aiosqlite.Connection) -> list[int]:
    """Задачи в running/claimed, чей захват ссылается на ЗАКОНЧЕННЫЙ прогон хаба."""
    rows = await fetchall(
        db,
        "SELECT t.id FROM tasks t JOIN executor_runs r ON r.id = t.executor_run_id "
        "WHERE t.status IN ('running', 'claimed') AND r.outcome <> 'running' "
        "AND r.task_id = t.id ORDER BY t.id",
    )
    return [int(r["id"]) for r in rows]


async def release_stuck_task(db: aiosqlite.Connection, task_id: int) -> bool:
    """Второй проход reaper'а для одной задачи (страховка от тупика); без коммита.

    Кандидата предложило чтение вне write-лока; решает перепроверка под локом
    в ``release_run_claim`` (бронь, чужой держатель, живая работа).
    """
    await _lock_for_write(db)
    found = await repo.get_task(db, int(task_id))
    if found is None:
        return False
    task = dict(found)
    if task["status"] not in ("running", "claimed"):
        return False
    run = await finished_run_of(db, task)
    if run is None:
        return False
    return await release_run_claim(
        db,
        int(task_id),
        run_id=int(run["id"]),
        statuses=(task["status"],),
        note=f"Захват снят: он принадлежал закончившемуся прогону хаба "
        f"#{run['id']} ({run['outcome']}), живых сессий и признаков работы "
        "нет (#1783).",
        reason="stuck_claim",
    )
