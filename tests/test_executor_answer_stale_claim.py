"""Answer without resume releases only the finished executor's claim (#2395)."""

import pytest

from hub import repository as repo
from hub.config import CHAT_PAIR_AGENT
from hub.services import executor_dispatch
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


async def _question_with_stale_claim(world):
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
    await world.db.execute(
        "UPDATE chat_pair_sessions SET revoked_at = datetime('now') WHERE executor_run_id=?",
        (run_id,),
    )
    await world.db.execute(
        "UPDATE executor_runs SET outcome='aborted' WHERE id=?", (run_id,)
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
