"""Project catalog branch observations, refreshed outside the HTTP request."""

from __future__ import annotations

import asyncio
import logging
from time import monotonic
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from hub import git_policy
from hub.integrations.git_ops import origin_matches_project
from hub.models import CachedProjectView, CloneBranchObservation, CloneBranchState
from hub.services import project_policy

log = logging.getLogger(__name__)
_REFRESH_WORKERS = 3
_BACKLOG_LIMIT = 64
_OBSERVATION_TTL = git_policy.REMOTE_PRESENCE_TTL_SECONDS


@dataclass(frozen=True)
class _ProjectKey:
    project_id: int
    repo: str
    forge: str
    workspace: str
    branch: str

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> _ProjectKey:
        return cls(
            row["id"],
            (row["repo"] or "").strip(),
            project_policy.forge_of(row),
            (row["workspace_path"] or "").strip(),
            (row["default_branch"] or "").strip(),
        )

    @property
    def applicable(self) -> bool:
        return bool(self.workspace and self.branch)


@dataclass
class _Observation:
    state: CloneBranchState
    checked_at: datetime
    monotonic_at: float


@dataclass
class _Entry:
    key: _ProjectKey
    unknown: CloneBranchState
    last_attempt: float = float("-inf")
    observation: _Observation | None = None
    stale: bool = False
    generation: int = 0

    @classmethod
    def new(cls, key: _ProjectKey) -> _Entry:
        reason = "сверка ветки ещё не выполнялась"
        if not key.workspace:
            reason = "у проекта не задан workspace для сверки ветки"
        elif not key.branch:
            reason = "у проекта не объявлена ветка для сверки"
        return cls(key, CloneBranchState(reason=reason, project_branch=key.branch))

    def as_view(self, row: dict[str, Any], now: float) -> CachedProjectView:
        observation = self.observation
        if observation is None:
            return CachedProjectView(
                **row,
                clone_branch=self.unknown,
                clone_branch_observation=CloneBranchObservation(
                    status="unknown", checked_at=None
                ),
            )
        outdated = self.stale or now - observation.monotonic_at >= _OBSERVATION_TTL
        return CachedProjectView(
            **row,
            clone_branch=observation.state,
            clone_branch_observation=CloneBranchObservation(
                status="stale" if outdated else "fresh",
                checked_at=observation.checked_at,
            ),
        )


