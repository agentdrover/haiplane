"""A pair task with a PR completes only once that PR is delivered (#605).

Until this gate the pair flow had NO merge trigger at all: APPROVED returned
the task to running, report_done completed it, and the PR hung unmerged —
the exact state #363 ruled out for headless. Every merge of the past week
was manual out of necessity, and every one of them now rings the drift
guard. Found by the first live run of #603, after gh finally existed on the
host: the path could not pass for ANY real task.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import aiosqlite
import pytest

from hub import repository as repo
from hub import services
from hub.integrations.noop import NoopDispatch, NoopGitOps
from hub.integrations.protocols import CIProbeOutcome, CIProbeResult
from hub.integrations.registry import plugins
from hub.models import TaskCreate, TaskReviewVerdict, TaskUpdateCreate


async def _approved_pair_task(
    db: aiosqlite.Connection, *, pr_number: int | None = 77
) -> int:
    """A pair task that walked the real path: start → submit → APPROVED."""
    tv = await services.create_task(db, TaskCreate(title="Deliver me"))
    await repo.add_task_update(db, tv.id, "dev", "status", "Plan: build")
    await db.commit()
    await services.pair_start_task(db, tv.id, caller="dev")
    if pr_number is not None:
        await repo.update_task(db, tv.id, pr_number=pr_number)
        await db.commit()
    await services.submit_for_review(db, tv.id)
    await services.record_review_verdict(
        db, tv.id, TaskReviewVerdict(verdict="approved", agent="reviewer")
    )
    return tv.id


def _git(ci: CIProbeOutcome = CIProbeOutcome.passed, *, merged: bool = True):
    g = NoopGitOps()
    g.check_pr_ci = AsyncMock(return_value=CIProbeResult(ci, f"checks_{ci.value}"))
    g.merge_pr = AsyncMock(return_value=merged)
    g.merge_commit_sha = AsyncMock(return_value="gate0merge0sha")
    g.pull_main = AsyncMock(return_value=True)
    g.delete_branch = AsyncMock(return_value=True)
    plugins.git_ops = g
    plugins.dispatch = NoopDispatch()
    return g


async def _report_done(db, task_id) -> None:
    await services.add_update(
        db,
        task_id,
        TaskUpdateCreate(agent="dev", kind="done", content="done, delivered"),
    )


# ---- AC-1: green CI merges, records the merge, completes ----


async def test_report_done_merges_an_approved_pair_pr_before_completing(db):
    g = _git(CIProbeOutcome.passed, merged=True)
    task_id = await _approved_pair_task(db)

    await _report_done(db, task_id)

    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "completed"
    assert g.merge_pr.await_count == 1, "delivery happens exactly once"
    rows = [
        dict(r)
        for r in await db.execute_fetchall(
            "SELECT pr_number, merge_sha FROM pipeline_merges"
        )
    ]
    assert rows and rows[0]["pr_number"] == 77
    assert rows[0]["merge_sha"] == "gate0merge0sha", (
        "the recorded commit is the PR's own, so the drift guard can vouch for it"
    )


# ---- AC-2: anything but a green CI refuses to merge or complete ----


@pytest.mark.parametrize(
    "outcome",
    [CIProbeOutcome.failed, CIProbeOutcome.pending, CIProbeOutcome.unavailable],
)
async def test_red_ci_blocks_pair_completion(db, outcome):
    g = _git(outcome, merged=True)
    task_id = await _approved_pair_task(db)

    await _report_done(db, task_id)

    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "needs_decision", (
        f"CI={outcome.value} must not read as deliverable"
    )
    g.merge_pr.assert_not_awaited()
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    assert any(f"ci_{outcome.value}" in (u.get("content") or "") for u in updates), (
        "the refusal must name the CI outcome, not just say something went wrong"
    )


# ---- AC-3: a refused merge never completes the task ----


async def test_a_refused_merge_does_not_complete_the_task(db):
    _git(CIProbeOutcome.passed, merged=False)
    task_id = await _approved_pair_task(db)

    await _report_done(db, task_id)

    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "needs_decision", (
        "completed with an unmerged PR is the state this gate exists to rule out"
    )
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    assert any("merge_failed" in (u.get("content") or "") for u in updates)


# ---- AC-4: a task with no PR is untouched ----


async def test_a_task_without_a_pr_completes_as_before(db):
    g = _git(CIProbeOutcome.failed, merged=False)  # would refuse if consulted
    task_id = await _approved_pair_task(db, pr_number=None)

    await _report_done(db, task_id)

    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "completed", (
        "config and docs tasks own no PR and must not feel the gate at all"
    )
    g.check_pr_ci.assert_not_awaited()
    g.merge_pr.assert_not_awaited()


# ---- AC-5: an exploding integration degrades, never 500s ----


async def test_unavailable_git_degrades_to_needs_decision(db):
    g = _git()
    g.check_pr_ci = AsyncMock(side_effect=RuntimeError("gh melted"))
    task_id = await _approved_pair_task(db)

    await _report_done(db, task_id)  # must not raise

    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "needs_decision"
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    assert any("merge_gate_error" in (u.get("content") or "") for u in updates), (
        "an exception is a reason the operator can read, not a crash or a "
        "silent completion"
    )


# ---- the PR is discovered at submission, so the gate has a key ----


async def test_submission_discovers_the_pr_for_the_branch(db):
    """Found after the first APPROVED of this very task: nothing in the pair
    flow ever sets pr_number — only headless create_pr does — so the gate
    would have keyed on a field nobody fills and never fired. The seventh
    "mechanism right, path not wired" of the streak, caught before closing
    because this task IS about that class."""
    g = _git(CIProbeOutcome.passed, merged=True)
    g.pr_for_branch = AsyncMock(return_value=238)

    tv = await services.create_task(db, TaskCreate(title="Discover me"))
    await repo.add_task_update(db, tv.id, "dev", "status", "Plan: build")
    await db.commit()
    await services.pair_start_task(db, tv.id, caller="dev")
    await services.submit_for_review(db, tv.id)

    task = dict(await repo.get_task(db, tv.id))
    assert task["pr_number"] == 238, (
        "the hub must look the PR up itself — nobody remembers a number"
    )

    await services.record_review_verdict(
        db, tv.id, TaskReviewVerdict(verdict="approved", agent="reviewer")
    )
    await _report_done(db, tv.id)
    assert dict(await repo.get_task(db, tv.id))["status"] == "completed"
    assert g.merge_pr.await_count == 1, "discovery is what arms the gate end to end"


async def test_a_branch_with_no_pr_submits_and_completes_as_before(db):
    """Config tasks own a branch but no PR (#602 was one). Discovery finding
    nothing — or blowing up — must not change their path by a byte."""
    g = _git(CIProbeOutcome.failed, merged=False)  # would refuse if consulted
    g.pr_for_branch = AsyncMock(side_effect=RuntimeError("gh melted"))

    tv = await services.create_task(db, TaskCreate(title="No PR here"))
    await repo.add_task_update(db, tv.id, "dev", "status", "Plan: config")
    await db.commit()
    await services.pair_start_task(db, tv.id, caller="dev")
    await services.submit_for_review(db, tv.id)  # must not raise

    task = dict(await repo.get_task(db, tv.id))
    assert task["pr_number"] is None

    await services.record_review_verdict(
        db, tv.id, TaskReviewVerdict(verdict="approved", agent="reviewer")
    )
    await _report_done(db, tv.id)
    assert dict(await repo.get_task(db, tv.id))["status"] == "completed"
    g.merge_pr.assert_not_awaited()


# ---- delivered once stays delivered: no second merge ----


async def test_an_already_delivered_pr_is_not_merged_again(db):
    """#363's exactly-once guard caught the gate double-merging on the
    headless conveyor: the poller merges, then walks the same shared
    transition, and a second merge_pr on a merged PR reads as a refusal —
    flipping a DELIVERED task into needs_decision. Delivery is judged by the
    pipeline record, not by who is asking."""
    g = _git(CIProbeOutcome.passed, merged=True)
    task_id = await _approved_pair_task(db)
    await repo.record_pipeline_merge(
        db, pr_number=77, merge_sha="earlier0sha", task_id=task_id
    )

    await _report_done(db, task_id)

    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "completed", (
        "an already-delivered PR must complete, not bounce to needs_decision"
    )
    g.merge_pr.assert_not_awaited()


# ---- AC-6: the gate's merge is not drift ----


async def test_the_gate_merge_is_not_drift(db):
    from hub.db import seed_default_project
    from hub.services import drift_guard

    # Same trap as #604's tests: the in-memory DB seeds no default project,
    # and an unresolvable project would record the merge against None —
    # which #534's review showed must never vouch across projects.
    await seed_default_project(db)
    _git(CIProbeOutcome.passed, merged=True)
    task_id = await _approved_pair_task(db)
    await _report_done(db, task_id)
    assert dict(await repo.get_task(db, task_id))["status"] == "completed"

    project = await repo.resolve_project_for_task(db, task_id)
    known = await repo.known_pipeline_shas(db, dict(project)["id"])
    assert "gate0merge0sha" in known

    raw = "gate0merge0sha\x1fdelivered by the gate\x1fhub\n"
    drifted = drift_guard.classify_commits(raw, known_shas=known)
    assert drifted == [], (
        "a delivery the gate performed must never ring the drift guard"
    )
