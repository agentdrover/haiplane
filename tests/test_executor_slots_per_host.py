# ruff: noqa: F811 - фикстуры pytest импортированы из соседнего мира
"""#2107: несколько слотов исполнителя на хост (k8 = 2).

Хост держит до ``executor_hosts.slots`` незакрытых заказов одновременно; по
умолчанию 1, как было. Бронь называет причину отказа, триггер базы ловит гонку.
"""

from __future__ import annotations

import asyncio

import aiosqlite
import pytest

from hub import config
from hub.services import admin as admin_svc
from hub.services import executor_orders as orders
from tests.test_executor_orders import (  # noqa: F401 - фикстуры и помощники того же мира
    _claim_and_start,
    _clean_limiter,
    _code_of,
    _launch,
    _no_cloud,
    _orders,
    _order_row,
    _redeem,
    _rows,
    _submit,
    _task,
    keys,
    world,
)


async def _slots(world, host: str, slots, auth=None):
    return await world.client.post(
        f"/api/admin/executor-hosts/{host}/slots",
        json={"slots": slots},
        headers=auth or world.human_auth,
    )


async def _open(world, host: str = "k8") -> list[dict]:
    await world.db.commit()
    return await _rows(
        world.db,
        "SELECT * FROM executor_orders WHERE host = ? AND state <> 'closed' ORDER BY id",
        (host,),
    )


async def test_second_order_on_two_slot_host(world):
    """AC-1: slots=2 — второй заказ выдан вместе с первым, третий — «2/2»; та же задача — отказ."""
    assert (await _slots(world, "k8", 2)).status_code == 200
    first, second, third = [await _task(world, f"т{i}") for i in range(3)]
    assert (await _launch(world, first)).status_code == 200
    assert (await _launch(world, second)).status_code == 200
    got = (await _orders(world)).json()["orders"]
    assert sorted(o["task_id"] for o in got) == sorted([first, second])
    refused = await _launch(world, third)
    assert refused.status_code == 409
    assert "слот занят: k8 2/2" in refused.text
    assert f"#{first}" in refused.text and f"#{second}" in refused.text
    # повтор той же задачи отказан и при свободном слоте
    assert (await _slots(world, "k8", 3)).status_code == 200
    again = await _launch(world, first)
    assert again.status_code == 409 and "слот занят" not in again.text
    assert len(await _open(world)) == 2


async def test_default_single_slot_unchanged(world):
    """AC-2: хост без настройки (slots=1) — второй заказ отказан прежним текстом."""
    host = await orders.get_host(world.db, "k9")
    assert host["slots"] == 1
    a, b = await _task(world, "a"), await _task(world, "b")
    assert (await _launch(world, a, "k9")).status_code == 200
    refused = await _launch(world, b, "k9")
    assert refused.status_code == 409
    assert f"слот занят: k9 держит задачу #{a}" in refused.text


async def test_race_for_last_slot_books_once(world):
    """AC-3: slots=2, одна бронь, две одновременные брони разных задач — ровно одна проходит."""
    assert (await _slots(world, "k8", 2)).status_code == 200
    held, x, y = (
        await _task(world, "держит"),
        await _task(world, "x"),
        await _task(world, "y"),
    )
    assert (await _launch(world, held)).status_code == 200
    both = await asyncio.gather(_launch(world, x), _launch(world, y))
    assert sorted(r.status_code for r in both) == [200, 409], [r.text for r in both]
    loser = y if both[0].status_code == 200 else x
    assert "2/2" in next(r for r in both if r.status_code == 409).text
    assert len(await _open(world)) == 2
    assert (
        await _rows(
            world.db, "SELECT 1 FROM executor_orders WHERE task_id = ?", (loser,)
        )
        == []
    )
    assert (
        await _rows(world.db, "SELECT 1 FROM executor_runs WHERE task_id = ?", (loser,))
        == []
    )
    # гарантия базы: третий незакрытый заказ k8 не вставить и в обход брони
    run_id = (await _open(world))[0]["executor_run_id"]
    with pytest.raises(aiosqlite.IntegrityError):
        await world.db.execute(
            "INSERT INTO executor_orders (host, task_id, generation, executor_run_id, "
            "expires_at, state) VALUES ('k8', ?, 1, ?, datetime('now', '+5 minutes'), 'pending')",
            (loser, run_id),
        )
    await world.db.rollback()


