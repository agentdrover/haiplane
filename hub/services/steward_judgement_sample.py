"""Образец тела суждения судьи (#1924).

Один источник для промпта судьи и для 422: судья видит тот же образец, что и
в отказе. Отдельный модуль, а не hub/models.py: файл моделей на потолке
размера.
"""

from __future__ import annotations

import json


def steward_judgement_body_sample(generation: int) -> str:
    """Ровно поля контракта #1022/#1327 для approve.

    Одинарных кавычек в тексте нет — образец вставляется в ``curl -d '...'``.
    """
    return json.dumps(
        {
            "generation": generation,
            "kind": "verdict",
            "verdict": "approve",
            "confidence": "high",
            "grounds": [
                {
                    "source": "ci_pinned_sha",
                    "detail": "конкретный факт пакета: значение, число, sha или поле",
                }
            ],
            "closures": [{"finding_uid": "uid из пакета", "type": "fixed"}],
        },
        ensure_ascii=False,
    )
