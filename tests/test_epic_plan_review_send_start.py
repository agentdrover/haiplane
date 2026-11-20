"""Metered review starts once and is ordered against plan termination."""

from __future__ import annotations

import json

import pytest

from hub import db as hub_db
from hub import repository as repo
from hub.services import (
    epic_plan,
    epic_plan_dispatch as dispatch,
    epic_plan_gates as gates,
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


@pytest.mark.parametrize(
    "boundary", ["revoked-before", "revoked-after", "risk-before", "reviewer-before"]
)
async def test_review_send_start_orders_against_plan_revocation(
    client,
    db,
    db_dsn,
    prepared,  # noqa: F811 - imported pytest fixture
    monkeypatch,
    boundary,
):
    _, owner_headers, reviewer_id, _ = prepared
    executor, reason = await dispatch.start_next(db)
    assert executor is not None, reason
    task = await _put_submission(db, 2377, 1, HEAD)
    version = (await epic_plan.active_decision(db)).version

    async def trusted(db, task):
        return task["submission_sha"] == HEAD

    async def revoke():
        result = await client.post(
            PLAN + "/terminate",
            headers=owner_headers,
            json={"state": "revoked", "reason": "Stop pilot", "version": version},
        )
        assert result.status_code == 200, result.text

    ledgers = []
    begin = gates.begin_review_send

    async def gated_begin(connection, permit):
        ledgers.append(permit)
        async with hub_db.write_transaction(connection):
            assert not await begin(connection, permit)
        if boundary == "revoked-before":
            await revoke()
        if boundary == "risk-before":
            await repo.update_task(connection, 2377, risk_class="R3")
            await connection.commit()
        if boundary == "reviewer-before":
            await connection.execute(
                "UPDATE principals SET status='disabled' WHERE id=?", (reviewer_id,)
            )
            await connection.commit()
        return await begin(connection, permit)

    class ReplyAfterTermination(SyntheticReview):
        async def launch(self, request, grant):
            # The provider adapter observes the durable start before any effect.
            async with _new_connection(db_dsn) as observer:
                [row] = [
                    r
                    for r in await _rows(observer, "epic_plan_work")
                    if r["phase"] == "review"
                ]
                assert row["review_send_started_at"]
                assert not await begin(observer, ledgers[0])
            result = await super().launch(request, grant)
            await revoke()
            return result

    backend = ReplyAfterTermination()
    monkeypatch.setattr(gates, "_trusted_ci", trusted)
    monkeypatch.setattr(transport, "review_transport", lambda: backend)
    monkeypatch.setattr(gates, "begin_review_send", gated_begin)
    request = transport.ReviewRequest(
        "review-send-fixture",
        "https://example.test/receipt.git",
        task["branch"],
        HEAD,
        "independent",
        "review the pinned SHA",
        "https://hub.example.test/mcp",
    )
    first = await review_dispatch._launch_metered_review(
        db, dict(task), request, reviewer_id, 1
    )
    async with _new_connection(db_dsn) as restarted:
        assert (
            await review_dispatch._launch_metered_review(
                restarted,
                dict(task),
                request,
                reviewer_id,
                1,
            )
            is None
        )
        [permit] = ledgers
        assert not await begin(restarted, permit)

    [review] = [
        row for row in await _rows(db, "epic_plan_work") if row["phase"] == "review"
    ]
    events = await hub_db.fetchall(
        db,
        "SELECT kind,payload FROM events WHERE kind IN "
        "('epic_plan_review_send_started','epic_plan_revoked') ORDER BY id",
    )
    if boundary == "revoked-after":
        assert first is not None and first[0].agent_id == "synthetic-review"
        assert len(backend.sent) == 1
        assert [event["kind"] for event in events] == [
            "epic_plan_review_send_started",
            "epic_plan_revoked",
        ]
        started = json.loads(events[0]["payload"])
        assert started["reservation_id"] == review["id"]
        assert started["started_at"] == review["review_send_started_at"]
        assert started["plan_version"] == version and started["sha"] == HEAD
    else:
        assert first is None and not backend.sent
        assert review["review_send_started_at"] is None
        assert [event["kind"] for event in events] == (
            ["epic_plan_revoked"] if boundary == "revoked-before" else []
        )

    # Legacy counter is preserved, while old/replayed permits never send again.
    assert review["send_count"] == 1


@pytest.mark.parametrize("legacy_send_count", [1, 2])
async def test_legacy_review_reservation_only_reconciles(
    db,
    db_dsn,
    prepared,  # noqa: F811 - imported pytest fixture
    monkeypatch,
    legacy_send_count,
):
    _, _, reviewer_id, _ = prepared
    executor, reason = await dispatch.start_next(db)
    assert executor is not None, reason
    task = await _put_submission(db, 2377, 1, HEAD)

    async def trusted(db, task):
        return task["submission_sha"] == HEAD

    monkeypatch.setattr(gates, "_trusted_ci", trusted)
    backend = SyntheticReview()
    monkeypatch.setattr(transport, "review_transport", lambda: backend)
    ready, original = await gates.reserve_review(
        db,
        task,
        channel="cloud",
        model="independent",
        principal_id=reviewer_id,
        attempt=1,
    )
    assert ready and original is not None
    # Rows written before the send-start migration have no local marker.
    await db.execute(
        "UPDATE epic_plan_work SET send_count=? WHERE id=?",
        (legacy_send_count, original.id),
    )
    await db.commit()
    request = transport.ReviewRequest(
        "legacy-review",
        "https://example.test/receipt.git",
        task["branch"],
        HEAD,
        "independent",
        "review the pinned SHA",
        "https://hub.example.test/mcp",
    )
    async with _new_connection(db_dsn) as restarted:
        assert (
            await review_dispatch._launch_metered_review(
                restarted, dict(task), request, reviewer_id, 1
            )
            is None
        )
        assert not backend.sent
        backend.sent[original.grant.reference] = "legacy-agent"
        adopted, permit = await review_dispatch._launch_metered_review(
            restarted, dict(task), request, reviewer_id, 1
        )
        assert adopted.adopted and adopted.agent_id == "legacy-agent"
        assert permit.id == original.id and permit.reconcile
        assert not await gates.begin_review_send(restarted, permit)
    assert not await hub_db.fetchall(
        db, "SELECT id FROM events WHERE kind='epic_plan_review_send_started'"
    )
