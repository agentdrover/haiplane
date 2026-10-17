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

_LIVE_FIELDS = (
    "status",
    "review_verdict",
    "review_verdict_generation",
    "submission_generation",
    "submission_sha",
)


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


#: Факты git, собранные ДО write-транзакции done-потока (prepare_ci_standing):
#: внутри транзакции guard только сравнивает, git не зовёт (#1783, круг 3).
UNCOMMITTED_KEY = "_uncommitted_work"
#: Отметка «пропущено ради хвоста»: после auto_commit sha проверяется заново.
EMPTY_UNLESS_COMMITTED = "_empty_unless_committed"


def tail_runs(task: dict[str, Any]) -> bool:
    """Git-хвост done (коммит и пуш в ``_route_after_done``) для задачи состоится."""
    from hub.services.orchestration import review_budget_exhausted

    return bool(
        task.get("auto_review")
        and task.get("branch")
        and not review_budget_exhausted(task.get("review_cycle", 0))
    )


def _tail_repo(task: dict[str, Any], workspace: str | None) -> str | None:
    """Каталог, в котором работает хвост: worktree pair-задачи, иначе проекта."""
    import os

    from hub.integrations.registry import plugins
    from hub.services.orchestration import worktree_per_task_enabled

    if worktree_per_task_enabled() and not task.get("job_id"):
        wt = plugins.git_ops.worktree_path(int(task["id"]), workspace)
        if wt and os.path.isdir(wt):
            return wt
    return workspace


async def collect_git_facts(
    db: aiosqlite.Connection, task: dict[str, Any]
) -> dict[str, bool]:
    """Есть ли в каталоге хвоста недокоммиченное или не запушенное; вызывать вне транзакции.

    Только там, где ответ может понадобиться: действующий «на доработку» и хвост
    впереди. Иначе пусто — git не зовётся вовсе.
    """
    if (
        (task.get("review_verdict") or "") != "changes_requested"
        or task.get("status") in ("review", "completed", "open")
        or not tail_runs(task)
    ):
        return {}
    from hub.integrations.registry import plugins
    from hub.services.orchestration import project_git_context

    workspace = (await project_git_context(db, int(task["id"]))).get("repo")
    git_repo = _tail_repo(task, workspace)
    if await plugins.git_ops.dirty_paths(repo=git_repo):
        return {UNCOMMITTED_KEY: True}
    branch = task.get("branch") or ""
    ahead = await plugins.git_ops.unpushed_commits(branch, git_repo) if branch else 0
    return {UNCOMMITTED_KEY: ahead > 0}


async def _refuse(
    db: aiosqlite.Connection, task: dict[str, Any], sha: str, *, undo_bump: bool
) -> str:
    """Пустая пересдача: pair — 422, headless — needs_decision с причиной."""
    from hub.services import watch_notices

    task_id = int(task["id"])
    generation = int(task.get("submission_generation") or 0)
    detail = empty_resubmission_detail(task_id=task_id, sha=sha, generation=generation)
    if not task.get("job_id"):
        raise HTTPException(422, detail=detail)  # done-поток откатывает и бамп
    if undo_bump:
        await db.execute(
            "UPDATE tasks SET submission_generation = submission_generation - 1 "
            "WHERE id = ?",
            (task_id,),
        )
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


async def recheck_after_commit(
    db: aiosqlite.Connection, task: dict[str, Any], committed: bool
) -> str:
    """После auto_commit: прежний sha — тот же исход, что у пустой пересдачи.

    Грязное дерево пропустило guard ради хвоста; если хвост ничего не закоммитил
    (повреждённые файлы отброшены) и своих коммитов впереди origin нет, правок
    нет. Бамп поколения уже сделан и откатывается.
    """
    if not task.get(EMPTY_UNLESS_COMMITTED) or committed:
        return ""
    from hub.integrations.registry import plugins
    from hub.services.orchestration import project_git_context

    workspace = (await project_git_context(db, int(task["id"]))).get("repo")
    git_repo = _tail_repo(task, workspace)
    branch = task.get("branch") or ""
    if await plugins.git_ops.unpushed_commits(branch, git_repo) > 0:
        return ""
    sha = (task.get("submission_sha") or "").strip()
    return await _refuse(db, task, sha, undo_bump=True)


async def stop_empty_done(
    db: aiosqlite.Connection, task: dict[str, Any], *, tail_pending: bool = False
) -> str:
    """done-отчёт на коммите поколения с действующим «на доработку».

    До бампа поколения и до любой записи. Pair: отказ (422), строка отчёта
    откатывается вместе с done-потоком. Headless: задача уходит в needs_decision
    с названной причиной, возвращается этот статус. Пустая строка — отчёт не
    пустой. ``tail_pending`` — вызов из done-потока, где git-хвост ещё впереди и
    закоммитит то, что лежит в рабочем каталоге; факты git берутся из
    ``UNCOMMITTED_KEY`` (собраны до транзакции), сам guard git не зовёт.
    """
    from hub.services import lifecycle

    orig = task
    # Вердикт и сдача — как их держит база сейчас: снимок вызывающего мог быть
    # прочитан до параллельного вердикта «на доработку» (APPROVAL_FIELDS в
    # transition_after_agent_done подставляются из него намеренно).
    live = await repo.get_task(db, int(task["id"]))
    if live is not None:
        task = {**task, **{k: dict(live)[k] for k in _LIVE_FIELDS}}
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
    if tail_pending and tail_runs(task) and task.get(UNCOMMITTED_KEY):
        # Правки исполнителя ещё не в удалённой ветке: их коммитит и пушит хвост
        # конвейера (``_route_after_done``) ПОСЛЕ этого места. Что хвост на деле
        # ничего не закоммитил, проверит ``recheck_after_commit``.
        orig[EMPTY_UNLESS_COMMITTED] = True
        return ""
    return await _refuse(db, task, sha, undo_bump=False)


async def commit_tail_work(
    db: aiosqlite.Connection,
    task: dict[str, Any],
    git_repo: str | None,
    branch: str,
    exit_code: int | None,
    result_text: str | None,
) -> str:
    """Коммит работы в git-хвосте done и перепроверка sha после него (#1783).

    Пусто — идти дальше. ``needs_decision`` — хвост остановлен: чужая ветка
    (``WorkspaceBranchMismatchError``) либо коммит ничего не добавил там, где
    guard пустой пересдачи пропустил отчёт ради этого коммита.
    """
    from hub.integrations.registry import plugins
    from hub.integrations.git_ops import WorkspaceBranchMismatchError
    from hub.services import watch_notices

    task_id = task["id"]
    try:
        committed = await plugins.git_ops.auto_commit(
            task_id,
            title=task.get("title", ""),
            repo=git_repo,
            expected_branch=branch,
        )
    except WorkspaceBranchMismatchError as exc:
        await repo.update_task(
            db,
            task_id,
            status="needs_decision",
            exit_code=exit_code,
            result_text=str(exc),
        )
        await repo.add_task_update(
            db,
            task_id,
            "hub",
            "blocker",
            f"{exc} — git-хвост done-конвейера остановлен, чтобы не "
            f"переписать историю чужой ветки. Решите через REST POST /api/tasks/<id>/decide.",
        )
        await watch_notices.emit_needs_decision(
            db,
            task_id=task_id,
            actor="hub",
            payload={"reason": "done_branch_mismatch", "branch": branch},
        )
        log.error("Task #%d → needs_decision: %s", task_id, exc)
        return "needs_decision"
    return await recheck_after_commit(db, task, committed)
