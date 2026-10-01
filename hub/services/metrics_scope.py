"""Срез практических метрик: окно, проект и модель-ревьюер (#1490, эпик #1462).

До этой задачи ``practice_metrics`` знал одно окно — ``since_days`` — и
склеивал проекты и модели в один итог. Срез — один объект, который читает
каждый раздел: окно, проект и модель собираются в условие SQL в ОДНОМ месте,
а не по-разному в двадцати запросах.

Правила:

* Окно — пара абсолютных отметок ``[start, end)``. У ``since_days`` правой
  границы нет: строка из будущего, как и раньше, входит в окно, поэтому ответ
  без новых параметров не меняется.
* Проект назначается по тому же правилу, что у ворот и ``/metrics`` (#747):
  ``project_id`` стоит на эпиках, потомки наследуют ближайший; задача вне
  проекта (и под неактивным проектом) — проекта ``default``.
* Модель — модель ревьюера, ``machine_reviews.model`` (спека метрик §8, C3).
  Пустая строка — группа «не заявлена»: она не пропадает, а выбирается по
  этому имени. Модель исполнителя (``tasks.submission_model``) — другой срез,
  его здесь нет.
* Разделы без модели-ревьюера (cycle time, CFR, shift-left, часы прод-дефекта,
  эскейпы, человеческие гейты, исходы вердиктов, исполнитель) от фильтра
  модели не меняются: :data:`MODEL_INDEPENDENT`.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

import aiosqlite

from hub import repository as repo
from hub.db import fetchall

#: Больше интервалов ряд не несёт: старые отбрасываются, и ответ это говорит.
MAX_SERIES_BUCKETS = 104

#: Группа отчётов, где модель не записана. Как в ``by_reviewer_model``.
UNDECLARED_MODEL = "не заявлена"

#: Ниже стольких наблюдений в КАЖДОМ из двух окон направление не рисуется
#: (спека метрик §5.1, решение владельца).
MIN_COMPARE_N = 10

_TS_FORMAT = "%Y-%m-%d %H:%M:%S"
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

#: Разделы ответа, на которые фильтр модели не влияет: у них нет модели
#: ревьюера. Печатается в ответе, чтобы страница не выдавала их за
#: отфильтрованные (спека §8).
MODEL_INDEPENDENT: tuple[str, ...] = (
    "cycle_times",
    "change_failure_rate",
    "shift_left",
    "prod_defect_clocks",
    "escaped_defects",
    "human_gates",
    "human_touches",
    "review_outcomes",
    "executor_runs",
    "validation_run_lines",
    "review_model_cascade",
    "steward_shadow",
    "review_economy.deep_cap",
    "review_economy.small_delta",
    "review_economy.escapes",
)

#: Разделы, у которых нет среза по проекту и окну: весь срок фазы.
UNFILTERED: tuple[str, ...] = ("steward_shadow",)


def _fmt(moment: datetime) -> str:
    return moment.strftime(_TS_FORMAT)


def _parse_date(raw: str, name: str) -> datetime:
    if not _DATE_RE.match(raw):
        raise ValueError(f"{name} must be YYYY-MM-DD, got {raw!r}")
    try:
        return datetime.combine(date.fromisoformat(raw), datetime.min.time())
    except ValueError as exc:
        raise ValueError(f"{name} is not a date: {raw!r}") from exc


@dataclass(frozen=True)
class Scope:
    """Окно, проект и модель одного расчёта."""

    start: str
    end: str | None = None
    project: str | None = None
    #: JSON-массив id задач проекта; None — проект не задан.
    project_task_ids: str | None = None
    #: Проект ``default`` берёт и задачи без id (событие без задачи).
    project_is_default: bool = False
    model: str | None = None
    #: Длина окна в днях, когда её назвали («последние N дней»): считать её
    #: заново от «сейчас» дало бы N+1 на микросекунды.
    window_days: int | None = None

    # --- построение ---------------------------------------------------

    @classmethod
    def relative(cls, days: int) -> Scope:
        """Окно «последние N дней» без проекта и модели."""
        start = datetime.now(UTC) - timedelta(days=int(days))
        return cls(start=_fmt(start), window_days=int(days))

    @classmethod
    def from_modifier(cls, modifier: str) -> Scope:
        """``'-90 days'`` — модификатор SQLite прежних вызовов."""
        match = re.fullmatch(r"-(\d+) days", modifier.strip())
        if match is None:
            raise ValueError(f"unsupported window modifier: {modifier!r}")
        return cls.relative(int(match.group(1)))

    # --- окно ---------------------------------------------------------

    @property
    def days(self) -> int:
        """Длина окна в днях, округлённая вверх (у открытого — до «сейчас»)."""
        if self.window_days is not None:
            return self.window_days
        return max(1, math.ceil(self._length().total_seconds() / 86400))

    def _bounds(self) -> tuple[datetime, datetime]:
        start = datetime.strptime(self.start, _TS_FORMAT)
        if self.end:
            return start, datetime.strptime(self.end, _TS_FORMAT)
        if self.window_days is not None:
            # "Last N days" is exactly N days long: measuring it to a later
            # "now" would add a sliver bucket to a series.
            return start, start + timedelta(days=self.window_days)
        return start, datetime.now(UTC).replace(tzinfo=None)

    def _length(self) -> timedelta:
        start, end = self._bounds()
        return end - start

    def previous(self) -> Scope:
        """Предыдущее окно той же длины; кончается там, где начинается это."""
        start, _ = self._bounds()
        length = self._length()
        return replace(
            self, start=_fmt(start - length), end=self.start, window_days=self.days
        )

    def slice(self, start: datetime, end: datetime) -> Scope:
        """То же окно, сдвинутое: проект и модель остаются."""
        return replace(self, start=_fmt(start), end=_fmt(end), window_days=None)

    def bucket_plan(
        self, bucket_days: int, limit: int = MAX_SERIES_BUCKETS
    ) -> tuple[list[Scope], int]:
        """Окно, нарезанное на интервалы по ``bucket_days`` от старого к новому.

        Последний интервал обрезан по правой границе окна. Интервалов не
        больше ``limit``: берутся самые свежие, а число отброшенных старых
        возвращается вторым значением — ряд не режется молча (#1490).
        """
        start, end = self._bounds()
        step = timedelta(days=max(1, int(bucket_days)))
        out: list[Scope] = []
        cursor = start
        while cursor < end:
            out.append(self.slice(cursor, min(cursor + step, end)))
            cursor += step
        dropped = max(len(out) - limit, 0)
        return out[dropped:], dropped

    # --- условия SQL --------------------------------------------------

    def window_sql(self, column: str) -> tuple[str, list[Any]]:
        if self.end is None:
            return f"{column} >= ?", [self.start]
        return f"({column} >= ? AND {column} < ?)", [self.start, self.end]

    def project_sql(self, column: str) -> tuple[str, list[Any]]:
        """Условие «задача ``column`` принадлежит проекту»; пусто без проекта."""
        if self.project_task_ids is None:
            return "", []
        clause = f"{column} IN (SELECT value FROM json_each(?))"  # nosec B608 - column is a literal from the caller
        if self.project_is_default:
            clause = f"({clause} OR {column} IS NULL)"
        return clause, [self.project_task_ids]

    def model_sql(self, column: str) -> tuple[str, list[Any]]:
        """Условие по модели ревьюера; пустая строка в БД — «не заявлена»."""
        if self.model is None:
            return "", []
        expr = f"CASE WHEN {column} = '' THEN ? ELSE {column} END = ?"
        return expr, [UNDECLARED_MODEL, self.model]

    def where(
        self,
        time_column: str | None,
        *,
        task_column: str | None = None,
        model_column: str | None = None,
    ) -> tuple[str, list[Any]]:
        """Условия окна, проекта и модели одной строкой (без ``AND`` впереди)."""
        parts: list[str] = []
        params: list[Any] = []
        if time_column is not None:
            sql, p = self.window_sql(time_column)
            parts.append(sql)
            params += p
        if task_column is not None:
            sql, p = self.project_sql(task_column)
            if sql:
                parts.append(sql)
                params += p
        if model_column is not None:
            sql, p = self.model_sql(model_column)
            if sql:
                parts.append(sql)
                params += p
        return " AND ".join(parts) or "1=1", params

    def stock_where(
        self, *, task_column: str, model_column: str
    ) -> tuple[str, list[Any]]:
        """Проект и модель без окна — для очередей, у которых окна нет."""
        return self.where(None, task_column=task_column, model_column=model_column)

    def project_admits(self, slug: str) -> bool:
        """Подходит ли проект ``slug`` (для разделов, что считают его в Python)."""
        return self.project is None or slug == self.project

    # --- описание -----------------------------------------------------

    def describe(self) -> dict[str, Any]:
        return {
            "project": self.project,
            "model": self.model,
            "from": self.start,
            "to": self.end,
            "days": self.days,
        }


async def project_task_ids(db: aiosqlite.Connection, slug: str) -> tuple[str, bool]:
    """Id задач проекта ``slug`` одним запросом и признак «это default».

    Правило то же, что ``repo.resolve_project_for_task`` (#335): проект лежит
    на эпике, потомки наследуют БЛИЖАЙШИЙ; без проекта и под неактивным
    проектом задача — default.
    """
    project = await repo.get_project_by_slug(db, slug)
    # A hub that never seeded the default project still has tasks outside any
    # project, and they are the default's (#335).
    if project is None and slug != "default":
        raise ValueError(f"unknown project: {slug!r}")
    default = await repo.get_project_by_slug(db, "default")
    is_default = slug == "default" or (
        project is not None
        and default is not None
        and int(project["id"]) == int(default["id"])
    )
    project_id = int(project["id"]) if project is not None else -1
    rows = await fetchall(
        db,
        "WITH RECURSIVE r(id, pid) AS ("  # nosec B608 - constant fragments
        " SELECT id, project_id FROM tasks WHERE parent_id IS NULL"
        " UNION ALL"
        " SELECT t.id, COALESCE(t.project_id, r.pid)"
        "  FROM tasks t JOIN r ON t.parent_id = r.id"
        ") SELECT id FROM r WHERE "
        + (
            "pid IS NULL OR pid = ? OR pid IN "
            "(SELECT id FROM projects WHERE status != 'active')"
            if is_default
            else "pid = ? AND pid IN (SELECT id FROM projects WHERE status = 'active')"
        ),
        (project_id,),
    )
    return json.dumps(sorted(int(r["id"]) for r in rows)), is_default


async def build_scope(
    db: aiosqlite.Connection,
    *,
    since_days: int,
    project: str | None = None,
    model: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> Scope:
    """Собрать срез. ``date_from``/``date_to`` (YYYY-MM-DD, ``date_to``
    включительно) заменяют ``since_days``; одна граница — от неё до «сейчас»
    или от начала времён."""
    if date_from is None and date_to is None:
        base = Scope.relative(since_days)
    else:
        start = (
            _parse_date(date_from, "date_from") if date_from else datetime(1970, 1, 1)
        )
        end = _parse_date(date_to, "date_to") + timedelta(days=1) if date_to else None
        if end is not None and end <= start:
            raise ValueError("date_to must not be before date_from")
        base = Scope(start=_fmt(start), end=_fmt(end) if end else None)
    model = model.strip() if model and model.strip() else None
    project = project.strip() if project and project.strip() else None
    if project is None:
        return replace(base, model=model)
    ids, is_default = await project_task_ids(db, project)
    return replace(
        base,
        project=project,
        project_task_ids=ids,
        project_is_default=is_default,
        model=model,
    )
