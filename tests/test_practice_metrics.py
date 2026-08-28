"""Slices of practice_metrics that must not flatter the practice.

Human gates (#737): how often the human click changes the outcome and how long
work queues for it. Only human decisions count — 'hub' and 'policy' actors are
excluded on both sides of the ratio; unmeasurable waits are reported, never
zeroed.

Review economics (#828) and escaped defects (#528) follow the same rule from
opposite ends: what a run cost, and what the gate failed to stop. Across all
three the invariant is the one #519 and #810 paid for — an exclusion is counted
and named, never folded into the number it would otherwise improve.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import aiosqlite
from httpx import AsyncClient

from hub import repository as repo
from hub import services
from hub.models import TaskDecide
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
    return (await practice_metrics(db, **kwargs))["escaped_defects"]


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
