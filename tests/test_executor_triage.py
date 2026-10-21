"""Разбор исхода прогона k8 в тени (#1942): рекомендация на заказ, разобранность, замер.

AC-1: рекомендация ровно по правилу постановки и ничего не запускается.
AC-2: одна рекомендация и одна запись ленты на заказ при любых повторах и сбоях.
AC-3: followup проставляется верно, совпадение считается по формуле, не воскресает.
"""

from __future__ import annotations

import asyncio
from typing import Any

from hub import db as hub_db
from hub import repository as repo
from hub.services import executor_orders as orders
from hub.services import executor_triage as triage
from hub.services import owner_minutes
from tests import test_executor_orders as _eo
from tests.test_executor_orders import (
    _launch_redeem,
    _rows,
    _submit,
    _task,
)

world = _eo.world
keys = _eo.keys
_clean_limiter = _eo._clean_limiter
_no_cloud = _eo._no_cloud

CLASS1 = "setup_failed"
CLASS2 = "no_new_head"
CLASS3 = "critic_cycles_exhausted"
CLASS4 = "integrity_violation"
CLASS0 = "critic_failed"


async def _report(world, oid: int, code: str, outcome: str = "rescued"):
    return await world.client.post(
        f"/api/executor-hosts/k8/orders/{oid}/report",
        json={"outcome": outcome, "reason_code": code, "reason": "проба"},
        headers=world.k8_auth,
    )


async def _fail(world, task_id: int, code: str) -> int:
    """Запуск, выкуп и report rescued с кодом: заказ закрыт неудачей."""
    lived = await _launch_redeem(world, task_id)
    oid = lived["order"]["id"]
    resp = await _report(world, oid, code)
    assert resp.status_code == 200, resp.text
    await world.db.commit()
    return oid


async def _recs(world, task_id: int | None = None) -> list[dict]:
    await world.db.commit()
    sql = "SELECT * FROM executor_triage_recommendations"
    if task_id is None:
        return await _rows(world.db, sql + " ORDER BY id")
    return await _rows(world.db, sql + " WHERE task_id = ? ORDER BY id", (task_id,))


async def _action_of(world, oid: int) -> str | None:
    rows = await _rows(
        world.db,
        "SELECT action FROM executor_triage_recommendations WHERE order_id = ?",
        (oid,),
    )
    return rows[0]["action"] if rows else None


async def _count(world, sql: str, params: tuple = ()) -> int:
    await world.db.commit()
    return (await _rows(world.db, sql, params))[0]["n"]


def _feed_sql(task_id: int) -> tuple[str, tuple]:
    return (
        "SELECT COUNT(*) AS n FROM task_updates WHERE task_id = ? "
        "AND content LIKE 'Разбор прогона k8 (тень, #1942)%'",
        (task_id,),
    )


async def _set_task(world, task_id: int, **fields: Any) -> None:
    await repo.update_task(world.db, task_id, **fields)
    await world.db.commit()


async def _relaunch(world, task_id: int) -> int:
    """Новый host-заказ задачи; слот освобождён закрытием без report (не неудача)."""
    oid = (await _launch_redeem(world, task_id))["order"]["id"]
    row = (await _rows(world.db, "SELECT * FROM executor_orders WHERE id = ?", (oid,)))[
        0
    ]
    assert await orders.close_order(
        world.db, row, orders.CLOSE_SESSION_GONE, orders.RUN_OUTCOME_ABORTED
    )
    await world.db.commit()
    return oid


async def _sweep(world):
    """Свип после закрытия текущей секунды: исход той же секунды откладывается."""
    await asyncio.sleep(1.1)
    return await triage.sweep(world.db)


async def _reopen(world, task_id: int) -> None:
    await _set_task(world, task_id, status="open", claimed_by=None)


# ---------------------------------------------------------------------------
# AC-1
# ---------------------------------------------------------------------------


