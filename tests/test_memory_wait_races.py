"""«Ждёт памяти»: граница 6 ч, гонки heartbeat/выкупа/sweep, прежние читатели (#1972).

Гонки настоящие: два-три соединения к одному файлу базы, ждущий действительно
стоит в очереди на write-lock (доказано незавершённой задачей), оба порядка.
"""

from __future__ import annotations

import asyncio

from hub import db as hub_db
from hub.services import executor_dispatch, executor_orders as orders
from hub.services import host_redeem, launch_feedback, run_claim
from tests import test_executor_orders as _eo
from tests.memory_wait_support import (
    clean_host_limiters,  # noqa: F401 - autouse-фикстура
    FREE,
    NEED,
    age,
    alive,
    code_row,
    deliver,
    events,
    feed,
    launch_order,
    lock_then,
    row,
    sweep,
    wait_for_memory,
)
from tests.test_executor_orders import _code_of, _launch

world = _eo.world
keys = _eo.keys
_clean_limiter = _eo._clean_limiter
_no_cloud = _eo._no_cloud

TIMEOUT_NEEDLE = "не дождался памяти"
TIMEOUT_EVENT = "executor_memory_wait_timeout"


async def _waiting(world, host: str = "k8", title: str = "ждёт"):
    """Ждущий заказ с живым heartbeat: (task_id, order_id, код хоста)."""
    task_id, order_id = await launch_order(world, host, title=title)
    code = _code_of(world, await deliver(world, host), host)
    assert (await wait_for_memory(world, order_id, host)).status_code == 200
    return task_id, order_id, code


async def _at_limit(world, host: str = "k8", title: str = "на границе"):
    """Заказ ждёт ровно 6 ч, а срок заказа и кода живой (heartbeat идёт)."""
    task_id, order_id, code = await _waiting(world, host, title)
    await world.db.execute(
        "UPDATE executor_orders SET waiting_since = datetime('now', '-6 hours'), "
        "last_seen_at = datetime('now', '-1 minutes'), "
        "expires_at = datetime('now', '+9 minutes') WHERE id = ?",
        (order_id,),
    )
    await world.db.execute(
        "UPDATE chat_pair_codes SET expires_at = datetime('now', '+9 minutes') "
        "WHERE id = (SELECT pair_code_id FROM executor_orders WHERE id = ?)",
        (order_id,),
    )
    await world.db.commit()
    assert await alive(world, order_id)
    return task_id, order_id, code


def _heartbeat(order_id: int, host: str = "k8"):
    async def run(conn):
        return await launch_feedback.record_phase(
            conn,
            host,
            order_id,
            "waiting_memory",
            need_mib=NEED,
            free_mib=FREE,
        )

    return run


def _redeem(order_id: int, code: str, host: str = "k8"):
    async def run(conn):
        return await host_redeem.redeem_for_host(conn, host, order_id, code)

    return run


async def _sweep(conn):
    return await orders.sweep_executor_orders(conn)


def _refused(outcome, status: int = 409) -> bool:
    return isinstance(outcome, orders.HostError) and outcome.status == status


async def _closed_once(world, task_id: int, order_id: int, reason: str):
    got = await row(world, order_id)
    assert got["state"] == "closed" and got["close_reason"] == reason, got
    assert await code_row(world, order_id) is None  # код сожжён
    return got


async def _timeout_counts(world, task_id: int) -> tuple[int, int, int]:
    """(запись ленты о закрытии, сигнал владельцу alert, событие) - каждого по одному."""
    return (
        len(await feed(world, task_id, TIMEOUT_NEEDLE)),
        len(await feed(world, task_id, "Ожидание памяти на", kind="alert")),
        len(await events(world, TIMEOUT_EVENT, task_id)),
    )


