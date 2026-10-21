"""Заказ роли k8: источник, актуальность, права, независимость от заказов исполнителя (#1973).

Роль по событию - не исполнитель: заказ роли не имеет кода implementer, не
создаёт прогонов и заказов исполнителя и не меняет ни одного их читателя. Здесь
проверяются источник (событие needs_decision -> ровно один заказ), актуальность
при выдаче, права трёх маршрутов хоста и показ заказов.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

from hub import config, mcp_server
from hub import repository as repo
from hub.services import admin as admin_svc
from hub.services import executor_launch, executor_orders, launch_feedback
from hub.services import executor_dispatch, role_orders
from hub.services.prod_state import format_prod_state
from tests import role_orders_support as _support
from tests.role_orders_support import (
    admit,
    age,
    enter_needs_decision,
    leave_needs_decision,
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

ROLE_FILES = ("hub/services/role_orders.py", "hub/role_host_api.py")
REPO = Path(__file__).resolve().parent.parent


async def _cursor(world) -> int:
    got = await rows(world.db, "SELECT cursor_event_id FROM role_orders_cursor")
    return int(got[0]["cursor_event_id"])


async def test_one_role_order_per_needs_decision_event(world, monkeypatch):
    """AC-1: ровно один заказ на событие за всю жизнь; квота не теряет событие; хвост не удаляется."""
    admit_task = await task_in_project(world, "ждёт решения")
    event = await enter_needs_decision(world, admit_task)

    assert await sweep(world) == 1
    assert await sweep(world) == 0  # повторный свип
    first = (await order_rows(world))[0]
    assert first["role"] == "roadmap" and first["state"] == "open"
    assert first["source_key"] == f"needs_decision:{event}"
    assert first["task_id"] == admit_task and first["project_id"] == world.pid

    # Рестарт / потеря курсора: тот же поток событий читается заново.
    await world.db.execute("UPDATE role_orders_cursor SET cursor_event_id = 0")
    await world.db.commit()
    assert await sweep(world) == 0
    assert len(await order_rows(world)) == 1

    # Повтор после done: заказ закрыт, а событие то же - второго заказа нет.
    await admit(world)
    got = await leased(world)
    assert (await started(world, "k8", got["id"], got["lease_id"])).status_code == 200
    done = await report(world, "k8", got["id"], got["lease_id"])
    assert done.status_code == 200, done.text
    await world.db.execute("UPDATE role_orders_cursor SET cursor_event_id = 0")
    await world.db.commit()
    assert await sweep(world) == 0
    assert [r["state"] for r in await order_rows(world)] == ["done"]

    # Квота исчерпана: событие не теряется, заказ появляется один раз после освобождения.
    monkeypatch.setattr(role_orders, "ROLE_ORDERS_PER_HOUR", 1)
    second_task = await task_in_project(world, "вторая")
    second_event = await enter_needs_decision(world, second_task)
    assert await sweep(world) == 0
    assert await sweep(world) == 0
    assert len(await order_rows(world)) == 1
    assert await _cursor(world) < second_event
    await age(world, first["id"], created_at="-2 hours")
    assert await sweep(world) == 1
    assert await sweep(world) == 0
    created = await order_rows(world)
    assert [r["source_key"] for r in created] == [
        f"needs_decision:{event}",
        f"needs_decision:{second_event}",
    ]

    # Retention: непрочитанный хвост события не удаляется, прочитанный - удаляется.
    monkeypatch.setattr(role_orders, "ROLE_ORDERS_PER_HOUR", 1)
    third_task = await task_in_project(world, "третья")
    third_event = await enter_needs_decision(world, third_task)
    await world.db.execute(
        "UPDATE events SET created_at = datetime('now', '-30 days') WHERE id IN (?, ?)",
        (second_event, third_event),
    )
    await world.db.commit()
    assert await sweep(world) == 0  # квота занята заказом второй задачи
    await repo.prune_events(world.db)
    await world.db.commit()
    left = {r["id"] for r in await rows(world.db, "SELECT id FROM events")}
    assert third_event in left, "непрочитанное событие удалено retention"
    assert second_event not in left, "прочитанное событие должно уходить по сроку"
    await age(world, created[1]["id"], created_at="-2 hours")
    assert await sweep(world) == 1
    assert (await order_rows(world))[-1][
        "source_key"
    ] == f"needs_decision:{third_event}"


async def test_a_stale_entry_expires_without_a_lease(world):
    """AC-4: вышла из needs_decision до выдачи - expired без lease; новый вход даёт свой заказ."""
    await admit(world)
    task_id = await task_in_project(world, "ушла")
    first_event = await enter_needs_decision(world, task_id)
    assert await sweep(world) == 1
    await leave_needs_decision(world, task_id)

    got = await poll(world)
    assert got.status_code == 200 and got.json()["orders"] == []
    old = (await order_rows(world))[0]
    assert old["state"] == "expired" and old["host"] in (None, "")
    assert old["lease_id"] in (None, "") and old["closed_at"]

    # Вышла и вошла снова: новый вход - новое событие - свой заказ.
    second_event = await enter_needs_decision(world, task_id)
    assert second_event != first_event
    assert await sweep(world) == 1
    fresh = await leased(world)
    assert fresh["source_key"] == f"needs_decision:{second_event}"
    assert (await order_row(world, old["id"]))["state"] == "expired"


async def test_a_reentry_before_the_lease_expires_the_old_order_only(world):
    """AC-4 (вход до выдачи): старый заказ expired, выдан только заказ нового входа."""
    await admit(world)
    task_id = await task_in_project(world, "ушла и вернулась")
    await enter_needs_decision(world, task_id)
    assert await sweep(world) == 1
    await leave_needs_decision(world, task_id)
    second_event = await enter_needs_decision(world, task_id)
    assert await sweep(world) == 1

    got = await leased(world)
    assert got["source_key"] == f"needs_decision:{second_event}"
    states = {r["source_key"]: r["state"] for r in await order_rows(world)}
    assert sorted(states.values()) == ["expired", "leased"]


async def test_entry_time_and_generation_decide_freshness(world):
    """AC-4: тот же номер события при другом заходе или поколении - заказ устарел."""
    await admit(world)
    by_time = await task_in_project(world, "другой заход")
    await enter_needs_decision(world, by_time)
    by_generation = await task_in_project(world, "другое поколение")
    await enter_needs_decision(world, by_generation)
    assert await sweep(world) == 2
    await world.db.execute(
        "UPDATE tasks SET status_entered_at = datetime('now', '+5 minutes') WHERE id = ?",
        (by_time,),
    )
    await world.db.execute(
        "UPDATE tasks SET submission_generation = submission_generation + 1 "
        "WHERE id = ?",
        (by_generation,),
    )
    await world.db.commit()
    got = await poll(world)
    assert got.json()["orders"] == []
    assert {r["state"] for r in await order_rows(world)} == {"expired"}


async def test_only_an_enabled_admitted_host_and_no_codes(world):
    """AC-5: чужой хост, агентский и человеческий ключ, disabled хост - 403; кода implementer нет."""
    await admit(world)
    task_id = await task_in_project(world, "права")
    await enter_needs_decision(world, task_id)
    assert await sweep(world) == 1
    before = await order_rows(world)
    codes_before = await rows(world.db, "SELECT COUNT(*) AS n FROM chat_pair_codes")

    base = "/api/executor-hosts/k8/role-orders"
    routes = (
        ("GET", base),
        ("POST", f"{base}/{before[0]['id']}/started"),
        ("POST", f"{base}/{before[0]['id']}/report"),
    )
    body = {"lease_id": "x", "outcome": "done", "note": ""}
    for who, auth in (
        ("k9", world.k9_auth),
        ("agent", world.agent_auth),
        ("human", world.human_auth),
    ):
        for method, path in routes:
            resp = await world.client.request(
                method, path, headers=auth, json=None if method == "GET" else body
            )
            assert resp.status_code == 403, (who, method, path, resp.text)
    assert (await world.client.get(base)).status_code in (401, 403)
    assert await order_rows(world) == before  # состояние не менялось

    # Допущенный включённый хост получает заказ; в нём нет кода implementer.
    got = await poll(world)
    assert got.status_code == 200
    order = got.json()["orders"][0]
    assert set(order) == {
        "id",
        "role",
        "source_key",
        "task_id",
        "lease_id",
        "attempt",
        "lease_deadline",
        "run_max_minutes",
    }
    assert "AH-" not in got.text and "code" not in json.dumps(order)
    assert (
        await rows(world.db, "SELECT COUNT(*) AS n FROM chat_pair_codes")
        == codes_before
    )
    # Код implementer роль заказать не может ни по какому имени.
    for forbidden in ("implementer", "slot", "chat_pair"):
        try:
            await role_orders.order_role(
                world.db, role=forbidden, source_key="x:1", task_id=task_id
            )
        except role_orders.RoleOrderError as exc:
            assert exc.status == 422
        else:
            raise AssertionError(f"роль {forbidden!r} принята")
    await _disabled_during_the_long_poll(world)
    # enabled=0 без отзыва принципала тоже закрывает lease: проверка в транзакции своя.
    await world.db.execute("UPDATE executor_hosts SET enabled = 0 WHERE name = 'k8'")
    await world.db.commit()
    principal = await rows(
        world.db, "SELECT principal_id FROM executor_hosts WHERE name = 'k8'"
    )
    try:
        await role_orders.lease(world.db, "k8", int(principal[0]["principal_id"]))
    except role_orders.RoleOrderError as exc:
        assert exc.status == 403
    else:
        raise AssertionError("хост с enabled=0 получил lease")


async def _disabled_during_the_long_poll(world) -> None:
    """Хост выключен, пока висит long-poll, - 403, заказ остаётся открытым."""
    await admit(world, "k9")
    waiting = asyncio.ensure_future(poll(world, "k9", wait=5))
    await asyncio.sleep(0.4)
    disabled = await world.client.post(
        "/api/admin/executor-hosts/k9/disable", headers=world.human_auth
    )
    assert disabled.status_code == 200, disabled.text
    task_id = await task_in_project(world, "выключен")
    await enter_needs_decision(world, task_id)
    assert await sweep(world) == 1
    answer = await asyncio.wait_for(waiting, 10)
    assert answer.status_code == 403, answer.text
    assert (await order_rows(world))[-1]["state"] == "open"
    # Прямой вызов службы после выключения даёт тот же отказ: проверка в транзакции lease.
    principal = await rows(
        world.db, "SELECT principal_id FROM executor_hosts WHERE name = 'k9'"
    )
    try:
        await role_orders.lease(world.db, "k9", int(principal[0]["principal_id"]))
    except role_orders.RoleOrderError as exc:
        assert exc.status == 403
    else:
        raise AssertionError("выключенный хост получил lease")
    assert (await order_rows(world))[-1]["state"] == "open"


async def test_admission_is_set_by_a_human_only(world):
    """Допуск хоста к роли и проекту ставит человек; агент и ключ хоста - 403; нет допуска - нет заказа."""
    task_id = await task_in_project(world, "допуск")
    await enter_needs_decision(world, task_id)
    assert await sweep(world) == 1
    assert (await poll(world)).json()["orders"] == []  # хост не допущен
    body = {"role": "roadmap", "project": world.slug}
    url = "/api/admin/executor-hosts/k8/role-admissions"
    for auth in (world.agent_auth, world.k8_auth):
        assert (
            await world.client.post(url, json=body, headers=auth)
        ).status_code == 403
    bad = await world.client.post(
        url,
        json={"role": "implementer", "project": world.slug},
        headers=world.human_auth,
    )
    assert bad.status_code == 422
    ok = await world.client.post(url, json=body, headers=world.human_auth)
    assert ok.status_code == 200, ok.text
    assert len((await poll(world)).json()["orders"]) == 1
    # k9 не допущен к проекту: заказ выдан k8, а у k9 пусто и lease не отнят.
    assert (await poll(world, "k9")).json()["orders"] == []


async def test_role_orders_do_not_touch_executor_readers(world):
    """AC-6: заказ роли не меняет _task_busy, redeem, read_order_outcome.newer, plaque, _live_run, task_budget."""
    await admit(world)
    task_id = await task_in_project(world, "исполнитель и роль")
    launched = await _eo_launch(world, task_id)
    order_id = launched.json()["order_id"]

    async def readers():
        db = world.db
        return {
            "busy": await executor_orders._task_busy(db, task_id),
            "outcome": _without_status(
                await executor_orders.read_order_outcome(db, "k8", order_id)
            ),
            "plaque": await launch_feedback.plaque(db, task_id),
            "live": await executor_launch._live_run(db, task_id),
            "budget": repr(await executor_dispatch.task_budget(db, task_id)),
            "tables": [
                (await rows(world.db, f"SELECT COUNT(*) AS n FROM {t}"))[0]["n"]  # nosec B608 - фиксированные имена
                for t in (
                    "executor_orders",
                    "executor_runs",
                    "chat_pair_codes",
                    "chat_pair_sessions",
                )
            ],
        }

    before = await readers()
    await enter_needs_decision(world, task_id)
    assert await sweep(world) == 1
    got = await leased(world)
    await started(world, "k8", got["id"], got["lease_id"])
    await report(world, "k8", got["id"], got["lease_id"], outcome="failed")
    after = await readers()
    # Статус задачи сменился входом в needs_decision; читатели исполнителя его не используют.
    assert after == before
    # Сами модули роли не знают таблиц и кода исполнителя.
    for rel in ROLE_FILES:
        source = (REPO / rel).read_text(encoding="utf-8")
        for word in ("executor_orders", "executor_runs", "chat_pair"):
            assert word not in source, f"{rel} упоминает {word}"


def _without_status(view):
    """Статус задачи сменил сам вход в needs_decision, а не заказ роли."""
    return {k: v for k, v in (view or {}).items() if k != "task_status"}


async def _eo_launch(world, task_id):
    from tests.test_executor_orders import _launch

    resp = await _launch(world, task_id, "k8")
    assert resp.status_code == 200, resp.text
    return resp


async def test_role_orders_are_on_prod_state_and_the_card(world):
    """AC-7: активные и терминальные за сутки заказы видны в REST, форматтёре CLI/MCP и на карточке."""
    await admit(world)
    tasks = [await task_in_project(world, f"заказ {n}") for n in range(4)]
    for task_id in tasks:
        await enter_needs_decision(world, task_id)
    assert await sweep(world) == 4
    ids = [r["id"] for r in await order_rows(world)]
    await world.db.execute(
        "UPDATE role_orders SET state='done', closed_at=datetime('now'), outcome='done' "
        "WHERE id = ?",
        (ids[1],),
    )
    await world.db.execute(
        "UPDATE role_orders SET state='failed', closed_at=datetime('now','-3 days'), "
        "outcome='failed' WHERE id = ?",
        (ids[2],),
    )
    await world.db.execute(
        "UPDATE role_orders SET state='expired', closed_at=datetime('now','-2 hours'), "
        "outcome='stale_entry' WHERE id = ?",
        (ids[3],),
    )
    await world.db.commit()

    snapshot = (
        await world.client.get("/api/prod-state", headers=world.human_auth)
    ).json()
    shown = {o["id"]: o["state"] for o in snapshot["role_orders"]}
    assert shown == {ids[0]: "open", ids[1]: "done", ids[3]: "expired"}
    text = format_prod_state(snapshot)
    assert "роадмап" in text and f"#{ids[0]}" in text and "open" in text
    assert f"#{ids[2]}" not in text  # терминальный старше суток

    card = (
        await world.client.get(f"/api/tasks/{tasks[0]}", headers=world.human_auth)
    ).json()
    assert card["role_orders"][0]["id"] == ids[0]
    assert card["role_orders"][0]["state"] == "open"
    session = await admin_svc.create_browser_session(world.db, world.human_id)
    await world.db.commit()
    page = await world.client.get(
        f"/tasks/{tasks[0]}",
        headers={"Cookie": f"{config.HUB_COOKIE_NAME}={session}"},
    )
    assert page.status_code == 200 and "роль: роадмап" in page.text
    # MCP: карточка несёт поле и строку «вызван роадмап».
    with (
        patch.object(mcp_server, "_api_post", AsyncMock(return_value={})),
        patch.object(mcp_server, "_api_get", AsyncMock(return_value=card)),
    ):
        res = await mcp_server.hub_task_status(tasks[0])
    assert res.structuredContent["task"]["role_orders"][0]["id"] == ids[0]
    from tests.test_mcp_server import _mcp_text

    assert "вызван роадмап" in _mcp_text(res)


async def test_the_sweep_is_registered_before_events_retention(world):
    """Свип заказов ролей стоит после начисления минут и ДО очистки events."""
    from hub.poller import SWEEPS

    names = [s.name for s in SWEEPS]
    assert names.index("owner_minutes") < names.index("role_orders")
    assert names.index("role_orders") < names.index("events_retention")


async def test_report_note_is_sanitised_and_bounded(world):
    """Свободный текст отчёта не уходит в ленту сырым: усечён, без секретоподобных строк."""
    await admit(world)
    task_id = await task_in_project(world, "заметка")
    await enter_needs_decision(world, task_id)
    assert await sweep(world) == 1
    got = await leased(world)
    await started(world, "k8", got["id"], got["lease_id"])
    too_long = await report(world, "k8", got["id"], got["lease_id"], note="я" * 501)
    assert too_long.status_code == 422
    secret = (
        "ahcp_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789abcdefg"  # pragma: allowlist secret
    )
    ok = await report(
        world, "k8", got["id"], got["lease_id"], note=f"итог\n\x00 {secret} конец"
    )
    assert ok.status_code == 200, ok.text
    row = await order_row(world, got["id"])
    assert secret not in row["note"] and "\x00" not in row["note"]
    feed = await repo.get_task_updates(world.db, task_id)
    text = " ".join(str(dict(u)["content"]) for u in feed)
    assert secret not in text and f"#{got['id']}" in text


async def test_pruning_the_source_event_does_not_stale_an_open_order(world):
    """Codex P2: очистка ленты удаляет старое событие, но заказ неизменной задачи остаётся годным."""
    await admit(world)
    task_id = await task_in_project(world, "старое событие")
    event = await enter_needs_decision(world, task_id)
    assert await sweep(world) == 1
    await world.db.execute(
        "UPDATE events SET created_at = datetime('now', '-30 days') WHERE id = ?",
        (event,),
    )
    await world.db.commit()
    await repo.prune_events(world.db)
    await world.db.commit()
    gone = await rows(world.db, "SELECT 1 FROM events WHERE id = ?", (event,))
    assert not gone, "событие должно было уйти по сроку"
    got = await leased(world)
    assert got["source_key"] == f"needs_decision:{event}"


async def test_the_stale_sweep_walks_every_page_of_open_orders(world, monkeypatch):
    """Codex P3: устаревшие заказы за пределами первой страницы тоже закрываются."""
    monkeypatch.setattr(role_orders, "REAP_BATCH", 2)
    tasks = [await task_in_project(world, f"хвост {n}") for n in range(5)]
    for task_id in tasks:
        await enter_needs_decision(world, task_id)
    assert await sweep(world) == 5
    for task_id in tasks:
        await leave_needs_decision(world, task_id)
    await role_orders.reap(world.db)
    await world.db.commit()
    assert {r["state"] for r in await order_rows(world)} == {"expired"}
