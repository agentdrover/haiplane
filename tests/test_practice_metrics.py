"""Slices of practice_metrics that must not flatter the practice.

Human gates (#737): how often the human click changes the outcome and how long
work queues for it. Only human decisions count — 'hub', 'policy', and 'steward'
actors are excluded on both sides of the ratio; unmeasurable waits are reported,
never zeroed. Steward judgements get their own gate=steward row (#1023).

Review economics (#828) and escaped defects (#528) follow the same rule from
opposite ends: what a run cost, and what the gate failed to stop. Across all
three the invariant is the one #519 and #810 paid for — an exclusion is counted
and named, never folded into the number it would otherwise improve.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import aiosqlite
import pytest
from httpx import AsyncClient

from hub import repository as repo
from hub import services
from hub.config import TokenIdentity
from hub.models import StewardGround, StewardJudgementSubmit, TaskDecide
from hub.services.steward_judgement import record_steward_judgement
from hub.db import _MIGRATIONS, _SCHEMA, _migrate  # noqa: F401
from hub.services.orchestration import practice_metrics


def _ts(hours_ago: float) -> str:
    moment = datetime.now(UTC) - timedelta(hours=hours_ago)
    return moment.strftime("%Y-%m-%d %H:%M:%S")


async def _task(
    db: aiosqlite.Connection,
    *,
    title: str,
    status: str = "draft",
    project_id: int | None = None,
) -> int:
    task_id = await repo.create_task(
        db,
        title=title,
        description="",
        runtime="auto",
        source="agent",
        assigned_agent="",
        rationale="",
        status=status,
        auto_review=False,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    if project_id is not None:
        await repo.update_task(db, task_id, project_id=project_id)
    return task_id


def _gate(metrics: list[dict], gate: str, project: str) -> dict:
    rows = [r for r in metrics if r["gate"] == gate and r["project"] == project]
    assert rows, f"no {gate} row for project {project}: {metrics}"
    return rows[0]


async def test_human_gates_override_rate(db: aiosqlite.Connection):
    # AC-1 (#737): approvals vs overrides per gate, split by project.
    other = await repo.create_project(db, slug="spike", name="Spike")

    a1 = await _task(db, title="approved 1")
    a2 = await _task(db, title="approved 2")
    r1 = await _task(db, title="rejected 1")
    b1 = await _task(db, title="other project approved", project_id=other)

    for tid in (a1, a2):
        await repo.insert_event(db, kind="task_approved", task_id=tid, actor="human")
    await repo.insert_event(db, kind="task_rejected", task_id=r1, actor="human")
    await repo.insert_event(db, kind="task_approved", task_id=b1, actor="human")

    v1 = await _task(db, title="verdict approved")
    v2 = await _task(db, title="verdict changes")
    await repo.insert_event(
        db,
        kind="review_verdict_recorded",
        task_id=v1,
        actor="reviewer",
        payload={"verdict": "approved"},
    )
    await repo.insert_event(
        db,
        kind="review_verdict_recorded",
        task_id=v2,
        actor="reviewer",
        payload={"verdict": "changes_requested"},
    )
    await db.commit()

    gates = (await practice_metrics(db))["human_gates"]

    dor_default = _gate(gates, "dor", "default")
    assert dor_default["approvals"] == 2
    assert dor_default["overrides"] == 1
    assert dor_default["override_rate"] == round(1 / 3, 3)

    dor_spike = _gate(gates, "dor", "spike")
    assert dor_spike["approvals"] == 1
    assert dor_spike["overrides"] == 0
    assert dor_spike["override_rate"] == 0.0

    verdict = _gate(gates, "verdict", "default")
    assert verdict["approvals"] == 1
    assert verdict["overrides"] == 1
    assert verdict["override_rate"] == 0.5


async def test_gate_wait_time_median(db: aiosqlite.Connection):
    # AC-2 (#737): the wait is measured from the moment the gate could act
    # (DoR passed / submitted) to the human decision; unmeasurable rows are
    # counted, not zeroed.
    waited = await _task(db, title="waited 3h")
    await repo.update_task(db, waited, ready_at=_ts(3.0))
    unmeasured = await _task(db, title="no ready_at")
    for tid in (waited, unmeasured):
        await repo.insert_event(db, kind="task_approved", task_id=tid, actor="human")

    submitted = await _task(db, title="verdict after 2h", status="review")
    await db.execute(
        "INSERT INTO task_updates (task_id, agent, kind, content, created_at) "
        "VALUES (?, '', 'status', 'Submitted for review (submission #1). x', ?)",
        (submitted, _ts(2.0)),
    )
    await repo.insert_event(
        db,
        kind="review_verdict_recorded",
        task_id=submitted,
        actor="reviewer",
        payload={"verdict": "approved"},
    )
    await db.commit()

    gates = (await practice_metrics(db))["human_gates"]

    dor = _gate(gates, "dor", "default")
    assert dor["wait_unaccounted"] == 1
    assert dor["median_wait_hours"] is not None
    assert 2.8 <= dor["median_wait_hours"] <= 3.2

    verdict = _gate(gates, "verdict", "default")
    assert verdict["median_wait_hours"] is not None
    assert 1.8 <= verdict["median_wait_hours"] <= 2.2


async def test_non_human_actors_excluded(db: aiosqlite.Connection):
    # AC-3 (#737): hub (auto-approve #584) and policy (#738) decisions are
    # not part of the HUMAN gates — neither side of the ratio.
    auto = await _task(db, title="auto approved")
    policy = await _task(db, title="policy approved")
    human = await _task(db, title="human approved")
    await repo.insert_event(db, kind="task_approved", task_id=auto, actor="hub")
    await repo.insert_event(db, kind="task_approved", task_id=policy, actor="policy")
    await repo.insert_event(db, kind="task_approved", task_id=human, actor="human")
    await repo.insert_event(
        db,
        kind="review_verdict_recorded",
        task_id=human,
        actor="policy",
        payload={"verdict": "approved"},
    )
    await db.commit()

    gates = (await practice_metrics(db))["human_gates"]

    dor = _gate(gates, "dor", "default")
    assert dor["approvals"] == 1, "hub and policy approvals must not count"
    assert not [r for r in gates if r["gate"] == "verdict"], (
        "a policy verdict must not create a human verdict row"
    )


async def test_decision_gate_counts_accept_and_rework(db: aiosqlite.Connection):
    # AC-4 (#737): decide_task leaves a countable trace — accept approves,
    # rework overrides, and the wait runs from entering needs_decision.
    accepted = await _task(db, title="decide accept", status="needs_decision")
    reworked = await _task(db, title="decide rework", status="needs_decision")
    for tid in (accepted, reworked):
        await repo.update_task(db, tid, status_entered_at=_ts(4.0))
    await db.commit()

    await services.decide_task(db, accepted, TaskDecide(action="accept"))
    await services.decide_task(
        db, reworked, TaskDecide(action="rework", instructions="redo")
    )

    gates = (await practice_metrics(db))["human_gates"]
    decision = _gate(gates, "decision", "default")
    assert decision["approvals"] == 1
    assert decision["overrides"] == 1
    assert decision["override_rate"] == 0.5
    assert decision["median_wait_hours"] is not None
    assert 3.8 <= decision["median_wait_hours"] <= 4.2


# ---------------------------------------------------------------------------
# Project attribution walks the hierarchy (#747)
# ---------------------------------------------------------------------------


async def _hierarchy_child(db: aiosqlite.Connection, *, project_id: int | None) -> int:
    """task → feature → epic, the epic optionally bound to a project."""
    epic = await repo.create_task(
        db,
        title="epic",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status="open",
        auto_review=False,
        task_type="epic",
        parent_id=None,
        priority="medium",
    )
    if project_id is not None:
        await repo.update_task(db, epic, project_id=project_id)
    feature = await repo.create_task(
        db,
        title="feature",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status="open",
        auto_review=False,
        task_type="feature",
        parent_id=epic,
        priority="medium",
    )
    return await repo.create_task(
        db,
        title="hierarchy child",
        description="",
        runtime="auto",
        source="agent",
        assigned_agent="",
        rationale="",
        status="draft",
        auto_review=False,
        task_type="task",
        parent_id=feature,
        priority="medium",
    )


async def test_child_task_attributed_to_epic_project(db: aiosqlite.Connection):
    # AC-1 (#747): the child has no project_id of its own — the decision
    # must still land under the epic's project, not under default.
    spike = await repo.create_project(db, slug="spike-attr", name="Spike Attr")
    task_id = await _hierarchy_child(db, project_id=spike)
    await repo.insert_event(db, kind="task_approved", task_id=task_id, actor="human")
    await db.commit()

    gates = (await practice_metrics(db))["human_gates"]
    row = _gate(gates, "dor", "spike-attr")
    assert row["approvals"] == 1
    assert not [r for r in gates if r["gate"] == "dor" and r["project"] == "default"], (
        "nothing here belongs to default"
    )


async def test_project_split_sums_to_gate_total(db: aiosqlite.Connection):
    # AC-2 (#747): attribution neither loses nor duplicates decisions.
    spike = await repo.create_project(db, slug="spike-sum", name="Spike Sum")
    in_spike = await _hierarchy_child(db, project_id=spike)
    outside = await _task(db, title="no hierarchy")
    for tid in (in_spike, outside):
        await repo.insert_event(db, kind="task_approved", task_id=tid, actor="human")
    await repo.insert_event(db, kind="task_rejected", task_id=outside, actor="human")
    await db.commit()

    gates = (await practice_metrics(db))["human_gates"]
    dor_rows = [r for r in gates if r["gate"] == "dor"]
    assert sum(r["approvals"] for r in dor_rows) == 2
    assert sum(r["overrides"] for r in dor_rows) == 1


async def test_attribution_matches_resolver_semantics(db: aiosqlite.Connection):
    # AC-3 (#747): outside any epic → default; under a PENDING project →
    # default — exactly the resolve_project_for_task rules, not a copy.
    pending = await repo.create_project(
        db, slug="spike-pending", name="Pending", status="pending"
    )
    under_pending = await _hierarchy_child(db, project_id=pending)
    loose = await _task(db, title="outside hierarchy")
    for tid in (under_pending, loose):
        await repo.insert_event(db, kind="task_approved", task_id=tid, actor="human")
    await db.commit()

    gates = (await practice_metrics(db))["human_gates"]
    row = _gate(gates, "dor", "default")
    assert row["approvals"] == 2
    assert not [r for r in gates if r["project"] == "spike-pending"], (
        "a pending project must not receive attribution"
    )


# --- First-pass acceptance & changes-requested rate (#522) -------------------
#
# Two rates over two denominators: tasks for first-pass, verdicts for the
# changes-requested proportion. What cannot be measured is reported, not
# scored — a verdict whose payload lost its generation says nothing about
# whether the work came back.


async def _verdict(
    db: aiosqlite.Connection,
    task_id: int,
    verdict: str,
    *,
    generation: int | None = 1,
    self_approved: bool = False,
    days_ago: float = 0.0,
) -> None:
    payload: dict = {"verdict": verdict, "self_approved": self_approved}
    if generation is not None:
        payload["submission_generation"] = generation
    event_id = await repo.insert_event(
        db,
        kind="review_verdict_recorded",
        task_id=task_id,
        actor="reviewer",
        payload=payload,
    )
    if days_ago:
        await db.execute(
            "UPDATE events SET created_at = ? WHERE id = ?",
            (_ts(days_ago * 24.0), event_id),
        )


async def test_first_pass_acceptance_and_changes_requested_rate(
    db: aiosqlite.Connection,
):
    # AC-1 (#522): hub_practice_metrics answers both rates, and each carries
    # the counts it was computed from.
    clean = await _task(db, title="approved on the first submission")
    reworked = await _task(db, title="changes requested, then approved")
    also_clean = await _task(db, title="approved on the first submission too")
    stale = await _task(db, title="approved before the window")

    await _verdict(db, clean, "approved")
    await _verdict(db, reworked, "changes_requested", generation=1)
    await _verdict(db, reworked, "approved", generation=2)
    await _verdict(db, also_clean, "approved")
    # Outside the 90d window: neither rate may see it.
    await _verdict(db, stale, "approved", days_ago=120)
    await db.commit()

    outcomes = (await practice_metrics(db))["review_outcomes"]

    assert outcomes["tasks"] == 3
    assert outcomes["first_pass_tasks"] == 2
    assert outcomes["first_pass_acceptance_rate"] == round(2 / 3, 3)
    assert outcomes["verdicts"] == 4
    assert outcomes["approved"] == 3
    assert outcomes["changes_requested"] == 1
    assert outcomes["changes_requested_rate"] == 0.25


async def test_changes_requested_on_first_submission_is_never_first_pass(
    db: aiosqlite.Connection,
):
    # A second verdict on the SAME generation must not launder the first one:
    # the work was sent back, whatever happened next.
    task_id = await _task(db, title="changes requested, then approved as-is")
    await _verdict(db, task_id, "changes_requested", generation=1)
    await _verdict(db, task_id, "approved", generation=1)
    await db.commit()

    outcomes = (await practice_metrics(db))["review_outcomes"]

    assert outcomes["tasks"] == 1
    assert outcomes["first_pass_tasks"] == 0
    assert outcomes["first_pass_acceptance_rate"] == 0.0


async def test_verdict_without_generation_is_reported_not_scored(
    db: aiosqlite.Connection,
):
    # An unreadable generation cannot answer "first time?". The task leaves
    # the first-pass denominator and is counted as unaccounted; its verdict
    # still counts toward the changes-requested rate, which needs no
    # generation.
    unknown = await _task(db, title="verdict without a generation")
    measured = await _task(db, title="approved on the first submission")
    await _verdict(db, unknown, "changes_requested", generation=None)
    await _verdict(db, measured, "approved")
    await db.commit()

    outcomes = (await practice_metrics(db))["review_outcomes"]

    assert outcomes["tasks"] == 1
    assert outcomes["tasks_unaccounted"] == 1
    assert outcomes["first_pass_acceptance_rate"] == 1.0
    assert outcomes["verdicts"] == 2
    assert outcomes["changes_requested_rate"] == 0.5


async def test_self_approved_first_pass_is_counted_separately(
    db: aiosqlite.Connection,
):
    # A submission its own author waved through is not evidence of quality.
    # It stays in the rate (it IS a first-pass acceptance) but is visible
    # beside it, so the number cannot be raised by removing the reviewer.
    solo = await _task(db, title="approved by its own author")
    reviewed = await _task(db, title="approved by someone else")
    await _verdict(db, solo, "approved", self_approved=True)
    await _verdict(db, reviewed, "approved")
    await db.commit()

    outcomes = (await practice_metrics(db))["review_outcomes"]

    assert outcomes["first_pass_tasks"] == 2
    assert outcomes["self_approved_first_pass"] == 1


async def test_empty_window_reports_none_not_zero(db: aiosqlite.Connection):
    # No verdicts is not a 0% acceptance rate. Zero would read as "everything
    # came back", which is the opposite of what an empty sample says.
    outcomes = (await practice_metrics(db))["review_outcomes"]

    assert outcomes["tasks"] == 0
    assert outcomes["verdicts"] == 0
    assert outcomes["first_pass_acceptance_rate"] is None
    assert outcomes["changes_requested_rate"] is None


# --- Cost by the provider's bill, not by self-report (#828) ------------------
#
# On the first live cross-model run the harness reported 175 000 tokens while
# Cursor billed 6 013 569 — a 34x gap (#818). Until now the billed figure was
# fetched for a mismatch alert and dropped, so the practice economics were
# computed from what the reviewed party said about itself.


async def _report(
    db: aiosqlite.Connection,
    task_id: int,
    *,
    confirmed: int = 1,
    tokens_spent: int | None = 1000,
    provider_tokens: int | None = None,
) -> None:
    findings = json.dumps(
        [{"title": f"f{i}", "severity": "medium"} for i in range(confirmed)]
    )
    # The task's own generation moves with the report. In production
    # hub_submit_for_review bumps it and the report is filed against the value
    # it just set, so a task sitting at 0 with a report at 1 is a state nothing
    # produces — and it reads as a SUPERSEDED report to anything that asks
    # which report is current (#1038).
    await db.execute(
        "UPDATE tasks SET submission_generation=1 WHERE id=?",
        (task_id,),
    )
    await repo.insert_machine_review(
        db,
        task_id=task_id,
        submission_generation=1,
        harness_skill="multi-agent-review",
        tokens_spent=tokens_spent,
        raw_count=confirmed,
        findings_confirmed=findings,
        incomplete=False,
    )
    if provider_tokens is not None:
        await repo.set_machine_review_provider_tokens(db, task_id, 1, provider_tokens)


async def test_provider_cost_uses_only_reports_with_provider_data(
    db: aiosqlite.Connection,
):
    # AC-2 (#828): the billed price is computed over the billed rows only,
    # and the share of the sample is named beside it — the #516 rule.
    billed = await _task(db, title="billed run")
    unbilled = await _task(db, title="unbilled run")
    await _report(db, billed, confirmed=2, tokens_spent=1000, provider_tokens=90_000)
    await _report(db, unbilled, confirmed=8, tokens_spent=1000)
    await db.commit()

    mr = (await practice_metrics(db))["machine_reviews"]

    assert mr["reports_with_provider"] == 1
    assert mr["provider_tokens_total"] == 90_000
    # 90 000 over the TWO findings of the billed run, not over all ten.
    assert mr["provider_tokens_per_confirmed"] == 45_000


async def test_self_reported_tokens_never_stand_in_for_provider_data(
    db: aiosqlite.Connection,
):
    # AC-3 (#828): a missing bill is not a cheap run. Substituting the
    # self-report would repeat #516 with a far larger error.
    task_id = await _task(db, title="self-reported only")
    await _report(db, task_id, confirmed=1, tokens_spent=175_000)
    await db.commit()

    mr = (await practice_metrics(db))["machine_reviews"]

    assert mr["reports_with_provider"] == 0
    assert mr["provider_tokens_total"] == 0
    assert mr["provider_tokens_per_confirmed"] is None, (
        "no bill means no billed price — not the self-reported one"
    )
    assert mr["tokens_per_confirmed"] == 175_000, "the self-report stays its own metric"


async def test_both_numbers_stay_visible_when_they_disagree(
    client: AsyncClient, db: aiosqlite.Connection
):
    # AC-4 (#828): the real #818 figures. Neither number corrects the other:
    # it is not established which is wrong, and a metric that quietly picks a
    # winner hides exactly the disagreement worth looking at.
    task_id = await _task(db, title="the #818 shape")
    await _report(
        db, task_id, confirmed=1, tokens_spent=175_000, provider_tokens=6_013_569
    )
    await db.commit()

    mr = (await practice_metrics(db))["machine_reviews"]
    assert mr["tokens_per_confirmed"] == 175_000
    assert mr["provider_tokens_per_confirmed"] == 6_013_569

    page = (await client.get("/metrics")).text
    assert "6013569" in page.replace(" ", "").replace("&nbsp;", "")
    assert "175000" in page.replace(" ", "").replace("&nbsp;", "")


async def _failed_dispatch(
    db: aiosqlite.Connection,
    task_id: int,
    *,
    provider_tokens: int | None = None,
) -> int:
    did = await repo.create_review_dispatch(
        db,
        task_id=task_id,
        submission_generation=1,
        agent_id="bc-waste",
        run_id="run-waste",
        model="grok-4.6",
    )
    if provider_tokens is not None:
        await repo.set_review_dispatch_provider_tokens(db, did, provider_tokens)
    await repo.set_review_dispatch_status(db, did, "failed")
    return did


async def test_wasted_dispatch_spend_is_a_sibling_of_confirmed_price(
    client: AsyncClient, db: aiosqlite.Connection
):
    # AC-3 (#1026): wasted spend is visible and does not enter the price of
    # a confirmed finding. Mixing the two would flatten a dead channel into
    # a cheaper-looking practice (#516).
    billed = await _task(db, title="billed report")
    silent = await _task(db, title="failed dispatch")
    await _report(db, billed, confirmed=2, tokens_spent=1000, provider_tokens=90_000)
    await _failed_dispatch(db, silent, provider_tokens=2_500_000)
    await db.commit()

    metrics = await practice_metrics(db)
    mr = metrics["machine_reviews"]
    rd = metrics["review_dispatches"]

    assert rd["wasted_provider_tokens_total"] == 2_500_000
    assert rd["wasted_dispatches"] == 1
    assert rd["unknown_usage"] == 0
    assert mr["provider_tokens_total"] == 90_000
    assert mr["provider_tokens_per_confirmed"] == 45_000
    assert mr["tokens_per_confirmed"] == 500

    page = (await client.get("/metrics")).text
    assert "2500000" in page.replace(" ", "").replace("&nbsp;", "")
    assert "Сожжено без отчёта" in page


async def test_unknown_dispatch_usage_is_not_a_zero_bill(
    db: aiosqlite.Connection,
):
    # AC-4 (#1026): NULL is unknown, counted beside the sum, never as 0.
    silent = await _task(db, title="failed with no bill")
    billed = await _task(db, title="failed with a bill")
    await _failed_dispatch(db, silent, provider_tokens=None)
    await _failed_dispatch(db, billed, provider_tokens=1_000_000)
    await db.commit()

    rd = (await practice_metrics(db))["review_dispatches"]
    assert rd["wasted_provider_tokens_total"] == 1_000_000
    assert rd["wasted_dispatches"] == 1
    assert rd["unknown_usage"] == 1
    assert rd["closed_dispatches"] == 2


# --- Escaped defects (#528) -------------------------------------------------
#
# The leak side of the ledger: what review did NOT stop. Every test below is
# about the same discipline the rest of this module is about — an exclusion is
# counted and named, never folded into the headline number.


async def _feature(
    db: aiosqlite.Connection,
    *,
    title: str,
    status: str = "completed",
    completed: str | None = "-10 days",
) -> int:
    """A feature, optionally closed without a completion stamp (pre-#517)."""
    feature_id = await repo.create_task(
        db,
        title=title,
        description="",
        runtime="auto",
        source="agent",
        assigned_agent="",
        rationale="",
        status=status,
        auto_review=False,
        task_type="feature",
        parent_id=None,
        priority="medium",
    )
    if completed is None:
        await db.execute("UPDATE tasks SET completed_at=NULL WHERE id=?", (feature_id,))
    else:
        await db.execute(
            "UPDATE tasks SET completed_at=datetime('now', ?) WHERE id=?",
            (completed, feature_id),
        )
    return feature_id


async def _bug(
    db: aiosqlite.Connection,
    *,
    title: str,
    parent_id: int | None,
    created: str = "-1 days",
) -> int:
    bug_id = await repo.create_task(
        db,
        title=title,
        description="",
        runtime="auto",
        source="agent",
        assigned_agent="",
        rationale="",
        status="open",
        auto_review=False,
        task_type="task",
        parent_id=parent_id,
        priority="medium",
    )
    await db.execute(
        "UPDATE tasks SET work_type='bug', created_at=datetime('now', ?) WHERE id=?",
        (created, bug_id),
    )
    return bug_id


async def _escaped(db: aiosqlite.Connection, **kwargs) -> dict:
    """The completed_at reconstruction (#528), kept since #918 as the second,
    labelled number — the tests below are about how it is reconstructed."""
    return (await practice_metrics(db, **kwargs))["escaped_defects"]["reconstructed"]


async def test_bug_after_feature_close_is_escaped(db: aiosqlite.Connection):
    """AC-1: filed after the close, so the gate let it through — and the
    feature is named, because a bare total starts no post-mortem."""
    feature_id = await _feature(db, title="the leaky one", completed="-10 days")
    await _bug(db, title="found in prod", parent_id=feature_id, created="-2 days")
    await _bug(db, title="found again", parent_id=feature_id, created="-1 days")
    await db.commit()

    escaped = await _escaped(db)
    assert escaped["escaped"] == 2
    assert escaped["features"] == [
        {"feature_id": feature_id, "title": "the leaky one", "bugs": 2}
    ]


async def test_bug_before_close_is_not_escaped(db: aiosqlite.Connection):
    """AC-2: a bug found while the feature was still being built is work, not
    a leak — nothing escaped a gate it never passed."""
    feature_id = await _feature(db, title="closed later", completed="-1 days")
    await _bug(
        db, title="found during the work", parent_id=feature_id, created="-5 days"
    )
    await db.commit()

    escaped = await _escaped(db)
    assert escaped["escaped"] == 0
    assert escaped["features"] == []
    assert escaped["bugs_without_feature"] == 0, "it does have a feature"


async def test_bug_without_feature_is_counted_apart(db: aiosqlite.Connection):
    """AC-3: no feature ancestor means no answer, and no answer gets counted.

    33 of 103 production bugs hang under an epic or under nothing at all.
    Dropping them silently would let the metric read as complete coverage.
    """
    await _bug(db, title="orphan bug", parent_id=None, created="-1 days")
    feature_id = await _feature(db, title="attributed", completed="-10 days")
    await _bug(db, title="attributed bug", parent_id=feature_id, created="-1 days")
    await db.commit()

    escaped = await _escaped(db)
    assert escaped["escaped"] == 1
    assert escaped["bugs_without_feature"] == 1
    assert escaped["bugs_in_window"] == 2


async def test_feature_without_completion_stamp_is_counted_not_estimated(
    db: aiosqlite.Connection,
):
    """AC-4: closed but unstamped — the bug is neither an escape nor a
    non-escape, and the missing date is NOT reconstructed from updated_at.

    That substitution is what #810 removed from cycle time. Here it would
    invent escapes wholesale: on production 53 of 82 closed features have no
    stamp, and every bug under them would be dated after a made-up close.
    """
    feature_id = await _feature(db, title="closed before #517", completed=None)
    await _bug(db, title="bug under it", parent_id=feature_id, created="-1 days")
    await db.commit()

    escaped = await _escaped(db)
    assert escaped["escaped"] == 0
    assert escaped["features"] == []
    assert escaped["features_without_completion"] == 1
    assert escaped["bugs_without_feature"] == 0, "the feature is there, its date is not"


async def test_open_feature_is_not_a_measurement_gap(db: aiosqlite.Connection):
    """A feature still in flight has not let anything escape yet, so it is not
    reported as a gap — only a CLOSED feature missing its stamp is."""
    feature_id = await _feature(db, title="still open", status="open", completed=None)
    await _bug(db, title="bug in flight", parent_id=feature_id, created="-1 days")
    await db.commit()

    escaped = await _escaped(db)
    assert escaped["escaped"] == 0
    assert escaped["features_without_completion"] == 0


async def test_window_applies_to_the_bug_date(db: aiosqlite.Connection):
    """AC-5: the window asks what surfaced lately, so it is measured on the
    bug. #518 was a numerator and a window keeping different clocks."""
    feature_id = await _feature(db, title="long closed", completed="-100 days")
    await _bug(db, title="old leak", parent_id=feature_id, created="-60 days")
    await _bug(db, title="fresh leak", parent_id=feature_id, created="-2 days")
    await db.commit()

    assert (await _escaped(db, since_days=30))["escaped"] == 1
    assert (await _escaped(db, since_days=365))["escaped"] == 2


async def test_nearest_feature_gets_the_attribution(db: aiosqlite.Connection):
    """A bug two levels down is attributed to its feature, not lost."""
    feature_id = await _feature(db, title="two levels up", completed="-10 days")
    task_id = await repo.create_task(
        db,
        title="a task under the feature",
        description="",
        runtime="auto",
        source="agent",
        assigned_agent="",
        rationale="",
        status="completed",
        auto_review=False,
        task_type="task",
        parent_id=feature_id,
        priority="medium",
    )
    await _bug(db, title="subtask bug", parent_id=task_id, created="-1 days")
    await db.commit()

    escaped = await _escaped(db)
    assert escaped["escaped"] == 1
    assert escaped["features"][0]["feature_id"] == feature_id


async def test_features_are_ordered_by_how_much_they_leaked(
    db: aiosqlite.Connection,
):
    quiet = await _feature(db, title="one leak", completed="-10 days")
    loud = await _feature(db, title="three leaks", completed="-10 days")
    await _bug(db, title="q1", parent_id=quiet, created="-1 days")
    for n in range(3):
        await _bug(db, title=f"l{n}", parent_id=loud, created="-1 days")
    await db.commit()

    escaped = await _escaped(db)
    assert [f["feature_id"] for f in escaped["features"]] == [loud, quiet]


async def test_metrics_page_shows_escaped_defects(
    client: AsyncClient, db: aiosqlite.Connection
):
    """AC-6: all three numbers on the page, and an empty list says so in words.

    A zero in the leaks column and a zero from having nothing to measure look
    identical to a reader — which is why the uncounted gets its own rows.
    """
    feature_id = await _feature(db, title="leaky feature", completed="-10 days")
    await _bug(db, title="prod bug", parent_id=feature_id, created="-1 days")
    await _feature(db, title="unstamped feature", completed=None)
    await _bug(db, title="orphan", parent_id=None, created="-1 days")
    await db.commit()

    page = (await client.get("/metrics")).text
    assert "Escaped defects" in page
    assert "leaky feature" in page
    assert f"/tasks/{feature_id}" in page
    assert "Багов без фичи-предка" in page
    assert "Закрытых фич без отметки завершения" in page


async def test_page_says_nothing_measurable_instead_of_zero(
    client: AsyncClient, db: aiosqlite.Connection
):
    await _feature(db, title="clean feature", completed="-10 days")
    await db.commit()

    page = (await client.get("/metrics")).text
    assert "нет измеримых утечек в этом окне" in page


# --- Change failure rate and measured escapes (#918) ----------------------
#
# CFR = share of successful deploys in the window with at least one prod defect
# bound to them through tasks.release_id (#917); the denominator travels with
# the share. escaped_defects is measured from found_in='prod' (#909); the
# completed_at walk stays as a separate, labelled reconstruction over bugs
# whose found_in was never recorded.


async def _deploy(
    db: aiosqlite.Connection,
    sha: str,
    *,
    project_id: int | None = None,
    status: str = "success",
    deployed: str = "-1 days",
) -> int:
    release_id = await repo.record_release(
        db, deployed_sha=sha, project_id=project_id, status=status, source="ci"
    )
    await db.execute(
        "UPDATE releases SET deployed_at=datetime('now', ?) WHERE id=?",
        (deployed, release_id),
    )
    return release_id


async def _defect(
    db: aiosqlite.Connection,
    *,
    title: str,
    found_in: str = "prod",
    release_id: int | None = None,
    parent_id: int | None = None,
) -> int:
    task_id = await _bug(db, title=title, parent_id=parent_id)
    await db.execute(
        "UPDATE tasks SET found_in=?, release_id=? WHERE id=?",
        (found_in, release_id, task_id),
    )
    return task_id


def _cfr_row(cfr: dict, project: str) -> dict:
    rows = [r for r in cfr["by_project"] if r["project"] == project]
    assert len(rows) == 1, cfr
    return rows[0]


async def test_change_failure_rate_and_restore_time(db: aiosqlite.Connection):
    """AC-1: the share comes with its denominator, deploys without a project
    belong to the default project (#915), and the defect that makes a deploy
    failed is the same one whose restore clock is measured (#916)."""
    spike = await repo.create_project(db, slug="spike", name="Spike")
    default_id = await repo.create_project(db, slug="default", name="Default")
    releases = [await _deploy(db, f"{i:040x}") for i in range(1, 5)]
    # Stamped with the default project: the same history as project-less rows.
    releases.append(await _deploy(db, "d" * 40, project_id=default_id))
    await _deploy(db, "a" * 40, deployed="-400 days")  # outside the window
    await _deploy(db, "b" * 40, status="failed")  # never reached prod
    other = await _deploy(db, "c" * 40, project_id=spike)

    fixed = await _defect(db, title="broke the first", release_id=releases[0])
    await _defect(db, title="broke it again", release_id=releases[0])
    await _defect(db, title="broke the second", release_id=releases[1])
    await _defect(
        db, title="caught at review", found_in="review", release_id=releases[2]
    )
    await _defect(db, title="release unknown")
    await _defect(db, title="spike broke", release_id=other)
    await db.execute(
        "UPDATE tasks SET detected_at=datetime('now', '-3 hours') WHERE id=?",
        (fixed,),
    )
    await db.commit()
    await repo.update_task(db, fixed, status="completed")
    await db.commit()

    metrics = await practice_metrics(db)
    cfr = metrics["change_failure_rate"]
    default = _cfr_row(cfr, "default")
    assert default["deploys"] == 5, "only successful deploys of the window"
    assert default["failed_deploys"] == 2, "two defects on one deploy count once"
    assert default["rate"] == 0.4
    assert default["small_sample"] is False
    spike_row = _cfr_row(cfr, "spike")
    assert (spike_row["deploys"], spike_row["failed_deploys"]) == (1, 1)
    assert spike_row["small_sample"] is True, "fewer than min_sample deploys"
    assert cfr["min_sample"] == 5
    assert cfr["defects_without_release"] == 1, "counted apart, not dropped"

    restore = metrics["prod_defect_clocks"]["time_to_restore"]
    assert restore["measured"] == 1
    assert 2.9 <= restore["median_hours"] <= 3.1


async def test_escapes_counted_from_found_in(db: aiosqlite.Connection):
    """AC-2: a prod defect without a feature ancestor is a measured escape,
    not a bug lost in bugs_without_feature."""
    await _defect(db, title="prod, under nothing")
    await _defect(db, title="caught at review", found_in="review")
    await _bug(db, title="old orphan, stage unknown", parent_id=None)
    await db.commit()

    escaped = (await practice_metrics(db))["escaped_defects"]
    assert escaped["escaped"] == 1
    assert escaped["source"] == "found_in"
    reconstructed = escaped["reconstructed"]
    assert reconstructed["bugs_in_window"] == 1, "only bugs without found_in"
    assert reconstructed["bugs_without_feature"] == 1, "the unknown orphan only"


async def test_reconstructed_escapes_labelled(
    client: AsyncClient, db: aiosqlite.Connection
):
    """AC-3: the reconstructed number carries its label and sits apart from
    the measured one — on the data, on the page and in the MCP text."""
    feature_id = await _feature(db, title="closed feature", completed="-10 days")
    await _bug(db, title="old bug, stage unknown", parent_id=feature_id)
    await _defect(db, title="prod defect", parent_id=feature_id)
    await _defect(db, title="prod defect, no feature")
    await db.commit()

    escaped = (await practice_metrics(db))["escaped_defects"]
    assert escaped["escaped"] == 2
    assert escaped["reconstructed"]["escaped"] == 1
    assert escaped["reconstructed"]["label"] == "реконструкция"

    api = (await client.get("/api/metrics/practices")).json()["escaped_defects"]
    assert (api["escaped"], api["reconstructed"]["escaped"]) == (2, 1)
    assert api["reconstructed"]["label"] == "реконструкция"

    econ = (await practice_metrics(db))["review_economy"]["escapes"]
    assert econ["escaped"] == 2
    assert econ["reconstructed"]["escaped"] == 1

    page = (await client.get("/metrics")).text
    assert 'data-metric="escaped_defects.escaped">2<' in page
    assert 'data-metric="escaped_defects.reconstructed.escaped">1<' in page
    assert "Утечек в прод, измерено" in page
    assert "Реконструкция" in page


async def test_metrics_page_shows_change_failure_rate(
    client: AsyncClient, db: aiosqlite.Connection
):
    """Under min_sample deploys the page prints the mark, not a share."""
    release_id = await _deploy(db, "e" * 40)
    await _defect(db, title="broke it", release_id=release_id)
    await db.commit()

    page = (await client.get("/metrics")).text
    assert "Change failure rate" in page
    assert 'data-metric="change_failure_rate.default.deploys">1<' in page
    assert "малая выборка" in page
    # The CFR table only: the shift-left table above it (#914) rightly shows
    # this one defect as 100% of the window's prod stage.
    cfr_section = page.split("Change failure rate (#918)", 1)[1].split("<h2>", 1)[0]
    assert "100.0%" not in cfr_section


async def test_mcp_practice_metrics_names_cfr_and_both_escapes():
    from unittest.mock import AsyncMock, patch

    from hub.mcp_server import hub_practice_metrics

    data = {
        "since_days": 90,
        "change_failure_rate": {
            "min_sample": 5,
            "defects_without_release": 1,
            "by_project": [
                {
                    "project": "default",
                    "deploys": 5,
                    "failed_deploys": 2,
                    "rate": 0.4,
                    "small_sample": False,
                },
                {
                    "project": "spike",
                    "deploys": 1,
                    "failed_deploys": 1,
                    "rate": 1.0,
                    "small_sample": True,
                },
            ],
        },
        "escaped_defects": {
            "escaped": 3,
            "source": "found_in",
            "reconstructed": {"label": "реконструкция", "escaped": 7},
        },
    }
    with patch("hub.mcp_server._api_get", new_callable=AsyncMock) as get:
        get.return_value = data
        result = await hub_practice_metrics()
    text = result.content[0].text
    assert (
        "Change failure rate: default 40.0% (2/5 deploys); "
        "spike small sample (1/1 deploys); 1 prod defect(s) without release"
    ) in text
    assert "Escaped to prod (measured, found_in): 3; reconstructed" in text
    assert "реконструкция: 7" in text


def test_cli_change_failure_rate_prints_the_section(capsys):
    import sys
    from unittest.mock import patch

    from hub import cli

    cfr = {"min_sample": 5, "by_project": [], "defects_without_release": 0}
    with (
        patch.object(
            sys, "argv", ["oc-hub", "change-failure-rate", "--since-days", "30"]
        ),
        patch.object(
            cli, "_api", return_value={"since_days": 30, "change_failure_rate": cfr}
        ) as api,
    ):
        rc = cli.main()
    assert rc in (0, None)
    assert api.call_args.args[:2] == ("GET", "/api/metrics/practices?since_days=30")
    assert json.loads(capsys.readouterr().out) == cfr


# --- Shift-left: where defects were caught (#914) ---------------------------
#
# A defect is a bug, or any task with a recorded found_in. Every stage gets a
# row, empty ones too; unknown is not folded into the others and has its own
# share, because a distribution over "only the rows someone filled in" reads as
# a finished picture when it is not.


async def test_shift_left_distribution(client: AsyncClient, db: aiosqlite.Connection):
    """#914 AC-2: the distribution by stage, with the unknown share apart —
    in the data, on the page and in the API."""
    await _defect(db, title="caught at review", found_in="review")
    await _defect(db, title="caught at review again", found_in="review")
    await _defect(db, title="caught by CI", found_in="ci")
    await _defect(db, title="escaped", found_in="prod")
    await _bug(db, title="stage never recorded", parent_id=None)
    await _task(db, title="a feature, stage unknown by default")  # no defect
    caught = await _task(db, title="a feature whose test run caught a defect")
    await db.execute("UPDATE tasks SET found_in='test' WHERE id=?", (caught,))
    old = await _defect(db, title="outside the window", found_in="staging")
    await db.execute(
        "UPDATE tasks SET created_at=datetime('now', '-400 days') WHERE id=?", (old,)
    )
    await db.commit()

    metrics = await practice_metrics(db)
    sl = metrics["shift_left"]
    assert sl["defects"] == 6, "bugs, plus any task with a recorded stage"
    assert [r["stage"] for r in sl["by_stage"]] == [
        "review",
        "ci",
        "test",
        "staging",
        "prod",
        "unknown",
    ]
    by = {r["stage"]: r for r in sl["by_stage"]}
    assert (by["review"]["defects"], by["review"]["share"]) == (2, 0.333)
    assert (by["ci"]["defects"], by["test"]["defects"]) == (1, 1)
    assert by["staging"]["defects"] == 0, "the window applies to created_at"
    assert by["prod"]["defects"] == metrics["escaped_defects"]["escaped"] == 1
    assert (by["unknown"]["defects"], sl["unknown"]) == (1, 1)
    assert sl["unknown_share"] == 0.167
    assert sl["recorded"] == 5

    api = (await client.get("/api/metrics/practices")).json()["shift_left"]
    assert api["unknown_share"] == 0.167

    page = (await client.get("/metrics")).text
    assert "Shift-left" in page
    assert 'data-metric="shift_left.review.defects">2<' in page
    assert 'data-metric="shift_left.unknown_share">16.7%<' in page


async def test_shift_left_empty_window_has_no_shares(db: aiosqlite.Connection):
    """No defects is not "0% unknown": the share is None, not zero."""
    sl = (await practice_metrics(db))["shift_left"]
    assert sl["defects"] == 0
    assert sl["unknown_share"] is None
    assert all(r["share"] is None for r in sl["by_stage"])


async def test_mcp_practice_metrics_names_shift_left():
    from unittest.mock import AsyncMock, patch

    from hub.mcp_server import hub_practice_metrics

    data = {
        "since_days": 90,
        "shift_left": {
            "defects": 5,
            "recorded": 4,
            "unknown": 1,
            "unknown_share": 0.2,
            "by_stage": [
                {"stage": "review", "defects": 2, "share": 0.4},
                {"stage": "ci", "defects": 1, "share": 0.2},
                {"stage": "test", "defects": 0, "share": 0.0},
                {"stage": "staging", "defects": 0, "share": 0.0},
                {"stage": "prod", "defects": 1, "share": 0.2},
                {"stage": "unknown", "defects": 1, "share": 0.2},
            ],
        },
    }
    with patch("hub.mcp_server._api_get", new_callable=AsyncMock) as get:
        get.return_value = data
        result = await hub_practice_metrics()
    text = result.content[0].text
    assert (
        "Shift-left (defects by found_in, 5): review 2, ci 1, test 0, "
        "staging 0, prod 1; unknown 1 (20.0%)"
    ) in text


def test_cli_shift_left_prints_the_section(capsys):
    import sys
    from unittest.mock import patch

    from hub import cli

    sl = {"defects": 0, "recorded": 0, "unknown": 0, "unknown_share": None}
    with (
        patch.object(sys, "argv", ["oc-hub", "shift-left", "--since-days", "30"]),
        patch.object(
            cli, "_api", return_value={"since_days": 30, "shift_left": sl}
        ) as api,
    ):
        rc = cli.main()
    assert rc in (0, None)
    assert api.call_args.args[:2] == ("GET", "/api/metrics/practices?since_days=30")
    assert json.loads(capsys.readouterr().out) == sl


# --- What the findings turned out to be (#877, on #876's data) ---------------
#
# Until this, review quality was measured by the review itself. precision and
# resolution answer a different question — whether the findings were real, and
# whether anyone acted on them.


async def _judged(
    db: aiosqlite.Connection,
    task_id: int,
    dispositions: list[str],
    *,
    profile: str = "lite",
    model: str = "grok-4.6",
    tokens_spent: int | None = 1000,
    provider_tokens: int | None = None,
) -> None:
    """A report with `len(dispositions)` confirmed findings, each judged."""
    await _report(
        db,
        task_id,
        confirmed=len(dispositions),
        tokens_spent=tokens_spent,
        provider_tokens=provider_tokens,
    )
    row = await repo.get_latest_machine_review(db, task_id)
    await db.execute(
        "UPDATE machine_reviews SET profile = ?, model = ? WHERE id = ?",
        (profile, model, row["id"]),
    )
    for index, disposition in enumerate(dispositions):
        await repo.upsert_finding_disposition(
            db,
            review_id=int(row["id"]),
            task_id=task_id,
            submission_generation=1,
            finding_index=index,
            finding_uid=f"uid-{index}",
            finding_title=f"f{index}",
            disposition=disposition,
            note="",
            decided_by="owner",
        )


async def test_precision_and_resolution_by_profile(db: aiosqlite.Connection):
    # AC-1 (#877): both rates, split by profile AND by reviewer model, with the
    # sample size beside each — a precision of 100% over two findings is not a
    # verdict on a profile.
    cheap = await _task(db, title="cheap run")
    await _judged(
        db, cheap, ["fixed", "false_positive"], profile="lite", model="grok-4.6"
    )
    deep = await _task(db, title="deep run")
    await _judged(
        db, deep, ["fixed", "wont_fix", "fixed"], profile="deep", model="gpt-5.3-codex"
    )
    await db.commit()

    metrics = await practice_metrics(db)
    disp = metrics["machine_reviews"]["dispositions"]

    # A defect nobody chose to fix is still a defect the reviewer found: only
    # false_positive counts against precision.
    assert disp["judged"] == 5 and disp["fixed"] == 3 and disp["false_positive"] == 1
    assert disp["precision"] == round(4 / 5, 3)
    assert disp["resolution_rate"] == round(3 / 5, 3)

    by_profile = {row["profile"]: row for row in metrics["by_profile"]}
    assert by_profile["lite"]["precision"] == 0.5
    assert by_profile["lite"]["judged"] == 2
    assert by_profile["deep"]["precision"] == 1.0
    assert by_profile["deep"]["resolution_rate"] == round(2 / 3, 3)

    by_model = {row["model"]: row for row in metrics["by_reviewer_model"]}
    assert by_model["grok-4.6"]["judged"] == 2
    assert by_model["gpt-5.3-codex"]["resolution_rate"] == round(2 / 3, 3)


async def test_reports_without_disposition_counted_apart(db: aiosqlite.Connection):
    # AC-2 (#877): an unjudged report is neither a hit nor a miss. Folding it
    # in as a zero would make an unanswered question look like a false
    # positive, and the coverage of the loop would stop being visible (#841).
    judged = await _task(db, title="judged run")
    await _judged(db, judged, ["fixed"])
    silent = await _task(db, title="nobody judged this")
    await _report(db, silent, confirmed=4)
    await db.commit()

    mr = (await practice_metrics(db))["machine_reviews"]
    disp = mr["dispositions"]

    assert disp["judged"] == 1, "only the judged finding is in the denominator"
    assert disp["precision"] == 1.0
    assert disp["reports_judged"] == 1 and disp["reports_unjudged"] == 1
    assert disp["confirmed_unjudged"] == 4, "the four unanswered findings stay visible"


async def test_tokens_per_fixed_takes_both_ends_from_the_same_rows(
    db: aiosqlite.Connection,
):
    # The #516 rule, applied to the honest price: a report that reported no
    # tokens contributes neither its tokens nor its fixed findings, and a
    # report nobody judged contributes nothing at all.
    priced = await _task(db, title="priced and judged")
    await _judged(db, priced, ["fixed", "fixed"], tokens_spent=100_000)
    unpriced = await _task(db, title="judged but unpriced")
    await _judged(db, unpriced, ["fixed"], tokens_spent=None)
    unjudged = await _task(db, title="priced but unjudged")
    await _report(db, unjudged, confirmed=5, tokens_spent=900_000)
    await db.commit()

    mr = (await practice_metrics(db))["machine_reviews"]

    assert mr["tokens_per_fixed"] == 50_000, "100k over ITS two fixed findings"
    assert mr["provider_tokens_per_fixed"] is None, "no bill means no billed price"


async def test_no_dispositions_reads_as_no_data(
    client: AsyncClient, db: aiosqlite.Connection
):
    # AC-3 (#877): a window nobody judged shows "нет данных", never 0% and
    # never 100%. Both readings would be a verdict the data cannot support.
    task_id = await _task(db, title="unjudged")
    await _report(db, task_id, confirmed=2)
    await db.commit()

    mr = (await practice_metrics(db))["machine_reviews"]
    assert mr["dispositions"]["precision"] is None
    assert mr["dispositions"]["resolution_rate"] is None
    assert mr["tokens_per_fixed"] is None

    page = (await client.get("/metrics")).text
    assert "ни одна находка не размечена" in page
    assert "сравнивать" in page, "the model table says why it is empty"


# --- The flywheel: a recurring class must become a check (#878) --------------
#
# recurring_categories has counted repeats since #384 and closed nothing. A
# class found in three tasks is still hunted by a model, at full price, on the
# fourth — the one cost in this economy that never has to be paid again.


async def _categorised(db: aiosqlite.Connection, title: str, categories: list[str]):
    """One report whose confirmed findings carry these categories."""
    task_id = await _task(db, title=title)
    await repo.insert_machine_review(
        db,
        task_id=task_id,
        submission_generation=1,
        harness_skill="multi-agent-review",
        tokens_spent=1000,
        raw_count=len(categories),
        findings_confirmed=json.dumps(
            [
                {"title": f"f{i}", "severity": "medium", "category": c}
                for i, c in enumerate(categories)
            ]
        ),
        incomplete=False,
    )


async def test_recurring_category_becomes_debt(db: aiosqlite.Connection):
    # AC-1 (#878): a category seen in three DISTINCT tasks lands in the debt
    # list marked as uncovered. Below the threshold it does not — the list has
    # to stay short enough to be read.
    for i in range(3):
        await _categorised(db, f"task {i}", ["timeouts"])
    await _categorised(db, "one-off", ["style"])
    # Ten hits inside ONE task are a fact about that task, not about the repo.
    await _categorised(db, "sprawling", ["naming"] * 10)
    await db.commit()

    debt = (await practice_metrics(db))["category_debt"]

    by_category = {row["category"]: row for row in debt}
    assert set(by_category) == {"timeouts"}, (
        "three tasks buy the debt; ten findings in one task do not"
    )
    assert by_category["timeouts"]["tasks"] == 3
    assert by_category["timeouts"]["covered"] is False
    assert by_category["timeouts"]["check_ref"] == ""


async def test_covered_category_leaves_debt_with_link(
    client: AsyncClient, db: aiosqlite.Connection
):
    # AC-2 (#878): closing a category needs the NAME of the check. The category
    # stays listed as covered rather than vanishing — a list that drops what
    # was closed cannot show that anything ever gets closed.
    for i in range(3):
        await _categorised(db, f"task {i}", ["timeouts"])
    await db.commit()

    resp = await client.post(
        "/api/metrics/category-checks",
        json={
            "category": "timeouts",
            "check_ref": "tests/test_review_dispatch.py::test_exhausted_lite",
        },
    )
    assert resp.status_code == 200, resp.text

    row = next(
        r
        for r in (await practice_metrics(db))["category_debt"]
        if r["category"] == "timeouts"
    )
    assert row["covered"] is True
    assert row["check_ref"].endswith("::test_exhausted_lite")

    page = (await client.get("/metrics")).text
    assert "test_exhausted_lite" in page
    assert "проверка не заведена" not in page


async def test_closing_a_category_without_naming_a_check_is_refused(
    client: AsyncClient, db: aiosqlite.Connection
):
    # A category closed by a tick is a category nobody covered: the debt list
    # would shrink while the token bill stayed exactly where it was.
    for i in range(3):
        await _categorised(db, f"task {i}", ["timeouts"])
    await db.commit()

    resp = await client.post(
        "/api/metrics/category-checks",
        json={"category": "timeouts", "check_ref": "   "},
    )

    assert resp.status_code in (400, 422)
    row = next(
        r
        for r in (await practice_metrics(db))["category_debt"]
        if r["category"] == "timeouts"
    )
    assert row["covered"] is False, "an unnamed check closes nothing"


async def test_debt_blocks_nothing_and_says_so_when_empty(
    client: AsyncClient, db: aiosqlite.Connection
):
    # A gate that shouts off-target stops being read, and then the real signal
    # is the one that gets missed. The list informs; it never blocks.
    task_id = await _task(db, title="clean")
    before = dict(await repo.get_task(db, task_id))["status"]
    for i in range(3):
        await _categorised(db, f"debt task {i}", ["timeouts"])
    await db.commit()

    debt = (await practice_metrics(db))["category_debt"]
    assert debt and debt[0]["covered"] is False, "the debt is real and open"
    # ...and nothing about the work moved because of it.
    assert dict(await repo.get_task(db, task_id))["status"] == before

    # The empty case says why it is empty rather than rendering a blank table.
    async with aiosqlite.connect(":memory:") as fresh:
        fresh.row_factory = aiosqlite.Row
        await fresh.executescript(_SCHEMA)
        await _migrate(fresh)
        assert (await practice_metrics(fresh))["category_debt"] == []


# --- A rule that did not hold (#920) ------------------------------------------
#
# A class closed by a named check (#878) should stop coming back. Whether it
# did is only visible against the date the rule was set up: a finding of the
# class AFTER that date is a breach, one BEFORE it is the history the rule was
# written from.


async def _finding_at(db, title: str, category: str, when: str) -> int:
    task_id = await _task(db, title=title)
    await repo.insert_machine_review(
        db,
        task_id=task_id,
        submission_generation=1,
        harness_skill="multi-agent-review",
        raw_count=1,
        findings_confirmed=json.dumps(
            [{"title": title, "severity": "high", "category": category}]
        ),
        incomplete=False,
    )
    await db.execute(
        "UPDATE machine_reviews SET created_at=? WHERE task_id=?", (when, task_id)
    )
    return task_id


async def _rule_set_up(db, category: str, when: str) -> None:
    await services.record_category_check(
        db, category=category, check_ref=f"tests/test_x.py::test_{category}"
    )
    await db.execute(
        "UPDATE category_checks SET created_at=?, recorded_at=? WHERE category=?",
        (when, when, category),
    )
    await db.commit()


async def test_repeat_after_rule_is_reported(
    client: AsyncClient, db: aiosqlite.Connection
):
    # AC-3: the class came back after its check was named; the report shows the
    # rule as breached, with the date it was set up.
    rule_date = _ts(48)
    await _finding_at(db, "before", "timeouts", _ts(72))
    await _rule_set_up(db, "timeouts", rule_date)
    after = await _finding_at(db, "after", "timeouts", _ts(24))
    await db.commit()

    report = (await practice_metrics(db))["rule_breaches"]
    assert report["rules_total"] == 1
    [row] = report["breached"]
    assert row["category"] == "timeouts"
    assert row["check_ref"] == "tests/test_x.py::test_timeouts"
    assert row["rule_created_at"] == rule_date
    assert row["breaches"] == 1
    assert row["task_ids"] == [after]

    # Re-recording a better check replaces the check, not the date the rule
    # was set up: the class was already promised closed from the first one.
    await services.record_category_check(
        db, category="timeouts", check_ref="tests/test_y.py::test_better"
    )
    [again] = (await practice_metrics(db))["rule_breaches"]["breached"]
    assert again["rule_created_at"] == rule_date
    assert again["check_ref"] == "tests/test_y.py::test_better"

    page = (await client.get("/metrics")).text
    assert "test_better" in page and rule_date[:10] in page

    from unittest.mock import AsyncMock, patch

    from hub import mcp_server

    data = await practice_metrics(db)
    with patch.object(mcp_server, "_api_get", AsyncMock(return_value=data)):
        result = await mcp_server.hub_practice_metrics()
    text = result.content[0].text
    assert "timeouts" in text and rule_date[:10] in text


async def test_repeat_before_rule_is_not_a_breach(db: aiosqlite.Connection):
    # AC-4: findings of the class from before the rule are what the rule was
    # written from — counting them would call every new rule broken on day one.
    await _finding_at(db, "one", "timeouts", _ts(96))
    await _finding_at(db, "two", "timeouts", _ts(72))
    await _rule_set_up(db, "timeouts", _ts(48))
    # Another class after the date breaches nothing of this rule.
    await _finding_at(db, "other", "naming", _ts(24))
    await db.commit()

    report = (await practice_metrics(db))["rule_breaches"]
    assert report["rules_total"] == 1
    assert report["breached"] == []


async def test_carried_over_copy_is_not_a_second_breach(db: aiosqlite.Connection):
    # A report re-attached over a base-only merge (#1361) is the same finding,
    # not another return of the class.
    await _rule_set_up(db, "timeouts", _ts(48))
    await _finding_at(db, "after", "timeouts", _ts(24))
    [original] = await db.execute_fetchall("SELECT id FROM machine_reviews")
    await repo.insert_machine_review(
        db,
        task_id=1,
        submission_generation=2,
        harness_skill="multi-agent-review",
        raw_count=1,
        findings_confirmed=json.dumps([{"title": "after", "category": "timeouts"}]),
        incomplete=False,
    )
    await db.execute(
        "UPDATE machine_reviews SET carried_from_review_id=? WHERE id != ?",
        (original["id"], original["id"]),
    )
    await db.commit()

    [row] = (await practice_metrics(db))["rule_breaches"]["breached"]
    assert row["breaches"] == 1


async def test_rule_date_migration_on_clean_and_populated_base():
    # #920 schema: a base with rules from before the column gets their set-up
    # date from the earliest record event, never later than recorded_at; a
    # second run changes nothing; a clean base just gets the column.
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    try:
        await conn.executescript(_SCHEMA)
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS _migrations (name TEXT PRIMARY KEY, "
            "applied_at TEXT DEFAULT (datetime('now')))"
        )
        for name, sql in _MIGRATIONS:
            if name == "add_category_checks_created_at":
                break
            try:
                await conn.execute(sql)
            except Exception:  # noqa: BLE001 - column already in _SCHEMA
                pass
            await conn.execute("INSERT INTO _migrations (name) VALUES (?)", (name,))
        await conn.execute(
            "INSERT INTO category_checks (category, check_ref, recorded_at) "
            "VALUES ('timeouts', 'a', '2026-08-20 10:00:00'), "
            "('naming', 'b', '2026-08-21 10:00:00')"
        )
        await conn.execute(
            "INSERT INTO events (kind, payload, created_at) VALUES "
            "('category_check_recorded', '{\"category\": \"timeouts\"}', "
            "'2026-08-01 09:00:00'), "
            "('category_check_recorded', 'not json', '2026-07-01 09:00:00')"
        )
        await conn.commit()
        await _migrate(conn)
        await _migrate(conn)
        rows = {
            r["category"]: r["created_at"]
            for r in await conn.execute_fetchall(
                "SELECT category, created_at FROM category_checks"
            )
        }
        assert rows == {
            "timeouts": "2026-08-01 09:00:00",
            "naming": "2026-08-21 10:00:00",
        }
    finally:
        await conn.close()

    async with aiosqlite.connect(":memory:") as fresh:
        fresh.row_factory = aiosqlite.Row
        await fresh.executescript(_SCHEMA)
        await _migrate(fresh)
        await repo.upsert_category_check(fresh, category="x", check_ref="c")
        [row] = await fresh.execute_fetchall("SELECT * FROM category_checks")
        assert row["created_at"], "a new rule is dated on its first record"


async def test_provider_cost_per_run_split_by_profile(db: aiosqlite.Connection):
    # AC-4 (#893): the number a profile decision rests on is what ONE run of
    # it bills. Measured, lite averaged 1.38M and deep 3.85M — a 2.8x gap,
    # not the 5x the self-reported tokens suggested. The sample size travels
    # with the average because two billed runs and two hundred are different
    # grounds for the same decision (#516).
    lite_a = await _task(db, title="lite one")
    lite_b = await _task(db, title="lite two")
    deep_one = await _task(db, title="deep one")
    unbilled = await _task(db, title="lite with no bill")
    await _report(db, lite_a, tokens_spent=25_000, provider_tokens=800_000)
    await _report(db, lite_b, tokens_spent=36_000, provider_tokens=1_600_000)
    await _report(db, deep_one, tokens_spent=104_000, provider_tokens=3_900_000)
    await _report(db, unbilled, tokens_spent=31_000)
    for task_id, profile in (
        (lite_a, "lite"),
        (lite_b, "lite"),
        (deep_one, "deep"),
        (unbilled, "lite"),
    ):
        await db.execute(
            "UPDATE machine_reviews SET profile=? WHERE task_id=?", (profile, task_id)
        )
    await db.commit()

    metrics = await services.practice_metrics(db)
    by_profile = {row["profile"]: row for row in metrics["by_profile"]}

    assert by_profile["lite"]["provider_tokens_per_run"] == 1_200_000
    # Three lite runs, two of them billed: the average must be over the two,
    # and the count must say so instead of quietly averaging a zero in.
    assert by_profile["lite"]["billed_runs"] == 2
    assert by_profile["deep"]["provider_tokens_per_run"] == 3_900_000


async def test_profile_with_no_bill_reports_unknown_not_zero(
    db: aiosqlite.Connection,
):
    # A profile nobody has a bill for costs an unknown amount, not nothing.
    # Zero here would read as "free" and make the cheapest profile the one we
    # simply never measured (#725).
    task_id = await _task(db, title="unbilled profile")
    await _report(db, task_id, tokens_spent=12_000)
    await db.execute(
        "UPDATE machine_reviews SET profile='lite' WHERE task_id=?", (task_id,)
    )
    await db.commit()

    metrics = await services.practice_metrics(db)
    lite = {row["profile"]: row for row in metrics["by_profile"]}["lite"]

    assert lite["provider_tokens_per_run"] is None
    assert lite["billed_runs"] == 0


# ---------------------------------------------------------------------------
# Human touches on delivered tasks (#1009)
# ---------------------------------------------------------------------------


async def _deliver(db: aiosqlite.Connection, task_id: int, *, pr: int) -> None:
    """A merge the hub performed: the denominator of the touch metric."""
    await db.execute(
        "INSERT INTO pipeline_merges (project_id, pr_number, task_id, merge_sha) "
        "VALUES (NULL, ?, ?, ?)",
        (pr, task_id, f"sha-{pr}"),
    )


async def test_touches_share_one_task_set(db: aiosqlite.Connection):
    # AC-2 (#1009): numerator and denominator come from the delivered set.
    # An undelivered task with plenty of human events must not enter either.
    shipped = await _task(db, title="delivered with two touches")
    also_shipped = await _task(db, title="delivered with none")
    still_open = await _task(db, title="undelivered with five touches")
    await _deliver(db, shipped, pr=101)
    await _deliver(db, also_shipped, pr=102)
    await repo.insert_event(db, kind="task_approved", task_id=shipped, actor="human")
    await repo.insert_event(
        db,
        kind="review_verdict_recorded",
        task_id=shipped,
        actor="reviewer",
        payload={"verdict": "approved"},
    )
    for _ in range(5):
        await repo.insert_event(
            db, kind="task_approved", task_id=still_open, actor="human"
        )
    await db.commit()

    touches = (await practice_metrics(db))["human_touches"]
    assert touches["delivered_tasks"] == 2, (
        "undelivered work must not pad the denominator"
    )
    assert touches["touches"] == 2, (
        "touches on undelivered tasks must not pad the numerator"
    )
    assert touches["touches_per_delivered"] == 1.0


async def test_machine_actors_are_not_touches(db: aiosqlite.Connection):
    # AC-3 (#1009): hub and policy on a delivered task are not human touches,
    # and they must not create a human touch that the gate metric would also
    # refuse. The filter is the same set of actors.
    shipped = await _task(db, title="delivered mixed actors")
    await _deliver(db, shipped, pr=201)
    await repo.insert_event(db, kind="task_approved", task_id=shipped, actor="hub")
    await repo.insert_event(db, kind="task_approved", task_id=shipped, actor="policy")
    await repo.insert_event(db, kind="task_approved", task_id=shipped, actor="human")
    await repo.insert_event(
        db,
        kind="review_verdict_recorded",
        task_id=shipped,
        actor="policy",
        payload={"verdict": "approved"},
    )
    await db.commit()

    touches = (await practice_metrics(db))["human_touches"]
    assert touches["delivered_tasks"] == 1
    assert touches["touches"] == 1, "hub and policy must not count as touches"


def test_gate_event_vocabulary_is_single_source():
    # AC-4 (#1009): the kind list lives in one place. Queries import it; they
    # must not restype the same strings into a second IN-list.
    import inspect

    from hub.services.gate_events import HUMAN_GATE_EVENT_KINDS, NON_HUMAN_GATE_ACTORS
    from hub.services import orchestration

    assert HUMAN_GATE_EVENT_KINDS == frozenset(
        {
            "task_approved",
            "task_rejected",
            "review_verdict_recorded",
            "task_decided",
            "audit_result",
            "disposition_recorded",
            "steward_judgement",
            "steward_applied",
            "steward_escalated",
        }
    )
    assert "unknown" not in HUMAN_GATE_EVENT_KINDS
    assert NON_HUMAN_GATE_ACTORS == frozenset({"hub", "policy", "steward"})

    gate_src = inspect.getsource(orchestration._human_gate_metrics)
    touch_src = inspect.getsource(orchestration._human_touch_metrics)
    assert "HUMAN_GATE_EVENT_KINDS" in gate_src
    assert "HUMAN_GATE_EVENT_KINDS" in touch_src
    assert "NON_HUMAN_GATE_ACTORS" in gate_src
    assert "NON_HUMAN_GATE_ACTORS" in touch_src
    assert "task_approved', 'task_rejected'" not in gate_src
    assert "task_approved', 'task_rejected'" not in touch_src


# ---------------------------------------------------------------------------
# Steward audit: events, author_kind, own metric row (#1023)
# ---------------------------------------------------------------------------


async def test_steward_records_are_never_human(db: aiosqlite.Connection):
    # AC-1 (#1023): a recorded judgement is steward in the feed and the
    # timeline. Neither the event nor the update looks like a human click
    # or a hub write (#559).
    task_id = await _task(db, title="steward judgement feed")
    await record_steward_judgement(
        db,
        task_id,
        StewardJudgementSubmit(
            generation=1,
            kind="verdict",
            verdict="approve",
            confidence="high",
            grounds=[StewardGround(source="ci_pinned_sha")],
        ),
        TokenIdentity("steward-bot", "steward"),
    )

    events = [dict(e) for e in await repo.list_events(db, since=0)]
    on_task = [e for e in events if e["task_id"] == task_id]
    kinds = {e["kind"] for e in on_task}
    assert "steward_judgement" in kinds
    assert "steward_applied" in kinds
    for event in on_task:
        if event["kind"].startswith("steward_"):
            assert event["actor"] == "steward"
            assert event["actor"] not in {"human", "hub"}

    updates = [dict(u) for u in await repo.get_task_updates(db, task_id)]
    steward_updates = [u for u in updates if u["author_kind"] == "steward"]
    assert steward_updates, f"expected author_kind=steward, got {updates}"
    assert all(u["author_kind"] not in {"human", "hub"} for u in steward_updates)

    escalated_id = await _task(db, title="steward escalate feed")
    await record_steward_judgement(
        db,
        escalated_id,
        StewardJudgementSubmit(
            generation=1,
            kind="verdict",
            verdict="approve",
            confidence="low",
            grounds=[StewardGround(source="ci_pinned_sha")],
        ),
        TokenIdentity("steward-bot", "steward"),
    )
    esc_kinds = {
        e["kind"]
        for e in await repo.list_events(db, since=0)
        if e["task_id"] == escalated_id
    }
    assert "steward_judgement" in esc_kinds
    assert "steward_escalated" in esc_kinds
    assert "steward_applied" not in esc_kinds


async def test_steward_excluded_from_human_gates(db: aiosqlite.Connection):
    # AC-2 (#1023): steward judgements do not enter the numerator or the
    # denominator of human gates — same exclusion as hub and policy.
    human = await _task(db, title="human dor")
    await repo.insert_event(db, kind="task_approved", task_id=human, actor="human")
    await repo.insert_event(db, kind="task_rejected", task_id=human, actor="human")
    await db.commit()

    before = [
        r for r in (await practice_metrics(db))["human_gates"] if r["gate"] != "steward"
    ]

    judged = await _task(db, title="steward beside human")
    await repo.insert_event(
        db, kind="steward_judgement", task_id=judged, actor="steward"
    )
    await repo.insert_event(db, kind="steward_applied", task_id=judged, actor="steward")
    await repo.insert_event(
        db,
        kind="review_verdict_recorded",
        task_id=judged,
        actor="steward",
        payload={"verdict": "approved"},
    )
    await db.commit()

    after_all = (await practice_metrics(db))["human_gates"]
    after = [r for r in after_all if r["gate"] != "steward"]
    assert after == before, "steward must not move human-gate counts"
    dor = _gate(after_all, "dor", "default")
    assert dor["approvals"] == 1
    assert dor["overrides"] == 1


async def test_steward_gate_row_exists(client: AsyncClient, db: aiosqlite.Connection):
    # AC-3 (#1023): applied, escalated, and a human change of outcome land
    # on gate=steward — not on a human gate.
    applied = await _task(db, title="steward applied")
    escalated = await _task(db, title="steward escalated")
    overridden = await _task(db, title="steward then human reject")
    await repo.insert_event(
        db, kind="steward_applied", task_id=applied, actor="steward"
    )
    await repo.insert_event(
        db, kind="steward_escalated", task_id=escalated, actor="steward"
    )
    applied_id = await repo.insert_event(
        db, kind="steward_applied", task_id=overridden, actor="steward"
    )
    await db.execute(
        "UPDATE events SET created_at=? WHERE id=?",
        (_ts(2.0), applied_id),
    )
    await repo.insert_event(db, kind="task_rejected", task_id=overridden, actor="human")
    await db.commit()

    gates = (await practice_metrics(db))["human_gates"]
    row = _gate(gates, "steward", "default")
    assert row["applied"] == 2
    assert row["escalated"] == 1
    assert row["overridden_by_human"] == 1

    page = (await client.get("/metrics")).text
    assert "gate=steward" in page or "Стюард" in page
    assert str(row["applied"]) in page


async def test_override_window_is_seven_days(db: aiosqlite.Connection):
    # AC-4 (#1023): a human change six days after steward_applied counts;
    # eight days later does not. The window is the denominator in time.
    inside = await _task(db, title="overridden inside window")
    outside = await _task(db, title="overridden outside window")
    applied_in = await repo.insert_event(
        db, kind="steward_applied", task_id=inside, actor="steward"
    )
    applied_out = await repo.insert_event(
        db, kind="steward_applied", task_id=outside, actor="steward"
    )
    reject_in = await repo.insert_event(
        db, kind="task_rejected", task_id=inside, actor="human"
    )
    reject_out = await repo.insert_event(
        db, kind="task_rejected", task_id=outside, actor="human"
    )
    await db.execute(
        "UPDATE events SET created_at=? WHERE id=?",
        (_ts(6 * 24 + 1), applied_in),
    )
    await db.execute(
        "UPDATE events SET created_at=? WHERE id=?",
        (_ts(1.0), reject_in),
    )
    await db.execute(
        "UPDATE events SET created_at=? WHERE id=?",
        (_ts(8 * 24 + 1), applied_out),
    )
    await db.execute(
        "UPDATE events SET created_at=? WHERE id=?",
        (_ts(1.0), reject_out),
    )
    await db.commit()

    row = _gate((await practice_metrics(db))["human_gates"], "steward", "default")
    assert row["applied"] == 2
    assert row["overridden_by_human"] == 1
    assert row["escalated"] == 0


# --- #912: пустой прогон не портит знаменатель суждений ----------------------


async def test_no_data_reports_excluded_from_precision(db: aiosqlite.Connection):
    """AC-1: отчёт, где судить нечего, не считается несуждённым.

    Прогон без кандидатов, без находок и без токенов — это не «отчёт, который
    никто не разобрал». Разбирать в нём нечего, и знаменатель, включающий его,
    делает привычку хуже, чем она есть. На живом окне в момент написания: 151
    отчёт, из них 61 пустой, покрытие читалось «0 из 151» там, где честный
    знаменатель 90.
    """
    judged = await _task(db, title="есть что судить")
    await _judged(db, judged, ["fixed"])
    silent = await _task(db, title="есть находки, никто не разобрал")
    await _report(db, silent, confirmed=3)
    empty_one = await _task(db, title="пустой прогон 1")
    await _report(db, empty_one, confirmed=0, tokens_spent=None)
    empty_two = await _task(db, title="пустой прогон 2")
    await _report(db, empty_two, confirmed=0, tokens_spent=None)
    await db.commit()

    mr = (await practice_metrics(db))["machine_reviews"]
    disp = mr["dispositions"]

    assert mr["no_data_reports"] == 2, "пустые прогоны никуда не делись"
    assert disp["reports_counted"] == 2, "в знаменателе только те, где есть что судить"
    assert disp["reports_judged"] == 1
    assert disp["reports_unjudged"] == 1, (
        "несуждённый ровно один — второй отчёт с находками; "
        "пустые прогоны в это число не входят"
    )


async def test_unjudged_window_states_itself(
    client: AsyncClient, db: aiosqlite.Connection
):
    """AC-2: пустые прогоны названы отдельно, а не спрятаны и не смешаны.

    И ноль печатается нулём. Прочерк читается как «неизвестно», тогда как это
    известная величина: посчитали, и их нет. Счётчик, исчезающий при нуле, учит
    читателя, что такого не бывает — ровно то, о чём предупреждает условие
    пересмотра этой задачи.
    """
    with_data = await _task(db, title="с находками")
    await _report(db, with_data, confirmed=2)
    await db.commit()

    def _row(page: str, label: str) -> str:
        """Содержимое ИМЕННО этой строки таблицы.

        Проверять подстроку по всей странице нельзя: «0 из 1» встречается и в
        соседней строке покрытия, поэтому утверждение проходило бы через неё и
        молчало о том, ради чего написано.
        """
        start = page.index(label)
        return page[start : page.index("</tr>", start)]

    # Сначала состояние БЕЗ пустых прогонов: именно здесь прочерк и прячется.
    page = (await client.get("/metrics")).text
    assert "0 из 1" in _row(page, "Без данных"), "ноль напечатан нулём, не прочерком"

    empty = await _task(db, title="пустой")
    await _report(db, empty, confirmed=0, tokens_spent=None)
    await db.commit()

    page = (await client.get("/metrics")).text
    assert "1 из 2" in _row(page, "Без данных"), "пустые названы рядом с общим числом"
    assert "0 из 1" in _row(page, "Разобрано отчётов"), (
        "знаменатель покрытия — только отчёты с данными"
    )


async def test_no_data_does_not_move_precision(db: aiosqlite.Connection):
    """AC-3: presence пустых прогонов не двигает precision и resolution_rate.

    Проверяется сравнением ДВУХ прогонов на одних и тех же суждениях: без
    пустых отчётов и с ними. Утверждение «precision равна 1.0» было бы верно и
    при сломанном расчёте, если суждение всего одно; сравнение двух состояний
    ловит именно вклад пустых.
    """
    task_id = await _task(db, title="суждения")
    await _judged(db, task_id, ["fixed", "false_positive"])
    await db.commit()
    before = (await practice_metrics(db))["machine_reviews"]["dispositions"]

    for i in range(3):
        empty = await _task(db, title=f"пустой {i}")
        await _report(db, empty, confirmed=0, tokens_spent=None)
    await db.commit()
    after = (await practice_metrics(db))["machine_reviews"]["dispositions"]

    assert before["precision"] == after["precision"]
    assert before["resolution_rate"] == after["resolution_rate"]
    assert before["judged"] == after["judged"] == 2
    assert after["reports_counted"] == before["reports_counted"], (
        "пустые прогоны не попали и в знаменатель покрытия"
    )


async def test_a_partly_judged_report_is_not_covered(db: aiosqlite.Connection):
    """«Разобран» значит, что ответ есть у КАЖДОЙ находки, а не у первой.

    При слабом правиле отчёт из трёх находок с одной диспозицией читался как
    полностью покрытый, и страница могла показать «90 из 90 разобрано» рядом с
    очередью на 180 неотвеченных находок.
    """
    task_id = await _task(db, title="разобран наполовину")
    await _report(db, task_id, confirmed=3)
    row = await repo.get_latest_machine_review(db, task_id)
    await repo.upsert_finding_disposition(
        db,
        review_id=int(dict(row)["id"]),
        task_id=task_id,
        submission_generation=1,
        finding_index=0,
        finding_uid="uid-0",
        finding_title="первая",
        disposition="fixed",
        note="",
        decided_by="denis",
    )
    await db.commit()

    disp = (await practice_metrics(db))["machine_reviews"]["dispositions"]
    assert disp["reports_counted"] == 1
    assert disp["reports_judged"] == 0, "одна диспозиция из трёх — это не разбор"
    assert disp["reports_unjudged"] == 1


async def test_a_superseded_report_is_not_in_the_denominator(
    db: aiosqlite.Connection,
):
    """Отчёт пересданной генерации судить НЕЛЬЗЯ — и требовать его нельзя.

    Очередь его не показывает, а record_finding_dispositions отвергает (#1038).
    Оставленный в знаменателе, он превращается в единицу, которую невозможно
    закрыть: «1 из 2» навсегда. Тот же класс, что пустой прогон.
    """
    task_id = await _task(db, title="пересдана")
    await _report(db, task_id, confirmed=5)
    # Задача ушла вперёд: отчёт генерации 1 стал устаревшим.
    await db.execute("UPDATE tasks SET submission_generation=2 WHERE id=?", (task_id,))
    await db.commit()

    disp = (await practice_metrics(db))["machine_reviews"]["dispositions"]
    assert disp["reports_counted"] == 0, (
        "устаревший отчёт не попадает в знаменатель, который просят закрыть"
    )
    assert disp["reports_unjudged"] == 0


async def test_an_empty_window_prints_zero_not_a_blank(client: AsyncClient):
    """Пустое окно печатает нули, а не пустые клетки.

    SUM по пустому набору — это SQL NULL, а не ноль, и Jinja печатает None
    пустой строкой: « из 0». Раньше это пряталось за прочерком, который убрали
    в этой же задаче, — то есть исправление одной честности вскрыло другую.
    """
    page = (await client.get("/metrics")).text
    start = page.index("Без данных")
    row = page[start : page.index("</tr>", start)]
    assert "0 из 0" in row, f"пустое окно печатает нули, а не пустоту: {row!r}"


# --- Сводка ревью для владельца (#1406) -------------------------------------
#
# Цена берётся только из счёта провайдера (review_dispatches.provider_tokens):
# tokens_spent — самоотчёт харнесса, занижен в 12–62 раза и в цену не входит.
# Прогон без счёта — отдельная строка, а не ноль и не среднее.

_SELF_REPORT_TOKENS = 99_999_999  # tokens_spent, которого не должно быть нигде


async def _order(
    db: aiosqlite.Connection,
    task_id: int,
    *,
    generation: int = 1,
    profile: str = "lite",
    bill: int | None = None,
    status: str = "done",
    channel: str = "cloud",
    replaces: int | None = None,
    agent_id: str = "bc-run",
) -> int:
    did = await repo.create_review_dispatch(
        db,
        task_id=task_id,
        submission_generation=generation,
        agent_id=agent_id,
        run_id="run",
        model="gpt-5.3-codex",
        profile=profile,
        channel=channel,
        replaces_dispatch_id=replaces,
    )
    if bill is not None:
        await repo.set_review_dispatch_provider_tokens(db, did, bill)
    await repo.set_review_dispatch_status(db, did, status)
    return did


async def _economy_report(
    db: aiosqlite.Connection,
    task_id: int,
    *,
    generation: int = 1,
    confirmed: int = 0,
    unresolved: int = 0,
    profile: str = "lite",
    self_reviewed: bool = False,
    bill: int | None = None,
) -> int:
    await db.execute(
        "UPDATE tasks SET submission_generation=? WHERE id=?", (generation, task_id)
    )
    rid = await repo.insert_machine_review(
        db,
        task_id=task_id,
        submission_generation=generation,
        harness_skill="multi-agent-review",
        tokens_spent=_SELF_REPORT_TOKENS,
        raw_count=confirmed + unresolved,
        findings_confirmed=json.dumps(
            [{"title": f"c{i}", "severity": "medium"} for i in range(confirmed)]
        ),
        unresolved=json.dumps(
            [{"title": f"u{i}", "severity": "medium"} for i in range(unresolved)]
        ),
        incomplete=False,
        profile=profile,
        self_reviewed=self_reviewed,
    )
    if bill is not None:
        await repo.set_machine_review_provider_tokens(
            db, task_id, generation, bill, review_id=rid
        )
    return rid


def _row(rows: list[dict], key: str, value: str) -> dict:
    found = [r for r in rows if r[key] == value]
    assert found, f"нет строки {key}={value}: {rows}"
    return found[0]


async def test_review_economy_counts_runs_by_profile_from_the_provider_bill(
    db: aiosqlite.Connection,
):
    # AC-1 (#1406): прогоны по профилям и типам заказа — из счёта провайдера.
    from hub.services.review_dispatch import MODEL_CASCADE_EVENT

    a = await _task(db, title="first + ladder")
    await _order(db, a, profile="lite", bill=1_000_000)
    await _order(db, a, profile="deep", bill=3_000_000)
    await _economy_report(db, a, confirmed=1)

    b = await _task(db, title="unbilled run")
    await _order(db, b, profile="deep", bill=None)
    await _economy_report(db, b, confirmed=1)

    c = await _task(db, title="ask again")
    lost = await _order(db, c, profile="lite", bill=500_000, status="failed")
    await _order(db, c, profile="lite", bill=700_000, replaces=lost)

    d = await _task(db, title="cascade")
    await _order(db, d, profile="deep", bill=2_000_000)
    cascade = await _order(db, d, profile="deep", bill=2_500_000)
    await repo.insert_event(
        db,
        kind=MODEL_CASCADE_EVENT,
        task_id=d,
        actor="policy",
        payload={"generation": 1, "dispatch_id": cascade, "attempt": 1},
    )

    e = await _task(db, title="second door")
    refused = await _order(db, e, profile="lite", bill=None, status="failed")
    await _order(db, e, profile="lite", bill=None, channel="local", replaces=refused)
    # Заглушка отказа (#1242) — не прогон: агента не было, счёта тоже.
    await _order(db, e, profile="lite", agent_id="", status="failed")
    await db.commit()

    econ = (await practice_metrics(db))["review_economy"]
    runs = econ["runs"]

    assert runs["total"] == 9
    assert runs["billed"] == 6
    assert runs["unbilled"] == 3, "прогон без счёта — своя строка, не ноль"
    assert runs["provider_tokens_total"] == 9_700_000

    lite = _row(runs["by_profile"], "profile", "lite")
    assert (lite["runs"], lite["billed_runs"], lite["unbilled_runs"]) == (5, 3, 2)
    assert lite["provider_tokens_total"] == 2_200_000
    deep = _row(runs["by_profile"], "profile", "deep")
    assert (deep["runs"], deep["billed_runs"], deep["unbilled_runs"]) == (4, 3, 1)
    assert deep["provider_tokens_total"] == 7_500_000
    assert deep["provider_tokens_per_run"] == 2_500_000

    by_kind = runs["by_kind"]
    first = _row(by_kind, "kind", "first")
    assert (first["runs"], first["billed_runs"]) == (5, 3)
    assert first["provider_tokens_total"] == 3_500_000
    assert _row(by_kind, "kind", "ladder")["provider_tokens_total"] == 3_000_000
    assert _row(by_kind, "kind", "ask_again")["provider_tokens_total"] == 700_000
    assert _row(by_kind, "kind", "cascade")["provider_tokens_total"] == 2_500_000
    second = _row(by_kind, "kind", "second_door")
    assert (second["runs"], second["unbilled_runs"]) == (1, 1)

    assert str(_SELF_REPORT_TOKENS) not in json.dumps(econ), (
        "tokens_spent не входит ни в одно число сводки"
    )


async def test_review_economy_shows_unresolved_apart_from_confirmed(
    db: aiosqlite.Connection,
):
    # AC-2 (#1406): unresolved и confirmed — разные строки; самоотчёт —
    # не независимое ревью.
    t1 = await _task(db, title="confirmed only")
    await _economy_report(db, t1, confirmed=2, profile="lite")
    t2 = await _task(db, title="unresolved only")
    await _economy_report(db, t2, unresolved=3, profile="deep")
    t3 = await _task(db, title="both")
    await _economy_report(db, t3, confirmed=1, unresolved=1, profile="deep")
    t4 = await _task(db, title="self review")
    await _economy_report(db, t4, confirmed=1, unresolved=5, self_reviewed=True)
    await db.commit()

    f = (await practice_metrics(db))["review_economy"]["findings"]

    assert f["independent_reports"] == 3
    assert f["reports_with_unresolved"] == 2
    assert f["unresolved_total"] == 4
    assert f["reports_with_confirmed"] == 2
    assert f["confirmed_total"] == 3
    assert f["reports_with_both"] == 1
    assert f["unresolved_report_share"] == round(2 / 3, 3)
    assert f["n"] == 3
    assert f["undersampled"] is True, "n<20 помечается недобором"

    assert f["self_reviewed"] == {"reports": 1, "confirmed": 1, "unresolved": 5}

    deep = _row(f["by_profile"], "profile", "deep")
    assert (deep["reports"], deep["confirmed"], deep["unresolved"]) == (2, 1, 4)
    lite = _row(f["by_profile"], "profile", "lite")
    assert (lite["reports"], lite["confirmed"], lite["unresolved"]) == (1, 2, 0)


async def _pin(db: aiosqlite.Connection, task_id: int, sha: str, ci: str | None):
    await repo.record_submission(
        db, task_id=task_id, generation=1, sha=sha, base_branch="develop"
    )
    if ci is not None:
        await repo.upsert_ci_run_report(
            db,
            task_id=task_id,
            head_sha=sha,
            ac_results="{}",
            validation_status=ci,
            validation_log="",
            reason="",
            reported_by="ci",
        )


async def test_review_economy_counts_runs_bought_on_red_ci(
    db: aiosqlite.Connection,
):
    # AC-3 (#1406): прогоны на сдачах, чей закреплённый sha CI назвал fail.
    red = await _task(db, title="red")
    await _pin(db, red, "sha-red", "fail")
    await _order(db, red, profile="lite", bill=1_000_000)
    await _order(db, red, profile="deep", bill=3_000_000)

    red_unbilled = await _task(db, title="red without bill")
    await _pin(db, red_unbilled, "sha-red-2", "fail")
    await _order(db, red_unbilled, bill=None)

    green = await _task(db, title="green, red on an old sha")
    await _pin(db, green, "sha-old", "fail")
    await _pin(db, green, "sha-green", "pass")
    await _order(db, green, bill=2_000_000)

    silent = await _task(db, title="no CI report")
    await _pin(db, silent, "sha-silent", None)
    await _order(db, silent, bill=5_000_000)
    await db.commit()

    red_ci = (await practice_metrics(db))["review_economy"]["red_ci"]

    assert red_ci["runs"] == 3
    assert red_ci["billed_runs"] == 2
    assert red_ci["provider_tokens"] == 4_000_000
    assert red_ci["runs_without_ci_report"] == 1, (
        "прогон без отчёта CI в строку «на красном CI» не попадает"
    )
    assert red_ci["runs_on_green_ci"] == 1


async def test_review_economy_reconciles_reports_with_paid_runs(
    db: aiosqlite.Connection,
):
    # AC-4 (#1406): расхождение отчётов и оплаченных прогонов — по корзинам.
    paid = await _task(db, title="paid run, billed report")
    await _order(db, paid, bill=1_000_000)
    await _economy_report(db, paid, confirmed=1, bill=1_000_000)

    own = await _task(db, title="self report")
    await _economy_report(db, own, confirmed=1, self_reviewed=True)

    outside = await _task(db, title="report without an order")
    await _economy_report(db, outside, confirmed=1)

    local = await _task(db, title="local door")
    await _order(db, local, bill=None, channel="local")
    await _economy_report(db, local, confirmed=1)

    unbilled = await _task(db, title="cloud order without a bill")
    await _order(db, unbilled, bill=None)
    await _economy_report(db, unbilled, confirmed=1)

    burnt = await _task(db, title="paid run without a report")
    await _order(db, burnt, bill=2_000_000, status="failed")

    twice = await _task(db, title="two reports of one paid run")
    await _order(db, twice, bill=3_000_000)
    await _economy_report(db, twice, confirmed=1, bill=3_000_000)
    await _economy_report(db, twice, confirmed=1)
    await db.commit()

    rec = (await practice_metrics(db))["review_economy"]["reconciliation"]

    assert rec["reports"] == 7
    assert rec["paid_runs"] == 3
    assert rec["gap"] == 4
    buckets = {b["bucket"]: b["count"] for b in rec["buckets"]}
    assert buckets["self_reviewed"] == 1
    assert buckets["no_dispatch"] == 1
    assert buckets["local_door"] == 1
    assert buckets["dispatch_without_bill"] == 1
    assert buckets["paid_without_report"] == -1
    assert buckets["unexplained"] == 1, "необъяснённый остаток — своя корзина"
    assert sum(buckets.values()) == rec["gap"]
    assert all(b["label"] for b in rec["buckets"]), "каждая корзина названа"


async def test_review_economy_does_not_count_a_carried_report(
    db: aiosqlite.Connection,
):
    # Находка deep-ревью сдачи 3 #1361: перенос отчёта на пересдачу «только
    # слита база» (#1361) — не прогон и не чтение. Сводка #1406 читала
    # machine_reviews без фильтра переноса, и одна копия давала два
    # независимых отчёта, вдвое больше находок и отчёт «без счёта».
    task_id = await _task(db, title="merge-only resubmission")
    await _order(db, task_id, bill=1_000_000)
    rid = await _economy_report(db, task_id, confirmed=2, bill=1_000_000)
    source = dict(await repo.get_machine_review(db, rid))
    await db.execute("UPDATE tasks SET submission_generation=2 WHERE id=?", (task_id,))
    await repo.insert_machine_review(
        db,
        task_id=task_id,
        submission_generation=2,
        harness_skill=source["harness_skill"],
        raw_count=int(source["raw_count"] or 0),
        findings_confirmed=source["findings_confirmed"],
        unresolved=source["unresolved"],
        incomplete=False,
        profile=source["profile"],
        carried_from_review_id=rid,
    )
    await db.commit()

    econ = (await practice_metrics(db))["review_economy"]

    assert econ["findings"]["independent_reports"] == 1, "перенос — не отчёт"
    assert econ["findings"]["confirmed_total"] == 2, "находки не удваиваются"
    rec = econ["reconciliation"]
    assert rec["reports"] == 1
    assert rec["gap"] == 0
    buckets = {b["bucket"]: b["count"] for b in rec["buckets"]}
    assert all(count == 0 for count in buckets.values()), (
        "перенос не попадает ни в одну корзину «без счёта»"
    )


async def test_review_economy_is_the_same_on_every_surface(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-5 (#1406): REST, MCP и /metrics отдают одни и те же числа.
    import re

    from hub import mcp_server

    red = await _task(db, title="red")
    await _pin(db, red, "sha-red", "fail")
    await _order(db, red, profile="lite", bill=1_000_000)
    await _order(db, red, profile="deep", bill=None)
    await _economy_report(db, red, confirmed=2, unresolved=1, bill=1_000_000)
    other = await _task(db, title="outside")
    exported = await _order(db, other, profile="deep", bill=4_000_000)
    await db.execute(
        "UPDATE review_dispatches SET billed_tokens = 9000000 WHERE id = ?",
        (exported,),
    )
    await _economy_report(db, other, unresolved=2, profile="deep")
    await db.commit()

    rest = (await client.get("/api/metrics/practices?since_days=90")).json()
    econ = rest["review_economy"]

    async def _via_client(path: str, **_: object) -> object:
        return (await client.get(path)).json()

    monkeypatch.setattr(mcp_server, "_api_get", _via_client)
    out = await mcp_server.hub_practice_metrics(since_days=90)
    assert out.structuredContent["metrics"]["review_economy"] == econ
    text = out.content[0].text
    assert (
        f"Review economy: {econ['runs']['total']} run(s), "
        f"{econ['runs']['billed']} billed / {econ['runs']['unbilled']} without a bill, "
        f"{econ['runs']['provider_tokens_total']} provider tokens"
    ) in text
    assert f"Unresolved: {econ['findings']['unresolved_total']} in" in text
    red_ci = econ["red_ci"]
    assert (
        f"CI undetermined {red_ci['runs_ci_undetermined']}, checks skipped "
        f"{red_ci['runs_ci_skipped']}, no CI report "
        f"{red_ci['runs_without_ci_report']}"
    ) in text

    page = (await client.get("/metrics?since_days=90")).text
    shown = dict(re.findall(r'data-metric="([\w.]+)"[^>]*>\s*(-?\d+)\s*<', page))
    expected = {
        "runs.total": econ["runs"]["total"],
        "runs.billed": econ["runs"]["billed"],
        "runs.unbilled": econ["runs"]["unbilled"],
        "runs.provider_tokens_total": econ["runs"]["provider_tokens_total"],
        "findings.reports_with_unresolved": econ["findings"]["reports_with_unresolved"],
        "findings.unresolved_total": econ["findings"]["unresolved_total"],
        "findings.confirmed_total": econ["findings"]["confirmed_total"],
        "red_ci.runs": econ["red_ci"]["runs"],
        "red_ci.provider_tokens": econ["red_ci"]["provider_tokens"],
        "red_ci.runs_on_green_ci": econ["red_ci"]["runs_on_green_ci"],
        "red_ci.runs_ci_undetermined": econ["red_ci"]["runs_ci_undetermined"],
        "red_ci.runs_ci_skipped": econ["red_ci"]["runs_ci_skipped"],
        "red_ci.runs_without_ci_report": econ["red_ci"]["runs_without_ci_report"],
        "reconciliation.reports": econ["reconciliation"]["reports"],
        "reconciliation.paid_runs": econ["reconciliation"]["paid_runs"],
        "reconciliation.gap": econ["reconciliation"]["gap"],
    }
    for bucket in econ["reconciliation"]["buckets"]:
        expected[f"reconciliation.{bucket['bucket']}"] = bucket["count"]
    # #1413: источник цены и покрытие выгрузкой — те же, что в REST и MCP.
    expected["runs.export_billed_runs"] = econ["runs"]["export_billed_runs"]
    expected["runs.api_lower_bound_runs"] = econ["runs"]["api_lower_bound_runs"]
    assert {k: int(v) for k, v in shown.items() if k in expected} == expected
    share = re.search(r'data-metric-share="runs.export_coverage_share">([^<]*)<', page)
    assert share and share.group(1) == str(econ["runs"]["export_coverage_share"])
    assert econ["runs"]["cost_source_note"] in page
    assert (
        f"Cursor export covers {econ['runs']['export_billed_runs']} run(s), "
        f"{econ['runs']['api_lower_bound_runs']} priced by the API (lower bound)"
    ) in text
    assert (
        econ["runs"]["export_billed_runs"],
        econ["runs"]["api_lower_bound_runs"],
    ) == (
        1,
        1,
    )
    # Числа попарно различны, иначе подмена одного другим прошла бы молча.
    assert (econ["runs"]["billed"], econ["runs"]["unbilled"]) == (2, 1)
    assert econ["findings"]["unresolved_total"] == 3


async def test_review_economy_red_ci_rows_cover_every_run(
    db: aiosqlite.Connection,
):
    # Находка a5370258c13e33c0 (#1406): unknown и skipped — полноправные
    # статусы CI. Молчание не успех и не провал: у них своя строка, и строки
    # вместе дают все прогоны, без безымянного остатка.
    for title, ci in (
        ("red", "fail"),
        ("green", "pass"),
        ("undetermined", "unknown"),
        ("skipped", "skipped"),
        ("no report", None),
    ):
        task_id = await _task(db, title=title)
        await _pin(db, task_id, f"sha-{title}", ci)
        await _order(db, task_id, bill=1_000_000)
    await db.commit()

    econ = (await practice_metrics(db))["review_economy"]
    red_ci = econ["red_ci"]

    assert red_ci["runs"] == 1, "unknown и skipped не красные"
    assert red_ci["runs_on_green_ci"] == 1
    assert red_ci["runs_ci_undetermined"] == 1
    assert red_ci["runs_ci_skipped"] == 1
    assert red_ci["runs_without_ci_report"] == 1
    assert (
        red_ci["runs"]
        + red_ci["runs_on_green_ci"]
        + red_ci["runs_ci_undetermined"]
        + red_ci["runs_ci_skipped"]
        + red_ci["runs_without_ci_report"]
    ) == econ["runs"]["total"]


# --- Slices: project, reviewer model, dates, previous period (#1490) ---------
#
# One scope object feeds every section (hub/services/metrics_scope.py). A
# filter that narrowed only the sections somebody remembered would be the
# mixed total this task exists to remove, so the tests read several sections
# under one filter.

LEGACY_KEYS = {
    "since_days",
    "machine_reviews",
    "incomplete_reasons",
    "review_model_cascade",
    "review_dispatches",
    "by_harness",
    "by_profile",
    "by_reviewer_model",
    "recurring_categories",
    "category_debt",
    "rule_breaches",
    "cycle_times",
    "escaped_defects",
    "prod_defect_clocks",
    "change_failure_rate",
    "shift_left",
    "model_declarations",
    "human_gates",
    "steward_shadow",
    "human_touches",
    "review_outcomes",
    "validation_run_lines",
    "executor_runs",
    "review_economy",
}


async def _completed(
    db: aiosqlite.Connection,
    title: str,
    *,
    project_id: int | None = None,
    hours: float = 5.0,
    completed_days_ago: float = 3.0,
) -> int:
    """A finished feature whose cycle time is exactly ``hours``."""
    task_id = await _task(db, title=title, status="completed", project_id=project_id)
    await db.execute(
        "UPDATE tasks SET work_type='feature', ready_at=?, completed_at=? WHERE id=?",
        (
            _ts(completed_days_ago * 24.0 + hours),
            _ts(completed_days_ago * 24.0),
            task_id,
        ),
    )
    return task_id


async def _report_at(
    db: aiosqlite.Connection,
    task_id: int,
    *,
    model: str = "",
    days_ago: float = 1.0,
    confirmed: int = 1,
) -> None:
    await _report(db, task_id, confirmed=confirmed)
    await db.execute(
        "UPDATE machine_reviews SET model=?, created_at=? WHERE task_id=?",
        (model, _ts(days_ago * 24.0), task_id),
    )


async def test_metrics_filter_by_project(db: aiosqlite.Connection):
    """AC-1 (#1490): every section counts only the project's tasks."""
    spike = await repo.create_project(db, slug="spike", name="Spike")
    d1 = await _task(db, title="default 1")
    d2 = await _task(db, title="default 2")
    s1 = await _task(db, title="spike 1", project_id=spike)
    for tid in (d1, d2):
        await _report_at(db, tid, confirmed=2)
    await _report_at(db, s1, confirmed=1)
    await _completed(db, "default feature", hours=10.0)
    await _completed(db, "spike feature", project_id=spike, hours=4.0)
    escaped = await _defect(db, title="spike prod defect", found_in="prod")
    await db.execute("UPDATE tasks SET project_id=? WHERE id=?", (spike, escaped))
    await _verdict(db, d1, "approved")
    await _verdict(db, s1, "changes_requested")
    await _deploy(db, "aaa111")
    await _deploy(db, "bbb222", project_id=spike)
    await db.commit()

    everything = await practice_metrics(db)
    only_spike = await practice_metrics(db, project="spike")
    only_default = await practice_metrics(db, project="default")

    assert everything["machine_reviews"]["confirmed_total"] == 5
    assert only_spike["machine_reviews"]["confirmed_total"] == 1
    assert only_default["machine_reviews"]["confirmed_total"] == 4
    assert only_spike["review_economy"]["reconciliation"]["reports"] == 1
    assert only_spike["review_economy"]["findings"]["confirmed_total"] == 1
    features = {
        name: {r["work_type"]: r for r in m["cycle_times"]}["feature"]
        for name, m in (("all", everything), ("spike", only_spike))
    }
    assert features["all"]["tasks"] == 2
    assert features["spike"]["tasks"] == 1
    assert features["spike"]["median_hours"] == 4.0
    assert only_spike["shift_left"]["defects"] == 1
    assert only_default["shift_left"]["defects"] == 0
    assert only_spike["escaped_defects"]["escaped"] == 1
    assert only_spike["review_outcomes"]["verdicts"] == 1
    assert only_spike["review_outcomes"]["changes_requested"] == 1
    assert only_default["review_outcomes"]["approved"] == 1
    assert everything["review_outcomes"]["verdicts"] == 2
    assert {g["project"] for g in only_spike["human_gates"]} <= {"spike"}
    cfr = {
        name: {r["project"] for r in m["change_failure_rate"]["by_project"]}
        for name, m in (("all", everything), ("spike", only_spike))
    }
    assert cfr == {"all": {"default", "spike"}, "spike": {"spike"}}
    assert only_spike["scope"]["project"] == "spike"


async def test_metrics_unknown_project_is_refused(db: aiosqlite.Connection):
    with pytest.raises(ValueError, match="unknown project"):
        await practice_metrics(db, project="no-such-project")


async def _verdict_pack(
    db: aiosqlite.Connection, *, approved: int, changed: int, days_ago: float
) -> None:
    for i in range(approved):
        tid = await _task(db, title=f"ok {days_ago} {i}")
        await _verdict(db, tid, "approved", days_ago=days_ago)
    for i in range(changed):
        tid = await _task(db, title=f"rework {days_ago} {i}")
        await _verdict(db, tid, "changes_requested", days_ago=days_ago)


def _indicator_row(comparison: dict, key: str) -> dict:
    return {r["key"]: r for r in comparison["indicators"]}[key]


async def test_metrics_period_comparison_delta(db: aiosqlite.Connection):
    """AC-2 (#1490): current, previous and delta; no delta below the floor."""
    # Window: the last 30 days; the previous one is days 30..60 back.
    await _verdict_pack(db, approved=10, changed=0, days_ago=5)
    await _verdict_pack(db, approved=5, changed=5, days_ago=40)
    await db.commit()

    cmp = (await practice_metrics(db, since_days=30, compare=True))["comparison"]
    row = _indicator_row(cmp, "first_pass")
    assert (row["current"], row["current_n"]) == (1.0, 10)
    assert (row["previous"], row["previous_n"]) == (0.5, 10)
    assert row["delta"] == 0.5
    assert row["delta_rel"] == 1.0
    assert (row["status"], row["direction"]) == ("compared", "better")
    assert cmp["min_compare_n"] == 10
    # Not a single number is invented for a key indicator without data.
    cfr = _indicator_row(cmp, "change_failure_rate")
    assert (cfr["current"], cfr["previous"], cfr["delta"]) == (None, None, None)
    assert cfr["status"] == "insufficient_data"

    # No previous data at all: the delta is not made up.
    lonely = (await practice_metrics(db, since_days=8, compare=True))["comparison"]
    lonely_row = _indicator_row(lonely, "first_pass")
    assert lonely_row["previous"] is None
    assert lonely_row["delta"] is None
    assert lonely_row["reason"] == "no_previous_data"

    # Nine in one window is below MIN_COMPARE_N: both values show, no delta.
    await db.execute("DELETE FROM events WHERE kind='review_verdict_recorded'")
    await _verdict_pack(db, approved=9, changed=0, days_ago=5)
    await _verdict_pack(db, approved=10, changed=0, days_ago=40)
    await db.commit()
    thin = _indicator_row(
        (await practice_metrics(db, since_days=30, compare=True))["comparison"],
        "first_pass",
    )
    assert (thin["current"], thin["previous"]) == (1.0, 1.0)
    assert thin["delta"] is None and thin["direction"] is None
    assert thin["reason"] == "below_min_n"

    # The floor holds for the previous window alone as well.
    await db.execute("DELETE FROM events WHERE kind='review_verdict_recorded'")
    await _verdict_pack(db, approved=10, changed=0, days_ago=5)
    await _verdict_pack(db, approved=9, changed=0, days_ago=40)
    await db.commit()
    thin_prev = _indicator_row(
        (await practice_metrics(db, since_days=30, compare=True))["comparison"],
        "first_pass",
    )
    assert thin_prev["delta"] is None
    assert thin_prev["reason"] == "below_min_n"

    # Under 5% relative change is "no change", not a move: 1.0 against 0.975.
    await db.execute("DELETE FROM events WHERE kind='review_verdict_recorded'")
    await _verdict_pack(db, approved=20, changed=0, days_ago=5)
    await _verdict_pack(db, approved=39, changed=1, days_ago=40)
    await db.commit()
    flat = _indicator_row(
        (await practice_metrics(db, since_days=30, compare=True))["comparison"],
        "first_pass",
    )
    assert (flat["status"], flat["direction"]) == ("compared", "flat")
    assert flat["delta_rel"] == 0.026


async def test_metrics_period_comparison_uses_same_window_length(
    db: aiosqlite.Connection,
):
    await _verdict_pack(db, approved=1, changed=0, days_ago=5)
    result = await practice_metrics(
        db, date_from="2026-01-11", date_to="2026-01-20", compare=True
    )
    previous = result["comparison"]["previous_window"]
    assert previous["from"] == "2026-01-01 00:00:00"
    assert previous["to"] == "2026-01-11 00:00:00"
    assert previous["days"] == result["scope"]["days"] == 10


async def test_metrics_filter_by_model_keeps_unknown_group(
    db: aiosqlite.Connection,
):
    """AC-3 (#1490): the reviewer model filter keeps the «не заявлена» group
    and leaves the model-blind blocks exactly as they were."""
    t1 = await _task(db, title="grok")
    t2 = await _task(db, title="kimi")
    t3 = await _task(db, title="no model recorded")
    await _judged(db, t1, ["fixed", "fixed"], model="grok-4.6")
    await _judged(db, t2, ["fixed"], model="kimi-k3")
    await _judged(db, t3, ["false_positive"], model="")
    await _completed(db, "feature", hours=6.0)
    await _defect(db, title="prod defect", found_in="prod")
    await db.commit()

    plain = await practice_metrics(db)
    assert {m["model"] for m in plain["by_reviewer_model"]} == {
        "grok-4.6",
        "kimi-k3",
        "не заявлена",
    }
    assert "scope" not in plain

    unknown = await practice_metrics(db, model="не заявлена")
    assert [m["model"] for m in unknown["by_reviewer_model"]] == ["не заявлена"]
    assert unknown["machine_reviews"]["reviews"] == 1
    assert unknown["machine_reviews"]["dispositions"]["false_positive"] == 1
    assert unknown["review_economy"]["findings"]["confirmed_total"] == 1

    grok = await practice_metrics(db, model="grok-4.6")
    assert grok["machine_reviews"]["reviews"] == 1
    assert grok["machine_reviews"]["confirmed_total"] == 2

    for section in ("cycle_times", "change_failure_rate", "shift_left"):
        assert grok[section] == plain[section], section
        assert section in grok["scope"]["model_independent"]
    assert grok["scope"]["model"] == "grok-4.6"
    assert plain["machine_reviews"]["reviews"] == 3


async def test_metrics_since_days_backward_compatible(db: aiosqlite.Connection):
    """AC-4 (#1490): since_days alone answers as before — the same keys and
    no slice, comparison, series or ranking added."""
    t1 = await _task(db, title="reviewed")
    await _judged(db, t1, ["fixed", "false_positive"])
    await _completed(db, "feature")
    await db.commit()

    plain = await practice_metrics(db, since_days=30)
    assert set(plain) == LEGACY_KEYS
    assert plain["since_days"] == 30
    assert plain["machine_reviews"]["reviews"] == 1
    assert plain["machine_reviews"]["dispositions"]["judged"] == 2
    assert plain == await practice_metrics(db, since_days=30, project=None)
    # The default stays the one named constant.
    assert (await practice_metrics(db))["since_days"] == 90


async def test_metrics_series_empty_bucket_is_missing_not_zero(
    db: aiosqlite.Connection,
):
    """AC-5 (#1490): a bucket without observations has no value, not 0; a
    bucket with data equals the same window asked for directly."""
    today = datetime.now(UTC).date()
    start = today - timedelta(days=27)
    await _verdict_pack(db, approved=2, changed=0, days_ago=26)  # bucket 1
    await _verdict_pack(db, approved=1, changed=1, days_ago=1)  # bucket 4
    await db.commit()

    result = await practice_metrics(
        db,
        date_from=start.isoformat(),
        date_to=today.isoformat(),
        series=True,
        series_days=7,
    )
    series = {i["key"]: i for i in result["series"]["indicators"]}["first_pass"]
    points = series["points"]
    assert len(points) == 4
    assert [p["n"] for p in points] == [2, 0, 0, 2]
    assert points[1]["value"] is None and points[2]["value"] is None
    assert points[0]["value"] == 1.0 and points[3]["value"] == 0.5
    # Every key indicator answers the same way for a bucket with no data.
    for indicator in result["series"]["indicators"]:
        assert all(p["value"] is None for p in indicator["points"][1:3])

    # The same code: bucket 4 is the window of its own dates.
    last = points[3]
    direct = await practice_metrics(
        db,
        date_from=last["from"][:10],
        date_to=(datetime.strptime(last["to"], "%Y-%m-%d %H:%M:%S") - timedelta(days=1))
        .date()
        .isoformat(),
    )
    assert direct["review_outcomes"]["first_pass_acceptance_rate"] == last["value"]
    assert direct["review_outcomes"]["tasks"] == last["n"]


async def test_metrics_problem_spots_ranked_worse_first(db: aiosqlite.Connection):
    """AC-6 (#1490): the ranking follows the spec — unchecked debt first, then
    the worsened indicator; an improved one is not listed at all; the order is
    stable; with no previous window nothing is claimed to have worsened."""
    # first-pass worsens 1.0 -> 0.5 (n=10 each): a problem.
    await _verdict_pack(db, approved=10, changed=0, days_ago=40)
    await _verdict_pack(db, approved=5, changed=5, days_ago=5)
    # precision improves 0.5 -> 1.0 (n=10 each): not a problem, not listed.
    old = await _task(db, title="old judged")
    await _judged(db, old, ["fixed"] * 5 + ["false_positive"] * 5)
    await db.execute(
        "UPDATE machine_reviews SET created_at=? WHERE task_id=?", (_ts(40 * 24), old)
    )
    new = await _task(db, title="new judged")
    await _judged(db, new, ["fixed"] * 10)
    # A category seen in three tasks with no check: debt.
    for i in range(3):
        await _categorised(db, f"debt {i}", ["flaky-shape"])
    await db.commit()

    result = await practice_metrics(db, since_days=30, compare=True)
    spots = result["problem_spots"]
    kinds = [s["kind"] for s in spots]
    assert kinds[0] == "debt_unchecked"
    assert spots[0]["title"] == "flaky-shape"
    assert "worsened" in kinds
    assert kinds.index("debt_unchecked") < kinds.index("worsened")
    titles = [s["title"] for s in spots]
    assert "Precision ревью" not in titles
    worse = [s for s in spots if s["kind"] == "worsened"]
    assert [s["key"] for s in worse] == ["first_pass"]
    assert [s["rank"] for s in spots] == list(range(1, len(spots) + 1))
    assert result["problem_spots_more"] == 0

    again = await practice_metrics(db, since_days=30, compare=True)
    assert again["problem_spots"] == spots, "the order is deterministic"

    # No previous window behind the current one: no worsening is claimed.
    blank = await practice_metrics(db, since_days=1, compare=True)
    assert "worsened" not in [s["kind"] for s in blank["problem_spots"]]
    assert "debt_unchecked" in [s["kind"] for s in blank["problem_spots"]]


def test_problem_spots_group_order_and_cap():
    from hub.services.metrics_compare import MAX_SPOTS, rank_problem_spots

    metrics = {
        "category_debt": [
            {"category": "b", "tasks": 3, "findings": 4, "covered": False},
            {"category": "a", "tasks": 3, "findings": 4, "covered": False},
            {"category": "done", "tasks": 9, "findings": 9, "covered": True},
            {"category": "big", "tasks": 5, "findings": 1, "covered": False},
        ],
        "rule_breaches": {
            "breached": [
                {"category": "r1", "breaches": 1},
                {"category": "r2", "breaches": 4},
            ]
        },
        "review_economy": {"reconciliation": {"gap": 3}, "runs": {"unbilled": 7}},
        "machine_reviews": {"dispositions": {"confirmed_unjudged": 0}},
        "shift_left": {"unknown_share": 0.3, "unknown": 3, "defects": 10},
    }
    comparison = {
        "indicators": [
            {
                "key": "touches_per_delivered",
                "label": "t",
                "status": "compared",
                "direction": "worse",
                "delta_rel": 0.2,
                "source_field": "x",
                "previous": 1,
                "previous_n": 10,
                "current": 1.2,
                "current_n": 10,
            },
            {
                "key": "first_pass",
                "label": "f",
                "status": "compared",
                "direction": "worse",
                "delta_rel": 0.09,  # under 10%: not a problem
                "source_field": "x",
                "previous": 1,
                "previous_n": 10,
                "current": 0.9,
                "current_n": 10,
            },
        ]
    }
    spots, more = rank_problem_spots(metrics, comparison)
    assert [(s["kind"], s["title"]) for s in spots[:3]] == [
        ("debt_unchecked", "big"),
        ("debt_unchecked", "a"),
        ("debt_unchecked", "b"),
    ]
    assert [s["title"] for s in spots if s["kind"] == "rule_breached"] == ["r2", "r1"]
    assert [s["title"] for s in spots if s["kind"] == "worsened"] == ["t"]
    assert len(spots) == MAX_SPOTS
    # 3 debt + 2 rules + 1 worsened + 3 gaps = 9 candidates; 7 are shown.
    assert more == 2


async def test_rest_practice_metrics_takes_the_slice(
    client: AsyncClient, db: aiosqlite.Connection
):
    """#1490: REST carries project, model, dates, compare and series."""
    await repo.create_project(db, slug="spike", name="Spike")
    tid = await _task(db, title="reviewed")
    await _judged(db, tid, ["fixed"], model="grok-4.6")
    await db.commit()

    body = (
        await client.get(
            "/api/metrics/practices",
            params={
                "project": "default",
                "model": "grok-4.6",
                "since_days": 30,
                "compare": "true",
                "series": "true",
                "series_days": 10,
            },
        )
    ).json()
    assert body["scope"]["project"] == "default"
    assert body["scope"]["model"] == "grok-4.6"
    assert body["machine_reviews"]["reviews"] == 1
    assert "problem_spots" in body and "comparison" in body
    assert body["series"]["bucket_days"] == 10

    other = (
        await client.get("/api/metrics/practices", params={"project": "spike"})
    ).json()
    assert other["machine_reviews"]["reviews"] == 0

    dated = (
        await client.get(
            "/api/metrics/practices",
            params={"date_from": "2020-01-01", "date_to": "2020-01-31"},
        )
    ).json()
    assert dated["since_days"] == 31
    assert dated["machine_reviews"]["reviews"] == 0

    plain = (await client.get("/api/metrics/practices")).json()
    assert set(plain) == LEGACY_KEYS


async def test_rest_practice_metrics_refuses_bad_slice(client: AsyncClient):
    missing = await client.get("/api/metrics/practices?project=nope")
    assert missing.status_code == 400
    assert "unknown project" in missing.json()["detail"]
    bad_date = await client.get("/api/metrics/practices?date_from=01.02.2026")
    assert bad_date.status_code == 400
    backwards = await client.get(
        "/api/metrics/practices?date_from=2026-02-02&date_to=2026-02-01"
    )
    assert backwards.status_code == 400


def test_cli_practice_metrics_passes_the_slice(capsys):
    import sys
    from unittest.mock import patch

    from hub import cli

    argv = [
        "oc-hub",
        "practice-metrics",
        "--since-days",
        "30",
        "--project",
        "spike",
        "--model",
        "не заявлена",
        "--date-from",
        "2026-01-01",
        "--compare",
        "--series",
        "--series-days",
        "14",
    ]
    with (
        patch.object(sys, "argv", argv),
        patch.object(cli, "_api", return_value={"since_days": 30}) as api,
    ):
        rc = cli.main()
    assert rc in (0, None)
    method, url = api.call_args.args[:2]
    assert method == "GET"
    assert url.startswith("/api/metrics/practices?since_days=30")
    for part in (
        "project=spike",
        "model=%D0%BD%D0%B5+%D0%B7%D0%B0%D1%8F%D0%B2%D0%BB%D0%B5%D0%BD%D0%B0",
        "date_from=2026-01-01",
        "compare=true",
        "series=true",
        "series_days=14",
    ):
        assert part in url, url
    assert "date_to" not in url
    assert json.loads(capsys.readouterr().out) == {"since_days": 30}

    with (
        patch.object(sys, "argv", ["oc-hub", "practice-metrics"]),
        patch.object(cli, "_api", return_value={}) as plain,
    ):
        cli.main()
    assert (
        plain.call_args.args[1] == "/api/metrics/practices?since_days=90&series_days=7"
    )


async def test_mcp_practice_metrics_passes_the_slice_and_names_the_ranking():
    from unittest.mock import AsyncMock, patch

    from hub.mcp_server import hub_practice_metrics

    data = {
        "since_days": 30,
        "scope": {
            "project": "spike",
            "model": "grok-4.6",
            "from": "2026-01-01 00:00:00",
            "to": None,
            "days": 30,
            "model_independent": ["cycle_times", "shift_left"],
        },
        "comparison": {
            "indicators": [
                {
                    "label": "С первого раза (first-pass)",
                    "current": 0.5,
                    "current_n": 10,
                    "previous": 1.0,
                    "previous_n": 10,
                    "delta": -0.5,
                    "direction": "worse",
                    "reason": "",
                },
                {
                    "label": "Change failure rate",
                    "current": None,
                    "current_n": 0,
                    "previous": None,
                    "previous_n": 0,
                    "delta": None,
                    "direction": None,
                    "reason": "no_current_data",
                },
            ]
        },
        "problem_spots": [
            {
                "rank": 1,
                "kind": "worsened",
                "title": "С первого раза (first-pass)",
                "reason": "было 1.0 (n=10), стало 0.5 (n=10)",
            }
        ],
    }
    with patch("hub.mcp_server._api_get", new_callable=AsyncMock) as get:
        get.return_value = data
        result = await hub_practice_metrics(
            project="spike", model="grok-4.6", compare=True, date_from="2026-01-01"
        )
    url = get.call_args.args[0]
    for part in (
        "project=spike",
        "model=grok-4.6",
        "date_from=2026-01-01",
        "compare=true",
    ):
        assert part in url, url
    assert "series" not in url
    text = result.content[0].text
    assert "Slice: project=spike, reviewer model=grok-4.6" in text
    assert "Model filter does not apply to: cycle_times, shift_left" in text
    assert "insufficient data (no_current_data)" in text
    assert "Problem #1 [worsened]" in text


async def test_metrics_slice_reaches_dispatches_executor_and_refusals(
    db: aiosqlite.Connection,
):
    """#1490: the sections that live outside orchestration.py read the slice
    too — review dispatches, the review-economy runs, executor runs and the
    incomplete-report counters — and the reviewer model narrows only the
    review ones."""
    spike = await repo.create_project(db, slug="spike", name="Spike")
    d = await _task(db, title="default")
    s = await _task(db, title="spike", project_id=spike)
    await _failed_dispatch(db, d, provider_tokens=100)
    await _failed_dispatch(db, s, provider_tokens=40)
    await db.execute(
        "UPDATE review_dispatches SET model='kimi-k3' WHERE task_id=?", (s,)
    )
    for task_id in (d, s):
        await repo.create_executor_run(
            db,
            task_id=task_id,
            submission_generation=1,
            agent_id="bc-x",
            run_id="r",
            model="composer",
        )
    await db.commit()

    everything = await practice_metrics(db)
    only_spike = await practice_metrics(db, project="spike")
    assert everything["review_dispatches"]["wasted_provider_tokens_total"] == 140
    assert only_spike["review_dispatches"]["wasted_provider_tokens_total"] == 40
    assert only_spike["review_economy"]["runs"]["total"] == 1
    assert everything["review_economy"]["runs"]["total"] == 2
    assert everything["executor_runs"]["runs"] == 2
    assert only_spike["executor_runs"]["runs"] == 1

    kimi = await practice_metrics(db, model="kimi-k3")
    assert kimi["review_dispatches"]["wasted_provider_tokens_total"] == 40
    assert kimi["review_economy"]["runs"]["total"] == 1
    assert kimi["executor_runs"] == everything["executor_runs"], "no reviewer model"

    # Incomplete reports are counted per slice as well.
    for task_id, model in ((d, "grok-4.6"), (s, "kimi-k3")):
        await _report_at(db, task_id, model=model)
    await db.execute("UPDATE machine_reviews SET incomplete=1")
    await db.commit()
    assert (await practice_metrics(db))["incomplete_reasons"]["incomplete_total"] == 2
    by_project = await practice_metrics(db, project="spike")
    assert by_project["incomplete_reasons"]["incomplete_total"] == 1
    by_model = await practice_metrics(db, model="grok-4.6")
    assert by_model["incomplete_reasons"]["incomplete_total"] == 1


async def test_metrics_unjudged_queue_and_rules_follow_the_slice(
    db: aiosqlite.Connection,
):
    """The unjudged-findings stock and the rule breaches carry no window, but
    they still belong to a project and a reviewer model (#1490)."""
    spike = await repo.create_project(db, slug="spike", name="Spike")
    d = await _task(db, title="default")
    s = await _task(db, title="spike", project_id=spike)
    await _report_at(db, d, model="grok-4.6", confirmed=2)
    await _report_at(db, s, model="kimi-k3", confirmed=1)
    await db.commit()

    def unjudged(m: dict) -> int:
        return m["machine_reviews"]["dispositions"]["confirmed_unjudged"]

    assert unjudged(await practice_metrics(db)) == 3
    assert unjudged(await practice_metrics(db, project="spike")) == 1
    assert unjudged(await practice_metrics(db, model="grok-4.6")) == 2
    assert unjudged(await practice_metrics(db, project="spike", model="grok-4.6")) == 0

    rule_date = _ts(72)
    await _rule_set_up(db, "timeouts", rule_date)
    for project_id, title, model in (
        (None, "breach default", "grok-4.6"),
        (spike, "breach spike", "kimi-k3"),
    ):
        task_id = await _finding_at(db, title, "timeouts", _ts(24))
        if project_id is not None:
            await db.execute(
                "UPDATE tasks SET project_id=? WHERE id=?", (project_id, task_id)
            )
        await db.execute(
            "UPDATE machine_reviews SET model=? WHERE task_id=?", (model, task_id)
        )
    await db.commit()

    def breaches(m: dict) -> int:
        return sum(r["breaches"] for r in m["rule_breaches"]["breached"])

    assert breaches(await practice_metrics(db)) == 2
    assert breaches(await practice_metrics(db, project="spike")) == 1
    assert breaches(await practice_metrics(db, model="grok-4.6")) == 1
