"""Общая подготовка тестов заказов ролей k8 (#1973): мир хостов и ход событий.

Мир (проект, человек, хосты k8 и k9 с настоящими ключами) берётся из тестов
заказов исполнителя, чтобы заказ роли проверялся рядом с теми же хостами.
"""

from __future__ import annotations

from typing import Any

from hub import repository as repo
from hub.services import role_orders, watch_notices
from tests import test_executor_orders as _eo

world = _eo.world
keys = _eo.keys
_clean_limiter = _eo._clean_limiter
_no_cloud = _eo._no_cloud
task_in_project = _eo._task


async def rows(db, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    cursor = await db.execute(sql, params)
    return [dict(r) for r in await cursor.fetchall()]


async def enter_needs_decision(world, task_id: int) -> int:
    """Задача входит в needs_decision так же, как в хабе: статус и событие вместе."""
    await repo.update_task(world.db, task_id, status="needs_decision")
    event_id = await watch_notices.emit_needs_decision(world.db, task_id=task_id)
    await world.db.commit()
    return event_id


async def leave_needs_decision(world, task_id: int) -> None:
    await repo.update_task(world.db, task_id, status="running")
    await world.db.commit()


async def admit(world, host: str = "k8", role: str = "roadmap") -> None:
    await role_orders.admit_host(world.db, host, role, world.pid)
    await world.db.commit()


async def sweep(world) -> int:
    """Один проход источника; число созданных заказов."""
    created = await role_orders.sweep_sources(world.db)
    await world.db.commit()
    return created


async def order_rows(world) -> list[dict[str, Any]]:
    return await rows(world.db, "SELECT * FROM role_orders ORDER BY id")


async def order_row(world, order_id: int) -> dict[str, Any]:
    found = await rows(world.db, "SELECT * FROM role_orders WHERE id = ?", (order_id,))
    return found[0]


async def poll(world, host: str = "k8", auth=None, wait: int = 0):
    return await world.client.get(
        f"/api/executor-hosts/{host}/role-orders?wait={wait}",
        headers=auth if auth is not None else getattr(world, f"{host}_auth"),
    )


async def started(world, host: str, order_id: int, lease_id: str, auth=None):
    return await world.client.post(
        f"/api/executor-hosts/{host}/role-orders/{order_id}/started",
        json={"lease_id": lease_id},
        headers=auth if auth is not None else getattr(world, f"{host}_auth"),
    )


async def report(
    world,
    host: str,
    order_id: int,
    lease_id: str,
    outcome: str = "done",
    note: str = "готово",
    auth=None,
):
    return await world.client.post(
        f"/api/executor-hosts/{host}/role-orders/{order_id}/report",
        json={"lease_id": lease_id, "outcome": outcome, "note": note},
        headers=auth if auth is not None else getattr(world, f"{host}_auth"),
    )


async def leased(world, host: str = "k8") -> dict[str, Any]:
    """Опрос хоста, который обязан выдать ровно один заказ."""
    got = await poll(world, host)
    assert got.status_code == 200, got.text
    orders = got.json()["orders"]
    assert len(orders) == 1, got.text
    return orders[0]


async def age(world, order_id: int, **columns: str) -> None:
    """Сдвинуть время строки: колонка = SQL-выражение времени, например «-1 hour»."""
    for column, shift in columns.items():
        await world.db.execute(
            f"UPDATE role_orders SET {column} = datetime('now', ?) WHERE id = ?",  # nosec B608 - имя колонки из тела теста
            (shift, order_id),
        )
    await world.db.commit()