async def test_each_class_gets_its_recommendation_and_nothing_is_launched(
    world, monkeypatch
):
    """AC-1: правило scope_in для каждого класса и случая; запуска нет."""
    # Класс 1: первый - повтор после проверки status, второй подряд - к владельцу.
    t1 = await _task(world, "класс 1")
    o1 = await _fail(world, t1, CLASS1)
    assert await _action_of(world, o1) == "retry_after_status"
    o1b = await _fail(world, t1, CLASS1)
    assert await _action_of(world, o1b) == "to_owner"

    # Класс 2: уточнить technical_hints, один повтор; повторный подряд - к владельцу.
    t2 = await _task(world, "класс 2")
    o2 = await _fail(world, t2, CLASS2)
    assert await _action_of(world, o2) == "refine_hints_and_retry"
    assert await _action_of(world, await _fail(world, t2, CLASS2)) == "to_owner"

    # Класс 3: первый - один повтор, второй - переписать или разделить.
    t3 = await _task(world, "класс 3")
    assert await _action_of(world, await _fail(world, t3, CLASS3)) == "retry_once"
    o3b = await _fail(world, t3, CLASS3)
    assert await _action_of(world, o3b) == "rewrite_or_split"

    # Класс 4 и «не классифицировано» (critic_failed) - к владельцу.
    t4 = await _task(world, "класс 4")
    assert await _action_of(world, await _fail(world, t4, CLASS4)) == "to_owner"
    t0 = await _task(world, "critic_failed")
    assert await _action_of(world, await _fail(world, t0, CLASS0)) == "to_owner"

    # Серия сброшена СДАЧЕЙ: между заказами задачу сдали и вернули в open.
    ts = await _task(world, "сброс сдачей")
    await _fail(world, ts, CLASS1)
    await _submit(world, ts, generation=1)
    await _reopen(world, ts)
    assert (
        await _action_of(world, await _fail(world, ts, CLASS1)) == "retry_after_status"
    )

    # Серия сброшена СМЕНОЙ statement_generation.
    tg = await _task(world, "сброс поколением")
    await _fail(world, tg, CLASS1)
    await _set_task(world, tg, statement_generation=1)
    assert (
        await _action_of(world, await _fail(world, tg, CLASS1)) == "retry_after_status"
    )

    # Третий «повтор» за 24 часа - к владельцу (классы чередуются, серии нет).
    tr = await _task(world, "лимит повторов")
    assert (
        await _action_of(world, await _fail(world, tr, CLASS1)) == "retry_after_status"
    )
    assert await _action_of(world, await _fail(world, tr, CLASS3)) == "retry_once"
    third = await _fail(world, tr, CLASS1)
    assert await _action_of(world, third) == "to_owner"
    assert "за 24 часа" in (await _recs(world, tr))[-1]["reason"]

    # История нечитаема - к владельцу с названной причиной, а не тишина.
    th = await _task(world, "история нечитаема")
    with monkeypatch.context() as patched:
        patched.setattr(triage, "_HISTORY_SQL", "SELECT * FROM no_such_table")
        oh = await _fail(world, th, CLASS1)
    assert await _action_of(world, oh) == "to_owner"
    assert "история прогонов не прочиталась" in (await _recs(world, th))[0]["reason"]

    # Заказ старого образца (поколение постановки неизвестно) - к владельцу.
    tu = await _task(world, "поколение неизвестно")
    lived = await _launch_redeem(world, tu)
    uid = lived["order"]["id"]
    await world.db.execute(
        "UPDATE executor_orders SET statement_generation = NULL WHERE id = ?", (uid,)
    )
    await world.db.commit()
    assert (await _report(world, uid, CLASS1)).status_code == 200
    assert await _action_of(world, uid) == "to_owner"

    # Задача уже взята человеком: никакой рекомендации.
    tk = await _task(world, "взята человеком")
    lived = await _launch_redeem(world, tk)
    kid = lived["order"]["id"]
    await _set_task(world, tk, claimed_by="human-1", claim_session_id="s-human")
    assert (await _report(world, kid, CLASS1)).status_code == 200
    assert await _action_of(world, kid) is None

    # Ни одного лишнего заказа, кода слоту, смены статуса и needs_decision.
    launched = {
        t1: 2,
        t2: 2,
        t3: 2,
        t4: 1,
        t0: 1,
        ts: 2,
        tg: 2,
        tr: 3,
        th: 1,
        tu: 1,
        tk: 1,
    }
    for task_id, count in launched.items():
        got = await _count(
            world,
            "SELECT COUNT(*) AS n FROM executor_orders WHERE task_id = ?",
            (task_id,),
        )
        assert got == count, task_id
    assert (
        await _count(
            world,
            "SELECT COUNT(*) AS n FROM tasks WHERE status = 'needs_decision'",
        )
        == 0
    )
    assert (
        await _count(
            world,
            "SELECT COUNT(*) AS n FROM executor_orders WHERE state <> 'closed'",
        )
        == 0
    )
    assert (
        await _count(
            world,
            "SELECT COUNT(*) AS n FROM chat_pair_sessions WHERE revoked_at IS NULL",
        )
        == 0
    )
    statuses = {r["status"] for r in await _rows(world.db, "SELECT status FROM tasks")}
    assert statuses == {"open", "claimed"} or statuses == {"open"}, statuses


