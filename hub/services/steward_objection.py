"""Вид возражения советника стюарда (#2025): чтение, только аналитика.

Основание возражения несёт ``kind`` из ``STEWARD_OBJECTION_KINDS``. Вид
классифицирует возражение для счётчика контура и показа человеку. На
применение он НЕ влияет: стюард одобряет лишь при обоснованном concur, а
object любого вида, в том числе без вида, остаётся у человека
(``steward_advisor.advisor_refusal``).
"""

from __future__ import annotations

import json
from typing import Any

from hub.models import STEWARD_OBJECTION_KINDS

#: Возражение, основания которого несут разные виды: однозначно не отнести.
KIND_MIXED = "mixed"
#: Возражение без единого основания с известным видом.
KIND_UNCLASSIFIED = "unclassified"
#: Ключи счётчика в порядке показа; сумма значений равна числу object.
OBJECTION_COUNT_KEYS: tuple[str, ...] = (
    *STEWARD_OBJECTION_KINDS,
    KIND_MIXED,
    KIND_UNCLASSIFIED,
)


def empty_kind_counts() -> dict[str, int]:
    return dict.fromkeys(OBJECTION_COUNT_KEYS, 0)


def objection_kind(raw_grounds: Any) -> str:
    """Один вид на возражение: общий вид оснований, ``mixed`` или ``unclassified``.

    Нечитаемые и неизвестные виды не считаются: неясный случай остаётся
    ``unclassified``, а не угадывается.
    """
    try:
        grounds = (
            json.loads(raw_grounds) if isinstance(raw_grounds, str) else raw_grounds
        )
    except ValueError:
        return KIND_UNCLASSIFIED
    kinds = {
        str(g.get("kind") or "").strip() for g in (grounds or []) if isinstance(g, dict)
    } & set(STEWARD_OBJECTION_KINDS)
    if not kinds:
        return KIND_UNCLASSIFIED
    return next(iter(kinds)) if len(kinds) == 1 else KIND_MIXED
