"""Answer without resume releases only the finished executor's claim (#2395)."""

import asyncio

import pytest

from hub import repository as repo
from hub.config import CHAT_PAIR_AGENT
from hub.poller import SWEEPS
from hub.services import executor_dispatch, run_claim
from tests.test_executor_orders import (  # noqa: F401
    _claim_and_start,
    _clean_limiter,
    _code_of,
    _launch,
    _launch_redeem,
    _no_cloud,
    _order_row,
    _orders,
    _other_connection,
    _redeem,
    _rows,
    _task,
    keys,
)
from tests.test_executor_orders import world as slot_world  # noqa: F401


async def _answered(world, task_id):
    result = await world.client.post(
        f"/api/tasks/{task_id}/answer",
        json={"answer": "Вернуть задачу в очередь", "resume": False},
        headers=world.human_auth,
    )
    assert result.status_code == 200, result.text
    return result.json()


async def _question_with_stale_claim(world, *, outcome="aborted", live_session=False):
    task_id = await _task(world)
    previous = await _launch_redeem(world, task_id)
    await _claim_and_start(world, task_id, previous["session"])
    held = dict(await repo.get_task(world.db, task_id))
    run_id = held["executor_run_id"]
    assert run_id is not None
    assert held["claim_session_id"]
    await repo.add_task_update(
        world.db, task_id, CHAT_PAIR_AGENT, "question", "Продолжать?"
    )
    if not live_session:
        await world.db.execute(
            "UPDATE chat_pair_sessions SET revoked_at = datetime('now') "
            "WHERE executor_run_id=?",
            (run_id,),
        )
    await world.db.execute(
        "UPDATE executor_runs SET outcome=? WHERE id=?", (outcome, run_id)
    )
    await world.db.execute(
        "UPDATE executor_orders SET state='closed', close_reason='aborted' WHERE id=?",
        (previous["order"]["id"],),
    )
    # Observed persisted state: the run and token are closed, but the
    # question path left its binding and session ID on the task.
    await repo.update_task(
        world.db,
        task_id,
        status="needs_info",
        claimed_by=None,
        claim_session_id=held["claim_session_id"],
    )
    await repo.update_task(world.db, task_id, executor_run_id=run_id)
    await world.db.commit()
    assert (await repo.get_task(world.db, task_id))["executor_run_id"] == run_id
    return task_id, run_id


async def test_answer_without_resume_releases_finished_run_claim(slot_world):  # noqa: F811
    world = slot_world
    task_id, old_run = await _question_with_stale_claim(world)
    assert (await _answered(world, task_id))["status"] == "open"
    released = dict(await repo.get_task(world.db, task_id))

    launched = await _launch(world, task_id)
    assert launched.status_code == 200, launched.text
    order = (await _orders(world)).json()["orders"][0]
    assert (await _order_row(world, order["id"]))["executor_run_id"] != old_run
    redeemed = await _redeem(world, _code_of(world, order))
    assert redeemed.status_code == 200, redeemed.text
    assert all(
        released[field] is None
        for field in ("claimed_by", "claim_session_id", "claimed_at", "executor_run_id")
    )
    await _claim_and_start(
        world, task_id, {"Authorization": f"Bearer {redeemed.json()['token']}"}
    )
    assert (await repo.get_task(world.db, task_id))["status"] == "running"


