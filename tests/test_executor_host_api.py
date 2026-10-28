"""Хост читает итог своего заказа ключом слота (#1831).

После сдачи хаб отзывает сессии прогона (#1783), и launcher k8 не может
проверить сдачу сессией агента. Итог заказа читается ключом хоста.
"""

from __future__ import annotations

import json

from hub import repository as repo

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


async def test_same_generation_foreign_submit_is_not_the_orders_result(world):
    """Codex P1: сдача облаком/ручным pair того же поколения не засчитывается заказу k8."""
    from hub import repository as repo

    # Заказ закрыт без сдачи; затем чужой прогон сдаёт задачу — поколение 1, как у заказа.
    task_id = await _task(world, "чужая сдача того же поколения")
    first = (await _eo._launch(world, task_id, "k8")).json()["order_id"]
    closed = await world.client.post(
        f"/api/executor-hosts/k8/orders/{first}/ack",
        json={"result": "redeem_failed"},
        headers=world.k8_auth,
    )
    assert closed.status_code == 200
    await repo.update_task(world.db, task_id, submission_sha="e" * 40, pr_number=3)
    await _submit(world, task_id, 1, holder=False)
    body = (await _read(world, "k8", first)).json()
    assert body["submitted"] is False and body["superseded"] is True
    assert body["submission_sha"] == "" and body["pr_number"] is None

    # То же при ещё не закрытом заказе: держателя задачи нет — сдача не его.
    other = await _task(world, "незакрытый заказ")
    lived = await _launch_redeem(world, other, "k9")
    await repo.update_task(world.db, other, submission_sha="f" * 40, pr_number=4)
    await _submit(world, other, 1, holder=False)
    got = await world.client.get(
        f"/api/executor-hosts/k9/orders/{lived['order']['id']}", headers=world.k9_auth
    )
    assert got.json()["submitted"] is False and got.json()["submission_sha"] == ""
    # Проход поллера не закрывает такой заказ причиной «submitted» (иначе она
    # стала бы постоянным доказательством чужой сдачи).
    from hub.services import executor_orders as orders

    await orders.sweep_executor_orders(world.db)
    again = await world.client.get(
        f"/api/executor-hosts/k9/orders/{lived['order']['id']}", headers=world.k9_auth
    )
    assert again.json()["close_reason"] != "submitted"
    assert again.json()["submitted"] is False and again.json()["superseded"] is True

    # Своя сдача (держатель — прогон заказа) засчитывается.
    mine = await _task(world, "своя")
    own = await _launch_redeem(world, mine)
    await repo.update_task(world.db, mine, submission_sha="9" * 40, pr_number=8)
    await _submit(world, mine, 1)
    ok = (await _read(world, "k8", own["order"]["id"])).json()
    assert ok["submitted"] is True and ok["submission_sha"] == "9" * 40


async def test_order_read_branch_hides_any_hub_code(world):
    """Codex P2: слаг ветки с кодом ручного pair (без префикса AH-) в ответ не попадает."""
    from hub import repository as repo
    from hub.services import chat_pair as cp

    task_id = await _task(world, "ветка с чужим кодом")
    lived = await _launch_redeem(world, task_id)
    acting = await cp.get_acting_agent(world.db)
    manual_task = await _task(world, "ручной pair")
    manual, _ = await cp.issue_code(
        world.db, int(acting["id"]), kind="implementer", bound_task_id=manual_task
    )
    await world.db.commit()
    bare = manual.removeprefix("AH-")
    await repo.update_task(world.db, task_id, branch=f"task-{task_id}/{bare}")
    await _submit(world, task_id, 1)
    got = await _read(world, "k8", lived["order"]["id"])
    assert got.json()["submitted"] is True and got.json()["branch"] == ""
    assert bare not in got.text and manual not in got.text


async def test_submitted_order_is_silent_after_the_task_moves_on(world, monkeypatch):
    """Codex P2: после возврата в работу сданный заказ не склеивает SHA прежней сдачи с новой веткой."""
    from hub import config
    from hub import repository as repo

    task_id = await _task(world, "возврат в работу")
    lived = await _launch_redeem(world, task_id)
    await repo.update_task(world.db, task_id, submission_sha="1" * 40, pr_number=2)
    await _submit(world, task_id, 1)
    from hub.services import executor_orders as orders

    await orders.sweep_executor_orders(world.db)  # сдача закрыла заказ
    await world.db.commit()
    done = (await _read(world, "k8", lived["order"]["id"])).json()
    assert done["submitted"] is True and done["submission_sha"] == "1" * 40
    # Задача ушла в новую работу с другой веткой, поколение прежнее.
    await repo.update_task(
        world.db, task_id, status="running", branch=f"task-{task_id}/new-run"
    )
    await world.db.commit()
    moved = (await _read(world, "k8", lived["order"]["id"])).json()
    assert moved["superseded"] is True and moved["submitted"] is False
    assert moved["submission_sha"] == "" and moved["branch"] == ""
    assert moved["task_status"] is None

    # Токен произвольного вида из окружения хаба в слаге ветки не возвращается.
    monkeypatch.setattr(
        config, "HUB_TOKENS", {"plain-secret-xyz": config.TokenIdentity("b", "agent")}
    )
    await repo.update_task(
        world.db, task_id, status="review", branch=f"task-{task_id}/plain-secret-xyz"
    )
    await world.db.commit()
    leak = await _read(world, "k8", lived["order"]["id"])
    assert "plain-secret-xyz" not in leak.text


