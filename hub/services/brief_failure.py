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
from typing import Any

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


# Публикационная граница (#2002, Codex r1-r4). Причины приходят из git, форджа и
# исключений; каждый новый источник мог принести путь клона или URL remote с
# токеном, а чистка «по полю» обходилась тремя раундами подряд. Поэтому чистится
# ВЕСЬ готовый ответ одним проходом (``scrub_strings``), а не поля по одному.
#
# Решения (по реальным полям брифа):
# * ссылок-контракта в брифе две: PR и прогон CI на github.com
#   (``https://github.com/<o>/<r>/pull/N``, ``…/actions/runs/N`` в provenance).
#   Их оставляем; любой другой URL и любой URL с userinfo (``user:token@``) —
#   ``<remote>``, как и scp-адрес ``git@host:path``.
# * Путь файловой системы — только то, что начинается с корня из белого списка
#   (/tmp, /var, /srv, /home, /Users, /opt, /etc, /root, /private, …) или с
#   ``~`` / ``~user``. Маршруты приложения (``/tasks/12``) путём не считаются.
# * Текст, написанный людьми и агентами (название, описание, критерии, итог
#   сдачи, вердикты), не чистится: там путь — содержание, а не утечка.
# * Длина режется до _MAX_SCAN до любого поиска, шаблоны без вложенных
#   кванторов: длинная строка без «://» не должна занимать цикл событий.
_MAX_SCAN = 4000
_ROOTS = "tmp|var|srv|home|Users|opt|etc|root|private|usr|mnt|run|Volumes|data|proc|sys"
_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]{0,15}://[^\s'\"`)<>,;]{1,2000}")
_SCP = re.compile(r"(?<![\w.@-])[\w.\-]{1,64}@[\w.\-]{1,255}:[^\s'\"`)]{1,500}")
_FS = rf"(?:~[\w.\-]{{0,32}}/|/(?:{_ROOTS})(?![\w.\-]))"
_QUOTED_PATH = re.compile(rf"(['\"`]){_FS}[^'\"`\n]{{0,500}}\1")
_ABS_PATH = re.compile(rf"(?<![\w.~/\-]){_FS}[^\s'\"`),;]{{0,500}}")
_PR_LINK = re.compile(
    r"https://github\.com/[\w.\-]{1,100}/[\w.\-]{1,100}/(?:pull|actions/runs)/\d{1,12}"
)


def _remote(match: re.Match[str]) -> str:
    """Оставить ссылку-контракт целиком, всё прочее заменить маркером."""
    url = match.group(0)
    if _PR_LINK.fullmatch(url.rstrip(".")):
        return url
    return "<remote>"


def public_reason(text: str) -> str:
    """Строка без путей файловой системы и адресов remote; sha, ветки, относительные
    пути и маршруты приложения целы. Длиннее _MAX_SCAN режется с маркером «…».
    """
    text = text or ""
    if len(text) > _MAX_SCAN:
        text = text[:_MAX_SCAN] + "…"
    text = _URL.sub(_remote, text)
    text = _SCP.sub("<remote>", text)
    text = _QUOTED_PATH.sub("<путь>", text)
    return _ABS_PATH.sub("<путь>", text)


def scrub_strings(obj: Any, *, skip: frozenset[str] = frozenset()) -> Any:
    """``public_reason`` для КАЖДОЙ строки готового ответа: dict, list, tuple, модели.

    Ключи из ``skip`` (текст людей и агентов) обходятся вместе с поддеревом.
    Модели pydantic правятся на месте, остальное собирается заново.
    """
    if isinstance(obj, str):
        return public_reason(obj)
    if isinstance(obj, dict):
        return {
            k: v if k in skip else scrub_strings(v, skip=skip) for k, v in obj.items()
        }
    if isinstance(obj, (list, tuple)):
        return type(obj)(scrub_strings(v, skip=skip) for v in obj)
    if hasattr(obj, "model_fields"):
        for name in type(obj).model_fields:
            if name not in skip:
                setattr(obj, name, scrub_strings(getattr(obj, name), skip=skip))
    return obj
