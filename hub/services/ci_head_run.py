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

Проход живёт под ОДНИМ дедлайном ``CYCLE_BUDGET_SECONDS``, отсчитанным от его
начала: каждая сетевая операция получает оставшееся время (не больше своего
потолка), не хватает времени - остальное следующим тиком. Каждый клон и каждая
задача изолированы: исключение - отказ этого клона с отступом, не прохода.

Область первого шага: форж GitHub и проекты с ``ci_before_submit`` не ``off`` и
с контрактом CI. Остальное пропускается молча. Попытку #1197 после сдачи
(``tasks.ci_run_requested_sha``) этот модуль не трогает.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, TypeVar

import aiosqlite

from hub import config
from hub import repository as repo
from hub import repository_ci_head as journal
from hub.db import write_transaction
from hub.integrations import git_ops as git_ops_mod
from hub.integrations.protocols import CIRunRequestOutcome
from hub.integrations.registry import plugins
from hub.services import ci_contract, ci_trust, project_policy, watch_notices

log = logging.getLogger("hub")

T = TypeVar("T")

#: После отказа форжа или клона ничего не спрашиваем столько секунд (образец ci_trust).
BACKOFF_SECONDS = 60.0
#: Дедлайн на ВЕСЬ проход (запросы и объявления); поллер ждёт его целиком.
CYCLE_BUDGET_SECONDS = 25.0
#: Потолки отдельных сетевых операций; операция получает min(потолок, остаток).
HEADS_CEILING_SECONDS = 20.0
PROBE_CEILING_SECONDS = 15.0
DISPATCH_CEILING_SECONDS = 30.0
#: Чтение голов только ради пометки «голова уже другая» - любезность, не условие:
#: на неё уходит не больше этого, а объявление не ждёт.
LABEL_BUDGET_SECONDS = 3.0
#: Доля бюджета прохода, которую может съесть половина запросов: остальное -
#: объявлениям, чтобы медленные клоны их не морили.
REQUEST_SHARE = 0.7
#: Меньше этого остатка платный запрос не начинаем: резерв не должен пропасть зря.
MIN_DISPATCH_LEFT_SECONDS = 2.0

_down_until = 0.0
_cursor = 0
#: Клон, из которого головы не прочитались, на минуту выпадает из очереди:
#: недоступный репозиторий не должен держать остальные проекты.
_heads_down: dict[str | None, float] = {}
#: Состояния, из которых запрос больше не просят.
_RETRYABLE = ("reserved", "requested")


class _OutOfTime(Exception):
    """Бюджет прохода кончился: остальное - следующим тиком, без отступов."""


class _Deadline:
    """Один дедлайн на проход; сетевые вызовы идут через ``run``."""

    def __init__(self, seconds: float) -> None:
        self.end = time.monotonic() + seconds

    def left(self) -> float:
        return self.end - time.monotonic()

    async def run(self, make: Callable[[], Awaitable[T]], ceiling: float) -> T:
        """Выполнить вызов за min(потолок, остаток); остатка нет - ``_OutOfTime``."""
        allowed = min(ceiling, self.left())
        if allowed <= 0:
            raise _OutOfTime
        async with asyncio.timeout(allowed):
            return await make()


def reset_state() -> None:
    """Забыть всё, что держится в памяти процесса (рестарт; тесты)."""
    global _down_until, _cursor, _last_prune
    _down_until = 0.0
    _last_prune = 0.0
    _heads_down.clear()
    _cursor = 0


def _workspace_down(workspace: str | None) -> bool:
    return time.monotonic() < _heads_down.get(workspace, 0.0)


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
        # PR задачи открывается на базу проекта.
        self.base = git_ops_mod._resolve_base(str(project["default_branch"] or ""))

    def key(self) -> tuple[Any, ...]:
        """Всё, от чего зависит, КУДА и ЧЕМ уйдёт запрос."""
        return (self.gh_repo, self.workspace, self.forge, self.workflow_path, self.base)


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


async def _fresh_scope(db: aiosqlite.Connection, scope: _Scope) -> _Scope | None:
    """Область задачи СЕЙЧАС (политика и контракт перечитаны); None - вышла из области."""
    project = await repo.resolve_project_for_task(db, scope.task_id)
    if project is None or not _in_scope(project):
        return None
    fresh = _Scope(scope.task_id, scope.branch, project)
    return fresh if fresh.key() == scope.key() else None


