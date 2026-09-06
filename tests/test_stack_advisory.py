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
    """Fake git_ops plugin declaring the branch GRAPH, not the two answers.

    ``stacked_pairs`` holds ordered ``(descendant, ancestor)`` pairs — "this
    branch is built on top of that one" — and ``shared_pairs`` holds branches
    that carry each other's unmerged commits without any ancestry between
    them. Both hub questions are derived from that one declaration on
    purpose (#1184): a test that could set "stacked: yes" and "head is the
    descendant" independently would be free to declare a graph git cannot
    produce, and answers that disagree with each other are the whole defect.
    """

    def __init__(
        self,
        stacked_pairs: set[tuple[str, str]] | None = None,
        error: bool = False,
        shared_pairs: set[tuple[str, str]] | None = None,
        ancestry_error: bool = False,
    ):
        self.stacked_pairs = stacked_pairs or set()
        self.shared_pairs = shared_pairs or set()
        self.error = error
        self.ancestry_error = ancestry_error
        self.calls: list[tuple[str, str, str, str | None]] = []
        self.ancestry_calls: list[tuple[str, str]] = []

    def _related(self, a: str, b: str) -> bool:
        """Sharing unmerged commits is symmetric — as it is in real git."""
        return (
            (a, b) in self.stacked_pairs
            or (b, a) in self.stacked_pairs
            or (a, b) in self.shared_pairs
            or (b, a) in self.shared_pairs
        )

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
        return self._related(branch, other_branch)

    async def branch_ancestry(
        self,
        branch: str,
        other_branch: str,
        repo: str | None = None,
    ) -> str:
        if self.ancestry_error:
            raise RuntimeError("branch missing from the clone")
        self.ancestry_calls.append((branch, other_branch))
        if (branch, other_branch) in self.stacked_pairs:
            return "head_is_descendant"
        if (other_branch, branch) in self.stacked_pairs:
            return "head_is_ancestor"
        if self._related(branch, other_branch):
            return "unrelated"
        return "unknown"


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
    # Filter by content, not by count: submit_for_review also reports whether
    # the declared-areas check could run (#550), and this test is about the
    # stacking advisory alone.
    alerts = [
        u
        for u in view.updates or []
        if u.kind == "alert" and "ADVISORY branch stacking" in u.content
    ]
    assert len(alerts) == 1
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
    assert not [
        u
        for u in view.updates or []
        if u.kind == "alert" and "ADVISORY branch stacking" in u.content
    ]


async def test_submit_for_review_skips_silently_without_repo_access(
    db: aiosqlite.Connection,
):
    task_id, _branch = await _pair_running_task(db, "No repo access")
    await _base_task_in_review(db, "task-392/base-work")
    plugins.git_ops = FakeStackingGitOps(error=True)

    view = await services.submit_for_review(db, task_id)

    assert view.status.value == "review"
    assert "ADVISORY branch stacking" not in (view.lifecycle_hint or "")
    assert not [
        u
        for u in view.updates or []
        if u.kind == "alert" and "ADVISORY branch stacking" in u.content
    ]


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
    assert not [
        u
        for u in view.updates or []
        if u.kind == "alert" and "ADVISORY branch stacking" in u.content
    ]


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


# --- which side merges first (#1184) ---
#
# Detecting the stack was never the problem: the predicate under it is an
# INTERSECTION check, true for a stacked pair read in either direction. The
# message, though, always cast the submitting branch as the descendant, so
# the same pair got opposite merge orders depending on who submitted — first
# observed on spike-bo #1175/#1183 when both branches sat in review at once.


async def _stacking_hint(view) -> str:
    """The stacking advisory out of a submission's lifecycle hint."""
    return view.lifecycle_hint or ""


async def test_stack_advisory_names_the_same_order_from_both_sides(
    db: aiosqlite.Connection,
):
    parent_id, parent_branch = await _pair_running_task(db, "Parent work")
    child_id, child_branch = await _pair_running_task(db, "Child on top")
    # The graph: child = parent + one commit.
    plugins.git_ops = FakeStackingGitOps({(child_branch, parent_branch)})

    child_view = await services.submit_for_review(
        db, child_id, TaskSubmitReview(agent="dev-agent")
    )
    # Now both branches sit in review — the state that flipped the advice.
    parent_view = await services.submit_for_review(
        db, parent_id, TaskSubmitReview(agent="dev-agent")
    )

    child_hint = await _stacking_hint(child_view)
    parent_hint = await _stacking_hint(parent_view)
    # Same order, named from either side: the parent merges first.
    assert f"merge task #{parent_id}'s branch first" in child_hint
    assert f"'{parent_branch}' merges into" in parent_hint
    assert "FIRST" in parent_hint
    assert f"merge task #{child_id}'s branch first" not in parent_hint


async def test_parent_branch_is_told_it_merges_first(db: aiosqlite.Connection):
    parent_id, parent_branch = await _pair_running_task(db, "Parent submits")
    child_branch = "task-1183/child-on-top"
    child_id = await _base_task_in_review(db, child_branch)
    plugins.git_ops = FakeStackingGitOps({(child_branch, parent_branch)})

    view = await services.submit_for_review(
        db, parent_id, TaskSubmitReview(agent="dev-agent")
    )

    hint = await _stacking_hint(view)
    assert f"#{child_id}" in hint
    assert "is built ON TOP of" in hint
    assert f"'{parent_branch}' merges into" in hint and "FIRST" in hint
    # The old text asserted the reverse of the truth about this very branch.
    assert f"'{parent_branch}' contains unmerged commits" not in hint
    assert f"merge task #{child_id}'s branch first" not in hint


