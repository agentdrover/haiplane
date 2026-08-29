"""No-op (null) implementations of all integration protocols.

These are used as defaults in the plugin registry so Hub always starts
even when no real integrations are configured.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from typing import Any

import aiosqlite

from hub.integrations.protocols import (
    CIProbeOutcome,
    CIProbeResult,
    MergeabilityOutcome,
)


class NoopDispatch:
    def is_available(self) -> bool:
        return False

    def list_jobs(self, limit: int = 50) -> list[dict[str, Any]]:
        return []

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        return None

    def job_log_tail(self, job_id: str, max_lines: int = 60) -> list[str]:
        return []

    def job_log_full(self, job_id: str) -> str:
        return ""

    def build_enriched_message(
        self,
        title: str,
        description: str,
        updates: list[dict[str, Any]] | None = None,
        branch: str = "",
        breadcrumb: str = "",
    ) -> str:
        return f"[noop] {title}"

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
    ) -> str:
        return f"[noop] review #{task_id}: {title}"

    def build_fix_message(
        self,
        task_id: int,
        title: str,
        description: str,
        review_comments: str,
        review_cycle: int,
        max_cycles: int,
        branch: str = "",
    ) -> str:
        return f"[noop] fix #{task_id}: {title}"

    def build_ci_fix_message(
        self,
        task_id: int,
        title: str,
        description: str,
        ci_failures: dict[str, Any],
        ci_fix_cycle: int,
        max_cycles: int,
        branch: str = "",
    ) -> str:
        return f"[noop] ci-fix #{task_id}: {title}"

    def build_arbiter_message(
        self,
        task_id: int,
        title: str,
        description: str,
        review_history: list[dict[str, Any]],
        review_cycle: int,
        max_cycles: int,
        branch: str = "",
    ) -> str:
        return f"[noop] arbiter #{task_id}: {title}"

    async def submit_task(
        self,
        message: str,
        runtime: str = "auto",
        repo_root: str | None = None,
        agent: str | None = None,
        task_id: int | None = None,
    ) -> dict[str, Any]:
        return {"error": "dispatch plugin not configured"}

    async def classify_task(
        self, message: str, repo_root: str | None = None
    ) -> dict[str, Any]:
        return {"error": "dispatch plugin not configured"}


class NoopGitOps:
    async def current_branch(self, repo: str | None = None) -> str:
        return ""

    async def head_sha(self, repo: str, base: str) -> str:
        return ""

    async def resolve_ref(self, name: str, repo: str) -> tuple[str, str]:
        """No git here — "could not look", never "the ref is missing" (#725)."""
        return ("unavailable", "git integration is not configured")

    async def base_freshness(self, repo: str, base: str, sha: str) -> tuple[str, str]:
        """Same rule one level down (#947): unverified, never "stale"."""
        return ("unverified", "git integration is not configured")

    async def ensure_remote_branch(
        self,
        branch: str,
        source: str,
        *,
        repo: str | None = None,
        gh_repo: str | None = None,
    ) -> tuple[str, str]:
        """No git here, so nothing is claimed about the branch (#947)."""
        return ("unavailable", "git integration is not configured")

    async def pr_for_branch(
        self,
        branch: str,
        repo: str | None = None,
        gh_repo: str | None = None,
    ) -> int | None:
        return None

    async def pr_state(
        self,
        pr_number: int,
        repo: str | None = None,
        gh_repo: str | None = None,
    ) -> str:
        """No git here — "could not look" (#802).

        Never "closed" and never "absent" (#959): both are answers, and this
        double has none. Silence is the only honest reply from a stub.
        """
        return ""

    async def pr_is_draft(
        self,
        pr_number: int,
        repo: str | None = None,
        gh_repo: str | None = None,
    ) -> bool:
        """No GitHub — "not a draft". Ignorance is not an accusation (#498)."""
        return False

    async def mark_pr_ready(
        self,
        pr_number: int,
        repo: str | None = None,
        gh_repo: str | None = None,
    ) -> bool:
        return False

    async def release_range(
        self,
        base: str,
        head: str,
        repo: str | None = None,
        gh_repo: str | None = None,
    ) -> list[str]:
        """No git here — an empty range means the release has nothing to say."""
        return []

    async def undelivered_release_range(
        self,
        base: str,
        head: str,
        repo: str | None = None,
        gh_repo: str | None = None,
    ) -> list[str] | None:
        """No git here — "could not look", never "the cut is empty" (#725)."""
        return None

    async def open_release_pr(
        self,
        base: str,
        head: str,
        title: str,
        body: str,
        repo: str | None = None,
        gh_repo: str | None = None,
    ) -> int | None:
        return None

    async def merge_commit_sha(
        self,
        pr_number: int,
        repo: str | None = None,
        gh_repo: str | None = None,
    ) -> str:
        return ""

    async def branch_diff(self, repo: str, base: str, branch: str) -> str | None:
        """No git here — the section must read this as "could not look" (#601)."""
        return None

    async def file_at_ref(self, repo: str, ref: str, path: str) -> str | None:
        """No git here — there is no rules file to read (#873)."""
        return None

    async def files_at_ref(self, repo: str, ref: str) -> set[str] | None:
        """No git here — "could not look", never "the submission lacks it" (#764)."""
        return None

    async def commit_exists(self, repo: str, sha: str) -> bool | None:
        """No git here — "could not look", never "the commit is gone" (#824)."""
        return None

    async def commit_diff(
        self, repo: str, base: str, sha: str, *, context: int = 3
    ) -> str | None:
        """No git here — the reader must see "could not look" (#824)."""
        return None

    async def is_ancestor(
        self, repo: str, ancestor: str, descendant: str
    ) -> bool | None:
        """No git here — "could not look", never "not in that history" (#497)."""
        return None

    async def commit_diff_stat(
        self, repo: str, base: str, sha: str
    ) -> list[tuple[int, int, str]] | None:
        """No git here — "could not look", never "nothing changed" (#825)."""
        return None

    async def content_differs(
        self,
        base: str,
        head: str,
        repo: str | None = None,
        gh_repo: str | None = None,
    ) -> bool | None:
        """No git here — "could not look", never "nothing to release" (#968).

        The distinction is load-bearing: False would silently stop every
        release, None makes the caller say why it did nothing.
        """
        return None

    async def check_pr_mergeable(
        self,
        pr_number: int,
        *,
        repo: str | None = None,
        gh_repo: str | None = None,
    ) -> tuple[MergeabilityOutcome, str]:
        """No GitHub here — "could not ask", never "mergeable" (#970).

        Answering ``mergeable`` would let a release walk into a merge nobody
        checked; answering ``conflicting`` would invent a conflict. Both are
        claims about a repository this integration cannot see.
        """
        return (MergeabilityOutcome.unavailable, "git integration is not configured")

    async def return_release_into_base(
        self,
        base: str,
        head: str,
        *,
        repo: str | None = None,
        gh_repo: str | None = None,
    ) -> tuple[str, str]:
        """No git here — "could not ask", never "nothing to return" (#969).

        The distinction is load-bearing the same way it is in
        content_differs: "nothing" would let the divergence pile up in
        silence, and a silent pipeline failure is found later than a noisy
        one.
        """
        return ("unavailable", "git integration is not configured")

    async def commit_with_same_tree(
        self, repo: str, sha: str, branch: str
    ) -> str | None:
        """No git here — "could not look", never "no such content" (#946).

        The empty string is a real answer ("looked, the branch never held this
        content"); None is the absence of one, and delivery state must keep
        them apart or a squash release starts reading as a failed one.
        """
        return None

    async def fetch_commit(
        self, repo: str, sha: str, ref: str = "", *, timeout: int = 20
    ) -> tuple[bool, str]:
        """No git here — nothing is fetched, and the caller must say so (#883)."""
        return (False, "git integration is not configured")

    async def fetch_base(self, repo: str, base: str) -> tuple[bool, str]:
        """No git here — the drift check must read this as "cannot check",
        never as "clean" (#534)."""
        return (False, "git integration is not configured")

    async def first_parent_log(self, repo: str, base: str, limit: int) -> str | None:
        return None

    async def branch_contains_unmerged_commits_of(
        self,
        branch: str,
        other_branch: str,
        base_branch: str | None = None,
        repo: str | None = None,
    ) -> bool:
        # No repo access — the advisory stacking check is silently skipped.
        return False

    async def create_branch(
        self,
        task_id: int,
        title: str,
        repo: str | None = None,
        base_branch: str | None = None,
    ) -> str:
        return ""

    async def pair_prepare_branch(
        self,
        task_id: int,
        title: str,
        *,
        branch_slug: str = "",
        repo: str | None = None,
        base_branch: str | None = None,
        notify: Callable[[str], Awaitable[None]] | None = None,
    ) -> str:
        from hub.integrations.git_ops import _slugify

        slug = (branch_slug or "").strip() or _slugify(title)
        return f"task-{task_id}/{slug}"

    async def pair_restore_workspace_base(
        self,
        task_id: int,
        *,
        repo: str | None = None,
        base_branch: str | None = None,
    ) -> bool:
        return False

    async def pair_switch_to_task_branch(
        self,
        task_id: int,
        branch: str,
        *,
        repo: str | None = None,
        base_branch: str | None = None,
    ) -> bool:
        return False

    def worktree_path(self, task_id: int, repo: str | None = None) -> str:
        return ""

    async def worktree_is_registered(self, path: str, repo: str | None = None) -> bool:
        return False

    async def pair_prepare_worktree(
        self,
        task_id: int,
        title: str,
        *,
        branch_slug: str = "",
        repo: str | None = None,
        base_branch: str | None = None,
    ) -> str:
        return f"task-{task_id}/{branch_slug or 'work'}"

    async def pair_remove_worktree(
        self, task_id: int, *, repo: str | None = None
    ) -> bool:
        return False

    async def origin_reachable(
        self, repo: str | None = None, *, timeout: int = 30
    ) -> bool:
        return False

    async def checkout(self, branch: str, repo: str | None = None) -> bool:
        return False

    async def dirty_paths(self, repo: str | None = None) -> list[str]:
        return []

    async def branch_diff_paths(
        self,
        branch: str,
        base_branch: str | None = None,
        repo: str | None = None,
    ) -> list[str] | None:
        return None

    async def auto_commit(
        self,
        task_id: int,
        title: str = "",
        message: str | None = None,
        repo: str | None = None,
        expected_branch: str | None = None,
    ) -> bool:
        return False

    async def pull_main(
        self, repo: str | None = None, base_branch: str | None = None
    ) -> bool:
        return False

    async def squash_branch(
        self,
        task_id: int,
        title: str,
        branch: str,
        repo: str | None = None,
        base_branch: str | None = None,
    ) -> bool:
        return False

    async def push_branch(
        self, branch: str, repo: str | None = None, force: bool = False
    ) -> bool:
        return False

    async def create_pr(
        self,
        task_id: int,
        title: str,
        description: str,
        branch: str,
        repo: str | None = None,
        gh_repo: str | None = None,
        base_branch: str | None = None,
    ) -> int | None:
        return None

    async def get_ci_failure_logs(
        self,
        pr_number: int,
        branch: str,
        max_log_chars: int = 4000,
        repo: str | None = None,
        gh_repo: str | None = None,
    ) -> dict[str, Any]:
        return {}

    async def check_pr_ci(
        self, pr_number: int, repo: str | None = None, gh_repo: str | None = None
    ) -> CIProbeResult:
        return CIProbeResult(CIProbeOutcome.pending, "noop")

    async def branch_ci_runs(
        self,
        branch: str,
        limit: int = 20,
        repo: str | None = None,
        gh_repo: str | None = None,
    ) -> list[dict] | None:
        # None, not []: this plugin cannot look, and "looked and found no
        # runs" is a different answer that would read as a green base (#929).
        return None

    async def merge_pr(
        self,
        pr_number: int,
        task_id: int,
        title: str,
        repo: str | None = None,
        gh_repo: str | None = None,
        delete_branch: bool = True,
    ) -> bool:
        return False

    async def delete_branch(
        self,
        branch: str,
        repo: str | None = None,
        base_branch: str | None = None,
    ) -> bool:
        return False

    async def clone_repo(
        self, repo_url: str, workspace_path: str, base_branch: str | None = None
    ) -> tuple[bool, str]:
        # Valid provision outcome (#347): the operator sees WHY it failed.
        return False, "git ops disabled (noop integration)"


class NoopGitHub:
    async def recent_commits(self, limit: int = 10) -> list[dict[str, Any]]:
        return []

    async def open_prs(self) -> list[dict[str, Any]]:
        return []


class NoopNotes:
    async def availability(self) -> dict[str, str]:
        return {"status": "no_binary", "detail": "notes integration disabled"}

    async def recent_decisions(
        self, space_id: str | None = None, limit: int = 10
    ) -> list[dict[str, Any]]:
        return []

    async def save_decision(
        self,
        task_id: int,
        action: str,
        summary: str,
        context: str = "",
    ) -> dict[str, Any] | None:
        return None


class NoopVast:
    async def has_active_vast_tasks(self, db: aiosqlite.Connection) -> bool:
        return False

    async def vast_up(self) -> dict[str, Any]:
        return {"error": "vast plugin not configured"}

    async def vast_status(self) -> dict[str, Any]:
        return {"managed": False}

    async def vast_down(self) -> dict[str, Any]:
        return {"destroyed": False, "error": "vast plugin not configured"}


class NoopTranscripts:
    def list_recent_transcripts(self, limit: int = 10) -> list[dict[str, Any]]:
        return []

    def transcript_detail(
        self, transcript_path: str, tail_events: int = 30
    ) -> list[dict[str, Any]]:
        return []
