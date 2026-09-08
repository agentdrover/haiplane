"""Advisory branch-stacking detection (#438).

At submit_for_review and in the review brief, the hub warns — never
blocks — when a task branch contains commits of ANOTHER unmerged task
branch in running/review status (incident #392: fixes #424→#426 were
stacked on the unmerged task-392 branch and nothing warned about it).
"""

from __future__ import annotations

import pytest
from unittest.mock import patch

import aiosqlite
from httpx import AsyncClient

from hub import repository as repo
from hub import services
from hub.integrations.git_ops import GitOpsIntegration
from hub.integrations.noop import NoopGitOps
from hub.integrations.protocols import StackProbeOutcome
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
        same_tip_pairs: set[tuple[str, str]] | None = None,
    ):
        self.stacked_pairs = stacked_pairs or set()
        self.shared_pairs = shared_pairs or set()
        self.same_tip_pairs = same_tip_pairs or set()
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
            or (a, b) in self.same_tip_pairs
            or (b, a) in self.same_tip_pairs
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
        if (
            branch,
            other_branch,
        ) in self.same_tip_pairs or (
            other_branch,
            branch,
        ) in self.same_tip_pairs:
            return "same_tip"
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


async def test_same_tip_is_named_as_the_same_commit(db: aiosqlite.Connection):
    """Two names on one commit is not a diamond, and must not read as one.

    A branch cut from another task's branch carries its commits and has none
    of its own yet: the tips are equal, so is_ancestor answers yes BOTH ways.
    The honest word for that is "the same commit" — saying "neither is an
    ancestor of the other" states the reverse of the fact and sends the
    reader to check by hand what the hub already computed (#1193).
    """
    task_id, branch = await _pair_running_task(db, "Branch cut from a branch")
    other_branch = "task-392/the-same-commit"
    other_id = await _base_task_in_review(db, other_branch)
    plugins.git_ops = FakeStackingGitOps(same_tip_pairs={(branch, other_branch)})

    view = await services.submit_for_review(
        db, task_id, TaskSubmitReview(agent="dev-agent")
    )

    hint = await _stacking_hint(view)
    assert "ADVISORY branch stacking" in hint
    assert f"#{other_id}" in hint
    assert "point at the SAME commit" in hint
    # The sentence this task exists to remove.
    assert "neither branch is an ancestor of the other" not in hint


async def test_same_tip_names_no_merge_order(db: aiosqlite.Connection):
    """The wording changes; the behaviour does not.

    Naming a side here would be worse than the false explanation it replaces:
    there is no order between one commit and itself.
    """
    task_id, branch = await _pair_running_task(db, "Same tip, no order")
    other_branch = "task-392/twin"
    other_id = await _base_task_in_review(db, other_branch)
    plugins.git_ops = FakeStackingGitOps(same_tip_pairs={(branch, other_branch)})

    view = await services.submit_for_review(
        db, task_id, TaskSubmitReview(agent="dev-agent")
    )

    hint = await _stacking_hint(view)
    assert f"#{other_id}" in hint
    assert f"'{branch}' contains unmerged commits" not in hint
    # Case-insensitive on purpose: an earlier version of this test looked for
    # the lowercase sentence only, and a mutation that named a side with a
    # capital M walked straight past it. "first" appears nowhere in an honest
    # same-tip advisory, so its absence is the whole property.
    assert "first" not in hint.lower()
    # Without this the test passes on an unhandled outcome too: "order could
    # not be determined" also names no side, and a guard that green-lights
    # the bug it guards against is not a guard.
    assert "could NOT be determined" not in hint
    assert "point at the SAME commit" in hint


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


# --- #1186: the probe's OWN outcomes, on the real git body ---
#
# Найдено измерением test-adequacy при машинном ревью #1186: тесты гейта
# доставки скриптуют branch_stacking_probe целиком через AsyncMock, поэтому
# настоящее тело GitOpsIntegration.branch_stacking_probe в них не исполняется
# ни разу. А ведь именно оно решает, будет ли исход unavailable (гейт держит
# и спрашивает снова) или clear (гейт мержит) — то есть ошибка ровно здесь
# вернула бы молчаливый мерж поверх несмерженной ветки, ради запрета которого
# заведена #1186. Тесты ниже гоняют это тело с подменённым _git, как уже
# сделано выше для bool-предиката.