async def test_shared_commits_without_ancestry_are_named_not_sided(
    db: aiosqlite.Connection,
):
    task_id, branch = await _pair_running_task(db, "Fork of a fork")
    other_branch = "task-392/sibling-work"
    other_id = await _base_task_in_review(db, other_branch)
    plugins.git_ops = FakeStackingGitOps(shared_pairs={(branch, other_branch)})

    view = await services.submit_for_review(
        db, task_id, TaskSubmitReview(agent="dev-agent")
    )

    hint = await _stacking_hint(view)
    assert "ADVISORY branch stacking" in hint
    assert f"#{other_id}" in hint
    assert "neither branch is an ancestor of the other" in hint
    # A third outcome, not a coin toss between the first two.
    assert f"merge task #{other_id}'s branch first" not in hint
    assert "FIRST" not in hint


async def test_unresolvable_ancestry_does_not_pick_a_side(db: aiosqlite.Connection):
    task_id, branch = await _pair_running_task(db, "Ancestry unavailable")
    other_branch = "task-392/base-work"
    other_id = await _base_task_in_review(db, other_branch)
    # The stack is detected, but git cannot answer about ancestry — the
    # branch is missing from the hub's clone, or the query failed.
    plugins.git_ops = FakeStackingGitOps({(branch, other_branch)}, ancestry_error=True)

    view = await services.submit_for_review(
        db, task_id, TaskSubmitReview(agent="dev-agent")
    )

    hint = await _stacking_hint(view)
    assert "ADVISORY branch stacking" in hint
    assert "could NOT be determined" in hint
    # "I could not check" must not decay into the guess it replaced.
    assert f"merge task #{other_id}'s branch first" not in hint
    assert "FIRST" not in hint


async def test_legacy_plugin_without_ancestry_gets_the_undetermined_wording(
    db: aiosqlite.Connection,
):
    """A plugin that predates branch_ancestry still gets an honest advisory."""
    task_id, branch = await _pair_running_task(db, "Legacy ancestry")
    other_branch = "task-392/base-work"
    await _base_task_in_review(db, other_branch)
    fake = FakeStackingGitOps({(branch, other_branch)})
    fake.branch_ancestry = None
    plugins.git_ops = fake

    view = await services.submit_for_review(
        db, task_id, TaskSubmitReview(agent="dev-agent")
    )

    assert "could NOT be determined" in await _stacking_hint(view)


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


# --- ancestry between two branches (patched _git, no real repo) ---


def _ancestry_git_factory(is_ancestor: dict[tuple[str, str], int]):
    """Fake _git answering rev-parse and merge-base --is-ancestor.

    ``is_ancestor`` maps (maybe_ancestor_sha, descendant_sha) to git's exit
    code: 0 yes, 1 no, anything else a failure. Missing pairs answer 1.
    """

    async def fake_git(*args, repo=None, check=True, **kw):
        if args[0] == "rev-parse" and "--verify" in args:
            sha = _shas().get(args[-1])
            return (0, sha, "") if sha else (1, "", "")
        if args[0] == "merge-base" and "--is-ancestor" in args:
            return (is_ancestor.get((args[-2], args[-1]), 1), "", "")
        return (0, "", "")

    return fake_git


async def _ancestry(is_ancestor: dict[tuple[str, str], int]) -> str:
    with patch(
        "hub.integrations.git_ops._git",
        side_effect=_ancestry_git_factory(is_ancestor),
    ):
        return await GitOpsIntegration().branch_ancestry(
            "task-424/fix", "task-392/base", repo="/tmp/repo"
        )


async def test_branch_ancestry_head_is_descendant() -> None:
    # base is an ancestor of fix → fix is built on top of base.
    assert await _ancestry({("bbb222", "aaa111"): 0}) == "head_is_descendant"


async def test_branch_ancestry_head_is_ancestor() -> None:
    # fix is an ancestor of base → the OTHER branch is the one on top, and
    # the advisory must not tell fix to merge base first (#1184).
    assert await _ancestry({("aaa111", "bbb222"): 0}) == "head_is_ancestor"


async def test_branch_ancestry_unrelated_branches() -> None:
    # Shared history, but neither tip reaches the other.
    assert await _ancestry({}) == "unrelated"


async def test_branch_ancestry_same_tip_names_no_side() -> None:
    # Two names on one commit: both answers are yes and neither is an order.
    assert (
        await _ancestry({("aaa111", "bbb222"): 0, ("bbb222", "aaa111"): 0})
        == "unrelated"
    )


async def test_branch_ancestry_git_failure_is_unknown() -> None:
    # Exit code 128 is "could not check", not "not an ancestor".
    assert await _ancestry({("bbb222", "aaa111"): 128}) == "unknown"


async def test_branch_ancestry_unresolvable_ref_is_unknown() -> None:
    with patch("hub.integrations.git_ops._git", side_effect=_ancestry_git_factory({})):
        relation = await GitOpsIntegration().branch_ancestry(
            "task-424/fix", "task-999/gone", repo="/tmp/repo"
        )
    assert relation == "unknown"
