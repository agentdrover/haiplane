"""Заказ машинного ревью после сдачи живёт вне HTTP-запроса (#2057).

Заказ ходит в сеть (Cursor, git), а ответ сдачи его не ждёт: клиент MCP
отваливается по таймауту раньше, сервер дорабатывает запись, и агент остаётся
без ответа при уже легшей сдаче. Поэтому после коммита сдачи заказ уходит
фоновой задачей на СВОЁМ соединении (соединение запроса закроется с ответом).

Инварианты:
* один заказ на (задача, поколение) в полёте — повторная сдача того же sha
  не плодит вторую задачу, а `maybe_dispatch_review` сам отказывает, если
  заказ уже лёг (review_job_id / строка диспетча);
* неудача не теряется молча: ограниченные повторы, затем алерт в ленту; путь
  восстановления — повторная сдача того же sha из review (no-op, #1265),
  которая заказывает ревью заново;
* без DSN (поллер, прямой вызов сервиса, тесты на общем соединении) заказ
  идёт на месте — раньше так и было.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
from typing import Final

import aiosqlite

from hub import repository as repo
from hub.db import connect

log = logging.getLogger(__name__)

#: DSN базы для фонового соединения; выставляет только маршрут сдачи.
detach_dsn: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "review_order_detach_dsn", default=None
)

#: Выключатель для тестов, которым нужен заказ на месте.
DETACH_ENABLED: bool = True

ATTEMPTS: Final = 3
RETRY_PAUSE_SECONDS: float = 2.0

_inflight: dict[tuple[int, int], asyncio.Task[None]] = {}


def inflight_count() -> int:
    return sum(1 for t in _inflight.values() if not t.done())


async def drain() -> None:
    """Дождаться фоновых заказов (тесты, остановка)."""
    pending = [t for t in _inflight.values() if not t.done()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


async def _dispatch_once(db: aiosqlite.Connection, task_id: int) -> None:
    from hub.services.review_dispatch import maybe_dispatch_review

    await maybe_dispatch_review(db, task_id)


async def _run(dsn: str, task_id: int, generation: int) -> None:
    last: Exception | None = None
    for attempt in range(ATTEMPTS):
        db = None
        try:
            db = await connect(dsn)
            await _dispatch_once(db, task_id)
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - заказ не должен ронять хаб
            last = exc
            log.exception(
                "review order failed for task #%s gen %s (attempt %s/%s)",
                task_id,
                generation,
                attempt + 1,
                ATTEMPTS,
            )
        finally:
            if db is not None:
                await db.close()
        if attempt + 1 < ATTEMPTS:
            await asyncio.sleep(RETRY_PAUSE_SECONDS)
    await _name_lost_order(dsn, task_id, generation, last)


async def _name_lost_order(
    dsn: str, task_id: int, generation: int, exc: Exception | None
) -> None:
    db = None
    try:
        db = await connect(dsn)
        await repo.add_task_update(
            db,
            task_id,
            "hub",
            "alert",
            f"Заказ ревью поколения {generation} не ушёл после {ATTEMPTS} "
            f"попыток ({type(exc).__name__}). Повторная сдача того же sha из "
            "review ничего не меняет в сдаче и закажет ревью заново.",
        )
        await db.commit()
    except Exception:  # noqa: BLE001
        log.exception("lost review order of task #%s not recorded", task_id)
    finally:
        if db is not None:
            await db.close()


async def order_after_submit(db: aiosqlite.Connection, task_id: int) -> None:
    """Заказать ревью после коммита сдачи: фоном, если запрос это позволяет."""
    dsn = detach_dsn.get()
    if not DETACH_ENABLED or not dsn:
        await _dispatch_once(db, task_id)
        return
    row = await repo.get_task(db, task_id)
    generation = int((dict(row).get("submission_generation") or 0) if row else 0)
    key = (task_id, generation)
    running = _inflight.get(key)
    if running is not None and not running.done():
        return
    task = asyncio.get_running_loop().create_task(_run(dsn, task_id, generation))
    _inflight[key] = task

    def _forget(done: asyncio.Task[None], key: tuple[int, int] = key) -> None:
        if _inflight.get(key) is done:
            del _inflight[key]

    task.add_done_callback(_forget)
