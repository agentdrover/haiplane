"""Причина, по которой бриф задачи на гейте не собрался (#1622).

Белый список: на карточку идёт имя типа исключения и категория из ФИКСИРОВАННОГО
словаря по типу. Текст исключения не выводится никогда: сборка брифа ходит в git
и gh, их stderr несёт URL с токенами, заголовки и пути, и любой фильтр по
чёрному списку пропускает новую форму секрета. Полный текст остаётся в журнале
хаба (``log.warning`` в ``gate_evidence``).
"""

from __future__ import annotations

import asyncio
import sqlite3
import subprocess
from dataclasses import dataclass

_HINT = "подробности в логе хаба"


@dataclass(frozen=True)
class BriefFailure:
    """The brief of a gate task could not be assembled: a safe, fixed-word cause."""

    reason: str


def _category(exc: BaseException) -> str:
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return "таймаут"
    if isinstance(exc, subprocess.SubprocessError):
        return "ошибка git"
    if isinstance(exc, sqlite3.Error):
        return "ошибка базы"
    if isinstance(exc, OSError):
        return "ошибка git"
    return "внутренняя ошибка"


def failure_from(exc: BaseException) -> BriefFailure:
    """Type name plus a category word; the exception text never leaves the log."""
    return BriefFailure(f"{type(exc).__name__} ({_category(exc)}), {_HINT}")