async def _scopes(db: aiosqlite.Connection) -> list[_Scope]:
    """Задачи в области, не больше N за цикл, по кругу (курсор по id задачи)."""
    found: list[_Scope] = []
    for row in await journal.running_candidates(db):
        try:
            project = await repo.resolve_project_for_task(db, int(row["id"]))
            if project is not None and _in_scope(project):
                scope = _Scope(int(row["id"]), str(row["branch"]), project)
                if not _workspace_down(scope.workspace):
                    found.append(scope)
        except Exception:  # noqa: BLE001 - one unreadable task is skipped, not the pass
            log.exception("CI head run: task #%s scope unreadable", row["id"])
    found.sort(key=lambda s: s.task_id)
    after = [s for s in found if s.task_id > _cursor]
    ordered = after + [s for s in found if s.task_id <= _cursor]
    return ordered[: max(1, int(config.CI_HEAD_RUN_TASKS_PER_CYCLE))]


async def _read_heads(scopes: list[_Scope], dl: _Deadline) -> dict[int, str]:
    """Головы веток: одно чтение на клон, каждое - за остаток дедлайна.

    Любое исключение клона (нет каталога, git не ответил, срок) - отказ ЭТОГО
    клона: он уходит в отступ, остальные читаются дальше. Задача без головы в
    ответе значит «головы не видно» и ничего не решает. Кончился бюджет прохода -
    ``_OutOfTime`` наружу: ни отступов, ни догадок.
    """
    by_workspace: dict[str | None, list[_Scope]] = {}
    for scope in scopes:
        by_workspace.setdefault(scope.workspace, []).append(scope)
    heads: dict[int, str] = {}
    for workspace, group in by_workspace.items():
        branches = [s.branch for s in group]

        async def read(
            ws: str | None = workspace, br: list[str] = branches
        ) -> dict[str, str] | None:
            return await plugins.git_ops.remote_heads(br, repo=ws)

        try:
            found = await dl.run(read, HEADS_CEILING_SECONDS)
        except _OutOfTime:
            raise
        except Exception as exc:  # noqa: BLE001 - this clone failed, not the pass
            log.warning("CI head run: heads of %r unreadable: %r", workspace, exc)
            found = None
        if found is None:
            _heads_down[workspace] = time.monotonic() + BACKOFF_SECONDS
            continue
        for scope in group:
            if found.get(scope.branch):
                heads[scope.task_id] = found[scope.branch]
    return heads


async def _runs_exist(scope: _Scope, sha: str, dl: _Deadline) -> bool | None:
    """Есть ли прогон workflow контракта по sha; None - форж не ответил."""
    try:
        runs = await dl.run(
            lambda: plugins.git_ops.ci_runs_for_sha(
                sha,
                repo=scope.workspace,
                gh_repo=scope.gh_repo,
                forge=scope.forge,
                workflow_path=scope.workflow_path,
            ),
            PROBE_CEILING_SECONDS,
        )
    except _OutOfTime:
        raise
    except Exception as exc:  # noqa: BLE001 - "could not ask" is not "no runs"
        log.warning("CI head run: runs of %s unreadable: %r", sha[:12], exc)
        return None
    return None if runs is None else bool(runs)


async def _open_pr(scope: _Scope, dl: _Deadline) -> bool | None:
    """Открытый PR ветки (его pull_request сам запускает CI); None - не прочитан.

    Список PR, который не удалось прочитать, - не «PR нет»: форж называет сбой
    причиной, и она читается как неизвестность.
    """
    try:
        number, why = await dl.run(
            lambda: plugins.git_ops.open_pr_between(
                scope.base,
                scope.branch,
                repo=scope.workspace,
                gh_repo=scope.gh_repo,
                forge=scope.forge,
            ),
            PROBE_CEILING_SECONDS,
        )
    except _OutOfTime:
        raise
    except Exception:  # noqa: BLE001 - unknown PR state: do not dispatch blind
        log.warning("CI head run: PR of %s unreadable", scope.branch)
        return None
    if why:
        return None
    return bool(number)