@pytest.mark.parametrize("competing", ["claimed", "reserved"])
async def test_answer_does_not_clear_live_competing_claim(
    slot_world,  # noqa: F811
    monkeypatch,
    competing,
):
    world = slot_world
    task_id, _ = await _question_with_stale_claim(world)
    competing_order = {}

    async def compete(db, seen, *, resume):
        assert seen["claim_session_id"] == f"slot-{task_id}"
        # First answer recorded. Another request reopens the task and
        # a real executor order wins on its own DB connection.
        other = await _other_connection(world)
        try:
            await repo.update_task(
                other,
                task_id,
                status="open",
                claim_session_id=None,
                claimed_at=None,
                executor_run_id=None,
            )
            await other.commit()
        finally:
            await other.close()
        launched = await _launch(world, task_id)
        assert launched.status_code == 200, launched.text
        order = (await _orders(world)).json()["orders"][0]
        competing_order.update(order)
        if competing == "claimed":
            redeemed = await _redeem(world, _code_of(world, order))
            assert redeemed.status_code == 200, redeemed.text
            await _claim_and_start(
                world, task_id, {"Authorization": f"Bearer {redeemed.json()['token']}"}
            )
        return False

    monkeypatch.setattr(executor_dispatch, "answer_for_executor", compete)
    expected_status = "running" if competing == "claimed" else "open"
    assert (await _answered(world, task_id))["status"] == expected_status
    live = await _order_row(world, competing_order["id"])
    if competing == "reserved":
        assert live["state"] == "delivered"
        redeemed = await _redeem(world, _code_of(world, competing_order))
        assert redeemed.status_code == 200, redeemed.text
        await _claim_and_start(
            world, task_id, {"Authorization": f"Bearer {redeemed.json()['token']}"}
        )
    winner = dict(await repo.get_task(world.db, task_id))
    assert winner["status"] == "running"
    assert winner["executor_run_id"] == live["executor_run_id"]
    assert winner["claim_session_id"] == f"slot-{task_id}"
    assert await _rows(
        world.db,
        "SELECT executor_run_id FROM chat_pair_sessions WHERE bound_task_id=? "
        "AND kind='implementer' AND revoked_at IS NULL",
        (task_id,),
    ) == [{"executor_run_id": live["executor_run_id"]}]


