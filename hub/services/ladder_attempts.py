"""Долговечная запись попытки добора лестницы ревью (#1946).

Лестница (#879) и вторая ось (#1243) докупают прогон, когда отчёт объявил себя
неполным. До #1946 связь «после какого отчёта -> каким заказом -> чем кончилось»
жила только в ленте событий, а ленту чистит retention (14 дней, ``prune_events``):
после очистки отказанная вторая ось объяснялась как «тоже не дочитала».

Здесь одна строка таблицы ``ladder_attempts`` на попытку, без retention:

* открывается ДО обращения к диспетчеру (``open_attempt``) — упавший вызов
  оставляет открытую строку, и читатель называет её «добор не завершился»;
* закрывается после возврата диспетчера (``close_attempt``): ``placed`` с
  заказом или ``refused`` с причиной.

События ``review_ladder_top_up*`` и ``review_model_cascade`` пишутся как раньше —
это история и вход метрик; ожидание и объяснение читаются отсюда.

Чтение НЕ глотает сбой: «не смог прочитать» и «прочитал, попыток нет» — разные
исходы. Таблица пуста для поколения только у сдач до миграции; тогда читатель
(``ladder_view``) откатывается на события.
"""

from __future__ import annotations

from typing import Any

import aiosqlite

from hub.db import fetchall

KIND_TOP_UP = "top_up"
KIND_SECOND_AXIS = "second_axis"

OUTCOME_PLACED = "placed"
OUTCOME_REFUSED = "refused"


async def open_attempt(
    db: aiosqlite.Connection,
    task_id: int,
    generation: int,
    kind: str,
    after_review_id: int | None,
    *,
    model: str = "",
) -> int:
    """Записать: лестница пошла на добор после ЭТОГО неполного отчёта. Коммитит."""
    cur = await db.execute(
        "INSERT INTO ladder_attempts (task_id, generation, kind, after_review_id, "
        "model) VALUES (?, ?, ?, ?, ?)",
        (task_id, generation, kind, after_review_id, model),
    )
    await db.commit()
    return int(cur.lastrowid or 0)


async def close_attempt(
    db: aiosqlite.Connection,
    attempt_id: int,
    *,
    dispatch_id: int | None,
    reason: str = "",
) -> None:
    """Закрыть попытку: заказ известен — ``placed``, иначе ``refused``. Коммитит.

    Уже закрытая попытка не переписывается: исход пишет ровно один вызов.
    """
    placed = dispatch_id is not None
    await db.execute(
        "UPDATE ladder_attempts SET outcome = ?, dispatch_id = ?, reason = ?, "
        "closed_at = datetime('now') WHERE id = ? AND closed_at IS NULL",
        (
            OUTCOME_PLACED if placed else OUTCOME_REFUSED,
            dispatch_id,
            reason if not placed else "",
            attempt_id,
        ),
    )
    await db.commit()


async def attempts_of_generation(
    db: aiosqlite.Connection, task_id: int, generation: int
) -> list[dict[str, Any]]:
    """Попытки поколения в порядке записи; пусто — строк нет (сдача до #1946).

    Исключение чтения поднимается: молчаливый пустой список выдал бы сбой за
    «добора не было».
    """
    rows = await fetchall(
        db,
        "SELECT * FROM ladder_attempts WHERE task_id = ? AND generation = ? "
        "ORDER BY id",
        (task_id, generation),
    )
    return [dict(r) for r in rows]


def is_open(attempt: dict[str, Any]) -> bool:
    """Попытка записана, а диспетчер ещё не вернул ответ (или вызов упал)."""
    return attempt.get("closed_at") is None
