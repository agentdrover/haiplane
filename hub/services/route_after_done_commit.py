"""Запись git-хвоста done-конвейера коммитится до сети (#2005).

Хвост ``_route_after_done`` пишет в ленту и в задачу (предупреждения области,
``gate_squashed_sha``), а затем идёт в сеть: ``git fetch`` в ``content_differs``
и ``push_branch``. Незакоммиченная запись держит write-лок SQLite на всё время
вызова, и соседние писатели получают «database is locked».

Две стороны, и граница коммита у каждой своя.

* Headless-вход (поллер, ``refresh_task``). Соединение принадлежит вызывающему,
  внешней транзакции нет: он и так коммитит после хвоста. Значит, коммит до
  сети — его же обязанность, и помощник её исполняет.
* Вход из done-flow (``task_updates.apply_update``: ``write_transaction`` и
  ``SAVEPOINT done_flow``). Транзакция ЧУЖАЯ. Коммит здесь снял бы савпоинт и
  сделал откат #364 пустым: упавший следом push оставил бы в базе done-строку,
  бамп поколения и запись маршрута, которых «не было». Поэтому в done-flow
  помощник НЕ коммитит и НЕ откатывает ничего: лок на время git-хвоста
  сохраняется. Это явное исключение, а не недосмотр; оно записано в
  docs/agent-context/invariants.md и закреплено
  ``test_done_flow_entry_never_commits_the_foreign_transaction``.

Признак done-flow — ``orchestration._deferred_completions``: его выставляет
только ``defer_completions`` в ``apply_update``, внутри той самой транзакции.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import aiosqlite

from hub import repository as repo


def in_done_flow() -> bool:
    """Идёт ли вызов под транзакцией done-flow (она чужая и неприкосновенна)."""
    from hub.services.orchestration import _deferred_completions

    return _deferred_completions.get() is not None


async def commit_unless_done_flow(db: aiosqlite.Connection) -> None:
    """Закоммитить записи вызывающего; внутри done-flow - не трогать."""
    if not in_done_flow() and db.in_transaction:
        await db.commit()


def committed_before(
    db: aiosqlite.Connection, call: Callable[..., Awaitable[Any]]
) -> Callable[..., Awaitable[Any]]:
    """Сетевой вызов, которому предшествует коммит записей вызывающего."""

    async def run(*args: Any, **kwargs: Any) -> Any:
        await commit_unless_done_flow(db)
        return await call(*args, **kwargs)

    return run


async def record_squash(db: aiosqlite.Connection, task_id: int, before: str) -> None:
    """Факт «гейт переписал историю» - записан и виден до ``push_branch``."""
    await repo.update_task(db, task_id, gate_squashed_sha=before)
    await commit_unless_done_flow(db)