async def _project(world, slug: str) -> int:
    pid = await repo.create_project(world.db, slug=slug, name=slug)
    await repo.update_project(
        world.db,
        pid,
        gate_policy=json.dumps({"executor_launch": "manual"}),
        forge="github",
        repo="agentdrover/haiplane",
    )
    await world.db.commit()
    return pid


async def _epic_task(world, epic_project: int, title: str) -> tuple[int, int]:
    """Лист в эпике, привязанном к проекту (проект наследуется от эпика)."""
    kw = dict(
        description="",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status="open",
        auto_review=True,
        priority="medium",
    )
    epic = await repo.create_task(
        world.db, title=f"эпик {title}", task_type="epic", parent_id=None, **kw
    )
    await repo.update_task(world.db, epic, project_id=epic_project)
    leaf = await repo.create_task(
        world.db, title=title, task_type="task", parent_id=epic, **kw
    )
    await repo.update_task(world.db, leaf, branch=f"task-{leaf}/work")
    await world.db.commit()
    return epic, leaf


async def _launch_in(world, slug: str, task_id: int, host: str = "k8"):
    resp = await world.client.post(
        f"/api/projects/{slug}/executor-launch",
        json={"target": f"slot:{host}", "task_id": task_id},
        headers=world.human_auth,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["order_id"]


async def test_delivered_order_carries_launch_project(world):
    """AC-1: оба ответа адресату несут тот же id и project; чужой хост заказа не видит."""
    pid = await _project(world, "olymp-helper")
    _epic, leaf = await _epic_task(world, pid, "лист олимпа")
    order_id = await _launch_in(world, "olymp-helper", leaf)

    first = (await _eo._orders(world, "k8")).json()["orders"]
    again = (await _eo._orders(world, "k8")).json()["orders"]
    assert [o["id"] for o in first] == [order_id] == [o["id"] for o in again]
    assert first[0]["project"] == "olymp-helper" == again[0]["project"]
    assert (await _eo._orders(world, "k9")).json()["orders"] == []


async def test_order_project_is_fixed_at_issue(world):
    """AC-2: смена привязки эпика после выпуска не меняет project выпущенного заказа."""
    pid = await _project(world, "olymp-helper")
    other = await _project(world, "other-proj")
    epic, leaf = await _epic_task(world, pid, "лист олимпа")
    order_id = await _launch_in(world, "olymp-helper", leaf)
    assert (await _eo._orders(world, "k8")).json()["orders"][0]["project"] == (
        "olymp-helper"
    )

    await repo.update_task(world.db, epic, project_id=other)
    await world.db.commit()
    got = (await _eo._orders(world, "k8")).json()["orders"]
    assert [o["id"] for o in got] == [order_id]
    assert got[0]["project"] == "olymp-helper"


async def test_order_project_null_for_legacy_and_present_in_waiting_memory(world):
    """AC-3: строка без project_slug -> null; waiting_memory несёт slug выпуска."""
    legacy = await _task(world, "старый")
    legacy_id = (await _eo._launch(world, legacy, "k8")).json()["order_id"]
    await world.db.execute(
        "UPDATE executor_orders SET project_slug = NULL WHERE id = ?", (legacy_id,)
    )
    await world.db.commit()
    got = (await _eo._orders(world, "k8")).json()["orders"]
    assert [o["id"] for o in got] == [legacy_id] and got[0]["project"] is None

    waiting = await _task(world, "ждёт памяти")
    waiting_id = (await _eo._launch(world, waiting, "k9")).json()["order_id"]
    phase = await world.client.post(
        f"/api/executor-hosts/k9/orders/{waiting_id}/phase",
        json={"phase": "waiting_memory", "need_mib": 6144, "free_mib": 3072},
        headers=world.k9_auth,
    )
    assert phase.status_code == 200, phase.text
    for _ in range(2):
        orders = (await _eo._orders(world, "k9")).json()["orders"]
        assert [o["id"] for o in orders] == [waiting_id]
        assert orders[0]["state"] == "waiting_memory"
        assert orders[0]["project"] == world.slug


async def test_migration_keeps_legacy_orders_with_null_project(monkeypatch):
    """Миграция #2122: заказ до шага сохранён, project_slug NULL, отдача даёт null, повтор пуст."""
    import aiosqlite

    import hub.db as db_module
    from hub.migrations import FROZEN_MIGRATIONS
    from hub.migrations.steps import load_steps
    from hub.services import executor_orders
    from tests.test_db_migrations import _SCHEMA

    steps = load_steps()
    name = "add_executor_orders_project_slug_2122"
    assert any(n == name for n, _ in steps), "нет шага миграции #2122"
    before = [(n, q) for n, q in steps if n != name]
    monkeypatch.setattr(db_module, "_MIGRATIONS", [*FROZEN_MIGRATIONS, *before])
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
        cols = [
            r[1]
            for r in await conn.execute_fetchall("PRAGMA table_info(executor_orders)")
        ]
        assert "project_slug" not in cols

        monkeypatch.setattr(db_module, "_MIGRATIONS", [*FROZEN_MIGRATIONS, *steps])
        await db_module._migrate(conn)
        await db_module._migrate(conn)  # повторный старт: без ошибок и дублей
        rows = await conn.execute_fetchall("SELECT * FROM executor_orders")
        assert len(rows) == 1 and rows[0]["project_slug"] is None
        assert executor_orders._order_view(dict(rows[0]))["project"] is None
    finally:
        await conn.close()
