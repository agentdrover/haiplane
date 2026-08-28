"""raw_count is reconciled with the findings a report actually lists (#519).

``raw_count`` is self-reported and was stored unchecked, which broke the
filtration metric in two different directions.

Upward: on production 75 of 192 raw findings — 39% — were never sorted into
confirmed or rejected. ``filtration_rate = 1 - confirmed/raw`` counted every
one of them as successfully filtered noise, inflating the rate from 0.573 to
0.740. The error flatters the harness, which is the direction nobody
questions.

Downward: three reports claimed fewer raw findings than they themselves
listed, one of them ``raw_count=0`` beside two confirmed findings. The intake
accepted all three.
"""

from __future__ import annotations

import aiosqlite
from httpx import AsyncClient

from hub import repository as repo
from hub import services
from hub.models import TaskCreate


async def _reviewable(client: AsyncClient, db: aiosqlite.Connection) -> int:
    """A task with a submission generation, so a machine review is accepted."""
    tv = await services.create_task(db, TaskCreate(title="reviewed"))
    await db.commit()
    await db.execute(
        "UPDATE tasks SET submission_generation=1, status='review' WHERE id=?",
        (tv.id,),
    )
    await db.commit()
    return tv.id


def _finding(idx: int) -> dict:
    """A confirmed finding: severity, category and where it sits (#1007)."""
    return {
        "title": f"f{idx}",
        "severity": "low",
        "category": "tests",
        "locator": "none",
    }


def _rejected(idx: int) -> dict:
    """A rejected finding carries a reason instead — a different shape."""
    return {"title": f"f{idx}", "reason": "noise"}


async def _submit(client: AsyncClient, task_id: int, **body) -> dict:
    payload = {
        "incomplete": False,
        "harness_skill": "multi-agent-review",
        "harness_version": 6,
        "findings_confirmed": [],
        "findings_rejected": [],
        **body,
    }
    resp = await client.post(f"/api/tasks/{task_id}/machine-review", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _totals(client: AsyncClient) -> dict:
    resp = await client.get("/api/metrics/practices?since_days=30")
    assert resp.status_code == 200, resp.text
    return resp.json()["machine_reviews"]


# --- AC-1: intake reconciles a raw_count below the findings listed ----------


async def test_raw_count_below_its_own_findings_is_normalised(
    db: aiosqlite.Connection, client: AsyncClient
):
    """AC-1. Production carried a report with raw_count=0 and two confirmed
    findings — a report claiming it found nothing while listing what it
    found."""
    task_id = await _reviewable(client, db)

    await _submit(
        client,
        task_id,
        raw_count=0,
        findings_confirmed=[_finding(1), _finding(2)],
        findings_rejected=[_rejected(3)],
    )

    saved = dict(await repo.get_latest_machine_review(db, task_id))
    assert saved["raw_count"] == 3, (
        "raw cannot be smaller than the findings the same report enumerates"
    )


async def test_the_report_is_kept_not_rejected(
    db: aiosqlite.Connection, client: AsyncClient
):
    """The recorded risk asks not to break existing clients. A miscounted
    header is not a reason to discard findings that are real."""
    task_id = await _reviewable(client, db)

    view = await _submit(
        client, task_id, raw_count=1, findings_confirmed=[_finding(1), _finding(2)]
    )

    assert view["raw_count"] == 2
    assert len(view["findings_confirmed"]) == 2


async def test_an_honest_raw_count_is_left_alone(
    db: aiosqlite.Connection, client: AsyncClient
):
    """The edge that would turn the norm into a defect: a report whose header
    matches its findings must pass through untouched."""
    task_id = await _reviewable(client, db)

    await _submit(
        client,
        task_id,
        raw_count=9,
        findings_confirmed=[_finding(1)],
        findings_rejected=[_rejected(2)],
    )

    saved = dict(await repo.get_latest_machine_review(db, task_id))
    assert saved["raw_count"] == 9, "a raw_count above the listed findings is legal"


# --- AC-2: the gap is stated, and filtration stops counting it as noise -----


async def test_filtration_ignores_findings_nobody_adjudicated(
    db: aiosqlite.Connection, client: AsyncClient
):
    """AC-2. 10 raw findings, 4 sorted: 1 confirmed, 3 rejected. The other 6
    were never looked at, and must not be credited as noise removal."""
    task_id = await _reviewable(client, db)
    await _submit(
        client,
        task_id,
        raw_count=10,
        findings_confirmed=[_finding(1)],
        findings_rejected=[_rejected(2), _rejected(3), _rejected(4)],
    )

    totals = await _totals(client)

    assert abs(totals["filtration_rate"] - (1 - 1 / 4)) < 0.001, (
        "against raw_count this would read 0.9 instead of 0.75"
    )
    assert totals["findings_unaccounted"] == 6


async def test_a_fully_adjudicated_window_reports_no_gap(
    db: aiosqlite.Connection, client: AsyncClient
):
    """When every raw finding was sorted there is nothing to disclose, and the
    rate is the same either way."""
    task_id = await _reviewable(client, db)
    await _submit(
        client,
        task_id,
        raw_count=4,
        findings_confirmed=[_finding(1)],
        findings_rejected=[_rejected(2), _rejected(3), _rejected(4)],
    )

    totals = await _totals(client)

    assert totals["findings_unaccounted"] == 0
    assert abs(totals["filtration_rate"] - 0.75) < 0.001


async def test_a_window_with_nothing_adjudicated_has_no_rate(
    db: aiosqlite.Connection, client: AsyncClient
):
    """No findings sorted means no filtration to report. Returning 0 would say
    the harness filtered nothing; returning 1 would say it filtered
    everything. Both are inventions."""
    task_id = await _reviewable(client, db)
    await _submit(client, task_id, raw_count=5)

    totals = await _totals(client)

    assert totals["filtration_rate"] is None
    assert totals["findings_unaccounted"] == 5
