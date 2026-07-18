"""Advisory branch-stacking detection (#438).

At submit_for_review and in the review brief, the hub warns — never
blocks — when a task branch contains commits of ANOTHER unmerged task
branch in running/review status (incident #392: fixes #424→#426 were
stacked on the unmerged task-392 branch and nothing warned about it).
"""

from __future__ import annotations

from unittest.mock import patch

import aiosqlite
from httpx import AsyncClient

from hub import repository as repo
from hub import services
from hub.integrations.git_ops import GitOpsIntegration
from hub.integrations.noop import NoopGitOps
from hub.integrations.registry import plugins
from hub.models import TaskCreate, TaskSubmitReview


class FakeStackingGitOps(NoopGitOps):
    """Fake git_ops plugin: declares which (branch, other_branch) pairs stack."""

    def __init__(
        self, stacked_pairs: set[tuple[str, str]] | None = None, error: bool = False
    ):
        self.stacked_pairs = stacked_pairs or set()
        self.error = error
        self.calls: list[tuple[str, str, str, str | None]] = []

    async def branch_contains_unmerged_commits_of(
        self,
        branch: str,
        other_branch: str,
        base_branch: str = "develop",
        repo: str | None = None,
    ) -> bool:
        if self.error:
            raise RuntimeError("no repo access")
        self.calls.append((branch, other_branch, base_branch, repo))
        return (branch, other_branch) in self.stacked_pairs


async def _pair_running_task(db: aiosqlite.Connection, title: str) -> tuple[int, str]:
    """Create a pair-running task with a branch; returns (task_id, branch)."""
    tv = await services.create_task(db, TaskCreate(title=title))
    await repo.add_task_update(db, tv.id, "dev", "status", "Plan: do the work")
    await db.commit()
    started = await services.pair_start_task(db, tv.id, caller="dev-agent")
    assert started.status.value == "running"
    assert started.branch
    return tv.id, started.branch


async def _base_task_in_review(
    db: aiosqlite.Connection, branch: str, status: str = "review"
) -> int:
    """Another unmerged task whose branch could be stacked upon."""
    tv = await services.create_task(db, TaskCreate(title="Base task under review"))
    await repo.update_task(db, tv.id, status=status, branch=branch)
    await db.commit()
    return tv.id


async def test_submit_for_review_warns_on_stacked_branch(db: aiosqlite.Connection):
    task_id, branch = await _pair_running_task(db, "Stacked fix")
    base_id = await _base_task_in_review(db, "task-392/base-work")
    plugins.git_ops = FakeStackingGitOps({(branch, "task-392/base-work")})

    view = await services.submit_for_review(
        db, task_id, TaskSubmitReview(agent="dev-agent")
    )

    assert view.status.value == "review"
    assert view.lifecycle_hint is not None
    assert "ADVISORY branch stacking" in view.lifecycle_hint
    assert f"#{base_id}" in view.lifecycle_hint
    assert "task-392/base-work" in view.lifecycle_hint
    alerts = [u for u in view.updates or [] if u.kind == "alert"]
    assert len(alerts) == 1
    assert "ADVISORY branch stacking" in alerts[0].content
    assert f"#{base_id}" in alerts[0].content


async def test_submit_for_review_no_warning_when_independent(
    db: aiosqlite.Connection,
):
    task_id, _branch = await _pair_running_task(db, "Independent work")
    await _base_task_in_review(db, "task-392/base-work")
    fake = FakeStackingGitOps(stacked_pairs=set())
    plugins.git_ops = fake

    view = await services.submit_for_review(db, task_id)

    assert view.status.value == "review"
    assert len(fake.calls) == 1  # the check ran against the other branch
    assert "ADVISORY branch stacking" not in (view.lifecycle_hint or "")
    assert not [u for u in view.updates or [] if u.kind == "alert"]


async def test_submit_for_review_skips_silently_without_repo_access(
    db: aiosqlite.Connection,
):
    task_id, _branch = await _pair_running_task(db, "No repo access")
    await _base_task_in_review(db, "task-392/base-work")
    plugins.git_ops = FakeStackingGitOps(error=True)

    view = await services.submit_for_review(db, task_id)

    assert view.status.value == "review"
    assert "ADVISORY branch stacking" not in (view.lifecycle_hint or "")
    assert not [u for u in view.updates or [] if u.kind == "alert"]


async def test_submit_for_review_skips_when_plugin_lacks_method(
    db: aiosqlite.Connection,
):
    task_id, _branch = await _pair_running_task(db, "Legacy plugin")
    await _base_task_in_review(db, "task-392/base-work")

    class LegacyGitOps(NoopGitOps):
        branch_contains_unmerged_commits_of = None

    plugins.git_ops = LegacyGitOps()

    view = await services.submit_for_review(db, task_id)

    assert view.status.value == "review"
    assert not [u for u in view.updates or [] if u.kind == "alert"]


