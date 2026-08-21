"""Human-gate override-rate and queue wait in practice_metrics (#737).

The section answers one question per gate and project: how often does the
human click change the outcome, and how long does work queue for it. Only
human decisions count — 'hub' and 'policy' actors are excluded on both
sides of the ratio; unmeasurable waits are reported, never zeroed.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import aiosqlite
from httpx import AsyncClient

from hub import repository as repo
from hub import services
from hub.models import TaskDecide
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
