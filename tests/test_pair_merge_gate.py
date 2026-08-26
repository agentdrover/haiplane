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


# ---- AC-2 (#605), narrowed by #951: only TERMINAL CI outcomes call a human ----
#
# This test used to parametrize failed|pending|unavailable into one behaviour.
# #951 split them on purpose: a red CI is a decision, a CI that is still
# running (or unreadable this minute) is a wait — the poller already treated
# it that way, and the needs_decision here cost a human rework on 25.08.2026
# for a CI that went green four minutes later (#949).


async def test_red_ci_blocks_pair_completion(db):
    g = _git(CIProbeOutcome.failed, merged=True)
    task_id = await _approved_pair_task(db)

    await _report_done(db, task_id)

    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "needs_decision", "CI=failed must not read as deliverable"
    g.merge_pr.assert_not_awaited()
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    assert any("ci_fail" in (u.get("content") or "") for u in updates), (
        "the refusal must name the CI outcome, not just say something went wrong"
    )


@pytest.mark.parametrize(
    "outcome",
    [CIProbeOutcome.pending, CIProbeOutcome.unavailable],
)
async def test_a_transient_ci_state_waits_instead_of_calling_a_human(db, outcome):
    # #951 AC-1: время — не решение. Задача остаётся в running, события
    # needs_decision нет, алерт называет исход CI и говорит пересдать после
    # зелёного. На базовом коде этот тест красный: задача уходила к человеку.
    g = _git(outcome, merged=True)
    task_id = await _approved_pair_task(db)

    await _report_done(db, task_id)

    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "running", (
        f"CI={outcome.value} — временное состояние, а не повод звать человека"
    )
    g.merge_pr.assert_not_awaited()
    events = [dict(e) for e in await repo.list_events(db, since=0)]
    assert not any(
        e["kind"] == "needs_decision" and e["task_id"] == task_id for e in events
    ), "временное состояние не должно рождать событие решения"
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    assert any(f"ci_{outcome.value}" in (u.get("content") or "") for u in updates), (
        "алерт обязан назвать исход CI"
    )


async def test_the_wait_resolves_into_delivery_on_the_next_done(db):
    # Путь целиком: жёлтый CI → running, CI позеленел → повторный done
    # доставляет. Ровно сценарий #949, каким он должен был быть.
    g = _git(CIProbeOutcome.pending, merged=True)
    task_id = await _approved_pair_task(db)
    await _report_done(db, task_id)
    assert dict(await repo.get_task(db, task_id))["status"] == "running"

    g.check_pr_ci = AsyncMock(
        return_value=CIProbeResult(CIProbeOutcome.passed, "checks_passed")
    )
    await _report_done(db, task_id)

    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "completed"
    g.merge_pr.assert_awaited()


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
    """A branch with no OBSERVABLE commits keeps today's path byte for byte.

    Narrowed by #967: it used to say "config tasks with a branch", but a
    branch whose diff positively shows commits now gets its PR opened by the
    hub. What this test pins is the ignorance half — branch_diff_paths
    answers None here (Noop), and discovery finding nothing, or blowing up,
    must not change the path of work nobody can accuse of stranding code."""
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


# ---- The approved commit is re-checked before delivery (#612) ----
#
# #572 bound the verdict to a commit and closed the order "push, then approve":
# a branch that moved before the verdict stales it. The opposite order stayed
# open — approve, THEN push, then report_done — because the completion path
# never looked at the pin. The verdict still counts as current (its generation
# never changed), and the gate merges the branch tip, so code the reviewer never
# saw lands in develop under their approval.
#
# Reproduced on a live task on 07.08.2026: #546 was approved at febb12b, then a
# fix and a develop merge moved the branch to c20bc40 while the verdict stayed
# generation 1 = generation 1. It was not exploited — the task was resubmitted —
# and this is the mechanism that makes discipline unnecessary, exactly as #572
# argued when three agent notes (#547, #601, #532) proved not to be enough.


def _git_seeing(monkeypatch, tip: str, *, reachable: bool = True, **kw):
    """A git double whose observed branch tip is scripted.

    The project must also have a workspace: without one resolve_branch_tip
    answers "nowhere to look" before it ever asks git, and every scripted tip
    here would be silently ignored — the first version of these tests did
    exactly that and tested nothing.
    """
    from hub.services import orchestration

    g = _git(**kw)
    g.fetch_base = AsyncMock(
        return_value=(True, "") if reachable else (False, "remote unreachable")
    )
    g.head_sha = AsyncMock(return_value=tip)
    monkeypatch.setattr(
        orchestration,
        "project_git_context",
        AsyncMock(return_value={"repo": "/srv/ws", "base_branch": "develop"}),
    )
    return g


