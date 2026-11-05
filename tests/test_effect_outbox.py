"""Outbox dedup, ambiguity, ordering and lease fencing."""

import json
from unittest.mock import AsyncMock

import pytest

from hub import config, repository as tasks, repository_idempotency as repo
from hub.db import write_transaction
from hub.idempotency import utc_timestamp
from hub.models import TaskCreate
from hub.services import effects, lifecycle
from tests.idempotency_support import OP, PRINCIPAL, seed_effect, seed_key


@pytest.fixture(autouse=True)
def _dispatch_switches(monkeypatch):
    """Outbox tests exercise the sender; both switches are on by default."""
    monkeypatch.setattr(config, "IDEMPOTENCY_ENABLED", True)
    monkeypatch.setattr(config, "IDEMPOTENCY_DISPATCH_ENABLED", True)


async def test_enqueue_dedup_preserves_first_payload(db):
    first = await seed_effect(db)
    assert await seed_effect(db) == first
    async with write_transaction(db):
        with pytest.raises(effects.EffectPayloadConflict):
            await effects.enqueue_effect(
                db,
                operation_id=OP,
                principal_ref=PRINCIPAL,
                effect_kind="task.dispatch",
                effect_key="dispatch-2241",
                payload={"task_id": 2},
            )
    rows = await repo.list_effects(db, PRINCIPAL, OP)
    assert len(rows) == 1
    assert json.loads(rows[0]["payload_json"]) == {"task_id": 1}


@pytest.mark.parametrize(
    "result,state,last_error",
    [
        ({"job_id": "job-one"}, "done", None),
        ({"error": "refused", "delivery": "not_sent"}, "failed", "dispatch_not_sent"),
        ({"error": "refused", "delivery": "permanent"}, "failed", "dispatch_refused"),
        (
            {"error": "refused", "delivery": "ambiguous"},
            "unknown",
            "dispatch_outcome_unknown",
        ),
        # No delivery proof: an error result is NOT evidence that no job ran.
        ({"error": "refused"}, "unknown", "dispatch_outcome_unknown"),
        (TimeoutError("ambiguous"), "unknown", "dispatch_outcome_unknown"),
    ],
)
async def test_dispatch_outcomes_and_no_retries(
    db, monkeypatch, result, state, last_error
):
    task_id = await tasks.create_task_full(
        db, TaskCreate(title="effect"), status="running"
    )
    await db.commit()
    assert task_id == 1
    effect_id = await seed_effect(db)
    dispatch = (
        AsyncMock(side_effect=result)
        if isinstance(result, Exception)
        else AsyncMock(return_value=result)
    )
    monkeypatch.setattr(lifecycle, "dispatch_task", dispatch)
    await effects.run_dispatch_effect(db, effect_id)
    row = await repo.get_effect(db, effect_id)
    assert row["state"] == state and row["attempts"] == 1
    assert row["lease_owner"] and row["lease_until"] is None
    if state == "done":
        assert row["resolution"] == "job_id=job-one"
    else:
        assert row["last_error"] == last_error
    await effects.process_ready_effects(db)
    await effects.run_dispatch_effect(db, effect_id)
    assert dispatch.await_count == 1


async def test_local_refusal_is_permanent_without_ambiguity(db, monkeypatch):
    """A refusal raised before submit proves nothing was sent (major 6)."""
    from fastapi import HTTPException

    await tasks.create_task_full(db, TaskCreate(title="refused"), status="running")
    await db.commit()
    effect_id = await seed_effect(db)
    monkeypatch.setattr(
        lifecycle, "dispatch_task", AsyncMock(side_effect=HTTPException(422, "nope"))
    )
    await effects.run_dispatch_effect(db, effect_id)
    row = await repo.get_effect(db, effect_id)
    assert row["state"] == "failed" and row["last_error"] == "dispatch_refused"


async def test_unexpected_exception_stays_unknown(db, monkeypatch):
    await tasks.create_task_full(db, TaskCreate(title="boom"), status="running")
    await db.commit()
    effect_id = await seed_effect(db)
    monkeypatch.setattr(
        lifecycle, "dispatch_task", AsyncMock(side_effect=RuntimeError("secret"))
    )
    await effects.run_dispatch_effect(db, effect_id)
    row = await repo.get_effect(db, effect_id)
    assert row["state"] == "unknown"
    assert row["last_error"] == "dispatch_outcome_unknown"


@pytest.mark.parametrize(
    "scenario,expected_state,expected_error",
    [
        ("completed", "dead", "stale_intent:task_completed"),
        ("archived", "dead", "stale_intent:task_archived"),
        ("open", "dead", "stale_intent:task_open"),
        ("missing", "dead", "stale_intent:task_missing"),
        ("has_job", "done", None),
    ],
)
async def test_stale_intent_is_finalized_without_dispatch(
    db, monkeypatch, scenario, expected_state, expected_error
):
    """The outbox fired completed/archived/job-bearing tasks (major 5)."""
    if scenario != "missing":
        task_id = await tasks.create_task_full(
            db, TaskCreate(title="stale"), status="running"
        )
        assert task_id == 1
        if scenario == "completed":
            await tasks.update_task(db, task_id, status="completed")
        elif scenario == "archived":
            await tasks.update_task(db, task_id, archived=1)
        elif scenario == "open":
            await tasks.update_task(db, task_id, status="open")
        elif scenario == "has_job":
            await tasks.update_task(db, task_id, job_id="existing-job")
        await db.commit()
    effect_id = await seed_effect(db)
    dispatch = AsyncMock(return_value={"job_id": "would-be-second"})
    monkeypatch.setattr(lifecycle, "dispatch_task", dispatch)
    await effects.run_dispatch_effect(db, effect_id)
    row = await repo.get_effect(db, effect_id)
    assert row["state"] == expected_state
    if expected_error:
        assert row["last_error"] == expected_error
    else:
        assert row["resolution"] == "job_id=existing-job"
    dispatch.assert_not_awaited()


