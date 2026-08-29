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
    CI dead-end. These five outcomes keep them distinct so the poller can
    branch on a typed value, never on free-form text.
    """

    passed = "pass"
    failed = "fail"
    pending = "pending"  # checks exist and are still running
    absent = "absent"  # the PR has no checks at all → skip the conveyor
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
    ) -> tuple[str, str]: ...
    async def check_pr_mergeable(
        self,
        pr_number: int,
        *,
        repo: str | None = None,
        gh_repo: str | None = None,
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
        base_branch: str | None = None,
    ) -> int | None: ...
    async def get_ci_failure_logs(
        self,
        pr_number: int,
        branch: str,
        max_log_chars: int = 4000,
        repo: str | None = None,
        gh_repo: str | None = None,
    ) -> dict[str, Any]: ...
    async def check_pr_ci(
        self, pr_number: int, repo: str | None = None, gh_repo: str | None = None
    ) -> CIProbeResult: ...
    async def branch_ci_runs(
        self,
        branch: str,
        limit: int = 20,
        repo: str | None = None,
        gh_repo: str | None = None,
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
    ) -> int | None: ...
    async def merge_commit_sha(
        self,
        pr_number: int,
        repo: str | None = None,
        gh_repo: str | None = None,
    ) -> str: ...
    async def pr_state(
        self, pr_number: int, repo: str | None = None, gh_repo: str | None = None
    ) -> str: ...
    async def release_range(
        self,
        base: str,
        head: str,
        repo: str | None = None,
        gh_repo: str | None = None,
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
    ) -> int | None: ...
    async def merge_pr(
        self,
        pr_number: int,
        task_id: int,
        title: str,
        repo: str | None = None,
        gh_repo: str | None = None,
        delete_branch: bool = True,
    ) -> bool: ...
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
