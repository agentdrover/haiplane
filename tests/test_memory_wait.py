"""«Ждёт памяти»: заказ k8 честно ждёт память до выкупа (#1972).

Хост шлёт на существующую ручку ``/phase`` фазу ``waiting_memory`` с цифрами
памяти ДО выкупа. Пока heartbeat живой, заказ и его код не сгорают; граница — 6 ч.
Гонки со сроком и выкупом — в ``test_memory_wait_races.py``.
"""

from __future__ import annotations

import re

from tests import test_executor_orders as _eo
from tests.memory_wait_support import (
    FREE,
    NEED,
    age,
    alive,
    code_row,
    deliver,
    hhmm,
    launch_order,
    phase,
    row,
    surfaces,
    sweep,
    wait_for_memory,
)
from tests.test_executor_orders import _code_of, _redeem

world = _eo.world
keys = _eo.keys
_clean_limiter = _eo._clean_limiter
_no_cloud = _eo._no_cloud

LINE = re.compile(
    r"ждёт памяти на (?P<host>\w+) с (?P<since>\d\d:\d\d): нужно (?P<need>[\d,]+) ГиБ, "
    r"свободно (?P<free>[\d,]+) ГиБ, последний сигнал (?P<seen>\d\d:\d\d)"
)


async def _expect_visible(world, task_id: int, order_id: int, host: str, capsys):
    """Ожидание видно на всех поверхностях одной и той же строкой, собранной хабом."""
    got = await row(world, order_id)
    seen = await surfaces(world, task_id, capsys)
    view = seen["rest"]["waiting_memory"]
    assert seen["rest"]["state"] == "waiting_memory"
    assert seen["cli_json"] == seen["rest"]
    line = view["line"]
    parsed = LINE.fullmatch(line)
    assert parsed, line
    assert (
        parsed["host"] == host
        and parsed["need"] == "6"
        and parsed["free"]
        in (
            "3",
            "0",
        )
    )
    assert parsed["since"] == hhmm(got["waiting_since"])
    assert parsed["seen"] == hhmm(got["last_seen_at"])
    assert (view["need_mib"], view["free_mib"]) == (got["need_mib"], got["free_mib"])
    for name in ("mcp_compact", "mcp_full"):
        assert line in seen[name], name
    assert line in seen["cli_err"]
    assert line in seen["page"] and 'data-waiting-memory="1"' in seen["page"]
    # Плашка запуска говорит об ожидании, а не «код выкуплен».
    assert "ждёт памяти" in seen["plaque"] and "код выкуплен" not in seen["plaque"]


async def _one_waiting_order(world, host: str, delivered: bool, capsys):
    task_id, order_id = await launch_order(world, host, title=f"ждёт {host}")
    if delivered:
        await deliver(world, host)
        assert (await row(world, order_id))["state"] == "delivered"
    else:
        assert (await row(world, order_id))["state"] == "pending"
    free = 0 if not delivered else FREE  # free_mib=0 допустим
    first = await wait_for_memory(world, order_id, host, free=free)
    assert first.status_code == 200, first.text
    state = await row(world, order_id)
    assert state["state"] == "waiting_memory"
    assert (state["need_mib"], state["free_mib"]) == (NEED, free)
    assert state["waiting_since"] and state["last_seen_at"]

    # > 300 с (первоначальный срок кода) с живым heartbeat: ничего не сгорело.
    for _ in range(3):
        await age(world, order_id, 360)
        before = await row(world, order_id)
        beat = await wait_for_memory(world, order_id, host, free=free)
        assert beat.status_code == 200, beat.text
        # heartbeat не переписывает waiting_since (его сдвинули только часы).
        assert (await row(world, order_id))["waiting_since"] == before["waiting_since"]
    aged = await row(world, order_id)
    assert aged["state"] == "waiting_memory" and await alive(world, order_id)
    code = await code_row(world, order_id)
    assert code is not None and code["alive"] and code["redeemed_at"] is None
    assert await sweep(world) == 0
    assert (await row(world, order_id))["state"] == "waiting_memory"
    await _expect_visible(world, task_id, order_id, host, capsys)

    # Повторная доставка отдаёт то же ожидание, не сбросив waiting_since.
    before = (await row(world, order_id))["waiting_since"]
    again = await deliver(world, host)
    assert again["id"] == order_id and again["state"] == "waiting_memory"
    after = await row(world, order_id)
    assert after["waiting_since"] == before and after["state"] == "waiting_memory"

    redeemed = await _redeem(world, _code_of(world, again, host))
    assert redeemed.status_code == 200, redeemed.text
    assert (await row(world, order_id))["state"] == "redeemed"


