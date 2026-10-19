"""«О чём задача простым языком» (#1811): запасной текст и граница новых задач."""

from __future__ import annotations

import re
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


#: Запасной текст берётся только если выглядит как фраза, а не как вывод
#: команды, код или секрет (#1811): маскировать секреты по ключевым словам
#: нельзя - sk-/ghp_ проходили, - поэтому подозрительное не показывается вовсе.
LIMIT = 300
_SUSPICIOUS = re.compile(
    r"(?i)sk-\w|\bgh[pousr]_|github_pat_|\bxox[a-z]-|\bAKIA[0-9A-Z]|\bbearer\b"
    r"|\b\w*(key|token|password|passwd|secret)\w*\s*[:=]"
    r"|[A-Za-z0-9+/_=-]{20,}"  # длинная hex/base64/путь-подобная строка
    r"|`|[{}]|\b\w+\s*\(|\w\s*=\s*\S|\$\s|^\s*[#>$]"
)
_SENTENCE_END = re.compile(r"[.!?…](?:\s|$)")


def _fallback(problem_statement: str | None) -> str:
    text = (problem_statement or "").strip()
    end = _SENTENCE_END.search(text)
    sentence = text[: end.end()].strip() if end else text
    if not sentence or len(sentence) > LIMIT or "\n" in sentence:
        return ""
    return "" if _SUSPICIOUS.search(sentence) else sentence


def plain_summary_view(
    plain_summary: str | None, problem_statement: str | None
) -> dict[str, str]:
    """Что показывать читателю и откуда оно взято.

    Пустой plain_summary (NULL или пробелы) не прячется: читатель получает
    problem_statement (первое предложение, если оно чистое), а
    ``plain_summary_source`` называет, что это запасной текст; иначе - ``none``.
    """
    own = (plain_summary or "").strip()
    if own:
        return {"plain_summary_shown": own, "plain_summary_source": "plain_summary"}
    fallback = _fallback(problem_statement)
    return {
        "plain_summary_shown": fallback,
        "plain_summary_source": "problem_statement" if fallback else "none",
    }


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