# ---------------------------------------------------------------------------
# AC-2
# ---------------------------------------------------------------------------


async def test_one_recommendation_per_order(world, monkeypatch):
    """AC-2: ровно одна рекомендация и одна запись ленты на заказ."""

    async def totals(task_id: int, oid: int) -> tuple[int, int, int]:
        recs = await _count(
            world,
            "SELECT COUNT(*) AS n FROM executor_triage_recommendations WHERE order_id = ?",
            (oid,),
        )
        events = await _count(
            world,
            "SELECT COUNT(*) AS n FROM events WHERE kind = ? AND task_id = ? "
            "AND payload LIKE ?",
            (triage.EVENT_RECOMMENDED, task_id, f'%"order_id": {oid},%'),
        )
        sql, params = _feed_sql(task_id)
        return recs, events, await _count(world, sql, params)

    # Закрытие заказа, затем поздний report по уже закрытому заказу.
    late = await _task(world, "поздний report")
    lived = await _launch_redeem(world, late)
    late_oid = lived["order"]["id"]
    row = (
        await _rows(world.db, "SELECT * FROM executor_orders WHERE id = ?", (late_oid,))
    )[0]
    assert await orders.close_order(
        world.db, row, orders.CLOSE_SESSION_GONE, orders.RUN_OUTCOME_ABORTED
    )
    await world.db.commit()
    assert await totals(late, late_oid) == (0, 0, 0)  # закрытие без report - не неудача
    assert (await _report(world, late_oid, CLASS1)).status_code == 200
    assert await totals(late, late_oid) == (1, 1, 1)

    # Дубликат того же report - тот же ответ, без второй рекомендации.
    again = await _report(world, late_oid, CLASS1)
    assert again.status_code == 200 and again.json()["duplicate"] is True
    assert await totals(late, late_oid) == (1, 1, 1)

    # Принятая сдача сильнее report failed: ни одной рекомендации.
    sub = await _task(world, "сдача и failed")
    lived = await _launch_redeem(world, sub)
    sub_oid = lived["order"]["id"]
    await _submit(world, sub, generation=1)
    assert (await _report(world, sub_oid, CLASS1, "failed")).status_code == 200
    assert await totals(sub, sub_oid) == (0, 0, 0)

    # Два конкурентных вызова с двух соединений: одна строка, одно событие, одна запись.
    race = await _task(world, "гонка")
    race_oid = await _fail(world, race, CLASS3)
    await world.db.execute(
        "DELETE FROM executor_triage_recommendations WHERE order_id = ?", (race_oid,)
    )
    await world.db.execute(
        "DELETE FROM events WHERE kind = ?", (triage.EVENT_RECOMMENDED,)
    )
    await world.db.execute(
        "DELETE FROM task_updates WHERE task_id = ? AND content LIKE 'Разбор прогона%'",
        (race,),
    )
    await world.db.commit()

    async def attempt() -> Any:
        conn = await _eo._other_connection(world)
        try:
            async with hub_db.write_transaction(conn):
                return await triage.recommend_for_order(conn, race_oid)
        finally:
            await conn.close()

    results = await asyncio.gather(attempt(), attempt())
    assert sum(r is not None for r in results) == 1, results
    assert await totals(race, race_oid) == (1, 1, 1)

    # Rollback не оставляет половины: ни строки, ни события, ни записи.
    rb = await _task(world, "rollback")
    rb_oid = await _fail(world, rb, CLASS1)
    await world.db.execute(
        "DELETE FROM executor_triage_recommendations WHERE order_id = ?", (rb_oid,)
    )
    await world.db.execute(
        "DELETE FROM task_updates WHERE task_id = ? AND content LIKE 'Разбор прогона%'",
        (rb,),
    )
    await world.db.execute(
        "DELETE FROM events WHERE task_id = ? AND kind = ?",
        (rb, triage.EVENT_RECOMMENDED),
    )
    await world.db.commit()
    try:
        async with hub_db.write_transaction(world.db):
            assert await triage.recommend_for_order(world.db, rb_oid) is not None
            raise RuntimeError("откат")
    except RuntimeError:
        pass
    assert await totals(rb, rb_oid) == (0, 0, 0)

    # Сбой записи ленты откатывает рекомендацию целиком, report при этом выживает.
    fail = await _task(world, "сбой ленты")
    lived = await _launch_redeem(world, fail)
    fail_oid = lived["order"]["id"]
    real = repo.add_task_update

    # Закрытие заказа пишет свою запись той же функцией: падает только запись разбора.
    async def selective(db, task_id, agent, kind, content, *a, **kw):
        if str(content).startswith("Разбор прогона"):
            raise RuntimeError("лента недоступна")
        return await real(db, task_id, agent, kind, content, *a, **kw)

    with monkeypatch.context() as patched:
        patched.setattr(repo, "add_task_update", selective)
        resp = await _report(world, fail_oid, CLASS1)
    assert resp.status_code == 200, resp.text
    assert await totals(fail, fail_oid) == (0, 0, 0)
    saved = await _rows(
        world.db, "SELECT report_outcome FROM executor_orders WHERE id = ?", (fail_oid,)
    )
    assert saved[0]["report_outcome"] == "rescued"
    # Свип добирает пропущенное одной рекомендацией.
    await _sweep(world)
    await _sweep(world)
    assert await totals(fail, fail_oid) == (1, 1, 1)

    # События очищены по retention: свип не создаёт вторую рекомендацию и запись.
    await world.db.execute(
        "DELETE FROM events WHERE kind = ?", (triage.EVENT_RECOMMENDED,)
    )
    await world.db.commit()
    await _sweep(world)
    recs, events, feed = await totals(fail, fail_oid)
    assert (recs, feed) == (1, 1) and events == 0