@pytest.mark.parametrize("switch", ["idempotency", "dispatch"])
async def test_sender_switches_freeze_pending_effects(db, monkeypatch, switch):
    """Neither the poller nor a direct call runs effects with its switch off."""
    await tasks.create_task_full(db, TaskCreate(title="frozen"), status="running")
    await db.commit()
    effect_id = await seed_effect(db)
    dispatch = AsyncMock(return_value={"job_id": "after-thaw"})
    monkeypatch.setattr(lifecycle, "dispatch_task", dispatch)
    if switch == "idempotency":
        monkeypatch.setattr(config, "IDEMPOTENCY_ENABLED", False)
    else:
        monkeypatch.setattr(config, "IDEMPOTENCY_DISPATCH_ENABLED", False)
    await effects.process_ready_effects(db)
    await effects.run_dispatch_effect(db, effect_id)
    row = await repo.get_effect(db, effect_id)
    assert row["state"] == "pending" and row["attempts"] == 0
    dispatch.assert_not_awaited()
    monkeypatch.setattr(config, "IDEMPOTENCY_ENABLED", True)
    monkeypatch.setattr(config, "IDEMPOTENCY_DISPATCH_ENABLED", True)
    await effects.run_dispatch_effect(db, effect_id)
    row = await repo.get_effect(db, effect_id)
    assert row["state"] == "done" and row["resolution"] == "job_id=after-thaw"
    assert dispatch.await_count == 1


async def test_external_dispatch_runs_outside_transactions(db, monkeypatch):
    await tasks.create_task_full(db, TaskCreate(title="outside"), status="running")
    await db.commit()
    effect_id = await seed_effect(db)

    async def dispatch(conn, task_id, task):
        assert not conn.in_transaction
        return {"job_id": "outside"}

    monkeypatch.setattr(lifecycle, "dispatch_task", dispatch)
    await effects.run_dispatch_effect(db, effect_id)
    assert (await repo.get_effect(db, effect_id))["state"] == "done"


@pytest.mark.parametrize("initial", ["running", "pending"])
async def test_crashed_dispatch_is_unknown_without_second_external_call(
    db, monkeypatch, initial
):
    effect_id = await seed_effect(db)
    await db.execute(
        "UPDATE effect_outbox SET state=?,attempts=1,lease_owner='dead-worker',lease_until='2000-01-01 00:00:00'",
        (initial,),
    )
    await db.commit()
    dispatch = AsyncMock()
    monkeypatch.setattr(lifecycle, "dispatch_task", dispatch)
    await effects.process_ready_effects(db)
    assert (await repo.get_effect(db, effect_id))["state"] == "unknown"
    dispatch.assert_not_awaited()


async def test_live_lease_is_not_stolen_and_stale_owner_is_fenced(db):
    effect_id = await seed_effect(db)
    async with write_transaction(db):
        assert await repo.mark_running(db, effect_id, "first-owner", utc_timestamp(60))
    await effects.process_ready_effects(db)
    assert (await repo.get_effect(db, effect_id))["state"] == "running"
    async with write_transaction(db):
        assert not await repo.finalize_effect(db, effect_id, "stale-owner", "done")
        assert await repo.finalize_effect(db, effect_id, "first-owner", "unknown")
        assert not await repo.finalize_effect(db, effect_id, "first-owner", "done")


@pytest.mark.parametrize("state", ["done", "failed", "dead"])
async def test_manual_resolution_audits_without_changing_saved_response(db, state):
    await seed_key(db)
    effect_id = await seed_effect(db)
    await db.execute("UPDATE effect_outbox SET state='unknown'")
    await db.commit()
    before = dict(await repo.get_key(db, PRINCIPAL, OP))
    await effects.resolve_effect(
        db, effect_id, state, "manual evidence #2241", "operator"
    )
    row = await repo.get_effect(db, effect_id)
    assert row["state"] == state and row["resolution"] == "manual evidence #2241"
    assert row["resolved_by"] == "operator" and row["resolved_at"]
    assert dict(await repo.get_key(db, PRINCIPAL, OP)) == before


@pytest.mark.parametrize("blocker", ["pending", "running", "unknown", "failed", "dead"])
async def test_fifo_blocks_later_effect_with_same_second_timestamp(db, blocker):
    first = await seed_effect(db, "first", ordering_key="task:1")
    second = await seed_effect(db, "second", ordering_key="task:1")
    await db.execute(
        "UPDATE effect_outbox SET state=?,created_at='2026-01-01 00:00:00' WHERE effect_id=?",
        (blocker, first),
    )
    await db.execute(
        "UPDATE effect_outbox SET created_at='2026-01-01 00:00:00' WHERE effect_id=?",
        (second,),
    )
    await db.commit()
    ready = await repo.pick_ready_effects(db)
    assert second not in [r["effect_id"] for r in ready]
    async with write_transaction(db):
        assert not await repo.mark_running(db, second, "owner", utc_timestamp(60))
    if blocker == "unknown":
        await effects.resolve_effect(db, first, "done", "found job", "human")
        assert second in [r["effect_id"] for r in await repo.pick_ready_effects(db)]
