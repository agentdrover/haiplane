"""Разбор k8 в тени (#1942, круг 2): followup по хронологии, split только человеком, восстановление.

Находка 1: исход не зависит от того, когда прошёл свип - берётся ПЕРВОЕ действие.
Находка 2: агентский draft-ребёнок не исход; split - только одобренный человеком.
Находка 3: пропущенная рекомендация восстанавливается и через 72 часа.
"""

from __future__ import annotations

from hub import repository as repo
from hub.services import executor_triage as triage
from tests.test_executor_triage import (
    CLASS1,
    CLASS3,
    _action_of,
    _fail,
    _followup,
    _recs,
    _relaunch,
    _sweep,
    _set_task,
    _task,
)
from tests import test_executor_orders as _eo
from tests.test_executor_orders import _rows

world = _eo.world
keys = _eo.keys
_clean_limiter = _eo._clean_limiter
_no_cloud = _eo._no_cloud


async def _at(world, table: str, row_id: int, column: str, minutes_ago: int) -> None:
    await world.db.execute(
        f"UPDATE {table} SET {column} = datetime('now', ?) WHERE id = ?",  # nosec B608
        (f"-{minutes_ago} minutes", row_id),
    )
    await world.db.commit()


async def _rec_at(world, oid: int, minutes_ago: int) -> None:
    await world.db.execute(
        "UPDATE executor_triage_recommendations SET created_at = datetime('now', ?) "
        "WHERE order_id = ?",
        (f"-{minutes_ago} minutes", oid),
    )
    await world.db.commit()


async def _submission(world, task_id: int, generation: int, minutes_ago: int) -> None:
    await world.db.execute(
        "INSERT INTO submissions (task_id, generation, sha, base_branch, submitted_at) "
        "VALUES (?, ?, '', 'develop', datetime('now', ?))",
        (task_id, generation, f"-{minutes_ago} minutes"),
    )
    await repo.update_task(
        world.db, task_id, status="review", submission_generation=generation
    )
    await world.db.commit()


async def _sequence(world, *, sweep_between: bool) -> tuple[int, int]:
    """retry_after_status -> новый заказ -> сдача; свип между действиями или только в конце."""
    task_id = await _task(world, f"хронология {sweep_between}")
    first = await _fail(world, task_id, CLASS1)
    await _rec_at(world, first, 60)
    second = await _relaunch(world, task_id)
    await _at(world, "executor_orders", second, "created_at", 40)
    if sweep_between:
        await _sweep(world)
    await _submission(world, task_id, 1, 20)
    await _sweep(world)
    return task_id, first


async def test_followup_is_the_first_action_not_the_state_at_sweep_time(world):
    """Находка 1: одна последовательность, свип в двух местах - один исход."""
    _t1, between = await _sequence(world, sweep_between=True)
    _t2, only_end = await _sequence(world, sweep_between=False)
    assert await _followup(world, between) == "new_order"
    assert await _followup(world, only_end) == "new_order"

    # retry_once + новый заказ + ПОЗЖЕ подзадача (одобренная) = new_order, совпало.
    t3 = await _task(world, "повтор и подзадача")
    o3 = await _fail(world, t3, CLASS3)
    await _rec_at(world, o3, 60)
    second = await _relaunch(world, t3)
    await _at(world, "executor_orders", second, "created_at", 40)
    child = await repo.create_task(
        world.db,
        title="часть",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status="open",
        auto_review=True,
        task_type="task",
        parent_id=t3,
        priority="medium",
    )
    await _at(world, "tasks", child, "created_at", 10)
    await _sweep(world)
    assert await _action_of(world, o3) == "retry_once"
    assert await _followup(world, o3) == "new_order"

    # retry_after_status + новый заказ после правки постановки = new_order.
    t4 = await _task(world, "повтор после правки")
    o4 = await _fail(world, t4, CLASS1)
    await _set_task(world, t4, statement_generation=1)
    await _sweep(world)
    assert await _followup(world, o4) is None  # правка без заказа - ещё не итог
    await _relaunch(world, t4)
    await _sweep(world)
    assert await _followup(world, o4) == "new_order"
    got = await triage.agreement(world.db)
    assert got["matched"] == got["total"] == 4, got


