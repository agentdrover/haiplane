"""Выкуп кода заказа k8 только хостом заказа, с безопасным повтором (#1825).

Границы, которые держат безопасность: код заказа хоста выкупает его хост своим
ключом; потерянный ответ безопасно повторяется (токен той же сессии заменяется,
пока ни разу не предъявлен); ни в одной гонке нет двух действующих токенов;
использованный токен ротация не отзывает; окно повтора фиксировано; публичный
путь закрывается флагом только для кодов хоста, облачный и ручной не меняются.
"""

from __future__ import annotations

import asyncio
import json
import logging

import pytest

from hub import config
from hub import db as hub_db
from hub.services import chat_pair as cp
from hub.services import executor_orders as orders
from hub.services import host_redeem
from tests import test_executor_orders as _eo
from tests.test_executor_orders import (
    _code_of,
    _dump,
    _launch,
    _order_row,
    _orders,
    _redeem,
    _rows,
    _task,
)

# Фикстуры мира заказов (pytest находит их по имени в модуле).
world = _eo.world
keys = _eo.keys
_clean_limiter = _eo._clean_limiter
_no_cloud = _eo._no_cloud

ASSERT_NO_STATE_CHANGE = "отказ не меняет состояние"


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _launched(world, host: str = "k8", title: str = "задача хоста"):
    """Запуск → заказ хосту → расшифрованный код: (task_id, order, code)."""
    task_id = await _task(world, title)
    assert (await _launch(world, task_id, host)).status_code == 200
    order = (await _orders(world, host)).json()["orders"][0]
    return task_id, order, _code_of(world, order, host)


async def _host_redeem(world, order_id: int, code: str, host: str = "k8", auth=None):
    resp = await world.client.post(
        f"/api/executor-hosts/{host}/orders/{order_id}/redeem",
        json={"code": code},
        headers=auth or getattr(world, f"{host}_auth"),
    )
    world.seen.append(resp.text)
    return resp


async def _whoami(world, token: str) -> int:
    return (await world.client.get("/api/whoami", headers=_bearer(token))).status_code


async def _sessions(world, run_id: int) -> list[dict]:
    await world.db.commit()
    return await _rows(
        world.db,
        "SELECT * FROM chat_pair_sessions WHERE executor_run_id = ? ORDER BY id",
        (run_id,),
    )


async def _snapshot(world, order_id: int, run_id: int) -> dict:
    """Всё, что отказ менять не вправе: заказ, сессии, коды слота."""
    await world.db.commit()
    return {
        "order": await _order_row(world, order_id),
        "sessions": await _sessions(world, run_id),
        "codes": await _rows(world.db, "SELECT * FROM chat_pair_codes ORDER BY id"),
    }


async def _live(world, run_id: int) -> list[dict]:
    return [s for s in await _sessions(world, run_id) if s["revoked_at"] is None]


async def _finish(world, host: str, order_id: int) -> None:
    """Закрыть заказ любым законным путём: токен не использован — redeem_lost."""
    auth = getattr(world, f"{host}_auth")
    lost = await world.client.post(
        f"/api/executor-hosts/{host}/orders/{order_id}/ack",
        json={"result": "redeem_lost"},
        headers=auth,
    )
    if lost.status_code == 409:
        done = await world.client.post(
            f"/api/executor-hosts/{host}/orders/{order_id}/report",
            json={"outcome": "failed", "note": "конец"},
            headers=auth,
        )
        assert done.status_code == 200, done.text
    else:
        assert lost.status_code == 200, lost.text


# ---------------------------------------------------------------------------
# AC-1: потерянный ответ и чужие вызывающие
# ---------------------------------------------------------------------------


