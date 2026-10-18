"""Хост читает итог своего заказа ключом слота (#1831).

После сдачи хаб отзывает сессии прогона (#1783), и launcher k8 не может
проверить сдачу сессией агента. Итог заказа читается ключом хоста.
"""

from __future__ import annotations

from tests import test_executor_orders as _eo
from tests.test_executor_orders import (
    _launch_redeem,
    _order_row,
    _submit,
    _task,
)

world = _eo.world
keys = _eo.keys
_clean_limiter = _eo._clean_limiter
_no_cloud = _eo._no_cloud


async def _read(world, host: str, order_id, auth=None):
    return await world.client.get(
        f"/api/executor-hosts/{host}/orders/{order_id}",
        headers=auth if auth is not None else getattr(world, f"{host}_auth"),
    )


async def test_host_reads_own_order_outcome_after_submit(world):
    """AC-1: после сдачи и отзыва сессий хост видит итог заказа своим ключом."""
    from hub import repository as repo

    task_id = await _task(world, "сдача")
    lived = await _launch_redeem(world, task_id)
    order_id = lived["order"]["id"]
    await _eo._claim_and_start(world, task_id, lived["session"])
    await repo.update_task(world.db, task_id, submission_sha="a" * 40, pr_number=77)
    await world.db.commit()
    await _submit(world, task_id)
    done = await world.client.post(
        f"/api/executor-hosts/k8/orders/{order_id}/report",
        json={"outcome": "submitted", "sha": "a" * 40},
        headers=world.k8_auth,
    )
    assert done.status_code == 200, done.text
    from hub.services import executor_orders as orders

    await orders.close_order(
        world.db, await _order_row(world, order_id), "submitted", "finished"
    )
    await world.db.commit()
    # Сессия прогона отозвана — старый путь проверки сдачи даёт 401.
    await world.db.execute(
        "UPDATE chat_pair_sessions SET revoked_at = datetime('now') "
        "WHERE executor_run_id = ?",
        (lived["order"]["executor_run_id"],),
    )
    await world.db.commit()
    assert (
        await world.client.get(f"/api/tasks/{task_id}", headers=lived["session"])
    ).status_code == 401

    got = await _read(world, "k8", order_id)
    assert got.status_code == 200, got.text
    body = got.json()
    row = await _order_row(world, order_id)
    assert body["id"] == order_id and body["state"] == row["state"]
    assert body["close_reason"] == row["close_reason"]
    assert body["task_id"] == task_id and body["task_status"] == "review"
    assert body["submission_generation"] == 1
    assert body["submission_sha"] == "a" * 40 and body["pr_number"] == 77
    assert body["branch"] == (await repo.get_task(world.db, task_id))["branch"] != ""
    assert body["close_reason"] == "submitted" and body["state"] == "closed"
    text = got.text
    for secret in ("code_ciphertext", "order_text", "code_hash", lived["code"]):
        assert secret not in text
    assert lived["session"]["Authorization"].split()[1] not in text


async def test_order_read_scoped_to_own_host(world, monkeypatch):
    """AC-2: чужой и несуществующий заказ — одинаковый 404; не-хост и без ключа — отказ; квота."""
    from hub import executor_host_api

    task_id = await _task(world, "своя")
    other = await _task(world, "чужая")
    mine = (await _eo._launch(world, task_id, "k8")).json()["order_id"]
    theirs = (await _eo._launch(world, other, "k9")).json()["order_id"]

    own = await _read(world, "k8", mine)
    assert own.status_code == 200
    foreign = await _read(world, "k8", theirs)  # заказ k9 по пути k8
    missing = await _read(world, "k8", 999999)
    assert foreign.status_code == 404 and missing.status_code == 404
    assert foreign.json() == missing.json()
    # Путь чужого хоста ключом k8 — отказ роли, как у остальных маршрутов.
    assert (await _read(world, "k9", theirs, world.k8_auth)).status_code == 403
    assert (await _read(world, "k8", mine, {})).status_code in (401, 403)
    assert (await _read(world, "k8", mine, world.agent_auth)).status_code == 403
    assert (await _read(world, "k8", mine, world.human_auth)).status_code == 403

    # Остальные права slot_runner прежние.
    for method, path in (("GET", "/api/tasks"), ("POST", "/api/tasks")):
        resp = await world.client.request(method, path, headers=world.k8_auth)
        assert resp.status_code == 403
    ack = await world.client.post(
        f"/api/executor-hosts/k8/orders/{mine}/ack",
        json={"result": "redeemed"},
        headers=world.k8_auth,
    )
    assert ack.status_code == 200

    # Квота на хост: сверх лимита 429, и чтение по-прежнему только чтение.
    executor_host_api._read_limiter._buckets.clear()
    monkeypatch.setattr(executor_host_api, "ORDER_READ_MAX", 3)
    codes = [(await _read(world, "k8", mine)).status_code for _ in range(5)]
    assert 429 in codes and codes[0] == 200


