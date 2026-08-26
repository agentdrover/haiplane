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
