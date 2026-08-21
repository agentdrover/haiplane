"""Derived outcome-hypothesis status (#576).

The answer store already exists (#819). This module pins the states that
must stay machine-distinct: no hypothesis, not yet due, due with no answer,
answered, and revised after the metric changed. Due/not-due come from the
last successful release (#839), never from parsing free-text outcome_deadline.
"""

from __future__ import annotations

import aiosqlite
from httpx import AsyncClient

from hub import repository as repo
from hub.services.outcomes import derive_outcome_status, outcome_debt


async def _project(db: aiosqlite.Connection, slug: str = "ship") -> int:
    return await repo.create_project(db, slug=slug, name=slug.title())


async def _completed_task(
    db: aiosqlite.Connection,
    *,
    title: str,
    metric: str,
    project_id: int | None = None,
    completed_at: str = "datetime('now', '-2 days')",
) -> int:
    task_id = await repo.create_task(
        db,
        title=title,
        description="",
        runtime="auto",
        source="agent",
        assigned_agent="",
        rationale="",
        status="completed",
        auto_review=True,
        task_type="feature",
        parent_id=None,
        priority="medium",
    )
    await db.execute(
        "UPDATE tasks SET outcome_metric=?, project_id=?, "
        f"completed_at={completed_at} WHERE id=?",
        (metric, project_id, task_id),
    )
    await db.commit()
    return task_id


async def _release_after_completion(
    db: aiosqlite.Connection, project_id: int, sha: str = "a" * 40
) -> None:
    await repo.record_release(
        db, deployed_sha=sha, project_id=project_id, ref="main", source="ci"
    )


async def test_not_due_and_unanswered_are_distinct(
    db: aiosqlite.Connection, client: AsyncClient
):
    """AC-1. Due vs not-due come from a machine carrier, not outcome_deadline."""
    project_id = await _project(db)
    waiting = await _completed_task(
        db,
        title="Hypothesis waiting for a deploy",
        metric="lead time 3d -> 1d",
        project_id=project_id,
    )
    due = await _completed_task(
        db,
        title="Hypothesis after a deploy",
        metric="lead time 3d -> 1d",
        project_id=project_id,
    )
    await db.execute(
        "UPDATE tasks SET outcome_deadline=? WHERE id IN (?, ?)",
        ("Within the first 30 captures", waiting, due),
    )
    await db.commit()
    await _release_after_completion(db, project_id)

    # Same free-text deadline, different machine states: waiting finished
    # after the release, due finished before it.
    await db.execute(
        "UPDATE tasks SET completed_at=datetime('now', '+1 days') WHERE id=?",
        (waiting,),
    )
    await db.commit()
    waiting_body = (await client.get(f"/api/tasks/{waiting}")).json()
    due_body = (await client.get(f"/api/tasks/{due}")).json()

    assert waiting_body["outcome_status"] == "not_due"
    assert due_body["outcome_status"] == "unanswered"
    assert waiting_body["outcome_status"] != due_body["outcome_status"]
    assert waiting_body["outcome_deadline"] == due_body["outcome_deadline"]


async def test_no_hypothesis_is_never_overdue(
    db: aiosqlite.Connection, client: AsyncClient
):
    """AC-2. Empty metric is no_hypothesis and stays out of overdue samples."""
    project_id = await _project(db, slug="bare")
    task_id = await _completed_task(
        db, title="Typical technical backlog", metric="", project_id=project_id
    )
    await _release_after_completion(db, project_id, sha="b" * 40)

    body = (await client.get(f"/api/tasks/{task_id}")).json()
    debt = await outcome_debt(db)

    assert body["outcome_status"] == "no_hypothesis"
    assert task_id not in [item["task_id"] for item in debt["items"]]
    assert task_id not in [item["task_id"] for item in debt["overdue"]]


async def test_status_is_derived_from_answers_not_a_stored_column(
    db: aiosqlite.Connection, client: AsyncClient
):
    """AC-3. The verdict lives in outcome_answers; tasks have no status column."""
    project_id = await _project(db, slug="answered")
    task_id = await _completed_task(
        db, title="Answered hypothesis", metric="X: 0 -> 5", project_id=project_id
    )
    await _release_after_completion(db, project_id, sha="c" * 40)
    resp = await client.post(
        f"/api/tasks/{task_id}/outcome-answers",
        json={"verdict": "moved", "measured_value": "0 → 5 on prod"},
    )
    assert resp.status_code == 200, resp.text

    body = (await client.get(f"/api/tasks/{task_id}")).json()
    cols = {
        row["name"] for row in await db.execute_fetchall("PRAGMA table_info(tasks)")
    }

    assert "outcome_status" not in cols
    assert body["outcome_status"] == "confirmed"