async def _ask_forge(scope: _Scope, dl: _Deadline) -> tuple[bool, str]:
    """Попросить прогон; ``(принят, пояснение)``. Исключение и таймаут - неоднозначно."""
    try:
        result = await dl.run(
            lambda: plugins.git_ops.request_ci_run(
                scope.branch,
                repo=scope.workspace,
                gh_repo=scope.gh_repo,
                forge=scope.forge,
                workflow_path=scope.workflow_path,
            ),
            DISPATCH_CEILING_SECONDS,
        )
    except _OutOfTime:
        raise  # бюджет кончился ДО вызова: ничего не отправлено, попытка вернётся
    except Exception as exc:  # noqa: BLE001 - after a send the outcome is unknown
        log.warning("CI run request for #%s failed: %r", scope.task_id, exc)
        return False, "ambiguous"
    if result.outcome == CIRunRequestOutcome.requested:
        return True, result.details or scope.workflow_path
    if result.outcome == CIRunRequestOutcome.unavailable:
        return False, "ambiguous"
    return False, f"{result.outcome.value}/{result.reason}"


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
            db, task_id, sha, state, note=note, only_from=journal.OPEN_STATES
        ):
            await repo.add_task_update(db, task_id, "hub", "status", text)


async def _reserve(db: aiosqlite.Connection, scope: _Scope, sha: str) -> bool:
    """Занять попытку; политика и контракт проекта перечитываются В ЭТОЙ ЖЕ транзакции."""
    async with write_transaction(db):
        if await _fresh_scope(db, scope) is None:
            return False
        return await journal.reserve_request(
            db,
            scope.task_id,
            sha,
            scope.branch,
            grace_seconds=int(config.CI_HEAD_RUN_GRACE_SECONDS),
            retry_seconds=int(config.CI_HEAD_RUN_RETRY_SECONDS),
        )


async def _external_ci_present(
    db: aiosqlite.Connection, scope: _Scope, sha: str, dl: _Deadline
) -> str:
    """Непосредственно перед dispatch: ``present`` | ``unknown`` | ``clear``.

    Прогон по sha уже есть или у ветки открытый PR - заявка закрывается как
    ``observed`` (``present``). Ответ форжа не получен - ``unknown``: форж уходит в
    отступ, dispatch не делается, «не знаю» не равно «нет».

    Остаточное окно (его называет отчёт): между этой проверкой и самим запросом
    проходят миллисекунды, а список прогонов GitHub отстаёт на секунды - ручной
    запуск, начатый в эти секунды, хаб не увидит.
    """
    runs = await _runs_exist(scope, sha, dl)
    if runs is None:
        _back_off()
        return "unknown"
    if runs:
        await journal.settle(db, scope.task_id, sha, "observed")
        return "present"
    pr = await _open_pr(scope, dl)
    if pr is None:
        _back_off()
        return "unknown"
    if pr:
        await journal.settle(db, scope.task_id, sha, "observed", note="pr_open")
        return "present"
    return "clear"


async def _release(
    db: aiosqlite.Connection, scope: _Scope, sha: str, before: tuple[Any, ...]
) -> None:
    """Dispatch не последовал - попытка возвращается, чтобы «не знаю» её не съедало."""
    state, attempts, reserved_at = before
    await journal.release_reservation(
        db,
        scope.task_id,
        sha,
        state=state,
        attempts=attempts,
        reserved_at=reserved_at,
    )


async def _request(
    db: aiosqlite.Connection,
    scope: _Scope,
    sha: str,
    dl: _Deadline,
    before: tuple[Any, ...],
) -> bool:
    """Попросить прогон по зарезервированной голове; True - форж принял."""
    seen = await _external_ci_present(db, scope, sha, dl)
    if seen != "clear":
        if seen == "unknown":
            await _release(db, scope, sha, before)  # dispatch не было: попытка цела
        return False
    if await _fresh_scope(db, scope) is None or not await journal.task_in_scope(
        db, scope.task_id, scope.branch
    ):
        await _release(db, scope, sha, before)
        return False  # политика или задача сменились, пока шли пробы: запроса нет
    accepted, why = await _ask_forge(scope, dl)
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