async def test_order_read_hides_secrets_stale_runs_and_overflow(world):
    """Codex P2/P2/P3: секрет в ветке, сдача следующего заказа, id вне диапазона SQLite."""
    from hub import repository as repo

    task_id = await _task(world, "старый заказ")
    first = (await _eo._launch(world, task_id, "k8")).json()["order_id"]
    code = _eo._code_of(
        world, (await _eo._orders(world, "k8")).json()["orders"][0], "k8"
    )
    # Секрет в ветке (слаг задаёт вызывающий pair-start): в ответ не попадает.
    await repo.update_task(world.db, task_id, branch=f"task-1/{code}")
    await world.db.commit()
    got = await _read(world, "k8", first)
    assert got.status_code == 200 and got.json()["branch"] == ""
    assert code not in got.text and code.removeprefix("AH-") not in got.text
    # Заказ закрыт, задача ушла на другой хост: чужая сдача старому заказу не видна.
    await repo.update_task(world.db, task_id, branch=f"task-{task_id}/work")
    await world.db.commit()
    lost = await world.client.post(
        f"/api/executor-hosts/k8/orders/{first}/ack",
        json={"result": "redeem_failed"},
        headers=world.k8_auth,
    )
    assert lost.status_code == 200
    assert (await _eo._launch(world, task_id, "k9")).status_code == 200
    await repo.update_task(
        world.db, task_id, submission_sha="b" * 40, pr_number=9, status="review"
    )
    await world.db.commit()
    old = (await _read(world, "k8", first)).json()
    assert old["superseded"] is True and old["state"] == "closed"
    assert old["submission_sha"] == "" and old["pr_number"] is None
    assert old["task_status"] is None and old["branch"] == ""
    # id вне диапазона SQLite: тот же 404, а не 500.
    big = await _read(world, "k8", 2**63)
    assert big.status_code == 404
    assert big.json() == (await _read(world, "k8", 999999)).json()


async def test_order_read_snapshot_is_atomic(world, monkeypatch):
    """CR-1: заказ, «есть ли новее» и задача читаются ОДНИМ запросом — одним снимком.

    Любое отдельное чтение задачи или заказов между ними оставляет окно, в
    которое успевает встать новый заказ; поэтому второе чтение здесь — отказ.
    """
    from hub.services import executor_orders as orders

    task_id = await _task(world, "снимок")
    first = (await _eo._launch(world, task_id, "k8")).json()["order_id"]

    async def second_read(*_a, **_k):
        raise AssertionError("итог читает задачу отдельным запросом: нет общего снимка")

    monkeypatch.setattr(orders.repo, "get_task", second_read)
    seen: list[str] = []
    real = orders.fetchall

    async def counting(db, sql, params=()):
        seen.append(sql)
        return await real(db, sql, params)

    monkeypatch.setattr(orders, "fetchall", counting)
    body = await _read(world, "k8", first)
    assert body.status_code == 200, body.text
    reads = [
        q for q in seen if "JOIN tasks" in q or "FROM tasks" in q or "n.task_id" in q
    ]
    assert len(reads) == 1, reads  # заказ, newer и задача — в одном запросе


async def test_order_read_binds_submission_to_its_own_run(world):
    """CR-2: сдача другого прогона (облако, ручной pair) не выдаётся за итог заказа."""
    from hub import repository as repo

    task_id = await _task(world, "чужая пересдача")
    lived = await _launch_redeem(world, task_id)
    order_id = lived["order"]["id"]
    # Прогон ещё не сдавал: чужих submission_* нет, вопрос «сдано ли» — нет.
    await repo.update_task(world.db, task_id, submission_sha="c" * 40, pr_number=5)
    await world.db.commit()
    waiting = (await _read(world, "k8", order_id)).json()
    assert waiting["superseded"] is False and waiting["submitted"] is False
    assert waiting["submission_sha"] == "" and waiting["pr_number"] is None
    # Свой прогон сдал: поколение заказа совпало.
    await _submit(world, task_id, 1)
    mine = (await _read(world, "k8", order_id)).json()
    assert mine["submitted"] is True and mine["superseded"] is False
    assert mine["submission_sha"] == "c" * 40 and mine["submission_generation"] == 1
    # Пересдача облаком или вручную (поколение 2): старый заказ — superseded без полей.
    await repo.update_task(
        world.db, task_id, submission_generation=2, submission_sha="d" * 40, pr_number=6
    )
    await world.db.commit()
    other = (await _read(world, "k8", order_id)).json()
    assert other["superseded"] is True and other["submitted"] is False
    for field in ("submission_sha", "branch"):
        assert other[field] == ""
    assert other["pr_number"] is None and other["task_status"] is None
    assert other["submission_generation"] is None