async def test_lost_response_retry_and_foreign_callers(world, monkeypatch):
    """AC-1: повтор отдаёт рабочий токен с большей версией; чужие — отказ без изменений."""
    _task_id, order, code = await _launched(world)
    run_id = order["executor_run_id"]

    first = await _host_redeem(world, order["id"], code)
    assert first.status_code == 200, first.text
    lost = first.json()  # ответ «потерян»: хост его не видел
    assert lost["token_version"] == 1 and lost["order_id"] == order["id"]
    assert lost["retry_until"] and lost["bound_task_id"] == order["task_id"]
    row = await _order_row(world, order["id"])
    assert row["state"] == "redeemed" and row["first_redeemed_at"]
    window = row["retry_until"]

    # Тот же хост, тот же код, в окне: новый токен, версия больше, прежний мёртв.
    retry = await _host_redeem(world, order["id"], code)
    assert retry.status_code == 200, retry.text
    again = retry.json()
    assert again["token_version"] == 2 and again["token"] != lost["token"]
    assert again["expires_at"] == lost["expires_at"]  # срок сессии прежний
    assert again["retry_until"] == window  # окно не продлено
    sessions = await _sessions(world, run_id)
    assert len(sessions) == 1 and sessions[0]["token_version"] == 2
    assert await _whoami(world, lost["token"]) == 401
    assert await _whoami(world, again["token"]) == 200
    assert len(await _live(world, run_id)) == 1

    # Чужие вызывающие: снимок до и после — без единой разницы.
    _t2, other, other_code = await _launched(world, "k9", "чужая")
    fresh = await _launched_unredeemed_k8(world)
    before = await _snapshot(
        world, fresh["order"]["id"], fresh["order"]["executor_run_id"]
    )
    before_other = await _snapshot(world, other["id"], other["executor_run_id"])
    foreign_attempts = [
        # другой хост с кодом и номером заказа k8
        await _host_redeem(world, fresh["order"]["id"], fresh["code"], "k9"),
        # свой хост, но код другого заказа
        await _host_redeem(world, fresh["order"]["id"], other_code, "k8"),
        # чужой заказ чужим номером
        await _host_redeem(world, other["id"], other_code, "k8"),
        # человек (admin API key) и агент: маршрут не их
        await _host_redeem(
            world, fresh["order"]["id"], fresh["code"], "k8", world.human_auth
        ),
        await _host_redeem(
            world, fresh["order"]["id"], fresh["code"], "k8", world.agent_auth
        ),
        # без ключа
        await world.client.post(
            f"/api/executor-hosts/k8/orders/{fresh['order']['id']}/redeem",
            json={"code": fresh["code"]},
        ),
    ]
    for resp in foreign_attempts:
        assert resp.status_code in (401, 403), resp.text
    after = await _snapshot(
        world, fresh["order"]["id"], fresh["order"]["executor_run_id"]
    )
    assert before == after, ASSERT_NO_STATE_CHANGE
    assert before_other == await _snapshot(world, other["id"], other["executor_run_id"])

    # Только код через публичный маршрут после флага: тот же 401, состояния нет.
    monkeypatch.setattr(config, "HOST_ORDER_PUBLIC_REDEEM_CLOSED", True)
    assert (await _redeem(world, fresh["code"])).status_code == 401
    assert (await _redeem(world, code)).status_code == 401
    assert after == await _snapshot(
        world, fresh["order"]["id"], fresh["order"]["executor_run_id"]
    )


async def _launched_unredeemed_k8(world) -> dict:
    """Второй заказ k8: слот занят выкупленным заказом, поэтому закрываем его."""
    open_orders = await _rows(
        world.db, "SELECT id FROM executor_orders WHERE host='k8' AND state<>'closed'"
    )
    for one in open_orders:
        await _finish(world, "k8", one["id"])
    task_id, order, code = await _launched(world, "k8", "ещё не выкуплен")
    return {"task_id": task_id, "order": order, "code": code}


# ---------------------------------------------------------------------------
# AC-2: гонки
# ---------------------------------------------------------------------------


async def test_races_keep_one_live_token(world, monkeypatch):
    """AC-2: две реальные пары соединений; везде ровно один действующий токен."""
    await _race_retry_retry(world)
    await _race_retry_vs_first_request(world)
    await _race_retry_vs_first_request_interleaved(world, monkeypatch)
    await _used_token_is_not_rotated(world)
    await _race_retry_vs_close(world)
    await _race_retry_vs_disable(world)


async def _redeemed(world, host: str = "k8", title: str = "гонка"):
    task_id, order, code = await _launched(world, host, title)
    first = await _host_redeem(world, order["id"], code, host)
    assert first.status_code == 200, first.text
    return task_id, order, code, first.json()