async def _handle_head(
    db: aiosqlite.Connection, scope: _Scope, sha: str, dl: _Deadline
) -> bool:
    """Одна голова одной задачи; True - запрос принят форжем в этом проходе.

    «requested» - НЕподтверждённая заявка: запрос идёт по ref ветки, а не по sha.
    Пока по sha нет прогона workflow контракта, заявка остаётся открытой: прогон
    нашёлся - ``observed``; срока повтора ещё нет - ждём; срок вышел - заявка
    возвращается в очередь (не больше ``MAX_REQUESTS`` на sha).
    """
    grace = int(config.CI_HEAD_RUN_GRACE_SECONDS)
    retry = int(config.CI_HEAD_RUN_RETRY_SECONDS)
    row = await journal.observe_head(
        db, scope.task_id, sha, scope.branch, grace_seconds=grace, retry_seconds=retry
    )
    if row["state"] in journal.SETTLED or not row["grace_over"]:
        return False
    exists = await _runs_exist(scope, sha, dl)
    if exists is None:
        _back_off()
        return False
    if exists:
        await journal.settle(
            db, scope.task_id, sha, "observed", only_from=journal.OPEN_STATES
        )
        return False
    if row["state"] in _RETRYABLE and not row["retry_over"]:
        return False
    if row["state"] in _RETRYABLE and row["attempts"] >= journal.MAX_REQUESTS:
        await _settle_noting(
            db,
            scope.task_id,
            sha,
            "exhausted",
            f"Хаб: по голове {sha[:12]} прогон так и не появился после "
            f"{journal.MAX_REQUESTS} запросов. Запустите CI сами.",
        )
        return False
    if dl.left() < MIN_DISPATCH_LEFT_SECONDS:
        raise _OutOfTime
    if not await _reserve(db, scope, sha):
        return False
    before = (row["state"], int(row["attempts"]), row["reserved_at"])
    try:
        return await _request(db, scope, sha, dl, before)
    except _OutOfTime:
        await _release(db, scope, sha, before)
        raise


async def _verify_after_requests(
    db: aiosqlite.Connection, accepted: list[tuple[_Scope, str]], dl: _Deadline
) -> None:
    """Голова не сдвинулась, пока шёл запрос? Одно чтение на клон за проход.

    Если голова ушла на B, прогон мог пойти по B. B записывается «запрошена,
    исход неизвестен»: следующий цикл ищет её прогон, а не заказывает второй.
    Прежняя голова остаётся открытой заявкой и вернётся в очередь по сроку.
    """
    if not accepted:
        return
    now = await _read_heads([scope for scope, _ in accepted], dl)
    for scope, sha in accepted:
        head = now.get(scope.task_id)
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


async def request_for_new_heads(
    db: aiosqlite.Connection, dl: _Deadline
) -> dict[int, str]:
    """Первая половина прохода: новые головы без прогона получают запрос.

    Возвращает головы, прочитанные в этом проходе (для пометки объявлений).
    """
    global _cursor
    if time.monotonic() < _down_until:
        return {}
    scopes = await _scopes(db)
    if not scopes:
        return {}
    heads: dict[int, str] = {}
    accepted: list[tuple[_Scope, str]] = []
    try:
        heads = await _read_heads(scopes, dl)
        for scope in scopes:
            if dl.left() <= 0:
                break  # остальные - следующим проходом, курсор на последней
            _cursor = scope.task_id
            sha = heads.get(scope.task_id)
            if not sha:
                continue
            try:
                if await _handle_head(db, scope, sha, dl):
                    accepted.append((scope, sha))
            except _OutOfTime:
                break
            except Exception:  # noqa: BLE001 - one task must not skip the rest
                log.exception("CI head run for task #%s failed", scope.task_id)
        await _verify_after_requests(db, accepted, dl)
    except _OutOfTime:
        pass
    return heads


# ---- объявление итога --------------------------------------------------------


def _announcement(row: Mapping[str, Any], stale: bool) -> str:
    conclusion = ci_trust.run_conclusion(row) or "итог не назван"
    text = (
        f"CI на sha {str(row['head_sha'])[:12]}: {conclusion} "
        f"(run {row['run_id']}, попытка {row['run_attempt']})"
    )
    return f"{text}; голова уже другая" if stale else text


def _in_scope_for_read(scope: _Scope) -> bool:
    """Головы читаются из клона проекта или из клона хаба (его репозиторий)."""
    return (
        bool(scope.workspace) or not scope.gh_repo or scope.gh_repo == config.REPO_NAME
    )


async def _label_scopes(
    db: aiosqlite.Connection, rows: list[dict[str, Any]], known: dict[int, str]
) -> list[_Scope]:
    """Задачи объявлений, чью голову ещё не читали и можно прочитать."""
    scopes: dict[int, _Scope] = {}
    for row in rows:
        task_id = int(row["task_id"])
        if task_id in known or task_id in scopes:
            continue
        try:
            task = await repo.get_task(db, task_id)
            project = await repo.resolve_project_for_task(db, task_id)
            if task is not None and project is not None and (task["branch"] or ""):
                scope = _Scope(task_id, str(task["branch"]), project)
                if _in_scope_for_read(scope) and not _workspace_down(scope.workspace):
                    scopes[task_id] = scope
        except Exception:  # noqa: BLE001 - the label is a courtesy, not a gate
            log.debug("CI head run: no label scope for #%s", task_id, exc_info=True)
    return list(scopes.values())


