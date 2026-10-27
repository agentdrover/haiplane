"""#1992: срок ожидания доставки, пока хаб не может получить свежую базу.

После #1983 любое расхождение голов при отказе fetch базы - временная причина
``base_fetch_pending``: поллер повторял её без предела, и одобренная задача висела
в running, пока кто-нибудь случайно не заметил. Здесь ожидание получает срок:

* отсчёт долговечный - строка ``delivery_wait_deadlines`` на (задача, поколение
  сдачи, ревизия вердикта); ``started_at`` пишется один раз, рестарт и смена
  текста ошибки его не двигают;
* срок - ключ политики проекта ``base_fetch_wait_hours`` (24 ч по умолчанию);
* по истечении срока владелец получает ОДНО уведомление, ожидание закрывается
  состоянием ``notified``. СТАТУС ЗАДАЧИ НЕ МЕНЯЕТСЯ и мерж не блокируется:
  арбитраж «мерж или эскалация» - отдельная задача (решение роадмапа после
  четырёх кругов ревью класса «устаревшая причина гейта = текущее состояние»);
* успех, иная причина отказа, новое поколение или новая ревизия вердикта
  закрывают ожидание (иная причина - любой ответ гейта на проходе, кроме
  «поиск замены PR не ответил»: он о гейте ничего не говорит).

Уведомление: запись ``alert`` в карточке (причина, срок, PR, ссылка на задачу),
событие ``delivery_wait_deadline`` и ``watch_notices.notify_steward("stale")`` -
всё в одной транзакции. ``emit_needs_decision`` НЕ используется: он пишет событие
``needs_decision`` (метрики человеческого гейта, подсказка стюарду звать decide),
а перехода статуса здесь нет.

Только ``base_fetch_pending``. Остальные временные причины доставки срока не
получают. Срок проверяет pair-свип поллера - единственный повторяющийся драйвер
доставки; отчёт агента об окончании работы проходит тот же гейт, но срока не
судит. «Не прочиталось» (политика, строка таймера) не равно «срок вышел» и не
равно «срока нет»: нечитаемая политика или сбой таймера - ожидание
продолжается, уведомления нет; нет ключа - умолчание. Журнал и все решения
читаются и пишутся под ОДНИМ write-локом; транзакцией владеет вызывающий: свои
записи идут через ``write_transaction`` и откатываются только если транзакцию
открыли мы - чужие записи прохода этот модуль не откатывает.
"""

from __future__ import annotations

import contextlib
import json
import logging
from typing import Any

import aiosqlite

from hub import repository as repo
from hub import repository_delivery_rerun as rerun_journal
from hub import repository_delivery_wait as journal
from hub.db import fetchall, write_transaction
from hub.services import project_policy, watch_notices

log = logging.getLogger("hub")

