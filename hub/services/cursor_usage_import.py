"""Импорт выгрузки usage Cursor: полная цена заказов ревью (#1413).

Живая проба 27.09: ``/v1/agents/{id}/usage`` заказа 420 видит один прогон и
1,95 млн токенов, а выгрузка Cursor по тому же Cloud Agent ID — 17 событий и
13,58 млн. Выгрузка считает то, чего API агента не показывает, и её приносит
только владелец. Поэтому:

* события выгрузки ложатся на заказ ревью с тем же ``agent_id`` в отдельные
  колонки ``billed_tokens`` / ``billed_events``; API-число
  (``provider_tokens``) остаётся рядом как нижняя граница;
* событие хранится один раз по ключу (Cloud Agent ID, Date, Total Tokens,
  Model): повтор или перекрывающаяся выгрузка сумму не удваивают;
* строки чужих агентов не пишутся, в ответе — их число и сумма;
* разбор строгий: нет колонки или нечисловой Total Tokens — отказ с
  названными колонками и строками, не записано ничего;
* без ``apply`` ничего не пишется — ответ называет план.
"""

from __future__ import annotations

import csv
import io
from typing import Any, NamedTuple

import aiosqlite

from hub.db import fetchall

#: Заголовок выгрузки Cursor (usage-events CSV), все колонки обязательны.
REQUIRED_COLUMNS: tuple[str, ...] = (
    "Date",
    "Cloud Agent ID",
    "Automation ID",
    "Kind",
    "Model",
    "Max Mode",
    "Input (w/ Cache Write)",
    "Input (w/o Cache Write)",
    "Cache Read",
    "Output Tokens",
    "Total Tokens",
    "Cost",
)
#: Потолок файла: выгрузка за день — около 1300 строк и 200 КБ.
MAX_CSV_CHARS = 5_000_000
#: Сколько битых строк называть в отказе.
_BAD_LINES_SHOWN = 20


class UsageImportRefused(ValueError):
    """Файл не принят; ``code`` и ``extra`` — структурная причина."""

    def __init__(self, code: str, detail: str, **extra: Any) -> None:
        super().__init__(detail)
        self.code, self.detail, self.extra = code, detail, extra

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "detail": self.detail, **self.extra}


class UsageEvent(NamedTuple):
    agent_id: str
    date: str
    total_tokens: int
    model: str


def parse_export(text: str) -> list[UsageEvent]:
    """Строки выгрузки или отказ; частичного разбора не бывает."""
    if len(text) > MAX_CSV_CHARS:
        raise UsageImportRefused(
            "too_large",
            f"файл больше {MAX_CSV_CHARS} символов",
            limit=MAX_CSV_CHARS,
        )
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    header = [name.strip() for name in reader.fieldnames or []]
    missing = [c for c in REQUIRED_COLUMNS if c not in header]
    if missing:
        raise UsageImportRefused(
            "missing_columns",
            "в выгрузке нет колонок: " + ", ".join(missing),
            columns=missing,
        )
    reader.fieldnames = header
    events: list[UsageEvent] = []
    bad: list[int] = []
    for line, row in enumerate(reader, start=2):
        raw = (row.get("Total Tokens") or "").strip().replace(",", "")
        if not raw.isdigit():
            bad.append(line)
            continue
        events.append(
            UsageEvent(
                (row.get("Cloud Agent ID") or "").strip(),
                (row.get("Date") or "").strip(),
                int(raw),
                (row.get("Model") or "").strip(),
            )
        )
    if bad:
        raise UsageImportRefused(
            "bad_tokens",
            f"нечисловой Total Tokens в {len(bad)} строк(ах)",
            column="Total Tokens",
            lines=bad[:_BAD_LINES_SHOWN],
        )
    return events


async def _orders_by_agent(
    db: aiosqlite.Connection, agents: set[str]
) -> dict[str, dict[str, Any]]:
    """Заказ ревью на агента; при нескольких строках — первая (усыновление)."""
    if not agents:
        return {}
    marks = ",".join("?" for _ in agents)
    sql = (
        "SELECT id, task_id, agent_id, profile, provider_tokens, billed_tokens, "  # nosec B608
        f"billed_events FROM review_dispatches WHERE agent_id IN ({marks}) "
        "ORDER BY id"
    )
    out: dict[str, dict[str, Any]] = {}
    for row in await fetchall(db, sql, tuple(sorted(agents))):
        out.setdefault(row["agent_id"], dict(row))
    return out


