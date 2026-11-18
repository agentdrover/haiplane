import sqlite3

import pytest

from hub import config
from hub.services import agent_outbox as outbox
from hub.services import agent_outbox_delivery as worker
from tests import outbox_support as fixtures
from tests.outbox_support import messages, publish, status

world = fixtures.world
keys = fixtures.keys
outbox_world = fixtures.outbox_world


async def test_restart_and_lost_ack_do_not_duplicate(outbox_world, monkeypatch):
    world = outbox_world
    accepted = await publish(world)
    assert accepted.status_code == 200, accepted.text
    delivery_id = accepted.json()["id"]
    leased = await worker._lease(world.db, "first-test-worker")
    original = outbox.append_message

    async def fail_after_append(*args, **kwargs):
        await original(*args, **kwargs)
        raise sqlite3.OperationalError("interrupted before worker ACK")

    with monkeypatch.context() as context:
        context.setattr(outbox, "append_message", fail_after_append)
        with pytest.raises(sqlite3.OperationalError):
            await worker._deliver(world.db, leased)
    assert (await status(world, delivery_id))["state"] == "pending"
    assert not await messages(world)
    await worker.sweep(world.db)
    assert not await messages(world)
    await world.db.execute(
        "UPDATE agent_outbox SET lease_until=CAST(strftime('%s','now') AS INTEGER)-1"
    )
    await world.db.commit()
    await worker.sweep(world.db)
    await worker.sweep(world.db)
    assert (await status(world, delivery_id))["state"] == "sent"
    assert len(await messages(world)) == 1
    assert (await publish(world)).json() == accepted.json()


async def test_bounded_retry_preserves_status_and_orders(outbox_world, monkeypatch):
    world = outbox_world
    monkeypatch.setattr(outbox, "PENDING_LIMIT", 1)
    monkeypatch.setattr(config, "MESSAGE_RATE_PER_MINUTE", 0)
    monkeypatch.setattr(worker, "MAX_ATTEMPTS", 2)
    accepted = await publish(world)
    assert accepted.status_code == 200
    delivery = accepted.json()["id"]
    assert (await publish(world, delivery_key="op-2")).status_code == 429
    await worker.sweep(world.db)
    current = await status(world, delivery)
    assert current["state"] == "pending" and current["reason"] == "rate_limited"
    await worker.sweep(world.db)
    assert (await status(world, delivery))["state"] == "pending"
    await world.db.execute(
        "UPDATE agent_outbox SET next_attempt=CAST(strftime('%s','now') AS INTEGER)-1"
    )
    await world.db.commit()
    await worker.sweep(world.db)
    assert (await status(world, delivery))["state"] == "failed"

    query = await world.db.execute(
        "SELECT state FROM role_orders WHERE id=?", (world.order["id"],)
    )
    assert (await query.fetchone())["state"] == "started"
    accepted = await publish(
        world, delivery_key="op-2", kind="question", continuation="retry"
    )
    assert accepted.status_code == 200
    assert (await status(world, accepted.json()["id"]))[
        "continuation_state"
    ] == "queued"

    async def storage_failure(*args, **kwargs):
        raise sqlite3.OperationalError("write failed")

    monkeypatch.setattr(config, "MESSAGE_RATE_PER_MINUTE", 20)
    monkeypatch.setattr(worker, "MAX_ATTEMPTS", 1)
    monkeypatch.setattr(outbox, "append_message", storage_failure)
    await worker.sweep(world.db)
    got = await status(world, accepted.json()["id"])
    assert got["state"] == "unknown"
    assert got["continuation_state"] == "undeliverable"
    assert not await messages(world)
