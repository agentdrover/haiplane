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
    """Удержанная приёмом задача: ни bump, ни пина, ни внешнего ревью; со `passed` - идёт дальше."""
    cloud = await co._cloud(world, tip)
    await repo.update_task(world.db, cloud.task_id, submission_sha=co.SHA_B)
    await world.db.commit()
    submit = AsyncMock(return_value={"job_id": "j-1"})
    plugins.dispatch.submit_task = submit  # type: ignore[method-assign]
    row = await _row(world, cloud.task_id)
    outcome = await orchestration.transition_after_agent_done(
        world.db, row, has_done=True
    )
    await world.db.commit()
    held = await _row(world, cloud.task_id)
    assert outcome == "needs_decision" and submit.await_count == 0
    assert (held["review_job_id"] or "") == ""
    assert (held["submission_generation"], held["submission_sha"]) == (0, co.SHA_B)

    # Контроль: со `passed` тот же вход принимает сдачу - bump и пин приёмом.
    await co._closed_order(world, cloud, co.SHA_A, "passed")
    await repo.update_task(world.db, cloud.task_id, status="running")
    await world.db.commit()
    row = await _row(world, cloud.task_id)
    await orchestration.transition_after_agent_done(world.db, row, has_done=True)
    await world.db.commit()
    accepted = await _row(world, cloud.task_id)
    assert (accepted["submission_generation"], accepted["submission_sha"]) == (
        1,
        co.SHA_A,
    )


async def test_a_new_submission_is_never_bumped_without_the_check(world, tip):
    """Re-entry из ci_check/needs_decision: bump без проверки критика не бывает."""
    for status in ("ci_check", "needs_decision"):
        cloud = await co._cloud(world, tip)
        await repo.update_task(
            world.db, cloud.task_id, status=status, submission_sha=co.SHA_B
        )
        await world.db.commit()
        row = await _row(world, cloud.task_id)
        outcome = await orchestration.transition_after_agent_done(
            world.db, row, has_done=True
        )
        await world.db.commit()
        after = await _row(world, cloud.task_id)
        assert after["submission_generation"] == 0, status
        assert (outcome, after["submission_sha"]) == ("needs_decision", co.SHA_B), (
            status
        )


async def test_a_local_submission_error_after_the_bump_rolls_the_bump_back(
    world, monkeypatch
):
    """Не-облачная автоматическая сдача: ошибка гейтов после приёма откатывает и bump."""
    task_id = await co._task(world, "локальная")
    await repo.update_task(world.db, task_id, status="running", auto_review=1)
    await world.db.commit()

    async def _boom(db, tid, task):
        raise RuntimeError("гейт упал")

    monkeypatch.setattr(orchestration, "_run_headless_submit_gates", _boom)
    row = await _row(world, task_id)
    try:
        await orchestration.transition_after_agent_done(world.db, row, has_done=True)
    except RuntimeError:
        await world.db.rollback()
    assert (await _row(world, task_id))["submission_generation"] == 0


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


async def test_a_refusal_inside_the_acceptance_leaves_no_open_transaction(world, tip):
    """422 из приёма вне чужой транзакции: свою транзакцию он откатывает, лок не висит."""
    import pytest
    from fastapi import HTTPException

    from hub.services import critic_orders

    cloud = await co._cloud(world, tip)
    row = await _row(world, cloud.task_id)
    row["_ci_prepared_sha"] = co.SHA_A
    assert not world.db.in_transaction
    with pytest.raises(HTTPException):
        await critic_orders.accept_in_transaction(world.db, row, bump=True)
    assert not world.db.in_transaction
    assert (await _row(world, cloud.task_id))["submission_generation"] == 0


async def test_the_critic_orders_sweep_is_idempotent(world, tip):
    """Свип critic_orders: просроченный заказ закрыт исходом expired один раз, повтор тих."""
    from hub.poller import SWEEPS

    sweep = next(s for s in SWEEPS if s.name == "critic_orders")
    cloud = await co._cloud(world, tip)
    assert (await co._say(world, cloud, co._line(co.SHA_A))).status_code == 200
    await world.db.execute(
        "UPDATE critic_orders SET expires_at = datetime('now', '-1 minute')"
    )
    await world.db.commit()
    for _ in range(3):
        await sweep.run(world.db)
        await world.db.commit()
    (order,) = await co._orders(world, cloud.task_id)
    assert (order["state"], order["outcome"], order["closed_reason"]) == (
        "closed",
        "expired",
        "ttl",
    )
    alerts = [a for a in await co._alerts(world, cloud.task_id) if "истёк" in a]
    assert len(alerts) == 1
