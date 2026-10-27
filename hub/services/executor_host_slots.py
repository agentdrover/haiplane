"""#2107: сколько незакрытых заказов хост исполнителя держит одновременно.

Вынесено из ``executor_orders`` (бюджет размера): настройка ``executor_hosts.slots``
человеком и счёт незакрытых заказов хоста. Бронь сравнивает с лимитом сама
(``executor_orders._slot_busy``), гонку ловит триггер базы (миграция #2107).
"""

from __future__ import annotations

from typing import Any

import aiosqlite

from hub.db import fetchall, write_transaction
from hub.services import admin as admin_svc
from hub.services.executor_orders import (
    HOST_SLOTS_MAX,
    HOST_SLOTS_MIN,
    HostError,
    _host_view,
    get_host,
)


async def open_order_count(db: aiosqlite.Connection, name: str) -> int:
    rows = await fetchall(
        db,
        "SELECT COUNT(*) AS n FROM executor_orders WHERE host = ? AND state <> 'closed'",
        (name,),
    )
    return int(rows[0]["n"]) if rows else 0


async def set_host_slots(
    db: aiosqlite.Connection, name: str, slots: Any, *, by: int | None
) -> dict[str, Any]:
    """#2107: сколько незакрытых заказов хост держит одновременно (1..4).

    Снижение ниже текущей занятости открытые заказы не трогает: новые просто не
    бронируются, пока их не станет меньше ``slots``.
    """
    if type(slots) is not int or not HOST_SLOTS_MIN <= slots <= HOST_SLOTS_MAX:
        raise HostError(
            422,
            {
                "reason": "slots_invalid",
                "message": f"slots: целое {HOST_SLOTS_MIN}..{HOST_SLOTS_MAX}",
            },
        )
    async with write_transaction(db):
        host = await get_host(db, name)
        if host is None:
            raise HostError(404, f"хост {name!r} не зарегистрирован")
        await db.execute(
            "UPDATE executor_hosts SET slots = ? WHERE name = ?", (slots, name)
        )
        await admin_svc.write_audit(
            db,
            actor_id=by,
            action="executor_host_slots",
            target_type="executor_host",
            target_id=name,
            summary=f"executor host {name}: slots {host.get('slots', 1)} -> {slots}",
            commit=False,
        )
    updated = await get_host(db, name)
    if updated is None:
        raise HostError(500, f"хост {name!r} исчез при смене слотов")
    return {**_host_view(updated), "open_orders": await open_order_count(db, name)}