async def test_detect_branch_stacking_ignores_own_and_branchless_tasks(
    db: aiosqlite.Connection,
):
    task_id, branch = await _pair_running_task(db, "Self check")
    # A branchless running task and a task sharing the same branch name must
    # not be treated as stacking bases.
    other = await services.create_task(db, TaskCreate(title="Branchless"))
    await repo.update_task(db, other.id, status="running", branch=None)
    same = await services.create_task(db, TaskCreate(title="Same branch"))
    await repo.update_task(db, same.id, status="review", branch=branch)
    await db.commit()
    fake = FakeStackingGitOps(stacked_pairs=set())
    plugins.git_ops = fake

    result = await services.detect_branch_stacking(db, task_id, branch)

    assert result is None
    assert fake.calls == []


async def test_review_brief_includes_stacking_warning(client: AsyncClient, db):
    task_id, branch = await _pair_running_task(db, "Brief stacked task")
    base_id = await _base_task_in_review(db, "task-392/base-work")
    plugins.git_ops = FakeStackingGitOps({(branch, "task-392/base-work")})
    await services.submit_for_review(db, task_id)

    resp = await client.get(f"/api/tasks/{task_id}/review-brief")

    assert resp.status_code == 200
    brief = resp.json()
    assert "ADVISORY branch stacking" in brief["stacking_warning"]
    assert f"#{base_id}" in brief["stacking_warning"]


async def test_review_brief_stacking_warning_empty_without_repo_access(
    client: AsyncClient, db
):
    task_id, _branch = await _pair_running_task(db, "Brief no repo")
    await _base_task_in_review(db, "task-392/base-work")
    plugins.git_ops = FakeStackingGitOps(error=True)
    await services.submit_for_review(db, task_id)

    resp = await client.get(f"/api/tasks/{task_id}/review-brief")

    assert resp.status_code == 200
    assert resp.json()["stacking_warning"] == ""


# --- git_ops merge-base analysis (patched _git, no real repo) ---


def _shas() -> dict[str, str]:
    return {
        "task-424/fix^{commit}": "aaa111",
        "task-392/base^{commit}": "bbb222",
        "develop^{commit}": "ccc333",
    }


def _fake_git_factory(excluded_count: str, total_count: str = "3"):
    async def fake_git(*args, repo=None, check=True, **kw):
        if args[0] == "rev-parse" and "--verify" in args:
            sha = _shas().get(args[-1])
            return (0, sha, "") if sha else (1, "", "")
        if args[0] == "rev-list":
            # ("rev-list", "--count", other, "^base") → total unique commits;
            # the extra "^head" arg → commits of other NOT contained in head.
            return (0, total_count if len(args) == 4 else excluded_count, "")
        return (0, "", "")

    return fake_git


async def test_git_ops_detects_stacked_branch() -> None:
    # other branch has 3 unmerged commits; only 1 is missing from head →
    # head contains 2 of them → stacked.
    with patch("hub.integrations.git_ops._git", side_effect=_fake_git_factory("1")):
        stacked = await GitOpsIntegration().branch_contains_unmerged_commits_of(
            "task-424/fix", "task-392/base", base_branch="develop", repo="/tmp/repo"
        )
    assert stacked is True


async def test_git_ops_independent_branch_not_stacked() -> None:
    # all 3 unmerged commits of the other branch are absent from head.
    with patch("hub.integrations.git_ops._git", side_effect=_fake_git_factory("3")):
        stacked = await GitOpsIntegration().branch_contains_unmerged_commits_of(
            "task-424/fix", "task-392/base", base_branch="develop", repo="/tmp/repo"
        )
    assert stacked is False


async def test_git_ops_unresolvable_ref_returns_false() -> None:
    with patch("hub.integrations.git_ops._git", side_effect=_fake_git_factory("0")):
        stacked = await GitOpsIntegration().branch_contains_unmerged_commits_of(
            "task-424/fix", "task-999/gone", base_branch="develop", repo="/tmp/repo"
        )
    assert stacked is False


async def test_git_ops_merged_base_branch_not_stacked() -> None:
    # other branch fully merged into develop: zero unmerged commits.
    with patch(
        "hub.integrations.git_ops._git",
        side_effect=_fake_git_factory("0", total_count="0"),
    ):
        stacked = await GitOpsIntegration().branch_contains_unmerged_commits_of(
            "task-424/fix", "task-392/base", base_branch="develop", repo="/tmp/repo"
        )
    assert stacked is False
