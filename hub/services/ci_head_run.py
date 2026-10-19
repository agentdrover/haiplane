"""Хаб сам просит CI на новой голове ветки задачи в работе и объявляет итог (#1862).

Облачный исполнитель не имеет ``gh`` и не может запустить CI на свой коммит.
Раньше он просил об этом записью в ленте, а запускал сессия роадмапа руками
(#1810 простояла 35 минут). Здесь это делает проход поллера ``ci_head_run``:

1. видит голову ветки задачи в ``running`` (без PR и без сдачи) одним чтением
   ``ls-remote`` на репозиторий;
2. ждёт grace - исполнитель мог запустить CI сам;
3. если прогона workflow из ``ci_contract`` проекта по sha нет, ОДИН раз просит
   его у форжа; всё, что известно, держится в журнале ``ci_head_runs`` с ключом
   (задача, sha), поэтому рестарт, два соединения и возврат головы A→B→A не
   рождают второй запрос;
4. итог прогона пишет в ленту один раз на (задача, sha, попытка) - только после
   того, как доверие к отчёту разрешено (``trust=verified``), не на приёме.

Область первого шага: форж GitHub и проекты с ``ci_before_submit`` не ``off`` и
с контрактом CI. Остальное пропускается молча. Попытку #1197 после сдачи
(``tasks.ci_run_requested_sha``) этот модуль не трогает.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from typing import Any

import aiosqlite

from hub import config
from hub import repository as repo
from hub import repository_ci_head as journal
from hub.db import write_transaction
from hub.integrations.protocols import CIRunRequestOutcome
from hub.integrations.registry import plugins
from hub.services import ci_contract, ci_trust, project_policy

log = logging.getLogger("hub")

#: После отказа форжа или git ничего не спрашиваем столько секунд (образец ci_trust).
BACKOFF_SECONDS = 60.0
#: Верхняя граница одного запроса прогона; дольше - исход неоднозначен.
REQUEST_TIMEOUT_SECONDS = 120.0

_down_until = 0.0
_cursor = 0


def reset_state() -> None:
    """Забыть всё, что держится в памяти процесса (рестарт; тесты)."""
    global _down_until, _cursor
    _down_until = 0.0
    _cursor = 0


def _back_off() -> None:
    global _down_until
    _down_until = time.monotonic() + BACKOFF_SECONDS


class _Scope:
    """Что нужно знать о задаче, чтобы просить для неё прогон."""

    def __init__(self, task_id: int, branch: str, project: Any) -> None:
        self.task_id = task_id
        self.branch = branch
        gh_repo = str(project["repo"] or "").strip()
        self.gh_repo = gh_repo or None
        self.workspace = str(project["workspace_path"] or "").strip() or None
        # Как project_git_context: форж едет только вместе с репозиторием.
        self.forge = project_policy.forge_of(project) if gh_repo else ""
        self.workflow_path = project_policy.ci_contract_of(project).workflow_path


def _in_scope(project: Any) -> bool:
    """GitHub, ci_before_submit не off, контракт CI у проекта есть."""
    policy = project_policy.gate_policy_of(project)
    if project_policy.ci_before_submit_of(policy) == project_policy.RED_TEST_OFF:
        return False
    if project_policy.forge_of(project) != "github":
        return False
    explicit = project_policy.CI_CONTRACT_KEY in policy
    if not (explicit or str(project["slug"] or "") == ci_contract.HUB_SLUG):
        return False
    return bool(project_policy.ci_contract_of(project).workflow_path)


async def _scopes(db: aiosqlite.Connection) -> list[_Scope]:
    """Задачи в области, не больше N за цикл, по кругу (курсор по id задачи)."""
    global _cursor
    found: list[_Scope] = []
    for row in await journal.running_candidates(db):
        project = await repo.resolve_project_for_task(db, int(row["id"]))
        if project is not None and _in_scope(project):
            found.append(_Scope(int(row["id"]), str(row["branch"]), project))
    found.sort(key=lambda s: s.task_id)
    after = [s for s in found if s.task_id > _cursor]
    ordered = after + [s for s in found if s.task_id <= _cursor]
    chosen = ordered[: max(1, int(config.CI_HEAD_RUN_TASKS_PER_CYCLE))]
    _cursor = chosen[-1].task_id if chosen and len(chosen) < len(found) else 0
    return chosen


async def _read_heads(scopes: list[_Scope]) -> dict[int, str] | None:
    """Головы веток: одно чтение на репозиторий; None - спросить не удалось."""
    by_workspace: dict[str | None, list[_Scope]] = {}
    for scope in scopes:
        by_workspace.setdefault(scope.workspace, []).append(scope)
    heads: dict[int, str] = {}
    for workspace, group in by_workspace.items():
        found = await plugins.git_ops.remote_heads(
            [s.branch for s in group], repo=workspace
        )
        if found is None:
            return None
        for scope in group:
            if found.get(scope.branch):
                heads[scope.task_id] = found[scope.branch]
    return heads


async def _runs_exist(scope: _Scope, sha: str) -> bool | None:
    """Есть ли прогон workflow контракта по sha; None - форж не ответил."""
    runs = await plugins.git_ops.ci_runs_for_sha(
        sha,
        repo=scope.workspace,
        gh_repo=scope.gh_repo,
        forge=scope.forge,
        workflow_path=scope.workflow_path,
    )
    return None if runs is None else bool(runs)


async def _ask_forge(scope: _Scope) -> tuple[bool, str]:
    """Попросить прогон; ``(принят, пояснение)``. Исключение и таймаут - неоднозначно."""
    try:
        async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
            result = await plugins.git_ops.request_ci_run(
                scope.branch,
                repo=scope.workspace,
                gh_repo=scope.gh_repo,
                forge=scope.forge,
                workflow_path=scope.workflow_path,
            )
    except Exception as exc:  # noqa: BLE001 - after a send the outcome is unknown
        log.warning("CI run request for #%s failed: %r", scope.task_id, exc)
        return False, "ambiguous"
    if result.outcome == CIRunRequestOutcome.requested:
        return True, result.details or scope.workflow_path
    if result.outcome == CIRunRequestOutcome.unavailable:
        return False, "ambiguous"
    return False, f"{result.outcome.value}/{result.reason}"


async def _note(db: aiosqlite.Connection, task_id: int, text: str) -> None:
    async with write_transaction(db):
        await repo.add_task_update(db, task_id, "hub", "status", text)


async def _head_after_request(scope: _Scope, sha: str) -> str:
    """Голова ветки ПОСЛЕ запроса: прогон шёл по ref, а не по sha."""
    again = await plugins.git_ops.remote_heads([scope.branch], repo=scope.workspace)
    return (again or {}).get(scope.branch) or sha


async def _request(db: aiosqlite.Connection, scope: _Scope, sha: str) -> None:
    accepted, why = await _ask_forge(scope)
    if accepted:
        await journal.settle(
            db, scope.task_id, sha, "requested", only_from=("reserved",)
        )
        now = await _head_after_request(scope, sha)
        moved = "" if now == sha else f" (голова сдвинулась с {sha[:12]})"
        await _note(
            db,
            scope.task_id,
            f"Хаб: запрос CI принят для ветки {scope.branch}, голова при запросе "
            f"{now[:12]}{moved}, workflow {why}. Итог появится здесь же, когда "
            "хаб подтвердит отчёт прогона.",
        )
    elif why != "ambiguous":
        await journal.settle(
            db, scope.task_id, sha, "declined", note=why, only_from=("reserved",)
        )
        await _note(
            db,
            scope.task_id,
            f"Хаб: запрос CI для ветки {scope.branch} (голова {sha[:12]}) не принят: "
            f"{why}. Запустите CI сами.",
        )


async def _handle_head(db: aiosqlite.Connection, scope: _Scope, sha: str) -> None:
    grace = int(config.CI_HEAD_RUN_GRACE_SECONDS)
    retry = int(config.CI_HEAD_RUN_RETRY_SECONDS)
    row = await journal.observe_head(
        db, scope.task_id, sha, scope.branch, grace_seconds=grace, retry_seconds=retry
    )
    if row["state"] in journal.SETTLED or not row["grace_over"]:
        return
    exists = await _runs_exist(scope, sha)
    if exists is None:
        _back_off()
        return
    if exists:
        await journal.settle(db, scope.task_id, sha, "observed")
        return
    if row["state"] == "reserved" and row["attempts"] >= journal.MAX_REQUESTS:
        if row["retry_over"] and await journal.settle(
            db, scope.task_id, sha, "exhausted", only_from=("reserved",)
        ):
            await _note(
                db,
                scope.task_id,
                f"Хаб: по голове {sha[:12]} прогон так и не появился после "
                f"{journal.MAX_REQUESTS} запросов. Запустите CI сами.",
            )
        return
    if await journal.reserve_request(
        db, scope.task_id, sha, grace_seconds=grace, retry_seconds=retry
    ):
        await _request(db, scope, sha)


async def request_for_new_heads(db: aiosqlite.Connection) -> None:
    """Первая половина прохода: новые головы без прогона получают запрос."""
    if time.monotonic() < _down_until:
        return
    scopes = await _scopes(db)
    if not scopes:
        return
    heads = await _read_heads(scopes)
    if heads is None:
        _back_off()
        return
    for scope in scopes:
        sha = heads.get(scope.task_id)
        if sha:
            try:
                await _handle_head(db, scope, sha)
            except Exception:  # noqa: BLE001 - one task must not skip the rest
                log.exception("CI head run for task #%s failed", scope.task_id)


# ---- объявление итога --------------------------------------------------------


def _announcement(row: Mapping[str, Any], stale: bool) -> str:
    conclusion = ci_trust.run_conclusion(row) or "итог не назван"
    text = (
        f"CI на sha {str(row['head_sha'])[:12]}: {conclusion} "
        f"(run {row['run_id']}, попытка {row['run_attempt']})"
    )
    return f"{text}; голова уже другая" if stale else text


async def _current_heads(
    db: aiosqlite.Connection, rows: list[dict[str, Any]]
) -> dict[int, str]:
    """Головы веток задач этих объявлений; пусто, если спросить не удалось."""
    scopes: dict[int, _Scope] = {}
    for row in rows:
        task_id = int(row["task_id"])
        if task_id in scopes:
            continue
        task = await repo.get_task(db, task_id)
        project = await repo.resolve_project_for_task(db, task_id)
        if task is not None and project is not None and (task["branch"] or ""):
            scopes[task_id] = _Scope(task_id, str(task["branch"]), project)
    try:
        return await _read_heads(list(scopes.values())) or {}
    except Exception:  # noqa: BLE001 - the label is a courtesy, not a gate
        return {}


async def announce_results(db: aiosqlite.Connection) -> None:
    """Вторая половина прохода: подтверждённые итоги - в ленту, по разу на попытку."""
    rows = await journal.unannounced_verified(db)
    if not rows:
        return
    heads = await _current_heads(db, rows)
    for row in rows:
        task_id = int(row["task_id"])
        fp = await ci_contract.fingerprint_for_task(db, task_id)
        if not ci_trust.is_verified(row, fp):
            continue
        key = f"{row['run_id']}:{row['run_attempt']}"
        head = heads.get(task_id)
        stale = bool(head) and head != row["head_sha"]
        async with write_transaction(db):
            if await journal.claim_announcement(db, task_id, row["head_sha"], key):
                await repo.add_task_update(
                    db, task_id, "hub", "status", _announcement(row, stale)
                )


async def sweep(db: aiosqlite.Connection) -> None:
    """Проход поллера: запросы на новые головы, затем объявление итогов."""
    await request_for_new_heads(db)
    await announce_results(db)
