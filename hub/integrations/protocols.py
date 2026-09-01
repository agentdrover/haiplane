"""Plugin protocol definitions for Hub integrations.

Each protocol defines the public interface that an integration must implement.
Hub core depends only on these protocols, never on concrete implementations.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol, runtime_checkable

import aiosqlite


class CIProbeOutcome(str, Enum):
    """Every observable result of probing a PR's CI checks (#419).

    The old string return collapsed running checks, an empty check set, a gh
    error and unparseable output all into ``pending`` — the single biggest
    CI dead-end. These outcomes keep them distinct so the poller can
    branch on a typed value, never on free-form text.
    """

    passed = "pass"
    failed = "fail"
    pending = "pending"  # checks exist and are still running
    absent = "absent"  # the repo has no workflows / the PR has no checks at all
    missing_run = "missing_run"  # workflows exist, but this SHA has no run yet
    unavailable = "unavailable"  # gh error / invalid JSON / unknown state


class MergeabilityOutcome(str, Enum):
    """Can this pull request be merged at all — and if not, why (#970).

    The release used to learn this by trying: it checked CI, called
    ``merge_pr``, and reported "GitHub отказал" when the call came back
    false. That names the actor, not the cause — a conflict, a revoked
    token, a deleted branch and a passing five-hundred all sound the same
    and are fixed by different hands. On 26.08.2026 release PR #83 stood
    conflicted at green CI, and the diagnosis a human got in one ``gh``
    call the mechanism could not state at all.

    ``unknown`` is not a diagnosis and must never be collapsed into
    ``conflicting``: GitHub computes mergeability asynchronously and
    answers UNKNOWN honestly for the first seconds of every new pull
    request. Reading that as a conflict would raise a false alarm on every
    release the moment it opens — #725 from the other side.
    """

    mergeable = "mergeable"
    conflicting = "conflicting"
    unknown = "unknown"  # GitHub has not computed it yet → ask again
    unavailable = "unavailable"  # gh error / invalid JSON / no answer


@dataclass(frozen=True)
class CIProbeResult:
    """A CI probe outcome plus a stable, machine-usable reason (#419)."""

    outcome: CIProbeOutcome
    reason: str
    details: str | None = None


@runtime_checkable
class DispatchPlugin(Protocol):
    def is_available(self) -> bool: ...
    def list_jobs(self, limit: int = 50) -> list[dict[str, Any]]: ...
    def get_job(self, job_id: str) -> dict[str, Any] | None: ...
    def job_log_tail(self, job_id: str, max_lines: int = 60) -> list[str]: ...
    def job_log_full(self, job_id: str) -> str: ...

    def build_enriched_message(
        self,
        title: str,
        description: str,
        updates: list[dict[str, Any]] | None = None,
        branch: str = "",
        breadcrumb: str = "",
    ) -> str: ...

    def build_review_message(
        self,
        task_id: int,
        title: str,
        description: str,
        review_cycle: int,
        max_cycles: int,
        branch: str = "",
        pr_number: int | None = None,
        breadcrumb: str = "",
    ) -> str: ...

    def build_fix_message(
        self,
        task_id: int,
        title: str,
        description: str,
        review_comments: str,
        review_cycle: int,
        max_cycles: int,
        branch: str = "",
    ) -> str: ...

    def build_ci_fix_message(
        self,
        task_id: int,
        title: str,
        description: str,
        ci_failures: dict[str, Any],
        ci_fix_cycle: int,
        max_cycles: int,
        branch: str = "",
    ) -> str: ...

    def build_arbiter_message(
        self,
        task_id: int,
        title: str,
        description: str,
        review_history: list[dict[str, Any]],
        review_cycle: int,
        max_cycles: int,
        branch: str = "",
    ) -> str: ...

    async def submit_task(
        self,
        message: str,
        runtime: str = "auto",
        repo_root: str | None = None,
        agent: str | None = None,
        task_id: int | None = None,
    ) -> dict[str, Any]: ...

    async def classify_task(
        self, message: str, repo_root: str | None = None
    ) -> dict[str, Any]: ...


@runtime_checkable
class GitOpsPlugin(Protocol):
    async def current_branch(self, repo: str | None = None) -> str: ...
    async def resolve_ref(self, name: str, repo: str) -> tuple[str, str]: ...
    async def branch_contains_unmerged_commits_of(
        self,
        branch: str,
        other_branch: str,
        base_branch: str | None = None,
        repo: str | None = None,
    ) -> bool: ...
    async def create_branch(
        self, task_id: int, title: str, repo: str | None = None
    ) -> str: ...
    async def pair_prepare_branch(
        self,
        task_id: int,
        title: str,
        *,
        branch_slug: str = "",
        repo: str | None = None,
        base_branch: str | None = None,
        notify: Callable[[str], Awaitable[None]] | None = None,
    ) -> str: ...
    async def pair_restore_workspace_base(
        self,
        task_id: int,
        *,
        repo: str | None = None,
        base_branch: str | None = None,
    ) -> bool: ...
    async def pair_switch_to_task_branch(
        self,
        task_id: int,
        branch: str,
        *,
        repo: str | None = None,
        base_branch: str | None = None,
    ) -> bool: ...
    def worktree_path(self, task_id: int, repo: str | None = None) -> str: ...
    async def worktree_is_registered(
        self, path: str, repo: str | None = None
    ) -> bool: ...
    async def pair_prepare_worktree(
        self,
        task_id: int,
        title: str,
        *,
        branch_slug: str = "",
        repo: str | None = None,
        base_branch: str | None = None,
    ) -> str: ...
    async def pair_remove_worktree(
        self, task_id: int, *, repo: str | None = None
    ) -> bool: ...
    async def origin_reachable(
        self, repo: str | None = None, *, timeout: int = 30
    ) -> bool: ...
    async def checkout(self, branch: str, repo: str | None = None) -> bool: ...
    async def dirty_paths(self, repo: str | None = None) -> list[str]: ...
    async def branch_diff_paths(
        self,
        branch: str,
        base_branch: str | None = None,
        repo: str | None = None,
    ) -> list[str] | None: ...
    async def commit_exists(self, repo: str, sha: str) -> bool | None: ...
    async def commit_diff(
        self, repo: str, base: str, sha: str, *, context: int = 3
    ) -> str | None: ...
    async def commit_diff_stat(
        self, repo: str, base: str, sha: str
    ) -> list[tuple[int, int, str]] | None: ...
    async def is_ancestor(
        self, repo: str, ancestor: str, descendant: str
    ) -> bool | None: ...
    async def content_differs(
        self,
        base: str,
        head: str,
        repo: str | None = None,
        gh_repo: str | None = None,
    ) -> bool | None: ...
    async def return_release_into_base(
        self,
        base: str,
        head: str,
        *,
        repo: str | None = None,
        gh_repo: str | None = None,
        forge: str = "",
    ) -> tuple[str, str]: ...
    async def check_pr_mergeable(
        self,
        pr_number: int,
        *,
        repo: str | None = None,
        gh_repo: str | None = None,
        forge: str = "",
    ) -> tuple[MergeabilityOutcome, str]: ...
    async def commit_with_same_tree(
        self, repo: str, sha: str, branch: str
    ) -> str | None: ...
    async def fetch_commit(
        self, repo: str, sha: str, ref: str = "", *, timeout: int = 20
    ) -> tuple[bool, str]: ...
    async def auto_commit(
        self,
        task_id: int,
        title: str = "",
        message: str | None = None,
        repo: str | None = None,
        expected_branch: str | None = None,
    ) -> bool: ...
    async def pull_main(
        self, repo: str | None = None, base_branch: str | None = None
    ) -> bool: ...
    async def squash_branch(
        self,
        task_id: int,
        title: str,
        branch: str,
        repo: str | None = None,
        base_branch: str | None = None,
    ) -> bool: ...
    async def push_branch(
        self, branch: str, repo: str | None = None, force: bool = False
    ) -> bool: ...
    async def create_pr(
        self,
        task_id: int,
        title: str,
        description: str,
        branch: str,
        repo: str | None = None,
        gh_repo: str | None = None,
        forge: str = "",
        base_branch: str | None = None,
    ) -> int | None: ...
    async def get_ci_failure_logs(
        self,
        pr_number: int,
        branch: str,
        max_log_chars: int = 4000,
        repo: str | None = None,
        gh_repo: str | None = None,
        forge: str = "",
    ) -> dict[str, Any]: ...
    async def check_pr_ci(
        self,
        pr_number: int,
        repo: str | None = None,
        gh_repo: str | None = None,
        forge: str = "",
    ) -> CIProbeResult: ...
    async def branch_ci_runs(
        self,
        branch: str,
        limit: int = 20,
        repo: str | None = None,
        gh_repo: str | None = None,
        forge: str = "",
    ) -> list[dict[str, Any]] | None: ...
    # Читатели git и GitHub, которые вызываются из services/, но в протоколе
    # отсутствовали: реализации (git_ops, noop) их имеют, контракт — нет. То
    # есть подменить плагин по этому протоколу было можно только угадав, что
    # ещё требуется сверх объявленного (#847).
    async def head_sha(self, repo: str, base: str) -> str: ...
    # #947: two questions the hub started asking after a release deleted the
    # branch everything is delivered into — is the base this diff stands on
    # still the one the remote carries, and does the integration branch still
    # exist after a release merge.
    async def base_freshness(
        self, repo: str, base: str, sha: str
    ) -> tuple[str, str]: ...
    async def ensure_remote_branch(
        self,
        branch: str,
        source: str,
        *,
        repo: str | None = None,
        gh_repo: str | None = None,
    ) -> tuple[str, str]: ...
    async def branch_diff(self, repo: str, base: str, branch: str) -> str | None: ...
    async def file_at_ref(self, repo: str, ref: str, path: str) -> str | None: ...
    async def files_at_ref(self, repo: str, ref: str) -> set[str] | None: ...
    async def fetch_base(self, repo: str, base: str) -> tuple[bool, str]: ...
    async def first_parent_log(
        self, repo: str, base: str, limit: int
    ) -> str | None: ...
    async def pr_for_branch(
        self,
        branch: str,
        repo: str | None = None,
        gh_repo: str | None = None,
        forge: str = "",
    ) -> int | None: ...
    async def merge_commit_sha(
        self,
        pr_number: int,
        repo: str | None = None,
        gh_repo: str | None = None,
        forge: str = "",
    ) -> str: ...
    async def pr_state(
        self,
        pr_number: int,
        repo: str | None = None,
        gh_repo: str | None = None,
        forge: str = "",
    ) -> str: ...
    async def pr_is_draft(
        self,
        pr_number: int,
        repo: str | None = None,
        gh_repo: str | None = None,
        forge: str = "",
    ) -> bool: ...
    async def mark_pr_ready(
        self,
        pr_number: int,
        repo: str | None = None,
        gh_repo: str | None = None,
        forge: str = "",
    ) -> bool: ...
    async def release_range(
        self,
        base: str,
        head: str,
        repo: str | None = None,
        gh_repo: str | None = None,
        forge: str = "",
    ) -> list[str]: ...
    async def undelivered_release_range(
        self,
        base: str,
        head: str,
        repo: str | None = None,
        gh_repo: str | None = None,
    ) -> list[str] | None: ...
    async def open_release_pr(
        self,
        base: str,
        head: str,
        title: str,
        body: str,
        repo: str | None = None,
        gh_repo: str | None = None,
        forge: str = "",
    ) -> int | None: ...
    async def merge_pr(
        self,
        pr_number: int,
        task_id: int,
        title: str,
        repo: str | None = None,
        gh_repo: str | None = None,
        forge: str = "",
        delete_branch: bool = True,
    ) -> bool: ...
    # То же, но с ПРИЧИНОЙ отказа (#1116, по ревью). Булевого ответа не
    # хватает: «слить не смогли» и «слили, но подтвердить не удалось» ведут к
    # противоположным действиям — первое к человеку, второе к повтору через
    # цикл. Схлопнутые в один False, они уводили доставленную работу в
    # needs_decision, оставляя PR открытым и реестр пустым.
    async def merge_pr_with_detail(
        self,
        pr_number: int,
        task_id: int,
        title: str,
        repo: str | None = None,
        gh_repo: str | None = None,
        forge: str = "",
        delete_branch: bool = True,
    ) -> tuple[bool, str]: ...
    async def delete_branch(
        self,
        branch: str,
        repo: str | None = None,
        base_branch: str | None = None,
    ) -> bool: ...
    async def clone_repo(
        self, repo_url: str, workspace_path: str, base_branch: str | None = None
    ) -> tuple[bool, str]: ...


@runtime_checkable
class ForgePlugin(Protocol):
    """Всё, что хаб спрашивает не у git, а у хостинга репозитория (#1113).

    До этого протокола форж не был объявлен нигде: девятнадцать методов
    ``git_ops`` звали ``gh`` напрямую, и «какой хостинг» было размазано по
    девятнадцати местам. Подключить второй (GitVerse, #1112) можно было
    только вторым набором ветвлений внутри и без того трёхтысячестрочного
    файла.

    Граница проведена по вопросу, а не по транспорту: форж отвечает
    ``CIProbeResult`` и ``MergeabilityOutcome``, а не сырым JSON. Это не
    вкусовщина. У GitHub исход CI собирается из двух источников (``gh pr
    checks`` и запуски Actions, #606), у GitVerse источник один и без поля
    ``conclusion``. Отдавай форж сырьё — и разбор его формы всё равно
    расползётся по вызывающим, вместе с семантикой #419, которую там будет
    некому защитить.

    Чистый git сюда не входит вовсе: ветки, коммиты, диффы, worktree —
    это ``GitOpsPlugin``, и форжу они безразличны.

    ``repo`` — локальный клон, из которого делается вызов (для gh это
    рабочий каталог); ``gh_repo`` — сам репозиторий в форже, ``owner/name``.
    """

    #: Машинное имя форжа: попадает в диагностику, чтобы отказ называл, КТО
    #: отказал, а не только что отказали.
    name: str

    #: Умеет ли форж СЛИТЬ pull request своими силами (#1116).
    #:
    #: Объявлено флагом, а не выяснено по ходу, потому что различие настоящее
    #: и дорогое: у GitHub мерж — это вызов API, у GitVerse такого вызова нет
    #: вовсе, и слить можно только локальным git с последующим push. Прятать
    #: это за «попробуем и посмотрим» нельзя: измерено 01.09.2026, что после
    #: merge --no-ff и push'а GitVerse оставляет PR в состоянии open,
    #: merged=False, а GET /pulls/{n}/merge отвечает 404 и ДО, и ПОСЛЕ мержа.
    #: То есть форж не сообщает об успехе даже задним числом — вызывающий
    #: обязан знать заранее, что доказательство придётся искать в другом месте.
    can_merge_via_api: bool

    async def create_pr(
        self,
        title: str,
        body: str,
        branch: str,
        base: str,
        *,
        repo: str | None = None,
        gh_repo: str | None = None,
    ) -> int | None: ...
    async def pr_for_branch(
        self, branch: str, *, repo: str | None = None, gh_repo: str | None = None
    ) -> int | None: ...
    async def open_or_update_pr(
        self,
        base: str,
        head: str,
        title: str,
        body: str,
        *,
        repo: str | None = None,
        gh_repo: str | None = None,
    ) -> int | None: ...
    async def pr_state(
        self, pr_number: int, *, repo: str | None = None, gh_repo: str | None = None
    ) -> str: ...
    async def pr_is_draft(
        self, pr_number: int, *, repo: str | None = None, gh_repo: str | None = None
    ) -> bool: ...
    async def mark_pr_ready(
        self, pr_number: int, *, repo: str | None = None, gh_repo: str | None = None
    ) -> bool: ...
    async def pr_head_sha(
        self, pr_number: int, *, repo: str | None = None, gh_repo: str | None = None
    ) -> str: ...
    # Базовая и головная ветки PR — нужны, чтобы НАЗВАТЬ файлы конфликта.
    # Сами файлы ищет git на стороне git_ops: форж их не знает, а ответ
    # «конфликт» без имён файлов — это причина, которая никуда не ведёт.
    async def pr_refs(
        self, pr_number: int, *, repo: str | None = None, gh_repo: str | None = None
    ) -> tuple[str, str]: ...
    async def pr_mergeability(
        self, pr_number: int, *, repo: str | None = None, gh_repo: str | None = None
    ) -> tuple[MergeabilityOutcome, str]: ...
    async def merge_commit_sha(
        self, pr_number: int, *, repo: str | None = None, gh_repo: str | None = None
    ) -> str: ...
    async def merge_pr(
        self,
        pr_number: int,
        subject: str,
        *,
        delete_branch: bool = True,
        repo: str | None = None,
        gh_repo: str | None = None,
    ) -> bool: ...
    # Закрыть PR, ничего не вливая (#1116). Нужен там, где мерж сделан не
    # форжем: GitVerse не замечает мержа пушем и оставляет PR открытым
    # навсегда, а открытый PR на доставленной ветке заставит pr_for_branch
    # находить его снова и снова.
    async def close_pr(
        self, pr_number: int, *, repo: str | None = None, gh_repo: str | None = None
    ) -> bool: ...
    # Достижим ли ``sha`` в ветке ``branch`` на remote — доказательство
    # доставки, не зависящее от существования и состояния PR (#1116).
    async def branch_contains(
        self,
        branch: str,
        sha: str,
        *,
        repo: str | None = None,
        gh_repo: str | None = None,
    ) -> bool | None: ...
    async def check_pr_ci(
        self, pr_number: int, *, repo: str | None = None, gh_repo: str | None = None
    ) -> CIProbeResult: ...
    async def branch_ci_runs(
        self,
        branch: str,
        limit: int = 20,
        *,
        repo: str | None = None,
        gh_repo: str | None = None,
    ) -> list[dict[str, Any]] | None: ...
    async def ci_failure_logs(
        self,
        pr_number: int,
        branch: str,
        max_log_chars: int = 12000,
        *,
        repo: str | None = None,
        gh_repo: str | None = None,
    ) -> dict[str, Any]: ...
    async def has_workflows(
        self, *, repo: str | None = None, gh_repo: str | None = None
    ) -> bool | None: ...
    async def compare_subjects(
        self,
        base: str,
        head: str,
        *,
        repo: str | None = None,
        gh_repo: str | None = None,
    ) -> list[str]: ...
    # Слить ``from_branch`` в ``into_branch`` силами форжа.
    # ``(returned <sha> | nothing | conflict | unavailable, detail)``.
    async def merge_branches(
        self,
        into_branch: str,
        from_branch: str,
        message: str,
        *,
        repo: str | None = None,
        gh_repo: str | None = None,
    ) -> tuple[str, str]: ...


@runtime_checkable
class GitHubPlugin(Protocol):
    async def recent_commits(self, limit: int = 10) -> list[dict[str, Any]]: ...
    async def open_prs(self) -> list[dict[str, Any]]: ...


@runtime_checkable
class NotesPlugin(Protocol):
    async def recent_decisions(
        self, space_id: str | None = None, limit: int = 10
    ) -> list[dict[str, Any]]: ...

    async def save_decision(
        self,
        task_id: int,
        action: str,
        summary: str,
        context: str = "",
    ) -> dict[str, Any] | None: ...


@runtime_checkable
class VastPlugin(Protocol):
    async def has_active_vast_tasks(self, db: aiosqlite.Connection) -> bool: ...
    async def vast_up(self) -> dict[str, Any]: ...
    async def vast_status(self) -> dict[str, Any]: ...
    async def vast_down(self) -> dict[str, Any]: ...


@runtime_checkable
class TranscriptsPlugin(Protocol):
    def list_recent_transcripts(self, limit: int = 10) -> list[dict[str, Any]]: ...
    def transcript_detail(
        self, transcript_path: str, tail_events: int = 30
    ) -> list[dict[str, Any]]: ...