async def _race_retry_retry(world):
    """retry × retry на двух соединениях: действует токен только последней ротации."""
    _t, order, code, first = await _redeemed(world, title="retry-retry")
    a, b = await hub_db.connect(world.dsn), await hub_db.connect(world.dsn)
    try:
        got = await asyncio.gather(
            host_redeem.redeem_for_host(a, "k8", order["id"], code),
            host_redeem.redeem_for_host(b, "k8", order["id"], code),
        )
    finally:
        await a.close()
        await b.close()
    versions = sorted(r["token_version"] for r in got)
    assert versions == [2, 3]
    by_version = {r["token_version"]: r["token"] for r in got}
    assert await _whoami(world, first["token"]) == 401
    assert await _whoami(world, by_version[2]) == 401
    assert await _whoami(world, by_version[3]) == 200
    assert len(await _live(world, order["executor_run_id"])) == 1
    await _finish(world, "k8", order["id"])


async def _race_retry_vs_first_request(world):
    """retry × первый запрос с токеном: исход один из двух, и согласованный."""
    seen: set[str] = set()
    for _ in range(6):
        _t, order, code, first = await _redeemed(world, title="retry-first")
        conn = await hub_db.connect(world.dsn)
        try:
            retry, used = await asyncio.gather(
                host_redeem.redeem_for_host(conn, "k8", order["id"], code),
                world.client.get("/api/whoami", headers=_bearer(first["token"])),
                return_exceptions=True,
            )
        finally:
            await conn.close()
        live = await _live(world, order["executor_run_id"])
        assert len(live) == 1
        if isinstance(retry, dict):
            # Ротация успела первой: запрос со старым токеном — отказ.
            seen.add("rotated")
            assert not isinstance(used, BaseException) and used.status_code == 401
            assert live[0]["first_used_at"] is None
            assert await _whoami(world, retry["token"]) == 200
        else:
            # Допуск успел первым: токен использован, ротация отказана.
            seen.add("used")
            assert isinstance(retry, orders.HostError) and retry.status == 409
            assert retry.detail["reason"] == host_redeem.REASON_IN_USE  # type: ignore[index]
            assert not isinstance(used, BaseException) and used.status_code == 200
            assert live[0]["first_used_at"] is not None
        await _finish(world, "k8", order["id"])
    assert seen  # хотя бы один порядок наблюдён; инвариант проверен в каждом


async def _race_retry_vs_first_request_interleaved(world, monkeypatch):
    """Запрос прочитал строку сессии и стоит перед отметкой; ротация проходит целиком.

    Детерминированный порядок «чтение допуска → ротация → запись допуска»: запись
    называет хеш СТАРОГО токена, строки уже нет, запрос отказан и отметки не ставит.
    """
    _t, order, code, first = await _redeemed(world, title="interleaved")
    real = host_redeem.admit_first_use
    entered, gate = asyncio.Event(), asyncio.Event()

    async def paused(db, token_hash, dsn):
        entered.set()
        await gate.wait()
        return await real(db, token_hash, dsn)

    monkeypatch.setattr(host_redeem, "admit_first_use", paused)
    request = asyncio.create_task(
        world.client.get("/api/whoami", headers=_bearer(first["token"]))
    )
    await asyncio.wait_for(entered.wait(), 10)
    conn = await hub_db.connect(world.dsn)
    try:
        rotated = await host_redeem.redeem_for_host(conn, "k8", order["id"], code)
    finally:
        await conn.close()
    gate.set()
    assert (await request).status_code == 401
    monkeypatch.setattr(host_redeem, "admit_first_use", real)
    live = await _live(world, order["executor_run_id"])
    assert len(live) == 1 and live[0]["first_used_at"] is None
    assert await _whoami(world, rotated["token"]) == 200
    await _finish(world, "k8", order["id"])


async def _used_token_is_not_rotated(world):
    """Токен предъявлен (GET, MCP-подобный допуск): повтор — already_redeemed_in_use."""
    _t, order, code, first = await _redeemed(world, title="used")
    assert await _whoami(world, first["token"]) == 200
    before = await _snapshot(world, order["id"], order["executor_run_id"])
    refused = await _host_redeem(world, order["id"], code)
    assert refused.status_code == 409
    assert refused.json()["detail"]["reason"] == "already_redeemed_in_use"
    assert before == await _snapshot(world, order["id"], order["executor_run_id"])
    assert await _whoami(world, first["token"]) == 200
    await _finish(world, "k8", order["id"])