# ---------------------------------------------------------------------------
# AC-3
# ---------------------------------------------------------------------------


async def _followup(world, oid: int) -> str | None:
    await world.db.commit()
    rows = await _rows(
        world.db,
        "SELECT followup FROM executor_triage_recommendations WHERE order_id = ?",
        (oid,),
    )
    return rows[0]["followup"]


async def test_resolution_and_agreement_are_measured(world):
    """AC-3: followup по каждому варианту, формула совпадения, без воскрешения."""
    await _owner_minutes_state(world)  # курсор учёта - до первой рекомендации
    assert (await triage.agreement(world.db))["total"] == 0
    assert (await triage.agreement(world.db))["rate"] is None

    # retry_after_status + новый host-заказ -> new_order (совпало).
    t_new = await _task(world, "новый заказ")
    o_new = await _fail(world, t_new, CLASS1)
    await _relaunch(world, t_new)
    # retry_once + смена постановки -> statement_changed (не совпало).
    t_stmt = await _task(world, "смена постановки")
    o_stmt = await _fail(world, t_stmt, CLASS3)
    await _set_task(world, t_stmt, statement_generation=1)
    # retry_after_status + подзадача -> split (не совпало).
    t_split = await _task(world, "разделение")
    o_split = await _fail(world, t_split, CLASS1)
    await repo.create_task(
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
        parent_id=t_split,
        priority="medium",
    )
    await world.db.commit()
    # refine + сдача -> submitted (устарела, вне знаменателя).
    t_sub = await _task(world, "сдача")
    o_sub = await _fail(world, t_sub, CLASS2)
    await _submit(world, t_sub, generation=1)
    # архив -> archived (устарела).
    t_arch = await _task(world, "архив")
    o_arch = await _fail(world, t_arch, CLASS1)
    await _set_task(world, t_arch, archived=1)
    # 72 часа без действия -> none_72h (отдельный счётчик).
    t_72 = await _task(world, "72 часа")
    o_72 = await _fail(world, t_72, CLASS1)
    await world.db.execute(
        "UPDATE executor_triage_recommendations SET created_at = datetime('now', '-73 hours') "
        "WHERE order_id = ?",
        (o_72,),
    )
    # to_owner + смена постановки -> statement_changed (совпало).
    t_c4 = await _task(world, "целостность")
    o_c4 = await _fail(world, t_c4, CLASS4)
    await _set_task(world, t_c4, statement_generation=1)
    # to_owner + новый заказ без смены постановки -> new_order (не совпало).
    t_c0 = await _task(world, "не классифицировано")
    o_c0 = await _fail(world, t_c0, CLASS0)
    await _relaunch(world, t_c0)
    # refine: правка постановки без заказа - ещё не итог; затем новый заказ -> new_order (совпало).
    t_ref = await _task(world, "уточнение")
    o_ref = await _fail(world, t_ref, CLASS2)
    await _set_task(world, t_ref, statement_generation=1)
    # rewrite_or_split + подзадача -> split (совпало).
    t_rw = await _task(world, "переписать")
    o_rw1 = await _fail(world, t_rw, CLASS3)
    await _reopen(world, t_rw)
    o_rw = await _fail(world, t_rw, CLASS3)
    assert await _action_of(world, o_rw) == "rewrite_or_split"
    await repo.create_task(
        world.db,
        title="часть 2",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status="open",
        auto_review=True,
        task_type="task",
        parent_id=t_rw,
        priority="medium",
    )
    # Без действия: остаётся в ожидании.
    t_wait = await _task(world, "ждёт")
    o_wait = await _fail(world, t_wait, CLASS1)
    await world.db.commit()

    await _sweep(world)
    assert await _followup(world, o_ref) is None  # правка без заказа - не итог
    await _relaunch(world, t_ref)
    await _sweep(world)

    expected = {
        o_new: "new_order",
        o_stmt: None,  # повтор + только правка постановки: ещё не итог
        o_split: "split",
        o_sub: "submitted",
        o_arch: "archived",
        o_72: "none_72h",
        o_c4: "statement_changed",
        o_c0: "new_order",
        o_ref: "new_order",
        o_rw: "split",
        o_rw1: "new_order",  # заказ раньше подзадачи: первое действие побеждает
        o_wait: None,
    }
    for oid, followup in expected.items():
        assert await _followup(world, oid) == followup, oid

    # Формула: знаменатель - только с followup, кроме устарела и без решения.
    got = await triage.agreement(world.db)
    assert (got["total"], got["matched"]) == (7, 5), got
    assert got["rate"] == 5 / 7
    assert (got["stale"], got["no_decision"], got["pending"]) == (2, 1, 2)
    assert got["by_class"][1] == {"total": 2, "matched": 1, "rate": 0.5}
    assert got["by_class"][2] == {"total": 1, "matched": 1, "rate": 1.0}
    assert got["by_class"][3] == {"total": 2, "matched": 2, "rate": 1.0}
    assert got["by_class"][4]["matched"] == 1 and got["by_class"][0]["matched"] == 0
    assert got["accepted_share"] is None

    # Разобранная не воскресает: задача снова open, свипы и повторные вызовы - то же самое.
    await _reopen(world, t_new)
    await _set_task(world, t_stmt, statement_generation=5)
    snapshot = await _recs(world)
    for oid in expected:
        await triage.recommend_for_order(world.db, oid)
    await _sweep(world)
    await _sweep(world)
    assert await _recs(world) == snapshot

    # Первое событие побеждает: у new_order следом архив ничего не меняет.
    await _set_task(world, t_new, archived=1)
    await _sweep(world)
    assert await _followup(world, o_new) == "new_order"

    # owner_minutes и человеческие касания не изменились от рекомендаций и свипа.
    # Начислен только человеческий запуск каждого заказа; событие разбора (actor hub)
    # минут не начисляет.
    accrued, _seconds = await _owner_minutes_state(world)
    launches = await _count(world, "SELECT COUNT(*) AS n FROM executor_orders")
    kinds = await _rows(
        world.db,
        "SELECT DISTINCT e.kind FROM owner_minute_accruals a "
        "JOIN events e ON e.id = a.source_event_id",
    )
    assert [k["kind"] for k in kinds] == [owner_minutes.EXECUTOR_ORDER_EVENT]
    assert accrued <= launches


