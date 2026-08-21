"""A report with no sign of a run is not a review (#841).

Measured on production on 2026-08-21: of the 103 machine-review reports in
the 90-day window, 60 carried ``raw_count=0``, no findings on either side,
no tokens and a single agent — and all 60 landed inside 36 minutes on
2026-08-19 under harness v7. #750 named that shape at intake and
deliberately kept the row: a stamp is still a record of what a client did.

What #750 did not change is how the row reads afterwards. Two consumers
took it for a review that ran and found nothing: the machine-review gap
(which then reports "policy satisfied" to the panel, and in ``require``
mode to the verdict) and the practice metrics (where ``reviews`` reads as
coverage). Zero candidates is absence of data, not absence of findings.
"""

from __future__ import annotations

import json

import aiosqlite
from httpx import AsyncClient

from hub import repository as repo
from hub import services
from hub.models import TaskCreate
from hub.services.orchestration import machine_review_gap


async def _reviewed_task(db: aiosqlite.Connection, title: str, **review) -> dict:
    """A task that requires machine review, carrying one report."""
    task = await services.create_task(db, TaskCreate(title=title))
    await repo.update_task(db, task.id, work_type="feature", size="M")
    await db.commit()
    row = dict(await repo.get_task(db, task.id))
    row["submission_generation"] = 1
    await repo.insert_machine_review(
        db, task_id=task.id, submission_generation=1, **review
    )
    await db.commit()
    return row


async def _totals(client: AsyncClient) -> dict:
    resp = await client.get("/api/metrics/practices?since_days=30")
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_gap_rejects_report_without_evidence(db: aiosqlite.Connection):
    """AC-1. The exact v7 shape: a report of the current generation that
    shows nothing having run does not close the requirement."""
    task = await _reviewed_task(
        db,
        "stamped review",
        raw_count=0,
        agent_count=1,
        tokens_spent=None,
        harness_skill="multi-agent-review",
        harness_version=7,
        incomplete=False,
    )

    gap = await machine_review_gap(db, task)

    assert gap is not None, "a report with no evidence must not read as a review"
    assert "без данных" in gap


async def test_gap_accepts_proven_empty_report(db: aiosqlite.Connection):
    """AC-2. Emptiness with a cost behind it is a result, not a stamp — the
    proven-empty path (#769) must keep working, and so must an honest run
    that counted only its own tokens or used more than one agent."""
    billed = await _reviewed_task(
        db, "empty but billed", raw_count=0, agent_count=1, tokens_spent=None
    )
    await repo.set_machine_review_provider_tokens(db, billed["id"], 1, 1_470_000)
    await db.commit()
    assert await machine_review_gap(db, billed) is None

    self_counted = await _reviewed_task(
        db, "empty but counted", raw_count=0, agent_count=1, tokens_spent=350_032
    )
    assert await machine_review_gap(db, self_counted) is None

    many_agents = await _reviewed_task(
        db, "empty but staffed", raw_count=0, agent_count=6, tokens_spent=None
    )
    assert await machine_review_gap(db, many_agents) is None

    adjudicated = await _reviewed_task(
        db,
        "empty after adjudication",
        raw_count=0,
        agent_count=1,
        findings_rejected=json.dumps([{"title": "n", "category": "tests"}]),
    )
    assert await machine_review_gap(db, adjudicated) is None


async def test_practice_metrics_splits_no_data(
    db: aiosqlite.Connection, client: AsyncClient
):
    """AC-3. The window reports both numbers, and the cost-coverage line
    stops counting stamps as reports that forgot their tokens."""
    await _reviewed_task(
        db,
        "real run",
        raw_count=3,
        agent_count=4,
        tokens_spent=None,
        findings_confirmed=json.dumps(
            [{"title": "f", "severity": "low", "category": "tests"}]
        ),
    )
    await _reviewed_task(db, "stamp one", raw_count=0, agent_count=1)
    await _reviewed_task(db, "stamp two", raw_count=0, agent_count=1)

    totals = (await _totals(client))["machine_reviews"]

    assert totals["reports_total"] == 3
    assert totals["reviews"] == 1
    assert totals["no_data_reports"] == 2
    # The one report that ran is also the only one that could have named a
    # cost and did not; the two stamps are not "missing" data, they are the
    # absence of a run.
    assert totals["reports_without_tokens"] == 1


async def test_by_harness_carries_no_data(
    db: aiosqlite.Connection, client: AsyncClient
):
    """AC-4. Per harness version, because that is where the v7 batch shows
    up as "60 reviews" and would otherwise be compared against v8."""
    await _reviewed_task(
        db,
        "v7 stamp",
        raw_count=0,
        agent_count=1,
        harness_version=7,
        harness_skill="multi-agent-review",
    )
    await _reviewed_task(
        db,
        "v8 run",
        raw_count=9,
        agent_count=3,
        tokens_spent=523_000,
        harness_version=8,
        harness_skill="multi-agent-review",
    )

    rows = {
        row["harness_version"]: row for row in (await _totals(client))["by_harness"]
    }

    assert rows[7]["reports_total"] == 1
    assert rows[7]["reviews"] == 0
    assert rows[7]["no_data_reports"] == 1
    assert rows[8]["reviews"] == 1
    assert rows[8]["no_data_reports"] == 0


async def test_by_profile_carries_no_data(
    db: aiosqlite.Connection, client: AsyncClient
):
    """AC-7. The profile block answers "how much it was allowed to spend"
    (#807); a stamp with no profile inflates the undeclared row the same way
    it inflated the harness one, so it is split there too."""
    await _reviewed_task(db, "undeclared stamp", raw_count=0, agent_count=1)
    await _reviewed_task(
        db,
        "lite run",
        raw_count=2,
        agent_count=1,
        tokens_spent=179_000,
        profile="lite",
    )

    rows = {row["profile"]: row for row in (await _totals(client))["by_profile"]}

    assert rows["не заявлен"]["no_data_reports"] == 1
    assert rows["не заявлен"]["reviews"] == 0
    assert rows["lite"]["no_data_reports"] == 0
    assert rows["lite"]["reviews"] == 1


async def test_report_without_evidence_is_still_stored(client: AsyncClient):
    """AC-5. Classification must not turn into refusal: #750 decided the
    report is kept as evidence of what the client did, and that stands."""
    resp = await client.post("/api/tasks", json={"title": "keeps the stamp"})
    task_id = resp.json()["id"]
    await client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": "dev", "kind": "status", "content": "Plan: implement"},
    )
    await client.post(
        f"/api/tasks/{task_id}/pair-start", json={"assigned_agent": "dev"}
    )
    await client.post(f"/api/tasks/{task_id}/submit-review", json={"agent": "dev"})

    stamped = await client.post(
        f"/api/tasks/{task_id}/machine-review",
        json={
            "harness_skill": "multi-agent-review",
            "harness_version": 7,
            "agent_count": 1,
            "raw_count": 0,
            "findings_confirmed": [],
            "findings_rejected": [],
            "incomplete": False,
            "unresolved": [],
            "lost_dimensions": [],
            "agent": "cursor_cloud",
        },
    )
    assert stamped.status_code == 200, stamped.text
    assert stamped.json()["raw_count"] == 0

    card = (await client.get(f"/api/tasks/{task_id}")).json()
    alerts = [
        u["content"]
        for u in card["updates"] or []
        if u["kind"] == "alert" and "raw_count=0" in u["content"]
    ]
    assert len(alerts) == 1, "the #750 alert is the visible half and stays"