async def test_a_waiting_order_keeps_its_code_and_is_visible(world, capsys):
    """AC-1: pending и delivered -> waiting_memory; код не сгорел за > 300 с; выкуп проходит."""
    await _one_waiting_order(world, "k8", delivered=False, capsys=capsys)
    await _one_waiting_order(world, "k9", delivered=True, capsys=capsys)


async def test_heartbeat_extends_but_never_revives(world):
    """AC-2: heartbeat не сбрасывает waiting_since; без него заказ истекает; поздний не оживляет."""
    # (a) повторный heartbeat: цифры обновились, waiting_since прежний.
    _task_a, first = await launch_order(world, "k8", title="heartbeat")
    assert (await wait_for_memory(world, first)).status_code == 200
    began = await row(world, first)
    await age(world, first, 120)
    aged = await row(world, first)
    assert (
        await wait_for_memory(world, first, need=8192, free=1024)
    ).status_code == 200
    beat = await row(world, first)
    assert beat["waiting_since"] == aged["waiting_since"] != ""
    assert beat["last_seen_at"] > aged["last_seen_at"]
    assert (beat["need_mib"], beat["free_mib"]) == (8192, 1024)
    assert (
        beat["expires_at"] > aged["expires_at"] and began["state"] == "waiting_memory"
    )

    # (b) heartbeat пропал > 10 мин: заказ истекает, как сейчас.
    _task_b, lost = await launch_order(world, "k9", title="без heartbeat")
    assert (await wait_for_memory(world, lost, "k9")).status_code == 200
    await age(world, lost, 11 * 60)
    assert not await alive(world, lost)
    assert await sweep(world) >= 1
    closed = await row(world, lost)
    assert closed["state"] == "closed" and closed["close_reason"] == "expired"
    assert await code_row(world, lost) is None  # код сожжён закрытием

    # (c) поздний heartbeat до прохода sweep просроченный заказ не оживляет.
    _task_c, late = await launch_order(world, "k9", title="поздний heartbeat")
    assert (await wait_for_memory(world, late, "k9")).status_code == 200
    await age(world, late, 11 * 60)
    stale = await row(world, late)
    revived = await wait_for_memory(world, late, "k9", free=0)
    assert revived.status_code == 409, revived.text
    unchanged = await row(world, late)
    for key in ("state", "expires_at", "waiting_since", "last_seen_at", "free_mib"):
        assert unchanged[key] == stale[key], key
    assert not await alive(world, late)
    assert await sweep(world) >= 1
    assert (await row(world, late))["close_reason"] == "expired"


async def test_only_the_host_of_an_unredeemed_order_may_wait(world):
    """AC-4: чужой хост и заказ, ключ агента, выкупленный заказ, плохие цифры — отказ без записи."""
    _t1, mine = await launch_order(world, "k8", title="свой")
    _t2, theirs = await launch_order(world, "k9", title="чужой")
    snapshot = await row(world, mine)
    other_snapshot = await row(world, theirs)

    async def refused(order_id, body, host="k8", auth=None, codes=(403, 409, 422)):
        resp = await phase(world, order_id, body, host, auth)
        assert resp.status_code in codes, (body, resp.status_code, resp.text)
        return resp

    good = {"phase": "waiting_memory", "need_mib": NEED, "free_mib": FREE}
    # чужой хост по пути k8, чужой заказ по пути k8, ключ агента и человека, без ключа
    await refused(mine, good, "k8", world.k9_auth, codes=(403,))
    await refused(theirs, good, "k8", None, codes=(403,))
    await refused(mine, good, "k8", world.agent_auth, codes=(403,))
    await refused(mine, good, "k8", world.human_auth, codes=(403,))
    await refused(mine, good, "k8", {}, codes=(401, 403))
    # неверные цифры
    for bad in (
        {"need_mib": 0},
        {"need_mib": -5},
        {"need_mib": 1_048_577},
        {"free_mib": -1},
        {"free_mib": 1_048_577},
        {"need_mib": "6144"},
        {"free_mib": "0"},
        {"need_mib": True},
        {"free_mib": False},
        {"need_mib": 6144.0},
        {"free_mib": 3.5},
        {"need_mib": None},
        {"free_mib": None},
    ):
        await refused(mine, {**good, **bad}, codes=(422,))
    for missing in ("need_mib", "free_mib"):
        body = dict(good)
        del body[missing]
        await refused(mine, body, codes=(422,))
    assert await row(world, mine) == snapshot
    assert await row(world, theirs) == other_snapshot
    assert snapshot["state"] in ("pending", "delivered")

    # Граничные значения допустимы; free_mib=0 тоже.
    edge = await phase(
        world,
        mine,
        {"phase": "waiting_memory", "need_mib": 1_048_576, "free_mib": 0},
    )
    assert edge.status_code == 200, edge.text
    # После выкупа ожидание невозможно.
    assert (
        await _redeem(world, _code_of(world, await deliver(world, "k8")))
    ).status_code == 200
    done = await row(world, mine)
    assert done["state"] == "redeemed"
    late = await phase(world, mine, good)
    assert late.status_code == 409, late.text
    assert await row(world, mine) == done
    # Закрытый заказ тоже.
    ack = await world.client.post(
        f"/api/executor-hosts/k9/orders/{theirs}/ack",
        json={"result": "redeem_failed"},
        headers=world.k9_auth,
    )
    assert ack.status_code == 200, ack.text
    after_close = await row(world, theirs)
    assert after_close["state"] == "closed"
    assert (await phase(world, theirs, good, "k9")).status_code == 409
    assert await row(world, theirs) == after_close