async def _race_retry_vs_close(world):
    """retry × redeem_lost: заказ закрыт, живого токена нет, повторный токен мёртв."""
    for _ in range(4):
        _t, order, code, _first = await _redeemed(world, title="retry-close")
        conn = await hub_db.connect(world.dsn)
        try:
            retry, closed = await asyncio.gather(
                host_redeem.redeem_for_host(conn, "k8", order["id"], code),
                world.client.post(
                    f"/api/executor-hosts/k8/orders/{order['id']}/ack",
                    json={"result": "redeem_lost"},
                    headers=world.k8_auth,
                ),
                return_exceptions=True,
            )
        finally:
            await conn.close()
        assert not isinstance(closed, BaseException) and closed.status_code == 200
        assert (await _order_row(world, order["id"]))["state"] == "closed"
        assert await _live(world, order["executor_run_id"]) == []
        if isinstance(retry, dict):
            assert await _whoami(world, retry["token"]) == 401
        else:
            assert isinstance(retry, orders.HostError) and retry.status == 409


async def _race_retry_vs_disable(world):
    """retry × отключение хоста: ни живой сессии, ни действующего токена."""
    _t, order, code, _first = await _redeemed(world, "k9", "retry-disable")
    conn, other = await hub_db.connect(world.dsn), await hub_db.connect(world.dsn)
    try:
        retry, disabled = await asyncio.gather(
            host_redeem.redeem_for_host(conn, "k9", order["id"], code),
            orders.disable_host(other, "k9", by=world.human_id),
            return_exceptions=True,
        )
    finally:
        await conn.close()
        await other.close()
    assert not isinstance(disabled, BaseException)
    assert await _live(world, order["executor_run_id"]) == []
    if isinstance(retry, dict):
        assert await _whoami(world, retry["token"]) == 401
    else:
        assert isinstance(retry, orders.HostError) and retry.status == 409


# ---------------------------------------------------------------------------
# AC-3: окно
# ---------------------------------------------------------------------------


async def test_retry_window_is_fixed(world, monkeypatch):
    """AC-3: окно от первого выкупа, не продлевается; purge и рестарт его не двигают."""
    _t, order, code, first = await _redeemed(world, title="окно")
    row = await _order_row(world, order["id"])
    window = row["retry_until"]
    assert row["first_redeemed_at"] and window > row["first_redeemed_at"]

    # Повторы окно не продлевают.
    again = await _host_redeem(world, order["id"], code)
    assert again.status_code == 200 and again.json()["retry_until"] == window
    assert (await _order_row(world, order["id"]))["retry_until"] == window

    # purge до retry_until не уносит запись кода и сессию, даже если срок хранения вышел.
    monkeypatch.setattr(config, "CHAT_PAIR_SPENT_RETENTION_HOURS", 0)
    await world.db.execute(
        "UPDATE chat_pair_codes SET redeemed_at = datetime('now', '-2 days') "
        "WHERE id = ?",
        (row["pair_code_id"],),
    )
    await world.db.commit()
    await cp.purge_expired(world.db)
    assert await _rows(
        world.db, "SELECT 1 FROM chat_pair_codes WHERE id = ?", (row["pair_code_id"],)
    )
    assert len(await _live(world, order["executor_run_id"])) == 1

    # На границе: последняя секунда окна — успех (после «рестарта»: новое соединение).
    await world.db.execute(
        "UPDATE executor_orders SET retry_until = datetime('now', '+2 seconds') "
        "WHERE id = ?",
        (order["id"],),
    )
    await world.db.commit()
    fresh = await hub_db.connect(world.dsn)
    try:
        last = await host_redeem.redeem_for_host(fresh, "k8", order["id"], code)
        assert last["retry_until"] != ""
        # Сразу за границей — отказ с понятным кодом; окно не пересчитывается.
        await fresh.execute(
            "UPDATE executor_orders SET retry_until = datetime('now', '-1 seconds') "
            "WHERE id = ?",
            (order["id"],),
        )
        await fresh.commit()
        with pytest.raises(orders.HostError) as late:
            await host_redeem.redeem_for_host(fresh, "k8", order["id"], code)
    finally:
        await fresh.close()
    assert late.value.status == 409
    assert late.value.detail["reason"] == "redeem_retry_expired"  # type: ignore[index]

    # После окна purge уносит код, и отказ прежний: запись заказа хранит всё нужное.
    await cp.purge_expired(world.db)
    await world.db.commit()
    assert not await _rows(
        world.db, "SELECT 1 FROM chat_pair_codes WHERE id = ?", (row["pair_code_id"],)
    )
    expired = await _host_redeem(world, order["id"], code)
    assert expired.status_code == 409
    assert expired.json()["detail"]["reason"] == "redeem_retry_expired"
    del first


