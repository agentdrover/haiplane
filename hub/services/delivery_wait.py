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
  закрывают ожидание (иная причина - любой ответ гейта на проходе, кроме
  «поиск замены PR не ответил»: он о гейте ничего не говорит).

Только ``base_fetch_pending``. Остальные временные причины доставки срока не
получают. Срок проверяет pair-свип поллера - единственный повторяющийся драйвер
доставки; отчёт агента об окончании работы проходит тот же гейт, но срока не
судит. «Не прочиталось» (политика, строка таймера) не равно «срок вышел» и не
равно «срока нет»: нечитаемая политика или сбой таймера - ожидание
продолжается, перевода нет; нет ключа - умолчание. Транзакцией владеет
вызывающий: свои записи здесь идут через ``write_transaction`` и откатываются
только если транзакцию открыли мы - чужие записи прохода (бронь #1956, события
обновления ветки) этот модуль не откатывает.
"""

from __future__ import annotations

import json
import logging
from functools import partial
from typing import Any

import aiosqlite

from hub import repository as repo
from hub import repository_delivery_wait as journal
from hub.db import write_transaction
from hub.services import delivery_writes, project_policy

log = logging.getLogger("hub")

KIND = "base_fetch_pending"
REASON = "base_fetch_deadline"
HINT = (
    " Хаб не может получить свежую базовую ветку в клон (fetch). Устраните "
    "отказ — доступ хаба к репозиторию — и решите: вернуть в работу или "
    "повторить доставку (REST POST /api/tasks/<id>/decide)."
)


async def wait_hours(db: aiosqlite.Connection, task_id: int) -> int | None:
    """Срок в часах: политика ЖИВОГО проекта; None - политика не прочиталась.

    Нет проекта или нет ключа - умолчание сервера. «Не прочиталась» не равно
    «нет ключа»: у проекта мог стоять срок длиннее, и перевод по умолчанию
    нарушил бы его выбор.
    """
    try:
        project = await repo.resolve_project_for_task(db, task_id)
        policy = json.loads((project["gate_policy"] if project else "") or "{}")
    except Exception:  # noqa: BLE001 - «не прочиталась» не равно «нет ключа»
        log.exception("policy of task #%s unreadable, no deadline judgement", task_id)
        return None
    if not isinstance(policy, dict):
        return None
    return project_policy.base_fetch_wait_hours_of(policy)


async def settle(
    db: aiosqlite.Connection, task: dict[str, Any], detail: str, pr_num: Any
) -> bool:
    """Итог прохода доставки для срока: True - задача ушла к человеку, стоп.

    ``detail`` - причина отказа гейта; пусто - гейт не отказал. Не
    ``base_fetch_pending`` закрывает ожидание; ``base_fetch_pending`` ведёт его
    и после срока переводит задачу.
    """
    from hub.services import orchestration

    task_id = int(task["id"])
    try:
        if not detail.startswith(orchestration.BASE_FETCH_PENDING_PREFIX):
            await _close_if_open(db, task_id)
            return False
        return await _judge(db, task, detail, pr_num)
    except Exception:  # noqa: BLE001 - сбой срока не ломает доставку и не переводит
        log.exception("Poll: delivery wait deadline of #%s not judged", task_id)
        return False


async def _close_if_open(db: aiosqlite.Connection, task_id: int) -> None:
    """Закрыть открытое ожидание; без открытого - ни записи, ни write-лока."""
    if not await journal.has_open(db, task_id, KIND):
        return
    async with write_transaction(db):
        await journal.close_open(db, task_id, KIND)


async def _judge(
    db: aiosqlite.Connection, task: dict[str, Any], detail: str, pr_num: Any
) -> bool:
    from hub.services import orchestration

    task_id = int(task["id"])
    hours = await wait_hours(db, task_id)
    async with write_transaction(db):
        # Живая строка и таймер читаются и пишутся под одним локом: ключ
        # (поколение, ревизия) не может смениться между чтением и записью.
        key = await _live_key(db, task_id)
        if key is None:
            return False  # устаревший снимок: свип уже не про эту задачу
        await journal.open_wait(db, *key)
        if hours is None or not await journal.is_overdue(db, *key, hours):
            return False
    written, escalated = await orchestration.record_delivery_outcome(
        db,
        task,
        partial(_escalate, db, key, pr_num, detail, hours),
        fallback=False,
    )
    return bool(written and escalated)


async def _live_key(
    db: aiosqlite.Connection, task_id: int
) -> tuple[int, int, int, str] | None:
    """Ключ ожидания по ЖИВОЙ строке; None - задача не в running."""
    live = await repo.get_task(db, task_id)
    if live is None or live["status"] != "running":
        return None
    return (
        task_id,
        int(live["submission_generation"] or 0),
        int(live["review_verdict_revision"] or 0),
        KIND,
    )


async def _escalate(
    db: aiosqlite.Connection,
    key: tuple[int, int, int, str],
    pr_num: Any,
    detail: str,
    hours: int,
) -> bool:
    """Под локом записи: тот же ключ жив, переход выигран, задача передана человеку."""
    if await _live_key(db, key[0]) != key:
        return False  # завершена, возвращена или сдана заново с прохода до лока
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