async def _known_keys(
    db: aiosqlite.Connection, agents: list[str]
) -> set[tuple[str, str, int, str]]:
    if not agents:
        return set()
    marks = ",".join("?" for _ in agents)
    sql = (
        "SELECT agent_id, event_date, total_tokens, model FROM cursor_usage_events "  # nosec B608
        f"WHERE agent_id IN ({marks})"
    )
    rows = await fetchall(db, sql, tuple(agents))
    return {(r[0], r[1], int(r[2]), r[3]) for r in rows}


async def import_cursor_usage(
    db: aiosqlite.Connection, text: str, *, apply: bool = False
) -> dict[str, Any]:
    """Разнести выгрузку по заказам ревью; без ``apply`` — только план."""
    events = parse_export(text)
    orders = await _orders_by_agent(db, {e.agent_id for e in events if e.agent_id})
    seen = await _known_keys(db, sorted(orders))
    fresh: list[UsageEvent] = []
    duplicates = 0
    foreign_agents: set[str] = set()
    foreign_tokens = foreign_events = 0
    for event in events:
        if event.agent_id not in orders:
            foreign_events += 1
            foreign_tokens += event.total_tokens
            foreign_agents.add(event.agent_id)
        elif tuple(event) in seen:
            duplicates += 1
        else:
            seen.add(tuple(event))  # type: ignore[arg-type]
            fresh.append(event)
    if apply and fresh:
        await db.executemany(
            "INSERT OR IGNORE INTO cursor_usage_events "
            "(agent_id, event_date, total_tokens, model, dispatch_id) "
            "VALUES (?, ?, ?, ?, ?)",
            [(*e, orders[e.agent_id]["id"]) for e in fresh],
        )
        await db.executemany(
            "UPDATE review_dispatches SET "
            "billed_tokens = (SELECT SUM(total_tokens) FROM cursor_usage_events "
            "WHERE dispatch_id = ?), "
            "billed_events = (SELECT COUNT(*) FROM cursor_usage_events "
            "WHERE dispatch_id = ?) WHERE id = ?",
            [(o["id"],) * 3 for o in orders.values()],
        )
        await db.commit()
    return {
        "apply": apply,
        "rows": len(events),
        "new_events": len(fresh),
        "duplicate_events": duplicates,
        "written": len(fresh) if apply else 0,
        "foreign": {
            "events": foreign_events,
            "tokens": foreign_tokens,
            "agents": len(foreign_agents),
        },
        "orders": [_order_line(o, fresh) for o in orders.values()],
    }


def _order_line(order: dict[str, Any], fresh: list[UsageEvent]) -> dict[str, Any]:
    mine = [e for e in fresh if e.agent_id == order["agent_id"]]
    return {
        "dispatch_id": order["id"],
        "task_id": order["task_id"],
        "agent_id": order["agent_id"],
        "profile": order["profile"] or "",
        "api_tokens": order["provider_tokens"],
        "new_events": len(mine),
        # Итог по заказу после импорта: уже лежавшее плюс новое.
        "billed_events": (order["billed_events"] or 0) + len(mine),
        "billed_tokens": (order["billed_tokens"] or 0)
        + sum(e.total_tokens for e in mine),
    }


def render_import(result: dict[str, Any]) -> str:
    """Отчёт импорта для человека (CLI)."""
    mode = "записано" if result.get("apply") else "сухой прогон, ничего не записано"
    foreign = result.get("foreign") or {}
    lines = [
        f"Выгрузка Cursor: {result.get('rows', 0)} строк — {mode}. "
        f"Новых событий {result.get('new_events', 0)}, "
        f"повторов {result.get('duplicate_events', 0)}.",
        f"Чужие агенты (не заказы ревью хаба): {foreign.get('events', 0)} "
        f"событий, {foreign.get('tokens', 0)} токенов, "
        f"{foreign.get('agents', 0)} агент(ов) — не записываются.",
    ]
    for o in result.get("orders") or []:
        lines.append(
            f"  заказ #{o['dispatch_id']} (task #{o['task_id']}, {o['profile'] or '-'}, "
            f"{o['agent_id']}): выгрузка {o['billed_tokens']} токенов / "
            f"{o['billed_events']} событий (+{o['new_events']}), "
            f"API {o['api_tokens']} — нижняя граница"
        )
    return "\n".join(lines)
