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
from hub import repository_delivery_rerun as rerun_journal
from hub import repository_delivery_wait as journal
from hub.db import write_transaction
from hub.services import delivery_claim, delivery_writes, project_policy

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
        key = _snapshot_key(task)
        if not detail.startswith(orchestration.BASE_FETCH_PENDING_PREFIX):
            await _close_if_open(db, task, key)
            return False
        return await _judge(db, task, key, detail, pr_num)
    except Exception:  # noqa: BLE001 - сбой срока не ломает доставку и не переводит
        log.exception("Poll: delivery wait deadline of #%s not judged", task_id)
        return False


def _snapshot_key(task: dict[str, Any]) -> tuple[int, int, int, str]:
    """Ключ ожидания по СНИМКУ прохода: чей это результат (поколение, ревизия)."""
    return (
        int(task["id"]),
        int(task.get("submission_generation") or 0),
        int(task.get("review_verdict_revision") or 0),
        KIND,
    )


async def _close_if_open(
    db: aiosqlite.Connection, task: dict[str, Any], key: tuple[int, int, int, str]
) -> None:
    """Закрыть открытое ожидание ключа прохода; проход чужого ключа или PR - нет."""
    if not await journal.has_open(db, *key):
        return  # без открытого - ни записи, ни write-лока
    async with write_transaction(db):
        if await _pass_is_live(db, task, key):
            await journal.close_open(db, *key)


async def _judge(
    db: aiosqlite.Connection,
    task: dict[str, Any],
    key: tuple[int, int, int, str],
    detail: str,
    pr_num: Any,
) -> bool:
    from hub.services import orchestration

    hours = await wait_hours(db, key[0])
    async with write_transaction(db):
        # Результат прохода относится к его снимку: если живая строка уже ушла
        # на другое поколение или ревизию, проход устарел и журнала не трогает.
        if not await _pass_is_live(db, task, key):
            return False
        await journal.open_wait(db, *key)
        if hours is None or not await journal.is_overdue(db, *key, hours):
            return False
    written, escalated = await orchestration.record_delivery_outcome(
        db,
        task,
        partial(_escalate, db, key, task, detail),
        fallback=False,
    )
    return bool(written and escalated)


async def _pass_is_live(
    db: aiosqlite.Connection, task: dict[str, Any], key: tuple[int, int, int, str]
) -> bool:
    """Проход ещё про живую задачу: тот же ключ и тот же PR, что в снимке.

    Замена PR без смены поколения ключ не меняет, но причина прохода (CI,
    fetch, мерж) относится к старому PR: такой проход журнала не трогает.
    """
    if await _live_key(db, key[0]) != key:
        return False
    live = await repo.get_task(db, key[0])
    return (
        live is not None
        and int(live["pr_number"] or 0) == int(task.get("pr_number") or 0)
        and (live["submission_sha"] or "") == (task.get("submission_sha") or "")
    )


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
    snap: dict[str, Any],
    detail: str,
) -> bool:
    """Под локом записи: тот же ключ жив, переход выигран, задача передана человеку.

    Всё, что решает переход, читается ЗДЕСЬ, под локом: ключ и PR, доставка,
    бронь и срок. Срок, прочитанный проходом до лока, лишь предварительный:
    второе соединение могло сохранить другой, и нечитаемая политика отменяет
    переход так же, как не вышедший срок.
    """
    pr_num = snap.get("pr_number")
    if not await _pass_is_live(db, snap, key):
        return False  # завершена, возвращена, сдана заново, PR или закрепление ушли
    if await _delivery_moved_on(db, key, int(pr_num or 0)):
        await journal.close_open(db, *key)
        return False  # доставлено соседним путём: переводить нечего
    if await _rerun_holds(db, key, int(pr_num or 0)):
        return False  # живая бронь #1956 обслужит поколение; срок не продлевается
    hours = await wait_hours(db, key[0])
    if hours is None or not await journal.is_overdue(db, *key, hours):
        return False  # срок сменился или не читается: решения нет
    # Память гейта - словарь процесса, лок SQLite её не защищает. Проверка стоит
    # последней и без await до условного UPDATE перехода: окно остаётся только
    # между этим UPDATE и записью статуса (известная граница, как у #1956).
    claim = (key[0], int(pr_num or 0))
    if _merge_in_gate_memory(*claim):
        return False  # мерж идёт или влит, реестр не записан: ждём, часы не трогаем
    # Без await между проверкой и отметкой: мерж, начатый позже, увидит отметку и
    # форж не позовёт (взаимное исключение с merge_if_approval_stands).
    delivery_claim.hold(claim)
    try:
        moved = await _transition(db, key, pr_num, detail, hours)
        if moved:  # тот же синхронный кусок, что и снятие отметки ниже
            delivery_claim.mark_escalated(key[0], key[1])
        return moved
    finally:
        delivery_claim.release(claim)


