"""Pilot paid-transport recovery tests using synthetic provider boundaries."""

from __future__ import annotations

import pytest

from hub import repository as repo
from hub.integrations.cursor_cloud import Reconciliation
from hub.services import (
    epic_plan,
    epic_plan_authority as authority,
    epic_plan_dispatch as dispatch,
    epic_plan_gates as gates,
    epic_plan_spend as spend,
    epic_plan_transport as transport,
    executor_orders,
    review_dispatch,
)
from hub.services.epic_plan_authority import get_charge as get_verified_charge
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


async def test_retry_reconciles_single_external_order(
    db,
    db_dsn,
    prepared,  # noqa: F811 - imported pytest fixture
    monkeypatch,
):
    _, _, reviewer_id, _ = prepared
    order_id, reason = await dispatch.start_next(db)
    assert order_id is not None, reason
    task = await _put_submission(db, 2377, 1, HEAD)

    async def trusted_ci(db, task):
        return task["submission_sha"] == HEAD

    monkeypatch.setattr(gates, "_trusted_ci", trusted_ci)
    request = transport.ReviewRequest(
        "review-fixture-unknown",
        "https://example.test/receipt.git",
        task["branch"],
        HEAD,
        "grok-fixture",
        "review the pinned submission",
        "https://hub.example.test/mcp",
    )
    launched = []
    lookups = []

    class LostReplyTransport(SyntheticReview):
        async def launch(self, sent_request, grant):
            launched.append((sent_request, grant))
            return review_dispatch._Started("", "", False, True, 1, None)

        async def lookup(self, sent_request, grant):
            lookups.append((sent_request, grant))
            if len(lookups) == 1:
                return Reconciliation("", "", False)
            if len(lookups) == 2:
                return Reconciliation("", "", True)
            if len(lookups) == 3:
                return Reconciliation("agent-original", "", True)
            return Reconciliation("agent-original", "run-original", True)

    monkeypatch.setattr(transport, "review_transport", LostReplyTransport)
    first, permit = await review_dispatch._launch_metered_review(
        db, dict(task), request, reviewer_id, 1
    )
    assert first.blind and not first.agent_id
    assert not permit.reconcile
    async with _new_connection(db_dsn) as restarted:
        for _ in range(3):
            assert (
                await review_dispatch._launch_metered_review(
                    restarted, dict(task), request, reviewer_id, 1
                )
                is None
            )
        adopted, recovered = await review_dispatch._launch_metered_review(
            restarted, dict(task), request, reviewer_id, 1
        )

    assert adopted.adopted
    assert (adopted.agent_id, adopted.run_id) == ("agent-original", "run-original")
    assert recovered.id == permit.id
    [(sent_request, grant)] = launched
    assert sent_request == request
    for looked_up, lookup_grant in lookups:
        assert looked_up == request
        assert lookup_grant.binding == grant.binding
        assert lookup_grant.reference == grant.reference
        assert lookup_grant.ceiling_cents == grant.ceiling_cents
    [review] = [
        row for row in await _rows(db, "epic_plan_work") if row["phase"] == "review"
    ]
    assert review["id"] == permit.id
    assert review["sha"] == request.head_sha
    assert review["model"] == request.model
    assert review["ceiling_cents"] == grant.ceiling_cents
    [notice] = await _rows(db, "epic_plan_review_notice_keys")
    assert notice["reason"] == "pilot_review_order_unreconciled"


