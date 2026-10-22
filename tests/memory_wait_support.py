"""Общие помощники тестов «ждёт памяти» (#1972): запуск заказа, фаза хоста, сдвиг часов.

Часы хаба — ``datetime('now')`` в SQL, подменить их нельзя. «Прошло N секунд»
значит сдвинуть ВСЕ метки времени заказа и его кода назад на N: для логики сроков
это то же самое, что сдвинуть часы вперёд.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

from hub import cli, config, mcp_server
from hub.services import admin as admin_svc
from hub.services import host_redeem
from tests import test_executor_orders as eo
from tests.test_executor_orders import _launch, _orders, _rows, _task

HUB_URL = eo.HUB_URL


@pytest.fixture(autouse=True)
def clean_host_limiters():
    """Квоты фаз и выкупа живут в процессе: тест не наследует чужой расход."""
    from hub import executor_host_api

    executor_host_api._phase_limiter._buckets.clear()
    host_redeem.host_redeem_limiter._buckets.clear()
    yield
    executor_host_api._phase_limiter._buckets.clear()
    host_redeem.host_redeem_limiter._buckets.clear()


NEED, FREE = 6144, 3072  # 6 ГиБ нужно, 3 ГиБ свободно


async def launch_order(world, host: str = "k8", *, title: str = "ждёт памяти"):
    """Заказ хосту: (task_id, order_id)."""
    task_id = await _task(world, title)
    resp = await _launch(world, task_id, host)
    assert resp.status_code == 200, resp.text
    return task_id, int(resp.json()["order_id"])


async def deliver(world, host: str = "k8") -> dict:
    """Что делает хост: забирает заказ; возвращает его вид."""
    got = (await _orders(world, host)).json()["orders"]
    assert len(got) == 1, got
    return got[0]


async def phase(world, order_id: int, body: dict, host: str = "k8", auth=None):
    resp = await world.client.post(
        f"/api/executor-hosts/{host}/orders/{order_id}/phase",
        json=body,
        headers=auth if auth is not None else getattr(world, f"{host}_auth"),
    )
    world.seen.append(resp.text)
    return resp


async def wait_for_memory(
    world, order_id: int, host: str = "k8", need: int = NEED, free: int = FREE
):
    return await phase(
        world,
        order_id,
        {"phase": "waiting_memory", "need_mib": need, "free_mib": free},
        host,
    )


async def row(world, order_id: int) -> dict:
    await world.db.commit()
    return (
        await _rows(world.db, "SELECT * FROM executor_orders WHERE id = ?", (order_id,))
    )[0]


async def code_row(world, order_id: int) -> dict | None:
    await world.db.commit()
    rows = await _rows(
        world.db,
        "SELECT c.*, c.expires_at > datetime('now') AS alive FROM chat_pair_codes c "
        "JOIN executor_orders o ON o.pair_code_id = c.id WHERE o.id = ?",
        (order_id,),
    )
    return rows[0] if rows else None


async def alive(world, order_id: int) -> bool:
    """Срок заказа ещё не вышел (по часам базы)."""
    await world.db.commit()
    got = await _rows(
        world.db,
        "SELECT expires_at > datetime('now') AS alive FROM executor_orders WHERE id = ?",
        (order_id,),
    )
    return bool(got[0]["alive"])


async def age(world, order_id: int, seconds: int) -> None:
    """Сдвинуть часы вперёд на ``seconds``: все метки заказа и его кода — назад."""
    shift = f"-{int(seconds)} seconds"
    await world.db.execute(
        "UPDATE executor_orders SET created_at = datetime(created_at, ?), "
        "expires_at = datetime(expires_at, ?), "
        "delivered_at = datetime(delivered_at, ?), "
        "waiting_since = datetime(waiting_since, ?), "
        "last_seen_at = datetime(last_seen_at, ?) WHERE id = ?",
        (shift, shift, shift, shift, shift, order_id),
    )
    await world.db.execute(
        "UPDATE chat_pair_codes SET expires_at = datetime(expires_at, ?) "
        "WHERE id = (SELECT pair_code_id FROM executor_orders WHERE id = ?)",
        (shift, order_id),
    )
    await world.db.commit()


async def sweep(world) -> int:
    from hub.services import executor_orders as orders

    closed = await orders.sweep_executor_orders(world.db)
    await world.db.commit()
    return closed


async def feed(world, task_id: int, needle: str, kind: str = "status") -> list[dict]:
    await world.db.commit()
    return await _rows(
        world.db,
        "SELECT * FROM task_updates WHERE task_id = ? AND kind = ? AND content LIKE ?",
        (task_id, kind, f"%{needle}%"),
    )


async def events(world, kind: str, task_id: int) -> list[dict]:
    await world.db.commit()
    return await _rows(
        world.db,
        "SELECT * FROM events WHERE kind = ? AND task_id = ?",
        (kind, task_id),
    )


def hhmm(stamp: str) -> str:
    """Время заказа (UTC в базе) так, как его читает владелец."""
    moment = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    return moment.astimezone(ZoneInfo(config.OWNER_MINUTES_TIMEZONE)).strftime("%H:%M")


async def _cookie(world) -> dict:
    session = await admin_svc.create_browser_session(world.db, world.human_id)
    await world.db.commit()
    return {"Cookie": f"{config.HUB_COOKIE_NAME}={session}", "Origin": HUB_URL}


async def surfaces(world, task_id: int, capsys) -> dict:
    """Пять поверхностей одной карточки: REST, MCP (кратко и полно), CLI, страница, плашка."""
    from tests.test_mcp_server import _mcp_text

    rest = (
        await world.client.get(f"/api/tasks/{task_id}", headers=world.human_auth)
    ).json()
    out: dict = {"rest": rest.get("last_executor_order")}
    for full in (False, True):
        with (
            patch.object(mcp_server, "_api_post", AsyncMock(return_value={})),
            patch.object(mcp_server, "_api_get", AsyncMock(return_value=rest)),
        ):
            res = await mcp_server.hub_task_status(task_id, full=full)
        out["mcp_full" if full else "mcp_compact"] = _mcp_text(res)
    capsys.readouterr()
    with patch.object(cli, "_api", return_value=rest):
        assert cli.cmd_status(__import__("argparse").Namespace(task_id=task_id)) == 0
    captured = capsys.readouterr()
    out["cli_json"] = json.loads(captured.out).get("last_executor_order")
    out["cli_err"] = captured.err
    headers = await _cookie(world)
    page = await world.client.get(f"/tasks/{task_id}", headers=headers)
    assert page.status_code == 200, page.text
    out["page"] = page.text
    plaque = await world.client.get(
        f"/partials/launch-plaque/{task_id}", headers=headers
    )
    assert plaque.status_code == 200, plaque.text
    out["plaque"] = plaque.text
    return out


async def lock_then(world, holder, waiter):
    """Настоящее ожидание write-lock: держатель занимает его первым, ждущий стоит в очереди.

    ``holder(conn)`` и ``waiter(conn)`` — корутины-операции над своими соединениями.
    Ждущий стартует, пока держатель держит транзакцию (``BEGIN IMMEDIATE``), и
    доказанно не завершён; держатель делает своё и коммитит, затем ждущий
    проходит. Возвращает ``(исход держателя, исход ждущего)``; исключение — как
    значение.
    """
    from hub import db as hub_db

    a, b = await hub_db.connect(world.dsn), await hub_db.connect(world.dsn)
    try:
        await world.db.commit()
        await a.execute("BEGIN IMMEDIATE")
        queued = asyncio.create_task(waiter(b))
        await asyncio.sleep(0.3)
        assert not queued.done(), "ждущий обязан стоять в очереди на write-lock"
        try:
            first = await holder(a)
        except Exception as exc:  # noqa: BLE001 - исход держателя - значение
            first = exc
        await a.commit()
        second = (await asyncio.gather(queued, return_exceptions=True))[0]
    finally:
        await a.close()
        await b.close()
    await world.db.commit()
    return first, second
