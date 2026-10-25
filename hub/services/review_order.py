"""Заказ машинного ревью после сдачи: durable намерение + свип (#2057).

Заказ ходит в сеть (Cursor, git); HTTP-ответ сдачи его не ждёт. Прод
перезапускается на каждом мерже, поэтому фоновая задача в памяти не может быть
носителем заказа. Носитель - строка ``review_order_intents``, записанная В ТОЙ
ЖЕ транзакции, что и сдача (``record_intent``).

Инварианты:
* Единственный исполнитель заказа - ``process_intent``. Его зовут свип
  (``sweep_review_orders``, из ``review_dispatch.sweep_review_dispatches``) и
  подталкивание маршрутом (``kick``); корректность от подталкивания не зависит.
  Рестарт = свип находит ``pending`` и заказывает.
* Обработка берётся условным UPDATE ``claimed_at`` (одна на строку); бронь
  старше ``CLAIM_STALE_MINUTES`` считается брошенной.
* Успех - только то, что видно в базе: строка заказа есть, либо сдача уже не та
  (статус/поколение/review_job_id), либо заказ отказан политикой БЕЗ удержанной
  брони. Удержанная бронь заказа без строки - не успех: попытка считается,
  откат растёт, после ``MAX_ATTEMPTS`` - один алерт и ``lost``. Повторная сдача
  того же sha из review (#1265) возвращает ``lost``/``done`` в ``pending``.
* Без DSN (прямой вызов сервиса, тесты на общем соединении) подталкивание
  обрабатывает намерение на месте.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import uuid
from typing import Any, Final

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

MAX_ATTEMPTS: Final = 4
#: Откат (секунды) после n-й неудачи; последний шаг длиннее брони заказа (10 мин).
BACKOFF_SECONDS: Final = (120, 360, 720)
#: Чужую бронь берут только у другого процесса и не раньше этого срока
#: (прежний процесс мёртв, его POST оборван; провайдер мог не успеть показать
#: агента в списке). Внутри живого процесса бронь по таймеру не отбирается.
FOREIGN_CLAIM_GRACE_MINUTES: Final = 2

#: Процесс хаба: владелец брони.
BOOT_ID: Final = uuid.uuid4().hex

#: (задача, поколение) намерения, которое обрабатывает эта корутина.
_current: contextvars.ContextVar[tuple[int, int] | None] = contextvars.ContextVar(
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

    Повтор той же сдачи возвращает ``lost``/``done`` в ``pending``: решение о
    заказе пересматривается (#1150), а уже лёгший заказ ``_settled`` узнаёт сам.
    """
    await db.execute(
        "INSERT INTO review_order_intents (task_id, generation) VALUES (?, ?) "
        "ON CONFLICT(task_id, generation) DO UPDATE SET state='pending', "
        "attempts=0, next_attempt_at=datetime('now'), claimed_at=NULL, "
        "claimed_by='' "
        "WHERE state IN ('lost', 'done')",
        (task_id, generation),
    )


async def _claim(db: aiosqlite.Connection, task_id: int, generation: int) -> bool:
    cur = await db.execute(
        "UPDATE review_order_intents SET claimed_at=datetime('now'), claimed_by=? "
        "WHERE task_id=? AND generation=? AND state='pending' "
        "AND next_attempt_at <= datetime('now') "
        "AND (claimed_at IS NULL OR (claimed_by <> ? "
        "AND claimed_at < datetime('now', ?)))",
        (
            BOOT_ID,
            task_id,
            generation,
            BOOT_ID,
            f"-{FOREIGN_CLAIM_GRACE_MINUTES} minutes",
        ),
    )
    await db.commit()
    return cur.rowcount == 1


async def _finish(db: aiosqlite.Connection, task_id: int, generation: int) -> None:
    await db.execute(
        "UPDATE review_order_intents SET state='done', claimed_at=NULL, "
        "claimed_by='' WHERE task_id=? AND generation=? AND claimed_by=?",
        (task_id, generation, BOOT_ID),
    )
    await db.commit()


