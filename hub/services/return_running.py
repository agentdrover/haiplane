"""Возврат commit-задачи из running в open, когда за ней нет работы (#1675).

``release_task`` работает только из claimed, ``return_to_work`` (#1356) — из
review и fix_requested. Задача, оставшаяся в running после ухода исполнителя
(pair-start из open не берёт claimed_by), выхода в очередь не имела: #404
провисела так с 03.10 по 11.10 и ушла в архив.

Возврат здесь допустим, только если нет НИ ОДНОГО признака работы. Сам статус
running признаком не считается — ради него путь и сделан. Признаки читаются и
переход пишется в ОДНОЙ write-транзакции (BEGIN IMMEDIATE) с перечиткой строки:
признак, появившийся после внешнего чтения, виден проверке под замком. Внутри
транзакции нет внешних вызовов (git, сеть, реестр диспетчера): job считается
по самому полю, а не по статусу в реестре, поэтому ``abandon_active_job`` отказ
не обходит.
"""

from __future__ import annotations

from typing import Any

import aiosqlite
from fastapi import HTTPException

from hub import repository as repo
from hub.actionable_errors import return_running_has_work_detail
from hub.db import fetchall, write_transaction
from hub.models import TaskReturnToWork, TaskView
from hub.services.orchestrator_queue import current_wait
from hub.services.sessions import pair_executor_online

#: Статусы, из которых commit-задача возвращается ПРИ ОТСУТСТВИИ работы. Это
#: отдельный набор: ``lifecycle.RETURN_TO_WORK_STATUSES`` читает ещё и
#: ``executor_launch`` (оркестратор), и его расширение изменило бы очередь.
RETURN_RUNNING_STATUSES = frozenset({"running"})


async def work_signs(
    db: aiosqlite.Connection,
    task: dict[str, Any],
    *,
    own_run_id: int | None = None,
    closing_own_sessions: bool = False,
    own_run_ended: bool = False,
) -> list[dict]:
    """Признаки работы у задачи: ``[{"sign": код, "detail": сведения}]``.

    Читает на соединении вызывающего, то есть внутри его транзакции. Пустой
    список — работы нет. Порядок фиксирован, чтобы отказ был воспроизводим.

    ``own_run_id`` (#1783) — закрываемое исполнение: прогон хаба, чей
    ``tasks.executor_run_id`` совпал с этим числом. Признаки, доказанно
    принадлежащие ЕМУ — захват (claimed_by, claim_session_id и онлайн-сессия
    реестра по нему) и его слот, — не считаются: их освобождающий путь закроет
    сам. Сессии implementer этого прогона не считаются только при
    ``closing_own_sessions`` (снятие после сдачи отзывает их в той же
    транзакции); иначе живая сессия — признак. Всё прочее (job, чужая живая сессия, чужой
    идущий прогон, объявленное ожидание) остаётся признаком. Чужой
    ``executor_run_id`` в задаче исключений не даёт.
    """
    task_id = int(task["id"])
    own = (
        own_run_id is not None
        and task.get("executor_run_id") is not None
        and int(task["executor_run_id"]) == int(own_run_id)
    )
    signs: list[dict] = []

    def add(sign: str, detail: str) -> None:
        signs.append({"sign": sign, "detail": detail})

    if (task.get("claimed_by") or "").strip() and not own:
        add("claimed_by", str(task["claimed_by"]).strip())
    for field in ("job_id", "review_job_id"):
        if (task.get(field) or "").strip():
            add(field, str(task[field]).strip())
    for row in await fetchall(
        db,
        "SELECT channel, slot, executor_run_id FROM executor_slots "
        "WHERE task_id = ? AND released_at IS NULL ORDER BY id",
        (task_id,),
    ):
        # Свой — только слот ЭТОГО прогона (#1783, круг 2): слот другого
        # держателя или без хозяина остаётся признаком работы.
        if own and row["executor_run_id"] == own_run_id:
            continue
        add("open_slot", f"{row['channel']}:{row['slot'] or '-'}")
    for row in await fetchall(
        db,
        "SELECT id, agent_id, run_id FROM executor_runs "
        "WHERE task_id = ? AND outcome = 'running' AND cost_wait_since IS NULL "
        "ORDER BY id",
        (task_id,),
    ):
        if own and own_run_ended and row["id"] == own_run_id:
            continue  # провайдер сказал «конец», строка ждёт только цену (#1783)
        add(
            "executor_run",
            f"run #{row['id']} agent_id={row['agent_id'] or '-'} "
            f"run_id={row['run_id'] or '-'}",
        )
    for row in await fetchall(
        db,
        "SELECT id, channel, slot, executor_run_id FROM chat_pair_sessions "
        "WHERE kind = 'implementer' AND bound_task_id = ? AND revoked_at IS NULL "
        "AND expires_at > datetime('now') ORDER BY id",
        (task_id,),
    ):
        if own and closing_own_sessions and row["executor_run_id"] is not None:
            if int(row["executor_run_id"]) == int(own_run_id or 0):
                continue
        add("implementer_session", f"сессия #{row['id']}")
    if not own and await pair_executor_online(db, task):
        add("executor_online", str(task.get("claim_session_id") or "").strip())
    wait = current_wait(task)
    if wait:
        add("declared_wait", wait)
    return signs