async def test_the_six_hour_limit_closes_once(world):
    """AC-3: на границе 6 ч heartbeat и выкуп отклонены, заказ закрыт один раз, одна запись и одно событие."""
    # Порядок 1: sweep держит write-lock, heartbeat стоит в очереди и отклонён.
    task_id, order_id, code = await _at_limit(world, "k8", "порядок 1")
    alone = await wait_for_memory(world, order_id)
    assert alone.status_code == 409, alone.text  # граница: отказ и без гонки
    assert (await row(world, order_id))["state"] == "waiting_memory"
    first, second = await lock_then(world, _sweep, _heartbeat(order_id))
    assert first == 1 and _refused(second), (first, second)
    await _closed_once(world, task_id, order_id, "memory_wait_timeout")
    assert await _timeout_counts(world, task_id) == (1, 1, 1)
    assert await sweep(world) == 0  # повторный sweep: ничего нового
    assert await _timeout_counts(world, task_id) == (1, 1, 1)

    # Порядок 2: heartbeat держит lock первым (отказ), sweep ждёт и закрывает один раз.
    task_id, order_id, code = await _at_limit(world, "k8", "порядок 2")
    first, second = await lock_then(world, _heartbeat(order_id), _sweep)
    assert _refused(first) and second == 1, (first, second)
    await _closed_once(world, task_id, order_id, "memory_wait_timeout")
    assert await _timeout_counts(world, task_id) == (1, 1, 1)

    # Выкуп на границе: отклонён в обоих порядках, код не потрачен до закрытия.
    task_id, order_id, code = await _at_limit(world, "k8", "выкуп первым")
    first, second = await lock_then(world, _redeem(order_id, code), _sweep)
    assert _refused(first), first
    assert second == 1
    await _closed_once(world, task_id, order_id, "memory_wait_timeout")
    assert await _timeout_counts(world, task_id) == (1, 1, 1)

    task_id, order_id, code = await _at_limit(world, "k8", "sweep первым")
    first, second = await lock_then(world, _sweep, _redeem(order_id, code))
    assert first == 1 and _refused(second), (first, second)
    await _closed_once(world, task_id, order_id, "memory_wait_timeout")
    assert await _timeout_counts(world, task_id) == (1, 1, 1)

    # Все три сразу на трёх соединениях и два sweep подряд: исход один.
    task_id, order_id, code = await _at_limit(world, "k8", "все сразу")
    conns = [await hub_db.connect(world.dsn) for _ in range(4)]
    try:
        results = await asyncio.gather(
            _sweep(conns[0]),
            _heartbeat(order_id)(conns[1]),
            _redeem(order_id, code)(conns[2]),
            _sweep(conns[3]),
            return_exceptions=True,
        )
    finally:
        for conn in conns:
            await conn.close()
    assert sorted(r for r in results if isinstance(r, int)) == [0, 1], results
    assert [r for r in results if isinstance(r, BaseException)] and all(
        _refused(r) for r in results if isinstance(r, BaseException)
    ), results
    await _closed_once(world, task_id, order_id, "memory_wait_timeout")
    assert await _timeout_counts(world, task_id) == (1, 1, 1)
    assert await sweep(world) == 0
    assert await _timeout_counts(world, task_id) == (1, 1, 1)

    # Предел и обычный срок вышли вместе (heartbeat давно нет): причина - предел.
    task_id, order_id, code = await _waiting(world, "k8", "оба срока")
    await world.db.execute(
        "UPDATE executor_orders SET waiting_since = datetime('now', '-7 hours'), "
        "expires_at = datetime('now', '-1 minutes') WHERE id = ?",
        (order_id,),
    )
    await world.db.commit()
    assert await sweep(world) == 1
    await _closed_once(world, task_id, order_id, "memory_wait_timeout")
    assert await _timeout_counts(world, task_id) == (1, 1, 1)


