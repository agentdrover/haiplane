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
from hub.services import memory_wait
from hub.services.return_running import work_signs

log = logging.getLogger(__name__)

EVENT_CLAIM_RELEASED = "executor_claim_released"
#: Статусы, в которых захват доказанного прогона ещё может стоять и подлежит
#: страховочному снятию: попытка снять при сдаче могла отказать (чужая живая
#: сессия), а статус — уйти дальше (review, needs_decision).
HELD_STATUSES = (
    "claimed",
    "running",
    "review",
    "fix_requested",
    "needs_decision",
    "ci_check",
    "pending_report",
    "completed",
)
#: Прогон кончился, пока у него были сессии или слот: что закрыто вместе с ним.
SLOT_REASON = "захват снят: прогон хаба #{run} закончен"


async def _lock_for_write(db: aiosqlite.Connection) -> None:
    """Write-лок до чтений: оператор, не совпавший ни с одной строкой, берёт его."""
    await db.execute("UPDATE tasks SET updated_at = updated_at WHERE id = -1")


async def finished_run_of(
    db: aiosqlite.Connection, task: dict[str, Any], *, ended: bool = False
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
    if int(row["task_id"]) != int(task["id"]):
        return None
    # ``ended`` — провайдер подтвердил конец прогона, а строка ещё ждёт цену:
    # снятие захвата от учёта стоимости не зависит (#1783, круг 2).
    if row["outcome"] == "running" and not (ended or row.get("cost_wait_since")):
        return None
    return row


async def _slot_is_about_to_claim(
    db: aiosqlite.Connection, run_id: int, session_token: str
) -> bool:
    """Прогон слота ещё возьмёт задачу: заказ не выкуплен либо сессия выкупила, но не брала.

    Это второй исполнитель на той же ветке, если сейчас взять задачу другой
    сессией (#1783, k8). Собственная сессия прогона не мешает себе.
    """
    from hub.services import chat_pair

    if await fetchall(
        db,
        "SELECT 1 FROM executor_orders WHERE executor_run_id = ? "
        f"AND state IN {memory_wait.UNREDEEMED_IN_SQL}",  # nosec B608 - constant
        (int(run_id),),
    ):
        return True
    own = chat_pair.hash_pair_code(session_token) if session_token else ""
    return bool(
        await fetchall(
            db,
            "SELECT 1 FROM chat_pair_sessions WHERE executor_run_id = ? "
            "AND kind = 'implementer' AND claimed_session IS NULL "
            "AND revoked_at IS NULL AND expires_at > datetime('now') "
            "AND token_hash <> ?",
            (int(run_id), own),
        )
    )


async def redeem_conflict(db: aiosqlite.Connection, row: dict[str, Any]) -> str:
    """Код слота нельзя выкупить: у задачи уже есть другой держатель или сессия.

    Под write-локом выкупа. Пусто — можно. Держатель с доказательством ДРУГОГО
    прогона, ручной захват и живая сессия, не принадлежащая этому прогону, — это
    второй исполнитель поверх занятой задачи (#1783, k8).
    """
    run_id, task_id = row.get("executor_run_id"), row.get("bound_task_id")
    if task_id is None:
        return ""
    if run_id is None:
        # Ручной код: поверх выданного, но не выкупленного заказа слоту он встал бы
        # вторым исполнителем; заказ и ручной код не блокируют друг друга навсегда —
        # выкупает первый, второй отказан.
        ordered = await fetchall(
            db,
            "SELECT 1 FROM executor_orders WHERE task_id = ? "
            "AND state IN ('pending', 'delivered', 'waiting_memory', 'redeemed')",
            (int(task_id),),
        )
        return "по задаче выдан заказ исполнителю на слот" if ordered else ""
    task = await repo.get_task(db, int(task_id))
    if task is not None and (
        (task["claimed_by"] or "").strip() or (task["claim_session_id"] or "").strip()
    ):
        if task["executor_run_id"] != int(run_id):
            return f"задачу уже держит {task['claimed_by'] or 'другая сессия'}"
    others = await fetchall(
        db,
        "SELECT 1 FROM chat_pair_sessions s WHERE s.kind = 'implementer' "
        "AND s.bound_task_id = ? AND s.revoked_at IS NULL "
        "AND s.expires_at > datetime('now') AND s.claimed_session IS NULL "
        "AND COALESCE(s.executor_run_id, -1) <> ? AND NOT EXISTS ("
        "  SELECT 1 FROM executor_runs r WHERE r.id = s.executor_run_id "
        "  AND (r.outcome <> 'running' OR r.cost_wait_since IS NOT NULL))",
        (int(task_id), int(run_id)),
    )
    return "у задачи есть живая сессия другого исполнителя" if others else ""


async def refuse_over_live_run(
    db: aiosqlite.Connection,
    task_id: int,
    caller_run_id: int | None,
    session_token: str = "",
) -> None:
    """claim/pair-start под write-локом: задачу нельзя взять поверх брони хаба (#1783).

    Круг починки оставляет задачу open с бронью прогона до сетевого create, и
    ручной захват на втором соединении иначе пропускал бы платного агента
    поверх занятой задачи. Облачный прогон (бронь или живой агент) задачу держит;
    свой прогон звонящего (по токену его сессии), прогон слота (его держит заказ
    хосту) и брошенная бронь без агента не мешают. Исключение — HTTP 409.
    """
    from fastapi import HTTPException

    from hub.services.executor_launch import _reservation_abandoned

    # Write-лок ДО чтения: claim без session_id его не берёт, и бронь, закоммитенная
    # на втором соединении между проверкой и переходом, иначе прошла бы (#1783).
    await _lock_for_write(db)
    from hub.services import chat_pair

    if await chat_pair.ended_run_session(db, session_token, task_id):
        raise HTTPException(
            409,
            f"Task #{task_id}: прогон этой сессии уже закончен — захватывать нечем (#1783)",
        )
    rows = await fetchall(
        db,
        "SELECT * FROM executor_runs WHERE task_id = ? AND outcome = 'running' "
        "AND cost_wait_since IS NULL ORDER BY id",
        (int(task_id),),
    )
    for raw in rows:
        row = dict(raw)
        if caller_run_id is not None and int(row["id"]) == int(caller_run_id):
            continue
        if row.get("channel", "cloud") == "slot" and not await _slot_is_about_to_claim(
            db, int(row["id"]), session_token
        ):
            # Заказ выкуплен и сессия хоста уже брала задачу (или её нет): прогон
            # держит она, закрывает sweep заказов — ручная передача задачи (человек
            # отпустил, взял другой агент) остаётся возможной.
            continue
        if (
            row.get("channel", "cloud") != "slot"
            and not row["agent_id"]
            and _reservation_abandoned(row)
        ):
            continue
        raise HTTPException(
            409,
            f"Task #{task_id}: по ней идёт прогон исполнителя хаба #{row['id']} "
            "(бронь или заказ) — захватить её нельзя, пока он не закончен (#1783)",
        )


async def expire_claim(db: aiosqlite.Connection, task: dict[str, Any]) -> bool:
    """Истёкшая аренда: освободить ИМЕННО выбранный захват, не новый (#1783).

    Выборка sweep'а читалась вне write-лока; захват успевали отпустить и взять
    заново. Под локом сверяются статус и время claim; без совпадения ничего не
    пишется. Без коммита.
    """
    await _lock_for_write(db)
    live = await repo.get_task(db, int(task["id"]))
    if live is None or live["status"] != "claimed":
        return False
    if live["claimed_at"] != task.get("claimed_at"):
        return False
    await repo.update_task(
        db,
        int(task["id"]),
        status="open",
        claimed_by=None,
        claim_session_id=None,
        claimed_at=None,
    )
    return True


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
        "outcome = 'freed', reason = ? WHERE task_id = ? AND released_at IS NULL "
        "AND executor_run_id = ?",
        (reason or SLOT_REASON.format(run=run_id), int(task_id), int(run_id)),
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
    run_ended: bool = False,
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
    run = await finished_run_of(db, task, ended=run_ended)
    if run is None or int(run["id"]) != int(run_id):
        return False
    if await work_signs(
        db,
        task,
        own_run_id=int(run_id),
        closing_own_sessions=close_own_sessions,
        own_run_ended=run_ended,
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
    if task["status"] == "claimed":
        # Прогон взял задачу и закончился, не начав работу (pair-start не было):
        # без держателя и без срока аренды она осталась бы claimed навсегда, и
        # sweep аренды её уже не видит — возвращается в очередь, как вернул бы он.
        await repo.transition_status_if(
            db, int(task_id), expected_from="claimed", new_status="open"
        )
    session = (task.get("claim_session_id") or "").strip()
    if session:
        await db.execute(
            "UPDATE agent_sessions SET current_task_id = NULL "
            "WHERE session_id = ? AND current_task_id = ?",
            (session, int(task_id)),
        )
    # #1817: прогон заказа хоста: итоговую запись даёт закрытие заказа, не снятие захвата.
    if not await fetchall(
        db,
        "SELECT 1 FROM executor_orders WHERE executor_run_id = ?",
        (int(run_id),),
    ):
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
    db: aiosqlite.Connection, run_id: int, *, why: str, run_ended: bool = False
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
        statuses=HELD_STATUSES,
        close_own_sessions=True,
        note=f"Захват снят: исполнение прогона хаба #{run_id} закрыто ({why}); "
        "сессии прогона отозваны, слот закрыт (#1783).",
        reason=why,
        run_ended=run_ended,
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
        "WHERE t.status IN ('claimed', 'running', 'review', 'fix_requested', "
        "'needs_decision', 'ci_check', 'pending_report', 'completed') AND (r.outcome <> 'running' OR r.cost_wait_since IS NOT NULL) "
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
    if task["status"] not in HELD_STATUSES:
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