async def test_delayed_answer_does_not_reopen_a_new_question(slot_world):  # noqa: F811
    world = slot_world
    task_id = await _task(world)
    original = executor_dispatch.answer_for_executor

    async def ask(question):
        response = await world.client.post(
            f"/api/tasks/{task_id}/question",
            json={"question": question, "agent": "bot"},
            headers=world.agent_auth,
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "needs_info"

    await ask("Первый вопрос")

    async def answer_first_then_ask_again(db, seen, *, resume):
        monkeypatch.undo()
        assert (await _answered(world, task_id))["status"] == "open"
        await ask("Второй вопрос")
        return await original(db, seen, resume=resume)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(
            executor_dispatch, "answer_for_executor", answer_first_then_ask_again
        )
        assert (await _answered(world, task_id))["status"] == "needs_info"

    assert (await repo.get_task(world.db, task_id))["status"] == "needs_info"
    assert (await _answered(world, task_id))["status"] == "open"


async def _expire_session(db, run_id):
    await db.execute(
        "UPDATE chat_pair_sessions SET expires_at = datetime('now', '-1 second') "
        "WHERE executor_run_id=?",
        (run_id,),
    )


async def _poll_orders(db):
    sweep = next(sweep for sweep in SWEEPS if sweep.name == "executor_orders")
    await sweep.run(db)


@pytest.mark.parametrize("blocker", ["running", "live_session"])
async def test_recorded_answer_reopens_after_live_blocker_ends(
    slot_world,  # noqa: F811
    blocker,
):
    world = slot_world
    task_id, run_id = await _question_with_stale_claim(
        world,
        outcome="running" if blocker == "running" else "aborted",
        live_session=blocker == "live_session",
    )
    assert (await _answered(world, task_id))["status"] == "needs_info"
    await _poll_orders(world.db)
    waiting = dict(await repo.get_task(world.db, task_id))
    assert waiting["status"] == "needs_info"
    assert waiting["executor_run_id"] == run_id
    assert waiting["claim_session_id"] == f"slot-{task_id}"
    await _assert_pending(world, task_id, 1)

    worker = await _other_connection(world)
    try:
        await worker.execute(
            "UPDATE executor_runs SET outcome='aborted' WHERE id=?", (run_id,)
        )
        await _expire_session(worker, run_id)
        await worker.commit()
        await asyncio.gather(_poll_orders(world.db), _poll_orders(worker))
    finally:
        await worker.close()

    assert (await repo.get_task(world.db, task_id))["status"] == "open"
    assert (
        len(
            [
                u
                for u in await repo.get_task_updates(world.db, task_id)
                if u["kind"] == "answer"
            ]
        )
        == 1
    )
    released = await _rows(
        world.db,
        "SELECT id FROM events WHERE kind=? AND task_id=?",
        (run_claim.EVENT_CLAIM_RELEASED, task_id),
    )
    assert len(released) == 1
    await _assert_pending(world, task_id, 0)
    next_run = await _launch_redeem(world, task_id)
    await _claim_and_start(world, task_id, next_run["session"])
    await _poll_orders(world.db)
    assert (await repo.get_task(world.db, task_id))["status"] == "running"


async def test_new_question_supersedes_deferred_answer(slot_world):  # noqa: F811
    world = slot_world
    task_id, run_id = await _question_with_stale_claim(world, live_session=True)
    assert (await _answered(world, task_id))["status"] == "needs_info"

    # A later authorized transition opens the task. A new question returns it
    # to needs_info with the same claim while the old intent is still pending.
    await repo.update_task(world.db, task_id, status="open")
    await world.db.commit()
    asked = await world.client.post(
        f"/api/tasks/{task_id}/question",
        json={"question": "Новый вопрос", "agent": "bot"},
        headers=world.agent_auth,
    )
    assert asked.status_code == 200, asked.text
    await _expire_session(world.db, run_id)
    await world.db.commit()

    await _poll_orders(world.db)
    current = dict(await repo.get_task(world.db, task_id))
    assert current["status"] == "needs_info"
    assert current["executor_run_id"] == run_id
    await _assert_pending(world, task_id, 0)
    assert (await _answered(world, task_id))["status"] == "open"


async def test_new_holder_supersedes_deferred_answer(slot_world):  # noqa: F811
    world = slot_world
    task_id, run_id = await _question_with_stale_claim(world, live_session=True)
    assert (await _answered(world, task_id))["status"] == "needs_info"

    newer = await _other_connection(world)
    try:
        await _expire_session(newer, run_id)
        await repo.update_task(
            newer,
            task_id,
            status="open",
            claim_session_id=None,
            claimed_at=None,
            executor_run_id=None,
        )
        await newer.commit()
        next_run = await _launch_redeem(world, task_id)
        await _claim_and_start(world, task_id, next_run["session"])
    finally:
        await newer.close()
    await _poll_orders(world.db)

    order = await _order_row(world, next_run["order"]["id"])
    current = dict(await repo.get_task(world.db, task_id))
    assert current["status"] == "running"
    assert current["executor_run_id"] == order["executor_run_id"] != run_id
    assert current["claimed_by"]
    await _assert_pending(world, task_id, 0)


async def _assert_pending(world, task_id, count):
    assert (
        len(
            await _rows(
                world.db,
                "SELECT answer_update_id FROM executor_answer_reopens WHERE task_id=?",
                (task_id,),
            )
        )
        == count
    )


async def test_agent_feed_note_does_not_supersede_deferred_owner_answer(
    slot_world,  # noqa: F811
):
    world = slot_world
    task_id, run_id = await _question_with_stale_claim(world, live_session=True)
    assert (await _answered(world, task_id))["status"] == "needs_info"

    note = await world.client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": "hub", "kind": "answer", "content": "Откройте позже"},
        headers=world.agent_auth,
    )
    assert note.status_code == 200, note.text
    assert note.json()["author_kind"] != "hub"
    await _expire_session(world.db, run_id)
    await world.db.commit()
    await _poll_orders(world.db)

    assert (await repo.get_task(world.db, task_id))["status"] == "open"
    await _assert_pending(world, task_id, 0)
