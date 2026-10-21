"""Приём облачной сдачи - РОВНО в момент принятия, до bump (#1953, круг 4).

Критик проверяется и sha закрепляется там, где растёт submission_generation (и в
same-sha и в завершении без ревью), в той же транзакции и ДО bump. Отказ -
удержание без bump. Ниже по течению (доставка, завершение, dispatch_review)
критика нет: принятое поколение не пересматривается.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from hub import repository as repo
from hub.integrations.protocols import CIProbeOutcome
from hub.integrations.registry import plugins
from hub.services import executor_dispatch, orchestration
from tests import test_critic_orders as co
from tests.test_pair_merge_gate import _git

world = co.world
keys = co.keys
tip = co.tip
_clean_limiter = co._clean_limiter
_no_cloud = co._no_cloud


async def _row(world, task_id: int) -> dict:
    await world.db.commit()
    return dict(
        (await co._rows(world.db, "SELECT * FROM tasks WHERE id=?", (task_id,)))[0]
    )


async def test_a_refused_automatic_submission_leaves_the_generation_and_the_run_alone(
    world, tip
):
    """Отказ приёма: поколение не растёт, идущий прогон не снимается как «сдача легла»."""
    cloud = await co._cloud(world, tip)
    await world.db.execute(
        "UPDATE executor_runs SET submission_generation = 1 WHERE id = ?",
        (cloud.run_id,),
    )
    await world.db.commit()
    before = (await _row(world, cloud.task_id))["submission_generation"]
    row = await _row(world, cloud.task_id)
    outcome = await orchestration.transition_after_agent_done(
        world.db, row, has_done=True
    )
    await world.db.commit()
    after = await _row(world, cloud.task_id)
    assert outcome == "needs_decision" and after["status"] == "needs_decision"
    assert after["submission_generation"] == before, "bump без принятой сдачи"
    run = (
        await co._rows(
            world.db, "SELECT * FROM executor_runs WHERE id=?", (cloud.run_id,)
        )
    )[0]
    assert await executor_dispatch._submission_landed(world.db, run) is False


async def test_a_delivery_rebind_survives_the_completion(world, tip, monkeypatch):
    """Доставка законно перепривязала sha на B (слияние базы): завершение не откатывает на A."""
    git = _git(CIProbeOutcome.passed, merged=True)
    cloud = await co._cloud(world, tip, auto_review=False)
    await repo.update_task(
        world.db, cloud.task_id, submission_sha=co.SHA_B, pr_number=77
    )
    await world.db.commit()
    await co._closed_order(world, cloud, co.SHA_A, "passed")
    tip.sha = co.SHA_A

    async def _rebind(db, task):
        await db.execute(
            "UPDATE tasks SET submission_sha = ? WHERE id = ?", (co.SHA_B, task["id"])
        )
        await db.commit()
        return True, ""

    monkeypatch.setattr(orchestration, "merge_before_completion", _rebind)
    row = await _row(world, cloud.task_id)
    outcome = await orchestration.transition_after_agent_done(
        world.db, row, has_done=True
    )
    await world.db.commit()
    final = await _row(world, cloud.task_id)
    assert (outcome, final["status"]) == ("completed", "completed")
    assert final["submission_sha"] == co.SHA_B, (
        "завершение откатило перепривязку на старый sha"
    )
    assert git is not None


async def test_a_held_task_does_not_start_an_external_review(world, tip):
    """Удержанная приёмом задача не доходит до dispatch_review: платное ревью не запущено."""
    cloud = await co._cloud(world, tip)
    submit = AsyncMock(return_value={"job_id": "j-1"})
    plugins.dispatch.submit_task = submit  # type: ignore[method-assign]
    row = await _row(world, cloud.task_id)
    outcome = await orchestration.transition_after_agent_done(
        world.db, row, has_done=True
    )
    await world.db.commit()
    assert outcome == "needs_decision"
    assert submit.await_count == 0
    assert (await _row(world, cloud.task_id))["review_job_id"] in (None, "")


async def test_a_completion_never_writes_over_a_hold(world, tip):
    """Другой вход удержал задачу (needs_decision): отложенное завершение её не завершает."""
    cloud = await co._cloud(world, tip, critic_host="", auto_review=False)
    await repo.update_task(world.db, cloud.task_id, status="needs_decision")
    await world.db.commit()
    row = await _row(world, cloud.task_id)
    written = await orchestration._write_completed(world.db, row, None, None)
    await world.db.commit()
    assert written is None
    assert (await _row(world, cloud.task_id))["status"] == "needs_decision"