async def _owner_minutes_state(world) -> tuple[int, int]:
    await owner_minutes.accrual_pass(world.db)
    await world.db.commit()
    row = (
        await _rows(
            world.db,
            "SELECT COUNT(*) AS n, COALESCE(SUM(seconds), 0) AS s "
            "FROM owner_minute_accruals",
        )
    )[0]
    return row["n"], row["s"]


# ---------------------------------------------------------------------------
# Схема и проход поллера
# ---------------------------------------------------------------------------


async def test_schema_generation_column_and_poller_sweep_repairs(world):
    """Колонка поколения постановки заполняется при выпуске; проход поллера добирает."""
    cols = {
        r["name"]: r
        for r in await _rows(world.db, "PRAGMA table_info(executor_orders)")
    }
    assert (
        "statement_generation" in cols and cols["statement_generation"]["notnull"] == 0
    )
    table = await _rows(world.db, "PRAGMA table_info(executor_triage_recommendations)")
    assert {"order_id", "action", "followup", "owner_verdict"} <= {
        r["name"] for r in table
    }

    task_id = await _task(world, "поколение в заказе")
    await _set_task(world, task_id, statement_generation=3)
    oid = await _fail(world, task_id, CLASS1)
    stored = await _rows(
        world.db,
        "SELECT statement_generation FROM executor_orders WHERE id = ?",
        (oid,),
    )
    assert stored[0]["statement_generation"] == 3

    # Пропущенная рекомендация добирается проходом поллера (sweep_executor_orders).
    await world.db.execute("DELETE FROM executor_triage_recommendations")
    await world.db.commit()
    await orders.sweep_executor_orders(world.db)
    assert await _action_of(world, oid) == "retry_after_status"
    await orders.sweep_executor_orders(world.db)
    assert len(await _recs(world, task_id)) == 1


