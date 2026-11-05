"""Outbox dedup, ambiguity, ordering and lease fencing."""

import json
from unittest.mock import AsyncMock

import pytest

from hub import repository as tasks, repository_idempotency as repo
from hub.db import write_transaction
from hub.idempotency import utc_timestamp
from hub.models import TaskCreate
from hub.services import effects, lifecycle
from tests.idempotency_support import OP, PRINCIPAL, seed_effect, seed_key


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
    "result,state",
    [
        ({"job_id": "job-one"}, "done"),
        ({"error": "refused"}, "failed"),
        (TimeoutError("ambiguous"), "unknown"),
    ],
)
async def test_dispatch_outcomes_and_no_retries(db, monkeypatch, result, state):
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
    if state == "unknown":
        assert row["last_error"] == "dispatch_outcome_unknown"
    await effects.process_ready_effects(db)
    await effects.run_dispatch_effect(db, effect_id)
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
