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
    _report,
    _sweep,
    _set_task,
    _task,
)
from tests import test_executor_orders as _eo
from tests.test_executor_orders import _launch_redeem, _rows

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
    """Находка 2: split - только подзадача, созданная человеком; агентский - никогда."""
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
    await _sweep(world)  # вторая секунда: засчитанный draft успел бы разрешиться
    assert await _followup(world, last) is None
    await repo.update_task(
        world.db, draft, status="open"
    )  # одобрен: всё равно не split
    await world.db.commit()
    await _sweep(world)
    await _sweep(world)
    assert await _followup(world, last) is None
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
        "INSERT OR REPLACE INTO executor_triage_state (id, activated_at) "
        "VALUES (1, datetime('now', '-500 hours'))"
    )
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


# ---------------------------------------------------------------------------
# Круг 3: только долговечные метки в замере; unclear; граница включения
# ---------------------------------------------------------------------------


async def test_sweep_placement_does_not_change_the_followup_or_agreement(world):
    """Одна последовательность (архив, снятие архива, новый заказ), свипы в разных местах."""

    async def run(sweep_between: bool) -> int:
        task_id = await _task(world, f"архив и заказ {sweep_between}")
        first = await _fail(world, task_id, CLASS1)
        await _rec_at(world, first, 60)
        await _set_task(world, task_id, archived=1)
        if sweep_between:
            await _sweep(world)
            await _sweep(world)
        await _set_task(world, task_id, archived=0)
        second = await _relaunch(world, task_id)
        await _at(world, "executor_orders", second, "created_at", 40)
        await _sweep(world)
        await _sweep(world)
        return first

    between, at_end = await run(True), await run(False)
    assert await _followup(world, between) == "new_order"
    assert await _followup(world, at_end) == "new_order"
    got = await triage.agreement(world.db)
    assert (got["total"], got["matched"]) == (2, 2), got


async def test_rejected_agent_draft_is_unclear_and_outside_the_denominator(world):
    """Агентский ребёнок в любом статусе (в том числе отклонённый) - не split."""
    task_id = await _task(world, "отклонённый draft")
    await _fail(world, task_id, CLASS3)
    await _set_task(world, task_id, status="open", claimed_by=None)
    oid = await _fail(world, task_id, CLASS3)
    assert await _action_of(world, oid) == "rewrite_or_split"
    child = await repo.create_task(
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
    await repo.update_task(world.db, child, status="rejected")
    await world.db.commit()
    await _sweep(world)
    await _sweep(world)
    assert (
        await _followup(world, oid) is None
    )  # исход без долговечной метки не фиксируется
    await _rec_at(world, oid, 73 * 60)
    await _sweep(world)
    await _sweep(world)
    assert await _followup(world, oid) == "unclear"
    got = await triage.agreement(world.db)
    assert got["unclear"] == 1 and got["total"] == 1, (
        got
    )  # total - первая рекомендация (заказ)


async def test_activation_boundary_skips_history_and_recovers_after_it(world):
    """Заказ, закрытый до активации, рекомендации не получает; пропуск после неё - бессрочно."""
    old_task = await _task(world, "давняя неудача")
    old = await _fail(world, old_task, CLASS1)
    await world.db.execute("DELETE FROM executor_triage_recommendations")
    await world.db.execute("DELETE FROM executor_triage_state")
    await world.db.execute(
        "UPDATE executor_orders SET closed_at = datetime('now', '-100 hours'), "
        "reported_at = datetime('now', '-100 hours') WHERE id = ?",
        (old,),
    )
    await world.db.commit()
    await _sweep(world)  # первый свип запоминает момент активации
    assert await _action_of(world, old) is None
    state = await _rows(world.db, "SELECT activated_at FROM executor_triage_state")
    assert len(state) == 1

    await world.db.execute(
        "UPDATE executor_triage_state SET activated_at = datetime('now', '-200 hours')"
    )
    await world.db.execute(
        "UPDATE executor_orders SET closed_at = datetime('now', '-300 hours') WHERE id = ?",
        (old,),
    )
    await world.db.commit()
    new_task = await _task(world, "пропуск после активации")
    missed = await _fail(world, new_task, CLASS1)
    await world.db.execute(
        "DELETE FROM executor_triage_recommendations WHERE order_id = ?", (missed,)
    )
    await world.db.execute(
        "UPDATE executor_orders SET closed_at = datetime('now', '-100 hours'), "
        "reported_at = datetime('now', '-100 hours') WHERE id = ?",
        (missed,),
    )
    await world.db.commit()
    await _sweep(world)
    assert await _action_of(world, missed) == "retry_after_status"
    assert await _action_of(world, old) is None


async def test_edit_without_order_is_unclear_for_retry_and_activation_is_pinned(
    world, monkeypatch
):
    """Круг 3, Codex: правка при retry - тоже unclear; граница не откатывается с разбором."""
    task_id = await _task(world, "правка при повторе")
    oid = await _fail(world, task_id, CLASS1)  # retry_after_status
    await _set_task(world, task_id, statement_generation=1)
    await _rec_at(world, oid, 73 * 60)
    await _sweep(world)
    await _sweep(world)
    assert await _followup(world, oid) == "unclear"
    got = await triage.agreement(world.db)
    assert (
        got["unclear"] == 1 and got["unmeasured"] == got["unclear"] + got["no_decision"]
    )

    # Граница включения переживает откат рекомендации (сбой записи ленты разбора).
    await world.db.execute("DELETE FROM executor_triage_state")
    await world.db.commit()
    real = repo.add_task_update

    async def selective(db, task_id, agent, kind, content, *a, **kw):
        if str(content).startswith("Разбор прогона"):
            raise RuntimeError("лента недоступна")
        return await real(db, task_id, agent, kind, content, *a, **kw)

    other = await _task(world, "сбой ленты")
    lived = await _launch_redeem(world, other)
    with monkeypatch.context() as patched:
        patched.setattr(repo, "add_task_update", selective)
        resp = await _report(world, lived["order"]["id"], CLASS1)
    assert resp.status_code == 200
    assert await _action_of(world, lived["order"]["id"]) is None
    state = await _rows(world.db, "SELECT activated_at FROM executor_triage_state")
    assert len(state) == 1
    await _sweep(world)
    assert await _action_of(world, lived["order"]["id"]) == "retry_after_status"
