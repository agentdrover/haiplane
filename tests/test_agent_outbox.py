import asyncio

import pytest

from hub import repository as repo
from hub.services import agent_outbox_delivery as worker
from hub.services import role_orders
from tests import outbox_support as fixtures
from tests.outbox_support import messages, answer, publish, status
from tests.role_orders_support import report, rows

world = fixtures.world
keys = fixtures.keys
outbox_world = fixtures.outbox_world


async def test_atomic_result_and_delivery_intent(outbox_world):
    world = outbox_world
    await world.db.execute("""
        CREATE TRIGGER outbox_interrupt BEFORE INSERT ON agent_outbox
        BEGIN SELECT RAISE(ABORT, 'simulated write failure'); END
    """)
    await world.db.commit()
    with pytest.raises(Exception, match="simulated write failure"):
        await publish(world)
    assert not await rows(world.db, "SELECT * FROM agent_outbox")
    assert not await messages(world)
    await world.db.execute("DROP TRIGGER outbox_interrupt")
    await world.db.commit()
    accepted = await publish(world)
    assert accepted.status_code == 200, accepted.text
    stored = await status(world, accepted.json()["id"])
    assert stored["state"] == "pending" and stored["message_id"] is None


async def test_concurrent_replay_returns_same_ack(outbox_world):
    world = outbox_world
    first, other = await asyncio.gather(publish(world), publish(world))
    assert first.status_code == other.status_code == 200
    assert first.json() == other.json()
    await worker.sweep(world.db)
    assert (await publish(world)).json() == first.json()
    assert len(await rows(world.db, "SELECT * FROM agent_outbox")) == 1
    assert len(await messages(world)) == 1


async def _sent_question(world):
    response = await publish(
        world,
        kind="question",
        continuation="after-owner",
        body="Can I collect more evidence?",
    )
    assert response.status_code == 200, response.text
    await worker.sweep(world.db)
    return await status(world, response.json()["id"])


async def test_delivery_and_replay_survive_closed_role_order(outbox_world):
    world = outbox_world
    first = await publish(world)
    assert first.json()["accepted_state"] == "pending"
    order = world.order
    closed = await report(world, "k8", order["id"], order["lease_id"])
    assert closed.status_code == 200, closed.text
    await worker.sweep(world.db)
    assert (await status(world, first.json()["id"]))["state"] == "sent"
    assert (await publish(world)).json() == first.json()


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("human_owner", "", "owner_changed"),
        ("archived", 1, "task_unavailable"),
        ("project_id", None, "task_project_changed"),
    ],
)
async def test_reinstating_task_does_not_revive_answer(
    outbox_world, field, value, reason
):
    world = outbox_world
    delivery = await _sent_question(world)
    assert (await answer(world, delivery["message_id"])).status_code == 200
    task = await repo.get_task(world.db, world.task)
    await repo.update_task(world.db, world.task, **{field: value})
    await repo.update_task(world.db, world.task, **{field: task[field]})
    await world.db.commit()
    got = await status(world, delivery["id"])
    assert got["continuation_state"] == "stale"
    assert got["continuation_reason"] == reason
    assert got["answer_message_id"] is None
    assert got["answer"] is None


async def test_answer_correlates_once_without_granting_gate(outbox_world):
    world = outbox_world
    delivery = await _sent_question(world)
    denied = await answer(world, delivery["message_id"], headers=world.agent_auth)
    assert denied.status_code == 403
    first, replay = await asyncio.gather(
        answer(world, delivery["message_id"]),
        answer(world, delivery["message_id"]),
    )
    assert first.status_code == replay.status_code == 200
    assert first.json() == replay.json()
    assert (
        await answer(world, delivery["message_id"], "Different answer")
    ).status_code == 409
    current = await status(world, delivery["id"])
    assert current["continuation_state"] == "answered"
    assert current["continuation"] == "after-owner"
    assert current["answer"] == "Continue diagnosis"
    assert current["answer_event_id"] > 0
    task = await repo.get_task(world.db, world.task)
    assert task["status"] == "needs_decision"
    sent = await messages(world)
    assert len(sent) == 2
    assert sent[0]["thread_id"] == sent[1]["thread_id"]
    wake = await rows(
        world.db,
        "SELECT * FROM events WHERE kind='message_posted' AND task_id=?",
        (world.task,),
    )
    assert sum('"to_kind": "role"' not in event["payload"] for event in wake) == 2


async def test_stale_answer_cannot_resume_wrong_generation(outbox_world):
    world = outbox_world
    delivery = await _sent_question(world)
    assert (await answer(world, delivery["message_id"])).status_code == 200
    await repo.update_task(
        world.db, world.task, submission_generation=world.scope["generation"] + 1
    )
    await world.db.commit()
    replay = await answer(world, delivery["message_id"])
    assert replay.status_code == 409
    current = await status(world, delivery["id"])
    assert current["continuation_state"] == "stale"
    assert current["continuation_reason"] == "generation_changed"
    assert current["answer"] is None


async def test_revoked_admission_does_not_revive_answer(outbox_world):
    world = outbox_world
    delivery = await _sent_question(world)
    assert (await answer(world, delivery["message_id"])).status_code == 200
    await role_orders.revoke_host(world.db, "k8", world.order["role"], world.pid)
    await role_orders.admit_host(world.db, "k8", world.order["role"], world.pid)
    got = await status(world, delivery["id"])
    assert got["continuation_state"] == "stale"
    assert got["continuation_reason"] == "admission_revoked"
    assert got["answer"] is None
    assert got["answer_event_id"] is None


async def test_silence_and_messaging_compatibility(outbox_world):
    world = outbox_world
    response = await publish(world, kind="silent", body="")
    assert response.status_code == 200, response.text
    await worker.sweep(world.db)
    delivery = await status(world, response.json()["id"])
    assert delivery["state"] == "sent" and delivery["reason"] == "silent"
    assert not await messages(world)
    legacy = await world.client.post(
        "/api/messages",
        json={"to_kind": "task", "to_ref": str(world.task), "body": "Legacy"},
        headers=world.human_auth,
    )
    assert legacy.status_code == 200, legacy.text
    assert legacy.json()["message"]["body"] == "Legacy"