async def test_probe_names_a_stack_rather_than_just_asserting_one() -> None:
    with patch("hub.integrations.git_ops._git", side_effect=_fake_git_factory("1")):
        probe = await GitOpsIntegration().branch_stacking_probe(
            "task-424/fix", "task-392/base", base_branch="develop", repo="/tmp/repo"
        )
    assert probe.outcome is StackProbeOutcome.stacked
    assert "2 of 3" in (probe.details or ""), "сколько коммитов общих — часть ответа"


async def test_probe_says_clear_only_after_actually_looking() -> None:
    with patch("hub.integrations.git_ops._git", side_effect=_fake_git_factory("3")):
        probe = await GitOpsIntegration().branch_stacking_probe(
            "task-424/fix", "task-392/base", base_branch="develop", repo="/tmp/repo"
        )
    assert probe.outcome is StackProbeOutcome.clear


async def test_probe_reports_an_unresolvable_ref_as_unavailable_not_clear() -> None:
    # Ветки нет в клоне. До #1186 это давало ровно тот же False, что и
    # «проверил, независимы», и гейт мержил бы. Теперь исход другой И несёт
    # имя ветки, которую не удалось разрешить.
    with patch("hub.integrations.git_ops._git", side_effect=_fake_git_factory("0")):
        probe = await GitOpsIntegration().branch_stacking_probe(
            "task-424/fix", "task-999/gone", base_branch="develop", repo="/tmp/repo"
        )
    assert probe.outcome is StackProbeOutcome.unavailable
    assert probe.reason == "ref_unresolved"
    assert "task-999/gone" in (probe.details or "")


async def test_probe_reports_a_failed_rev_list_as_unavailable_not_clear() -> None:
    # git ответил ненулевым кодом. Тоже не «стопки нет».
    async def failing_git(*args, repo=None, check=True, **kw):
        if args[0] == "rev-parse" and "--verify" in args:
            sha = _shas().get(args[-1])
            return (0, sha, "") if sha else (1, "", "")
        if args[0] == "rev-list":
            return (128, "", "fatal: bad revision")
        return (0, "", "")

    with patch("hub.integrations.git_ops._git", side_effect=failing_git):
        probe = await GitOpsIntegration().branch_stacking_probe(
            "task-424/fix", "task-392/base", base_branch="develop", repo="/tmp/repo"
        )
    assert probe.outcome is StackProbeOutcome.unavailable
    assert probe.reason == "rev_list_failed"


async def test_probe_reports_unreadable_counts_as_unavailable_not_clear() -> None:
    # rev-list вернул ноль, но не число. Ни «стопка», ни «независимы».
    with patch(
        "hub.integrations.git_ops._git",
        side_effect=_fake_git_factory("не число", total_count="тоже не число"),
    ):
        probe = await GitOpsIntegration().branch_stacking_probe(
            "task-424/fix", "task-392/base", base_branch="develop", repo="/tmp/repo"
        )
    assert probe.outcome is StackProbeOutcome.unavailable
    assert probe.reason == "rev_list_unparseable"


# --- ancestry between two branches (patched _git, no real repo) ---


def _ancestry_git_factory(
    is_ancestor: dict[tuple[str, str], int],
    missing_commits: frozenset[str] = frozenset(),
):
    """Fake _git answering rev-parse and merge-base --is-ancestor.

    ``is_ancestor`` maps (maybe_ancestor_sha, descendant_sha) to git's exit
    code: 0 yes, 1 no. Missing pairs answer 1. ``missing_commits`` makes
    ``cat-file -e`` fail — the clone does not carry that commit, which is how
    "git cannot answer" actually reaches this code (#497 guards).
    """

    async def fake_git(*args, repo=None, check=True, **kw):
        if args[0] == "rev-parse" and "--verify" in args:
            sha = _shas().get(args[-1])
            return (0, sha, "") if sha else (1, "", "")
        if args[0] == "cat-file":
            return (1 if args[-1].split("^")[0] in missing_commits else 0, "", "")
        if args[0] == "merge-base" and "--is-ancestor" in args:
            return (is_ancestor.get((args[-2], args[-1]), 1), "", "")
        return (0, "", "")

    return fake_git