async def test_heartbeat_and_sweep_race_in_both_orders(world):
    """Heartbeat × sweep без границы: живой продлевается, просроченный не оживает — в обоих порядках."""
    # Живой заказ: heartbeat первым — sweep ничего не закрывает; sweep первым — heartbeat проходит.
    _t, live, _c = await _waiting(world, "k8", "живой 1")
    first, second = await lock_then(world, _heartbeat(live), _sweep)
    assert first["state"] == "waiting_memory" and second == 0, (first, second)
    assert (await row(world, live))["state"] == "waiting_memory"
    first, second = await lock_then(world, _sweep, _heartbeat(live))
    assert first == 0 and second["state"] == "waiting_memory", (first, second)

    # Просроченный (heartbeat пропал > 10 мин): оба порядка дают закрытие expired, а не оживление.
    for holder_is_sweep in (True, False):
        _t, order_id, _c = await _waiting(world, "k9", f"просрочен {holder_is_sweep}")
        await age(world, order_id, 11 * 60)
        if holder_is_sweep:
            first, second = await lock_then(world, _sweep, _heartbeat(order_id, "k9"))
            assert first == 1 and _refused(second), (first, second)
        else:
            first, second = await lock_then(world, _heartbeat(order_id, "k9"), _sweep)
            assert _refused(first) and second == 1, (first, second)
        got = await row(world, order_id)
        assert got["state"] == "closed" and got["close_reason"] == "expired", got


async def test_redeem_and_heartbeat_race_in_both_orders(world):
    """Выкуп × heartbeat: ровно один исход; ожидание после выкупа невозможно, выкуп после heartbeat проходит."""
    _t, order_id, code = await _waiting(world, "k8", "выкуп и heartbeat 1")
    first, second = await lock_then(
        world, _redeem(order_id, code), _heartbeat(order_id)
    )
    assert isinstance(first, dict) and first["token"], first
    assert _refused(second), second
    assert (await row(world, order_id))["state"] == "redeemed"
    # закрыть, чтобы освободить слот
    await orders.close_order(
        world.db, await row(world, order_id), "run_finished", "finished"
    )
    await world.db.commit()

    _t, order_id, code = await _waiting(world, "k8", "выкуп и heartbeat 2")
    first, second = await lock_then(
        world, _heartbeat(order_id), _redeem(order_id, code)
    )
    assert first["state"] == "waiting_memory", first
    assert isinstance(second, dict) and second["token"], second
    assert (await row(world, order_id))["state"] == "redeemed"


async def test_waiting_does_not_change_existing_readers(world):
    """AC-5: при заказе в waiting_memory ручной run_claim, _task_busy, read_order_outcome, task_budget — как до."""
    task_id, order_id = await launch_order(world, "k8", title="читатели")
    await deliver(world)
    run_id = (await row(world, order_id))["executor_run_id"]
    manual = {"executor_run_id": None, "bound_task_id": task_id}

    async def probe() -> dict:
        return {
            "busy": await orders._task_busy(world.db, task_id),
            "slot_busy": await orders._slot_busy(world.db, "k8"),
            "outcome": await orders.read_order_outcome(world.db, "k8", order_id),
            "budget": await executor_dispatch.task_budget(world.db, task_id),
            "manual_redeem": await run_claim.redeem_conflict(world.db, manual),
            "about_to_claim": await run_claim._slot_is_about_to_claim(
                world.db, int(run_id), ""
            ),
        }

    before = await probe()
    assert before["busy"] and before["slot_busy"] and before["manual_redeem"]
    assert before["about_to_claim"] is True

    assert (await wait_for_memory(world, order_id)).status_code == 200
    after = await probe()
    state = "waiting_memory"
    assert after["outcome"] == {**before["outcome"], "state": state}
    assert after["budget"] == before["budget"]
    assert after["manual_redeem"] == before["manual_redeem"]
    assert after["about_to_claim"] is True
    # Задача и слот заняты: тот же отказ, с новым словом состояния в тексте.
    assert after["busy"] == before["busy"].replace("delivered", state)
    assert after["slot_busy"] == before["slot_busy"].replace("delivered", state)
    # Второй исполнитель не выдаётся: ни та же задача на другой хост, ни другая задача на слот.
    again = await _launch(world, task_id, "k9")
    assert again.status_code != 200, again.text
    other = await _eo._task(world, "другая задача")
    busy_slot = await _launch(world, other, "k8")
    assert busy_slot.status_code != 200, busy_slot.text
    assert await alive(world, order_id)