async def test_a_commit_pushed_after_approval_is_not_delivered(db, monkeypatch):
    # AC-1 (#612): approved at one commit, branch now at another → no merge, and
    # the refusal names BOTH commits so the reader can see what diverged.
    g = _git_seeing(monkeypatch, "approved0commit")
    task_id = await _approved_pair_task(db)
    assert dict(await repo.get_task(db, task_id))["submission_sha"] == (
        "approved0commit"
    ), "precondition: the hub pinned the commit that was approved"

    g.head_sha = AsyncMock(return_value="pushed0after0approval")
    await _report_done(db, task_id)

    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "needs_decision", (
        "delivering here would put unreviewed code in develop under an approval"
    )
    g.merge_pr.assert_not_awaited()
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    refusal = " ".join(u.get("content") or "" for u in updates)
    assert "stale_approval" in refusal
    assert "approved0com" in refusal and "pushed0after" in refusal, (
        "both commits must appear, or the reason explains nothing"
    )


async def test_an_unchanged_branch_still_delivers(db, monkeypatch):
    # AC-2 (#612): the normal path is untouched. A check that also blocks honest
    # deliveries would simply be traded away the first time it got in the way.
    g = _git_seeing(monkeypatch, "steady0commit")
    task_id = await _approved_pair_task(db)

    await _report_done(db, task_id)

    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "completed"
    assert g.merge_pr.await_count == 1
    rows = [
        dict(r)
        for r in await db.execute_fetchall("SELECT pr_number FROM pipeline_merges")
    ]
    assert rows and rows[0]["pr_number"] == 77


async def test_an_unresolvable_tip_delivers_with_a_visible_note(db, monkeypatch):
    # AC-3 (#612): the network is not allowed to stop the conveyor. An
    # unreachable remote means "not checked" — said out loud — never a refusal.
    # The same shape as #572 on the verdict path, and the same lesson the
    # dependency advisory taught today: a check that can go red on its own
    # blocks every task, not just the one it was about.
    g = _git_seeing(monkeypatch, "pinned0commit")
    task_id = await _approved_pair_task(db)

    g.fetch_base = AsyncMock(return_value=(False, "remote unreachable"))
    await _report_done(db, task_id)

    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "completed", "a blinking remote must not block delivery"
    assert g.merge_pr.await_count == 1
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    note = " ".join(u.get("content") or "" for u in updates)
    assert "Сверка кода с одобрением НЕ проводилась" in note, (
        "unchecked is a state the reader must see, not an absence of news; "
        "the wording matches the verdict path (#572) on purpose"
    )
    assert "remote unreachable" in note, "and it must carry the cause"
    assert "pinned0commi" in note, "and say which commit was approved"


async def test_a_task_without_a_pin_delivers_as_before(db, monkeypatch):
    # AC-4 (#612): tasks submitted before #572, or ones whose pin could not be
    # taken, must keep working. A new check that retroactively blocks old work
    # is a new outage, not a new guarantee.
    g = _git_seeing(monkeypatch, "")  # head_sha returns nothing → nothing was pinned
    task_id = await _approved_pair_task(db)
    assert dict(await repo.get_task(db, task_id))["submission_sha"] == ""

    await _report_done(db, task_id)

    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "completed"
    assert g.merge_pr.await_count == 1
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    note = " ".join(u.get("content") or "" for u in updates)
    assert "Сверка кода с одобрением НЕ проводилась" in note, (
        "say that nothing was compared"
    )
    assert "не был закреплён" in note, "and why"


# ---------------------------------------------------------------------------
# #767: an empty pr_number meant two different things, and one of them
# completed tasks over unmerged branches.
# ---------------------------------------------------------------------------


async def test_pr_opened_after_submit_is_found_at_done(db, monkeypatch):
    # AC-1 (#767). Live case: #725 was submitted for review BEFORE its PR was
    # opened, so discovery — which ran only inside submit_for_review — found
    # nothing and pr_number stayed empty. The gate keys on that field, so the
    # done report completed the task while PR #336 sat open. Nothing in the
    # flow forbids that order; the hub simply stopped looking. Now it looks
    # again at done time.
    g = _git(CIProbeOutcome.passed, merged=True)
    g.pr_for_branch = AsyncMock(return_value=None)  # no PR yet at submit time
    task_id = await _approved_pair_task(db, pr_number=None)
    assert dict(await repo.get_task(db, task_id))["pr_number"] is None, (
        "precondition: submission-time discovery found nothing"
    )
    g.pr_for_branch = AsyncMock(return_value=336)  # the PR is opened afterwards

    await _report_done(db, task_id)

    task = dict(await repo.get_task(db, task_id))
    assert task["pr_number"] == 336, "the number is recorded, not merely used"
    assert task["status"] == "completed"
    assert g.merge_pr.await_count == 1, "the gate delivered instead of being skipped"
    rows = [
        dict(r)
        for r in await db.execute_fetchall("SELECT pr_number FROM pipeline_merges")
    ]
    assert rows and rows[0]["pr_number"] == 336, (
        "and the delivery is recorded, so the drift guard can vouch for it"
    )


