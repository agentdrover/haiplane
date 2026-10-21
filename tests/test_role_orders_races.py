"""Заказ роли k8: lease, старт, отчёт и истечение на двух соединениях (#1973).

Гонки здесь настоящие: два соединения к одной файловой базе, второе ждёт
write-лок первого, исход решает порядок взятия замка. Оба порядка проверяются
отдельно: тест, где побеждает всегда один и тот же, ничего не доказывает.
"""

from __future__ import annotations

import asyncio

from hub import db as hub_db
from hub import repository as repo
from hub.services import role_orders
from tests import role_orders_support as _support
from tests.role_orders_support import (
    admit,
    age,
    enter_needs_decision,
    leased,
    order_row,
    order_rows,
    poll,
    report,
    rows,
    started,
    sweep,
    task_in_project,
)

world = _support.world
keys = _support.keys
_clean_limiter = _support._clean_limiter
_no_cloud = _support._no_cloud
_cursor_ready = _support._cursor_ready


async def _one_order(world, title: str = "заказ") -> int:
    task_id = await task_in_project(world, title)
    await enter_needs_decision(world, task_id)
    assert await sweep(world) == 1
    return (await order_rows(world))[-1]["id"]


async def _principal(world, host: str) -> int:
    got = await rows(
        world.db, "SELECT principal_id FROM executor_hosts WHERE name = ?", (host,)
    )
    return int(got[0]["principal_id"])


async def _blocked_then(world, blocked, holder_work):
    """``blocked`` ждёт write-лок другого соединения; ``holder_work`` идёт под этим замком.

    Порядок: чужой замок взят -> ``blocked`` стартует и встаёт в очередь -> чужая
    работа завершена и закоммичена -> ``blocked`` проходит уже по новому состоянию.
    """
    holder = await hub_db.connect(world.dsn)
    waiter_db = await hub_db.connect(world.dsn)
    try:
        await holder.execute("BEGIN IMMEDIATE")
        waiting = asyncio.ensure_future(blocked(waiter_db))
        await asyncio.sleep(0.4)
        assert not waiting.done(), "второе соединение не ждёт write-лок"
        await holder_work(holder)
        await holder.commit()
        try:
            return await asyncio.wait_for(waiting, 10)
        except role_orders.RoleOrderError as exc:
            return exc
    finally:
        await holder.close()
        await waiter_db.close()


async def _clear(world) -> None:
    """Закрыть все незавершённые заказы: следующий раздел теста начинает с чистого стола."""
    await world.db.execute(
        "UPDATE role_orders SET state = 'done', closed_at = datetime('now') "
        "WHERE state IN ('open', 'leased', 'started')"
    )
    await world.db.commit()


async def _expire_lease(world, order_id: int) -> None:
    await age(world, order_id, lease_deadline="-1 minute")


async def test_a_lease_goes_to_one_admitted_host(world):
    """AC-2: два хоста опрашивают одновременно - lease у одного; потеря ответа; недопущенный хост."""
    await admit(world, "k8")
    await admit(world, "k9")
    order_id = await _one_order(world)

    first, second = await asyncio.gather(poll(world, "k8"), poll(world, "k9"))
    answers = {"k8": first.json()["orders"], "k9": second.json()["orders"]}
    winners = [h for h, o in answers.items() if o]
    assert len(winners) == 1, answers
    winner = winners[0]
    loser = "k9" if winner == "k8" else "k8"
    lease = answers[winner][0]
    assert lease["id"] == order_id and answers[loser] == []
    row = await order_row(world, order_id)
    assert row["state"] == "leased" and row["host"] == winner

    # Потерян ответ: тот же хост спрашивает снова и получает ТОТ ЖЕ lease.
    again = (await poll(world, winner)).json()["orders"]
    assert again and again[0]["lease_id"] == lease["lease_id"]
    assert again[0]["lease_deadline"] == lease["lease_deadline"]
    # Не больше одного активного lease на хост: второй заказ ему не выдаётся.
    second_order = await _one_order(world, "второй заказ")
    assert (await poll(world, winner)).json()["orders"][0]["id"] == order_id
    assert (await poll(world, loser)).json()["orders"][0]["id"] == second_order

    # Хост не допущен к проекту заказа - заказа не получает, строка остаётся открытой.
    other_project = await repo.create_project(world.db, slug="other-1973", name="o")
    foreign = await task_in_project(world, "чужой проект")
    await world.db.execute(
        "UPDATE tasks SET project_id = ? WHERE id = ?", (other_project, foreign)
    )
    await world.db.commit()
    await _clear(world)
    await enter_needs_decision(world, foreign)
    assert await sweep(world) == 1
    third = (await order_rows(world))[-1]
    for host in ("k8", "k9"):
        got = (await poll(world, host)).json()["orders"]
        assert all(o["id"] != third["id"] for o in got), host
    assert (await order_row(world, third["id"]))["state"] == "open"