async def test_rewritten_metric_reads_as_revised(
    db: aiosqlite.Connection, client: AsyncClient
):
    """AC-4. An answer to the old metric is not an answer to the new one."""
    project_id = await _project(db, slug="rewritten")
    task_id = await _completed_task(
        db,
        title="Metric will change",
        metric="old number: 0 -> 5",
        project_id=project_id,
    )
    await client.post(
        f"/api/tasks/{task_id}/outcome-answers",
        json={"verdict": "moved", "measured_value": "5 on prod"},
    )
    await repo.update_task(db, task_id, outcome_metric="new number: 10 -> 20")
    await db.commit()

    body = (await client.get(f"/api/tasks/{task_id}")).json()
    assert body["outcome_status"] == "revised"
    assert body["outcome_status"] != "confirmed"


async def test_legacy_answer_without_snapshot_is_not_revised(
    db: aiosqlite.Connection, client: AsyncClient
):
    """AC-5. Pre-snapshot answers read as answered, not revised."""
    project_id = await _project(db, slug="legacy")
    task_id = await _completed_task(
        db,
        title="Answer from before the snapshot",
        metric="X: 0 -> 5",
        project_id=project_id,
    )
    await db.execute(
        "INSERT INTO outcome_answers (task_id, verdict, measured_value, note, "
        "answered_by) VALUES (?, 'moved', '0 → 5', '', 'owner')",
        (task_id,),
    )
    await db.commit()

    body = (await client.get(f"/api/tasks/{task_id}")).json()
    assert body["outcome_status"] == "confirmed"
    assert body["outcome_status"] != "revised"


async def test_review_brief_and_card_expose_status(
    db: aiosqlite.Connection, client: AsyncClient
):
    project_id = await _project(db, slug="surfaces")
    task_id = await _completed_task(
        db, title="Visible status", metric="a number", project_id=project_id
    )
    await _release_after_completion(db, project_id, sha="d" * 40)

    brief = (await client.get(f"/api/tasks/{task_id}/review-brief")).json()
    card = await client.get(f"/tasks/{task_id}")

    assert brief["outcome_status"] == "unanswered"
    assert card.status_code == 200
    assert 'data-outcome-status="unanswered"' in card.text
    assert "Срок наступил, ответа нет" in card.text


async def test_overdue_sample_uses_machine_deadline(db: aiosqlite.Connection):
    project_id = await _project(db, slug="overdue")
    waiting = await _completed_task(
        db, title="Not yet shipped", metric="a number", project_id=project_id
    )
    due = await _completed_task(
        db, title="Shipped, no answer", metric="a number", project_id=project_id
    )
    await _release_after_completion(db, project_id, sha="e" * 40)
    await db.execute(
        "UPDATE tasks SET completed_at=datetime('now', '+1 days') WHERE id=?",
        (waiting,),
    )
    await db.commit()

    debt = await outcome_debt(db)
    overdue_ids = [item["task_id"] for item in debt["overdue"]]
    by_id = {item["task_id"]: item["outcome_status"] for item in debt["items"]}

    assert by_id[waiting] == "not_due"
    assert by_id[due] == "unanswered"
    assert due in overdue_ids
    assert waiting not in overdue_ids


async def test_derive_maps_verdicts_and_ignores_deadline_text():
    """Pure mapping: one stored fact, one status. Free-text deadline unused."""
    answers = [{"verdict": "not_moved", "hypothesis_snapshot": "metric"}]
    assert (
        derive_outcome_status(
            outcome_metric="metric",
            answers=answers,
            completed_at="2026-01-01T00:00:00+00:00",
            latest_release={"deployed_at": "2026-02-01T00:00:00+00:00"},
        )
        == "refuted"
    )
    assert (
        derive_outcome_status(
            outcome_metric="",
            answers=[],
            completed_at="2026-01-01T00:00:00+00:00",
            latest_release={"deployed_at": "2026-02-01T00:00:00+00:00"},
        )
        == "no_hypothesis"
    )
    assert (
        derive_outcome_status(
            outcome_metric="metric",
            answers=[],
            completed_at="2026-01-01T00:00:00+00:00",
            latest_release=None,
        )
        == "not_due"
    )