async def test_migration_rebuilds_the_check_and_keeps_orders(monkeypatch):
    """Шаг миграции: CHECK принимает waiting_memory, строки и индексы целы, повтор — пустая работа."""
    import aiosqlite

    import hub.db as db_module
    from hub.migrations import FROZEN_MIGRATIONS
    from hub.migrations.steps import load_steps
    from tests.test_db_migrations import _SCHEMA

    steps = [(n, s) for n, s in load_steps() if "1972" in n]
    assert steps, "нет шага миграции #1972"
    monkeypatch.setattr(db_module, "_MIGRATIONS", list(FROZEN_MIGRATIONS))
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    try:
        await conn.executescript(_SCHEMA)
        await db_module._migrate(conn)
        await conn.execute("PRAGMA foreign_keys = OFF")
        await conn.execute(
            "INSERT INTO executor_orders (host, task_id, generation, executor_run_id, "
            "state, expires_at, order_text) VALUES ('k8', 7, 1, 9, 'delivered', "
            "'2030-01-01 00:00:00', 'text')"
        )
        await conn.commit()
        try:
            await conn.execute(
                "UPDATE executor_orders SET state = 'waiting_memory' WHERE id = 1"
            )
            raise AssertionError("до миграции CHECK обязан отказывать")
        except aiosqlite.IntegrityError:
            await conn.rollback()

        monkeypatch.setattr(db_module, "_MIGRATIONS", [*FROZEN_MIGRATIONS, *steps])
        await db_module._migrate(conn)
        kept = await conn.execute_fetchall("SELECT * FROM executor_orders")
        assert [dict(r)["order_text"] for r in kept] == ["text"]
        assert dict(kept[0])["state"] == "delivered"
        assert dict(kept[0])["waiting_since"] is None
        await conn.execute(
            "UPDATE executor_orders SET state = 'waiting_memory', waiting_since = "
            "datetime('now'), last_seen_at = datetime('now'), need_mib = 1, "
            "free_mib = 0 WHERE id = 1"
        )
        await conn.commit()
        names = {
            r[0]
            for r in await conn.execute_fetchall(
                "SELECT name FROM sqlite_master WHERE tbl_name = 'executor_orders'"
            )
        }
        assert {
            "idx_executor_orders_one_open_per_host",
            "idx_executor_orders_task",
        } <= names
        assert "executor_orders_mw" not in {
            r[0]
            for r in await conn.execute_fetchall(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        # Занятый слот держит индекс и в ожидании.
        try:
            await conn.execute(
                "INSERT INTO executor_orders (host, task_id, generation, "
                "executor_run_id, expires_at) VALUES ('k8', 8, 1, 10, '2030-01-01')"
            )
            raise AssertionError("слот k8 занят: UNIQUE обязан отказать")
        except aiosqlite.IntegrityError:
            await conn.rollback()
        await db_module._migrate(conn)  # повторный старт ничего не меняет
        again = await conn.execute_fetchall("SELECT state FROM executor_orders")
        assert [r[0] for r in again] == ["waiting_memory"]
    finally:
        await conn.close()