async def test_retry_is_capped_per_order_and_429_keeps_the_order(world, monkeypatch):
    """Лимит ротаций на заказ: 429 заказ не закрывает и токен не трогает."""
    monkeypatch.setattr(config, "HOST_REDEEM_MAX_ROTATIONS", 2)
    _t, order, code, _first = await _redeemed(world, title="лимит")
    for expected in (2, 3):
        got = await _host_redeem(world, order["id"], code)
        assert got.status_code == 200 and got.json()["token_version"] == expected
    before = await _snapshot(world, order["id"], order["executor_run_id"])
    limited = await _host_redeem(world, order["id"], code)
    assert limited.status_code == 429
    assert limited.json()["detail"]["reason"] == "redeem_retry_limited"
    assert before == await _snapshot(world, order["id"], order["executor_run_id"])
    assert (await _order_row(world, order["id"]))["state"] == "redeemed"


# ---------------------------------------------------------------------------
# AC-5: флаг, облачный и ручной пути
# ---------------------------------------------------------------------------


async def test_public_redeem_gate_and_cloud_unchanged(world, monkeypatch, caplog):
    """AC-5: выключен — прежнее поведение; включён — код хоста отказан, прочие как раньше."""
    caplog.set_level(logging.DEBUG)
    assert config.HOST_ORDER_PUBLIC_REDEEM_CLOSED is False  # по умолчанию выключен

    # Флаг выключен: старый клиент k8 выкупает публично, токен рабочий.
    old_task, old_order, old_code = await _launched(world, title="старый клиент")
    legacy = await _redeem(world, old_code)
    assert legacy.status_code == 200, legacy.text
    assert await _whoami(world, legacy.json()["token"]) == 200
    spent = await _order_row(world, old_order["id"])
    assert spent["state"] == "redeemed" and spent["session_id"]
    await _finish(world, "k8", old_order["id"])

    # Облачный и ручной коды: ни флаг, ни маршрут хоста их не касаются.
    acting = await cp.get_acting_agent(world.db)
    cloud_task = await _task(world, "облако")
    cloud_code, _ = await cp.issue_run_code(
        world.db, cloud_task, 1, issued_by_principal_id=world.human_id
    )
    manual_task = await _task(world, "ручной")
    manual_code, _ = await cp.issue_code(
        world.db,
        int(acting["id"]),
        kind="implementer",
        bound_task_id=manual_task,
        channel="slot",
        slot="manual-unregistered",
    )
    await world.db.commit()

    monkeypatch.setattr(config, "HOST_ORDER_PUBLIC_REDEEM_CLOSED", True)
    # Включён: код заказа хоста — тот же 401, что у несуществующего кода.
    new_task, new_order, new_code = await _launched(world, title="после флага")
    refused = await _redeem(world, new_code)
    unknown = await _redeem(world, "AH-ZZZZZZZZ")
    assert refused.status_code == 401 and unknown.status_code == 401
    assert refused.json() == unknown.json()
    unspent = await _order_row(world, new_order["id"])
    assert (
        unspent["state"] in ("pending", "delivered")
        and not unspent["first_redeemed_at"]
    )
    assert await _rows(
        world.db,
        "SELECT 1 FROM chat_pair_codes WHERE id = ? AND redeemed_at IS NULL",
        (unspent["pair_code_id"],),
    )
    # Облачный и ручной коды выкупаются публично, как раньше.
    assert (await _redeem(world, cloud_code)).status_code == 200
    assert (await _redeem(world, manual_code)).status_code == 200
    # Код хоста при этом цел: хост выкупает его своим маршрутом.
    ours = await _host_redeem(world, new_order["id"], new_code)
    assert ours.status_code == 200, ours.text
    assert await _whoami(world, ours.json()["token"]) == 200
    del old_task, new_task

    # Ни кода, ни токена нет в базе, ленте задач, ответах чтения и логах.
    secrets = [old_code, new_code, legacy.json()["token"], ours.json()["token"]]
    dump = _dump(world.dsn)
    feed = json.dumps(
        await _rows(world.db, "SELECT content FROM task_updates"), ensure_ascii=False
    )
    for secret in secrets:
        bare = secret.removeprefix("AH-")
        for needle in (secret, bare, secret.lower()):
            assert needle not in dump
            assert needle not in feed
            assert needle not in caplog.text
            if not secret.startswith(cp.TOKEN_PREFIX):
                # Токен — тело ответа выкупа хосту; прочие ответы его не несут.
                assert all(needle not in text for text in world.seen)
    assert "retry" not in feed.lower()  # повторы выкупа итогов в ленту не пишут


