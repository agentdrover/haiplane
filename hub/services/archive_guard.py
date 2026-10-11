"""Признаки работы у архивируемой задачи (#1673).

Архив уводит задачу из watchdog и инбокса (фильтр archived=0), но не
останавливает исполнителя. Поэтому у задачи с признаками работы архив требует
причину, а отказ называет саму работу. Правило одно, и применяет его
``lifecycle.archive_task``: и для commit, и для state-задач.
"""

from __future__ import annotations

from typing import Any

import aiosqlite

from hub.db import fetchall

#: Статусы, в которых за задачей числится живой исполнитель или живой гейт.
WORK_STATUSES = frozenset(
    {
        "claimed",
        "running",
        "review",
        "fix_requested",
        "ci_check",
        "needs_info",
        "pending_report",
        "needs_decision",
    }
)


async def _count(db: aiosqlite.Connection, sql: str, task_id: int) -> int:
    row = await (await db.execute(sql, (task_id,))).fetchone()
    return int(row[0]) if row else 0


async def blocking_work(
    db: aiosqlite.Connection, ids: list[int]
) -> list[dict[str, Any]]:
    """По каждой НЕархивной задаче из ``ids`` с признаками работы — её сведения.

    Признак: статус из ``WORK_STATUSES``, или job/review_job, который реестр
    ещё зовёт активным (терминальный и неизвестный не в счёт), или открытый
    слот executor_slots любого канала. Читает на соединении вызывающего —
    внутри его транзакции.
    """
    from hub.services.lifecycle import _active_jobs_on_task

    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    tasks = await fetchall(
        db,
        "SELECT id, status, claimed_by, claim_session_id, job_id, review_job_id, "  # nosec B608
        f"branch, pr_number FROM tasks WHERE archived=0 AND id IN ({marks}) "
        "ORDER BY id",
        tuple(ids),
    )
    slots = {
        int(r["task_id"]): r
        for r in await fetchall(
            db,
            "SELECT task_id, channel, slot FROM executor_slots "  # nosec B608
            f"WHERE released_at IS NULL AND task_id IN ({marks})",
            tuple(ids),
        )
    }
    out: list[dict[str, Any]] = []
    for task in tasks:
        task_id = int(task["id"])
        jobs = _active_jobs_on_task(dict(task))
        slot = slots.get(task_id)
        if task["status"] not in WORK_STATUSES and not jobs and slot is None:
            continue
        out.append(
            {
                "task_id": task_id,
                "status": task["status"],
                "claimed_by": task["claimed_by"],
                "claim_session_id": task["claim_session_id"],
                "active_job": "; ".join(f"{f} {j} ({s})" for f, j, s in jobs) or None,
                "open_slot": (
                    f"{slot['channel']}:{slot['slot'] or '-'}" if slot else None
                ),
                "updates_by_agents": await _count(
                    db,
                    "SELECT COUNT(*) FROM task_updates u JOIN principals p "
                    "ON p.id = u.principal_id WHERE u.author_kind='principal' "
                    "AND p.kind='agent' AND u.task_id=?",
                    task_id,
                ),
                "answers": await _count(
                    db,
                    "SELECT COUNT(*) FROM task_updates "
                    "WHERE kind='answer' AND task_id=?",
                    task_id,
                ),
                "branch": task["branch"],
                "pr_number": task["pr_number"],
            }
        )
    return out
