"""«О чём задача простым языком» (#1811): запасной текст и граница новых задач."""

from __future__ import annotations

from typing import Any

import aiosqlite
from pydantic import BaseModel, Field


class PlainSummaryInput(BaseModel):
    """Поле записи в TaskCreate и TaskRefine (#1811); вынесено из hub/models.py."""

    plain_summary: str | None = Field(default=None, max_length=300)


class PlainSummaryView(PlainSummaryInput):
    """Читаемая часть TaskView: что показывать и откуда оно взято (#1811).

    Вынесено из hub/models.py (бюджет размера, #1669); ``plain_summary`` -
    как записано (NULL у старых задач) - наследуется от PlainSummaryInput.
    """

    plain_summary_shown: str = ""
    plain_summary_source: str = ""


def plain_summary_view(
    plain_summary: str | None, problem_statement: str | None
) -> dict[str, str]:
    """Что показывать читателю и откуда оно взято.

    Только то, что написал автор постановки. Пустой plain_summary (NULL или
    пробелы) даёт пустой показ и источник ``none``: запасной текст из
    problem_statement убран решением ревьюера (gen3, #1811) - он бывает
    выводом команды, кодом или секретом, и фильтра надёжнее отказа нет.
    ``problem_statement`` оставлен в сигнатуре: читатели зовут её одним видом.
    """
    own = (plain_summary or "").strip()
    if own:
        return {"plain_summary_shown": own, "plain_summary_source": "plain_summary"}
    return {"plain_summary_shown": "", "plain_summary_source": "none"}


async def is_new_task(db: aiosqlite.Connection, task_id: int) -> bool:
    """Создана ли задача после выката plain_summary: ``id`` больше водяного знака.

    Знак - ``MAX(tasks.id)`` на момент миграции (таблица ``plain_summary_boundary``);
    id растёт монотонно, поэтому порядок вставки различим и при одинаковых метках
    времени. Нет знака - границы нет, и задача считается старой: требование не
    должно появиться там, где его некому было выполнить.
    """
    cursor = await db.execute(
        "SELECT t.id > b.max_task_id FROM tasks t, plain_summary_boundary b "
        "WHERE t.id = ? AND b.id = 1",
        (task_id,),
    )
    row: Any = await cursor.fetchone()
    return bool(row and row[0])