async def test_two_orders_live_independently(world):
    """AC-4: два заказа: свой выкуп, heartbeat только своему, закрытие одного не трогает другой."""
    assert (await _slots(world, "k8", 2)).status_code == 200
    a, b = await _task(world, "A"), await _task(world, "B")
    assert (await _launch(world, a)).status_code == 200
    assert (await _launch(world, b)).status_code == 200
    got = {o["task_id"]: o for o in (await _orders(world)).json()["orders"]}
    before_b = (await _order_row(world, got[b]["id"]))["expires_at"]
    beat = await world.client.post(
        f"/api/executor-hosts/k8/orders/{got[a]['id']}/phase",
        json={"phase": "waiting_memory", "need_mib": 5734, "free_mib": 1000},
        headers=world.k8_auth,
    )
    assert beat.status_code == 200, beat.text
    assert (await _order_row(world, got[a]["id"]))["state"] == "waiting_memory"
    assert (await _order_row(world, got[b]["id"]))["expires_at"] == before_b
    sessions = {}
    for task_id in (a, b):
        redeemed = await _redeem(world, _code_of(world, got[task_id]))
        assert redeemed.status_code == 200, redeemed.text
        sessions[task_id] = {"Authorization": f"Bearer {redeemed.json()['token']}"}
        await _claim_and_start(
            world, task_id, sessions[task_id]
        )  # оба исполнителя работают
    await _submit(world, a)
    await orders.settle(world.db, host="k8")
    await world.db.commit()
    assert (await _order_row(world, got[a]["id"]))["state"] == "closed"
    assert (await _order_row(world, got[b]["id"]))["state"] == "redeemed"
    assert (
        await world.client.get("/api/whoami", headers=sessions[b])
    ).status_code == 200
    task_b = await world.client.get(f"/api/tasks/{b}", headers=sessions[b])
    assert task_b.status_code == 200 and task_b.json()["status"] == "running"
    live = await _rows(
        world.db,
        "SELECT 1 FROM executor_slots WHERE task_id = ? AND released_at IS NULL",
        (b,),
    )
    assert live, "захват B не должен сниматься сдачей A"
    row_b = (
        await _rows(
            world.db,
            "SELECT claimed_by, claim_session_id, executor_run_id FROM tasks WHERE id = ?",
            (b,),
        )
    )[0]
    assert row_b["claimed_by"] and row_b["claim_session_id"] == f"slot-{b}"
    assert row_b["executor_run_id"] == got[b]["executor_run_id"]
    acted = await world.client.post(  # B ещё может действовать как держатель задачи
        f"/api/tasks/{b}/updates",
        json={
            "agent": config.CHAT_PAIR_AGENT,
            "kind": "progress",
            "content": "B работает после сдачи A",
        },
        headers=sessions[b],
    )
    assert acted.status_code in (200, 201), acted.text
    assert len(await _open(world)) == 1
    off = await world.client.post(
        "/api/admin/executor-hosts/k8/disable", headers=world.human_auth
    )
    assert off.status_code == 200, off.text
    assert await _open(world) == []


async def test_set_slots_and_capacity_view(world):
    """AC-5: set-slots — человек 200, агент 403, вне 1..4 — 422; N/slots по незакрытым; снижение ниже занятости."""
    assert (await _slots(world, "k8", 2, auth=world.agent_auth)).status_code == 403
    for bad in (0, 5, "2", True):
        assert (await _slots(world, "k8", bad)).status_code == 422, bad
    ok = await _slots(world, "k8", 2)
    assert (
        ok.status_code == 200
        and ok.json()["slots"] == 2
        and ok.json()["open_orders"] == 0
    )
    a, b, c = await _task(world, "a"), await _task(world, "b"), await _task(world, "c")
    assert (await _launch(world, a)).status_code == 200  # pending: ещё не выдан хосту
    view = (
        await world.client.get("/api/executor-slots", headers=world.human_auth)
    ).json()
    k8 = next(h for h in view["hosts"] if h["name"] == "k8")
    assert (k8["open"], k8["slots"], k8["task_ids"]) == (1, 2, [a])
    assert (await _launch(world, b)).status_code == 200
    lowered = await _slots(world, "k8", 1)
    assert lowered.status_code == 200 and lowered.json()["open_orders"] == 2
    assert len(await _open(world)) == 2  # открытые заказы живы
    refused = await _launch(world, c)
    assert refused.status_code == 409 and "2/1" in refused.text
    from hub.services.executor_slots import format_occupancy

    full = (
        await world.client.get("/api/executor-slots", headers=world.human_auth)
    ).json()
    text = format_occupancy(full)
    assert any(line.startswith(f"хост k8: 2/1 — #{a}, #{b}") for line in text), text
    assert (
        "Слоты свободны, задач в работе нет." not in text
    )  # брони без захвата — не «свободно»
    # карточка: кнопка называет занятость хоста
    session = await admin_svc.create_browser_session(world.db, world.human_id)
    await world.db.commit()
    page = await world.client.get(
        f"/tasks/{c}", headers={"Cookie": f"{config.HUB_COOKIE_NAME}={session}"}
    )
    assert page.status_code == 200 and "Запустить на k8 · 2/1" in page.text
