"""Согласованный снимок входов DoR и его отпечаток (#1610).

Одобрение считает DoR вне write-лока: DoR включает проверку путей постановки,
а она читает git базы проекта, и git под write-локом хаба запрещён (#1456).
Между расчётом и переходом draft -> open постановку может изменить конкурентный
refine, и решение иначе принималось бы по устаревшему DoR.

Поэтому DoR, оценка, рекомендации и отпечаток считаются из ОДНОГО снимка БД,
снятого одной read-транзакцией (она закрыта до ``BEGIN IMMEDIATE``). Под
write-локом снимок перечитывается (только БД) и сверяется по отпечатку.

Гарантия ограничена изменениями БД. Два входа расчёта - внешние наблюдения на
момент расчёта и под локом не перечитываются (``EXCLUDED_INPUTS``).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import aiosqlite

from hub import repository as repo
from hub.db import fetchall

#: Поля задачи, из которых считаются DoR, оценка и рекомендации одобрения.
#: Снимок содержит ТОЛЬКО их: чтение любого другого поля в расчёте - KeyError,
#: то есть новый вход DoR без отпечатка падает, а не даёт тихую гонку.
TASK_FIELDS: tuple[str, ...] = (
    "work_type",
    "user_story",
    "problem_statement",
    "business_value",
    "scope_in",
    "validation_commands",
    "size",
    "wip_tag",
    "affected_areas",
    "outcome_metric",
    "redesign_decision",
    "agent_fit",
    "risks",
    # #1647: профиль DoR задачи-состояния читает эти два поля.
    "result_kind",
    "rollback",
)

#: Поля критерия приёмки (строка acceptance_criteria).
AC_FIELDS: tuple[str, ...] = (
    "ac_id",
    "given",
    "when_clause",
    "then_clause",
    "verifiable_by",
    "test_ref",
    "expectation_source",
)

#: Разрешённый проект задачи (с наследованием, активностью и запасным default):
#: ``source_id`` - узел, давший проект (None - запасной default),
#: ``statement_paths`` - действующий режим проверки путей постановки.
PROJECT_FIELDS: tuple[str, ...] = (
    "id",
    "slug",
    "workspace_path",
    "default_branch",
    "statement_paths",
    "source_id",
)

#: Входы расчёта, которых нет в отпечатке, и причина. Это внешние наблюдения:
#: перечитывать их под write-локом нельзя (#1456), а расчёт вне лока - их
#: единственное чтение.
EXCLUDED_INPUTS: dict[str, str] = {
    "git_tree": (
        "дерево базовой ветки проекта читает git; git под write-локом хаба "
        "запрещён (#1456), поэтому это наблюдение на момент расчёта"
    ),
    "filesystem": (
        "существование путей рабочей копии проекта - наблюдение файловой "
        "системы на момент расчёта; под write-локом не перечитывается"
    ),
}


@dataclass(frozen=True)
class DorSnapshot:
    """Входы DoR одной задачи в один момент времени."""

    task_id: int
    task: dict[str, Any]
    acs: tuple[dict[str, Any], ...]
    project: dict[str, Any]

    def fingerprint(self) -> str:
        """Отпечаток входов; равные снимки дают равные отпечатки."""
        payload = {
            "task": {name: self.task[name] for name in TASK_FIELDS},
            "acs": [{name: ac[name] for name in AC_FIELDS} for ac in self.acs],
            "project": {name: self.project[name] for name in PROJECT_FIELDS},
        }
        text = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    @property
    def workspace_path(self) -> str:
        return str(self.project["workspace_path"] or "").strip()


async def _main_db_file(db: aiosqlite.Connection) -> str:
    """Файл основной базы соединения; пусто для базы в памяти."""
    rows = await fetchall(db, "PRAGMA database_list")
    for row in rows:
        if row[1] == "main":
            return str(row[2] or "")
    return ""


@contextlib.asynccontextmanager
async def _reader(db: aiosqlite.Connection) -> AsyncIterator[aiosqlite.Connection]:
    """Соединение, с которого читается снимок: все чтения из одного состояния БД.

    Самостоятельный снимок читается на ОТДЕЛЬНОМ коротком соединении к тому же
    файлу, в своей read-транзакции (WAL: читатель не блокирует писателя). Так
    снимок не трогает транзакционное состояние соединения вызывающего: другая
    корутина на нём может одновременно писать, и ни наш BEGIN, ни наш rollback
    её не заденут (#1610).

    Внутри чужой транзакции (или под write-локом) читаем на самом соединении:
    состояние там и так согласовано. База в памяти недоступна второму
    соединению; для неё читаем на соединении вызывающего без собственной
    транзакции - согласованность тогда держит сверка отпечатка под локом
    (расхождённый снимок не совпадёт и уйдёт в повтор).
    """
    path = "" if db.in_transaction else await _main_db_file(db)
    if not path:
        yield db
        return
    conn = await aiosqlite.connect(path)
    try:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA busy_timeout = 5000")
        await conn.execute("BEGIN")
        try:
            yield conn
        finally:
            await conn.rollback()
    finally:
        await conn.close()


async def load_dor_snapshot(db: aiosqlite.Connection, task_id: int) -> DorSnapshot:
    """Снять входы DoR из БД; без git и без файловой системы."""
    from hub.services.project_policy import gate_policy_of, statement_paths_of

    async with _reader(db) as conn:
        row = await repo.get_task(conn, task_id)
        if row is None:
            raise ValueError(f"task {task_id} not found")
        ac_rows = await repo.list_acceptance_criteria(conn, task_id)
        project_row, source_id = await repo.resolve_project_with_source(conn, task_id)

    task = {name: row[name] for name in TASK_FIELDS}
    acs = tuple({name: (dict(ac).get(name)) for name in AC_FIELDS} for ac in ac_rows)
    if project_row is None:
        project: dict[str, Any] = {name: None for name in PROJECT_FIELDS}
    else:
        project = {
            "id": project_row["id"],
            "slug": project_row["slug"],
            "workspace_path": project_row["workspace_path"],
            "default_branch": project_row["default_branch"],
            "statement_paths": statement_paths_of(gate_policy_of(project_row)),
            "source_id": source_id,
        }
    return DorSnapshot(task_id=task_id, task=task, acs=acs, project=project)