async def test_agent_draft_child_is_not_a_split(world):
    """Находка 2: split - только ребёнок, одобренный человеком."""
    task_id = await _task(world, "разделение")
    oid = await _fail(world, task_id, CLASS3)
    await _reopen_and_second_fail(world, task_id)
    assert (
        await _action_of(world, await _last_order(world, task_id)) == "rewrite_or_split"
    )
    draft = await repo.create_task(
        world.db,
        title="предложение",
        description="",
        runtime="auto",
        source="agent",
        assigned_agent="",
        rationale="",
        status="draft",
        auto_review=True,
        task_type="task",
        parent_id=task_id,
        priority="medium",
    )
    await world.db.commit()
    last = await _last_order(world, task_id)
    await _sweep(world)
    assert await _followup(world, last) is None
    await repo.update_task(world.db, draft, status="open")
    await world.db.commit()
    await _sweep(world)  # первое наблюдение одобрения
    await _sweep(world)  # секунда наблюдения закрыта
    assert await _followup(world, last) == "split"
    assert oid != last


async def _last_order(world, task_id: int) -> int:
    rows = await _rows(
        world.db,
        "SELECT MAX(id) AS id FROM executor_orders WHERE task_id = ?",
        (task_id,),
    )
    return rows[0]["id"]


async def _reopen_and_second_fail(world, task_id: int) -> None:
    await _set_task(world, task_id, status="open", claimed_by=None)
    await _fail(world, task_id, CLASS3)


async def test_missed_recommendation_is_recovered_after_72_hours(world):
    """Находка 3: пропуск старше 72 часов восстанавливается, пока задача не закрыта."""
    task_id = await _task(world, "давний пропуск")
    oid = await _fail(world, task_id, CLASS1)
    await world.db.execute("DELETE FROM executor_triage_recommendations")
    await world.db.execute(
        "UPDATE executor_orders SET closed_at = datetime('now', '-100 hours'), "
        "reported_at = datetime('now', '-100 hours') WHERE id = ?",
        (oid,),
    )
    await world.db.commit()
    await _sweep(world)
    assert await _action_of(world, oid) == "retry_after_status"
    assert len(await _recs(world, task_id)) == 1
    # Закрытая задача (сдана) рекомендации не получает и после восстановления.
    done = await _task(world, "сдана")
    did = await _fail(world, done, CLASS1)
    await world.db.execute(
        "DELETE FROM executor_triage_recommendations WHERE order_id = ?", (did,)
    )
    await world.db.commit()
    await _submission(world, done, 1, 5)
    await _sweep(world)
    assert await _action_of(world, did) is None


async def test_agent_draft_approved_late_does_not_precede_a_new_order(world):
    """Круг 2, Codex P2: одобрение draft считается с момента одобрения, не создания."""
    task_id = await _task(world, "позднее одобрение")
    oid = await _fail(world, task_id, CLASS3)  # retry_once
    await _rec_at(world, oid, 90)
    second = await _relaunch(world, task_id)
    await _at(world, "executor_orders", second, "created_at", 40)
    draft = await repo.create_task(
        world.db,
        title="предложение",
        description="",
        runtime="auto",
        source="agent",
        assigned_agent="",
        rationale="",
        status="draft",
        auto_review=True,
        task_type="task",
        parent_id=task_id,
        priority="medium",
    )
    await _at(world, "tasks", draft, "created_at", 80)
    await repo.update_task(world.db, draft, status="open")  # одобрено ПОСЛЕ заказа
    await world.db.commit()
    await _sweep(world)
    assert await _followup(world, oid) == "new_order"


def test_current_second_is_deferred_and_ties_follow_the_rank():
    """Круг 2, Codex P3: исход текущей секунды откладывается; ничья - по рангу, не по свипу."""
    now = "2026-10-21 10:00:05"
    only_split = {"events": [(now, "split")], "now": now}
    assert triage.classify_followup(only_split) is None  # ещё может прийти заказ
    both = {
        "events": [(now, "split"), (now, "new_order")],
        "now": "2026-10-21 10:00:06",
    }
    assert triage.classify_followup(both) == "new_order"
    earlier = {
        "events": [("2026-10-21 10:00:04", "split"), (now, "new_order")],
        "now": now,
    }
    assert triage.classify_followup(earlier) == "split"  # прошлая секунда закрыта
