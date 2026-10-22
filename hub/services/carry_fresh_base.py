"""Сверка «правка та же» для переноса отчёта — на свежей базе (#1990).

Продолжение #1361/#1983: клон хранит ``origin/<base>`` на момент последнего
fetch. Пересдача, где слита только новая база, на устаревшем ref даёт разные
диффы (чужие коммиты базы выглядят правкой автора), и хаб платит за
повторное ревью неизменной работы. Базу обновляет тот же механизм, что у
сверки одобрения (``orchestration._gate_update_base``) — не второй fetch.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import aiosqlite

log = logging.getLogger(__name__)

# Свежая база нужна читателю ожидания стюарда (read_only) и диспетчеру в одном
# цикле: без памяти это два-три fetch подряд на одну и ту же пересдачу.
# Помним только УСПЕШНОЕ обновление, коротко: отказ повторяется в
# следующий раз, а устаревшее знание о базе не живёт дольше окна.
FRESH_BASE_TTL_SECONDS = 60.0
_refreshed: dict[tuple[int, str], float] = {}


def _clock() -> float:
    return time.monotonic()


async def carry_is_same_edit(
    db: aiosqlite.Connection, task: dict[str, Any], source_sha: str, pinned: str
) -> tuple[bool, str]:
    """``(правка та же, основание)``; базу не получили -> ``(False, причина)``.

    Одинаковые коммиты сравнивать нечего — fetch не нужен. «Не получили
    базу» — не «правка автора»: ответ тот же (не переносим), но причина
    называется в логе, а не теряется.
    """
    from hub.services import orchestration

    key = (int(task["id"]), pinned)
    now = _clock()
    if (
        source_sha != pinned
        and now - _refreshed.get(key, -1e9) > FRESH_BASE_TTL_SECONDS
    ):
        waiting = await orchestration._gate_update_base(db, task)
        if waiting:
            log.warning("carry-over of the report for #%s: %s", task["id"], waiting)
            return False, waiting
        for old in [
            k for k, t in _refreshed.items() if now - t > FRESH_BASE_TTL_SECONDS
        ]:
            del _refreshed[old]
        _refreshed[key] = now
    return await orchestration.base_merge_kept_the_verdict(db, task, source_sha, pinned)
