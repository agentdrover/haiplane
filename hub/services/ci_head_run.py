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
REQUEST_TIMEOUT_SECONDS = 60.0
#: Новые задачи цикла не начинаются позже этого срока: проход не держит поллер.
CYCLE_BUDGET_SECONDS = 45.0

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
    if not project_policy.ci_contract_of(project).workflow_path:
        return False
    # Головы читаются из клона проекта. Без клона git_ops берёт клон САМОГО хаба,
    # и это верно только для репозитория хаба: чужой репозиторий без клона
    # читался бы не оттуда, откуда уйдёт запрос.
    gh_repo = str(project["repo"] or "").strip()
    has_clone = bool(str(project["workspace_path"] or "").strip())
    return has_clone or not gh_repo or gh_repo == config.REPO_NAME


async def _scopes(db: aiosqlite.Connection) -> list[_Scope]:
    """Задачи в области, не больше N за цикл, по кругу (курсор по id задачи)."""
    found: list[_Scope] = []
    for row in await journal.running_candidates(db):
        project = await repo.resolve_project_for_task(db, int(row["id"]))
        if project is not None and _in_scope(project):
            found.append(_Scope(int(row["id"]), str(row["branch"]), project))
    found.sort(key=lambda s: s.task_id)
    after = [s for s in found if s.task_id > _cursor]
    ordered = after + [s for s in found if s.task_id <= _cursor]
    return ordered[: max(1, int(config.CI_HEAD_RUN_TASKS_PER_CYCLE))]


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


async def _settle_noting(
    db: aiosqlite.Connection,
    task_id: int,
    sha: str,
    state: str,
    text: str,
    *,
    note: str = "",
) -> None:
    """Состояние журнала и запись в ленту - ОДНА транзакция: без записи без состояния."""
    async with write_transaction(db):
        if await journal.settle(
            db, task_id, sha, state, note=note, only_from=("reserved",)
        ):
            await repo.add_task_update(db, task_id, "hub", "status", text)


async def _request(db: aiosqlite.Connection, scope: _Scope, sha: str) -> bool:
    """Попросить прогон по зарезервированной голове; True - форж принял."""
    accepted, why = await _ask_forge(scope)
    if accepted:
        await _settle_noting(
            db,
            scope.task_id,
            sha,
            "requested",
            f"Хаб: запрос CI принят для ветки {scope.branch}, голова при запросе "
            f"{sha[:12]}, workflow {why}. Итог появится здесь же, когда хаб "
            "подтвердит отчёт прогона.",
        )
    elif why == "ambiguous":
        _back_off()  # исход неизвестен: резерв держится, форж пока не трогаем
    else:
        await _settle_noting(
            db,
            scope.task_id,
            sha,
            "declined",
            f"Хаб: запрос CI для ветки {scope.branch} (голова {sha[:12]}) не "
            f"принят: {why}. Запустите CI сами.",
            note=why,
        )
    return accepted


async def _handle_head(db: aiosqlite.Connection, scope: _Scope, sha: str) -> bool:
    """Одна голова одной задачи; True - запрос принят форжем в этом проходе."""
    grace = int(config.CI_HEAD_RUN_GRACE_SECONDS)
    retry = int(config.CI_HEAD_RUN_RETRY_SECONDS)
    row = await journal.observe_head(
        db, scope.task_id, sha, scope.branch, grace_seconds=grace, retry_seconds=retry
    )
    if row["state"] in journal.SETTLED or not row["grace_over"]:
        return False
    exists = await _runs_exist(scope, sha)
    if exists is None:
        _back_off()
        return False
    if exists:
        await journal.settle(db, scope.task_id, sha, "observed")
        return False
    if row["state"] == "reserved" and row["attempts"] >= journal.MAX_REQUESTS:
        if row["retry_over"]:
            await _settle_noting(
                db,
                scope.task_id,
                sha,
                "exhausted",
                f"Хаб: по голове {sha[:12]} прогон так и не появился после "
                f"{journal.MAX_REQUESTS} запросов. Запустите CI сами.",
            )
        return False
    if not await journal.reserve_request(
        db, scope.task_id, sha, scope.branch, grace_seconds=grace, retry_seconds=retry
    ):
        return False
    return await _request(db, scope, sha)


async def _verify_after_requests(
    db: aiosqlite.Connection, accepted: list[tuple[_Scope, str]]
) -> None:
    """Голова не сдвинулась, пока шёл запрос? Одно чтение на репозиторий за проход.

    Запрос идёт по ref ветки, а не по sha: если голова ушла на B, прогон мог
    пойти по B. B записывается «запрошена, исход неизвестен», чтобы следующий
    цикл искал её прогон, а не заказывал второй.
    """
    if not accepted:
        return
    try:
        now = await _read_heads([scope for scope, _ in accepted])
    except Exception:  # noqa: BLE001 - the check is best effort
        return
    for scope, sha in accepted:
        head = (now or {}).get(scope.task_id)
        if head and head != sha:
            async with write_transaction(db):
                await journal.adopt_unplanned(db, scope.task_id, head, scope.branch)
                await repo.add_task_update(
                    db,
                    scope.task_id,
                    "hub",
                    "status",
                    f"Хаб: голова ветки {scope.branch} сдвинулась с {sha[:12]} на "
                    f"{head[:12]}, пока шёл запрос CI; прогон мог пойти по новой "
                    "голове. Хаб сначала ищет её прогон, а не заказывает второй.",
                )