async def _transition(
    db: aiosqlite.Connection,
    key: tuple[int, int, int, str],
    pr_num: Any,
    detail: str,
    hours: int,
) -> bool:
    """Сам переход, под отметкой ``delivery_claim``: UPDATE, статус, событие."""
    if not await journal.escalate(db, *key):
        return False  # переход уже сделал другой проход
    await delivery_writes.hand_pair_to_human(
        db,
        key[0],
        pr_num,
        f"{detail} — ожидание свежей базы дольше срока ({hours} ч)"
        + await _rerun_outcome_note(db, key),
        REASON,
        "",
        HINT,
    )
    return True


def _merge_in_gate_memory(task_id: int, pr_num: int) -> bool:
    """Мерж гейта по этому PR идёт или влит, а строка реестра ещё не записана."""
    from hub.services import orchestration

    return bool(pr_num) and (task_id, pr_num) in orchestration._gate_merges  # noqa: SLF001


async def _delivery_moved_on(
    db: aiosqlite.Connection, key: tuple[int, int, int, str], pr_num: int
) -> bool:
    """Под локом: PR живой задачи уже доставлен (мерж записан соседним путём).

    Отказ ``base_fetch_pending`` получен ДО лока; за это время соседний путь
    мог записать мерж. Переводить к человеку по прочитанной раньше причине
    нельзя.
    """
    return bool(pr_num) and await repo.pipeline_merge_recorded(db, key[0], pr_num)


async def _rerun_holds(
    db: aiosqlite.Connection, key: tuple[int, int, int, str], pr_num: int
) -> bool:
    """Живая бронь автоперезапуска (#1956) этого поколения и PR, ещё в своём сроке.

    Бронь другого PR или при выключенной политике #1956 закрывает как
    ``superseded`` на проходе, до которого отказ fetch не доходит, - держать
    ею срок нельзя. Политика не прочиталась - бронь держит (не «off»).

    Просроченная бронь не держит: её срок судит #1956 на проходе, до которого
    при отказе fetch базы дело не доходит, и бессрочная бронь превратила бы
    срок этой задачи в ничто.
    """
    from hub.services import delivery_rerun

    if await delivery_rerun.policy_mode(db, key[0]) is False:
        return False
    return any(
        int(r["submission_generation"]) == key[1]
        and int(r["pr_number"]) == pr_num
        and not delivery_rerun.deadline_passed(r)
        for r in await rerun_journal.open_rows(db, key[0])
    )


async def _rerun_outcome_note(
    db: aiosqlite.Connection, key: tuple[int, int, int, str]
) -> str:
    """Закрытый исход к человеку брони #1956 называется в переходе, а не теряется."""
    from hub.services import delivery_rerun

    row = await rerun_journal.get(db, key[0], key[1])
    if row is None or row["state"] != rerun_journal.CLOSED:
        return ""
    if row["outcome"] not in delivery_rerun.HUMAN_OUTCOMES:
        return ""
    return (
        f". Бронь автоперезапуска CI (#1956) этого поколения закрыта исходом "
        f"{row['outcome']}, упавшие job: {row['failed_jobs']}; инструкция по "
        "возврату — в карточке delivery_rerun"
    )
