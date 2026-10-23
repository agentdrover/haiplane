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
from enum import Enum
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
# * Чистятся ТОЛЬКО строки под ключами-диагнозами (белый список по имени ключа,
#   ``_REASON_KEY``): reason, *_reason, why, detail, error, unavailable. Всё
#   остальное — текст людей и агентов (text, note, observation, headline, lines,
#   description, summary) или код — не трогается: там путь — содержание, а не
#   утечка, и чёрный список «авторских» полей всякий раз что-то пропускал.
#   Новое поле ``foo_reason`` покрыто само. Не-строки (Enum, числа) проходят как есть.
# * Вход НЕ режется (#2006): длина одного совпадения ограничена (_MAXM), шаблоны
#   без вложенных кванторов, обход линеен. Прежняя обрезка до 4000 знаков
#   отрезала хвост JSON вместе с закрывающими скобками.
_ROOTS = "tmp|var|srv|home|Users|opt|etc|root|private|usr|mnt|run|Volumes|data|proc|sys"
# Конец URL и пути (#2006): пробел, кавычка, скобки ()[]{}<>, «|» (ячейка
# markdown-таблицы), а «,» и «;» - только перед пробелом или концом строки (внутри
# URL они бывают). Длина ОДНОГО совпадения ограничена, а вход не режется:
# шаблоны линейны, JSON и длинные токены не теряют хвост.
_MAXM = 2048
_END = r"""\s'"`(){}\[\]<>|"""
_TAIL = rf"(?:[^{_END},;]|[,;](?![\s]|$))"
# Хвост, дошедший до потолка _MAXM, дочитывается до пробела или жёсткого
# разделителя (_REST): токен длиннее потолка не публикует «?token=…» после него.
# Две ветки без вложенных кванторов, обход линеен.
_REST = r"""[^\s'"`<>|(){}\[\]]*"""


def _run(low: int) -> str:
    return rf"(?:{_TAIL}{{{_MAXM}}}{_REST}|{_TAIL}{{{low},{_MAXM - 1}}})"


_SCHEME = r"[A-Za-z][A-Za-z0-9+.\-]{0,15}://"


def _host(low: int) -> str:
    """Хост: ЛИБО [ipv6] (без обязательных знаков после него), ЛИБО обычный; далее порт и путь."""
    return rf"(?:\[[^\]\s]{{1,64}}\]{_run(0)}|{_run(low)})"


# URL с учётными данными (#2006, раунд 7). Безопасность важнее формы текста:
# если в куске после ``scheme://`` до пробела (или конца текста) есть «@» ГДЕ БЫ
# ТО НИ БЫЛО, весь кусок - ``<remote>``, без исключений по разделителям. Класс
# символов userinfo каждый раунд оказывался дырявым («|», «{}», кавычки, пустой
# userinfo перед [ipv6]); кусок без пробелов с «@» не разбирается вовсе. Цена
# осознанная и закреплена тестом: в компактном JSON или таблице без пробелов
# соседняя ссылка или поле до ближайшего пробела пропадают вместе с ним. Утечка
# хуже испорченного форматирования. Кусок читается ``\S*`` - линейно, без
# вложенных кванторов; обратного хода нет.
_CRED_CHUNK = re.compile(rf"{_SCHEME}\S*")


def _credential_chunk(match: re.Match[str]) -> str:
    chunk = match.group(0)
    return "<remote>" if "@" in chunk else chunk


_URL = re.compile(rf"{_SCHEME}{_host(1)}")
_SCP_HOST = r"(?:\[[^\]\s]{1,64}\]|[\w.\-]{1,255})"
_SCP = re.compile(rf"(?<![\w.@-])[\w.\-]{{1,64}}@{_SCP_HOST}:{_run(1)}")
_FS = rf"(?:~[\w.\-]{{0,32}}/|/(?:{_ROOTS})(?![\w.\-]))"
# Значение JSON ("ключ": "/путь", ["/путь"]) - маркер ВНУТРИ кавычек, строка
# остаётся строкой и json.loads её читает.
_JSON_PATH = re.compile(rf'(?P<pre>"\s*:\s*|[\[,]\s*)"{_FS}(?:[^"\\\n]|\\.)*"')
# Путь в кавычках (#2006, раунд 8): закрывающая кавычка - та, за которой ТОЛЬКО
# пробел или конец текста; кавычка внутри имени с любым знаком после неё
# (',', ')', ';', '|', ...) путь не обрывает, а \' и \" внутри - экранирование,
# как в _JSON_PATH. Нет такой кавычки (неясно) - путь берётся до пробела
# (``_OPEN_QUOTE_PATH``), и знаки после него теряются: безопасность важнее формы.
_QUOTED_PATH = re.compile(rf"(['\"`]){_FS}(?:[^\\\n]|\\.){{0,{_MAXM}}}?\1(?=\s|\Z)")
_OPEN_QUOTE_PATH = re.compile(rf"['\"`]{_FS}\S*")
_ABS_PATH = re.compile(rf"(?<![\w.~/\-]){_FS}{_run(0)}")
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
    пути и маршруты приложения целы. Вход не режется: длина совпадения ограничена.

    Правила безопасности (#2006): кусок ``scheme://...`` без пробелов, в котором
    есть «@», заменяется целиком (утечка хуже испорченного форматирования); путь
    в кавычках с кавычкой внутри берётся до закрывающей кавычки, за которой только
    пробел или конец текста (\' и \" внутри - экранирование), а если такой нет - до
    пробела вместе со знаками после пути. URL без «@» и scp-адрес идут по
    правилам с разделителями, поэтому таблицы, JSON и ссылки без учётных данных
    остаются читаемыми.
    """
    text = _CRED_CHUNK.sub(_credential_chunk, text or "")
    text = _URL.sub(_remote, text)
    text = _SCP.sub("<remote>", text)
    text = _JSON_PATH.sub(r'\g<pre>"<путь>"', text)
    text = _QUOTED_PATH.sub("<путь>", text)
    text = _OPEN_QUOTE_PATH.sub("<путь>", text)
    return _ABS_PATH.sub("<путь>", text)


# Отчёты, написанные ревьюером или автором: ``detail``, ``reason``, ``why``
# внутри них — содержание («пишет в /etc/hosts вместо /etc/resolv.conf»), а не
# диагноз хаба. Поддерево под этими ключами не обходится вовсе.
_AUTHORED_SUBTREES = frozenset({"machine_review", "review_report", "latest_review"})
_REASON_KEY = re.compile(r"(?:.*_)?(?:reasons?|why|details?|errors?|unavailable)")


def scrub_strings(obj: Any, *, key: str = "") -> Any:
    """``public_reason`` для строк под ключами-диагнозами во всём готовом ответе.

    Обходит dict, list, tuple и модели pydantic рекурсивно; строка чистится, если
    имя ключа (или ключа списка, в котором она лежит) подходит под ``_REASON_KEY``.
    Модели правятся на месте, остальное собирается заново.
    """
    if isinstance(obj, Enum):
        return obj
    if key in _AUTHORED_SUBTREES:
        return obj
    if isinstance(obj, str):
        return public_reason(obj) if _REASON_KEY.fullmatch(key) else obj
    if isinstance(obj, dict):
        return {k: scrub_strings(v, key=str(k)) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return type(obj)(scrub_strings(v, key=key) for v in obj)
    if hasattr(obj, "model_fields"):
        for name in type(obj).model_fields:
            setattr(obj, name, scrub_strings(getattr(obj, name), key=name))
    return obj
