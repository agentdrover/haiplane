"""Причина, по которой бриф задачи на гейте не собрался (#1622).

Белый список: на карточку идёт имя типа исключения и категория из ФИКСИРОВАННОГО
словаря по типу. Текст исключения не выводится никогда: сборка брифа ходит в git
и gh, их stderr несёт URL с токенами, заголовки и пути, и любой фильтр по
чёрному списку пропускает новую форму секрета. Полный текст остаётся в журнале
хаба (``log.warning`` в ``gate_evidence``).
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
import subprocess  # nosec B404 - only for isinstance on SubprocessError
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


# Публикационная граница (#2002, Codex r1-r3): причины приходят из git, форджа и
# исключений, и каждый новый источник мог принести путь клона или URL remote с
# токеном. Источники по одному не отлавливаются; текст чистится ОДИН раз, на выходе.
_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://[^\s'\"`)]+")
_SCP = re.compile(r"(?<![\w.@-])[\w.\-]+@[\w.\-]+:[^\s'\"`)]+")
_ABS_PATH = re.compile(r"(?<![\w.~/\-])~?/[^\s'\"`),;]+")


def public_reason(text: str) -> str:
    """Причина без абсолютных путей и адресов remote; sha, ветки и относительные пути целы.

    ``/srv/clone/x`` -> ``<путь>``, ``https://host/r`` и ``git@host:r`` -> ``<remote>``.
    ``hub/web.py``, ``task-1/x``, ``origin/develop`` не трогаются: слэш внутри слова
    не начало абсолютного пути.
    """
    text = _URL.sub("<remote>", text or "")
    text = _SCP.sub("<remote>", text)
    return _ABS_PATH.sub("<путь>", text)
