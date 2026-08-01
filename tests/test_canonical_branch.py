"""The branch a task owns is an obligation, and a mismatch is refused (#533).

The policy used to allow the local branch to differ from the one recorded on
the task, with divergence resolved by hand. While the names differ the task
points at one branch, the work happens in another, and CI and the reviewer
read code nobody wrote.

WHAT THIS DOES NOT DO, stated here because a test file is where the next
reader looks for the real contract: the hub compares what the client REPORTS
against the canonical name. It has no working copy to inspect — on production
the workspace holds a single .placeholder file — so a client that names the
right branch while sitting in another passes untouched. This catches
forgetting to switch, which is the failure that actually occurs. It is not a
guarantee, and it does not replace the client-side check (#532) or drift
detection (#534).
"""

from __future__ import annotations

import aiosqlite
import pytest
from fastapi import HTTPException

from hub import repository as repo
from hub import services
from hub.models import TaskCreate, TaskSubmitReview, TaskUpdateCreate

CANONICAL = "task-1/canonical-branch-name"


async def _pair_task_ready_to_submit(
    db: aiosqlite.Connection, *, branch: str = CANONICAL, job_id: str | None = None
) -> int:
    tv = await services.create_task(db, TaskCreate(title="Canonical branch"))
    await repo.add_task_update(db, tv.id, "dev", "status", "Plan: work")
    await db.commit()
    await services.pair_start_task(db, tv.id, caller="dev")
    await repo.update_task(db, tv.id, branch=branch, job_id=job_id)
    await db.commit()
    return tv.id


# --- AC-2: a mismatch is refused, with both names and a way out ------------


async def test_a_branch_that_is_not_the_canonical_one_is_refused(
    db: aiosqlite.Connection,
):
    task_id = await _pair_task_ready_to_submit(db)

    with pytest.raises(HTTPException) as exc:
        await services.submit_for_review(
            db, task_id, TaskSubmitReview(branch="task-1/something-else")
        )

    assert exc.value.status_code == 409
    detail = exc.value.detail
    assert detail["error"] == "branch_mismatch"
    assert detail["expected"] == CANONICAL
    assert detail["reported"] == "task-1/something-else"
    assert CANONICAL in detail["hint"], "the way out has to name the branch to use"

    assert dict(await repo.get_task(db, task_id))["status"] == "running", (
        "a refused submission must not move the task"
    )


async def test_the_matching_branch_goes_through(db: aiosqlite.Connection):
    task_id = await _pair_task_ready_to_submit(db)

    tv = await services.submit_for_review(
        db, task_id, TaskSubmitReview(branch=CANONICAL)
    )

    assert tv.status.value == "review"


async def test_omitting_the_branch_skips_the_comparison(db: aiosqlite.Connection):
    """Existing callers do not report a branch. The check is additive: it
    cannot start refusing submissions that never claimed anything."""
    task_id = await _pair_task_ready_to_submit(db)

    tv = await services.submit_for_review(db, task_id, TaskSubmitReview())

    assert tv.status.value == "review"


async def test_a_task_with_no_recorded_branch_is_not_second_guessed(
    db: aiosqlite.Connection,
):
    """Nothing to compare against is not a mismatch."""
    task_id = await _pair_task_ready_to_submit(db, branch="")

    tv = await services.submit_for_review(
        db, task_id, TaskSubmitReview(branch="whatever-i-am-on")
    )

    assert tv.status.value == "review"


# --- AC-3: the headless path is untouched ----------------------------------


async def test_a_headless_task_is_not_subject_to_this_check(
    db: aiosqlite.Connection,
):
    """A dispatched task's branch belongs to its job, and its client never
    reports one. The refusal it already gets must stay the one about headless
    submission — not a new branch complaint."""
    task_id = await _pair_task_ready_to_submit(db, job_id="job-42")

    with pytest.raises(HTTPException) as exc:
        await services.submit_for_review(
            db, task_id, TaskSubmitReview(branch="task-1/wrong")
        )

    assert exc.value.status_code == 400
    assert "headless" in str(exc.value.detail), (
        "the headless contract answers first; #533 must not change what a "
        "dispatched task hears"
    )


# --- AC-1: pair-start states the obligation --------------------------------


async def test_pair_start_records_the_canonical_name(db: aiosqlite.Connection):
    """The name itself is produced by the hub in task-<id>/<slug> form; the
    obligation wording lives in the MCP response and is covered in
    tests/test_mcp_server.py."""
    tv = await services.create_task(db, TaskCreate(title="Canonical branch"))
    await repo.add_task_update(db, tv.id, "dev", "status", "Plan: work")
    await db.commit()

    await services.pair_start_task(db, tv.id, caller="dev")

    branch = dict(await repo.get_task(db, tv.id))["branch"]
    assert branch.startswith(f"task-{tv.id}/"), branch


async def test_the_done_report_path_is_unaffected(db: aiosqlite.Connection):
    """#533 touches submission, not completion. A done report carries no
    branch and must not acquire an opinion about one."""
    task_id = await _pair_task_ready_to_submit(db)
    await services.submit_for_review(db, task_id, TaskSubmitReview(branch=CANONICAL))
    await db.commit()

    updates = await repo.get_task_updates(db, task_id)
    assert any(u["kind"] == "status" for u in updates)
    await services.add_update(
        db, task_id, TaskUpdateCreate(agent="dev", kind="status", content="still here")
    )
