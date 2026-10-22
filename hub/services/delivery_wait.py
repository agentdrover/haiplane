"""#1992: срок ожидания доставки, пока хаб не может получить свежую базу.

После #1983 любое расхождение голов при отказе fetch базы - временная причина
``base_fetch_pending``: поллер повторял её без предела, и одобренная задача висела
в running, пока кто-нибудь случайно не заметил. Здесь ожидание получает срок:

* отсчёт долговечный - строка ``delivery_wait_deadlines`` на (задача, поколение
  сдачи, ревизия вердикта); ``started_at`` пишется один раз, рестарт и смена
  текста ошибки его не двигают;
* срок - ключ политики проекта ``base_fetch_wait_hours`` (24 ч по умолчанию);
* просрочка проверяет одобрение заново и переводит задачу в needs_decision ОДИН
  раз тем же путём, что любой отказ гейта на pair-свипе
  (``delivery_writes.hand_pair_to_human`` -> ``emit_needs_decision``);
* успех, иная причина отказа, новое поколение или новая ревизия вердикта
  закрывают ожидание.

Только ``base_fetch_pending``. Остальные временные причины доставки срока не
получают. Срок проверяет pair-свип поллера - единственный повторяющийся драйвер
доставки; отчёт агента об окончании работы проходит тот же гейт, но срока не
судит. «Не прочиталось» (политика, строка таймера) не равно «срок вышел» и не
равно «срока нет»: ожидание продолжается по умолчанию, перевода нет.
"""

from __future__ import annotations

import json
import logging
from functools import partial
from typing import Any

import aiosqlite

from hub import repository as repo
from hub import repository_delivery_wait as journal
from hub.services import delivery_writes, project_policy

log = logging.getLogger("hub")

KIND = "base_fetch_pending"
REASON = "base_fetch_deadline"
HINT = (
    " Хаб не может получить свежую базовую ветку в клон (fetch). Устраните "
    "отказ — доступ хаба к репозиторию — и решите: вернуть в работу или "
    "повторить доставку (REST POST /api/tasks/<id>/decide)."
)


async def wait_hours(db: aiosqlite.Connection, task_id: int) -> int:
    """Срок в часах: политика ЖИВОГО проекта, иначе умолчание (не «без срока»)."""
    try:
        project = await repo.resolve_project_for_task(db, task_id)
        policy = json.loads((project["gate_policy"] if project else "") or "{}")
    except Exception:  # noqa: BLE001 - нечитаемая политика = умолчание, не отмена срока
        log.exception("policy of task #%s unreadable, default wait deadline", task_id)
        policy = {}
    return project_policy.base_fetch_wait_hours_of(policy)


async def settle(
    db: aiosqlite.Connection, task: dict[str, Any], detail: str, pr_num: Any
) -> bool:
    """Итог прохода доставки для срока: True - задача ушла к человеку, стоп.

    ``detail`` - причина отказа гейта; пусто - гейт не отказал. Не
    ``base_fetch_pending`` закрывает ожидание; ``base_fetch_pending`` ведёт его
    и после срока переводит задачу.
    """
    from hub.services import orchestration as services

    task_id = int(task["id"])
    try:
        live = await repo.get_task(db, task_id)
        if live is None or live["status"] != "running":
            return False  # устаревший снимок: свип уже не про эту задачу
        if not detail.startswith(services.BASE_FETCH_PENDING_PREFIX):
            if await journal.close_open(db, task_id, KIND):
                await db.commit()
            return False
        key = (
            task_id,
            int(live["submission_generation"] or 0),
            int(live["review_verdict_revision"] or 0),
            KIND,
        )
        await journal.open_wait(db, *key)
        hours = await wait_hours(db, task_id)
        if not await journal.is_overdue(db, *key, hours):
            await db.commit()
            return False
        written, escalated = await services.record_delivery_outcome(
            db,
            task,
            partial(_escalate, db, key, pr_num, detail, hours),
            fallback=False,
        )
        return bool(written and escalated)
    except Exception:  # noqa: BLE001 - сбой срока не ломает доставку и не переводит
        log.exception("Poll: delivery wait deadline of #%s not judged", task_id)
        await db.rollback()
        return False


async def _escalate(
    db: aiosqlite.Connection,
    key: tuple[int, int, int, str],
    pr_num: Any,
    detail: str,
    hours: int,
) -> bool:
    """В транзакции с проверкой одобрения: выиграть переход и передать человеку."""
    if not await journal.escalate(db, *key):
        return False  # переход уже сделал другой проход
    await delivery_writes.hand_pair_to_human(
        db,
        key[0],
        pr_num,
        f"{detail} — ожидание свежей базы дольше срока ({hours} ч)",
        REASON,
        "",
        HINT,
    )
    return True