@pytest.mark.parametrize(
    "authority_available", [False, True], ids=["missing-authority", "late-charge"]
)
async def test_missing_authority_blocks_spend_and_late_cost_is_recorded_once(
    client,
    db,
    db_dsn,
    prepared,  # noqa: F811 - imported pytest fixture
    monkeypatch,
    authority_available,
):
    _, owner_headers, _, _ = prepared
    if not authority_available:

        async def missing_grant(binding):
            return None

        monkeypatch.setattr(dispatch, "get_grant", missing_grant)
        assert await dispatch.start_next(db) == (
            None,
            "pilot_spending_authority_unavailable",
        )
        assert not await _rows(db, "executor_orders")
        assert await spend.usage(db, 2377) == (0, 0, False)
        return

    await repo.update_task(db, 2377, branch="task-2377/pilot")
    await db.commit()
    order_id, reason = await dispatch.start_next(db)
    assert order_id is not None, reason
    await executor_orders.deliver_orders(db, "k8")
    decision = await epic_plan.active_decision(db)
    response = await client.post(
        PLAN + "/terminate",
        headers=owner_headers,
        json={"state": "revoked", "reason": "Stop pilot", "version": decision.version},
    )
    assert response.status_code == 200, response.text
    for _ in range(2):
        outcome = await executor_orders.report_outcome(
            db, "k8", order_id, outcome="submitted", sha=HEAD, branch="task-2377/pilot"
        )
        assert outcome["outcome"] == "submitted"

    [work] = await _rows(db, "epic_plan_work")
    binding = spend.binding_of(work)
    reference = work["authority_ref"]
    ceiling = work["ceiling_cents"]
    calls = []

    async def charge(route, payload):
        calls.append((route, payload))
        return authority.ChargeReceipt(
            reference=reference, binding=binding, charged_cents=37
        ).model_dump_json()

    monkeypatch.setattr(authority, "_call", charge)
    monkeypatch.setattr(authority, "get_charge", get_verified_charge)
    assert await spend.usage(db, 2377) == (ceiling, ceiling, False)
    async with _new_connection(db_dsn) as restarted:
        await dispatch.sweep(restarted)
        await dispatch.sweep(restarted)
        assert await dispatch.start_next(restarted) == (None, "plan_revoked")

    assert calls == [
        ("charge", {"binding": binding.model_dump(), "reference": reference})
    ]
    assert await spend.usage(db, 2377) == (37, 37, False)
    [settled] = await _rows(db, "epic_plan_work")
    assert settled["id"] == work["id"] and settled["charged_cents"] == 37
    [saved] = await _rows(db, "executor_orders")
    assert saved["id"] == order_id and saved["report_outcome"] == "submitted"
    assert (await repo.get_task(db, 2377))["status"] == "open"


async def test_actual_payment_terms_are_part_of_signature(
    client,
    db,
    prepared,  # noqa: F811 - imported pytest fixture
    monkeypatch,
):
    from hub.epic_plan_view import PlanTerms

    _, headers, _, standard = prepared
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

    async def plan():
        response = await client.get(PLAN, headers=headers)
        assert response.status_code == 200
        return response.json()

    current = await plan()
    assert current["state"] == "stopped"
    assert current["snapshot"]["terms"]["total_cents"] == 0
    original_terms = current["snapshot"]["dispatch_authority"]
    assert original_terms["executor_subscription"]["provider"] == "OpenAI"
    assert original_terms["review_subscription"]["model"] == "grok-4.7"
    approved = await client.post(
        PLAN + "/approve", headers=headers, json={"version": current["version"]}
    )
    assert approved.status_code == 200, approved.text
    proof = SyntheticReview().subscription()
    with monkeypatch.context() as changed:
        changed.setattr(
            SyntheticReview,
            "subscription",
            lambda self: proof.model_copy(update={"account_fingerprint": "1" * 64}),
        )
        different = await plan()
        assert different["version"] != current["version"]
        assert different["reason"] == "plan_changed"
        assert await dispatch.start_next(db) == (None, "plan_changed")
    with monkeypatch.context() as missing:
        missing.setattr(SyntheticReview, "subscription", lambda self: None)
        assert (await plan())["snapshot"]["dispatch_authority"] is None
        assert await dispatch.start_next(db) == (None, "plan_changed")
    # Existing paid grants remain unusable, even with a live owner signature.
    assert await dispatch.start_next(db) == (None, "pilot_subscription_unverified")
    available = False

    async def grant(binding):
        return (await standard(binding)).model_copy(
            update={
                "ceiling_cents": 0,
                "subscription_account_fingerprint": original_terms[
                    "executor_subscription"
                ]["account_fingerprint"],
                "subscription_terms_fingerprint": original_terms[
                    "executor_subscription"
                ]["terms_fingerprint"],
                "quota_available": available,
            }
        )

    monkeypatch.setattr(dispatch, "get_grant", grant)
    assert await dispatch.start_next(db) == (None, "pilot_wait_for_quota_reset")
    assert not await _rows(db, "executor_orders")
    assert not await _rows(db, "epic_plan_escalations")
    available = True
    order, reason = await dispatch.start_next(db)
    assert order is not None, reason
    [hold] = await _rows(db, "epic_plan_work")
    assert hold["ceiling_cents"] == 0 and hold["charged_cents"] is None
    assert (await plan())["version"] == current["version"]
