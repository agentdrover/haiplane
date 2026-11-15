"""Что ревьюер проверил, когда кандидатов в дефекты не нашёл (#2336).

``raw_count=0`` не различает «ревьюер всё прочитал и прогнал тесты» и «ревьюер
не запускался»: хаб читал оба как «нет данных» (#750), и человек не видел ни
одной сделанной проверки. Список ``checks_done`` — слово ревьюера о себе: что
проверено, чем (команда или чтение кода) и с каким итогом. Хаб его не
перепроверяет и не выдаёт за факт; доверие к автовердикту даёт только счёт
провайдера по собственному диспетчу хаба (``auto_verdict``).

Список принимается мягко. Единственный путь облачного ревьюера — текстовый блок,
а ``parse_report_block`` отбрасывает отчёт целиком, если поле отказано, так что
кривая запись стоила бы находок: нечитаемая запись выкидывается (и логируется),
длинная режется. Мягкость идёт только в сторону недоверия — запись, которая
не названа целиком, проверкой не считается и «проверено» не создаёт.
"""

from __future__ import annotations

import json
import logging
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, Field

log = logging.getLogger("hub")

CHECK_WHAT_LIMIT = 200
CHECK_HOW_LIMIT = 300
CHECK_RESULT_LIMIT = 300
CHECKS_MAX = 40

#: Потолок хранимого текста облачного прогона, знаков (после ``scrub_secrets``).
#: Режется хвост: начало рассказа о проверках дороже, чем JSON-блок отчёта в
#: конце, который уже лежит в полях отчёта.
RUN_TEXT_LIMIT = 6000
RUN_TEXT_CUT_MARK = "\n…[текст прогона обрезан хабом]"

#: Пометка у показа текста: это слово прогона о себе, а не факт, который хаб
#: проверил.
RUN_TEXT_NOTE = (
    "слово ревьюера о себе: текст его прогона, хаб его не проверял и за факт не выдаёт"
)

_FIELDS = (
    ("what", CHECK_WHAT_LIMIT),
    ("how", CHECK_HOW_LIMIT),
    ("result", CHECK_RESULT_LIMIT),
)


class CheckDone(BaseModel):
    """Одна проверка ревьюера: что, чем, с каким итогом. Все три слова обязательны."""

    what: str = Field(..., min_length=1, max_length=CHECK_WHAT_LIMIT)
    how: str = Field(..., min_length=1, max_length=CHECK_HOW_LIMIT)
    result: str = Field(..., min_length=1, max_length=CHECK_RESULT_LIMIT)


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _entry(raw: Any) -> dict[str, str] | None:
    """Запись, приведённая к трём строкам по потолкам, или ``None``, если она не названа."""
    if not isinstance(raw, dict):
        return None
    entry: dict[str, str] = {}
    for name, limit in _FIELDS:
        value = raw.get(name)
        text = value.strip() if isinstance(value, str) else ""
        if not text:
            return None
        entry[name] = _cut(text, limit)
    return entry


def clean_checks(value: Any) -> list[dict[str, str]]:
    """Список проверок из чего угодно: только названные записи, не больше ``CHECKS_MAX``."""
    if isinstance(value, str):
        try:
            value = json.loads(value or "[]")
        except ValueError:
            return []
    if not isinstance(value, list):
        return []
    entries = [e for e in map(_entry, value) if e is not None]
    if len(entries) < len(value) or len(entries) > CHECKS_MAX:
        log.warning(
            "machine review checks_done: %s of %s entries kept (all three words "
            "are required, at most %s)",
            min(len(entries), CHECKS_MAX),
            len(value),
            CHECKS_MAX,
        )
    return entries[:CHECKS_MAX]


#: Тип поля ``checks_done`` и в приёме отчёта, и в показе (строка БД — JSON).
CheckList = Annotated[list[CheckDone], BeforeValidator(clean_checks)]


def cap_run_text(text: str) -> str:
    """Текст прогона для хранения: потолок длины, с пометкой об обрезке."""
    text = text.strip()
    if len(text) <= RUN_TEXT_LIMIT:
        return text
    return text[: RUN_TEXT_LIMIT - len(RUN_TEXT_CUT_MARK)] + RUN_TEXT_CUT_MARK


def dropped_checks(sent: Any) -> tuple[int, int]:
    """Сколько записей ``clean_checks`` отбросил и почему: ``(без слова, сверх потолка)``.

    Считает по тому же ``_entry``, что и сам отбор: две причины не смешиваются,
    и квитанция не называет нехватку слов там, где хвост срезал потолок.
    """
    if not isinstance(sent, list):
        return 0, 0
    named = sum(1 for raw in sent if _entry(raw) is not None)
    return len(sent) - named, max(0, named - CHECKS_MAX)


def checks_receipt(sent: Any, kept: int) -> str:
    """Фраза квитанции о списке проверок: сколько записей хаб принял (#2336).

    ``sent`` - что послал ревьюер, ``kept`` - сколько хаб записал. Пусто, когда
    список не посылали. Отброшенное названо по причинам: молчание об этом
    оставило бы ревьюера уверенным, что «проверено».
    """
    if not sent:
        return ""
    if kept == len(sent):
        return f" Список проверок принят: {kept} (слово ревьюера)."
    unnamed, over = dropped_checks(sent)
    reasons = []
    if unnamed:
        reasons.append(f"{unnamed} без одного из слов what, how, result")
    if over:
        reasons.append(f"{over} сверх потолка {CHECKS_MAX}")
    why = f" Отброшено: {'; '.join(reasons)}." if reasons else ""
    return f" Список проверок: принято {kept} из {len(sent)}.{why}"