async def _close_stale_wait(
    db: aiosqlite.Connection, task: dict[str, Any], *, actor: str
) -> None:
    """Просроченное или неполное объявление снимается с записью в ленту."""
    what = (task.get("waiting_for") or "").strip()
    until = (task.get("waiting_until") or "").strip()
    by = (task.get("waiting_declared_by") or "").strip()
    if not (what or until or by):
        return
    task_id = int(task["id"])
    await db.execute(
        "UPDATE tasks SET waiting_for='', waiting_until='', waiting_declared_by='' "
        "WHERE id=?",
        (task_id,),
    )
    await repo.add_task_update(
        db,
        task_id,
        actor,
        "status",
        f"Ожидание снято при возврате в очередь: «{what or '—'}» до "
        f"{until or '—'} (объявил {by or '—'}) — срок вышел или объявление "
        f"неполное ({actor}).",
    )


async def apply_return_to_open(
    db: aiosqlite.Connection,
    task: dict[str, Any],
    *,
    actor: str,
    reason: str,
    manual_verification: str = "",
) -> None:
    """Возврат running → open в транзакции вызывающего; без коммита (#1783).

    Признаки работы вызывающий уже проверил под своим write-локом. Одно тело
    для возврата человеком и для допуска круга починки: два экземпляра
    «что значит вернуть» разошлись бы.
    """
    from hub.db import log_activity
    from hub.hub_instance import mutation_activity_detail
    from hub.services import lifecycle

    task_id = int(task["id"])
    await _close_stale_wait(db, task, actor=actor)
    if not await repo.transition_status_if(
        db, task_id, expected_from="running", new_status="open"
    ):
        raise HTTPException(
            409,
            f"Task #{task_id} left running while being returned to work; "
            "nothing was changed",
        )
    closed_generation = await lifecycle.close_approval_for_rework(db, task_id, task)
    await repo.update_task(
        db,
        task_id,
        claimed_by=None,
        claim_session_id=None,
        claimed_at=None,
        implementer_principal_id=None,
        job_id=None,
        review_job_id=None,
    )
    # Указатель сессии снимается только если он на ЭТУ задачу, а
    # last_seen_at не трогается: возврат делает человек, это не признак
    # жизни сессии (иначе offline-сессия стала бы online и начала бы
    # блокировать возврат других задач через pair_executor_online).
    await db.execute(
        "UPDATE agent_sessions SET current_task_id = NULL "
        "WHERE session_id = ? AND current_task_id = ?",
        (task.get("claim_session_id") or "", task_id),
    )
    await repo.add_task_update(
        db,
        task_id,
        actor,
        "decision",
        lifecycle._return_to_work_note(
            task, actor=actor, reason=reason, from_status="running"
        )
        + (
            f" Проверка владельца: {manual_verification}" if manual_verification else ""
        ),
    )
    await repo.insert_event(
        db,
        kind="task_returned_to_work",
        task_id=task_id,
        actor=actor,
        payload={
            "from_status": "running",
            "reason": reason,
            "previous_holder": task.get("claimed_by") or "",
            "previous_session": task.get("claim_session_id") or "",
            "previous_principal_id": task.get("implementer_principal_id"),
            "closed_verdict_generation": closed_generation,
            "abandoned_jobs": [],
            **(
                {"manual_verification": manual_verification}
                if manual_verification
                else {}
            ),
        },
    )
    await log_activity(
        db,
        "task_returned_to_work",
        f"Task #{task_id} returned to work from running by {actor}",
        detail=mutation_activity_detail(),
        commit=False,
    )


async def return_running_to_open(
    db: aiosqlite.Connection, task_id: int, body: TaskReturnToWork, *, actor: str
) -> TaskView:
    """Человек возвращает running-задачу без признаков работы в open."""
    from hub.services import lifecycle

    async with write_transaction(db):
        row = await repo.get_task(db, task_id)
        if not row:
            raise HTTPException(404, "task not found")
        task = dict(row)
        if task["status"] not in RETURN_RUNNING_STATUSES:
            raise HTTPException(
                409,
                f"Task #{task_id} moved from running to {task['status']} "
                "while being returned to work; nothing was changed",
            )
        recovery = body.manual_claim_recovery
        if recovery:
            updates = await repo.get_task_updates(db, task_id)
            last_update_id = updates[-1]["id"] if updates else 0
            if (
                recovery.expected_last_update_id != last_update_id
                or recovery.expected_status != task["status"]
                or recovery.expected_holder != (task.get("claimed_by") or "")
                or recovery.expected_session != (task.get("claim_session_id") or "")
                or recovery.expected_claimed_at != (task.get("claimed_at") or "")
                or task.get("result_kind") == "state"
                or task.get("executor_run_id") is not None
                or task.get("claim_nonce") is not None
            ):
                raise HTTPException(
                    409, "Снимок захвата изменился или захват не является ручным"
                )
        signs = await work_signs(db, task)
        if recovery:
            signs = [sign for sign in signs if sign["sign"] != "claimed_by"]
        if signs:
            raise HTTPException(409, detail=return_running_has_work_detail(signs))
        await apply_return_to_open(
            db,
            task,
            actor=actor,
            reason=body.reason,
            manual_verification=recovery.verification if recovery else "",
        )
    row = await repo.get_task(db, task_id)
    updates = await repo.get_task_updates(db, task_id)
    return lifecycle.row_to_task(row, updates=updates)  # type: ignore[arg-type]