async def test_closed_order_session_is_revoked_for_every_unused_close(world):
    """Любое закрытие заказа с непредъявленным токеном отзывает его сессию (AC-4, места)."""
    # redeem_failed после выкупа без использования.
    _t, order, _code, first = await _redeemed(world, title="failed")
    failed = await world.client.post(
        f"/api/executor-hosts/k8/orders/{order['id']}/ack",
        json={"result": "redeem_failed"},
        headers=world.k8_auth,
    )
    assert failed.status_code == 200
    assert await _live(world, order["executor_run_id"]) == []
    assert await _whoami(world, first["token"]) == 401
    # Отключение хоста с непредъявленным токеном.
    _t, order, _code, first = await _redeemed(world, "k9", "disable")
    await orders.disable_host(world.db, "k9", by=world.human_id)
    assert await _live(world, order["executor_run_id"]) == []
    assert await _whoami(world, first["token"]) == 401


async def test_rotation_update_refuses_a_token_used_after_the_check(world, monkeypatch):
    """AC-2: UPDATE ротации сам называет first_used_at IS NULL, а не полагается на проверку выше.

    Проверка в _retry видит устаревшее «не использован» (подмена чтения), а токен
    уже предъявлен: ротация обязана отказать и не заменить хеш.
    """
    _t, order, code, first = await _redeemed(world, title="устаревшее чтение")
    assert await _whoami(world, first["token"]) == 200  # токен использован
    before = await _snapshot(world, order["id"], order["executor_run_id"])
    real = host_redeem.fetchall

    async def stale(db, sql, params=()):
        rows = await real(db, sql, params)
        if "JOIN chat_pair_sessions s" in sql:
            rows = [{**dict(r), "first_used_at": None} for r in rows]
        return rows

    monkeypatch.setattr(host_redeem, "fetchall", stale)
    conn = await hub_db.connect(world.dsn)
    try:
        with pytest.raises(orders.HostError) as refused:
            await host_redeem.redeem_for_host(conn, "k8", order["id"], code)
    finally:
        await conn.close()
    assert refused.value.detail["reason"] == host_redeem.REASON_IN_USE  # type: ignore[index]
    assert before == await _snapshot(world, order["id"], order["executor_run_id"])
    assert await _whoami(world, first["token"]) == 200


async def test_public_gate_only_for_slot_channel_codes(world, monkeypatch):
    """AC-5: код канала НЕ slot с именем слота, совпавшим с хостом, флагом не закрывается."""
    acting = await cp.get_acting_agent(world.db)
    task_id = await _task(world, "канал не slot")
    code, _ = await cp.issue_code(
        world.db,
        int(acting["id"]),
        kind="implementer",
        bound_task_id=task_id,
        channel="",
        slot="k8",
    )
    await world.db.commit()
    monkeypatch.setattr(config, "HOST_ORDER_PUBLIC_REDEEM_CLOSED", True)
    assert (await _redeem(world, code)).status_code == 200