async def _current_heads(
    db: aiosqlite.Connection,
    rows: list[dict[str, Any]],
    known: dict[int, str],
    dl: _Deadline,
) -> dict[int, str]:
    """Головы веток задач этих объявлений: известные с прохода + чтение на остальных."""
    found = dict(known)
    readable = await _label_scopes(db, rows, known)
    if readable:
        try:
            label_dl = _Deadline(min(LABEL_BUDGET_SECONDS, max(dl.left(), 0.0)))
            found.update(await _read_heads(readable, label_dl))
        except _OutOfTime:
            pass  # без пометки «голова уже другая»: она - любезность, не условие
    return found


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


async def _announce_row(
    db: aiosqlite.Connection,
    row: dict[str, Any],
    heads: dict[int, str],
    fps: dict[int, str],
) -> None:
    """Одна строка очереди. Непригодную по снимку отсекаем БЕЗ транзакции."""
    task_id = int(row["task_id"])
    if task_id not in fps:
        fps[task_id] = await ci_contract.fingerprint_for_task(db, task_id)
    if not ci_trust.is_verified(row, fps[task_id]):
        return  # подтверждение под другим контрактом: позже, когда его перепроверят
    await _announce_one(db, row, heads)


async def announce_results(
    db: aiosqlite.Connection,
    known_heads: dict[int, str] | None = None,
    dl: _Deadline | None = None,
) -> None:
    """Вторая половина прохода: подтверждённые итоги - в ленту, по разу на попытку.

    Очередь читается СВЕРХУ ВНИЗ постранично с сохраняемым курсором, и курсор
    стоит на ПОСЛЕДНЕЙ ОБРАБОТАННОЙ строке при любом исходе - в том числе когда
    время вышло посреди страницы. Пачка необъявляемых строк (контракт сменился
    после подтверждения) проходит мимо за несколько тиков и не запирает
    объявляемую за собой. Новая попытка отчёта (id выше прежней вершины)
    возвращает очередь к вершине: свежий итог не ждёт хвоста.
    """
    dl = dl or _Deadline(CYCLE_BUDGET_SECONDS)
    known = known_heads or {}
    top = await journal.top_attempt_id(db)
    if top > await journal.get_state(db, journal.ANNOUNCE_TOP):
        await journal.set_state(db, journal.ANNOUNCE_TOP, top)
        await journal.set_state(db, journal.ANNOUNCE_CURSOR, 0)
    cursor = await journal.get_state(db, journal.ANNOUNCE_CURSOR)
    before: int | None = cursor or None
    progress = cursor
    fps: dict[int, str] = {}
    try:
        for _page in range(journal.ANNOUNCE_PAGES):
            rows = await journal.unannounced_verified(
                db, limit=journal.ANNOUNCE_PAGE, before_id=before
            )
            if not rows:
                progress = 0  # до дна дошли
                return
            heads = await _current_heads(db, rows, known, dl)
            for row in rows:
                if dl.left() <= 0:
                    raise _OutOfTime
                try:
                    await _announce_row(db, row, heads, fps)
                except Exception:  # noqa: BLE001 - one row must not stop the queue
                    log.exception(
                        "CI head run: announcing attempt %s failed", row["id"]
                    )
                progress = int(row["id"])
            if len(rows) < journal.ANNOUNCE_PAGE:
                progress = 0
                return
            before = progress
    except _OutOfTime:
        pass  # время вышло: курсор уже на последней обработанной строке
    finally:
        await journal.set_state(db, journal.ANNOUNCE_CURSOR, progress)


async def _overdue_scopes(
    db: aiosqlite.Connection, rows: list[dict[str, Any]]
) -> dict[int, _Scope]:
    """Области задач из просроченных ожиданий, которые СЕЙЧАС ещё законны.

    Те же условия, что и для dispatch: политика и контракт проекта, GitHub,
    задача в running на той же ветке и без сдачи.
    """
    scopes: dict[int, _Scope] = {}
    for row in rows:
        task_id = int(row["task_id"])
        try:
            project = await repo.resolve_project_for_task(db, task_id)
            if project is None or not _in_scope(project):
                continue
            if not await journal.task_in_scope(db, task_id, str(row["branch"])):
                continue
            scopes[task_id] = _Scope(task_id, str(row["branch"]), project)
        except Exception:  # noqa: BLE001 - unreadable scope: no alert this time
            log.exception("CI head run: overdue scope of #%s unreadable", task_id)
    return scopes


