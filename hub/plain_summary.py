"""«О чём задача простым языком» (#1811): запасной текст и граница новых задач."""

from __future__ import annotations

import re
from typing import Any

import aiosqlite
from pydantic import BaseModel, Field

#: Миграция колонки. Её ``applied_at`` - граница: задача, созданная строго позже,
#: «новая», и DoR требует у неё plain_summary; старым он только рекомендован.
MIGRATION = "add_tasks_plain_summary"


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


#: Предел поля и вырезаемые секреты запасного текста: problem_statement бывает
#: выводом команды, а новое поле должно оставаться короткой фразой (#1811).
LIMIT = 300
_BEARER = re.compile(r"(?i)\b(bearer)\s+\S+")
_SECRET = re.compile(
    r"(?i)\b(bearer|token|authorization|api[_-]?key|password|secret)\b"
    r"(\s*[:=]?\s*)\S+"
)


def _fallback(problem_statement: str | None) -> str:
    lines = (ln.strip() for ln in (problem_statement or "").splitlines())
    first = next((ln for ln in lines if ln), "")
    text = _BEARER.sub(r"\1 [redacted]", first)
    text = _SECRET.sub(r"\1\2[redacted]", text)
    return text if len(text) <= LIMIT else text[: LIMIT - 1] + "…"


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
    fallback = _fallback(problem_statement)
    return {
        "plain_summary_shown": fallback,
        "plain_summary_source": "problem_statement" if fallback else "",
    }


async def is_new_task(db: aiosqlite.Connection, task_id: int) -> bool:
    """Создана ли задача строго после применения миграции plain_summary.

    Метки с точностью до секунды: задача той же секунды, что и миграция, могла
    быть вставлена до неё, и ошибиться безопасно только в сторону «старая»
    (требование не блокирует), а не «новая» (отнимает DoR).

    Нет записи о миграции - границы нет, и задача считается старой: требование
    не должно появиться там, где его некому было выполнить.
    """
    cursor = await db.execute(
        "SELECT t.created_at > m.applied_at FROM tasks t, _migrations m "
        "WHERE t.id = ? AND m.name = ?",
        (task_id, MIGRATION),
    )
    row: Any = await cursor.fetchone()
    return bool(row and row[0])