async def test_open_pr_blocks_a_silent_completion(db):
    # AC-2 (#767): once the PR is found, an undelivered one must stop the
    # completion the same way a known pr_number always did. "Completed" over
    # an open PR is the state #363 and #605 both exist to rule out.
    g = _git(CIProbeOutcome.passed, merged=False)  # GitHub refuses the merge
    g.pr_for_branch = AsyncMock(return_value=None)
    task_id = await _approved_pair_task(db, pr_number=None)
    g.pr_for_branch = AsyncMock(return_value=336)

    await _report_done(db, task_id)

    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "needs_decision"
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    alert = " ".join(u.get("content") or "" for u in updates)
    assert "PR #336" in alert, "the refusal must name the PR it could not deliver"
    assert "merge_failed" in alert, "and the cause"


async def test_a_task_without_a_branch_completes_as_before(db):
    # AC-3 (#767): work that owns no branch owns no PR. It must complete
    # exactly as today, and must not pay for a lookup that cannot answer —
    # a check that fires where there is nothing to check is how a warning
    # becomes noise and then gets muted (#534).
    g = _git(CIProbeOutcome.failed, merged=False)  # would refuse if consulted
    g.pr_for_branch = AsyncMock(return_value=None)
    tv = await services.create_task(db, TaskCreate(title="Config only"))
    await repo.update_task(db, tv.id, status="running", auto_review=0)
    await db.commit()

    await _report_done(db, tv.id)

    task = dict(await repo.get_task(db, tv.id))
    assert task["status"] == "completed"
    assert task["branch"] is None
    g.pr_for_branch.assert_not_awaited(), "no branch, no question to ask"
    g.merge_pr.assert_not_awaited()


async def test_pr_lookup_failure_is_a_reason_not_an_exception(db):
    # AC-4 (#767): gh missing, network down, GitHub blinking. A done report
    # must not 500 because the hub could not ask — but the reader has to be
    # told which check did not run, or the completion reads as "there was
    # nothing to deliver", which is the very inference this task removes.
    g = _git(CIProbeOutcome.passed, merged=True)
    g.pr_for_branch = AsyncMock(return_value=None)
    task_id = await _approved_pair_task(db, pr_number=None)
    g.pr_for_branch = AsyncMock(side_effect=RuntimeError("gh: command not found"))

    await _report_done(db, task_id)

    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "completed", "today's rule still decides completion"
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    note = " ".join(u.get("content") or "" for u in updates)
    assert "не ответил" in note, "the failed check says so"
    assert "gh: command not found" in note, "and carries its cause"


async def test_pinned_sha_message_has_no_false_not_pinned(db, monkeypatch):
    # AC-5 (#767): the "NOT pinned" line used to hang off the CI-adoption
    # branch while speaking about pinning, so a submission with a pinned sha
    # and no CI report to adopt said both "Branch tip at submission: 97e4707"
    # and "Branch tip NOT pinned: " with an empty reason — contradicting
    # itself in one sentence (#725, #763). Small, but it is a false signal on
    # exactly the axis the reviewer is asked to trust.
    _git_seeing(monkeypatch, "pinned0commit")
    tv = await services.create_task(db, TaskCreate(title="Pin me"))
    await repo.add_task_update(db, tv.id, "dev", "status", "Plan: build")
    await db.commit()
    await services.pair_start_task(db, tv.id, caller="dev")

    await services.submit_for_review(db, tv.id)

    assert dict(await repo.get_task(db, tv.id))["submission_sha"] == "pinned0commit"
    updates = [dict(u) for u in await repo.get_task_updates(db, tv.id)]
    submission = next(
        u["content"]
        for u in reversed(updates)
        if "Submitted for review" in (u["content"] or "")
    )
    assert "Branch tip at submission: pinned0commi" in submission
    assert "NOT pinned" not in submission, (
        "a pinned commit and 'NOT pinned' cannot both be true of one submission"
    )


# ---- #802: a recorded PR number is an observation, not a fact ----
#
# A pull request can close without its author: a stacked PR dies when its base
# branch is merged and deleted. That stranded #774 — live branch, green PR,
# closed number on the task — and the gate, holding the number, refused to
# deliver approved work at all. Same rule as #767, other direction: the field
# caches what was seen, so it has to be re-checked before it is trusted.


def _git_with_state(state: str, *, found: int | None = None):
    g = _git(CIProbeOutcome.passed, merged=True)
    g.pr_state = AsyncMock(return_value=state)
    g.pr_for_branch = AsyncMock(return_value=found)
    return g


async def test_a_closed_pr_is_replaced_by_the_live_one(db):
    g = _git_with_state("closed", found=362)
    task_id = await _approved_pair_task(db, pr_number=360)
    await repo.update_task(db, task_id, branch="task-774/message-wakeup")
    await db.commit()

    await _report_done(db, task_id)

    task = dict(await repo.get_task(db, task_id))
    assert task["pr_number"] == 362, "the live PR is recorded on the task"
    assert g.merge_pr.await_args.args[0] == 362, "and it is the one delivered"
    assert task["status"] == "completed"
    feed = " ".join(
        dict(u)["content"] for u in await repo.get_task_updates(db, task_id)
    )
    assert "#360" in feed and "#362" in feed, "both numbers are named in the feed"


