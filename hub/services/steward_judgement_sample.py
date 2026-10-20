"""Образец тела суждения судьи (#1924).

Один источник для промпта судьи и для 422: судья видит тот же образец, что и
в отказе. Отдельный модуль, а не hub/models.py: файл моделей на потолке
размера.

Образец НЕИСПОЛНИМ как есть. Каждое поле, где судья обязан выбрать или
назвать факт, несёт текст-подсказку, а не допустимое значение: verdict,
confidence и source закрытых словарей не пройдут разбор, а detail с маркером
``__ЗАПОЛНИ__`` не засчитывается как основание (:func:`is_placeholder_detail`). Иначе судья,
скопировавший образец и убравший лишнее, получил бы «применимое» одобрение с
подсказкой вместо факта.
"""

from __future__ import annotations

import json
import unicodedata

#: Служебный маркер: в образце им заменён каждый заполняемый ``detail``.
#: Распознаётся по СОДЕРЖАНИЮ после нормализации, а не по виду текста:
#: чёрный список «как выглядит подсказка» не сходится (приписки, точки,
#: невидимые символы, другие скобки) и совпадает с настоящими фактами вроде
#: ``<none>`` (#1924, возвраты gen1 и gen2).
DETAIL_MARKER = "__ЗАПОЛНИ__"
_VERDICT_HINT = "<approve|changes_requested|escalate>"
_CONFIDENCE_HINT = "<high|medium|low>"
_SOURCE_HINT = "<источник из закрытого множества>"


#: Буквы-невидимки: категория L, но на экране пусто (Hangul filler и т.п.).
_BLANK_LETTERS = frozenset("\u115f\u1160\u3164\uffa0")


def _letters_and_digits(text: str) -> str:
    return "".join(
        ch
        for ch in text
        if unicodedata.category(ch)[0] in ("L", "N") and ch not in _BLANK_LETTERS
    )


def _visible(text: str) -> str:
    """Только буквы и цифры после NFKC и casefold: проекция «что видно».

    Пробелы, знаки, подчёркивания, форматные и комбинирующие символы
    (U+200B, U+034F, U+FE0F, NBSP) в проекцию не попадают, поэтому вставка
    любого из них не прячет маркер и не делает пустую строку «непустой».
    Фильтр стоит ДО и ПОСЛЕ casefold: сам casefold превращает комбинирующий
    U+0345 в букву, и одного прохода после него мало.
    """
    folded = _letters_and_digits(unicodedata.normalize("NFKC", text or ""))
    return _letters_and_digits(unicodedata.normalize("NFKC", folded.casefold()))


_MARKER_KEY = _visible(DETAIL_MARKER)


def has_visible_content(detail: str) -> bool:
    """True, если в detail есть хоть одна видимая буква или цифра."""
    return bool(_visible(detail))


def is_placeholder_detail(detail: str) -> bool:
    """True, если в detail остался служебный маркер образца.

    Только маркер: общие слова без факта и форма записи (``<none>``,
    ``<ci_run=1234>``) основание не отменяют.
    """
    return _MARKER_KEY in _visible(detail)


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
            "grounds": [{"source": _SOURCE_HINT, "detail": DETAIL_MARKER}],
            "closures": [],
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