class ProjectCatalog:
    """Own the snapshots and a bounded work queue for one running Hub app.

    Entries and the queue live on the app loop. Git commands run off the loop,
    and observations never participate in project selection or delivery gates.
    """

    def __init__(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._entries: dict[int, _Entry] = {}
        self._recent_attempts: dict[_ProjectKey, float] = {}
        self._pending: set[_ProjectKey] = set()
        self._queue: asyncio.Queue[_Entry] = asyncio.Queue(maxsize=_BACKLOG_LIMIT)
        self._closed = False
        self._unwatch = git_policy.watch_remote_presence_forget(
            self._presence_forgotten
        )
        self._workers = [
            asyncio.create_task(self._run(), name=f"project-catalog-{index}")
            for index in range(_REFRESH_WORKERS)
        ]

    def list_views(self, rows) -> list[CachedProjectView]:
        now = monotonic()
        # Keep recent attempts even if a project temporarily changes its key.
        self._recent_attempts = {
            key: attempted
            for key, attempted in self._recent_attempts.items()
            if now - attempted < _OBSERVATION_TTL
        }
        views = []
        due = []
        for raw in rows:
            row = dict(raw)
            entry = self._entry_for(_ProjectKey.from_row(row))
            views.append(entry.as_view(row, now))
            if self._refresh_due(entry):
                due.append(entry)
        self._enqueue(due)
        return views

    def _entry_for(self, key: _ProjectKey) -> _Entry:
        entry = self._entries.get(key.project_id)
        if entry is None or entry.key != key:
            entry = self._entries[key.project_id] = _Entry.new(key)
        return entry

    def _refresh_due(self, entry: _Entry) -> bool:
        return (
            entry.key.applicable
            and entry.key not in self._pending
            and entry.key not in self._recent_attempts
        )

    def _enqueue(self, entries: list[_Entry]) -> None:
        if self._closed:
            return
        # When the queue overflows, projects skipped at the end of the catalog
        # get a turn on the next read, ahead of those we already attempted.
        for entry in sorted(entries, key=lambda item: item.last_attempt):
            if self._queue.full():
                break
            self._queue.put_nowait(entry)
            self._pending.add(entry.key)

    def _presence_forgotten(self, workspace: str, branch: str) -> None:
        with suppress(RuntimeError):
            if asyncio.get_running_loop() is self._loop:
                self._invalidate(workspace, branch)
                return
        with suppress(RuntimeError):  # The loop may have shut down on this thread.
            self._loop.call_soon_threadsafe(self._invalidate, workspace, branch)

    def _invalidate(self, workspace: str, branch: str) -> None:
        if self._closed:
            return
        self._recent_attempts = {
            key: attempted
            for key, attempted in self._recent_attempts.items()
            if not self._matches(key, workspace, branch)
        }
        for entry in self._entries.values():
            if self._matches(entry.key, workspace, branch):
                entry.stale = True
                entry.last_attempt = float("-inf")
                entry.generation += 1

    @staticmethod
    def _matches(key: _ProjectKey, workspace: str, branch: str) -> bool:
        if not workspace:
            return True
        return key.workspace == workspace and (not branch or key.branch == branch)

    async def _run(self) -> None:
        while True:
            entry = await self._queue.get()
            try:
                if self._entries.get(entry.key.project_id) is entry:
                    await self._refresh(entry)
            finally:
                self._pending.discard(entry.key)
                self._queue.task_done()

    async def _refresh(self, entry: _Entry) -> None:
        key = entry.key
        generation = entry.generation
        entry.last_attempt = self._recent_attempts[key] = monotonic()
        outcome = "cancelled"
        log.info("project_catalog_refresh project_id=%s phase=start", key.project_id)
        try:
            state = await self._observe(key)
            if self._closed or entry is not self._entries.get(key.project_id):
                outcome = "superseded"
            elif generation != entry.generation:
                outcome = "invalidated"
            else:
                outcome = self._save(entry, state)
        finally:
            log.info(
                "project_catalog_refresh project_id=%s phase=end outcome=%s",
                key.project_id,
                outcome,
            )

    @staticmethod
    async def _observe(
        key: _ProjectKey,
    ) -> git_policy.BranchSyncState | git_policy.ConfirmedBranchObservation:
        try:
            return await git_policy.observe_clone_branch(
                key.workspace,
                key.branch,
                accept_origin=lambda origin: origin_matches_project(
                    origin, key.repo, key.forge
                ),
            )
        except Exception:  # noqa: BLE001 - one failed probe must not stop the queue
            return git_policy.BranchSyncState(
                reason="ошибка фоновой сверки ветки", project_branch=key.branch
            )

    @staticmethod
    def _save(
        entry: _Entry,
        result: git_policy.BranchSyncState | git_policy.ConfirmedBranchObservation,
    ) -> str:
        if isinstance(result, git_policy.ConfirmedBranchObservation):
            state = CloneBranchState.model_validate(result.state, from_attributes=True)
            entry.observation = _Observation(
                state, result.checked_at, result.monotonic_at
            )
            entry.stale = False
            return "confirmed"
        entry.unknown = CloneBranchState.model_validate(result, from_attributes=True)
        entry.stale = True
        return "unknown"

    async def stop(self) -> None:
        self._closed = True
        self._unwatch()
        while not self._queue.empty():
            entry = self._queue.get_nowait()
            self._pending.discard(entry.key)
            self._queue.task_done()
        for worker in self._workers:
            worker.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
