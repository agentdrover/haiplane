"""Пустая пересдача после «на доработку» (#1783).

my-home #1732, 16.10: повторный прогон сдал поколение 2 на том же коммите, что
поколение 1, без правок и без исходов находок, и хаб засчитал его новым
поколением. Находки остаются открытыми; отказ называет это исполнителю.

Две двери, одно правило: ``submit_for_review`` (шаг конвейера) и done-отчёт
(``orchestration.transition_after_agent_done``: pair — отказ, headless —
needs_decision, потому что гейты headless не отказывают, #1122).
"""

from __future__ import annotations

import logging
from typing import Any

import aiosqlite
from fastapi import HTTPException

from hub import repository as repo
from hub.actionable_errors import empty_resubmission_detail

log = logging.getLogger(__name__)


def empty_resubmission_of(task: dict[str, Any], sha: str) -> bool:
    """Сдача ``sha`` повторяет коммит поколения с действующим changes_requested.

    Действующий — вердикт «на доработку» привязан к ТЕКУЩЕМУ поколению (а не
    заменён его ростом) и коммит поколения закреплён. Закрытие вердикта
    возвратом в работу (``review_verdict_closed_generation``) сюда не входит:
    повторный прогон, сдавший тот же коммит, — ровно случай #1732. Только из
    любого статуса, кроме review: из review тот же sha — прежний no-op (#1054,
    #1265), а running, claimed (done после повторного захвата) и fix_requested
    (headless) обходить запрет не могут (#1783, круг 2). Пустой sha ничего о коде
    не доказывает и совпадением не считается.
    """
    if task.get("status") in ("review", "completed", "open") or not sha:
        return False
    if (task.get("review_verdict") or "") != "changes_requested":
        return False
    generation = int(task.get("submission_generation") or 0)
    if generation < 1 or task.get("review_verdict_generation") != generation:
        return False
    return (task.get("submission_sha") or "").strip() == sha.strip()


async def _uncommitted_work(
    db: aiosqlite.Connection, task_id: int, branch: str
) -> bool:
    """В рабочем каталоге headless-задачи есть то, что хвост ещё закоммитит или запушит."""
    from hub.integrations.registry import plugins
    from hub.services.orchestration import project_git_context

    ctx = await project_git_context(db, task_id)
    workspace = ctx.get("repo")
    if not workspace:
        return False
    if await plugins.git_ops.dirty_paths(repo=workspace):
        return True
    return (
        bool(branch) and await plugins.git_ops.unpushed_commits(branch, workspace) > 0
    )


async def stop_empty_done(db: aiosqlite.Connection, task: dict[str, Any]) -> str:
    """done-отчёт на коммите поколения с действующим «на доработку».

    До бампа поколения и до любой записи. Pair: отказ (422), строка отчёта
    откатывается вместе с done-потоком. Headless: задача уходит в needs_decision
    с названной причиной, возвращается этот статус. Пустая строка — отчёт не
    пустой.
    """
    from hub.services import lifecycle, watch_notices

    if (
        task.get("status") in ("review", "completed", "open")
        or (task.get("review_verdict") or "") != "changes_requested"
    ):
        return ""
    # Подготовленный вне транзакции результат — в том числе «вершина не
    # прочитана» (пусто) — не повод идти в сеть под write-локом. Спрашиваем сами
    # только там, где подготовки не было (headless-путь поллера).
    sha = (task.get(lifecycle.PREPARED_CI_SHA) or "").strip()
    if not sha and lifecycle.PREPARED_CI_SHA not in task:
        sha, _ = await lifecycle.resolve_branch_tip(
            db, int(task["id"]), task.get("branch") or ""
        )
    if not empty_resubmission_of(task, sha):
        return ""
    task_id = int(task["id"])
    if task.get("job_id") and await _uncommitted_work(
        db, task_id, task.get("branch") or ""
    ):
        # Headless: правки исполнителя лежат в рабочем каталоге (или в локальных
        # коммитах), их коммитит и пушит хвост конвейера ПОСЛЕ этого места. Удалённая вершина до него —
        # прежняя, и это не пустая пересдача (#1783, круг 2).
        return ""
    generation = int(task.get("submission_generation") or 0)
    detail = empty_resubmission_detail(task_id=task_id, sha=sha, generation=generation)
    if not task.get("job_id"):
        raise HTTPException(422, detail=detail)
    await repo.update_task(db, task_id, status="needs_decision")
    await repo.add_task_update(
        db,
        task_id,
        "hub",
        "alert",
        f"{detail['message']} Решение за человеком (#1783).",
    )
    await watch_notices.emit_needs_decision(
        db,
        task_id=task_id,
        actor="hub",
        payload={"reason": "resubmit_without_changes", "detail": detail["message"]},
    )
    log.info("Task #%d → needs_decision (done report without changes)", task_id)
    return "needs_decision"
