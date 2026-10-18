"""«О чём задача простым языком» (#1811): запасной текст и граница новых задач."""

from __future__ import annotations

from typing import Any

import aiosqlite
from pydantic import BaseModel

#: Миграция колонки. Её ``applied_at`` - граница: задача, созданная не раньше,
#: «новая», и DoR требует у неё plain_summary; старым он только рекомендован.
MIGRATION = "add_tasks_plain_summary"


class PlainSummaryView(BaseModel):
    """Читаемая часть TaskView: что показывать и откуда оно взято (#1811).

    Вынесено из hub/models.py (бюджет размера, #1669); ``plain_summary`` -
    как записано (NULL у старых задач) - объявлено в самом TaskView.
    """

    plain_summary_shown: str = ""
    plain_summary_source: str = ""


def plain_summary_view(
    plain_summary: str | None, problem_statement: str | None
) -> dict[str, str]:
    """Что показывать читателю и откуда оно взято.

    Пустой plain_summary (NULL или пробелы) не прячется: читатель получает
    problem_statement, а ``plain_summary_source`` называет, что это запасной текст.
    """
    own = (plain_summary or "").strip()
    if own:
        return {"plain_summary_shown": own, "plain_summary_source": "plain_summary"}
    fallback = (problem_statement or "").strip()
    return {
        "plain_summary_shown": fallback,
        "plain_summary_source": "problem_statement" if fallback else "",
    }


async def is_new_task(db: aiosqlite.Connection, task_id: int) -> bool:
    """Создана ли задача не раньше применения миграции plain_summary.

    Нет записи о миграции - границы нет, и задача считается старой: требование
    не должно появиться там, где его некому было выполнить.
    """
    cursor = await db.execute(
        "SELECT t.created_at >= m.applied_at FROM tasks t, _migrations m "
        "WHERE t.id = ? AND m.name = ?",
        (task_id, MIGRATION),
    )
    row: Any = await cursor.fetchone()
    return bool(row and row[0])