async def announce_overdue(db: aiosqlite.Connection) -> None:
    """Итог не пришёл за срок: одна запись в ленту, alert и уведомление ответственному.

    Срок ``CI_HEAD_RUN_WAIT_MINUTES`` считается от заявки (или от первого
    наблюдения головы, если CI запустил сам исполнитель). Один раз на (задача,
    sha, число заявок): повторная заявка - новое ожидание. Alert говорит только о
    том, что ещё актуально: область задачи перепроверяется, а голова ветки
    читается заново и должна совпасть с ожидаемым sha; голову прочитать не
    удалось - молчим до следующего прохода.
    """
    rows = await journal.overdue_waits(db, int(config.CI_HEAD_RUN_WAIT_MINUTES))
    scopes = await _overdue_scopes(db, rows)
    if not scopes:
        return
    try:
        heads = await _read_heads(
            list(scopes.values()), _Deadline(LABEL_BUDGET_SECONDS)
        )
    except _OutOfTime:
        return
    for row in rows:
        task_id, sha = int(row["task_id"]), str(row["sha"])
        if task_id not in scopes or heads.get(task_id) != sha:
            continue
        try:
            run_id = await journal.latest_run_id(db, task_id, sha)
            text = (
                f"CI не пришёл за {row['waited']} мин: sha {sha[:12]}, run "
                f"{run_id or 'не найден'}. Итог не объявлен; запустите или "
                "проверьте CI."
            )
            key = f"timeout:{row['attempts']}"
            async with write_transaction(db):
                if await journal.claim_announcement(db, task_id, sha, key):
                    await repo.add_task_update(db, task_id, "hub", "alert", text)
                    await watch_notices.notify_steward(
                        db,
                        "stale",
                        task_id=task_id,
                        source_key=f"ci_head_timeout:{task_id}:{sha}:{row['attempts']}",
                    )
        except Exception:  # noqa: BLE001 - one task must not stop the others
            log.exception("CI head run: overdue note for #%s failed", task_id)


_last_prune = 0.0
PRUNE_EVERY_SECONDS = 3600.0
JOURNAL_RETENTION_DAYS = 14


async def prune_journals(db: aiosqlite.Connection) -> None:
    """Раз в час: журналы завершённых задач старше срока уходят."""
    global _last_prune
    if time.monotonic() - _last_prune < PRUNE_EVERY_SECONDS and _last_prune:
        return
    _last_prune = time.monotonic()
    await journal.prune_finished(db, days=JOURNAL_RETENTION_DAYS)


async def sweep(db: aiosqlite.Connection) -> None:
    """Проход поллера: запросы на новые головы, затем объявление итогов.

    Одно общее время на обе половины; сбой одной не отменяет другую.
    """
    dl = _Deadline(float(CYCLE_BUDGET_SECONDS))
    request_dl = _Deadline(float(CYCLE_BUDGET_SECONDS) * REQUEST_SHARE)
    heads: dict[int, str] = {}
    try:
        heads = await request_for_new_heads(db, request_dl)
    except Exception:  # noqa: BLE001 - announcing must still happen
        log.exception("CI head run: the request half failed")
    try:
        await announce_results(db, heads, dl)
    except _OutOfTime:
        pass
    except Exception:  # noqa: BLE001
        log.exception("CI head run: the announce half failed")
    try:
        await announce_overdue(db)
        await prune_journals(db)
    except Exception:  # noqa: BLE001
        log.exception("CI head run: the overdue half failed")


async def run_sweep(db: aiosqlite.Connection) -> None:
    """Вход для SWEEPS поллера: проход не имеет права убить тик.

    Дедлайн прохода - КООПЕРАТИВНЫЙ: сеть идёт под ``asyncio.timeout`` (там отмена
    безопасна, транзакция не держится), а между шагами БД проверяется остаток.
    Операции БД снаружи НЕ отменяются никогда: отменённый await оставляет
    aiosqlite дорабатывать SQL, и общее соединение поллера оказывается в
    транзакции, которую никто не завершит. Ожидание замка ограничено
    ``busy_timeout`` самого соединения.
    """
    try:
        await sweep(db)
    except Exception:  # noqa: BLE001 - the sweep must not kill the loop
        log.exception("CI head run sweep failed")
