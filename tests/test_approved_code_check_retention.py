"""Сверка одобрения обновляет базу сама, а не по событиям гейта (#1983).

Находка ревью #1956: ``_gate_update_base`` ходил за свежей базой только если в
events нашёлся ``gate_branch_update`` (retention 14 дней). После очистки
событий и рестарта клон держал старую ``origin/<base>``, и дифф «ветка к
базе» на новой вершине нёс чужие коммиты базы как правку автора -> ложный
``stale_approval``.

Двойник ниже отвечает на ``branch_diff`` РАЗНО до и после ``fetch_base`` базы:
тест с одинаковыми ответами проходил бы и на сломанном коде.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import aiosqlite

from hub import repository as repo
from hub import services
from hub.integrations.noop import NoopGitOps
from hub.integrations.registry import plugins
from hub.models import TaskCreate
from hub.services import orchestration

PINNED = "a" * 40
TIP = "b" * 40
BRANCH = "task-1983/x"

AUTHOR = (
    "diff --git a/hub/x.py b/hub/x.py\n--- a/hub/x.py\n+++ b/hub/x.py\n"
    "@@ -1,0 +2 @@\n+author = 1\n"
)
FROM_BASE = (
    "diff --git a/hub/y.py b/hub/y.py\n--- a/hub/y.py\n+++ b/hub/y.py\n"
    "@@ -1,0 +2 @@\n+brought_by_base = 1\n"
)
AUTHOR_EDIT = (
    "diff --git a/hub/x.py b/hub/x.py\n--- a/hub/x.py\n+++ b/hub/x.py\n"
    "@@ -1,0 +2,2 @@\n+author = 1\n+author_again = 2\n"
)


class StaleBaseGit(NoopGitOps):
    """Клон с устаревшей базой: свежесть наступает только после fetch базы."""

    def __init__(self, *, base_ok: bool = True, raises: bool = False, edit=False):
        super().__init__()
        self.base_ok = base_ok
        self.raises = raises
        self.edit = edit
        self.base_fetched = False
        self.diff_calls: list[tuple[str, bool]] = []

    async def fetch_base(self, repo: str, base: str):
        if base != "develop":
            return True, ""  # ветка задачи для resolve_branch_tip
        if self.raises:
            raise RuntimeError("network down")
        if not self.base_ok:
            return False, "fatal: unable to access"
        self.base_fetched = True
        return True, ""

    async def head_sha(self, repo: str, ref: str):
        return TIP

    async def branch_diff(self, repo: str, base: str, branch: str, context: int = 0):
        self.diff_calls.append((branch, self.base_fetched))
        if branch == PINNED:
            return AUTHOR
        if not self.base_fetched:
            return AUTHOR + FROM_BASE  # старая база: чужое выглядит правкой
        return AUTHOR_EDIT if self.edit else AUTHOR


async def _task(db: aiosqlite.Connection) -> dict:
    tv = await services.create_task(db, TaskCreate(title="Approved, then base moved"))
    await repo.update_task(
        db, tv.id, branch=BRANCH, submission_sha=PINNED, pr_number=77
    )
    await db.commit()
    return dict(await repo.get_task(db, tv.id))


def _install(monkeypatch, git: StaleBaseGit) -> None:
    plugins.git_ops = git
    monkeypatch.setattr(
        orchestration,
        "project_git_context",
        AsyncMock(return_value={"repo": "/srv/ws", "base_branch": "develop"}),
    )


async def test_base_is_fetched_before_the_diff_comparison(db, monkeypatch):
    # Событий gate_branch_update нет вовсе (retention) — база всё равно свежая.
    git = StaleBaseGit()
    _install(monkeypatch, git)
    task = await _task(db)

    refusal, note = await orchestration._approved_code_check(db, task, 77)

    assert refusal == ""
    assert "Мерж базы в ветку" in note
    assert git.base_fetched
    assert git.diff_calls and all(fetched for _, fetched in git.diff_calls)


async def test_author_edit_after_the_fetch_is_still_stale_approval(db, monkeypatch):
    git = StaleBaseGit(edit=True)
    _install(monkeypatch, git)
    task = await _task(db)

    refusal, _ = await orchestration._approved_code_check(db, task, 77)

    assert refusal.startswith("stale_approval")
    assert git.base_fetched


async def test_failed_base_fetch_is_a_temporary_wait_not_a_verdict(db, monkeypatch):
    for git in (StaleBaseGit(base_ok=False), StaleBaseGit(raises=True)):
        _install(monkeypatch, git)
        task = await _task(db)

        refusal, note = await orchestration._approved_code_check(db, task, 77)

        assert refusal.startswith(orchestration.BASE_FETCH_PENDING_PREFIX)
        assert "stale_approval" not in refusal
        assert note == ""
        assert git.diff_calls == [], "без свежей базы дифф не сравнивают"


async def test_no_base_fetch_when_the_tip_is_the_approved_commit(db, monkeypatch):
    git = StaleBaseGit()
    git.head_sha = AsyncMock(return_value=PINNED)
    _install(monkeypatch, git)
    task = await _task(db)

    assert await orchestration._approved_code_check(db, task, 77) == ("", "")
    assert not git.base_fetched
    assert git.diff_calls == []
