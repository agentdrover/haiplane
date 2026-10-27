"""Ожидаемое поколение постановки при одобрении с листа карточки (#1813).

Лист одобрения называет поколение постановки, которое человек видел на экране.
Сверка с живой задачей должна стоять РЯДОМ с записью, а не до неё: между
чтением и переходом draft -> open постановку можно переписать. Расчёт DoR идёт
вне write-лока (там git, #1456), поэтому держать вокруг него транзакцию нельзя;
вместо этого ожидание кладётся в ContextVar запроса и проверяется там, где
``approve_task`` уже читает строку под локом (``_open_draft_under_lock``).

ContextVar локален задаче asyncio: чужие запросы его не видят. Без ожидания
(REST, CLI, MCP, формы «Подробностей») поведение не меняется.
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from fastapi import HTTPException

_EXPECTED: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "expected_statement_generation", default=None
)


@contextmanager
def expecting_statement_generation(value: int | None) -> Iterator[None]:
    token = _EXPECTED.set(value)
    try:
        yield
    finally:
        _EXPECTED.reset(token)


def refuse_if_statement_moved(fresh: Any) -> None:
    """409 до любой записи, если постановка уже не того поколения, что на экране."""
    expected = _EXPECTED.get()
    if expected is None:
        return
    live = int(fresh["statement_generation"] or 0)
    if live != expected:
        raise HTTPException(
            409,
            f"Постановка изменилась: на экране поколение {expected}, сейчас "
            f"{live}. Ничего не записано; обновите страницу и перечитайте "
            "постановку.",
        )
