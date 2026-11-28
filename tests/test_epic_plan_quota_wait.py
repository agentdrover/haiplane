"""The signed pilot waits and wakes through both ordinary dispatch doors."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from hub import db as hub_db
from hub.epic_plan_view import PlanTerms
from hub.services import (
    epic_plan,
    epic_plan_authority as authority,
    epic_plan_dispatch as dispatch,
    epic_plan_gates as gates,
    epic_plan_quota as quota,
    epic_plan_transport as transport,
    review_dispatch,
)
from tests.test_epic_plan_approval import pilot  # noqa: F401
from tests.test_epic_plan_dispatch import (
    HEAD,
    PLAN,
    SyntheticReview,
    _new_connection,
    _put_submission,
    _rows,
    prepared,  # noqa: F401
)
from tests.test_executor_orders import keys  # noqa: F401


async def _quota_events(db):
    rows = await hub_db.fetchall(
        db,
        "SELECT kind,payload FROM events WHERE kind LIKE 'epic_plan_quota_%' ORDER BY id",
    )
    return [(row["kind"], json.loads(row["payload"])) for row in rows]


@pytest.mark.parametrize("phase", ["executor", "review"])
@pytest.mark.parametrize("with_reset", [False, True])
@pytest.mark.parametrize("revoked", [False, True])
async def test_quota_wait_survives_restart_and_resumes_once(
    client,
    db,
    db_dsn,
    prepared,  # noqa: F811 - imported pytest fixture
    monkeypatch,
    phase,
    with_reset,
    revoked,
):
    _, headers, reviewer_id, standard = prepared
    monkeypatch.setattr(
        epic_plan,
        "TERMS",
        PlanTerms(
            billing_mode="included_only",
            quota_action="wait_for_reset",
            total_cents=0,
            leaf_cents=0,
        ),
    )
    snapshot = (await client.get(PLAN, headers=headers)).json()
    version = snapshot["version"]
    approved = await client.post(
        PLAN + "/approve", headers=headers, json={"version": version}
    )
    assert approved.status_code == 200, approved.text

    current_time = datetime.now(UTC)
    reset = current_time + timedelta(minutes=15) if with_reset else None

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return current_time

    monkeypatch.setattr(quota, "datetime", Clock)
    available = False
    unknown = False
    readings = []

    async def grant(binding):
        readings.append(binding)
        if unknown:
            return None
        terms = snapshot["snapshot"]["dispatch_authority"][
            f"{binding.phase}_subscription"
        ]
        return (await standard(binding)).model_copy(
            update={
                "ceiling_cents": 0,
                "subscription_terms_fingerprint": terms["terms_fingerprint"],
                "subscription_account_fingerprint": terms["account_fingerprint"],
                "quota_available": available,
                "reset_at": reset if not available else None,
            }
        )

    monkeypatch.setattr(dispatch, "get_grant", grant)
    monkeypatch.setattr(authority, "get_grant", grant)
    if phase == "review":
        available = True
        order_id, detail = await dispatch.start_next(db)
        assert order_id is not None, detail
        readings.clear()
        available = False
        task = await _put_submission(db, 2377, 1, HEAD)

        async def trusted(db, task):
            return task["submission_sha"] == HEAD

        monkeypatch.setattr(gates, "_trusted_ci", trusted)
        backend = SyntheticReview()
        monkeypatch.setattr(transport, "review_transport", lambda: backend)
        request = transport.ReviewRequest(
            "review-quota-fixture",
            "https://example.test/receipt.git",
            task["branch"],
            HEAD,
            "grok-4.7",
            "review the pinned SHA",
            "https://hub.example.test/mcp",
        )

        async def step(connection):
            result = await review_dispatch._launch_metered_review(
                connection,
                dict(task),
                request,
                reviewer_id,
                1,
            )
            return bool(result and not result[0].adopted)
    else:

        async def step(connection):
            result, detail = await dispatch.start_next(connection)
            return result is not None

    assert not await step(db)
    [(kind, payload)] = await _quota_events(db)
    assert kind == "epic_plan_quota_waiting"
    assert (payload["plan_version"], payload["phase"]) == (version, phase)
    assert payload["reset_at"] == (reset.isoformat() if reset else None)
    async with _new_connection(db_dsn) as restarted:
        assert not await step(restarted)
        assert len(readings) == (1 if with_reset else 2)
        assert not [
            row
            for row in await _rows(restarted, "epic_plan_work")
            if row["phase"] == phase
        ]
        if reset is not None:
            current_time = reset
        unknown = True
        assert not await step(restarted)
        assert await _rows(restarted, "epic_plan_quota_waits")
        assert not await _rows(restarted, "epic_plan_escalations")
        assert len(await _quota_events(restarted)) == 1
        unknown = False
        available = True
        if revoked:
            stopped = await client.post(
                PLAN + "/terminate",
                headers=headers,
                json={"state": "revoked", "reason": "Stop pilot", "version": version},
            )
            assert stopped.status_code == 200, stopped.text
            prior_reads = len(readings)
            assert not await step(restarted)
            assert len(readings) == prior_reads
            assert len(await _quota_events(restarted)) == 1
            assert not [
                row
                for row in await _rows(restarted, "epic_plan_work")
                if row["phase"] == phase
            ]
            return
        results = await asyncio.gather(step(db), step(restarted))
        assert results.count(True) == 1

    assert all(binding == readings[0] for binding in readings)
    assert [kind for kind, _ in await _quota_events(db)] == [
        "epic_plan_quota_waiting",
        "epic_plan_quota_resumed",
    ]
    assert not await _rows(db, "epic_plan_quota_waits")
    [work] = [row for row in await _rows(db, "epic_plan_work") if row["phase"] == phase]
    assert work["plan_version"] == version and work["ceiling_cents"] == 0
    if phase == "review":
        assert len(backend.sent) == 1
    else:
        assert len(await _rows(db, "executor_orders")) == 1