async def _fail(
    db: aiosqlite.Connection, task_id: int, generation: int, why: str
) -> None:
    cur = await db.execute(
        "UPDATE review_order_intents SET attempts=attempts+1, claimed_at=NULL, "
        "claimed_by='', last_error=? WHERE task_id=? AND generation=? "
        "AND claimed_by=? RETURNING attempts",
        (why[:300], task_id, generation, BOOT_ID),
    )
    row = await cur.fetchone()
    if row is None:  # бронь уже не наша: исход пишет её владелец
        await db.commit()
        return
    attempts = int(row[0])
    if attempts >= MAX_ATTEMPTS:
        await db.execute(
            "UPDATE review_order_intents SET state='lost' "
            "WHERE task_id=? AND generation=?",
            (task_id, generation),
        )
        await repo.add_task_update(
            db,
            task_id,
            "hub",
            "alert",
            f"Заказ ревью поколения {generation} не ушёл после {attempts} "
            f"попыток ({why[:120]}). Повторная сдача того же sha из review "
            "ничего не меняет в сдаче и закажет ревью заново.",
        )
    else:
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
    """Заказ закрыт по данным базы: сдача уже не та или (rows) строка заказа есть.

    До вызова диспетчера строки не смотрим: он сам отказывает по ним, и его
    отказ (#1152) — наблюдаемое поведение повтора того же sha.
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
    """Обработать одно намерение: единственный путь, которым хаб заказывает ревью."""
    if not await _claim(db, task_id, generation):
        return
    marker_scope = _current.set((task_id, generation))
    try:
        if await _settled(db, task_id, generation, rows=False):
            await _finish(db, task_id, generation)
            return
        from hub.services.review_dispatch import maybe_dispatch_review

        placed = await maybe_dispatch_review(db, task_id)
    except asyncio.CancelledError:
        # Исход заказа неизвестен: бронь снимается, метка остаётся - следующая
        # попытка сначала спросит провайдера.
        await asyncio.shield(_release(db, task_id, generation))
        raise
    except Exception as exc:  # noqa: BLE001 - заказ не должен ронять хаб
        log.exception("review order failed for task #%s gen %s", task_id, generation)
        await db.rollback()
        await _fail(db, task_id, generation, f"{type(exc).__name__}: {exc}")
        return
    finally:
        _current.reset(marker_scope)
    if placed or await _settled(db, task_id, generation):
        await _finish(db, task_id, generation)
    elif await _claim_is_held(db, task_id, generation):
        await _fail(db, task_id, generation, "бронь заказа удержана, строки заказа нет")
    else:
        await _finish(db, task_id, generation)  # отказ политики/конфигурации


async def _release(db: aiosqlite.Connection, task_id: int, generation: int) -> None:
    await db.execute(
        "UPDATE review_order_intents SET claimed_at=NULL, claimed_by='' "
        "WHERE task_id=? AND generation=? AND claimed_by=?",
        (task_id, generation, BOOT_ID),
    )
    await db.commit()


async def reconcile_before_create(
    db: aiosqlite.Connection | None, task_id: int, marker: str
) -> Any:
    """Шов перед платным POST провайдеру: метка в базу, повтор - сначала сверка.

    Первый заказ по метке: пишет ``ordering_marker`` (commit) и отпускает
    создание (None). Повтор по той же метке (прежняя попытка могла купить
    агента и не записать строку) спрашивает провайдера: агент найден - его и
    подбирают; спросить не удалось - создание запрещено (``blind``); «нет» при
    удавшемся вопросе - создание разрешено. Вне обработки намерения (переспрос,
    лестница) шов молчит: у них своя сверка.
    """
    scope = _current.get()
    if db is None or scope is None or scope[0] != task_id:
        return None
    generation = scope[1]
    cur = await db.execute(
        "SELECT ordering_marker FROM review_order_intents "
        "WHERE task_id=? AND generation=? AND claimed_by=?",
        (task_id, generation, BOOT_ID),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    if row[0] == marker:
        from hub.integrations import cursor_cloud
        from hub.services.review_dispatch import _Started

        seen = await cursor_cloud.find_agent_by_name(marker)
        if seen.agent_id or not seen.asked:
            return _Started(
                seen.agent_id, seen.run_id, bool(seen.agent_id), not seen.asked, 0, None
            )
        return None
    await db.execute(
        "UPDATE review_order_intents SET ordering_marker=?, ordering_at=datetime('now') "
        "WHERE task_id=? AND generation=? AND claimed_by=?",
        (marker, task_id, generation, BOOT_ID),
    )
    await db.commit()
    return None


async def sweep_review_orders(db: aiosqlite.Connection) -> None:
    """Проход поллера: заказать то, что записано и не заказано."""
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
