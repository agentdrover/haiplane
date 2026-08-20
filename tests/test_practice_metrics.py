"""Human-gate override-rate and queue wait in practice_metrics (#737).

The section answers one question per gate and project: how often does the
human click change the outcome, and how long does work queue for it. Only
human decisions count — 'hub' and 'policy' actors are excluded on both
sides of the ratio; unmeasurable waits are reported, never zeroed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import aiosqlite

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
