"""Контракт доказательств сдачи задачи-состояния (#1647). Чистые функции.

Сдача state-задачи — не коммит, а список наблюдений: по одной записи на каждый
критерий приёмки (AC). Запись — ``{ac_id, action, observed, target,
observed_at}``:

* ``action`` — что выполнено: команда, запрос или ручное действие;
* ``observed`` — что увидели в ответ (обезличенно: правило отправителя);
* ``target`` — над каким объектом или окружением;
* ``observed_at`` — когда.

Хаб команды из ``action`` НЕ исполняет (решение 31.07): он хранит заявление
автора и привязывает его к поколению сдачи и к снимку AC. Контракт проверяет
форму и полноту, а не истинность.

ОШИБКИ НЕ ВОЗВРАЩАЮТ ВХОДНЫЕ ЗНАЧЕНИЯ. В ``observed`` может оказаться секрет, и
отказ, цитирующий его, сам становится утечкой, поэтому ``EvidenceError`` несёт
только ВИД ошибки, имя поля из закрытого набора и номера AC, которые читатель
уже видит в постановке (идентификатор вида ``AC-N``, иначе — без эха).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from hub.services.secret_values import find_secret

#: Поля записи — закрытый набор: лишнее поле тоже ошибка, но без его имени.
FIELDS: tuple[str, ...] = ("ac_id", "action", "observed", "target", "observed_at")

#: Предел длины по полю (после strip). Пределы в одном месте с контрактом.
LIMITS: dict[str, int] = {
    "ac_id": 20,
    "action": 1000,
    "observed": 4000,
    "target": 300,
    "observed_at": 64,
}

#: Потолок записей на сдачу; AC у задачи не больше (models.MAX_ACCEPTANCE_CRITERIA).
MAX_ITEMS = 50

#: Виды ошибок — то, что читает клиент.
KIND_TYPE = "type"
KIND_EMPTY_SET = "empty_set"
KIND_TOO_MANY = "too_many"
KIND_UNKNOWN_FIELD = "unknown_field"
KIND_MISSING_FIELD = "missing_field"
KIND_EMPTY_VALUE = "empty_value"
KIND_TOO_LONG = "too_long"
KIND_CREDENTIAL = "secret"
KIND_DUPLICATE_AC = "duplicate_ac"
KIND_UNKNOWN_AC = "unknown_ac"
KIND_MISSING_AC = "missing_ac"

_SAFE_AC = re.compile(r"^AC-\d{1,6}$")


class EvidenceError(ValueError):
    """Ошибка контракта доказательств: вид, поле и AC — и больше ничего."""

    def __init__(
        self,
        kind: str,
        *,
        ac_ids: Iterable[str] = (),
        field: str = "",
        pattern: str = "",
    ) -> None:
        super().__init__(kind)
        self.kind = kind
        self.ac_ids = sorted(set(ac_ids))
        self.field = field
        self.pattern = pattern


def _safe_ac(value: Any) -> list[str]:
    """AC-идентификатор для ответа: только строгой формы ``AC-N``, иначе пусто."""
    if isinstance(value, str) and _SAFE_AC.match(value):
        return [value]
    return []


def _check_shape(items: Any) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        raise EvidenceError(KIND_TYPE)
    if not items:
        raise EvidenceError(KIND_EMPTY_SET)
    if len(items) > MAX_ITEMS:
        raise EvidenceError(KIND_TOO_MANY)
    for item in items:
        if not isinstance(item, dict):
            raise EvidenceError(KIND_TYPE)
        if set(item) - set(FIELDS):
            raise EvidenceError(KIND_UNKNOWN_FIELD, ac_ids=_safe_ac(item.get("ac_id")))
        for field in FIELDS:
            if field not in item:
                raise EvidenceError(
                    KIND_MISSING_FIELD,
                    ac_ids=_safe_ac(item.get("ac_id")),
                    field=field,
                )
            if not isinstance(item[field], str):
                raise EvidenceError(
                    KIND_TYPE, ac_ids=_safe_ac(item.get("ac_id")), field=field
                )
    return items


def _check_values(items: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Пустота, длина, секрет — по каждому полю каждой записи."""
    clean: list[dict[str, str]] = []
    for item in items:
        ac = _safe_ac(item["ac_id"])
        row = {field: item[field].strip() for field in FIELDS}
        for field in FIELDS:
            if not row[field]:
                raise EvidenceError(KIND_EMPTY_VALUE, ac_ids=ac, field=field)
        for field in FIELDS:
            if len(row[field]) > LIMITS[field]:
                raise EvidenceError(KIND_TOO_LONG, ac_ids=ac, field=field)
        for field in FIELDS:
            pattern = find_secret(row[field])
            if pattern:
                raise EvidenceError(
                    KIND_CREDENTIAL, ac_ids=ac, field=field, pattern=pattern
                )
        clean.append(row)
    return clean


def _check_coverage(items: list[dict[str, str]], known: set[str]) -> None:
    """Ровно одна запись на каждый AC: без дублей, без лишних, без пропусков."""
    seen: set[str] = set()
    duplicates: list[str] = []
    for item in items:
        if item["ac_id"] in seen:
            duplicates.append(item["ac_id"])
        seen.add(item["ac_id"])
    if duplicates:
        raise EvidenceError(
            KIND_DUPLICATE_AC, ac_ids=[a for d in duplicates for a in _safe_ac(d)]
        )
    extra = seen - known
    if extra:
        raise EvidenceError(
            KIND_UNKNOWN_AC, ac_ids=[a for e in extra for a in _safe_ac(e)]
        )
    missing = known - seen
    if missing:
        raise EvidenceError(KIND_MISSING_AC, ac_ids=missing)


def validate_evidence(raw: Any, known_ac_ids: Iterable[str]) -> list[dict[str, str]]:
    """Нормализованный комплект доказательств или ``EvidenceError``.

    Порядок проверок — форма, значения, покрытие: самый дешёвый и самый общий
    отказ идёт первым, а «не хватает AC-2» — когда всё присланное годно.
    """
    items = _check_values(_check_shape(raw))
    _check_coverage(items, {str(a) for a in known_ac_ids})
    return items
