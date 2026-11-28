"""The Hub repeats only the exact closed order with an included-quota grant."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from hub import brand, config, repository as repo
from hub.db import connect, fetchall
from hub.services import admin as admin_svc
from hub.services import executor_limited_retry as retry
from hub.services import executor_orders as orders
from hub.services import steward_limited_act as scope
from tests.executor_slot_support import seed_slot_skills
from tests.test_executor_orders import _make_key
from tests.test_slot_repair_running import FINDINGS_JSON
from tests.test_steward_applied import _client_task, _grant_act
from tests.test_steward_limited_act import _pilot
from tests.test_steward_shadow import _project

SHA = "b" * 40
MODEL = "gpt-6.2-sol"


class Transport:
    def __init__(self):
        self.calls = 0
        self.resume_after = None
        self.additional_spend_prevented = True
        self.ready = True
        self.before_issue = None

    async def preflight(self, intent):
        self.calls += 1
        return retry.IncludedQuota(
            intent,
            datetime.now(UTC) + timedelta(minutes=2),
            self.ready,
            self.additional_spend_prevented,
            self.resume_after,
        )

    async def issue(self, db, project, booking, grant):
        if self.before_issue:
            await self.before_issue()
        order_id, _ = await orders._issue_atomically(
            db,
            project,
            booking,
            issuer_principal_id=None,
            pilot_start=(grant.intent.starting_sha, grant.intent.model),
        )
        return order_id


@pytest.fixture(scope="module")
def public_key():
    return _make_key().public_pem


@pytest.fixture
async def candidate(db, monkeypatch, public_key):
    return await _launch_failed_source(db, monkeypatch, public_key)


async def _launch_failed_source(db, monkeypatch, public_key, *, returning=False):
    monkeypatch.setenv(brand.ENV_PREFIX + "HUB_URL", "https://hub.example.test")
    _grant_act(monkeypatch)
    await seed_slot_skills(db)
    project_id = await _project(db, "default")
    await repo.update_project(
        db,
        project_id,
        gate_policy='{"verdict":"steward","executor_launch":"manual"}',
        forge="github",
        repo="agentdrover/haiplane",
    )
    await db.commit()
    await orders.register_host(
        db, name="k8", public_key_pem=public_key, created_by=None
    )
    task_id = await _client_task(db, project_id, risk_class="R1")
    issuer_id = None
    if returning:
        issuer = await admin_svc.create_principal(
            db, kind="human", username="repair-owner", role_slug="operator"
        )
        issuer_id = issuer["id"]
        await repo.update_task(
            db,
            task_id,
            status="running",
            review_verdict="changes_requested",
            review_verdict_generation=1,
            review_findings=FINDINGS_JSON,
        )
        previous_run = await repo.create_executor_run(
            db,
            task_id=task_id,
            submission_generation=1,
            agent_id="previous",
            run_id="previous",
            model=MODEL,
        )
        await repo.update_executor_run(
            db, previous_run, tokens=1000, cents=10.0, outcome="finished", finish=True
        )
    else:
        await repo.update_task(db, task_id, status="open")
    pilot = await _pilot(db, monkeypatch, project_id, task_id)
    await db.commit()
    pilot.tasks[0].retry = scope.RetryTarget(starting_sha=SHA, model=MODEL)
    monkeypatch.setattr(config, "STEWARD_ACT_PILOT", pilot.model_dump_json())
    project = await repo.resolve_project_for_task(db, task_id)
    launched = await orders.launch_on_slot(
        db,
        project,
        host="k8",
        task_id=task_id,
        issuer_principal_id=issuer_id,
        issuer_human=returning,
    )
    assert launched.launched, launched.reason
    order_id, run_id = launched.order_id, launched.row_id
    assert order_id is not None and run_id is not None
    if not returning:
        original_run = await repo.get_executor_run(db, run_id)
        assert (original_run["starting_ref"], original_run["model"]) == (SHA, MODEL)
    delivered = await orders.deliver_orders(db, "k8")
    assert [order["id"] for order in delivered] == [order_id]
    if not returning:
        assert SHA in delivered[0]["order_text"] and MODEL in delivered[0]["order_text"]
    await orders.ack_order(db, "k8", order_id, orders.ACK_REDEEM_FAILED)
    transport = Transport()
    monkeypatch.setattr(retry, "retry_transport", lambda: transport)
    return task_id, order_id, run_id, transport


async def test_returning_repair_order_allows_one_retry(db, monkeypatch, public_key):
    task_id, source, source_run_id, _ = await _launch_failed_source(
        db, monkeypatch, public_key, returning=True
    )

    repeated = await retry.sweep(db)
    assert repeated is not None
    assert await retry.sweep(db) is None
    source_run = await repo.get_executor_run(db, source_run_id)
    assert (source_run["starting_ref"], source_run["model"]) == (SHA, MODEL)
    rows = await fetchall(
        db,
        "SELECT source_order_id, order_id FROM executor_limited_retry WHERE task_id=?",
        (task_id,),
    )
    assert [tuple(row) for row in rows] == [(source, repeated)]
    delivered = await orders.deliver_orders(db, "k8")
    assert [row["id"] for row in delivered] == [repeated]
    await orders.ack_order(db, "k8", repeated, orders.ACK_REDEEM_FAILED)
    assert await retry.sweep(db) is None


async def test_confirmed_prestart_failure_allows_only_one_retry(db, db_dsn, candidate):
    task_id, original, _, transport = candidate
    other = await connect(db_dsn)
    stop = asyncio.Event()
    real_preflight = transport.preflight

    async def same_intent(intent):
        proof = await real_preflight(intent)
        if transport.calls == 2:
            stop.set()
        await asyncio.wait_for(stop.wait(), timeout=3)
        return proof

    transport.preflight = same_intent
    try:
        results = await asyncio.gather(retry.sweep(db), retry.sweep(other))
    finally:
        await other.close()
    issued = [result for result in results if result is not None]
    assert len(issued) == 1
    assert await retry.sweep(db) is None
    rows = await fetchall(
        db,
        "SELECT source_order_id, order_id FROM executor_limited_retry WHERE task_id=?",
        (task_id,),
    )
    assert [tuple(row) for row in rows] == [(original, issued[0])]
    seen = await orders.deliver_orders(db, "k8")
    assert [row["id"] for row in seen] == issued
    assert [row["id"] for row in await orders.deliver_orders(db, "k8")] == issued
    await orders.ack_order(db, "k8", issued[0], orders.ACK_REDEEM_FAILED)
    restarted = await connect(db_dsn)
    try:
        assert await retry.sweep(restarted) is None
    finally:
        await restarted.close()
    events = await fetchall(
        db,
        "SELECT 1 FROM events WHERE task_id=? AND kind='steward_executor_retried'",
        (task_id,),
    )
    assert len(events) == 1


@pytest.mark.parametrize(
    "change",
    [
        "redeemed",
        "ambiguous_ack",
        "changed_task",
        "changed_sha",
        "unbound_source",
        "different_source_id",
        "unknown_cost",
        "offline",
    ],
)
async def test_does_not_launch_uncertain_or_stale_work(
    db, monkeypatch, candidate, change
):
    task_id, order_id, _, transport = candidate
    if change == "redeemed":
        await db.execute(
            "UPDATE executor_orders SET first_redeemed_at=datetime('now') WHERE id=?",
            (order_id,),
        )
    elif change == "ambiguous_ack":
        await db.execute(
            "UPDATE executor_orders SET ack_result='redeem_lost' WHERE id=?",
            (order_id,),
        )
    elif change == "changed_task":
        await repo.update_task(db, task_id, statement_generation=55)
    elif change == "changed_sha":
        await repo.update_task(db, task_id, submission_sha="c" * 40)
    elif change == "unbound_source":
        await db.execute(
            "DELETE FROM events WHERE kind=? AND task_id=?",
            (scope.RETRY_SOURCE_EVENT, task_id),
        )
    elif change == "different_source_id":
        pilot = scope.read_pilot()
        assert pilot is not None and pilot.tasks[0].retry is not None
        pilot.tasks[0].retry.source_order_id = order_id + 1
        monkeypatch.setattr(config, "STEWARD_ACT_PILOT", pilot.model_dump_json())
    elif change == "unknown_cost":
        transport.additional_spend_prevented = False
    elif change == "offline":
        transport.ready = False
    await db.commit()
    assert await retry.sweep(db) is None
    rows = await fetchall(
        db, "SELECT id FROM executor_orders WHERE task_id=?", (task_id,)
    )
    assert [row["id"] for row in rows] == [order_id]


async def test_quota_wait_persists_and_cannot_launch_early(db, db_dsn, candidate):
    task_id, source, _, transport = candidate
    transport.resume_after = datetime.now(UTC) + timedelta(minutes=30)
    assert await retry.sweep(db) is None
    transport.resume_after = None
    restarted = await connect(db_dsn)
    try:
        assert await retry.sweep(restarted) is None
        assert transport.calls == 1
        await restarted.execute(
            "UPDATE executor_limited_retry SET resume_after=datetime('now', '-1 day') "
            "WHERE source_order_id=?",
            (source,),
        )
        await restarted.commit()
        assert await retry.sweep(restarted) is not None
        assert await retry.sweep(restarted) is None
    finally:
        await restarted.close()
    rows = await fetchall(
        db, "SELECT id FROM executor_orders WHERE task_id=?", (task_id,)
    )
    assert len(rows) == 2


async def test_rollback_does_not_consume_retry(db, candidate):
    _, _, _, transport = candidate

    async def fail():
        raise RuntimeError("write unavailable")

    transport.before_issue = fail
    with pytest.raises(RuntimeError, match="write unavailable"):
        await retry.sweep(db)
    assert await fetchall(db, "SELECT 1 FROM executor_limited_retry") == []
    transport.before_issue = None
    assert await retry.sweep(db) is not None


async def test_lost_or_unavailable_proof_leaves_original_order_only(
    db, monkeypatch, candidate
):
    task_id, original, _, transport = candidate
    monkeypatch.setattr(retry, "retry_transport", lambda: None)
    assert await retry.sweep(db) is None
    assert transport.calls == 0
    rows = await fetchall(
        db, "SELECT id FROM executor_orders WHERE task_id=?", (task_id,)
    )
    assert [row["id"] for row in rows] == [original]


async def test_move_during_preflight_invalidates_the_proof(db, candidate):
    task_id, original, _, transport = candidate
    preflight = transport.preflight

    async def moved(intent):
        proof = await preflight(intent)
        await repo.update_task(db, task_id, statement_generation=44)
        await db.commit()
        return proof

    transport.preflight = moved
    assert await retry.sweep(db) is None
    rows = await fetchall(
        db, "SELECT id FROM executor_orders WHERE task_id=?", (task_id,)
    )
    assert [row["id"] for row in rows] == [original]
    assert await fetchall(db, "SELECT 1 FROM executor_limited_retry") == []


async def test_backend_mismatched_model_rolls_back_order_and_ledger(db, candidate):
    task_id, original, _, transport = candidate
    issue = transport.issue

    async def wrong_model(conn, project, booking, grant):
        new_id = await issue(conn, project, booking, grant)
        await conn.execute(
            "UPDATE executor_runs SET model='other' WHERE id="
            "(SELECT executor_run_id FROM executor_orders WHERE id=?)",
            (new_id,),
        )
        return new_id

    transport.issue = wrong_model
    with pytest.raises(RuntimeError, match="exact bounded order"):
        await retry.sweep(db)
    rows = await fetchall(
        db, "SELECT id FROM executor_orders WHERE task_id=?", (task_id,)
    )
    assert [row["id"] for row in rows] == [original]
    assert await fetchall(db, "SELECT 1 FROM executor_limited_retry") == []
