"""Образец тела суждения судьи (#1924).

Один источник для промпта судьи и для 422: судья видит тот же образец, что и
в отказе. Отдельный модуль, а не hub/models.py: файл моделей на потолке
размера.

Образец НЕИСПОЛНИМ как есть. Каждое поле, где судья обязан выбрать или
назвать факт, несёт текст-подсказку, а не допустимое значение: verdict,
confidence и type закрытых словарей не пройдут разбор, а detail-заглушка не
засчитывается как основание (:func:`is_placeholder_detail`). Иначе судья,
скопировавший образец и убравший лишнее, получил бы «применимое» одобрение с
подсказкой вместо факта.
"""

from __future__ import annotations

import json

#: Подсказки образца. Любая из них в ``detail`` основания — не факт пакета.
PLACEHOLDER_DETAILS: tuple[str, ...] = ("<факт пакета: значение, число, sha или поле>",)
PLACEHOLDER_FINDING_UID = "<finding_uid находки из пакета>"
_VERDICT_HINT = "<approve|changes_requested|escalate>"
_CONFIDENCE_HINT = "<high|medium|low>"
_CLOSURE_TYPE_HINT = "<тип закрытия из закрытого множества>"
_SOURCE_HINT = "<источник из закрытого множества>"

_HINTS = (
    *PLACEHOLDER_DETAILS,
    PLACEHOLDER_FINDING_UID,
    _SOURCE_HINT,
    _CLOSURE_TYPE_HINT,
    _VERDICT_HINT,
    _CONFIDENCE_HINT,
    "<суждение по контракту>",
    "<одна причина из закрытого множества>",
)
_BARE_HINTS = frozenset(h.strip("<>").strip().casefold() for h in _HINTS)


def is_placeholder_detail(detail: str) -> bool:
    """True, если detail — подсказка образца, а не факт пакета.

    Подсказка — это любая строка в угловых скобках целиком, а также текст
    любой подсказки образца без скобок (судья убрал скобки и оставил слова).
    """
    text = (detail or "").strip()
    if len(text) >= 2 and text.startswith("<") and text.endswith(">"):
        return True
    return text.strip("<>").strip().casefold() in _BARE_HINTS


def steward_judgement_body_sample(generation: int) -> str:
    """Поля контракта #1022/#1327; значения-подсказки в угловых скобках.

    Одинарных кавычек в тексте нет — образец вставляется в ``curl -d '...'``.
    """
    return json.dumps(
        {
            "generation": generation,
            "kind": "verdict",
            "verdict": _VERDICT_HINT,
            "confidence": _CONFIDENCE_HINT,
            "grounds": [{"source": _SOURCE_HINT, "detail": PLACEHOLDER_DETAILS[0]}],
            "closures": [
                {"finding_uid": PLACEHOLDER_FINDING_UID, "type": _CLOSURE_TYPE_HINT}
            ],
        },
        ensure_ascii=False,
    )


def steward_escalate_body_sample(generation: int) -> str:
    """Образец эскалации: оснований не требует, причина из закрытого множества."""
    return json.dumps(
        {
            "generation": generation,
            "kind": "verdict",
            "verdict": "escalate",
            "escalate_reason": "<одна причина из закрытого множества>",
        },
        ensure_ascii=False,
    )