async def test_late_report_recovery_child_floor_and_cancellation(world, monkeypatch):
    """Находки Codex: давний заказ с поздним report, старая подзадача, отмена."""
    # Заказ закрыт 73 часа назад, report пришёл только что: свип всё равно добирает.
    old = await _task(world, "давний заказ")
    lived = await _launch_redeem(world, old)
    oid = lived["order"]["id"]
    row = (await _rows(world.db, "SELECT * FROM executor_orders WHERE id = ?", (oid,)))[
        0
    ]
    assert await orders.close_order(
        world.db, row, orders.CLOSE_SESSION_GONE, orders.RUN_OUTCOME_ABORTED
    )
    await world.db.execute(
        "UPDATE executor_orders SET closed_at = datetime('now', '-73 hours') WHERE id = ?",
        (oid,),
    )
    await world.db.commit()
    with monkeypatch.context() as patched:

        async def nothing(db, order_id):
            return None

        patched.setattr(triage, "recommend_safely", nothing)
        assert (await _report(world, oid, CLASS1)).status_code == 200
    assert await _action_of(world, oid) is None
    await _sweep(world)
    assert await _action_of(world, oid) == "retry_after_status"

    # Подзадача, созданная ДО рекомендации (в ту же секунду), не даёт split.
    kid = await _task(world, "с подзадачей")
    kid_oid = (await _launch_redeem(world, kid))["order"]["id"]
    await repo.create_task(
        world.db,
        title="старая часть",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status="open",
        auto_review=True,
        task_type="task",
        parent_id=kid,
        priority="medium",
    )
    await world.db.commit()
    assert (await _report(world, kid_oid, CLASS1)).status_code == 200
    await _sweep(world)
    assert await _followup(world, kid_oid) is None
    await repo.create_task(
        world.db,
        title="новая часть",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status="open",
        auto_review=True,
        task_type="task",
        parent_id=kid,
        priority="medium",
    )
    await world.db.commit()
    await _sweep(world)
    assert await _followup(world, kid_oid) == "split"

    # Отмена посреди разбора откатывает точку: ни строки, ни события без ленты.
    can = await _task(world, "отмена")
    can_oid = await _fail(world, can, CLASS3)
    await world.db.execute(
        "DELETE FROM executor_triage_recommendations WHERE order_id = ?", (can_oid,)
    )
    await world.db.execute(
        "DELETE FROM events WHERE task_id = ? AND kind = ?",
        (can, triage.EVENT_RECOMMENDED),
    )
    await world.db.execute(
        "DELETE FROM task_updates WHERE task_id = ? AND content LIKE 'Разбор прогона%'",
        (can,),
    )
    await world.db.commit()
    real = repo.add_task_update

    async def cancelled(db, task_id, agent, kind, content, *a, **kw):
        if str(content).startswith("Разбор прогона"):
            raise asyncio.CancelledError
        return await real(db, task_id, agent, kind, content, *a, **kw)

    with monkeypatch.context() as patched:
        patched.setattr(repo, "add_task_update", cancelled)
        await world.db.execute("BEGIN IMMEDIATE")
        try:
            await triage.recommend_safely(world.db, can_oid)
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("отмена проглочена")
        await world.db.commit()  # внешний владелец поймал отмену и закоммитил
    assert (
        await _count(
            world,
            "SELECT COUNT(*) AS n FROM executor_triage_recommendations WHERE order_id = ?",
            (can_oid,),
        )
        == 0
    )
    assert (
        await _count(
            world,
            "SELECT COUNT(*) AS n FROM events WHERE task_id = ? AND kind = ?",
            (can, triage.EVENT_RECOMMENDED),
        )
        == 0
    )
