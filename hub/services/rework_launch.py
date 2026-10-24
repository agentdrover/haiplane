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
import logging
import secrets
import sqlite3
from typing import Any

import aiosqlite
from fastapi import HTTPException

from hub import repository as repo
from hub.db import fetchall, write_transaction
from hub.integrations.registry import plugins

log = logging.getLogger("hub")

#: Сколько бронь может ждать ответа диспетчера (его вызов длится до 30 с),
#: прежде чем сверка сочтёт заказ оборванным.
GRACE_SECONDS = 180
#: Сколько заданий диспетчера сверка просматривает в поисках метки.
JOBS_SCANNED = 200

RESERVED = "reserved"
LAUNCHED = "launched"
DECLINED = "declined"
NEEDS_HUMAN = "needs_human"
CLOSED = "closed"


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


async def claim_submit(db: aiosqlite.Connection, reservation_id: int) -> str | None:
    """Право попытки заказать задание: токен брони или ``None``, если бронь закрыта.

    Один условный UPDATE (``reserved`` -> ``reserved``) обновляет срок брони: сверка
    и попытка делят одну строку, и выигрывает та, чей UPDATE прошёл раньше. Закрытая
    сверкой бронь права не даёт (попытка не заказывает); взявшая право попытка
    удерживает бронь от сверки на ``GRACE_SECONDS``, а вызов диспетчера короче.
    """
    async with write_transaction(db):
        cur = await db.execute(
            "UPDATE rework_launches SET reserved_at=datetime('now') "
            "WHERE id=? AND state=?",
            (reservation_id, RESERVED),
        )
        if not cur.rowcount:
            return None
        rows = await fetchall(
            db, "SELECT nonce FROM rework_launches WHERE id=?", (reservation_id,)
        )
    return str(rows[0]["nonce"])


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

    task = await require_entry(db, task_id)
    if not confirm_no_job:
        await refuse_unconfirmed_outcome(db, task_id)
    live = await executor_launch._live_run(db, task_id, rework_block=False)
    if live:
        raise _taken(task_id, live)
    try:
        cur = await db.execute(
            "INSERT INTO rework_launches (task_id, nonce) VALUES (?, ?)",
            (task_id, secrets.token_hex(8)),
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
) -> bool:
    """Закрыть бронь условно: только из ``reserved``. Коммит - за вызывающим."""
    cur = await db.execute(
        "UPDATE rework_launches SET state=?, job_id=?, reason=?, "
        "resolved_at=datetime('now') WHERE id=? AND state=?",
        (state, job_id, reason, reservation_id, RESERVED),
    )
    return (cur.rowcount or 0) > 0


async def close_for_task(db: aiosqlite.Connection, task_id: int, reason: str) -> None:
    """Снять действующую бронь задачи, которой заказ не понадобился (state-задача)."""
    held = await reserved_row(db, task_id)
    if held is not None:
        await resolve(db, int(held["id"]), CLOSED, reason=reason)
        await db.commit()


async def settle_launched(
    db: aiosqlite.Connection, reservation_id: int, task_id: int, job_id: str
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
        f"через {GRACE_SECONDS} с хаб сверит задания диспетчера и либо свяжет "
        "задачу с найденным, либо вернёт решение человеку.",
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
            await resolve(
                db,
                rid,
                state,
                job_id=str(task.get("job_id") or ""),
                reason=f"задача ушла из needs_decision (статус {task.get('status')})",
            )
        return state
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
        await resolve(db, rid, NEEDS_HUMAN, reason=reason)
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


async def sweep(db: aiosqlite.Connection) -> None:
    """Сверка оборванных броней: старше ``GRACE_SECONDS`` и всё ещё ``reserved``."""
    rows = await fetchall(
        db,
        "SELECT * FROM rework_launches WHERE state=? "
        "AND reserved_at <= datetime('now', ?) ORDER BY id",
        (RESERVED, f"-{GRACE_SECONDS} seconds"),
    )
    for row in rows:
        held = dict(row)
        try:
            await _reconcile(db, held)
        except Exception:
            log.exception("rework launch #%s: сверка не удалась", held["id"])
            if db.in_transaction:
                await db.rollback()
