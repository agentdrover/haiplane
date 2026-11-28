"""Signed subscription review travels through the ordinary dispatch path."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hub import config, repository as repo
from hub.epic_plan_view import PlanTerms
from hub.services import (
    epic_plan,
    epic_plan_authority as authority,
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
    _put_submission,
    _rows,
    prepared,  # noqa: F401
)
from tests.test_executor_orders import keys  # noqa: F401


@pytest.fixture
async def included_review(client, db, monkeypatch, prepared):  # noqa: F811
    _, headers, reviewer_id, standard = prepared
    terms = PlanTerms(
        billing_mode="included_only",
        quota_action="wait_for_reset",
        total_cents=0,
        leaf_cents=0,
    )
    monkeypatch.setattr(epic_plan, "TERMS", terms)
    plan = (await client.get(PLAN, headers=headers)).json()
    approved = await client.post(
        PLAN + "/approve", headers=headers, json={"version": plan["version"]}
    )
    assert approved.status_code == 200, approved.text

    async def grant(binding):
        snapshot = plan["snapshot"]["dispatch_authority"][
            f"{binding.phase}_subscription"
        ]
        return (await standard(binding)).model_copy(
            update={
                "ceiling_cents": 0,
                "quota_available": True,
                "subscription_terms_fingerprint": snapshot["terms_fingerprint"],
                "subscription_account_fingerprint": snapshot["account_fingerprint"],
            },
        )

    monkeypatch.setattr(dispatch, "get_grant", grant)
    monkeypatch.setattr(authority, "get_grant", grant)
    order_id, detail = await dispatch.start_next(db)
    assert order_id is not None, detail
    task = dict(await _put_submission(db, 2377, 1, HEAD))
    await repo.record_submission(
        db, task_id=2377, generation=1, sha=HEAD, base_branch="develop"
    )
    await db.commit()
    monkeypatch.setattr(gates, "_trusted_ci", AsyncMock(return_value=True))
    return task, reviewer_id, grant


@pytest.mark.parametrize(
    ("author_model", "forced_model", "allowed"),
    [
        ("gpt-5.3-codex", "", True),
        ("grok-4.7", "", False),
        ("gpt-5.3-codex", "composer-2.5", False),
    ],
)
async def test_ordinary_dispatch_uses_signed_independent_model(
    db, monkeypatch, included_review, author_model, forced_model, allowed
):
    task, reviewer_id, _ = included_review
    await repo.update_task(db, task["id"], submission_model=author_model)
    await db.commit()
    monkeypatch.setattr(config, "CURSOR_REVIEW_MODEL", "")
    monkeypatch.setattr(
        review_dispatch, "_submission_still_live", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(
        review_dispatch, "_local_first_order", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        review_dispatch, "_cloud_config_missing", AsyncMock(return_value=False)
    )
    monkeypatch.setattr(
        review_dispatch, "reviewer_principal_id", AsyncMock(return_value=reviewer_id)
    )
    prepared_models = []

    async def compose(connection, row, **kwargs):
        model = kwargs.get("force_model") or review_dispatch.pick_review_model(
            row["submission_model"]
        )
        prepared_models.append(model)
        return review_dispatch.ReviewOrder(
            model,
            "lite",
            [],
            "pinned review",
            "",
            "",
            SimpleNamespace(state="fixture", passed=()),
        )

    class Review(SyntheticReview):
        async def launch(self, request, grant):
            assert (request.model, grant.binding.model) == ("grok-4.7", "grok-4.7")
            return await super().launch(request, grant)

    backend = Review()
    monkeypatch.setattr(transport, "review_transport", lambda: backend)
    monkeypatch.setattr(review_dispatch, "prepare_review_order", compose)

    assert (
        bool(
            await review_dispatch.maybe_dispatch_review(
                db, task["id"], force_model=forced_model
            )
        )
        is allowed
    )
    assert prepared_models == (["grok-4.7"] if allowed else [])
    assert len(backend.sent) == int(allowed)
    if not allowed:
        assert await _blocker_reasons(db) == ["pilot_signed_review_model_unavailable"]


@pytest.mark.parametrize(
    "bad_fields",
    [
        {"ceiling_cents": 1},
        {"quota_available": None},
        {"subscription_account_fingerprint": "a" * 64},
        {"subscription_terms_fingerprint": "b" * 64},
    ],
)
async def test_review_subscription_refusal_visible_once_without_order(
    db, monkeypatch, included_review, bad_fields
):
    task, reviewer_id, valid = included_review

    async def invalid(binding):
        return (await valid(binding)).model_copy(update=bad_fields)

    monkeypatch.setattr(authority, "get_grant", invalid)
    for _ in range(2):
        ready, permit = await gates.reserve_review(
            db,
            task,
            channel="cloud",
            model="grok-4.7",
            principal_id=reviewer_id,
            attempt=1,
        )
        assert not ready and permit is None
    assert not [
        row for row in await _rows(db, "epic_plan_work") if row["phase"] == "review"
    ]
    assert await _blocker_reasons(db) == ["pilot_subscription_unverified"]


async def _blocker_reasons(db):
    return [
        json.loads(row["payload"])["reason"]
        for row in await _rows(db, "events")
        if row["kind"] == "epic_plan_review_blocked"
    ]