async def request_for_new_heads(db: aiosqlite.Connection) -> dict[int, str]:
    """Первая половина прохода: новые головы без прогона получают запрос.

    Возвращает головы, прочитанные в этом проходе (для пометки объявлений).
    """
    global _cursor
    if time.monotonic() < _down_until:
        return {}
    scopes = await _scopes(db)
    if not scopes:
        return {}
    heads = await _read_heads(scopes)
    if heads is None:
        _back_off()
        return {}
    started = time.monotonic()
    accepted: list[tuple[_Scope, str]] = []
    for scope in scopes:
        if time.monotonic() - started > CYCLE_BUDGET_SECONDS:
            break  # остальные - следующим проходом, курсор остаётся на последней
        _cursor = scope.task_id
        sha = heads.get(scope.task_id)
        if not sha:
            continue
        try:
            if await _handle_head(db, scope, sha):
                accepted.append((scope, sha))
        except Exception:  # noqa: BLE001 - one task must not skip the rest
            log.exception("CI head run for task #%s failed", scope.task_id)
    await _verify_after_requests(db, accepted)
    return heads


# ---- объявление итога --------------------------------------------------------


def _announcement(row: Mapping[str, Any], stale: bool) -> str:
    conclusion = ci_trust.run_conclusion(row) or "итог не назван"
    text = (
        f"CI на sha {str(row['head_sha'])[:12]}: {conclusion} "
        f"(run {row['run_id']}, попытка {row['run_attempt']})"
    )
    return f"{text}; голова уже другая" if stale else text


async def _current_heads(
    db: aiosqlite.Connection, rows: list[dict[str, Any]], known: dict[int, str]
) -> dict[int, str]:
    """Головы веток задач этих объявлений: известные с прохода + одно чтение на остальных."""
    found = dict(known)
    scopes: dict[int, _Scope] = {}
    for row in rows:
        task_id = int(row["task_id"])
        if task_id in found or task_id in scopes:
            continue
        task = await repo.get_task(db, task_id)
        project = await repo.resolve_project_for_task(db, task_id)
        if task is not None and project is not None and (task["branch"] or ""):
            scopes[task_id] = _Scope(task_id, str(task["branch"]), project)
    readable = [s for s in scopes.values() if _in_scope_for_read(s)]
    if readable:
        try:
            found.update(await _read_heads(readable) or {})
        except Exception:  # noqa: BLE001 - the label is a courtesy, not a gate
            pass
    return found


def _in_scope_for_read(scope: _Scope) -> bool:
    """Головы читаются из клона проекта или из клона хаба (его репозиторий)."""
    return (
        bool(scope.workspace) or not scope.gh_repo or scope.gh_repo == config.REPO_NAME
    )


async def _announce_one(
    db: aiosqlite.Connection, row: dict[str, Any], heads: dict[int, str]
) -> None:
    """Объявить одну попытку, перечитав её В ТРАНЗАКЦИИ: снимок выборки мог устареть."""
    task_id = int(row["task_id"])
    key = f"{row['run_id']}:{row['run_attempt']}"
    async with write_transaction(db):
        fresh = await journal.get_attempt(db, int(row["id"]))
        fp = await ci_contract.fingerprint_for_task(db, task_id)
        if fresh is None or not ci_trust.is_verified(fresh, fp):
            return
        if await journal.claim_announcement(db, task_id, fresh["head_sha"], key):
            head = heads.get(task_id)
            stale = bool(head) and head != fresh["head_sha"]
            await repo.add_task_update(
                db, task_id, "hub", "status", _announcement(fresh, stale)
            )


async def announce_results(
    db: aiosqlite.Connection, known_heads: dict[int, str] | None = None
) -> None:
    """Вторая половина прохода: подтверждённые итоги - в ленту, по разу на попытку."""
    before: int | None = None
    for _page in range(journal.ANNOUNCE_PAGES):
        rows = await journal.unannounced_verified(
            db, limit=journal.ANNOUNCE_PAGE, before_id=before
        )
        if not rows:
            return
        heads = await _current_heads(db, rows, known_heads or {})
        for row in rows:
            await _announce_one(db, row, heads)
        if len(rows) < journal.ANNOUNCE_PAGE:
            return
        before = min(int(r["id"]) for r in rows)


async def sweep(db: aiosqlite.Connection) -> None:
    """Проход поллера: запросы на новые головы, затем объявление итогов."""
    heads = await request_for_new_heads(db)
    await announce_results(db, heads)
