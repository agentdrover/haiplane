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


@contextlib.asynccontextmanager
async def _one_read_transaction(db: aiosqlite.Connection) -> AsyncIterator[None]:
    """Все чтения снимка - из одного состояния БД.

    Своя read-транзакция открывается и закрывается здесь; внутри чужой (или
    под write-локом) чтения и так согласованы, и мы её не трогаем.
    """
    if db.in_transaction:
        yield
        return
    await db.execute("BEGIN")
    try:
        yield
    finally:
        await db.rollback()


async def load_dor_snapshot(db: aiosqlite.Connection, task_id: int) -> DorSnapshot:
    """Снять входы DoR из БД; без git и без файловой системы."""
    from hub.services.project_policy import gate_policy_of, statement_paths_of

    async with _one_read_transaction(db):
        row = await repo.get_task(db, task_id)
        if row is None:
            raise ValueError(f"task {task_id} not found")
        ac_rows = await repo.list_acceptance_criteria(db, task_id)
        project_row, source_id = await repo.resolve_project_with_source(db, task_id)

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
