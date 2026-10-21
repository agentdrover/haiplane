"""Срок прогона заказа хосту по размеру задачи (#1960).

Заказ на слот хоста получает ``run_seconds`` при выпуске (снимок), а сессия
implementer этого заказа живёт ровно столько — от первого выкупа. Остальные
коды (облако, intake, ручной pair) берут ``CHAT_PAIR_TTL_SECONDS``. Продления
посреди прогона нет (#983).

Таблица «размер -> секунды» задаётся env ``HAIPLANE_EXECUTOR_RUN_SECONDS``
(``M=10800,L=14400``). Разбор «всё или ничего»: любой плохой элемент отвергает
всю строку — таблица кода (7200 всем), предупреждение в журнал, ``error`` в
сводке; запуск заказа не отказывает. Потолок — константа кода, env его не
поднимает.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Final

import aiosqlite

from hub import config
from hub.db import fetchall

log = logging.getLogger(__name__)

RUN_SECONDS_FLOOR: Final = 3600
RUN_SECONDS_CEILING: Final = 14400
RUN_SECONDS_DEFAULT: Final = 7200
NO_SIZE: Final = "none"
SIZE_KEYS: Final = ("XS", "S", "M", "L", "XL", NO_SIZE)
ENV_SUFFIX: Final = "EXECUTOR_RUN_SECONDS"

_DIGITS = re.compile(r"[0-9]+")


@dataclass(frozen=True)
class RunTable:
    seconds: dict[str, int]
    source: str  # "env" | "default"
    error: str = ""


def _default_seconds() -> dict[str, int]:
    return dict.fromkeys(SIZE_KEYS, RUN_SECONDS_DEFAULT)


def _parse(raw: str) -> dict[str, int]:
    """Таблица из строки; ValueError с причиной на первом плохом элементе."""
    table = _default_seconds()
    seen: set[str] = set()
    for item in raw.split(","):
        name, sep, value = (part.strip() for part in item.partition("="))
        key = NO_SIZE if name.lower() == NO_SIZE else name.upper()
        if not sep or key not in SIZE_KEYS:
            raise ValueError(f"неизвестный элемент {item.strip()!r}")
        if key in seen:
            raise ValueError(f"размер {key} указан дважды")
        if not _DIGITS.fullmatch(value):
            raise ValueError(f"{key}: не целое число {value!r}")
        seconds = int(value)
        if not RUN_SECONDS_FLOOR <= seconds <= RUN_SECONDS_CEILING:
            raise ValueError(
                f"{key}={seconds} вне [{RUN_SECONDS_FLOOR}, {RUN_SECONDS_CEILING}]"
            )
        seen.add(key)
        table[key] = seconds
    return table


def run_table() -> RunTable:
    """Действующая таблица; env читается при каждом вызове."""
    raw = (config.env_get(ENV_SUFFIX, "") or "").strip()
    if not raw:
        return RunTable(_default_seconds(), "default")
    try:
        return RunTable(_parse(raw), "env")
    except ValueError as exc:
        log.warning(
            "%s отвергнута целиком, действует таблица кода (%d с): %s",
            ENV_SUFFIX,
            RUN_SECONDS_DEFAULT,
            exc,
        )
        return RunTable(_default_seconds(), "default", str(exc))


def run_seconds_for(task: Any, table: RunTable | None = None) -> int:
    """Срок прогона для задачи по её размеру; без размера — ключ ``none``."""
    size = str((dict(task) if task is not None else {}).get("size") or "").upper()
    seconds = (table or run_table()).seconds
    return seconds.get(size if size in SIZE_KEYS else NO_SIZE, RUN_SECONDS_DEFAULT)


def hours_text(seconds: int) -> str:
    """7200 -> «2 ч», 5400 -> «1,5 ч»."""
    hours = seconds / 3600
    return (f"{hours:g}" if hours == int(hours) else f"{hours:.1f}").replace(
        ".", ","
    ) + " ч"


def summary() -> dict[str, Any]:
    """Блок ``server.executor_run`` сводки политики."""
    table = run_table()
    block: dict[str, Any] = {
        "table": dict(table.seconds),
        "floor": RUN_SECONDS_FLOOR,
        "ceiling": RUN_SECONDS_CEILING,
        "source": table.source,
    }
    if table.error:
        block["error"] = table.error
    return block


def summary_line(block: dict[str, Any]) -> str:
    cells = ", ".join(f"{k} {hours_text(v)}" for k, v in block["table"].items())
    line = (
        f"Executor run deadline: {cells} (source {block['source']}, floor "
        f"{hours_text(block['floor'])}, ceiling {hours_text(block['ceiling'])})"
    )
    return line + (f"; env rejected: {block['error']}" if block.get("error") else "")


async def session_ttl(db: aiosqlite.Connection, row: dict[str, Any]) -> int:
    """TTL сессии при выкупе кода ``row``: срок заказа для кода слота, иначе общий.

    Заказ ищется по тому же предикату, что и переход заказа в ``redeemed``;
    значение вне границ (битая строка) в сессию не попадает.
    """
    if row.get("channel") != "slot":
        return config.CHAT_PAIR_TTL_SECONDS
    rows = await fetchall(
        db,
        "SELECT run_seconds FROM executor_orders WHERE pair_code_id = ? "
        "AND state IN ('pending', 'delivered')",
        (row["id"],),
    )
    if not rows:
        return config.CHAT_PAIR_TTL_SECONDS
    seconds = rows[0]["run_seconds"]
    if isinstance(seconds, int) and RUN_SECONDS_FLOOR <= seconds <= RUN_SECONDS_CEILING:
        return seconds
    # Битая строка заказа: прежний срок слота, а не общий TTL — тот не ограничен
    # потолком 14400 и может оказаться выше него.
    return RUN_SECONDS_DEFAULT