async def test_a_merged_pr_is_never_replaced(db):
    g = _git_with_state("merged", found=999)
    task_id = await _approved_pair_task(db, pr_number=360)
    await repo.update_task(db, task_id, branch="task-774/message-wakeup")
    await db.commit()

    await _report_done(db, task_id)

    task = dict(await repo.get_task(db, task_id))
    assert task["pr_number"] == 360, "a merged PR keeps its number"
    g.pr_for_branch.assert_not_awaited()
    assert g.merge_pr.await_args.args[0] == 360, (
        "merging twice is not extra safety — #605 had to guard exactly this"
    )


async def test_no_live_pr_is_a_named_refusal(db):
    _git_with_state("closed", found=None)
    task_id = await _approved_pair_task(db, pr_number=360)
    await repo.update_task(db, task_id, branch="task-774/message-wakeup")
    await db.commit()

    await _report_done(db, task_id)

    task = dict(await repo.get_task(db, task_id))
    assert task["status"] != "completed", "nothing was delivered, nothing completes"
    feed = " ".join(
        dict(u)["content"] for u in await repo.get_task_updates(db, task_id)
    )
    assert "#360" in feed and "закрыт" in feed


async def test_pr_state_failure_is_a_reason_not_an_exception(db):
    g = _git_with_state("open")
    g.pr_state = AsyncMock(side_effect=RuntimeError("gh is not installed"))
    task_id = await _approved_pair_task(db, pr_number=360)
    await repo.update_task(db, task_id, branch="task-774/message-wakeup")
    await db.commit()

    await _report_done(db, task_id)

    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "completed", (
        "an unanswerable question keeps today's behaviour instead of blocking"
    )
    feed = " ".join(
        dict(u)["content"] for u in await repo.get_task_updates(db, task_id)
    )
    assert "gh is not installed" in feed


# ---- #952: подсказка называет только действия, доступные в достигнутом статусе ----


async def test_the_terminal_refusal_hint_names_reachable_actions_only(db):
    """Алерт пишется вместе с переходом в needs_decision и читается после него.

    Из needs_decision hub_report_done отвергается самим хабом
    (human_decision_required) — 25.08 подсказка «report done again» отправила
    исполнителя ровно в этот отказ (#949). Текст обязан вести через решение:
    hub_decide_task, и объяснять, что даёт каждый исход.
    """
    _git(CIProbeOutcome.failed, merged=True)
    task_id = await _approved_pair_task(db)

    await _report_done(db, task_id)

    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "needs_decision"
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    alert = next(
        u
        for u in updates
        if u["kind"] == "alert" and "NOT completed" in (u["content"] or "")
    )
    assert "report done again" not in alert["content"], (
        "подсказка не должна предлагать вызов, который этот статус отвергает"
    )
    assert "hub_decide_task" in alert["content"], (
        "подсказка обязана назвать доступное действие"
    )
    assert "rework" in alert["content"] and "accept" in alert["content"], (
        "оба исхода решения названы — читатель выбирает осознанно"
    )


# ---- #959: "нет такого PR" и "спросить не удалось" — разные ответы ----
#
# Оба сегодня схлопывались в "" и вели к одному решению: номер остаётся.
# Для сетевой икоты это верно, для номера из чужого репозитория — нет: после
# переезда проекта записанные до него номера не найдутся НИКОГДА, а гейт
# продолжал ждать по ним зелёного CI. Поймано на #880 25.08.2026 — вердикт
# APPROVED, PR открыт, CI зелёный, и всё равно не доставлено.


async def test_an_absent_recorded_pr_is_replaced_by_the_live_one(db):
    g = _git_with_state("absent", found=29)
    task_id = await _approved_pair_task(db, pr_number=472)
    await repo.update_task(db, task_id, branch="task-880/incremental-review-delta")
    await db.commit()

    await _report_done(db, task_id)

    task = dict(await repo.get_task(db, task_id))
    assert task["pr_number"] == 29, "живой PR ветки записывается на задачу"
    assert g.merge_pr.await_args.args[0] == 29, "и доставляется именно он"
    assert task["status"] == "completed"
    feed = " ".join(
        dict(u)["content"] for u in await repo.get_task_updates(db, task_id)
    )
    assert "#472" in feed and "#29" in feed, "оба номера названы в ленте"


async def test_an_unreachable_lookup_keeps_the_recorded_pr(db):
    # Регрессия к сегодняшнему поведению: "спросить не удалось" — не факт об
    # отсутствии. Сеть моргнула — номер обязан остаться на месте, иначе первый
    # же сбой сети переписал бы задаче PR.
    g = _git_with_state("", found=29)
    task_id = await _approved_pair_task(db, pr_number=472)
    await repo.update_task(db, task_id, branch="task-880/incremental-review-delta")
    await db.commit()

    await _report_done(db, task_id)

    task = dict(await repo.get_task(db, task_id))
    assert task["pr_number"] == 472, "непрочитанное состояние не меняет номер"
    g.pr_for_branch.assert_not_awaited(), "и не запускает поиск замены"


