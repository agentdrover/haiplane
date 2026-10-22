"""Подтверждается ли CI проекта: предупреждение для effective-policy и карточки (#2027).

Проект мог слать отчёты CI месяцами и ни разу не получить verified
(my-home, olymp-helper: прямая отправка ключом записи, no_provenance). Узнать
об этом можно было только по двухчасовым ожиданиям стюарда. Читатель смотрит на
последние сдачи проекта и называет исход одним из четырёх состояний:

* ``no_submissions`` - сдач ещё нет, спрашивать не о чем;
* ``no_reports`` - сдачи есть, отчётов CI по их закреплённым коммитам нет:
  это не неисправность (отсутствие данных не данные), своя фраза;
* ``verified`` - хотя бы один отчёт подтверждён на закреплённом коммите;
* ``unverified`` - отчёты есть, ни один не подтверждён: предупреждение с
  причинами и ссылкой на инструкцию.

Модуль только читает (``ci_run_reports``, ``submissions``) и в сеть не ходит.
Оценка идёт по контракту CI проекта СЕЙЧАС, как у остальных читателей доверия.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from typing import Any

import aiosqlite

from hub.db import fetchall
from hub.services import ci_trust, project_policy

log = logging.getLogger(__name__)

#: Сколько последних сдач проекта читается.
RECENT_SUBMISSIONS = 5
GUIDE = "docs/satellite-ci-report.md"

NO_SUBMISSIONS = "no_submissions"
NO_REPORTS = "no_reports"
VERIFIED = "verified"
UNVERIFIED = "unverified"


def _block(state: str, message: str, **extra: Any) -> dict[str, Any]:
    return {
        "state": state,
        "warning": state == UNVERIFIED,
        "message": message,
        "reasons": extra.get("reasons", {}),
        "checked": extra.get("checked", 0),
    }


# Task -> owning project, the rule of ``metrics_scope.project_task_ids`` (#335):
# the project sits on the epic, descendants inherit the nearest one, and a task
# under no or an inactive project belongs to ``default``. One statement for ALL
# projects, so the list page costs the same for one project and for fifty.
_RECENT_SQL = """
WITH RECURSIVE r(id, pid) AS (
    SELECT id, project_id FROM tasks WHERE parent_id IS NULL
    UNION ALL
    SELECT t.id, COALESCE(t.project_id, r.pid) FROM tasks t JOIN r ON t.parent_id = r.id
), owned AS (
    SELECT s.id AS sid, s.task_id, s.sha,
           CASE WHEN r.pid IS NULL
                  OR r.pid IN (SELECT id FROM projects WHERE status != 'active')
                THEN COALESCE((SELECT id FROM projects WHERE slug = 'default'), -1)
                ELSE r.pid END AS owner
    FROM submissions s JOIN r ON r.id = s.task_id WHERE s.sha != ''
), ranked AS (
    SELECT owner, task_id, sha,
           ROW_NUMBER() OVER (PARTITION BY owner ORDER BY sid DESC) AS n
    FROM owned
)
SELECT owner, task_id, sha FROM ranked WHERE n <= ? ORDER BY owner, n
"""


async def _batch(
    db: aiosqlite.Connection, projects: list[Any]
) -> tuple[dict[int, list[tuple[int, str]]], dict[tuple[int, str], dict[str, Any]]]:
    """Last submissions per project and the reports of their pinned commits: 2 statements."""
    wanted = {int(p["id"]) for p in projects}
    recent: dict[int, list[tuple[int, str]]] = {}
    for r in await fetchall(db, _RECENT_SQL, (RECENT_SUBMISSIONS,)):
        if int(r["owner"]) in wanted:
            recent.setdefault(int(r["owner"]), []).append(
                (int(r["task_id"]), str(r["sha"]))
            )
    pairs = sorted({pair for subs in recent.values() for pair in subs})
    reports: dict[tuple[int, str], dict[str, Any]] = {}
    if pairs:
        rows = await fetchall(
            db,
            "SELECT r.* FROM ci_run_reports r JOIN json_each(?) j "
            "ON r.task_id = json_extract(j.value, '$[0]') "
            "AND r.head_sha = json_extract(j.value, '$[1]')",
            (json.dumps([list(p) for p in pairs]),),
        )
        reports = {(int(r["task_id"]), str(r["head_sha"])): dict(r) for r in rows}
    return recent, reports


def _block_of(
    project: Any,
    submissions: list[tuple[int, str]],
    stored: dict[tuple[int, str], dict[str, Any]],
) -> dict[str, Any]:
    fingerprint = project_policy.ci_contract_of(project).fingerprint
    if not submissions:
        return _block(
            NO_SUBMISSIONS, "Сдач ещё нет: подтверждаемость CI не проверялась."
        )
    reports = [stored[pair] for pair in submissions if pair in stored]
    checked = len(submissions)
    if not reports:
        return _block(
            NO_REPORTS,
            f"У последних сдач ({checked}) отчётов CI нет: проверять нечего, "
            f"это не неисправность. Как подключить: {GUIDE}.",
            checked=checked,
        )
    reasons: Counter[str] = Counter()
    for report in reports:
        trust, reason = ci_trust.trust_of(report, fingerprint)
        if trust == ci_trust.VERIFIED:
            return _block(VERIFIED, "", checked=checked)
        reasons[reason or ci_trust.LEGACY] += 1
    named = ", ".join(f"{reason} x{n}" for reason, n in sorted(reasons.items()))
    return _block(
        UNVERIFIED,
        f"CI-отчёты есть ({len(reports)} из последних {checked} сдач), но ни один "
        f"не подтверждён на закреплённом коммите: {named}. Стюард ждёт "
        f"подтверждения впустую. Что настроить: {GUIDE}.",
        reasons=dict(reasons),
        checked=checked,
    )


async def ci_trust_block(db: aiosqlite.Connection, project: Any) -> dict[str, Any]:
    """Состояние подтверждаемости CI у проекта ``project`` (строка или отображение)."""
    recent, stored = await _batch(db, [project])
    return _block_of(project, recent.get(int(project["id"]), []), stored)


async def blocks_by_project(
    db: aiosqlite.Connection, projects: Any
) -> dict[int, dict[str, Any] | None]:
    """``{id проекта: блок}`` для списка: запросов столько же, сколько для одного проекта.

    Отказ чтения - None у всех (страница не падает); отказ расчёта одного - у него.
    """
    rows = list(projects)
    try:
        recent, stored = await _batch(db, rows)
    except Exception:
        log.exception("CI trust blocks could not be read")
        return {int(p["id"]): None for p in rows}
    out: dict[int, dict[str, Any] | None] = {}
    for project in rows:
        try:
            out[int(project["id"])] = _block_of(
                project, recent.get(int(project["id"]), []), stored
            )
        except Exception:
            log.exception("CI trust block failed for project %s", project["id"])
            out[int(project["id"])] = None
    return out
