"""Plugin protocol definitions for Hub integrations.

Each protocol defines the public interface that an integration must implement.
Hub core depends only on these protocols, never on concrete implementations.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

import aiosqlite


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
    async def create_branch(
        self, task_id: int, title: str, repo: str | None = None
    ) -> str: ...
    async def checkout(self, branch: str, repo: str | None = None) -> bool: ...
    async def auto_commit(
        self,
        task_id: int,
        title: str = "",
        message: str | None = None,
        repo: str | None = None,
    ) -> bool: ...
    async def pull_main(self, repo: str | None = None) -> bool: ...
    async def squash_branch(
        self, task_id: int, title: str, branch: str, repo: str | None = None
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
    ) -> int | None: ...
    async def get_ci_failure_logs(
        self,
        pr_number: int,
        branch: str,
        max_log_chars: int = 4000,
        repo: str | None = None,
    ) -> dict[str, Any]: ...
    async def check_pr_ci(self, pr_number: int, repo: str | None = None) -> str: ...
    async def merge_pr(
        self, pr_number: int, task_id: int, title: str, repo: str | None = None
    ) -> bool: ...
    async def delete_branch(self, branch: str, repo: str | None = None) -> None: ...
    async def clone_repo(
        self, repo_url: str, workspace_path: str, base_branch: str = "develop"
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