async def test_an_absent_pr_without_replacement_calls_a_human(db):
    _git_with_state("absent", found=None)
    task_id = await _approved_pair_task(db, pr_number=472)
    await repo.update_task(db, task_id, branch="task-880/incremental-review-delta")
    await db.commit()

    await _report_done(db, task_id)

    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "needs_decision", (
        "менять номер не на что — это решение человека, а не ожидание"
    )
    feed = " ".join(
        dict(u)["content"] for u in await repo.get_task_updates(db, task_id)
    )
    assert "#472" in feed
    assert "CI станет зелёным" not in feed, (
        "обещать зелёный CI по PR, которого нет, — отправлять в тупик"
    )


async def test_a_deferral_on_an_unestablished_pr_does_not_promise_green_ci(db):
    # Состояние PR прочитать не удалось, и CI по нему тоже не читается. Ждать
    # тут по-прежнему правильно, но называть причиной жёлтый CI — нет: гейт не
    # установил даже, о каком PR речь.
    g = _git_with_state("")
    g.check_pr_ci = AsyncMock(
        return_value=CIProbeResult(CIProbeOutcome.unavailable, "gh_error")
    )
    task_id = await _approved_pair_task(db, pr_number=472)
    await repo.update_task(db, task_id, branch="task-880/incremental-review-delta")
    await db.commit()

    await _report_done(db, task_id)

    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "running", "непрочитанное состояние — всё ещё ожидание"
    alert = next(
        dict(u)["content"]
        for u in reversed(await repo.get_task_updates(db, task_id))
        if "отложена" in (dict(u)["content"] or "")
    )
    assert "CI станет зелёным" not in alert, (
        "причина отсрочки — недоступный PR, а не жёлтый CI"
    )
    assert "#472" in alert and "состояние" in alert.lower()


# ---------------------------------------------------------------------------
# #967: ветка с подтверждёнными коммитами не завершается без PR — хаб сам
# открывает его вместо предупреждения постфактум.
#
# Живой случай #961: сдача без PR, APPROVED, done — completed, а 17 изменённых
# файлов остались висеть на ветке. PR #45 открыли руками через 25 секунд ПОСЛЕ
# того, как гейт уже прошёл мимо. Предупреждение #498 сработало и никого не
# остановило. Та же неделя: #963, #965, #966.
#
# Блок взводится ТОЛЬКО положительным знанием: branch_diff_paths вернул
# непустой список. None («не смог посмотреть») и [] («ничего не меняет»)
# сохраняют сегодняшний путь байт в байт — линия #498/#767 «обвинение по
# незнанию хуже молчания». NoopGitOps возвращает None, поэтому все тесты выше
# этой секции идут без правок.
# ---------------------------------------------------------------------------


async def _running_pair_task(db) -> int:
    """A pair task at the moment of submission: start, plan, no verdict yet."""
    tv = await services.create_task(db, TaskCreate(title="Open my PR"))
    await repo.add_task_update(db, tv.id, "dev", "status", "Plan: build")
    await db.commit()
    await services.pair_start_task(db, tv.id, caller="dev")
    return tv.id


async def test_submission_opens_a_pr_for_a_branch_with_commits(db):
    """AC-1 (#967). Ветка проверяемо меняет файлы, PR нет — сдача сама пушит
    и открывает его, чтобы CI шёл параллельно с ревью, а не стартовал после
    done."""
    g = _git(CIProbeOutcome.passed, merged=True)
    g.pr_for_branch = AsyncMock(return_value=None)
    g.branch_diff_paths = AsyncMock(return_value=["hub/app.py"])
    g.push_branch = AsyncMock(return_value=True)
    g.create_pr = AsyncMock(return_value=512)
    task_id = await _running_pair_task(db)

    await services.submit_for_review(db, task_id)

    task = dict(await repo.get_task(db, task_id))
    assert task["pr_number"] == 512, "созданный PR записан, а не просто открыт"
    assert g.push_branch.await_count == 1, (
        "gh pr create на незапушенной ветке отказывает — сначала push"
    )
    assert g.push_branch.await_args.args[0] == task["branch"]
    assert g.create_pr.await_count == 1


async def test_a_failed_pr_creation_warns_and_the_submission_proceeds(db):
    """AC-1 (#967), половина про отказ: GitHub сказал «нет». Ревью в хабе всё
    ещё валидно — сдача проходит, но читателю названо, что PR не открыт:
    молчание здесь воспроизводит #961 на done."""
    g = _git(CIProbeOutcome.passed, merged=True)
    g.pr_for_branch = AsyncMock(return_value=None)
    g.branch_diff_paths = AsyncMock(return_value=["hub/app.py"])
    g.push_branch = AsyncMock(return_value=True)
    g.create_pr = AsyncMock(return_value=None)
    task_id = await _running_pair_task(db)

    await services.submit_for_review(db, task_id)  # не должен упасть

    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "review", "сдача сама по себе обязана пройти"
    assert task["pr_number"] is None
    feed = " ".join(
        dict(u)["content"] or "" for u in await repo.get_task_updates(db, task_id)
    )
    assert "не удалось" in feed and task["branch"] in feed, (
        "отказ создания — предупреждение с именем ветки, не тишина"
    )


