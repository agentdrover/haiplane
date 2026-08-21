"""Carrying develop into main by policy, not by hand (#812).

One session repeated the same four steps six times on 21.08.2026 — open the
release PR, wait for CI, merge, wait for the deploy job — and none of them
holds a decision. Two facts from those releases are pinned here: a release
takes develop whole, so its body must name everything it carries; and turning
this on is a project decision, so manual stays the default.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import aiosqlite

from hub import repository as repo
from hub import services
from hub.integrations.noop import NoopGitOps
from hub.integrations.protocols import CIProbeOutcome, CIProbeResult
from hub.integrations.registry import plugins
from hub.models import TaskCreate, TaskReviewVerdict, TaskUpdateCreate
from hub.services.release import release_body


def _git(*, ci: CIProbeOutcome = CIProbeOutcome.passed, existing_pr: int | None = None):
    g = NoopGitOps()
    g.check_pr_ci = AsyncMock(return_value=CIProbeResult(ci, f"checks_{ci.value}"))
    g.merge_pr = AsyncMock(return_value=True)
    g.merge_commit_sha = AsyncMock(return_value="release0merge0sha")
    g.pull_main = AsyncMock(return_value=True)
    g.delete_branch = AsyncMock(return_value=True)
    g.pr_state = AsyncMock(return_value="open")
    g.pr_for_branch = AsyncMock(return_value=existing_pr)
    g.release_range = AsyncMock(
        return_value=[
            "feat(task): live-check evidence (#813)",
            "fix(task): telemetry reason slug (#809)",
            "chore: bump nothing in particular",
        ]
    )
    g.open_release_pr = AsyncMock(return_value=777)
    plugins.git_ops = g
    return g


async def _release_project(db: aiosqlite.Connection, mode: str) -> int:
    """A project whose release policy is auto, manual, or unset."""
    pid = await repo.create_project(db, slug="shipper", name="Shipper")
    policy = json.dumps({"release": mode}) if mode else "{}"
    await repo.update_project(db, pid, gate_policy=policy)
    await db.commit()
    return pid


async def _delivered_task(db: aiosqlite.Connection, project_id: int) -> int:
    """A pair task walked to the point where the delivery gate merges it."""
    tv = await services.create_task(db, TaskCreate(title="Ship me"))
    await repo.update_task(db, tv.id, project_id=project_id)
    await db.commit()
    await repo.add_task_update(db, tv.id, "dev", "status", "Plan: build")
    await db.commit()
    await services.pair_start_task(db, tv.id, caller="dev")
    await repo.update_task(db, tv.id, pr_number=77, branch="task-x/y")
    await db.commit()
    await services.submit_for_review(db, tv.id)
    await services.record_review_verdict(
        db, tv.id, TaskReviewVerdict(verdict="approved", agent="reviewer")
    )
    return tv.id


async def _report_done(db: aiosqlite.Connection, task_id: int) -> None:
    await services.add_update(
        db,
        task_id,
        TaskUpdateCreate(agent="dev", kind="done", content="done, delivered"),
    )


# ---- AC-1: the body names everything the release carries ----


async def test_release_pr_names_everything_it_carries(db: aiosqlite.Connection):
    g = _git()
    pid = await _release_project(db, "auto")
    task_id = await _delivered_task(db, pid)

    await _report_done(db, task_id)

    assert g.open_release_pr.await_count == 1
    body = g.open_release_pr.await_args.args[3]
    assert "#813" in body and "#809" in body, (
        "a release carries develop whole, including other sessions' work — "
        "naming one task while shipping three is a record that lies"
    )
    assert "chore: bump nothing in particular" in body, (
        "a commit without a task number still ships, so it is still listed"
    )
    feed = " ".join(
        dict(u)["content"] for u in await repo.get_task_updates(db, task_id)
    )
    assert "#777" in feed and "#813" in feed


def test_release_body_reads_task_numbers_out_of_subjects():
    body, ids = release_body(
        [
            "feat(task): something (#101)",
            "fix: no number here",
            "feat(task): another (#102)",
        ]
    )
    assert ids == [101, 102]
    assert "no number here" in body, "unnumbered commits are shown, not hidden"
    assert "depends_on" in body, (
        "the body says out loud that order between tasks is not understood here"
    )


# ---- AC-2: green CI releases and the fact is recorded ----


async def test_green_release_is_merged_and_recorded(db: aiosqlite.Connection):
    g = _git(existing_pr=777)
    pid = await _release_project(db, "auto")
    from hub.services.release import merge_ready_release

    project = await repo.get_project(db, pid)
    merged, reason = await merge_ready_release(db, project)

    assert merged is True
    assert "777" in reason
    assert g.merge_pr.await_args.args[0] == 777


# ---- AC-3: a red CI never releases, and says so once ----


async def test_red_ci_never_releases(db: aiosqlite.Connection):
    g = _git(ci=CIProbeOutcome.failed, existing_pr=777)
    pid = await _release_project(db, "auto")
    from hub.services.release import merge_ready_release

    project = await repo.get_project(db, pid)
    merged, reason = await merge_ready_release(db, project)

    assert merged is False
    assert "ci_fail" in reason, "a refusal names its cause"
    g.merge_pr.assert_not_awaited()

    # The same refusal on the next sweep must be the same string, so the
    # poller can recognise it as already reported instead of repeating it.
    _, again = await merge_ready_release(db, project)
    assert again == reason


# ---- AC-4: a manual project is untouched ----


async def test_manual_project_is_untouched(db: aiosqlite.Connection):
    g = _git()
    pid = await _release_project(db, "manual")
    task_id = await _delivered_task(db, pid)

    await _report_done(db, task_id)

    g.open_release_pr.assert_not_awaited()
    g.release_range.assert_not_awaited()

    from hub.services.release import merge_ready_release

    project = await repo.get_project(db, pid)
    merged, reason = await merge_ready_release(db, project)
    assert (merged, reason) == (False, "")
    # The task's own PR is still delivered by the gate — that is the existing
    # behaviour and not what this task changes. What must not happen is a
    # release merge: no call carries the release PR number.
    merged_prs = {call.args[0] for call in g.merge_pr.await_args_list}
    assert 777 not in merged_prs, "a manual project releases when its owner says so"


async def test_unset_policy_reads_as_manual(db: aiosqlite.Connection):
    """An unreadable or absent policy must never ship code (#743's rule)."""
    g = _git()
    pid = await _release_project(db, "")
    task_id = await _delivered_task(db, pid)

    await _report_done(db, task_id)

    g.open_release_pr.assert_not_awaited()


# ---- AC-5: two deliveries share one release PR ----


async def test_release_pr_creation_is_idempotent(db: aiosqlite.Connection):
    g = _git()
    pid = await _release_project(db, "auto")

    first = await _delivered_task(db, pid)
    await _report_done(db, first)
    second = await _delivered_task(db, pid)
    await repo.update_task(db, second, pr_number=78)
    await db.commit()
    await _report_done(db, second)

    assert g.open_release_pr.await_count == 2, "both deliveries refresh the release"
    # The upsert itself decides between create and edit; what matters here is
    # that the caller never asks for a second PR over the same range.
    heads = {call.args[1] for call in g.open_release_pr.await_args_list}
    bases = {call.args[0] for call in g.open_release_pr.await_args_list}
    assert len(heads) == 1 and len(bases) == 1, (
        "one range, one release — a second PR would split one release into two "
        "stories about the same commits"
    )
