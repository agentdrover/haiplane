"""Internal pilot tests. The budget authority and Git/CI reads are synthetic."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import HTTPException

from hub import db as hub_db, repository as repo
from hub.agent_outbox_models import OutboxPublish
from hub.services import (
    admin,
    agent_outbox,
    agent_outbox_delivery,
    epic_plan,
    epic_plan_authority as authority,
    epic_plan_dispatch as dispatch,
    epic_plan_escalation as escalation,
    epic_plan_gates as gates,
    epic_plan_spend as spend,
    executor_orders,
    orchestration,
    role_orders,
)
from tests.executor_slot_support import seed_slot_skills
from tests.test_epic_plan_approval import PLAN, _approved, pilot  # noqa: F401
from tests.test_executor_orders import keys  # noqa: F401

BASE = "a" * 40
HEAD = "b" * 40
AUTHORITY_URL = "https://budget.example.test/pilot"
AUTHORITY_TOKEN = "fixture-synthetic-auth"  # pragma: allowlist secret


@pytest.fixture
async def prepared(
    client,
    db,
    monkeypatch,
    pilot,  # noqa: F811 - imported pytest fixture
    keys,  # noqa: F811 - imported pytest fixtures
):
    pid, actors = pilot
    agent, _ = actors["plan-agent"]
    owner_id, owner = actors["plan-owner"]
    reviewer = await admin.create_principal(
        db, kind="agent", username="pilot-independent-reviewer", role_slug="agent"
    )
    reviewer_id = reviewer["id"]
    await seed_slot_skills(db)
    await repo.update_project(
        db,
        pid,
        repo="example/receipt",
        default_branch="develop",
        gate_policy=json.dumps(
            {"executor_launch": "manual", "review": "dispatch", "verdict": "auto"}
        ),
    )
    await db.commit()
    response = await client.post(
        "/api/admin/executor-hosts",
        json={"name": "k8", "public_key_pem": keys.k8.public_pem},
        headers=owner,
    )
    assert response.status_code == 200, response.text
    await role_orders.admit_host(db, "k8", role_orders.ROLE_ROADMAP, pid)
    monkeypatch.setenv("HAIPLANE_EPIC_PLAN_COST_AUTHORITY_URL", AUTHORITY_URL)
    monkeypatch.setenv("HAIPLANE_EPIC_PLAN_COST_AUTHORITY_TOKEN", AUTHORITY_TOKEN)
    monkeypatch.setenv("HAIPLANE_EPIC_PLAN_DISPATCH_PRINCIPAL_ID", str(agent))
    monkeypatch.setenv("HAIPLANE_EPIC_PLAN_EXECUTOR_MODEL", "codex-fixture")

    async def origin(db, task):
        return task["submission_sha"] or BASE

    async def grant(binding: authority.WorkBinding):
        return authority.SpendingGrant(
            binding=binding,
            reference=hashlib.sha256(binding.model_dump_json().encode()).hexdigest(),
            ceiling_cents=100,
            expires_at=datetime.now(UTC) + timedelta(hours=2),
            workspace_ready=True,
            hard_cap_enforced=True,
            idempotent_reference=True,
            source_ci_verified=True,
            independent_review_ready=True,
            owner_checkpoint_ready=True,
        )

    async def no_charge(binding, reference):
        return None

    async def route_ready(db, project, excluded):
        return reviewer_id not in excluded

    monkeypatch.setattr(dispatch, "review_route_ready", route_ready)
    monkeypatch.setattr(dispatch, "_starting_sha", origin)
    monkeypatch.setattr(authority, "get_grant", grant)
    monkeypatch.setattr(dispatch, "get_grant", grant)
    monkeypatch.setattr(authority, "get_charge", no_charge)
    await _approved(client, pilot)
    await repo.update_task(db, 2377, dor_passed=True)
    await db.commit()
    return owner_id, owner, reviewer_id, grant


async def _rows(db, table):
    # The only call sites use constant table identifiers in synthetic tests.
    return await hub_db.fetchall(db, f"SELECT * FROM {table}")  # nosec B608


@asynccontextmanager
async def _new_connection(db_dsn):
    connection = await hub_db.connect(db_dsn)
    try:
        yield connection
    finally:
        await connection.close()


async def test_concurrent_wakeup_reserves_one_order(db, db_dsn, prepared):
    async with _new_connection(db_dsn) as second:
        results = await asyncio.gather(
            dispatch.start_next(db), dispatch.start_next(second)
        )
    issued = [order for order, _ in results if order is not None]
    assert len(issued) == 1
    reservations = await _rows(db, "epic_plan_work")
    [reservation] = reservations
    assert reservation["executor_order_id"] == issued[0]
    assert reservation["task_id"] == 2377
    assert reservation["phase"] == "executor"
    assert reservation["principal_id"] != prepared[0]
    assert reservation["host"] == "k8"
    assert (reservation["sha"], reservation["generation"], reservation["attempt"]) == (
        BASE,
        1,
        1,
    )
    assert len(await _rows(db, "executor_orders")) == 1
    async with _new_connection(db_dsn) as restarted:
        assert await dispatch.start_next(restarted) == (None, "pilot_order_in_flight")
        delivered = await executor_orders.deliver_orders(restarted, "k8")
        replay = await executor_orders.deliver_orders(restarted, "k8")
    assert [x["id"] for x in delivered] == [x["id"] for x in replay] == issued
    assert (await repo.get_task(db, 2378))["status"] == "open"


async def _put_submission(db, task_id: int, generation: int, sha: str):
    await repo.update_task(
        db,
        task_id,
        status="review",
        branch=f"task-{task_id}/pilot",
        submission_sha=sha,
        submission_generation=generation,
        risk_class="R2",
    )
    await db.commit()
    return await repo.get_task(db, task_id)


async def test_rework_round_is_bound_to_new_sha(db, prepared, monkeypatch):
    await repo.update_task(db, 2377, branch="task-2377/pilot")
    await db.commit()
    first_order, first_reason = await dispatch.start_next(db)
    assert first_order is not None, first_reason
    [old_order] = await _rows(db, "executor_orders")
    await executor_orders.close_order(
        db,
        dict(old_order),
        executor_orders.CLOSE_SUBMITTED,
        executor_orders.RUN_OUTCOME_FINISHED,
        return_task=False,
    )
    await db.commit()
    await _put_submission(db, 2377, 1, HEAD)
    await repo.update_task(
        db,
        2377,
        status="running",
        review_verdict="changes_requested",
        review_verdict_generation=1,
        review_findings=json.dumps(
            [
                {
                    "id": 1,
                    "severity": "high",
                    "file": "app/main.py",
                    "line": 12,
                    "message": "Preserve the original receipt total",
                },
            ]
        ),
    )
    await db.commit()
    second_order, second_reason = await dispatch.start_next(db)
    assert second_order is not None and second_order != first_order, second_reason
    ordered = await _rows(db, "executor_orders")
    assert ordered[1]["task_id"] == 2377 and ordered[1]["generation"] == 2
    assert "Preserve the original receipt total" in ordered[1]["order_text"]
    [_, repaired] = await _rows(db, "epic_plan_work")
    assert repaired["sha"] == HEAD and repaired["generation"] == 2
    repaired_sha = "d" * 40
    owner_id, _, reviewer_id, _ = prepared

    async def trusted(db, task):
        return task["submission_sha"] in {HEAD, repaired_sha}

    monkeypatch.setattr(gates, "_trusted_ci", trusted)
    stale = await _put_submission(db, 2377, 2, HEAD)
    assert not await gates.can_prepare_review(db, stale)
    task = await _put_submission(db, 2377, 2, repaired_sha)
    assert await gates.can_prepare_review(db, task)
    assert await gates.reserve_review(
        db,
        task,
        channel="cloud",
        model="independent",
        principal_id=owner_id,
        attempt=1,
    ) == (False, None)
    ready, reservation = await gates.reserve_review(
        db,
        task,
        channel="cloud",
        model="independent",
        principal_id=reviewer_id,
        attempt=1,
    )
    assert ready and reservation is not None
    replay_ready, replay = await gates.reserve_review(
        db,
        task,
        channel="cloud",
        model="independent",
        principal_id=reviewer_id,
        attempt=1,
    )
    assert replay_ready and replay is not None and replay.reconcile
    assert replay.id == reservation.id
    assert len(await _rows(db, "epic_plan_work")) == 3
    assert (
        await gates.approval_refusal(db, task, automatic=True)
        == "pilot_review_unavailable"
    )
    dispatch_id = await repo.create_review_dispatch(
        db,
        task_id=2377,
        submission_generation=2,
        agent_id="synthetic-review",
        run_id="run-fixture",
        model="independent",
        reviewer_principal_id=reviewer_id,
    )
    await gates.attach_dispatch(db, reservation, dispatch_id)
    await db.execute(
        """INSERT INTO machine_reviews (
            task_id,submission_generation,principal_id,dispatch_id,reviewed_sha,
            incomplete,findings_confirmed,unresolved
        ) VALUES (2377,2,?,?,?,0,'[]','[]')""",
        (reviewer_id, dispatch_id, repaired_sha),
    )
    await db.commit()
    assert await gates.approval_refusal(db, task, automatic=True) == ""
    stale = await _put_submission(db, 2377, 2, "c" * 40)
    assert (
        await gates.approval_refusal(db, stale, automatic=True) == "pilot_ci_unverified"
    )


async def test_exception_escalates_once(db, prepared, monkeypatch):
    _, _, _, standard = prepared

    async def too_expensive(binding):
        return (await standard(binding)).model_copy(update={"ceiling_cents": 1501})

    monkeypatch.setattr(dispatch, "get_grant", too_expensive)
    assert await dispatch.start_next(db) == (None, "plan_budget_exhausted")
    decision = await epic_plan.active_decision(db)
    task = await repo.get_task(db, 2377)
    assert not await escalation.maybe_notify(
        db, decision, task, "plan_budget_exhausted"
    )
    [case] = await _rows(db, "epic_plan_escalations")
    assert case["reason"] == "plan_budget_exhausted"
    row = (
        await hub_db.fetchall(
            db, "SELECT payload FROM events WHERE id=?", (case["event_id"],)
        )
    )[0]
    event = json.loads(row["payload"])
    assert event["plan_version"] == decision.version
    assert {item["action"] for item in event["options"]} == {"retry", "stop"}
    assert event["recommendation"] == "stop"
    assert await spend.usage(db, 2377) == (0, 0, False)


async def test_late_result_cannot_spend_or_deliver(client, db, prepared):
    _, headers, _, _ = prepared
    await repo.update_task(db, 2377, branch="task-2377/pilot")
    await db.commit()
    order, why = await dispatch.start_next(db)
    assert order is not None, why
    await executor_orders.deliver_orders(db, "k8")
    version = (await epic_plan.active_decision(db)).version
    ended = await client.post(
        PLAN + "/terminate",
        headers=headers,
        json={"state": "revoked", "reason": "Пилот остановлен", "version": version},
    )
    assert ended.status_code == 200, ended.text
    feedback = await executor_orders.report_outcome(
        db, "k8", order, outcome="submitted", sha=HEAD, branch="task-2377/pilot"
    )
    assert feedback["outcome"] == "submitted"
    assert await dispatch.start_next(db) == (None, "plan_revoked")
    task = await _put_submission(db, 2377, 1, HEAD)
    await repo.update_task(db, 2377, pr_number=22)
    await db.commit()
    outcome = await orchestration.merge_if_approval_stands(
        db, dict(task, pr_number=22), {}
    )
    assert outcome == (orchestration.MergeStep.RERUN_HOLD, "plan_revoked")
    assert await gates.approval_refusal(db, {"id": 5312}, automatic=True) == ""


async def test_old_owner_signature_cannot_launch(client, db, prepared, monkeypatch):
    _, headers, _, _ = prepared
    monkeypatch.delenv("HAIPLANE_EPIC_PLAN_COST_AUTHORITY_URL")
    view = await client.get(PLAN, headers=headers)
    assert view.json()["snapshot"]["schema_version"] == "1"
    unextended = await client.post(
        PLAN + "/approve", headers=headers, json={"version": view.json()["version"]}
    )
    assert unextended.status_code == 200, unextended.text
    assert await dispatch.start_next(db) == (None, "pilot_execution_not_authorized")
    monkeypatch.setenv("HAIPLANE_EPIC_PLAN_COST_AUTHORITY_URL", AUTHORITY_URL)
    assert await dispatch.start_next(db) == (None, "plan_changed")
    assert not await _rows(db, "executor_orders")


async def test_owner_checkpoint_reuses_role_outbox(db, prepared, monkeypatch):
    owner_id, _, _, standard = prepared

    async def inflated(binding):
        return (await standard(binding)).model_copy(update={"ceiling_cents": 1600})

    monkeypatch.setattr(dispatch, "get_grant", inflated)
    assert await dispatch.start_next(db) == (None, "plan_budget_exhausted")
    await role_orders.run_sweep(db)
    host = await executor_orders.get_host(db, "k8")
    order = await role_orders.lease(db, "k8", host["principal_id"])
    assert order["task_id"] == 2377
    await role_orders.started(
        db, "k8", host["principal_id"], order["id"], order["lease_id"]
    )
    ack = await agent_outbox.publish(
        db,
        "k8",
        host["principal_id"],
        order["id"],
        OutboxPublish(
            project=epic_plan.PROJECT,
            role="roadmap",
            task_id=2377,
            generation=0,
            run=order["attempt"],
            lease_id=order["lease_id"],
            delivery_key="pilot-budget-question",
            continuation="resume-pilot",
            kind="question",
            body="Повторить проверку или остановиться? Ответьте retry либо stop.",
        ),
    )
    await agent_outbox_delivery.sweep(db)
    [question] = await _rows(db, "agent_outbox")
    assert question["id"] == ack.id and question["owner_id"] == owner_id
    message_id = question["message_id"]
    assert message_id in await agent_outbox.answerable_messages(db, 2377, owner_id)
    with pytest.raises(HTTPException) as refused:
        await agent_outbox.answer(
            db, 2377, message_id, owner_id + 1, "other-admin", "retry"
        )
    assert refused.value.status_code == 404
    result = await agent_outbox.answer(
        db, 2377, message_id, owner_id, "plan-owner", "retry"
    )
    assert (
        await agent_outbox.answer(db, 2377, message_id, owner_id, "plan-owner", "retry")
    ) == result
    await escalation.resume(db)
    await escalation.resume(db)
    assert (await repo.get_task(db, 2377))["status"] == "open"
    [case] = await _rows(db, "epic_plan_escalations")
    assert case["answer_event_id"] is not None
    assert not await _rows(db, "executor_orders")


async def test_authority_refuses_redirect_and_foreign_binding(monkeypatch):
    monkeypatch.setenv("HAIPLANE_EPIC_PLAN_COST_AUTHORITY_URL", AUTHORITY_URL)
    monkeypatch.setenv("HAIPLANE_EPIC_PLAN_COST_AUTHORITY_TOKEN", AUTHORITY_TOKEN)
    binding = authority.WorkBinding(
        phase="executor",
        project=epic_plan.PROJECT,
        plan_version="1" * 64,
        task_id=2377,
        generation=1,
        sha=BASE,
        host="k8",
        model="codex-fixture",
        principal_id=8,
        actor_principal_id=9,
        attempt=1,
    )
    client_class = httpx.AsyncClient
    requested = []

    async def redirect(request):
        requested.append(request.url)
        return httpx.Response(
            307, headers={"Location": "https://other.example.test/grant"}
        )

    def client(**options):
        assert options == {"follow_redirects": False, "trust_env": False}
        return client_class(transport=httpx.MockTransport(redirect), **options)

    monkeypatch.setattr(authority.httpx, "AsyncClient", client)
    assert await authority.get_grant(binding) is None
    assert requested == [httpx.URL(AUTHORITY_URL + "/grant")]

    foreign = authority.SpendingGrant(
        binding=binding.model_copy(update={"task_id": 2378}),
        reference="foreign-work-reference",
        ceiling_cents=100,
        expires_at=datetime.now(UTC) + timedelta(minutes=1),
        workspace_ready=True,
        hard_cap_enforced=True,
        idempotent_reference=True,
        source_ci_verified=True,
        independent_review_ready=True,
        owner_checkpoint_ready=True,
    )

    async def wrong_scope(route, payload):
        assert route == "grant" and payload == binding.model_dump()
        return foreign.model_dump_json().encode()

    monkeypatch.setattr(authority, "_call", wrong_scope)
    assert await authority.get_grant(binding) is None


async def test_agent_cannot_impersonate_pilot_policy(db, prepared):
    from hub.models import TaskReviewVerdict
    from hub.services import lifecycle
    from hub.services.auto_verdict import _POLICY_ACTOR

    _, _, reviewer_id, _ = prepared
    task = await _put_submission(db, 2377, 1, HEAD)
    body = TaskReviewVerdict(verdict="approved", agent=_POLICY_ACTOR)
    state = lifecycle.VerdictContext(
        db=db,
        task_id=2377,
        task=dict(task),
        body=body,
        principal_id=reviewer_id,
        agent_caller=True,
    )
    with pytest.raises(HTTPException) as rejected:
        await lifecycle._vstep_pilot_approval(state)
    assert rejected.value.status_code == 403
    assert rejected.value.detail["reason"] == "pilot_verdict_actor_not_authorized"
