"""Заказ машинного ревью после сдачи: durable намерение + свип (#2057).

Заказ ходит в сеть (Cursor, git); HTTP-ответ сдачи его не ждёт. Прод
перезапускается на каждом мерже, поэтому носитель заказа - строка
``review_order_intents``, записанная В ТОЙ ЖЕ транзакции, что и сдача
(``record_intent``).

Главное правило: НЕ БОЛЬШЕ ОДНОГО автоматического платного POST на
(задачу, поколение). Каждая схема «повтор + сверка» рождала новый путь ко
второму платному заказу, поэтому после любой попытки, которая могла дойти до
провайдера, автоматического пути назад нет.

* ``pending`` -> ``ordering``: условный UPDATE, огражённый токеном владельца и
  выполняемый ПЕРЕД POST (``begin_ordering``, шов в ``_create_or_adopt``) с
  commit; метка заказа привязывается к намерению. Не вышло (владение потеряно,
  поколение другое) - POST не отправляется.
* ``ordering`` -> ``done``, если строка заказа записана; иначе ``unknown`` и
  ОДИН алерт (исключение после отправки, провалившийся INSERT строки,
  зависание дольше ``ORDERING_TIMEOUT_MINUTES``). Свип такие строки не заказывает.
* До шва (подготовка заказа, сеть git) попытки безопасны: откат и не более
  ``MAX_ATTEMPTS``, затем ``failed`` и один алерт.
* Свип (из ``review_dispatch.sweep_review_dispatches``) и ``kick`` заказывают
  только ``pending``; рестарт = свип находит ``pending``. Повтор сдачи того же
  sha из review (#1265) - осознанный запрос: он возвращает ``done``/``unknown``/
  ``failed`` в ``pending`` в своей транзакции; ``ordering`` не трогает.
* Без DSN (прямой вызов сервиса, тесты) подталкивание обрабатывает на месте.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import uuid
from typing import Final

import aiosqlite

from hub import repository as repo
from hub.db import connect

log = logging.getLogger(__name__)

#: DSN базы для фонового соединения; выставляет только маршрут сдачи.
detach_dsn: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "review_order_detach_dsn", default=None
)

#: Выключатель фонового подталкивания (тесты, которым нужна обработка на месте).
DETACH_ENABLED: bool = True

MAX_ATTEMPTS: Final = 3
#: Откат (секунды) после n-й неудачи ДО шва; POST тут ещё не было.
BACKOFF_SECONDS: Final = (60, 300)
#: Бронь обработки ``pending`` без шва старше срока считается брошенной.
CLAIM_STALE_MINUTES: Final = 5
#: ``ordering`` дольше срока - исход неизвестен (создание длится до ~6 минут).
ORDERING_TIMEOUT_MINUTES: Final = 15


class OrderingRefused(Exception):
    """Шов отказал: POST не отправляется. ``superseded`` - сдача уже другая."""

    def __init__(self, why: str, *, superseded: bool = False) -> None:
        super().__init__(why)
        self.superseded = superseded


#: (задача, поколение, токен владельца) обработки, идущей в этой корутине.
_current: contextvars.ContextVar[tuple[int, int, str] | None] = contextvars.ContextVar(
    "review_order_current", default=None
)

_inflight: dict[tuple[int, int], asyncio.Task[None]] = {}


def inflight_count() -> int:
    return sum(1 for t in _inflight.values() if not t.done())


async def drain() -> None:
    pending = [t for t in _inflight.values() if not t.done()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


async def record_intent(
    db: aiosqlite.Connection, task_id: int, generation: int
) -> None:
    """Записать намерение; коммит - за вызывающим (транзакция сдачи).

    Повтор той же сдачи возвращает закрытое намерение в ``pending`` (решение о
    заказе пересматривается, #1150); ``pending`` и ``ordering`` не трогает.
    """
    await db.execute(
        "INSERT INTO review_order_intents (task_id, generation) VALUES (?, ?) "
        "ON CONFLICT(task_id, generation) DO UPDATE SET state='pending', "
        "attempts=0, next_attempt_at=datetime('now'), claimed_at=NULL, "
        "claimed_by='', ordering_marker='', ordering_at=NULL, last_error='' "
        "WHERE state IN ('done', 'unknown', 'failed')",
        (task_id, generation),
    )


async def _claim(
    db: aiosqlite.Connection, task_id: int, generation: int, token: str
) -> bool:
    cur = await db.execute(
        "UPDATE review_order_intents SET claimed_at=datetime('now'), claimed_by=? "
        "WHERE task_id=? AND generation=? AND state='pending' "
        "AND next_attempt_at <= datetime('now') "
        "AND (claimed_at IS NULL OR claimed_at < datetime('now', ?))",
        (token, task_id, generation, f"-{CLAIM_STALE_MINUTES} minutes"),
    )
    await db.commit()
    return cur.rowcount == 1


async def begin_ordering(db: aiosqlite.Connection, task_id: int, marker: str) -> None:
    """Шов перед платным POST: ``pending`` -> ``ordering`` или отказ без POST.

    Вне обработки намерения (переспрос, лестница) шов молчит: у них своя сверка.
    """
    scope = _current.get()
    if scope is None or scope[0] != task_id:
        return
    _, generation, token = scope
    row = await repo.get_task(db, task_id)
    live = int((dict(row).get("submission_generation") or 0) if row else 0)
    if live != generation or f":g{generation}:" not in marker:
        raise OrderingRefused("поколение сдачи сменилось", superseded=True)
    cur = await db.execute(
        "SELECT 1 FROM review_order_intents WHERE task_id=? AND generation=? "
        "AND claimed_by=? AND state IN ('ordering', 'done')",
        (task_id, generation, token),
    )
    if await cur.fetchone() is not None:
        return  # следующая ступень лестницы той же обработки: шов уже пройден
    cur = await db.execute(
        "UPDATE review_order_intents SET state='ordering', ordering_marker=?, "
        "ordering_at=datetime('now') WHERE task_id=? AND generation=? "
        "AND state='pending' AND claimed_by=?",
        (marker, task_id, generation, token),
    )
    await db.commit()
    if cur.rowcount != 1:
        raise OrderingRefused("владение обработкой потеряно")


async def _state(db: aiosqlite.Connection, task_id: int, generation: int) -> str:
    cur = await db.execute(
        "SELECT state FROM review_order_intents WHERE task_id=? AND generation=?",
        (task_id, generation),
    )
    row = await cur.fetchone()
    return str(row[0]) if row else ""


async def _set_closed(
    db: aiosqlite.Connection,
    task_id: int,
    generation: int,
    token: str,
    from_state: str,
    to_state: str,
) -> bool:
    cur = await db.execute(
        "UPDATE review_order_intents SET state=?, claimed_at=NULL "
        "WHERE task_id=? AND generation=? AND state=? AND claimed_by=?",
        (to_state, task_id, generation, from_state, token),
    )
    await db.commit()
    return cur.rowcount == 1


async def _alert(db: aiosqlite.Connection, task_id: int, text: str) -> None:
    await repo.add_task_update(db, task_id, "hub", "alert", text)
    await db.commit()


async def _unknown(
    db: aiosqlite.Connection, task_id: int, generation: int, token: str, why: str
) -> None:
    if await _set_closed(db, task_id, generation, token, "ordering", "unknown"):
        await _alert(
            db,
            task_id,
            f"Заказ ревью поколения {generation} мог уйти в Cursor, исход "
            f"неизвестен ({why[:120]}) - строки заказа нет. Проверьте агента "
            "по маркеру или пересдайте.",
        )


async def _fail_before_post(
    db: aiosqlite.Connection, task_id: int, generation: int, token: str, why: str
) -> None:
    cur = await db.execute(
        "UPDATE review_order_intents SET attempts=attempts+1, claimed_at=NULL, "
        "last_error=? WHERE task_id=? AND generation=? AND state='pending' "
        "AND claimed_by=? RETURNING attempts",
        (why[:300], task_id, generation, token),
    )
    row = await cur.fetchone()
    if row is None:
        await db.commit()
        return
    attempts = int(row[0])
    if attempts >= MAX_ATTEMPTS:
        await db.execute(
            "UPDATE review_order_intents SET state='failed' "
            "WHERE task_id=? AND generation=?",
            (task_id, generation),
        )
        await _alert(
            db,
            task_id,
            f"Заказ ревью поколения {generation} не начат после {attempts} "
            f"попыток ({why[:120]}); провайдеру ничего не отправлено. "
            "Повторная сдача того же sha закажет ревью заново.",
        )
        return
    pause = BACKOFF_SECONDS[min(attempts - 1, len(BACKOFF_SECONDS) - 1)]
    await db.execute(
        "UPDATE review_order_intents SET next_attempt_at=datetime('now', ?) "
        "WHERE task_id=? AND generation=?",
        (f"+{pause} seconds", task_id, generation),
    )
    await db.commit()


async def _settled(
    db: aiosqlite.Connection, task_id: int, generation: int, *, rows: bool = True
) -> bool:
    """Сдача уже не та или (rows) строка заказа есть.

    До вызова диспетчера строки не смотрим: он сам отказывает по ним, и его
    отказ (#1152) - наблюдаемое поведение повтора того же sha.
    """
    row = await repo.get_task(db, task_id)
    task = dict(row) if row else None
    if (
        task is None
        or task.get("status") != "review"
        or int(task.get("submission_generation") or 0) != generation
        or task.get("review_job_id")
    ):
        return True
    if not rows:
        return False
    cur = await db.execute(
        "SELECT 1 FROM review_dispatches WHERE task_id=? AND submission_generation=? "
        "AND status <> 'failed' LIMIT 1",
        (task_id, generation),
    )
    return await cur.fetchone() is not None


async def _claim_is_held(
    db: aiosqlite.Connection, task_id: int, generation: int
) -> bool:
    cur = await db.execute(
        "SELECT 1 FROM review_order_claims WHERE task_id=? AND submission_generation=? "
        "AND claimed_at >= datetime('now', ?) LIMIT 1",
        (task_id, generation, f"-{repo.REVIEW_ORDER_CLAIM_TTL_MINUTES} minutes"),
    )
    return await cur.fetchone() is not None


async def process_intent(
    db: aiosqlite.Connection, task_id: int, generation: int
) -> None:
    """Обработать одно ``pending`` намерение: единственный путь заказа ревью."""
    token = uuid.uuid4().hex
    if not await _claim(db, task_id, generation, token):
        return
    scope = _current.set((task_id, generation, token))
    try:
        if await _settled(db, task_id, generation, rows=False):
            await _set_closed(db, task_id, generation, token, "pending", "done")
            return
        from hub.services.review_dispatch import maybe_dispatch_review

        placed = await maybe_dispatch_review(db, task_id)
    except OrderingRefused as refused:
        await db.rollback()
        if refused.superseded:
            await _set_closed(db, task_id, generation, token, "pending", "done")
        return  # потерянное владение: исход пишет владелец
    except asyncio.CancelledError:
        await asyncio.shield(_after_cancel(db, task_id, generation, token))
        raise
    except Exception as exc:  # noqa: BLE001 - заказ не должен ронять хаб
        log.exception("review order failed for task #%s gen %s", task_id, generation)
        await db.rollback()
        why = f"{type(exc).__name__}: {exc}"
        if await _state(db, task_id, generation) == "ordering":
            await _unknown(db, task_id, generation, token, why)
        else:
            await _fail_before_post(db, task_id, generation, token, why)
        return
    finally:
        _current.reset(scope)
    await _finish_after_call(db, task_id, generation, token, placed)


async def _after_cancel(
    db: aiosqlite.Connection, task_id: int, generation: int, token: str
) -> None:
    """Отмена: до шва бронь снимается; после шва остаётся ``ordering`` (свип)."""
    await db.execute(
        "UPDATE review_order_intents SET claimed_at=NULL WHERE task_id=? "
        "AND generation=? AND state='pending' AND claimed_by=?",
        (task_id, generation, token),
    )
    await db.commit()


async def _finish_after_call(
    db: aiosqlite.Connection,
    task_id: int,
    generation: int,
    token: str,
    placed: bool,
) -> None:
    settled = placed or await _settled(db, task_id, generation)
    if await _state(db, task_id, generation) == "ordering":
        if settled:
            await _set_closed(db, task_id, generation, token, "ordering", "done")
        else:
            await _unknown(db, task_id, generation, token, "строки заказа нет")
    elif settled:
        await _set_closed(db, task_id, generation, token, "pending", "done")
    elif await _claim_is_held(db, task_id, generation):
        await _fail_before_post(db, task_id, generation, token, "бронь заказа удержана")
    else:
        await _set_closed(db, task_id, generation, token, "pending", "done")


async def _expire_stuck_orders(db: aiosqlite.Connection) -> None:
    """``ordering`` дольше срока: строка заказа есть - ``done``, иначе ``unknown``."""
    cur = await db.execute(
        "SELECT task_id, generation, claimed_by FROM review_order_intents "
        "WHERE state='ordering' AND ordering_at < datetime('now', ?)",
        (f"-{ORDERING_TIMEOUT_MINUTES} minutes",),
    )
    for task_id, generation, token in [tuple(r) for r in await cur.fetchall()]:
        if await _settled(db, int(task_id), int(generation)):
            await _set_closed(db, task_id, generation, token, "ordering", "done")
        else:
            await _unknown(db, task_id, generation, token, "зависание заказа")


async def sweep_review_orders(db: aiosqlite.Connection) -> None:
    """Проход поллера: закрыть зависшие ``ordering``, заказать ``pending``."""
    await _expire_stuck_orders(db)
    cur = await db.execute(
        "SELECT task_id, generation FROM review_order_intents WHERE state='pending' "
        "AND next_attempt_at <= datetime('now') ORDER BY created_at LIMIT 20"
    )
    for task_id, generation in [(int(r[0]), int(r[1])) for r in await cur.fetchall()]:
        try:
            await process_intent(db, task_id, generation)
        except Exception:  # noqa: BLE001 - один заказ не валит проход
            log.exception("review order sweep failed for task #%s", task_id)


async def _background(dsn: str, task_id: int, generation: int) -> None:
    db = None
    try:
        db = await connect(dsn)
        await process_intent(db, task_id, generation)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - свип подберёт
        log.exception("review order kick failed for task #%s", task_id)
    finally:
        if db is not None:
            await db.close()


async def kick(db: aiosqlite.Connection, task_id: int) -> None:
    """Подтолкнуть заказ текущего поколения после коммита сдачи.

    Намерение (и его возврат в ``pending`` на повторе того же sha) пишет сама
    сдача в своей транзакции; подталкивание его не создаёт и не взводит.
    """
    row = await repo.get_task(db, task_id)
    generation = int((dict(row).get("submission_generation") or 0) if row else 0)
    if not generation:
        return
    dsn = detach_dsn.get()
    if not DETACH_ENABLED or not dsn:
        await process_intent(db, task_id, generation)
        return
    key = (task_id, generation)
    running = _inflight.get(key)
    if running is not None and not running.done():
        return
    task = asyncio.get_running_loop().create_task(_background(dsn, task_id, generation))
    _inflight[key] = task

    def _forget(done: asyncio.Task[None], key: tuple[int, int] = key) -> None:
        if _inflight.get(key) is done:
            del _inflight[key]

    task.add_done_callback(_forget)