KIND = "base_fetch_pending"
EVENT = "delivery_wait_deadline"
HINT = (
    " Статус задачи не менялся, доставка не заблокирована. Хаб не может получить "
    "свежую базовую ветку в клон (fetch): проверьте доступ хаба к репозиторию."
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
    """Вести ожидание и после срока уведомить владельца; True - уведомление ушло."""
    async with write_transaction(db):
        # Журнал, живая строка, политика и состояние доставки читаются и пишутся
        # под одним локом. Результат прохода относится к его снимку: если живая
        # строка уже ушла на другое поколение, ревизию, PR или закрепление,
        # проход устарел и журнала не трогает.
        if not await _pass_is_live(db, task, key):
            return False
        await journal.open_wait(db, *key)
        hours = await wait_hours(db, key[0])
        if hours is None or not await journal.is_overdue(db, *key, hours):
            return False  # срок не вышел или политика не читается: уведомления нет
        return await _notify_owner(db, key, task, detail, hours)


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
    # Закрепление: снимок или голова, на которую проход сам его перезакрепил
    # (``_checked_sha`` ставит _approved_code_check); чужое перезакрепление - нет.
    pins = {x for x in (task.get("submission_sha"), task.get("_checked_sha")) if x}
    return (
        live is not None
        and int(live["pr_number"] or 0) == int(task.get("pr_number") or 0)
        and (live["submission_sha"] or "") in (pins or {""})
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


async def _notify_owner(
    db: aiosqlite.Connection,
    key: tuple[int, int, int, str],
    snap: dict[str, Any],
    detail: str,
    hours: int | None = None,
) -> bool:
    """Под локом записи: срок вышел - ОДНО уведомление владельцу, статус прежний.

    ``hours`` не передан (прямой вызов) - срок читается здесь же и перепроверяется.
    """
    from hub.services import orchestration

    pr_num = int(snap.get("pr_number") or 0)
    if not await _pass_is_live(db, snap, key):
        return False  # завершена, возвращена, сдана заново, PR или закрепление ушли
    if hours is None:
        hours = await wait_hours(db, key[0])
        if hours is None or not await journal.is_overdue(db, *key, hours):
            return False
    if await _delivery_moved_on(db, key, pr_num):
        await journal.close_open(db, *key)
        return False  # доставлено соседним путём: сообщать не о чем
    if await _rerun_holds(db, key, pr_num):
        return False  # живая бронь #1956 обслужит поколение; срок не продлевается
    if _merge_in_gate_memory(key[0], pr_num):
        return False  # мерж идёт или влит, реестр не записан: ждём, часы не трогаем
    if not await orchestration.approval_stands(db, snap):
        return False  # одобрения нет: сообщать об «одобренной» работе нельзя
    async with _savepoint(db):  # notified без полного уведомления не остаётся
        return await _write_notice(db, key, pr_num, detail, hours)


async def _silence_is_legal(
    db: aiosqlite.Connection, task_id: int, source_key: str
) -> bool:
    """notify_steward ничего не отправил законно: уже объявлено, нет проекта, повод выключен."""
    if await fetchall(db, "SELECT 1 FROM watch_notices WHERE key=?", (source_key,)):
        return True  # дедуп: этот источник уже объявлен
    project, _ = await repo.resolve_project_with_source(db, task_id)
    if project is None:
        return True
    policy = project_policy.gate_policy_of(project)
    return "stale" not in project_policy.steward_watch_of(policy)


@contextlib.asynccontextmanager
async def _savepoint(db: aiosqlite.Connection):
    """Откат только своих записей: транзакция вызывающего может быть открыта."""
    await db.execute("SAVEPOINT delivery_wait_notice")
    try:
        yield
    except BaseException:
        await db.execute("ROLLBACK TO SAVEPOINT delivery_wait_notice")
        await db.execute("RELEASE SAVEPOINT delivery_wait_notice")
        raise
    await db.execute("RELEASE SAVEPOINT delivery_wait_notice")


async def _write_notice(
    db: aiosqlite.Connection,
    key: tuple[int, int, int, str],
    pr_num: int,
    detail: str,
    hours: int,
) -> bool:
    """Переход open -> notified и вся запись уведомления: либо целиком, либо ничего."""
    if not await journal.notify_once(db, *key):
        return False  # уведомление по этому ключу уже ушло
    text = (
        f"Доставка одобренной задачи #{key[0]} (PR #{pr_num}) ждёт свежую базу "
        f"дольше срока base_fetch_wait_hours ({hours} ч): {detail}. "
        f"Причина {KIND}.{HINT}" + await _rerun_outcome_note(db, key)
    )
    await repo.add_task_update(db, key[0], "hub", "alert", text)
    event_id = await repo.insert_event(
        db,
        kind=EVENT,
        task_id=key[0],
        actor="hub",
        payload={"reason": KIND, "pr_number": pr_num, "hours": hours, "detail": detail},
    )
    source_key = f"{EVENT}:{key[0]}:{key[1]}:{key[2]}"
    sent = await watch_notices.notify_steward(
        db,
        "stale",
        task_id=key[0],
        source_key=source_key,
        source_event_id=event_id,
    )
    if not sent and not await _silence_is_legal(db, key[0], source_key):
        # notify_steward глотает ошибки и возвращает False, как при «наблюдение
        # выключено» и «уже объявлено». Иное молчание - сбой: откат SAVEPOINT выше
        # возвращает ожидание в open, и следующий проход донесёт уведомление.
        raise RuntimeError("steward notice was not delivered")
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
