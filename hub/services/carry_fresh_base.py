"""Сверка «правка та же» для переноса отчёта — на свежей базе (#1990).

Продолжение #1361/#1983: клон хранит ``origin/<base>`` на момент последнего
fetch. Пересдача, где слита только новая база, на устаревшем ref даёт разные
диффы (чужие коммиты базы выглядят правкой автора), и хаб платит за
повторное ревью неизменной работы. Базу обновляет тот же механизм, что у
сверки одобрения (``orchestration._gate_update_base``) — не второй fetch.
"""

from __future__ import annotations

import logging
from typing import Any

import aiosqlite

log = logging.getLogger(__name__)


async def carry_is_same_edit(
    db: aiosqlite.Connection, task: dict[str, Any], source_sha: str, pinned: str
) -> tuple[bool, str]:
    """``(правка та же, основание)``; базу не получили -> ``(False, причина)``.

    Одинаковые коммиты сравнивать нечего — fetch не нужен. «Не получили
    базу» — не «правка автора»: ответ тот же (не переносим), но причина
    называется в логе, а не теряется.
    """
    from hub.services import orchestration

    if source_sha != pinned:
        waiting = await orchestration._gate_update_base(db, task)
        if waiting:
            log.warning("carry-over of the report for #%s: %s", task["id"], waiting)
            return False, waiting
    return await orchestration.base_merge_kept_the_verdict(
        db, task, source_sha, pinned
    )