async def test_done_without_pr_creates_one_and_delivers_through_the_gate(db):
    """AC-2 (#967). Точная последовательность #961 — сдача без PR, APPROVED,
    done — теперь заканчивается доставкой, а не completed с брошенной
    веткой."""
    g = _git(CIProbeOutcome.passed, merged=True)
    g.pr_for_branch = AsyncMock(return_value=None)
    task_id = await _approved_pair_task(db, pr_number=None)
    # Коммиты становятся видимыми только теперь: None у Noop держал настройку
    # на сегодняшнем пути — тот же приём, что у тестов #767 с pr_for_branch.
    g.branch_diff_paths = AsyncMock(return_value=["hub/app.py", "tests/test_a.py"])
    g.push_branch = AsyncMock(return_value=True)
    g.create_pr = AsyncMock(return_value=513)

    await _report_done(db, task_id)

    task = dict(await repo.get_task(db, task_id))
    assert task["pr_number"] == 513
    assert task["status"] == "completed"
    assert g.merge_pr.await_count == 1, "гейт доставил PR, который сам же открыл"
    assert g.merge_pr.await_args.args[0] == 513
    rows = [
        dict(r)
        for r in await db.execute_fetchall("SELECT pr_number FROM pipeline_merges")
    ]
    assert rows and rows[0]["pr_number"] == 513, (
        "доставка записана — сторож дрейфа может за неё поручиться"
    )
    feed = " ".join(
        dict(u)["content"] or "" for u in await repo.get_task_updates(db, task_id)
    )
    assert "открыт хабом" in feed, "кто открыл PR — факт для ленты, не для логов"


@pytest.mark.parametrize(
    "refusal",
    [None, RuntimeError("gh: rate limited")],
    ids=["returned_none", "raised"],
)
async def test_confirmed_commits_without_a_creatable_pr_block_completion(db, refusal):
    """AC-3 (#967). git подтвердил коммиты, так что completed заведомо бросил
    бы их на ветке. Это положительное знание — противоположность тишине
    #498 — и единственный случай, который останавливает завершение."""
    g = _git(CIProbeOutcome.passed, merged=True)
    g.pr_for_branch = AsyncMock(return_value=None)
    task_id = await _approved_pair_task(db, pr_number=None)
    g.branch_diff_paths = AsyncMock(return_value=["hub/app.py"])
    g.push_branch = AsyncMock(return_value=True)
    g.create_pr = (
        AsyncMock(return_value=None)
        if refusal is None
        else AsyncMock(side_effect=refusal)
    )

    await _report_done(db, task_id)  # не должен упасть (AC-5 из #605)

    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "needs_decision", (
        "коммиты есть, PR открыть нельзя — это решение человека, не completed"
    )
    g.merge_pr.assert_not_awaited()
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    alert = next(
        u["content"]
        for u in updates
        if u["kind"] == "alert" and "NOT completed" in (u["content"] or "")
    )
    assert task["branch"] in alert, "отказ называет ветку, которую не смог доставить"
    assert "hub_decide_task" in alert, "подсказка ведёт через доступное действие (#952)"


@pytest.mark.parametrize("diff", [None, []], ids=["could_not_look", "empty_diff"])
async def test_ignorance_still_completes_as_today(db, diff):
    """AC-4 (#967). None — «не смог посмотреть», [] — «ничего не меняет»:
    ни то ни другое не знание о брошенных коммитах, а обвинение по незнанию
    хуже молчания (#498). Оба сохраняют сегодняшнее завершение, и create_pr
    не вызывается вовсе."""
    g = _git(CIProbeOutcome.failed, merged=False)  # would refuse if consulted
    g.pr_for_branch = AsyncMock(return_value=None)
    task_id = await _approved_pair_task(db, pr_number=None)
    g.branch_diff_paths = AsyncMock(return_value=diff)
    g.push_branch = AsyncMock(return_value=True)
    g.create_pr = AsyncMock(side_effect=RuntimeError("must not be consulted"))

    await _report_done(db, task_id)

    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "completed", "сегодняшнее правило решает завершение"
    g.create_pr.assert_not_awaited()
    g.merge_pr.assert_not_awaited()


async def test_a_freshly_created_pr_with_running_ci_keeps_the_task_running(db):
    """AC-5 (#967), сторож #951. У PR, созданного секунды назад, CI бежит по
    построению. Это временное состояние, а не решение — задача ждёт в
    running ровно как установил #951, и тест, уводящий это в needs_decision,
    сломал бы ту починку."""
    g = _git(CIProbeOutcome.pending, merged=False)
    g.pr_for_branch = AsyncMock(return_value=None)
    task_id = await _approved_pair_task(db, pr_number=None)
    g.branch_diff_paths = AsyncMock(return_value=["hub/app.py"])
    g.push_branch = AsyncMock(return_value=True)
    g.create_pr = AsyncMock(return_value=514)

    await _report_done(db, task_id)

    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "running", "бегущий CI — ожидание, а не решение человека"
    assert task["pr_number"] == 514
    g.merge_pr.assert_not_awaited()
    alert = next(
        dict(u)["content"]
        for u in reversed(await repo.get_task_updates(db, task_id))
        if "отложена" in (dict(u)["content"] or "")
    )
    assert "#514" in alert


