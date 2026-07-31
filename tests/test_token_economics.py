"""Cost per finding is computed over one sample, not two (#516).

``tokens_per_confirmed`` divided every token reported in the window by every
confirmed finding in the window — but only some reports name a cost. The
numerator covered 22 reports on production while the denominator covered all
35, and the answer came out 38% low: 268846 against an honest 433623.

An understated number is worse here than a missing one. Nobody re-checks a
figure that looks plausible, and this one is the headline of the review
economics — the number used to compare harness versions and decide whether a
review pays for itself.
"""

from __future__ import annotations

import json

import aiosqlite
from httpx import AsyncClient

from hub import repository as repo
from hub import services
from hub.models import TaskCreate


async def _review(
    db: aiosqlite.Connection,
    *,
    tokens: int | None,
    confirmed: int,
    version: int = 6,
) -> None:
    """Record one machine review with the given cost and finding count."""
    tv = await services.create_task(db, TaskCreate(title=f"reviewed {version}"))
    await db.commit()
    await repo.insert_machine_review(
        db,
        task_id=tv.id,
        submission_generation=1,
        raw_count=confirmed,
        incomplete=False,
        tokens_spent=tokens,
        duration_ms=1000,
        harness_skill="multi-agent-review",
        harness_version=version,
        findings_confirmed=json.dumps(
            [
                {"title": f"f{i}", "severity": "low", "category": "tests"}
                for i in range(confirmed)
            ]
        ),
        findings_rejected="[]",
    )
    await db.commit()


async def _totals(client: AsyncClient) -> dict:
    resp = await client.get("/api/metrics/practices?since_days=30")
    assert resp.status_code == 200, resp.text
    return resp.json()["machine_reviews"]


async def test_cost_per_finding_ignores_findings_that_reported_no_cost(
    db: aiosqlite.Connection, client: AsyncClient
):
    """AC-1. The report that stayed silent about its cost must not dilute the
    price of the one that spoke."""
    await _review(db, tokens=1_000_000, confirmed=2)
    await _review(db, tokens=None, confirmed=8)

    totals = await _totals(client)

    assert totals["tokens_total"] == 1_000_000
    assert totals["confirmed_total"] == 10, "all findings are still counted"
    assert totals["confirmed_with_tokens"] == 2
    assert totals["tokens_per_confirmed"] == 500_000, (
        "dividing by all 10 would report 100000 — five times too cheap"
    )


async def test_coverage_is_reported_next_to_the_price(
    db: aiosqlite.Connection, client: AsyncClient
):
    """AC-2. A correct ratio over an unstated fraction of the window is still
    misleading; the reader has to see how much of the sample it covers."""
    await _review(db, tokens=500_000, confirmed=3)
    await _review(db, tokens=None, confirmed=4)
    await _review(db, tokens=None, confirmed=1)

    totals = await _totals(client)

    assert totals["reviews"] == 3
    assert totals["reports_without_tokens"] == 2
    assert totals["confirmed_with_tokens"] == 3
    assert totals["confirmed_total"] == 8


async def test_no_reported_cost_at_all_means_no_price(
    db: aiosqlite.Connection, client: AsyncClient
):
    """The edge that must not quietly return zero.

    When nothing in the window named a cost there is no price to give.
    Reporting 0 would say reviews are free — the same defect the task set out
    to fix, restated."""
    await _review(db, tokens=None, confirmed=4)

    totals = await _totals(client)

    assert totals["tokens_per_confirmed"] is None
    assert totals["confirmed_with_tokens"] == 0


async def test_a_stored_cost_is_returned_as_given(
    db: aiosqlite.Connection, client: AsyncClient
):
    """What is actually testable of AC-1 as written.

    The task asks that the harness pass tokens_spent — which the hub cannot
    verify, because the caller is an external agent. What the hub owes is that
    a value it is given survives, and that an absent one is never replaced by
    a zero. On production this half already works: harness v4-v6 reports all
    carry tokens; the empty ones are v1 and older."""
    await _review(db, tokens=777_777, confirmed=1, version=6)

    totals = await _totals(client)

    assert totals["tokens_total"] == 777_777
    assert totals["reports_without_tokens"] == 0
