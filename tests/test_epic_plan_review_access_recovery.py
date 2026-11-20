"""Recover a review order and the credential actually delivered to its sandbox."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from hub import config, repository as repo
from hub.integrations.cursor_cloud import Reconciliation
from hub.services import chat_pair, review_dispatch
from hub.services import epic_plan_dispatch as dispatch
from hub.services import epic_plan_gates as gates
from hub.services import epic_plan_transport as transport
from tests.test_auto_verdict import _CLEAN_REVIEW
from tests.test_epic_plan_approval import pilot  # noqa: F401
from tests.test_epic_plan_dispatch import (
    HEAD,
    SyntheticReview,
    _new_connection,
    _put_submission,
    _rows,
    prepared,  # noqa: F401
)
from tests.test_executor_orders import keys  # noqa: F401


@pytest.mark.parametrize("early_report", [False, True])
async def test_adopted_review_binds_original_access_session(
    client,
    db,
    db_dsn,
    prepared,  # noqa: F811 - imported pytest fixture
    monkeypatch,
    early_report,
):
    _, _, reviewer_id, _ = prepared
    order, detail = await dispatch.start_next(db)
    assert order is not None, detail
    task = await _put_submission(db, 2377, 1, HEAD)
    await repo.record_submission(
        db, task_id=2377, generation=1, sha=HEAD, base_branch="develop"
    )
    await db.commit()

    async def yes(*args, **kwargs):
        return True

    async def no(*args, **kwargs):
        return False

    async def no_local(*args, **kwargs):
        return None

    async def principal(*args, **kwargs):
        return reviewer_id

    async def compose(connection, task, **kwargs):
        code = (
            "fixture-placeholder"
            if kwargs.get("defer_access_code")
            else await review_dispatch._access_code(connection, 2377, 1, reviewer_id)
        )
        return review_dispatch.ReviewOrder(
            "grok-4.7",
            "lite",
            [],
            json.dumps({"code": code}),
            "",
            "",
            SimpleNamespace(state="fixture", passed=()),
            access_code=code,
        )

    monkeypatch.setattr(gates, "_trusted_ci", yes)
    monkeypatch.setattr(review_dispatch, "_policy_and_novelty_allow", yes)
    monkeypatch.setattr(review_dispatch, "_submission_still_live", yes)
    monkeypatch.setattr(review_dispatch, "_local_first_order", no_local)
    monkeypatch.setattr(review_dispatch, "_cloud_config_missing", no)
    monkeypatch.setattr(review_dispatch, "reviewer_principal_id", principal)
    monkeypatch.setattr(review_dispatch, "prepare_review_order", compose)
    monkeypatch.setattr(config, "AUTO_APPROVE_MAX_CLASS", "off")
    accepted = False
    delivered = []

    class LostReply(SyntheticReview):
        async def launch(self, request, grant):
            delivered.append(json.loads(request.prompt)["code"])
            return review_dispatch._Started("", "", False, True, 1, None)

        async def lookup(self, request, grant):
            return Reconciliation(
                "provider-original" if accepted else "",
                "provider-run" if accepted else "",
                True,
            )

    monkeypatch.setattr(transport, "review_transport", LostReply)

    async def redeem(code):
        response = await client.post("/api/auth/chat-pair/redeem", json={"code": code})
        assert response.status_code == 200, response.text
        return {"Authorization": f"Bearer {response.json()['token']}"}

    async def report(headers):
        response = await client.post(
            "/api/tasks/2377/machine-review",
            json={**_CLEAN_REVIEW, "expected_generation": 1, "reviewed_sha": HEAD},
            headers=headers,
        )
        assert response.status_code == 200, response.text

    assert task["submission_sha"] == HEAD
    assert not await review_dispatch.maybe_dispatch_review(db, 2377)
    [original] = delivered
    [source] = [
        row for row in await _rows(db, "chat_pair_codes") if row["kind"] == "reviewer"
    ]
    if early_report:
        await report(await redeem(original))
        assert (await _rows(db, "machine_reviews"))[0]["dispatch_id"] is None

    async with _new_connection(db_dsn) as restarted:
        # Let the ordinary dispatcher claim expire between poller passes.
        await restarted.execute("DELETE FROM review_order_claims")
        await restarted.commit()
        assert not await review_dispatch.maybe_dispatch_review(restarted, 2377)
        accepted = True
        assert await review_dispatch.maybe_dispatch_review(restarted, 2377)

    [retained] = [
        row for row in await _rows(db, "chat_pair_codes") if row["kind"] == "reviewer"
    ]
    assert retained["id"] == source["id"]
    [reservation] = [
        row for row in await _rows(db, "epic_plan_work") if row["phase"] == "review"
    ]
    assert reservation["reviewer_code_id"] == source["id"]
    if not early_report:
        await report(await redeem(original))
    unrelated, _ = await chat_pair.issue_code(
        db, reviewer_id, kind="reviewer", bound_task_id=2377, bound_generation=1
    )
    await report(await redeem(unrelated))
    [placed] = await _rows(db, "review_dispatches")
    assert (placed["agent_id"], placed["run_id"]) == (
        "provider-original",
        "provider-run",
    )
    assert len(delivered) == 1
    for table in ("chat_pair_sessions", "machine_reviews"):
        rows = [
            row for row in await _rows(db, table) if row["principal_id"] == reviewer_id
        ]
        assert [row["dispatch_id"] for row in rows] == [placed["id"], None]
        assert rows[0]["code_id"] == source["id"]