async def test_no_reissue_after_start_and_stale_reports_refused(world):
    """AC-3: до started - перевыдача не больше двух; после started - нет; поздний report - 409; гонки."""
    await admit(world, "k8")
    await admit(world, "k9")
    k8_id = await _principal(world, "k8")

    # 1. Lease без started: перевыдача с новым lease_id, не больше двух раз.
    order_id = await _one_order(world)
    lease_ids = []
    for attempt in (1, 2, 3):
        got = await leased(world, "k8")
        assert got["attempt"] == attempt
        lease_ids.append(got["lease_id"])
        await _expire_lease(world, order_id)
        await role_orders.run_sweep(world.db)
        await world.db.commit()
    assert len(set(lease_ids)) == 3
    row = await order_row(world, order_id)
    assert row["state"] == "failed" and row["outcome"] == "lease_exhausted"
    assert (await poll(world, "k8")).json()["orders"] == []
    # Устаревший lease не запускает работу.
    late = await started(world, "k8", order_id, lease_ids[0])
    assert late.status_code == 409

    # 2. Старт, потом истечение срока работы: перевыдачи НЕТ, failed + уведомление стюарду.
    second = await _one_order(world, "стартовал")
    lease = await leased(world, "k8")
    assert (await started(world, "k8", second, lease["lease_id"])).status_code == 200
    await age(
        world, second, lease_deadline="-1 hour"
    )  # lease давно вышел, но работа идёт
    await role_orders.run_sweep(world.db)
    await world.db.commit()
    assert (await order_row(world, second))["state"] == "started"
    # Повтор started тем же lease: 200, started_at не обновляется.
    await age(world, second, started_at="-5 minutes")
    stamp = (await order_row(world, second))["started_at"]
    again = await started(world, "k8", second, lease["lease_id"])
    assert again.status_code == 200
    assert (await order_row(world, second))["started_at"] == stamp
    await age(world, second, started_at=f"-{role_orders.RUN_MAX_MINUTES + 1} minutes")
    await role_orders.run_sweep(world.db)
    await world.db.commit()
    row = await order_row(world, second)
    assert row["state"] == "failed" and row["outcome"] == "run_timeout"
    assert row["attempt"] == 1 and (await poll(world, "k9")).json()["orders"] == []
    notices = await rows(
        world.db,
        "SELECT body FROM agent_messages WHERE to_ref LIKE 'steward@%' "
        "AND body LIKE '%role_order_failed%'",
    )
    assert len(notices) == 2  # lease_exhausted и run_timeout, по одному на заказ
    late_report = await report(world, "k8", second, lease["lease_id"])
    assert late_report.status_code == 409
    assert (await order_row(world, second))["state"] == "failed"

    # 3. Поздний report прежней попытки: заказ перевыдан другому хосту.
    await _clear(world)
    third = await _one_order(world, "перевыдан")
    old = await leased(world, "k8")
    await _expire_lease(world, third)
    await role_orders.run_sweep(world.db)
    await world.db.commit()
    new = await leased(world, "k9")
    assert new["id"] == third and new["lease_id"] != old["lease_id"]
    assert (await started(world, "k9", third, new["lease_id"])).status_code == 200
    stale = await report(world, "k8", third, old["lease_id"])
    assert stale.status_code in (403, 404, 409)
    assert (await order_row(world, third))["state"] == "started"
    wrong_lease = await report(world, "k9", third, old["lease_id"])
    assert wrong_lease.status_code == 409
    assert (await order_row(world, third))["state"] == "started"

    # 4. Гонка «истечение lease / started», оба порядка.
    # 4a. Истечение первым: started после перевыдачи отказан.
    await _clear(world)
    fourth = await _one_order(world, "гонка a")
    lease_a = await leased(world, "k8")
    await _expire_lease(world, fourth)

    async def expire(conn):
        await role_orders.run_sweep(conn)

    async def start_a(conn):
        return await role_orders.started(conn, "k8", k8_id, fourth, lease_a["lease_id"])

    outcome = await _blocked_then(world, start_a, expire)
    assert isinstance(outcome, role_orders.RoleOrderError) and outcome.status == 409
    row = await order_row(world, fourth)
    assert row["state"] == "open" and row["attempt"] == 2
    # 4b. Started первым (срок ещё не вышел): истёкший к коммиту lease уже ничего не перевыдаёт.
    await _clear(world)
    fifth = await _one_order(world, "гонка b")
    lease_b = await leased(world, "k8")

    async def start_b(conn):
        await role_orders.started(conn, "k8", k8_id, fifth, lease_b["lease_id"])
        await conn.execute(
            "UPDATE role_orders SET lease_deadline = datetime('now', '-1 minute') "
            "WHERE id = ?",
            (fifth,),
        )

    await _blocked_then(world, expire, start_b)
    row = await order_row(world, fifth)
    assert row["state"] == "started" and row["attempt"] == 1

    # 5. Гонка «истечение срока работы / report», оба порядка.
    await _clear(world)
    sixth = await _one_order(world, "гонка c")
    lease_c = await leased(world, "k8")
    await started(world, "k8", sixth, lease_c["lease_id"])
    await age(world, sixth, started_at=f"-{role_orders.RUN_MAX_MINUTES + 1} minutes")

    async def report_c(conn):
        return await role_orders.report(
            conn, "k8", k8_id, sixth, lease_c["lease_id"], "done", "готово"
        )

    outcome = await _blocked_then(world, report_c, expire)
    assert isinstance(outcome, role_orders.RoleOrderError) and outcome.status == 409
    assert (await order_row(world, sixth))["outcome"] == "run_timeout"
    await _clear(world)
    seventh = await _one_order(world, "гонка d")
    lease_d = await leased(world, "k8")
    await started(world, "k8", seventh, lease_d["lease_id"])

    async def report_d(conn):
        await role_orders.report(
            conn, "k8", k8_id, seventh, lease_d["lease_id"], "done", "готово"
        )
        await conn.execute(
            "UPDATE role_orders SET started_at = datetime('now', ?) WHERE id = ?",
            (f"-{role_orders.RUN_MAX_MINUTES + 1} minutes", seventh),
        )

    await _blocked_then(world, expire, report_d)
    assert (await order_row(world, seventh))["state"] == "done"


async def test_report_repeats_are_noops_and_changes_are_refused(world):
    """Одинаковый повтор после done/failed - no-op, изменённый - 409; сначала повтор, потом отказ по state."""
    await admit(world, "k8")
    order_id = await _one_order(world)
    lease = await leased(world, "k8")
    await started(world, "k8", order_id, lease["lease_id"])
    first = await report(world, "k8", order_id, lease["lease_id"], "failed", "сбой")
    assert first.status_code == 200 and first.json().get("repeat") is False
    closed_at = (await order_row(world, order_id))["closed_at"]
    again = await report(world, "k8", order_id, lease["lease_id"], "failed", "сбой")
    assert again.status_code == 200 and again.json()["repeat"] is True
    changed = await report(world, "k8", order_id, lease["lease_id"], "done", "сбой")
    assert changed.status_code == 409
    row = await order_row(world, order_id)
    assert row["state"] == "failed" and row["closed_at"] == closed_at
    updates = await repo.get_task_updates(world.db, row["task_id"])
    assert sum(f"#{order_id}" in dict(u)["content"] for u in updates) == 1
    bad = await report(world, "k8", order_id, lease["lease_id"], "maybe", "x")
    assert bad.status_code == 422
