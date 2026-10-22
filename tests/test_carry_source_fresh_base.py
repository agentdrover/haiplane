"""Перенос отчёта ревью сверяет диффы на свежей базе (#1990, продолжение #1361/#1983).

Пересдача, где слита только база, при устаревшем ``origin/<base>`` давала
разные диффы (чужие коммиты базы выглядели правкой автора) -> отчёт не
переносился -> повторное платное ревью неизменной работы.

Двойник отвечает на ``branch_diff`` РАЗНО до и после ``fetch_base``: тест с
одинаковыми ответами проходил бы и на сломанном коде.
"""

from __future__ import annotations

import json
import logging
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from hub import repository as repo
from hub import services
from hub.integrations.noop import NoopGitOps
from hub.integrations.registry import plugins
from hub.models import TaskCreate
from hub.services import carry_fresh_base, orchestration
from hub.services import review_dispatch as dispatcher

SOURCE = "a" * 40  # коммит, который читал ревьюер (поколение 1)
TIP = "b" * 40  # пересдача (поколение 2)
BRANCH = "task-1990/x"

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
        self.fetches = 0
        self.base_fetched = False
        self.diff_calls: list[tuple[str, bool]] = []

    async def fetch_base(self, repo: str, base: str):
        self.fetches += 1
        if self.raises:
            raise RuntimeError("network down")
        if not self.base_ok:
            return False, "fatal: unable to access"
        self.base_fetched = True
        return True, ""

    async def branch_diff(self, repo: str, base: str, branch: str, context: int = 0):
        self.diff_calls.append((branch, self.base_fetched))
        if branch == SOURCE:
            return AUTHOR
        if not self.base_fetched:
            return AUTHOR + FROM_BASE  # старая база: чужое выглядит правкой
        return AUTHOR_EDIT if self.edit else AUTHOR


@pytest.fixture(autouse=True)
def _forget_refreshes():
    carry_fresh_base._refreshed.clear()
    yield
    carry_fresh_base._refreshed.clear()


async def _resubmitted(
    db: aiosqlite.Connection, *, incomplete: int = 0, source_sha: str = SOURCE
) -> dict:
    """Задача на поколении 2 с закреплённой TIP и отчётом поколения 1."""
    tv = await services.create_task(db, TaskCreate(title="Merge-only resubmission"))
    await repo.update_task(
        db, tv.id, branch=BRANCH, submission_sha=TIP, submission_generation=2
    )
    for generation, sha in ((1, source_sha), (2, TIP)):
        await db.execute(
            "INSERT INTO submissions (task_id, generation, sha, submitted_at) "
            "VALUES (?, ?, ?, datetime('now', '-10 minutes'))",
            (tv.id, generation, sha),
        )
    await db.execute(
        "INSERT INTO machine_reviews (task_id, submission_generation, model, "
        "submitted_by, raw_count, findings_confirmed, incomplete) "
        "VALUES (?, 1, 'gpt-5.2', 'rev-agent', 2, ?, ?)",
        (tv.id, json.dumps([{"finding_uid": "u1", "title": "t"}]), incomplete),
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


async def _carried_count(db: aiosqlite.Connection, task_id: int) -> int:
    rows = await db.execute_fetchall(
        "SELECT id FROM machine_reviews WHERE task_id=? AND submission_generation=2 "
        "AND carried_from_review_id IS NOT NULL",
        (task_id,),
    )
    return len(rows)


async def test_a_base_only_merge_carries_the_report(db, monkeypatch):
    git = StaleBaseGit()
    _install(monkeypatch, git)
    task = await _resubmitted(db)

    assert await dispatcher._carry_the_report_over(db, task) is True

    assert git.base_fetched
    assert git.diff_calls and all(fetched for _, fetched in git.diff_calls), (
        "оба диффа сняты уже после fetch базы"
    )
    assert await _carried_count(db, task["id"]) == 1


async def test_an_author_edit_after_the_fetch_is_not_carried(db, monkeypatch):
    git = StaleBaseGit(edit=True)
    _install(monkeypatch, git)
    task = await _resubmitted(db)

    assert await dispatcher._carry_the_report_over(db, task) is False

    assert git.base_fetched, "правка обнаружена на свежей базе, а не на старой"
    assert await _carried_count(db, task["id"]) == 0


async def test_a_failed_fetch_means_no_carry_with_a_reason(db, monkeypatch, caplog):
    for git in (StaleBaseGit(base_ok=False), StaleBaseGit(raises=True)):
        _install(monkeypatch, git)
        task = await _resubmitted(db)
        caplog.clear()

        with caplog.at_level(logging.WARNING):
            assert await dispatcher._carry_the_report_over(db, task) is False

        assert git.diff_calls == [], "без свежей базы дифф не сравнивают"
        assert await _carried_count(db, task["id"]) == 0
        assert orchestration.BASE_FETCH_PENDING_PREFIX in caplog.text


async def test_incomplete_report_is_still_excluded_and_costs_no_fetch(db, monkeypatch):
    git = StaleBaseGit()
    _install(monkeypatch, git)
    task = await _resubmitted(db, incomplete=1)

    assert await dispatcher._carry_the_report_over(db, task) is False
    assert git.fetches == 0, "нет переносимого отчёта — базу не трогаем"


async def test_the_reader_does_not_write_and_fetches_once(db, monkeypatch):
    git = StaleBaseGit()
    _install(monkeypatch, git)
    task = await _resubmitted(db)

    found = await dispatcher._carry_source(db, task)

    assert found is not None
    assert git.fetches == 1
    assert await _carried_count(db, task["id"]) == 0


async def test_same_commit_needs_no_fetch(db, monkeypatch):
    git = StaleBaseGit()
    _install(monkeypatch, git)
    task = await _resubmitted(db, source_sha=TIP)

    assert await dispatcher._carry_source(db, task) is not None
    assert git.fetches == 0


async def test_reader_then_dispatcher_share_one_fetch(db, monkeypatch):
    # Читатель стюарда (read_only) и диспетчер спрашивают один и тот же
    # вопрос в одном цикле: база обновляется один раз, а не по разу на каждого.
    git = StaleBaseGit()
    _install(monkeypatch, git)
    task = await _resubmitted(db)

    assert await dispatcher._carry_source(db, task) is not None
    assert await dispatcher._carry_source(db, task) is not None
    assert await dispatcher._carry_the_report_over(db, task) is True
    assert git.fetches == 1


async def test_the_shared_fetch_expires(db, monkeypatch):
    git = StaleBaseGit()
    _install(monkeypatch, git)
    task = await _resubmitted(db)
    now = [1000.0]
    monkeypatch.setattr(carry_fresh_base, "_clock", lambda: now[0])

    await dispatcher._carry_source(db, task)
    now[0] += carry_fresh_base.FRESH_BASE_TTL_SECONDS + 1
    await dispatcher._carry_source(db, task)

    assert git.fetches == 2


async def test_a_failed_fetch_is_not_remembered(db, monkeypatch):
    git = StaleBaseGit(base_ok=False)
    _install(monkeypatch, git)
    task = await _resubmitted(db)

    assert await dispatcher._carry_source(db, task) is None
    git.base_ok = True
    assert await dispatcher._carry_source(db, task) is not None
    assert git.fetches == 2