# ---- находка ревью #967: снимок #498 берётся ДО перехода и писался ПОСЛЕ ----
#
# undelivered_warning вычисляется в add_update до transition_after_agent_done,
# а пишется в ленту и warnings ответа после. Теперь переход сам отвечает на
# вопрос снимка: открывает PR (и в AC-2 даже сливает его) или уходит в
# needs_decision со своим, более громким алертом. Старый текст «хаб не знает
# ни PR» поверх только что записанного PR — та самая ложь, от которой AC-2
# #498 предостерегает: предупреждение, поймавшееся на вранье, перестают читать.


def _project_ctx(monkeypatch) -> None:
    """A project context with a workspace, so the #498 snapshot can look."""
    from hub import services as services_module

    ctx = AsyncMock(return_value={"repo": "/srv/ws", "base_branch": "develop"})
    monkeypatch.setattr(services_module, "project_git_context", ctx)
    monkeypatch.setattr(
        "hub.services.orchestration.project_git_context", ctx, raising=False
    )


async def test_the_stale_snapshot_is_not_written_over_a_delivered_pr(db, monkeypatch):
    _project_ctx(monkeypatch)
    g = _git(CIProbeOutcome.passed, merged=True)
    g.pr_for_branch = AsyncMock(return_value=None)
    task_id = await _approved_pair_task(db, pr_number=None)
    g.branch_diff_paths = AsyncMock(return_value=["hub/app.py"])
    g.push_branch = AsyncMock(return_value=True)
    g.create_pr = AsyncMock(return_value=515)

    await _report_done(db, task_id)

    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "completed" and task["pr_number"] == 515
    feed = " ".join(
        dict(u)["content"] or "" for u in await repo.get_task_updates(db, task_id)
    )
    assert "не начала доставляться" not in feed, (
        "хаб сам открыл и слил PR — снимок, снятый до перехода, обязан умолкнуть"
    )
    assert "остались в ветке" not in feed


async def test_the_stale_snapshot_is_not_written_over_a_refusal(db, monkeypatch):
    # needs_decision пишет свой алерт («NOT completed», действия по #952);
    # снимок #498 рядом с ним утверждал бы «Задача завершена» — неправду.
    _project_ctx(monkeypatch)
    g = _git(CIProbeOutcome.passed, merged=True)
    g.pr_for_branch = AsyncMock(return_value=None)
    task_id = await _approved_pair_task(db, pr_number=None)
    g.branch_diff_paths = AsyncMock(return_value=["hub/app.py"])
    g.push_branch = AsyncMock(return_value=True)
    g.create_pr = AsyncMock(return_value=None)

    await _report_done(db, task_id)

    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "needs_decision"
    feed = " ".join(
        dict(u)["content"] or "" for u in await repo.get_task_updates(db, task_id)
    )
    assert "NOT completed" in feed, "отказ гейта говорит сам, громче и точнее"
    assert "Задача завершена" not in feed, (
        "рядом с needs_decision снимок #498 утверждал бы неправду о статусе"
    )


# ---------------------------------------------------------------------------
# #971 — доставка следует из состояния, а не из вызова агента
# ---------------------------------------------------------------------------
#
# 26.08.2026 сессия сдала #954 на ревью в 20:34:28Z и умерла (последний
# heartbeat 20:28:08Z). APPROVED пришёл в 21:36:16Z — через час. PR #84 был
# открыт, MERGEABLE/CLEAN, CI зелёный, вердикт по текущей генерации. Доставки
# не произошло: у pair-задачи мерж запускает ТОЛЬКО done-отчёт, а звать его
# было некому. Задача осталась в running, и снять её оттуда мог лишь человек
# роутом force-complete, кнопки на который в вебе нет.
#
# Автоматика для этого написана давно — _deliver_approved_review, — но живёт
# за _sweep_review, который требует статус review И review_job_id. У pair-задачи
# нет ни того, ни другого: ревью клиентское (#307), а APPROVED возвращает её в
# running. Здесь тот же гейт запускается по состоянию.


async def _drain_pair_delivery(db) -> None:
    from hub import poller

    await poller._sweep_pair_delivery(db)


