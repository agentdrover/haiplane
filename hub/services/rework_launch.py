"""Бронь запуска доработки по решению «на доработку» (#1991).

Решение rework коммитится, потом идёт сеть диспетчера, и лишь после неё задача
выходит из needs_decision. Между этими точками второй вход (REST, веб-форма,
лист карточки, прогон слияния) видел тот же needs_decision и запускал второе
задание. Процессный замок листа (#1813) закрывает только лист в одном процессе.

Здесь вход потребляется на уровне базы. ``reserve`` под write-локом проверяет
статус и заводит строку ``rework_launches`` в состоянии ``reserved`` ВМЕСТЕ с
записью решения; частичный UNIQUE по ``task_id`` не даёт завести вторую активную
бронь, как бы ни разошлись проверки двух соединений. Проигравший получает 409 и
ничего не записывает. Сеть идёт после коммита брони, без write-лока; её исход
(``launched`` с job_id, ``declined`` без задания) пишется условно, одной
транзакцией со статусом задачи.

Диспетчер ключа идемпотентности не знает, поэтому исход заказа, оборванного
сбоем процесса, определяется сверкой: ``sweep`` ищет метку брони в заданиях
диспетчера. Нашли - задача связывается с ним. Не нашли - повторного запуска
нет: задача остаётся у человека с причиной, а бронь снимается, чтобы он мог
решить заново осознанно.
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import secrets
import sqlite3
import threading
from pathlib import Path
from typing import Any

import aiosqlite
from fastapi import HTTPException

from hub import repository as repo
from hub.db import fetchall, write_transaction
from hub.integrations.registry import plugins

log = logging.getLogger("hub")

#: Сколько заданий диспетчера сверка просматривает в поисках метки.
JOBS_SCANNED = 200

RESERVED = "reserved"
LAUNCHED = "launched"
DECLINED = "declined"
NEEDS_HUMAN = "needs_human"
CLOSED = "closed"


# Единственность процесса на базе: восстановление по boot_id верно, пока второго
# живого хаба на той же базе нет. Владение - явный объект: PID, открытый дескриптор
# с удерживаемым flock и список зарегистрированных приложений. Владельцем считается
# только тот, кто такой дескриптор держит; отсутствие отказа владением не является.
#: Заявители по пути базы: приложения процесса, включая не получившие lock.
_APPS: dict[str, list[Any]] = {}
_OWNERS: dict[str, _Ownership] = {}
_BOOT: dict[str, Any] = {"pid": 0, "id": ""}
#: Процесс, которому принадлежат _APPS и _OWNERS; после fork это уже процесс родителя.
_STATE: dict[str, int] = {"pid": os.getpid()}
#: Один писатель реестров. Переход PID (очистка и публикация нового PID) и работа
#: со _APPS/_OWNERS идут под ним, чтобы второй поток не обогнал очистку.
_LOCK = threading.Lock()


def _renew_lock_in_child() -> None:
    """Замок, занятый в момент fork другим потоком родителя, в потомке не освободит
    никто; а реестры, оставленные тем потоком на середине правки, потомок отбросит
    переходом PID. Поэтому замок заводится заново."""
    global _LOCK
    _LOCK = threading.Lock()


os.register_at_fork(after_in_child=_renew_lock_in_child)


class _Ownership:
    def __init__(self, handle: Any) -> None:
        self.pid = os.getpid()
        self.handle = handle

    def valid(self) -> bool:
        return self.pid == os.getpid() and not self.handle.closed


def _key(db_path: Any) -> str:
    """Один файл базы - один ключ, как бы к нему ни обратились (симлинк)."""
    return os.path.realpath(str(db_path))


def boot_id() -> str:
    """Идентификатор процесса хаба; после fork потомок получает свой."""
    pid = os.getpid()
    if _BOOT["pid"] != pid:
        _BOOT["pid"], _BOOT["id"] = pid, secrets.token_hex(8)
    return str(_BOOT["id"])


def _forget_inherited() -> None:
    """Под _LOCK. В потомке fork отбросить всё, что принадлежит родителю: и дескрипторы, и заявки.

    Дескрипторы только закрываются, без LOCK_UN: открытое описание файла общее с
    родителем, и снятие блокировки сняло бы её у него. Заявки родителя - не наши:
    оставшись, они держали бы lock потомка после ухода его последнего приложения.
    Проверка по PID стоит на каждом входе, а не в хуке fork: так достаточно одного
    правила для любого пути, которым потомок впервые зашёл в модуль. Новый PID
    публикуется только после очистки: иначе поток, вошедший следом, прошёл бы
    проверку и работал бы с реестрами, которые очистка ещё сотрёт.
    """
    pid = os.getpid()
    if _STATE["pid"] == pid:
        return
    for owner in _OWNERS.values():
        if not owner.handle.closed:
            owner.handle.close()
    _OWNERS.clear()
    _APPS.clear()
    _STATE["pid"] = pid


def _drop(key: str) -> None:
    """Убрать владение. В потомке fork дескриптор только закрывается: LOCK_UN
    снял бы блокировку родителя (общее открытое описание файла)."""
    owner = _OWNERS.pop(key, None)
    if owner is not None and not owner.handle.closed:
        owner.handle.close()


def _acquire(key: str) -> bool:
    owner = _OWNERS.get(key)
    if owner is not None and owner.valid():
        return True
    if owner is not None:
        _drop(key)
    try:
        Path(key).parent.mkdir(parents=True, exist_ok=True)
        handle = open(f"{key}.owner.lock", "a+")  # noqa: SIM115
    except OSError:
        # Не доказали единственность - владения нет (повтор при следующей попытке).
        log.warning("блокировка владельца базы %s недоступна: владения нет", key)
        return False
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return False
    _OWNERS[key] = _Ownership(handle)
    return True


def claim_process(db_path: Any, app: Any = None) -> bool:
    """Зарегистрировать приложение и взять право на базу, если оно свободно."""
    key = _key(db_path)
    with _LOCK:
        _forget_inherited()
        _APPS.setdefault(key, []).append(app if app is not None else "anon")
        return _acquire(key)


def release_process(db_path: Any, app: Any = None) -> None:
    """Снять заявку приложения; с уходом последнего закрывается и дескриптор."""
    key = _key(db_path)
    with _LOCK:
        _forget_inherited()
        apps = _APPS.get(key)
        if apps is None:
            return
        token = app if app is not None else "anon"
        if token in apps:
            apps.remove(token)
        if not apps:
            del _APPS[key]
            _drop(key)


def release_all() -> None:
    """Снять все заявки и владения процесса (остановка, чистка в тестах)."""
    with _LOCK:
        _APPS.clear()
        for key in list(_OWNERS):
            _drop(key)


def owns_db() -> bool:
    """Процесс держит дескриптор с flock и вправе сверять брони и заказывать.

    Без зарегистрированного приложения lock не берётся; зарегистрированное, но
    не получившее lock, повторяет попытку здесь.
    """
    from hub import config

    key = _key(config.HUB_DB_PATH)
    with _LOCK:
        _forget_inherited()
        return key in _APPS and _acquire(key)


def marker(reservation_id: int) -> str:
    """Метка брони: номер, по которому сверка узнаёт своё задание."""
    return f"rework-launch:{reservation_id}"


def marker_line(reservation_id: int, task_id: int, nonce: str) -> str:
    """Последняя строка текста заказа: метка, задача и токен брони.

    Узнаётся только как ЦЕЛАЯ последняя строка поля ``message`` задания и только
    с токеном брони, который знает лишь её заказ: цитата метки в описании чужой
    задачи (без токена, в середине текста, с чужим номером) за свою не сходит.
    """
    return f"(метка заказа {marker(reservation_id)}:{nonce}, задача #{task_id})"


def _taken(task_id: int, why: str) -> HTTPException:
    return HTTPException(
        409,
        f"Task #{task_id}: {why}. Второе решение не принято, ничего не записано; "
        "обновите страницу.",
    )


async def reserved_row(db: aiosqlite.Connection, task_id: int) -> dict[str, Any] | None:
    """Действующая бронь задачи или ``None``."""
    rows = await fetchall(
        db,
        "SELECT * FROM rework_launches WHERE task_id=? AND state=?",
        (task_id, RESERVED),
    )
    return dict(rows[0]) if rows else None


async def refuse_if_reserved(db: aiosqlite.Connection, task_id: int) -> None:
    """409, если запуск доработки по задаче уже заказан и не закончен."""
    held = await reserved_row(db, task_id)
    if held is not None:
        raise _taken(
            task_id,
            f"запуск доработки уже заказан (бронь №{held['id']} от "
            f"{held['reserved_at']})",
        )


async def launch_block(db: aiosqlite.Connection, task_id: int) -> str:
    """Почему запуск исполнителя по задаче закрыт заказом доработки; пусто - не закрыт.

    Закрывает и действующая бронь, и последний заказ с неустановленным исходом:
    пока человек не подтвердил отсутствие задания (``confirm_no_job``), другая
    дверь (прогон слияния) вторым исполнителем не пользуется.
    """
    held = await reserved_row(db, task_id)
    if held is not None:
        return f"заказ доработки, бронь №{held['id']}"
    rows = await fetchall(
        db,
        "SELECT id, state FROM rework_launches WHERE task_id=? "
        "ORDER BY id DESC LIMIT 1",
        (task_id,),
    )
    if rows and rows[0]["state"] == NEEDS_HUMAN:
        return (
            f"исход заказа доработки (бронь №{rows[0]['id']}) не установлен: "
            "решите rework с confirm_no_job"
        )
    return ""


async def refuse_if_unresolved(db: aiosqlite.Connection, task_id: int) -> None:
    """409 для выхода из needs_decision (пересдача #1362): бронь или неснятый исход.

    Исход заказа, который сверка не установила (``needs_human``), снимает только
    решение человека rework с ``confirm_no_job``, а оно принимается лишь в
    needs_decision. Выход оттуда пересдачей оставил бы блок запуска навсегда.
    """
    await refuse_if_reserved(db, task_id)
    await refuse_unconfirmed_outcome(db, task_id)


async def is_current(db: aiosqlite.Connection, reservation_id: int) -> bool:
    rows = await fetchall(
        db,
        "SELECT 1 FROM rework_launches WHERE id=? AND state=?",
        (reservation_id, RESERVED),
    )
    return bool(rows)


async def claim_submit(db: aiosqlite.Connection, reservation_id: int) -> str | None:
    """Токен брони для заказа или ``None``, если бронь уже закрыта.

    Закрыть бронь при живом отправителе сверка не может (см. ``boot_id``), так что
    ``None`` - это бронь, закрытую другим путём (задача завершена, процесс
    перезапущен): такая попытка задание не заказывает.
    """
    async with write_transaction(db):
        rows = await fetchall(
            db,
            "SELECT nonce FROM rework_launches WHERE id=? AND state=?",
            (reservation_id, RESERVED),
        )
    return str(rows[0]["nonce"]) if rows else None


async def close_open(
    db: aiosqlite.Connection, task_id: int, reason: str, *, reserved: bool = True
) -> None:
    """Завершение задачи человеком: открытая бронь и неснятый исход закрываются.

    Иначе ``needs_human`` оставался бы блоком запуска у задачи, которой запуск
    уже не нужен. ``reserved=False`` оставляет действующую бронь (архив не
    останавливает живого отправителя). Коммит - за вызывающим.
    """
    await db.execute(
        "UPDATE rework_launches SET state=?, reason=?, resolved_at=datetime('now') "
        "WHERE task_id=? AND state IN (?, ?)",
        (CLOSED, reason, task_id, RESERVED if reserved else NEEDS_HUMAN, NEEDS_HUMAN),
    )


async def require_entry(db: aiosqlite.Connection, task_id: int) -> dict[str, Any]:
    """Свежая строка задачи под write-локом, если вход needs_decision ещё не потреблён."""
    row = await repo.get_task(db, task_id)
    if row is None:
        raise HTTPException(404, "task not found")
    task = dict(row)
    if task["status"] != "needs_decision":
        raise _taken(
            task_id,
            f"решение по ней уже принято другим входом (статус сейчас {task['status']})",
        )
    await refuse_if_reserved(db, task_id)
    return task


async def refuse_unconfirmed_outcome(db: aiosqlite.Connection, task_id: int) -> None:
    """409, пока исход прошлого заказа не установлен и человек не подтвердил.

    Сверка не нашла задание, но это не доказательство, что его нет (оно могло
    выпасть из просмотренных). Новое решение rework после ``needs_human``
    возможно только с ``confirm_no_job``: иначе повтор того же запроса молча
    запустил бы второе задание.
    """
    rows = await fetchall(
        db,
        "SELECT id, state FROM rework_launches WHERE task_id=? "
        "ORDER BY id DESC LIMIT 1",
        (task_id,),
    )
    if rows and rows[0]["state"] == NEEDS_HUMAN:
        raise _taken(
            task_id,
            f"исход прошлого заказа (бронь №{rows[0]['id']}) не установлен: "
            "проверьте очередь диспетчера и повторите решение с "
            "confirm_no_job=true, если задания там нет",
        )


async def reserve(
    db: aiosqlite.Connection, task_id: int, *, confirm_no_job: bool = False
) -> tuple[dict[str, Any], int]:
    """Потребить вход needs_decision и забронировать запуск: (задача, номер брони).

    Вызывается ВНУТРИ ``write_transaction`` вызывающего, вместе с записью
    решения: бронь и решение коммитятся или откатываются вместе.
    """
    from hub.services import executor_launch

    if not owns_db():
        raise HTTPException(
            503,
            "Другой экземпляр хаба держит эту базу: доработку заказывает он. "
            "Повторите запрос позже.",
        )
    task = await require_entry(db, task_id)
    if not confirm_no_job:
        await refuse_unconfirmed_outcome(db, task_id)
    live = await executor_launch._live_run(db, task_id, rework_block=False)
    if live:
        raise _taken(task_id, live)
    try:
        cur = await db.execute(
            "INSERT INTO rework_launches (task_id, nonce, boot_id) VALUES (?, ?, ?)",
            (task_id, secrets.token_hex(8), boot_id()),
        )
    except sqlite3.IntegrityError as exc:
        raise _taken(task_id, "запуск доработки уже заказан") from exc
    return task, int(cur.lastrowid or 0)


async def resolve(
    db: aiosqlite.Connection,
    reservation_id: int,
    state: str,
    *,
    job_id: str = "",
    reason: str = "",
    from_state: str = RESERVED,
) -> bool:
    """Закрыть бронь условно: только из ``from_state``. Коммит - за вызывающим."""
    sql = (
        "UPDATE rework_launches SET state=?, job_id=?, reason=?, "
        "resolved_at=datetime('now') WHERE id=? AND state=?"
    )
    if from_state == NEEDS_HUMAN:
        # Неснятый исход привязывается только пока он последний заказ задачи:
        # повтор решения человека его уже заменил.
        sql += (
            " AND id=(SELECT MAX(id) FROM rework_launches WHERE task_id="
            "(SELECT task_id FROM rework_launches WHERE id=?))"
        )
    args: tuple[Any, ...] = (state, job_id, reason, reservation_id, from_state)
    if from_state == NEEDS_HUMAN:
        args += (reservation_id,)
    cur = await db.execute(sql, args)
    return (cur.rowcount or 0) > 0


async def close_for_task(db: aiosqlite.Connection, task_id: int, reason: str) -> None:
    """Снять действующую бронь задачи, которой заказ не понадобился (state-задача)."""
    held = await reserved_row(db, task_id)
    if held is not None:
        await resolve(db, int(held["id"]), CLOSED, reason=reason)
        await db.commit()


async def settle_launched(
    db: aiosqlite.Connection,
    reservation_id: int,
    task_id: int,
    job_id: str,
    *,
    from_state: str = RESERVED,
) -> bool:
    """Задание принято: статус fix_requested и job_id пишутся вместе с бронью.

    ``False`` - задача успела выйти из needs_decision другим путём: job_id всё
    равно сохранён в брони, а задача уходит к человеку с названием задания.
    """
    async with write_transaction(db):
        row = await repo.get_task(db, task_id)
        moved = row is not None and dict(row)["status"] == "needs_decision"
        # Бронь закрывается первой и условно: поздний ответ после сверки не
        # трогает задачу, чью бронь уже закрыли.
        won = await resolve(
            db,
            reservation_id,
            LAUNCHED if moved else NEEDS_HUMAN,
            job_id=job_id,
            reason="" if moved else "задача вышла из needs_decision, пока заказ шёл",
            from_state=from_state,
        )
        if won and moved:
            await repo.update_task(db, task_id, status="fix_requested", job_id=job_id)
            return True
        why = (
            "задача уже не в needs_decision"
            if won
            else "бронь уже закрыта сверкой или другим путём"
        )
        await repo.add_task_update(
            db,
            task_id,
            "hub",
            "alert",
            f"Доработка заказана (задание {job_id}), но {why}. Задание не "
            "привязано к задаче: проверьте его вручную.",
        )
    return False


async def release_sender(db: aiosqlite.Connection, reservation_id: int) -> None:
    """Отправитель заказа закончил (вызов вернулся, оборвался или запись упала).

    Живого, кто мог бы создать задание, больше нет, и сверка вправе решать без
    перезапуска хаба. Сбой самой записи не скрывает исходную ошибку: бронь тогда
    дождётся перезапуска.
    """
    try:
        await db.execute(
            "UPDATE rework_launches SET boot_id='' WHERE id=? AND state=?",
            (reservation_id, RESERVED),
        )
        await db.commit()
    except Exception:
        log.exception("rework launch #%s: не удалось освободить бронь", reservation_id)


async def note_unknown_outcome(
    db: aiosqlite.Connection, task_id: int, reservation_id: int, error: BaseException
) -> None:
    """Сеть оборвалась: исход заказа неизвестен, бронь держит повтор до сверки."""
    await repo.add_task_update(
        db,
        task_id,
        "hub",
        "alert",
        f"Заказ доработки оборвался ({type(error).__name__}): принял ли диспетчер "
        f"задание, неизвестно. Бронь №{reservation_id} не даёт заказать второе; "
        "сверка проверит задания диспетчера и либо свяжет "
        "задачу с найденным, либо вернёт решение человеку.",
    )
    # Отправитель этого заказа закончил (вызов вернулся или оборвался): живого,
    # кто мог бы создать задание, нет, и сверка вправе решать без перезапуска.
    await db.execute(
        "UPDATE rework_launches SET boot_id='' WHERE id=?", (reservation_id,)
    )
    await db.commit()


def _job_id_of(job: dict[str, Any]) -> str:
    return str(job.get("id") or job.get("job_id") or "")


def _last_line(job: dict[str, Any]) -> str:
    text = job.get("message")
    lines = (
        [ln.strip() for ln in text.splitlines() if ln.strip()]
        if isinstance(text, str)
        else []
    )
    return lines[-1] if lines else ""


async def _find_job(
    reservation_id: int, task_id: int, nonce: str
) -> tuple[dict[str, Any] | None, bool]:
    """Задание этой брони: (задание, неоднозначно ли совпадение).

    Совпадает только последняя строка ``message`` целиком (см. ``marker_line``).
    Два и более совпадения - неоднозначность: привязать нельзя ни одно.
    """
    if not nonce:
        return None, False
    want = marker_line(reservation_id, task_id, nonce)
    jobs = await asyncio.to_thread(plugins.dispatch.list_jobs, JOBS_SCANNED)
    found = [j for j in jobs if isinstance(j, dict) and _last_line(j) == want]
    if len(found) > 1:
        return None, True
    return (found[0] if found else None), False


async def _reconcile(db: aiosqlite.Connection, held: dict[str, Any]) -> str:
    """Определить исход одной оборванной брони; вернуть итоговое состояние."""
    rid, task_id = int(held["id"]), int(held["task_id"])
    row = await repo.get_task(db, task_id)
    task = dict(row) if row is not None else {}
    if task.get("status") != "needs_decision":
        state = LAUNCHED if task.get("job_id") else CLOSED
        async with write_transaction(db):
            closed = await resolve(
                db,
                rid,
                state,
                job_id=str(task.get("job_id") or ""),
                reason=f"задача ушла из needs_decision (статус {task.get('status')})",
            )
        return state if closed else RESERVED
    job, ambiguous = await _find_job(rid, task_id, str(held.get("nonce") or ""))
    job_id = _job_id_of(job) if job else ""
    if job_id:
        if not await settle_launched(db, rid, task_id, job_id):
            return NEEDS_HUMAN
        await repo.add_task_update(
            db,
            task_id,
            "hub",
            "status",
            f"Сверка брони №{rid}: задание {job_id} найдено у диспетчера, задача "
            "связана с ним. Второго запуска нет.",
        )
        await db.commit()
        return LAUNCHED
    if ambiguous:
        reason = "меткой брони помечено несколько заданий диспетчера"
    elif job:
        reason = "задание у диспетчера найдено без id"
    else:
        reason = "среди последних заданий диспетчера задания с меткой брони нет"
    async with write_transaction(db):
        if not await resolve(db, rid, NEEDS_HUMAN, reason=reason):
            return RESERVED  # бронь закрыта другим путём: не наша
        await repo.add_task_update(
            db,
            task_id,
            "hub",
            "alert",
            f"Заказ доработки (бронь №{rid}) оборвался, и установить, принял ли "
            f"диспетчер задание, не удалось: {reason}. Повторного запуска нет. "
            "Проверьте очередь диспетчера; если задания там нет, примите решение "
            "заново с confirm_no_job=true (REST POST /api/tasks/<id>/decide).",
        )
    return NEEDS_HUMAN


async def _lift_unproven(db: aiosqlite.Connection, held: dict[str, Any]) -> str:
    """Исход ``needs_human``: задание с собственной меткой появилось - блок снят.

    Нового заказа нет: бронь связывается с найденным заданием. Нет задания или
    совпадение неоднозначно - исход остаётся за человеком.
    """
    rid, task_id = int(held["id"]), int(held["task_id"])
    job, _ambiguous = await _find_job(rid, task_id, str(held.get("nonce") or ""))
    job_id = _job_id_of(job) if job else ""
    if not job_id:
        return NEEDS_HUMAN
    row = await repo.get_task(db, task_id)
    if row is not None and dict(row)["status"] == "needs_decision":
        if not await settle_launched(db, rid, task_id, job_id, from_state=NEEDS_HUMAN):
            return NEEDS_HUMAN
    else:
        async with write_transaction(db):
            if not await resolve(
                db,
                rid,
                LAUNCHED,
                job_id=job_id,
                reason="задание найдено сверкой после неустановленного исхода",
                from_state=NEEDS_HUMAN,
            ):
                return NEEDS_HUMAN
    await repo.add_task_update(
        db,
        task_id,
        "hub",
        "status",
        f"Сверка брони №{rid}: задание {job_id} найдено у диспетчера, исход "
        "установлен, блок запуска снят. Второго запуска нет.",
    )
    await db.commit()
    return LAUNCHED


async def sweep(db: aiosqlite.Connection) -> None:
    """Сверка: брони прежнего процесса и неснятые исходы ``needs_human``.

    Бронь текущего процесса не трогается: его отправитель может ещё создать
    задание, и время этого не опровергает.
    """
    if not owns_db():
        return
    rows = await fetchall(
        db,
        "SELECT * FROM rework_launches r WHERE "
        "(state=? AND boot_id != ?) OR "
        "(state=? AND id=(SELECT MAX(id) FROM rework_launches WHERE task_id=r.task_id)) "
        "ORDER BY id",
        (RESERVED, boot_id(), NEEDS_HUMAN),
    )
    for row in rows:
        held = dict(row)
        try:
            if held["state"] == NEEDS_HUMAN:
                await _lift_unproven(db, held)
            else:
                await _reconcile(db, held)
        except Exception:
            log.exception("rework launch #%s: сверка не удалась", held["id"])
            if db.in_transaction:
                await db.rollback()