async def _ancestry(
    is_ancestor: dict[tuple[str, str], int],
    missing_commits: frozenset[str] = frozenset(),
) -> str:
    with patch(
        "hub.integrations.git_ops._git",
        side_effect=_ancestry_git_factory(is_ancestor, missing_commits),
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
    # The name still holds — no side is named — but the outcome is its own
    # (#1193): folding it into "unrelated" is what let the advisory claim
    # neither branch was an ancestor of the other, the reverse of the fact.
    assert (
        await _ancestry({("aaa111", "bbb222"): 0, ("bbb222", "aaa111"): 0})
        == "same_tip"
    )


async def test_branch_ancestry_git_failure_is_unknown() -> None:
    # The clone does not carry the other branch's commit. "Could not check"
    # must not be read as "neither is an ancestor" — that would name a third
    # outcome from a question git never answered.
    assert await _ancestry({}, missing_commits=frozenset({"bbb222"})) == "unknown"


async def test_branch_ancestry_unresolvable_ref_is_unknown() -> None:
    with patch("hub.integrations.git_ops._git", side_effect=_ancestry_git_factory({})):
        relation = await GitOpsIntegration().branch_ancestry(
            "task-424/fix", "task-999/gone", repo="/tmp/repo"
        )
    assert relation == "unknown"


async def test_probe_refreshes_a_missing_ref_before_calling_it_missing() -> None:
    # #1204, найдено машинным ревью сдачи №3. Резолвер читает ТОЛЬКО локальные
    # ссылки, поэтому «ветки нет в клоне» и «ветку сюда не тянули» давали один
    # и тот же ref_unresolved. Пока ответ был advisory, разницы не было; гейт
    # доставки начал на ней действовать и говорил человеку «эту ветку никто не
    # вернёт» про ветку, которая всё это время лежала на origin.
    fetched: list[str] = []
    present = dict(_shas())
    present.pop("task-392/base^{commit}")

    async def fake_git(*args, repo=None, check=True, **kw):
        if args[0] == "rev-parse" and "--verify" in args:
            sha = present.get(args[-1])
            return (0, sha, "") if sha else (1, "", "")
        if args[0] == "ls-remote":
            # origin answers: that head exists.
            return (0, f"bbb222\t{args[-1]}\n", "")
        if args[0] == "fetch":
            fetched.append(args[-1])
            present["task-392/base^{commit}"] = "bbb222"
            return (0, "", "")
        if args[0] == "rev-list":
            return (0, "3" if len(args) == 4 else "1", "")
        return (0, "", "")

    with patch("hub.integrations.git_ops._git", side_effect=fake_git):
        probe = await GitOpsIntegration().branch_stacking_probe(
            "task-424/fix", "task-392/base", base_branch="develop", repo="/tmp/repo"
        )

    assert fetched == ["+refs/heads/task-392/base:refs/remotes/origin/task-392/base"], (
        "обновляется ровно та ссылка, которой не хватило, и явным рефспеком"
    )
    assert probe.outcome is StackProbeOutcome.stacked, (
        "после обновления ответ настоящий, а не «посмотреть не удалось»"
    )


async def test_probe_still_says_unresolved_when_the_refresh_does_not_help() -> None:
    # Обратная сторона того же: ветки нет и на origin. Тогда ref_unresolved
    # остаётся, но теперь он значит «сервер её тоже не знает», а не «мы не
    # смотрели» — и только на таком ответе гейту можно что-то утверждать.
    # «Не знает» здесь — ОТВЕТ origin (ls-remote отработал, вывод пустой), а
    # не отсутствие ответа: сдача №4 моделировала fetch с rc=0 и тем закрепляла
    # ложную посылку, что сбой fetch неотличим от пустого origin.
    asked: list[str] = []
    fetched: list[str] = []

    async def fake_git(*args, repo=None, check=True, **kw):
        if args[0] == "rev-parse" and "--verify" in args:
            sha = _shas().get(args[-1])
            return (0, sha, "") if sha else (1, "", "")
        if args[0] == "ls-remote":
            asked.append(args[-1])
            return (0, "", "")
        if args[0] == "fetch":
            fetched.append(args[-1])
            return (0, "", "")
        return (0, "", "")

    with patch("hub.integrations.git_ops._git", side_effect=fake_git):
        probe = await GitOpsIntegration().branch_stacking_probe(
            "task-424/fix", "task-999/gone", base_branch="develop", repo="/tmp/repo"
        )

    assert asked == ["refs/heads/task-999/gone"], (
        "origin обязан быть спрошен именно про эту ветку до вывода"
    )
    assert fetched == [], "нечего тянуть: origin ответил, что такой ветки нет"
    assert probe.outcome is StackProbeOutcome.unavailable
    assert probe.reason == "ref_unresolved"
    assert (probe.details or "").strip() == "task-999/gone", (
        "в details только то, что не разрешилось ПОСЛЕ обновления"
    )


@pytest.mark.parametrize(
    ("rc", "err"),
    [
        (124, ""),
        (
            128,
            "fatal: unable to access 'https://github.com/x/y/': Could not resolve host",
        ),
        (128, "fatal: Authentication failed"),
    ],
    ids=["timeout", "host_unreachable", "auth"],
)
async def test_probe_does_not_call_a_branch_gone_when_origin_did_not_answer(
    rc: int, err: str
) -> None:
    # #1204, найдено машинным ревью сдачи №4. Сбой самого обновления —
    # таймаут в 60 с, auth, моргание origin — выбрасывался: check=False и
    # возврат _git не читался, после чего утверждалось «на origin её нет, ждать
    # бесполезно». Рядом в этом же файле ls-remote --heads различает «не
    # ответил» (rc != 0) и «ветки нет» (пустой вывод), а пробный merge на
    # rc 124/128 отвечает «спросить не удалось». Теперь и проба так: сбой —
    # отдельный исход, не ref_unresolved, и гейт по нему человека не зовёт.
    async def fake_git(*args, repo=None, check=True, **kw):
        if args[0] == "rev-parse" and "--verify" in args:
            sha = _shas().get(args[-1])
            return (0, sha, "") if sha else (1, "", "")
        if args[0] == "ls-remote":
            return (rc, "", err)
        if args[0] == "fetch":
            raise AssertionError("после неотвеченного ls-remote тянуть нечего")
        return (0, "", "")

    with patch("hub.integrations.git_ops._git", side_effect=fake_git):
        probe = await GitOpsIntegration().branch_stacking_probe(
            "task-424/fix", "task-999/gone", base_branch="develop", repo="/tmp/repo"
        )

    assert probe.outcome is StackProbeOutcome.unavailable
    assert probe.reason == "remote_unreachable", (
        "сбой обновления — не «ветки нет на origin»"
    )
    assert "task-999/gone" in (probe.details or "")
    assert f"rc={rc}" in (probe.details or "")


async def test_probe_treats_a_failed_fetch_as_no_answer_too() -> None:
    # origin ответил, что ветка есть, а сам fetch упал — lock параллельного
    # fetch, обрыв на середине. Это тоже «спросить не удалось», не «ветки нет».
    async def fake_git(*args, repo=None, check=True, **kw):
        if args[0] == "rev-parse" and "--verify" in args:
            sha = _shas().get(args[-1])
            return (0, sha, "") if sha else (1, "", "")
        if args[0] == "ls-remote":
            return (0, f"eee555\t{args[-1]}\n", "")
        if args[0] == "fetch":
            return (
                128,
                "",
                "fatal: Unable to create '.git/FETCH_HEAD.lock': File exists",
            )
        return (0, "", "")

    with patch("hub.integrations.git_ops._git", side_effect=fake_git):
        probe = await GitOpsIntegration().branch_stacking_probe(
            "task-424/fix", "task-999/gone", base_branch="develop", repo="/tmp/repo"
        )

    assert probe.outcome is StackProbeOutcome.unavailable
    assert probe.reason == "remote_unreachable"
    assert "FETCH_HEAD.lock" in (probe.details or "")
