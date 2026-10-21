"""Причина, по которой бриф задачи на гейте не собрался (#1622).

Текст исключения попадает в HTML карточки, а сборка брифа ходит в git и gh:
их stderr несёт URL remote с токеном, заголовки и абсолютные пути. Поэтому
причина — имя типа и ПЕРВАЯ строка текста, пропущенная через санитайзер хаба
(URL, учётные данные, Bearer/ghp_, известные секреты) плюс маски пути и
``token=``-пар. Санация идёт до обрезки.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from hub.services.steward_breaker import _safe as _scrub_line

_PATH = re.compile(r"(?<![\w.])/(?:[\w.@+~-]+/)*[\w.@+~-]*")
_PAIR = re.compile(
    r"\b(?:token|secret|password|passwd|api[_-]?key|key|authorization)\s*[=:]\s*\S+",
    re.IGNORECASE,
)
_BARE = re.compile(r"\b(?:sk|crsr|glpat|xox[abp])[-_][\w-]+", re.IGNORECASE)
_LIMIT = 200


@dataclass(frozen=True)
class BriefFailure:
    """The brief of a gate task could not be assembled: the safe cause."""

    reason: str


def failure_from(exc: BaseException) -> BriefFailure:
    """Safe reason: exception type plus a scrubbed first line of its text."""
    name = type(exc).__name__
    line = _scrub_line(str(exc), limit=10_000)
    line = _PAIR.sub("[redacted]", line)
    line = _BARE.sub("[redacted]", line)
    line = _PATH.sub("<путь>", line).strip()
    return BriefFailure(f"{name}: {line}"[:_LIMIT] if line else f"сбой сборки: {name}")