async def test_approved_pair_task_delivers_without_the_agent(db):
    # AC-1: агент не сделал НИ ОДНОГО вызова после апрува — работа всё равно
    # доехала. Это вся суть задачи: условия доставки наблюдаемы хабом целиком,
    # и ни одно из них не про то, жив ли какой-то процесс.
    g = _git(CIProbeOutcome.passed, merged=True)
    task_id = await _approved_pair_task(db)

    await _drain_pair_delivery(db)

    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "completed", "одобренная работа обязана доехать сама"
    assert g.merge_pr.await_count == 1, "доставка ровно один раз"
    rows = [
        dict(r)
        for r in await db.execute_fetchall(
            "SELECT pr_number, merge_sha, task_id FROM pipeline_merges"
        )
    ]
    assert rows and rows[0]["pr_number"] == 77
    assert rows[0]["merge_sha"] == "gate0merge0sha", (
        "SHA записан тот же, что и на done-пути, иначе drift-guard не поручится"
    )


async def test_auto_delivery_obeys_every_existing_gate(db):
    # AC-2: меняется ТРИГГЕР проверки, а не проверка. Красный CI на этом пути
    # обязан вести себя ровно так же, как на done-пути: не мержить, не
    # завершать, позвать человека (#363).
    g = _git(CIProbeOutcome.failed, merged=True)
    task_id = await _approved_pair_task(db)

    await _drain_pair_delivery(db)

    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "needs_decision"
    g.merge_pr.assert_not_awaited()
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    assert any("ci_fail" in (u.get("content") or "") for u in updates), updates


async def test_transient_ci_waits_instead_of_calling_a_human(db):
    # AC-2, вторая половина (#951): жёлтый CI — это «спросить снова через
    # минуту», а не решение. Задача остаётся в running, человека не зовут, и в
    # лог на каждом цикле ничего не сыплется (#534): свип проходит здесь
    # постоянно, а строка на цикл — это способ заглушить настоящий сигнал.
    g = _git(CIProbeOutcome.pending, merged=True)
    task_id = await _approved_pair_task(db)

    await _drain_pair_delivery(db)
    await _drain_pair_delivery(db)

    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "running", "ожидание CI — не повод звать человека"
    g.merge_pr.assert_not_awaited()
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    waiting = [u for u in updates if "ci_pending" in (u.get("content") or "")]
    assert len(waiting) <= 1, (
        f"два цикла ожидания не должны давать две записи: {waiting}"
    )


async def test_stale_approval_never_auto_delivers(db):
    # AC-3: апрув прошлой генерации не доставляет. После апрува была пересдача
    # — значит одобрен другой код, и вердикт к нынешнему отношения не имеет
    # (#306). Автопуть обязан быть здесь строже, а не мягче: агента, который
    # объяснил бы разницу, тут нет.
    g = _git(CIProbeOutcome.passed, merged=True)
    task_id = await _approved_pair_task(db)
    await services.submit_for_review(db, task_id)

    await _drain_pair_delivery(db)

    task = dict(await repo.get_task(db, task_id))
    assert task["status"] != "completed", "устаревший апрув не доставляет"
    g.merge_pr.assert_not_awaited()


async def test_late_done_report_lands_as_a_record(db):
    # AC-4: агент вернулся к жизни после автодоставки. Его отчёт — рассказ, а
    # не вторая попытка доставки: второго мержа нет, отказа нет, текст лежит в
    # фиде. Иначе вернувшийся агент упирался бы в собственную доставленную
    # работу — и это худший способ узнать, что всё хорошо.
    g = _git(CIProbeOutcome.passed, merged=True)
    task_id = await _approved_pair_task(db)
    await _drain_pair_delivery(db)
    assert dict(await repo.get_task(db, task_id))["status"] == "completed"

    await _report_done(db, task_id)

    task = dict(await repo.get_task(db, task_id))
    assert task["status"] == "completed"
    assert g.merge_pr.await_count == 1, "мерж ровно один на обе дороги (#363)"
    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    assert any("done, delivered" in (u.get("content") or "") for u in updates), (
        f"рассказ вернувшегося агента обязан лечь в фид: {updates}"
    )


async def test_poller_delivery_says_who_delivered(db):
    # AC-5: завершённая задача без отчёта агента не должна читаться как
    # потерянная запись. В фиде прямо сказано, что доставил хаб.
    _git(CIProbeOutcome.passed, merged=True)
    task_id = await _approved_pair_task(db)

    await _drain_pair_delivery(db)

    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    assert any(
        "хаб" in (u.get("content") or "").lower()
        and "достав" in (u.get("content") or "").lower()
        for u in updates
    ), f"кто доставил — должно быть видно, а не выводиться: {updates}"


async def test_a_task_with_a_dispatch_job_is_left_to_its_own_conveyor(db):
    # Граница: headless-задача судится своим свипом (_sweep_review) по своей
    # джобе. Забрать её сюда значило бы завести второго хозяина у одной
    # задачи — и два конвейера, спорящих за один PR.
    g = _git(CIProbeOutcome.passed, merged=True)
    task_id = await _approved_pair_task(db)
    await repo.update_task(db, task_id, job_id="job-1")
    await db.commit()

    await _drain_pair_delivery(db)

    g.merge_pr.assert_not_awaited()
    assert dict(await repo.get_task(db, task_id))["status"] == "running"
