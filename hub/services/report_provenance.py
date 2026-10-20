"""Граф происхождения отчётов машинного ревью (#1912).

«Принять» в справке обязано судить не «ближайший отчёт», а всё, что стоит за
сдачей. Перенос отчёта на пересдачу (#1361) кладёт в новое поколение копию со
ссылкой ``carried_from_review_id``; ссылка схлопывается до корня и промежуточных
поколений не помнит. Прежние обходы лечили частные случаи (один источник, все
отчёты поколения источника, ближайшее поколение и корень) — и каждый круг
ревью #1810 находил следующий. Здесь определение одно.

Узлы графа — отчёты. Множество замкнуто:

* все отчёты текущего поколения задачи;
* для каждой копии (отчёта со ссылкой) — её источник и ВСЕ отчёты поколений от
  поколения источника до поколения копии включительно: копия могла быть снята
  с любого из них, а грязный отчёт между ними — сомнение в переносе;
* то же для каждого добавленного узла, транзитивно.

Что угодно неопределённое — не «чисто», а ``UnreadableReport``: источник не
найден, чужой, не из более раннего поколения (так же отсекаются самоссылка и
цикл), ссылка не число, раздел нечитаем, узлов больше ``MAX_NODES``. Каждый
узел потом проходит один и тот же предикат (``report_fit``), поэтому «Принять»
не зависит от того, КАК отчёт попал в граф.

Только чтение базы: сети здесь нет.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import aiosqlite

from hub import repository as repo
from hub.db import fetchall
from hub.models import MachineReviewView

#: Защита показа: справка строится на каждый рендер, граф не должен расти без меры.
MAX_NODES = 200

ROLE_CURRENT = "текущее поколение"
ROLE_SOURCE = "источник переноса"
ROLE_BETWEEN = "между источником и переносом"

#: Разделы отчёта, обязанные быть списками. Битый JSON или объект вместо
#: массива мягкий декодер модели превращает в [] — а пустой список читается
#: как «находок нет».
LIST_SECTIONS = (
    "findings_confirmed",
    "findings_rejected",
    "unresolved",
    "lost_dimensions",
)


class UnreadableReport(Exception):
    """Узел графа не разобрался или связь битая: «неизвестно», а не «чисто»."""

    def __init__(self, report_id: int, why: str, *, broken_link: bool = False) -> None:
        super().__init__(f"#{report_id}: {why}")
        self.report_id = report_id
        self.why = why
        self.broken_link = broken_link


@dataclass(frozen=True)
class Node:
    """Отчёт в графе: что он такое и откуда взялся."""

    view: MachineReviewView
    generation: int
    role: str

    @property
    def label(self) -> str:
        return f"#{self.view.id} (поколение {self.generation}, {self.role})"


def strict_row(row: dict[str, Any]) -> dict[str, Any]:
    """Строка отчёта, у которой каждый раздел — список; иначе UnreadableReport.

    Пустая строка и ``None`` — тоже нечитаемо (json.loads("") падает, None не
    список): «раздел не записан» не равно «находок нет».
    """
    out = dict(row)
    for name in LIST_SECTIONS:
        raw = out.get(name)
        try:
            value = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError as exc:
            raise UnreadableReport(int(row["id"]), f"раздел {name}: не JSON") from exc
        if not isinstance(value, list):
            raise UnreadableReport(int(row["id"]), f"раздел {name}: не список")
        out[name] = value
    return out


def _generation_of(row: dict[str, Any]) -> int:
    value = row.get("submission_generation")
    if isinstance(value, bool) or not isinstance(value, int):
        raise UnreadableReport(int(row["id"]), "поколение отчёта не число")
    return value


def link_of(row: dict[str, Any]) -> int | None:
    """Ссылка на корень переноса: ``None`` — самостоятельный отчёт; мусор — ошибка."""
    raw = row.get("carried_from_review_id")
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        raise UnreadableReport(
            int(row["id"]), f"ссылка переноса {raw!r} не id отчёта", broken_link=True
        )
    return raw


async def _source_of(
    db: aiosqlite.Connection, row: dict[str, Any], link: int
) -> dict[str, Any]:
    """Источник копии; всякая неувязка — битая связь."""
    report_id = int(row["id"])

    def broken(why: str) -> UnreadableReport:
        return UnreadableReport(report_id, why, broken_link=True)

    if link == report_id:
        raise broken("отчёт ссылается сам на себя")
    found = await repo.get_machine_review(db, link)
    if found is None:
        raise broken(f"исходный отчёт #{link} не найден")
    source = dict(found)
    if source.get("task_id") != row.get("task_id"):
        raise broken(f"исходный отчёт #{link} принадлежит другой задаче")
    if _generation_of(source) >= _generation_of(row):
        # Строго более раннее поколение: этим же отсекается цикл.
        raise broken(
            f"исходный отчёт #{link} не из более раннего поколения (цикл или порча)"
        )
    return source


async def _reports_between(
    db: aiosqlite.Connection, task_id: int, first: int, last: int
) -> list[aiosqlite.Row]:
    """Все отчёты поколений ``first``..``last`` включительно, по id."""
    return list(
        await fetchall(
            db,
            "SELECT * FROM machine_reviews WHERE task_id=? "
            "AND submission_generation BETWEEN ? AND ? ORDER BY id",
            (task_id, first, last),
        )
    )


class _Walk:
    """Один обход: накопленные узлы, очередь и уже прочитанные диапазоны поколений."""

    def __init__(self, db: aiosqlite.Connection, task_id: int) -> None:
        self.db = db
        self.task_id = task_id
        self.rows: dict[int, dict[str, Any]] = {}
        self.roles: dict[int, str] = {}
        self.todo: list[int] = []
        self.ranges: set[tuple[int, int]] = set()

    def add(self, row: dict[str, Any], role: str) -> None:
        report_id = int(row["id"])
        if report_id in self.rows:
            return
        if len(self.rows) >= MAX_NODES:
            raise UnreadableReport(
                report_id, f"в графе больше {MAX_NODES} отчётов: не доказано"
            )
        self.rows[report_id] = row
        self.roles[report_id] = role
        self.todo.append(report_id)

    async def expand(self, report_id: int) -> None:
        row = self.rows[report_id]
        link = link_of(row)
        if link is None:
            return
        source = await _source_of(self.db, row, link)
        self.add(source, ROLE_SOURCE)
        span = (_generation_of(source), _generation_of(row))
        if span in self.ranges:
            return
        self.ranges.add(span)
        for between in await _reports_between(self.db, self.task_id, *span):
            self.add(dict(between), ROLE_BETWEEN)


def _root_tokens(rows: dict[int, dict[str, Any]], row: dict[str, Any]) -> Any:
    """``provider_tokens`` копии берутся у корня: перенос не чтение и токенов не несёт."""
    current, hops = row, 0
    while (link := link_of(current)) is not None:
        current = rows[link]
        hops += 1
        if hops > len(rows):  # недостижимо при строгом порядке поколений
            raise UnreadableReport(int(row["id"]), "цикл переносов", broken_link=True)
    return current.get("provider_tokens")


def _view_of(rows: dict[int, dict[str, Any]], row: dict[str, Any]) -> MachineReviewView:
    report_id = int(row["id"])
    fixed = dict(row)
    if link_of(row) is not None:
        fixed["provider_tokens"] = _root_tokens(rows, row)
    try:
        return MachineReviewView(**strict_row(fixed))
    except UnreadableReport:
        raise
    except (TypeError, ValueError) as exc:
        raise UnreadableReport(report_id, "отчёт не разобрался") from exc


async def provenance_graph(
    db: aiosqlite.Connection, task: dict[str, Any]
) -> list[Node]:
    """Все отчёты, которые стоят за сдачей, в порядке id. Нечитаемое — UnreadableReport."""
    task_id = int(task["id"])
    generation = int(task.get("submission_generation") or 0)
    walk = _Walk(db, task_id)
    for row in await repo.machine_reviews_of_generation(db, task_id, generation):
        walk.add(dict(row), ROLE_CURRENT)
    while walk.todo:
        await walk.expand(walk.todo.pop())
    return [
        Node(
            _view_of(walk.rows, walk.rows[i]),
            _generation_of(walk.rows[i]),
            walk.roles[i],
        )
        for i in sorted(walk.rows)
    ]
