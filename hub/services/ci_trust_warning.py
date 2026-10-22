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

import logging
from collections import Counter
from typing import Any

import aiosqlite

from hub import repository as repo
from hub.db import fetchall
from hub.services import ci_trust, metrics_scope, project_policy

#: Сколько последних сдач проекта читается.
log = logging.getLogger(__name__)

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


async def _recent_submissions(
    db: aiosqlite.Connection, slug: str
) -> list[tuple[int, str]]:
    ids, _ = await metrics_scope.project_task_ids(db, slug)
    rows = await fetchall(
        db,
        "SELECT task_id, sha FROM submissions WHERE sha != '' AND task_id IN "
        "(SELECT value FROM json_each(?)) ORDER BY id DESC LIMIT ?",
        (ids, RECENT_SUBMISSIONS),
    )
    return [(int(r["task_id"]), str(r["sha"])) for r in rows]


async def ci_trust_block(db: aiosqlite.Connection, project: Any) -> dict[str, Any]:
    """Состояние подтверждаемости CI у проекта ``project`` (строка или отображение)."""
    fingerprint = project_policy.ci_contract_of(project).fingerprint
    submissions = await _recent_submissions(db, str(project["slug"]))
    if not submissions:
        return _block(
            NO_SUBMISSIONS, "Сдач ещё нет: подтверждаемость CI не проверялась."
        )
    reports: list[dict[str, Any]] = []
    for task_id, sha in submissions:
        stored = await repo.get_ci_run_report(db, task_id, sha)
        if stored is not None:
            reports.append(dict(stored))
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


async def blocks_by_project(
    db: aiosqlite.Connection, projects: Any
) -> dict[int, dict[str, Any] | None]:
    """``{id проекта: блок}`` для страницы проектов; отказ расчёта одного - None."""
    out: dict[int, dict[str, Any] | None] = {}
    for project in projects:
        try:
            out[int(project["id"])] = await ci_trust_block(db, project)
        except Exception:
            log.exception("CI trust block failed for project %s", project["id"])
            out[int(project["id"])] = None
    return out
