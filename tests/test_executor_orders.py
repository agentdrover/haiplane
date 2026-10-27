"""Хаб выдаёт код исполнителя слоту хоста: запуск человеком, без переноса кода (#1712).

Человек нажимает «запустить на k8» — хаб в ОДНОЙ транзакции создаёт прогон, код
implementer channel=slot и заказ хосту с шифротекстом на ключ хоста. Хост
забирает заказ своим ключом роли slot_runner, расшифровывает и выкупает код.

Здесь проверяются границы, которые держат безопасность: открытого кода нет
нигде (грепается вся база, ответы и журнал), ключ хоста открывает шесть маршрутов
своего хоста и ничего больше (настоящий DB API key, не подмена роли), бронь слота
держит индекс, а любое закрытие сжигает код.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from hub import brand, config
from hub import db as hub_db
from hub import repository as repo
from hub.config import TokenIdentity
from hub.csrf import token_for_session_cookie
from hub.db import seed_default_skills
from hub.integrations import cursor_cloud
from hub.services import admin as admin_svc
from hub.services import chat_pair as cp
from hub.services import executor_launch as el
from hub.services import executor_orders as orders
from hub.services.executor_dispatch import poll_executor_runs

AGENT_TOKEN = "agent-token-1712"  # pragma: allowlist secret
# Look like a session token and an API key by prefix only: the report filter must
# refuse them, and the secret scan must not take the fixtures for real ones.
FAKE_SESSION_TOKEN = (
    "ahcp_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789abcdefg"  # pragma: allowlist secret
)
FAKE_DOTTED = "Q7m9R2t8V4x6Z1b3.N5p0S8u2W9y4A6c1"  # pragma: allowlist secret
FAKE_DASHED = "h8v3q9m2n7w4r6t1-k5z0p2s9u8x4y6b3"  # pragma: allowlist secret
FAKE_KEY_TAIL = "ochk_AbCdEfGhIjKlMnOpQrStUvWxYz01234567"  # pragma: allowlist secret
HUB_URL = "https://hub.example.test"
SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "executor_host_decrypt.py"


# ---------------------------------------------------------------------------
# Ключи хостов: RSA-3072 стоит секунд, поэтому один раз на модуль
# ---------------------------------------------------------------------------


def _make_key() -> SimpleNamespace:
    private = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    return SimpleNamespace(
        private_pem=private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
        public_pem=private.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode(),
    )


@pytest.fixture(scope="module")
def keys() -> SimpleNamespace:
    return SimpleNamespace(k8=_make_key(), k9=_make_key())


def _reference():
    spec = importlib.util.spec_from_file_location("executor_host_decrypt", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Мир: человек, проект в режиме manual, два хоста, открытые задачи
# ---------------------------------------------------------------------------


async def _rows(db, sql: str, params: tuple = ()) -> list[dict]:
    cursor = await db.execute(sql, params)
    return [dict(r) for r in await cursor.fetchall()]


@pytest.fixture(autouse=True)
def _clean_limiter():
    cp.chat_pair_limiter._buckets.clear()
    yield
    cp.chat_pair_limiter._buckets.clear()


@pytest.fixture(autouse=True)
def _no_cloud(monkeypatch):
    """Облако в этой задаче не зовётся НИКОГДА: вызов проваливает тест."""
    calls: list[dict] = []

    async def _boom(**kw):
        calls.append(kw)
        raise AssertionError("cursor_cloud.create_agent_attempt зван слотом")

    monkeypatch.setattr(cursor_cloud, "create_agent_attempt", _boom)
    return calls


@pytest.fixture
async def world(client, db, db_dsn, keys, monkeypatch):
    monkeypatch.setattr(
        config, "HUB_TOKENS", {AGENT_TOKEN: TokenIdentity("bot", "agent")}
    )
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    monkeypatch.setenv(brand.ENV_PREFIX + "HUB_URL", HUB_URL)
    await seed_default_skills(db)
    human = await admin_svc.create_principal(
        db, kind="human", username="owner1712", role_slug="operator"
    )
    human_key = await admin_svc.create_api_key(db, human["id"], name="laptop")
    pid = await repo.create_project(db, slug="slot-1712", name="slot-1712")
    await repo.update_project(
        db,
        pid,
        gate_policy=json.dumps({"executor_launch": "manual"}),
        forge="github",
        repo="agentdrover/haiplane",
    )
    await db.commit()
    world = SimpleNamespace(
        client=client,
        db=db,
        dsn=db_dsn,
        keys=keys,
        human_id=int(human["id"]),
        pid=pid,
        slug="slot-1712",
        human_auth={"Authorization": f"Bearer {human_key['plaintext_key']}"},
        agent_auth={"Authorization": f"Bearer {AGENT_TOKEN}"},
        seen=[],
    )
    for name, pair in (("k8", keys.k8), ("k9", keys.k9)):
        created = await client.post(
            "/api/admin/executor-hosts",
            json={"name": name, "public_key_pem": pair.public_pem},
            headers=world.human_auth,
        )
        assert created.status_code == 200, created.text
        setattr(
            world,
            f"{name}_auth",
            {"Authorization": f"Bearer {created.json()['api_key']}"},
        )
    return world


async def _task(world, title: str = "задача на слот", status: str = "open") -> int:
    task_id = await repo.create_task(
        world.db,
        title=title,
        description="",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status=status,
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await repo.update_task(
        world.db, task_id, project_id=world.pid, branch=f"task-{task_id}/work"
    )
    await world.db.commit()
    return task_id


async def _launch(world, task_id: int | None, host: str = "k8", **extra):
    body: dict = {"target": f"slot:{host}"}
    if task_id is not None:
        body["task_id"] = task_id
    resp = await world.client.post(
        f"/api/projects/{world.slug}/executor-launch",
        json=body,
        headers=world.human_auth,
        **extra,
    )
    world.seen.append(resp.text)
    return resp


async def _orders(world, host: str = "k8", wait: int = 0, auth: dict | None = None):
    resp = await world.client.get(
        f"/api/executor-hosts/{host}/orders?wait={wait}",
        headers=auth or getattr(world, f"{host}_auth"),
    )
    world.seen.append(resp.text)
    return resp


def _code_of(world, order: dict, host: str = "k8") -> str:
    """Что сделал бы хост: расшифровал закрытым ключом."""
    private = getattr(world.keys, host).private_pem
    return _reference().decrypt_code(private, order["code_ciphertext"])


async def _redeem(world, code: str):
    return await world.client.post(
        "/api/auth/chat-pair/redeem",
        json={"code": code},
        headers={"x-forwarded-for": "203.0.113.17"},
    )


async def _claim_and_start(world, task_id: int, session: dict) -> None:
    claimed = await world.client.post(
        f"/api/tasks/{task_id}/claim",
        json={"agent": config.CHAT_PAIR_AGENT, "session_id": f"slot-{task_id}"},
        headers=session,
    )
    assert claimed.status_code == 200, claimed.text
    started = await world.client.post(
        f"/api/tasks/{task_id}/pair-start",
        json={
            "assigned_agent": config.CHAT_PAIR_AGENT,
            "plan": "Plan: go",
            "session_id": f"slot-{task_id}",
            "git_mode": "remote",
        },
        headers=session,
    )
    assert started.status_code == 200, started.text


async def _launch_redeem_keep_stale(world, task_id: int) -> dict:
    """Запуск и выкуп, но с запиской заказа ДО выкупа — как у читателя без блокировки."""
    assert (await _launch(world, task_id)).status_code == 200
    order = (await _orders(world)).json()["orders"][0]
    stale = await _order_row(world, order["id"])
    assert stale["state"] == "delivered"
    redeemed = await _redeem(world, _code_of(world, order))
    assert redeemed.status_code == 200, redeemed.text
    return {"order": order, "stale": stale}


async def _launch_redeem(world, task_id: int, host: str = "k8") -> dict:
    """Запуск → заказ хосту → расшифровка → выкуп; сессия и заказ."""
    launched = await _launch(world, task_id, host)
    assert launched.status_code == 200, launched.text
    got = (await _orders(world, host)).json()["orders"]
    assert len(got) == 1, got
    code = _code_of(world, got[0], host)
    redeemed = await _redeem(world, code)
    assert redeemed.status_code == 200, redeemed.text
    return {
        "order": got[0],
        "code": code,
        "session": {"Authorization": f"Bearer {redeemed.json()['token']}"},
    }


async def _order_row(world, order_id: int) -> dict:
    await world.db.commit()
    return (
        await _rows(world.db, "SELECT * FROM executor_orders WHERE id = ?", (order_id,))
    )[0]


async def _submit(
    world, task_id: int, generation: int = 1, *, holder: bool = True
) -> None:
    """Сдача задачи прогоном заказа: держатель задачи — прогон её последнего заказа (#1831).

    ``holder=False`` — сдача чужого прогона (облако, ручной pair): держателя нет.
    """
    fields: dict = {"status": "review", "submission_generation": generation}
    last = await _rows(
        world.db,
        "SELECT executor_run_id FROM executor_orders WHERE task_id = ? "
        "ORDER BY id DESC LIMIT 1",
        (task_id,),
    )
    if holder and last:
        fields["executor_run_id"] = last[0]["executor_run_id"]
    await repo.update_task(world.db, task_id, **fields)
    await world.db.commit()


def _dump(dsn: str) -> str:
    """Вся база текстом — для грепа открытого кода."""
    con = sqlite3.connect(dsn)
    try:
        return "\n".join(con.iterdump())
    finally:
        con.close()


async def _other_connection(world):
    return await hub_db.connect(world.dsn)


# ---------------------------------------------------------------------------
# AC-1
# ---------------------------------------------------------------------------


async def test_slot_launch_is_one_transaction_without_plain_code(
    world, _no_cloud, monkeypatch, caplog
):
    """AC-1: прогон, код и заказ — одна транзакция; открытого кода нигде нет."""
    caplog.set_level(logging.DEBUG)
    first = await _task(world, "первая")
    chosen = await _task(world, "выбранная")

    resp = await _launch(world, chosen)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["task_id"] == chosen and body["host"] == "k8"
    assert body["order_id"] and "code" not in body and body["agent_id"] == ""
    assert _no_cloud == []
    runs = await _rows(world.db, "SELECT * FROM executor_runs")
    assert len(runs) == 1
    run = runs[0]
    assert (run["channel"], run["slot"], run["launched_by"]) == (
        "slot",
        "k8",
        world.human_id,
    )
    assert (run["task_id"], run["submission_generation"]) == (chosen, 1)
    codes = await _rows(world.db, "SELECT * FROM chat_pair_codes WHERE channel='slot'")
    assert len(codes) == 1
    code_row = codes[0]
    assert (
        code_row["slot"],
        code_row["bound_task_id"],
        code_row["bound_generation"],
    ) == (
        "k8",
        chosen,
        1,
    )
    order = (await _orders(world)).json()["orders"][0]
    assert order["task_id"] == chosen and first != chosen
    assert order["code_ciphertext"] and "{{PAIR_CODE}}" in order["order_text"]
    plain = _code_of(world, order)
    assert plain.startswith("AH-")
    audit = await _rows(
        world.db,
        "SELECT summary, detail FROM admin_audit_log "
        "WHERE action = 'implementer_code_dispatched'",
    )
    assert len(audit) == 1
    assert "channel=slot" in audit[0]["summary"] and "k8" in audit[0]["summary"]
    assert json.loads(audit[0]["detail"])["launched_by"] == world.human_id

    # Открытого кода нет в базе, ответах и журнале.
    raw = plain.removeprefix("AH-")
    dump = _dump(world.dsn)
    for needle in (plain, raw, plain.lower()):
        assert needle not in dump
        assert all(needle not in text for text in world.seen)
        assert needle not in caplog.text
    # Плейсхолдер на месте, а вокруг него нет значения кода.
    assert raw not in order["order_text"]
    await _rollback_part(world, monkeypatch)
    await _nested_rollback_part(world, monkeypatch)


async def _nested_rollback_part(world, monkeypatch):
    """AC-1: и когда транзакция уже открыта снаружи: отказ не оставляет код и прогон."""
    task_id = await _task(world, "вложенный откат")
    project = dict(await repo.get_project(world.db, world.pid))
    before = {
        table: len(await _rows(world.db, f"SELECT 1 FROM {table}"))
        for table in ("executor_runs", "chat_pair_codes", "executor_orders")
    }

    def _bad_key(*_a, **_k):
        raise orders.HostError(422, "ключ хоста непригоден")

    monkeypatch.setattr(orders, "encrypt_code", _bad_key)
    # Внешний владелец транзакции: DML до запуска и коммит после него.
    await world.db.execute("UPDATE tasks SET title = title WHERE id = ?", (task_id,))
    assert world.db.in_transaction
    refused = await el.launch_executor(
        world.db,
        project,
        issuer_principal_id=world.human_id,
        target="slot:k9",
        task_id=task_id,
    )
    await world.db.commit()
    assert not refused.launched and "непригоден" in refused.reason
    after = {
        table: len(await _rows(world.db, f"SELECT 1 FROM {table}"))
        for table in ("executor_runs", "chat_pair_codes", "executor_orders")
    }
    assert after == before


async def _rollback_part(world, monkeypatch):
    """AC-1: сбой до INSERT заказа откатывает и код, и прогон (хост k9, k8 занят)."""
    task_id = await _task(world, "откат")
    project = dict(await repo.get_project(world.db, world.pid))
    before = {
        table: len(await _rows(world.db, f"SELECT 1 FROM {table}"))
        for table in ("executor_runs", "chat_pair_codes", "executor_orders")
    }
    audits = len(
        await _rows(
            world.db,
            "SELECT 1 FROM admin_audit_log WHERE action='implementer_code_dispatched'",
        )
    )

    def _boom(*_a, **_k):
        raise RuntimeError("шифрование упало")

    monkeypatch.setattr(orders, "encrypt_code", _boom)
    with pytest.raises(RuntimeError):
        await el.launch_executor(
            world.db,
            project,
            issuer_principal_id=world.human_id,
            target="slot:k9",
            task_id=task_id,
        )

    await world.db.commit()
    after = {
        table: len(await _rows(world.db, f"SELECT 1 FROM {table}"))
        for table in ("executor_runs", "chat_pair_codes", "executor_orders")
    }
    assert after == before
    assert (
        len(
            await _rows(
                world.db,
                "SELECT 1 FROM admin_audit_log "
                "WHERE action='implementer_code_dispatched'",
            )
        )
        == audits
    )


# ---------------------------------------------------------------------------
# AC-2
# ---------------------------------------------------------------------------


async def test_slot_runner_key_reaches_only_its_orders(world, monkeypatch):
    """AC-2: настоящий DB API key хоста — шесть маршрутов своего хоста и всё."""
    # Пустой long-poll держится в пределах wait.
    started = time.monotonic()
    empty = await _orders(world, "k8", wait=2)
    waited = time.monotonic() - started
    assert empty.status_code == 200 and empty.json()["orders"] == []
    assert 1.0 <= waited < 6.0

    own_task = await _task(world, "своя")
    other_task = await _task(world, "чужая")
    assert (await _launch(world, own_task, "k8")).status_code == 200
    assert (await _launch(world, other_task, "k9")).status_code == 200
    k9_order = (await _orders(world, "k9")).json()["orders"][0]

    # Роль настоящая и не падает в agent.
    key = world.k8_auth["Authorization"].removeprefix("Bearer ")
    identity = await admin_svc.resolve_api_key(world.db, key)
    assert identity is not None and identity.role == "slot_runner"
    assert identity.auth_source == "db_api_key"
    assert not identity.is_agent and not identity.is_human and not identity.is_admin
    for perm in ("tasks.read", "tasks.update", "tasks.agent_report", "tasks.create"):
        assert not identity.has_permission(perm)

    started = time.monotonic()
    own = await _orders(world, "k8", wait=30)
    assert own.status_code == 200
    assert [o["task_id"] for o in own.json()["orders"]] == [own_task]
    assert time.monotonic() - started < 5.0  # заказ есть — отвечает сразу

    # Чужой {name}.
    assert (await _orders(world, "k9", auth=world.k8_auth)).status_code == 403
    # Свой {name}, чужой {id}.
    foreign = await world.client.post(
        f"/api/executor-hosts/k8/orders/{k9_order['id']}/ack",
        json={"result": "redeemed"},
        headers=world.k8_auth,
    )
    assert foreign.status_code == 403
    # Любой другой маршрут — в том числе безобидные чтения.
    for method, path in (
        ("GET", "/api/whoami"),
        ("GET", f"/api/tasks/{own_task}"),
        ("GET", "/api/tasks"),
        ("POST", "/api/tasks"),
        ("POST", f"/api/tasks/{own_task}/claim"),
        ("GET", "/api/admin/executor-hosts"),
        ("POST", "/api/auth/chat-pair/start"),
        ("GET", "/api/events"),
    ):
        resp = await world.client.request(
            method, path, headers=world.k8_auth, json={} if method == "POST" else None
        )
        assert resp.status_code == 403, (method, path, resp.status_code)
    # Агент и человек на маршруты хоста.
    for auth in (world.agent_auth, world.human_auth):
        assert (await _orders(world, "k8", auth=auth)).status_code == 403
        assert (
            await world.client.post(
                f"/api/executor-hosts/k8/orders/{k9_order['id']}/ack",
                json={"result": "redeemed"},
                headers=auth,
            )
        ).status_code == 403
    # Вариации пути не расширяют allowlist: слэш, регистр, двойной слэш, точки.
    for path in (
        "/api/executor-hosts/k8/orders/",
        "/API/executor-hosts/k8/orders",
        "/api//executor-hosts/k8/orders",
        "/api/executor-hosts/k8/orders/../../tasks",
        "/api/executor-hosts/k8/orders/%2e%2e/%2e%2e/tasks",
        "/api/executor-hosts/K8/orders",
        "/api/executor-hosts/k8/orders/1/ack/",
        "/api/executor-hosts/k8/orders/1/ack/extra",
    ):
        resp = await world.client.request("GET", path, headers=world.k8_auth)
        assert resp.status_code in (403, 404), (path, resp.status_code)
        assert "orders" not in resp.text or resp.status_code == 403, path
    head = await world.client.request(
        "HEAD", "/api/executor-hosts/k9/orders", headers=world.k8_auth
    )
    assert head.status_code == 403
    # Лишняя роль не выводит ключ из-под allowlist: slot_runner первый в порядке.
    host = await orders.get_host(world.db, "k8")
    await admin_svc.set_principal_roles(
        world.db, host["principal_id"], ["agent", "operator", "admin", "slot_runner"]
    )
    await world.db.commit()
    again = await admin_svc.resolve_api_key(world.db, key)
    assert again is not None and again.role == "slot_runner" and not again.is_admin
    assert (
        await world.client.get("/api/whoami", headers=world.k8_auth)
    ).status_code == 403
    # Один хост не копит открытых long-poll без предела.
    from hub import executor_host_api

    monkeypatch.setattr(executor_host_api, "ORDERS_POLL_MAX", 1)
    await world.db.execute("UPDATE executor_orders SET state='closed'")
    await world.db.commit()
    first, second = await asyncio.gather(
        _orders(world, "k8", wait=3), _orders(world, "k8", wait=3)
    )
    assert sorted([first.status_code, second.status_code]) == [200, 429]
    assert executor_host_api._polling == {}


# ---------------------------------------------------------------------------
# AC-3
# ---------------------------------------------------------------------------


async def test_one_task_per_slot_across_both_doors(world):
    """AC-3: один заказ на хост на уровне базы; ручная дверь — 409; сдача освобождает."""
    first = await _task(world, "т0")
    second = await _task(world, "т1")
    third = await _task(world, "т2")

    both = await asyncio.gather(_launch(world, first), _launch(world, second))
    codes = sorted(r.status_code for r in both)
    assert codes == [200, 409], [r.text for r in both]
    winner = first if both[0].status_code == 200 else second
    loser = second if winner == first else first
    assert "слот занят" in next(r for r in both if r.status_code == 409).text
    assert len(await _rows(world.db, "SELECT 1 FROM executor_orders")) == 1
    # Бронь держит сама база (триггер слотов #2107), не только проверка: второй
    # открытый заказ хоста с одним слотом база не примет, закрытый — примет.
    import aiosqlite

    open_order = (await _rows(world.db, "SELECT * FROM executor_orders"))[0]
    clone = (
        "INSERT INTO executor_orders (host, task_id, generation, executor_run_id, "
        "expires_at, state) VALUES ('k8', ?, 1, ?, datetime('now', '+5 minutes'), ?)"
    )
    with pytest.raises(aiosqlite.IntegrityError):
        await world.db.execute(clone, (loser, open_order["executor_run_id"], "pending"))
    await world.db.rollback()
    await world.db.execute(clone, (loser, open_order["executor_run_id"], "closed"))
    await world.db.execute("DELETE FROM executor_orders WHERE state = 'closed'")
    await world.db.commit()

    # Ручная дверь для зарегистрированного хоста — 409, для чужого имени как раньше.
    manual = await world.client.post(
        "/api/auth/chat-pair/start",
        json={"kind": "implementer", "task_id": loser, "channel": "slot", "slot": "k8"},
        headers=world.human_auth,
    )
    assert manual.status_code == 409
    assert "executor-launch target=slot:k8" in manual.text
    unregistered = await world.client.post(
        "/api/auth/chat-pair/start",
        json={
            "kind": "implementer",
            "task_id": loser,
            "channel": "slot",
            "slot": "slot-7",
        },
        headers=world.human_auth,
    )
    assert unregistered.status_code == 200, unregistered.text

    # Бронь держится между выкупом и захватом, и после захвата.
    order = (await _orders(world)).json()["orders"][0]
    redeemed = await _redeem(world, _code_of(world, order))
    assert redeemed.status_code == 200
    session = {"Authorization": f"Bearer {redeemed.json()['token']}"}
    assert (await _launch(world, third)).status_code == 409
    await _claim_and_start(world, winner, session)
    assert (await _launch(world, third)).status_code == 409
    assert (await _order_row(world, order["id"]))["state"] == "redeemed"

    # Сдача поколением == generation закрывает заказ и освобождает слот.
    await _submit(world, winner, generation=1)
    freed = await _launch(world, third)
    assert freed.status_code == 200, freed.text
    closed = await _order_row(world, order["id"])
    assert closed["state"] == "closed" and closed["close_reason"] == "submitted"
    run = (
        await _rows(world.db, "SELECT * FROM executor_runs WHERE task_id=?", (winner,))
    )[0]
    assert run["outcome"] == "finished"


# ---------------------------------------------------------------------------
# AC-4
# ---------------------------------------------------------------------------


async def test_redelivery_and_ack_contract(world):
    """AC-4: потерянный ответ — тот же id; ack идемпотентен; чужой — 403."""
    task_id = await _task(world)
    other = await _task(world, "на k9")
    assert (await _launch(world, task_id)).status_code == 200
    assert (await _launch(world, other, "k9")).status_code == 200

    first = (await _orders(world)).json()["orders"]
    again = (await _orders(world)).json()["orders"]
    assert [o["id"] for o in first] == [o["id"] for o in again]
    assert first[0]["state"] == "delivered"
    order_id = first[0]["id"]
    stamped = (await _order_row(world, order_id))["delivered_at"]
    assert stamped
    assert (await _orders(world)).json()["orders"][0]["delivered_at"] == stamped

    url = f"/api/executor-hosts/k8/orders/{order_id}/ack"
    one = await world.client.post(
        url, json={"result": "redeemed"}, headers=world.k8_auth
    )
    two = await world.client.post(
        url, json={"result": "redeemed"}, headers=world.k8_auth
    )
    assert one.status_code == two.status_code == 200
    assert (one.json()["duplicate"], two.json()["duplicate"]) == (False, True)
    # ack redeemed бронь не снимает: слот всё ещё занят, код по-прежнему жив.
    assert (await _order_row(world, order_id))["state"] == "delivered"
    assert (await _launch(world, await _task(world, "ещё"))).status_code == 409
    # Чужой заказ и несуществующий — 403; неизвестный результат — 422.
    k9_id = (await _orders(world, "k9")).json()["orders"][0]["id"]
    for target in (k9_id, 99999):
        denied = await world.client.post(
            f"/api/executor-hosts/k8/orders/{target}/ack",
            json={"result": "redeemed"},
            headers=world.k8_auth,
        )
        assert denied.status_code == 403
    bad = await world.client.post(url, json={"result": "banana"}, headers=world.k8_auth)
    assert bad.status_code == 422
    # Противоречащий повтор не закрывает работающий заказ.
    late = await world.client.post(
        url, json={"result": "redeem_failed"}, headers=world.k8_auth
    )
    assert late.status_code == 409
    assert (await _order_row(world, order_id))["state"] == "delivered"
    assert (
        await _launch(world, await _task(world, "после опоздавшего"))
    ).status_code == 409
    audit = await _rows(
        world.db,
        "SELECT action FROM admin_audit_log WHERE action LIKE 'executor_order_%'",
    )
    assert [a["action"] for a in audit].count("executor_order_delivered") == 2
    assert [a["action"] for a in audit].count("executor_order_acked") == 1


# ---------------------------------------------------------------------------
# AC-5
# ---------------------------------------------------------------------------


async def _burnt(world, order: dict, code: str) -> None:
    """Код сожжён: строки нет, поздний выкуп — тот же 401, что у неизвестного."""
    await world.db.commit()
    assert (
        await _rows(
            world.db,
            "SELECT 1 FROM chat_pair_codes WHERE id = ?",
            (order["pair_code_id"],),
        )
        == []
    )
    assert (await _redeem(world, code)).status_code == 401


async def test_any_close_burns_the_code(world, monkeypatch):
    """AC-5: истечение, redeem_failed, redeem_lost, смерть сессии; гонка — один исход."""
    # (1) ack redeem_failed: заказ закрыт, код сожжён, прогон aborted, задача open.
    t1 = await _task(world, "failed")
    assert (await _launch(world, t1)).status_code == 200
    o1 = (await _orders(world)).json()["orders"][0]
    c1 = _code_of(world, o1)
    full1 = await _order_row(world, o1["id"])
    failed = await world.client.post(
        f"/api/executor-hosts/k8/orders/{o1['id']}/ack",
        json={"result": "redeem_failed"},
        headers=world.k8_auth,
    )
    assert failed.status_code == 200 and failed.json()["state"] == "closed"
    closed1 = await _order_row(world, o1["id"])
    assert (
        closed1["close_reason"] == "redeem_failed" and closed1["code_ciphertext"] == ""
    )
    await _burnt(world, full1, c1)
    run1 = (
        await _rows(
            world.db, "SELECT outcome FROM executor_runs WHERE task_id=?", (t1,)
        )
    )[0]
    assert run1["outcome"] == "aborted"
    assert (await repo.get_task(world.db, t1))["status"] == "open"

    # (2) истечение невыкупленного: срок заказа вышел, код ещё жив — сжигается.
    t2 = await _task(world, "expired")
    assert (await _launch(world, t2)).status_code == 200
    o2 = (await _orders(world)).json()["orders"][0]
    c2 = _code_of(world, o2)
    full2 = await _order_row(world, o2["id"])
    await world.db.execute(
        "UPDATE executor_orders SET expires_at = datetime('now', '-1 minute') WHERE id=?",
        (o2["id"],),
    )
    await world.db.commit()
    assert await orders.sweep_executor_orders(world.db) == 1
    assert (await _order_row(world, o2["id"]))["close_reason"] == "expired"
    await _burnt(world, full2, c2)
    run2 = (
        await _rows(
            world.db, "SELECT outcome FROM executor_runs WHERE task_id=?", (t2,)
        )
    )[0]
    assert run2["outcome"] == "expired"

    # (3) redeem_lost при живой ИСПОЛЬЗОВАННОЙ сессии (#1825): 409, заказ и
    # задача не тронуты; задачу, которую держит живая сессия, никто не возвращает.
    t3 = await _task(world, "lost")
    lived = await _launch_redeem(world, t3)
    await _claim_and_start(world, t3, lived["session"])
    lost = await world.client.post(
        f"/api/executor-hosts/k8/orders/{lived['order']['id']}/ack",
        json={"result": "redeem_lost"},
        headers=world.k8_auth,
    )
    assert lost.status_code == 409
    assert (await _order_row(world, lived["order"]["id"]))["state"] == "redeemed"
    assert (await repo.get_task(world.db, t3))["status"] == "running"
    # Живая сессия держит задачу и под блокировкой записи: устаревшее решение
    # «вернуть» (сессия тем временем возобновилась) её не возвращает.
    assert (
        await cp.return_task_to_open(
            world.db,
            t3,
            from_status="running",
            note="stale",
            event_kind="stale_probe",
        )
        is False
    )
    await world.db.commit()
    assert (await repo.get_task(world.db, t3))["status"] == "running"
    # Работающий заказ закрывает итог хоста, а не redeem_lost: слот освобождается.
    ended = await world.client.post(
        f"/api/executor-hosts/k8/orders/{lived['order']['id']}/report",
        json={"outcome": "failed", "note": "прогон упал"},
        headers=world.k8_auth,
    )
    assert ended.status_code == 200, ended.text

    # (4) смерть сессии после выкупа: reaper возвращает задачу, заказ закрыт той же операцией.
    t4 = await _task(world, "reaper")
    alive = await _launch_redeem(world, t4)
    await _claim_and_start(world, t4, alive["session"])
    await world.db.execute(
        "UPDATE chat_pair_sessions SET expires_at = datetime('now', '-1 minute') "
        "WHERE bound_task_id = ?",
        (t4,),
    )
    await world.db.commit()
    assert await cp.release_expired_implementer_tasks(world.db) == [t4]
    await world.db.commit()
    closed4 = await _order_row(world, alive["order"]["id"])
    assert closed4["state"] == "closed" and closed4["close_reason"] == "session_gone"
    assert (await repo.get_task(world.db, t4))["status"] == "open"
    run4 = (
        await _rows(
            world.db, "SELECT outcome FROM executor_runs WHERE task_id=?", (t4,)
        )
    )[0]
    assert run4["outcome"] == "aborted"
    # Закрытие по устаревшему снимку заказа не берёт чужой исход: между чтением и
    # закрытием заказ успел быть выкуплен — условный UPDATE по состоянию отказывает,
    # код не сожжён, сессия жива.
    t5 = await _task(world, "устаревший снимок")
    stale_run = await _launch_redeem_keep_stale(world, t5)
    assert (
        await orders.close_order(
            world.db,
            stale_run["stale"],
            "expired",
            "expired",
            only_states=("pending", "delivered"),
        )
        is False
    )
    await world.db.commit()
    kept = await _order_row(world, stale_run["order"]["id"])
    assert kept["state"] == "redeemed" and kept["close_reason"] == ""
    assert await _rows(
        world.db,
        "SELECT 1 FROM chat_pair_sessions WHERE executor_run_id = ? "
        "AND revoked_at IS NULL",
        (stale_run["order"]["executor_run_id"],),
    )
    freed = await world.client.post(
        f"/api/executor-hosts/k8/orders/{stale_run['order']['id']}/ack",
        json={"result": "redeem_lost"},
        headers=world.k8_auth,
    )
    assert freed.status_code == 200
    await _reaper_failure_part(world, monkeypatch)
    # Слот свободен для следующего.
    assert (await _launch(world, await _task(world, "следующая"))).status_code == 200
    await _expiry_race_part(world)


async def _reaper_failure_part(world, monkeypatch):
    """AC-5: сбой reaper'а посреди возврата откатывает ВСЁ и не оставляет транзакцию."""
    task_id = await _task(world, "сбой reaper")
    alive = await _launch_redeem(world, task_id)
    await _claim_and_start(world, task_id, alive["session"])
    await world.db.execute(
        "UPDATE chat_pair_sessions SET expires_at = datetime('now', '-1 minute') "
        "WHERE bound_task_id = ?",
        (task_id,),
    )
    await world.db.commit()

    async def _boom(*_a, **_k):
        raise RuntimeError("закрытие заказа упало")

    with monkeypatch.context() as patch:
        patch.setattr(orders, "close_for_released_task", _boom)
        assert await cp.release_expired_implementer_tasks(world.db) == []
    assert not world.db.in_transaction  # ничего не осталось открытым
    await world.db.commit()
    assert (await repo.get_task(world.db, task_id))["status"] == "running"
    assert (await _order_row(world, alive["order"]["id"]))["state"] == "redeemed"
    # Очистка не уносит улики неудавшегося возврата: мёртвая сессия остаётся.
    with monkeypatch.context() as patch:
        patch.setattr(orders, "close_for_released_task", _boom)
        await cp.purge_expired(world.db)
    assert await _rows(
        world.db,
        "SELECT 1 FROM chat_pair_sessions WHERE bound_task_id = ?",
        (task_id,),
    )
    # Внутри чужой транзакции сбой откатывает именно эту задачу (SAVEPOINT).
    await world.db.execute("UPDATE tasks SET title = title WHERE id = ?", (task_id,))
    with monkeypatch.context() as patch:
        patch.setattr(orders, "close_for_released_task", _boom)
        assert await cp.release_expired_implementer_tasks(world.db) == []
    await world.db.commit()
    assert (await repo.get_task(world.db, task_id))["status"] == "running"
    # Следующий проход без сбоя доводит то же самое до конца.
    assert await cp.release_expired_implementer_tasks(world.db) == [task_id]
    await world.db.commit()
    assert (await repo.get_task(world.db, task_id))["status"] == "open"
    assert (await _order_row(world, alive["order"]["id"]))["state"] == "closed"


async def _expiry_race_part(world):
    """AC-5: истечение и выкуп параллельно — либо сессия, либо сожжённый код."""
    task_id = await _task(world, "гонка")
    assert (await _launch(world, task_id, "k9")).status_code == 200
    order = (await _orders(world, "k9")).json()["orders"][0]
    code = _code_of(world, order, "k9")
    await world.db.execute(
        "UPDATE executor_orders SET expires_at = datetime('now', '-1 minute') WHERE id=?",
        (order["id"],),
    )
    await world.db.commit()
    sweeper = await _other_connection(world)
    try:
        redeemed, _closed = await asyncio.gather(
            _redeem(world, code), orders.sweep_executor_orders(sweeper)
        )
    finally:
        await sweeper.close()

    row = await _order_row(world, order["id"])
    sessions = await _rows(
        world.db,
        "SELECT 1 FROM chat_pair_sessions WHERE executor_run_id = ?",
        (order["executor_run_id"],),
    )
    if redeemed.status_code == 200:
        assert row["state"] == "redeemed" and len(sessions) == 1
    else:
        assert redeemed.status_code == 401
        assert row["state"] == "closed" and row["close_reason"] == "expired"
        assert sessions == []


# ---------------------------------------------------------------------------
# AC-6
# ---------------------------------------------------------------------------


async def test_host_disable_burns_codes_and_sessions(world, monkeypatch):
    """AC-6: отключение — ключ, коды, заказы и сессии одной транзакцией."""
    t1 = await _task(world, "старая")
    live = await _launch_redeem(world, t1)
    await _claim_and_start(world, t1, live["session"])
    await _submit(world, t1)  # сдача закрыла заказ; сессия ещё жива
    t2 = await _task(world, "невыкупленная")
    assert (await _launch(world, t2)).status_code == 200
    pending = (await _orders(world)).json()["orders"][0]
    pending_code = _code_of(world, pending)
    full = await _order_row(world, pending["id"])

    off = await world.client.post(
        "/api/admin/executor-hosts/k8/disable", headers=world.human_auth
    )
    assert off.status_code == 200 and off.json()["enabled"] is False

    assert (await _orders(world)).status_code == 401  # ключ отозван
    await _burnt(world, full, pending_code)
    closed = await _order_row(world, pending["id"])
    assert closed["state"] == "closed" and closed["close_reason"] == "host_disabled"
    await world.db.commit()
    assert (
        await _rows(
            world.db,
            "SELECT 1 FROM chat_pair_sessions WHERE slot='k8' AND revoked_at IS NULL",
        )
        == []
    )
    used = await world.client.get(f"/api/tasks/{t1}", headers=live["session"])
    assert used.status_code == 401
    again = await _launch(world, await _task(world, "после"))
    assert again.status_code == 409 and "отключён" in again.text
    audit = await _rows(
        world.db, "SELECT 1 FROM admin_audit_log WHERE action='executor_host_disabled'"
    )
    assert len(audit) == 1
    await _disable_race_part(world, monkeypatch)
    await _reclaimed_task_part(world)


async def _reclaimed_task_part(world):
    """AC-6: отключение хоста не снимает захват другого агента с той же задачи."""
    held = await _task(world, "переехала к другому")
    # Третий хост (k8 и k9 к этому месту уже отключены); ключ шифрования — k8.
    created = await world.client.post(
        "/api/admin/executor-hosts",
        json={"name": "k10", "public_key_pem": world.keys.k8.public_pem},
        headers=world.human_auth,
    )
    assert created.status_code == 200, created.text
    world.k10_auth = {"Authorization": f"Bearer {created.json()['api_key']}"}
    world.keys.k10 = world.keys.k8
    live = await _launch_redeem(world, held, "k10")
    await _claim_and_start(world, held, live["session"])
    # Человек отпустил захват, задачу взял агент с ключом другого принципала.
    other = await admin_svc.create_principal(
        db=world.db, kind="agent", username="local-agent-1712", role_slug="agent"
    )
    other_key = await admin_svc.create_api_key(world.db, other["id"], name="local")
    await repo.update_task(
        world.db,
        held,
        status="open",
        claimed_by=None,
        claim_session_id=None,
        implementer_principal_id=None,
    )
    await world.db.commit()
    claimed = await world.client.post(
        f"/api/tasks/{held}/claim",
        json={"agent": "local-agent-1712", "session_id": "local-1"},
        headers={"Authorization": f"Bearer {other_key['plaintext_key']}"},
    )
    assert claimed.status_code == 200, claimed.text
    off = await world.client.post(
        "/api/admin/executor-hosts/k10/disable", headers=world.human_auth
    )
    assert off.status_code == 200
    task = await repo.get_task(world.db, held)
    assert task["status"] == "claimed" and task["claimed_by"] == "local-agent-1712"
    # И reaper сессий чужой захват не снимает: отозванная сессия старого хоста —
    # не повод забрать задачу у агента, который держит её теперь.
    assert await cp.release_expired_implementer_tasks(world.db) == []
    # Устаревшее чтение reaper'а (задачу успели взять заново) отбрасывается под
    # блокировкой записи: чужой держатель не совпадает с принципалом мёртвой сессии.
    acting = await cp.get_acting_agent(world.db)
    assert (
        await cp.return_task_to_open(
            world.db,
            held,
            from_status="claimed",
            note="stale",
            event_kind="stale_probe",
            holders=frozenset({int(acting["id"])}),
        )
        is False
    )
    await world.db.commit()
    task = await repo.get_task(world.db, held)
    assert task["status"] == "claimed" and task["claimed_by"] == "local-agent-1712"
    order = (
        await _rows(world.db, "SELECT * FROM executor_orders WHERE task_id=?", (held,))
    )[0]
    assert order["state"] == "closed"


async def _disable_race_part(world, monkeypatch):
    """AC-6: отключение — всё или ничего; параллельный выкуп — до него или отказ."""
    task_id = await _task(world, "гонка отключения")
    assert (await _launch(world, task_id, "k9")).status_code == 200
    order = (await _orders(world, "k9")).json()["orders"][0]
    code = _code_of(world, order, "k9")

    # Сбой посреди отключения откатывает ВСЁ: хост включён, ключ жив, код цел.
    async def _boom(*_a, **_k):
        raise RuntimeError("закрытие заказа упало")

    with monkeypatch.context() as patch:
        patch.setattr(orders, "close_order", _boom)
        with pytest.raises(RuntimeError):
            await orders.disable_host(world.db, "k9", by=world.human_id)
    await world.db.rollback()
    assert (await orders.get_host(world.db, "k9"))["enabled"] == 1
    assert (await _orders(world, "k9")).status_code == 200
    await world.db.commit()
    code_id = (await _order_row(world, order["id"]))["pair_code_id"]
    assert await _rows(
        world.db, "SELECT 1 FROM chat_pair_codes WHERE id = ?", (code_id,)
    )

    redeemed, off = await asyncio.gather(
        _redeem(world, code),
        world.client.post(
            "/api/admin/executor-hosts/k9/disable", headers=world.human_auth
        ),
    )

    assert off.status_code == 200
    assert redeemed.status_code in (200, 401)
    await world.db.commit()
    assert (
        await _rows(
            world.db,
            "SELECT 1 FROM chat_pair_sessions WHERE slot='k9' AND revoked_at IS NULL",
        )
        == []
    )
    assert (await _order_row(world, order["id"]))["state"] == "closed"
    assert (await _redeem(world, code)).status_code == 401


# ---------------------------------------------------------------------------
# AC-7
# ---------------------------------------------------------------------------


async def test_cloud_checks_leave_slot_runs_alone(world, monkeypatch):
    """AC-7: прогон слота без agent_id старше 450 с не «сорванная бронь»."""
    task_id = await _task(world)
    assert (await _launch(world, task_id)).status_code == 200
    await world.db.execute(
        "UPDATE executor_runs SET started_at = datetime('now', '-2 hours')"
    )
    await world.db.commit()

    assert await poll_executor_runs(world.db) == 0
    # Облачная строка (в том числе записанная до миграции, без канала) опрашивается.
    legacy = await _task(world, "облачная до миграции")
    await world.db.execute(
        "INSERT INTO executor_runs (task_id, agent_id, run_id, outcome) "
        "VALUES (?, 'bc-legacy', 'run-legacy', 'running')",
        (legacy,),
    )
    await world.db.commit()
    asked: list[str] = []

    async def _silent(agent_id: str, run_id: str, *_a):
        asked.append(agent_id)
        return None

    monkeypatch.setattr(cursor_cloud, "get_run", _silent)
    monkeypatch.setattr(cursor_cloud, "get_usage", _silent)
    assert await poll_executor_runs(world.db) == 1 and asked == ["bc-legacy"]
    assert (
        await _rows(
            world.db, "SELECT channel FROM executor_runs WHERE task_id=?", (legacy,)
        )
    )[0]["channel"] == "cloud"
    live = await el._live_run(world.db, task_id)
    assert live and "слот k8" in live
    run = (await _rows(world.db, "SELECT outcome, reason FROM executor_runs"))[0]
    assert run["outcome"] == "running"

    # Стоимость исполнителя — счёт облачного провайдера: прогон слота («без цены»
    # по построению) в него не попадает.
    from hub.services.executor_dispatch import executor_run_metrics
    from hub.services.metrics_scope import Scope

    metrics = await executor_run_metrics(world.db, Scope.relative(30))
    # Считается ровно одна строка — облачная; строка слота в счёт не входит.
    assert (metrics["runs"], metrics["runs_without_cents"]) == (1, 1)
    # Повторный запуск на ту же задачу (на другой хост) — отказ, прогон цел.
    again = await _launch(world, task_id, "k9")
    assert again.status_code == 409 and "уже идёт прогон" in again.text
    assert (await _rows(world.db, "SELECT outcome FROM executor_runs"))[0][
        "outcome"
    ] == "running"


# ---------------------------------------------------------------------------
# AC-8
# ---------------------------------------------------------------------------


async def test_reference_decrypt_redeems_to_slot_session(world, tmp_path):
    """AC-8: эталон расшифровывает, выкуп даёт сессию channel=slot, slot=k8."""
    task_id = await _task(world)
    assert (await _launch(world, task_id)).status_code == 200
    order = (await _orders(world)).json()["orders"][0]

    key_file = tmp_path / "host.pem"
    key_file.write_bytes(world.keys.k8.private_pem)
    out = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), "--key", str(key_file)],
        input=order["code_ciphertext"],
        capture_output=True,
        text=True,
        check=True,
    )
    code = out.stdout.strip()
    assert code == _code_of(world, order)

    text = _reference().fill_order_text(order["order_text"], code)
    assert code in text and "{{PAIR_CODE}}" not in text
    assert code not in order["order_text"]

    redeemed = await _redeem(world, code)
    assert redeemed.status_code == 200, redeemed.text
    session = (
        await _rows(
            world.db, "SELECT * FROM chat_pair_sessions WHERE kind='implementer'"
        )
    )[0]
    assert (session["channel"], session["slot"]) == ("slot", "k8")
    assert (session["bound_task_id"], session["bound_generation"]) == (task_id, 1)
    assert session["executor_run_id"] == order["executor_run_id"]
    assert redeemed.json()["token"].startswith("ahcp_")
    assert (await _order_row(world, order["id"]))["state"] == "redeemed"
    # Сессия работает как implementer: захват называет канал.
    await _claim_and_start(
        world, task_id, {"Authorization": f"Bearer {redeemed.json()['token']}"}
    )
    updates = await _rows(
        world.db, "SELECT content FROM task_updates WHERE task_id=?", (task_id,)
    )
    assert any("канал: k8" in u["content"] for u in updates)


# ---------------------------------------------------------------------------
# AC-9
# ---------------------------------------------------------------------------


async def test_host_admin_is_human_only_with_csrf(world, keys, monkeypatch):
    """AC-9: cookie без CSRF и агент — отказ; Bearer человека — хост, ключ один раз."""
    monkeypatch.setattr(config, "CSRF_MODE", "warn", raising=False)
    session = await admin_svc.create_browser_session(world.db, world.human_id)
    await world.db.commit()
    cookie = {"Cookie": f"{config.HUB_COOKIE_NAME}={session}", "Origin": HUB_URL}
    body = {"name": "k10", "public_key_pem": keys.k8.public_pem}

    bare = await world.client.post(
        "/api/admin/executor-hosts", json=body, headers=cookie
    )
    assert bare.status_code == 403, bare.text
    wrong = await world.client.post(
        "/api/admin/executor-hosts",
        json=body,
        headers={**cookie, "X-CSRF-Token": "not-the-token"},
    )
    assert wrong.status_code == 403
    off = await world.client.post(
        "/api/admin/executor-hosts/k9/disable", headers=cookie
    )
    assert off.status_code == 403
    agent = await world.client.post(
        "/api/admin/executor-hosts", json=body, headers=world.agent_auth
    )
    assert agent.status_code == 403
    assert (await orders.get_host(world.db, "k10")) is None
    assert (await orders.get_host(world.db, "k9"))["enabled"]

    good = await world.client.post(
        "/api/admin/executor-hosts", json=body, headers=world.human_auth
    )
    assert good.status_code == 200, good.text
    made = good.json()
    assert made["api_key"].startswith("ochk_") and made["name"] == "k10"
    listing = await world.client.get(
        "/api/admin/executor-hosts", headers=world.human_auth
    )
    assert "ochk_" not in listing.text and "api_key" not in listing.text
    await world.db.commit()
    dump = _dump(world.dsn)
    assert made["api_key"] not in dump
    audit = await _rows(
        world.db, "SELECT * FROM admin_audit_log WHERE action='executor_host_created'"
    )
    assert any(a["target_id"] == "k10" for a in audit)
    # Слабый ключ и дубликат — отказ.
    weak = await world.client.post(
        "/api/admin/executor-hosts",
        json={"name": "k11", "public_key_pem": "not a key"},
        headers=world.human_auth,
    )
    assert weak.status_code == 422
    small = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    small_pem = (
        small.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    short = await world.client.post(
        "/api/admin/executor-hosts",
        json={"name": "k11", "public_key_pem": small_pem},
        headers=world.human_auth,
    )
    assert short.status_code == 422 and "3072" in short.text
    dup = await world.client.post(
        "/api/admin/executor-hosts", json=body, headers=world.human_auth
    )
    assert dup.status_code == 409


# ---------------------------------------------------------------------------
# AC-10
# ---------------------------------------------------------------------------


async def test_launch_a_chosen_task_on_the_slot_from_the_card(world):
    """AC-10: кнопка на карточке запускает именно эту задачу; отказы понятны."""
    head = await _task(world, "голова очереди")
    chosen = await _task(world, "выбранная на карточке")
    session = await admin_svc.create_browser_session(world.db, world.human_id)
    await world.db.commit()
    cookie = {"Cookie": f"{config.HUB_COOKIE_NAME}={session}", "Origin": HUB_URL}
    token = token_for_session_cookie(session)

    page = await world.client.get(f"/tasks/{chosen}", headers=cookie)
    assert page.status_code == 200
    assert f"/tasks/{chosen}/web-executor-slot" in page.text
    assert "Запустить на k8" in page.text and "Запустить на k9" in page.text

    async def press(task_id: int, host: str = "k8", csrf: str | None = None):
        return await world.client.post(
            f"/tasks/{task_id}/web-executor-slot",
            data={"host": host, "csrf_token": token if csrf is None else csrf},
            headers=cookie,
            follow_redirects=False,
        )

    # Без верного CSRF — отказ, заказа нет.
    assert (await press(chosen, csrf="stale")).status_code == 403
    assert await _rows(world.db, "SELECT 1 FROM executor_orders") == []

    ok = await press(chosen)
    assert ok.status_code == 303, ok.text
    placed = await _rows(world.db, "SELECT task_id, generation FROM executor_orders")
    assert placed == [{"task_id": chosen, "generation": 1}]
    assert head != chosen

    # Слот занят — понятный отказ без выпуска.
    busy = await press(head)
    assert busy.status_code == 409 and "слот занят" in busy.text
    # Не open и чужой проект.
    await repo.update_task(world.db, head, status="running")
    foreign_project = await repo.create_project(world.db, slug="other-1712", name="x")
    stranger = await _task(world, "чужая")
    await repo.update_task(world.db, stranger, project_id=foreign_project)
    await world.db.commit()
    not_open = await press(head, "k9")
    assert not_open.status_code == 409 and "не open" in not_open.text
    # С карточки проект берётся у самой задачи: у чужого проекта запуск не включён.
    other = await press(stranger, "k9")
    assert other.status_code == 409 and "выключен" in other.text
    # А через REST проекта-названного-в-пути задача чужая.
    wrong = await _launch(world, stranger, "k9")
    assert wrong.status_code == 409 and "другого проекта" in wrong.text
    assert len(await _rows(world.db, "SELECT 1 FROM executor_orders")) == 1

    # После сдачи и return-to-work (open) та же кнопка выпускает следующее поколение.
    await _submit(world, chosen, generation=1)
    await repo.update_task(world.db, chosen, status="open")
    await world.db.commit()
    again = await press(chosen)
    assert again.status_code == 303, again.text
    gens = await _rows(
        world.db,
        "SELECT generation, state FROM executor_orders WHERE task_id=? ORDER BY id",
        (chosen,),
    )
    assert gens == [
        {"generation": 1, "state": "closed"},
        {"generation": 2, "state": "pending"},
    ]


# ---------------------------------------------------------------------------
# AC-11
# ---------------------------------------------------------------------------


async def test_host_reports_run_outcome_to_the_feed(world):
    """AC-11: итог хоста сохранён в заказе (в ленту не пишется, #1817), повтор идемпотентен."""
    task_id = await _task(world)
    other = await _task(world, "на k9")
    assert (await _launch(world, task_id)).status_code == 200
    assert (await _launch(world, other, "k9")).status_code == 200
    order = (await _orders(world)).json()["orders"][0]
    k9_order = (await _orders(world, "k9")).json()["orders"][0]
    url = f"/api/executor-hosts/k8/orders/{order['id']}/report"
    sha = "0123456789abcdef0123456789abcdef01234567"  # pragma: allowlist secret
    payload = {
        "outcome": "submitted",
        "sha": sha,
        "branch": f"task-{task_id}/work",
        "note": "всё хорошо",
    }
    status_before = (await repo.get_task(world.db, task_id))["status"]

    first = await world.client.post(url, json=payload, headers=world.k8_auth)
    assert first.status_code == 200, first.text
    assert first.json()["duplicate"] is False
    again = await world.client.post(url, json=payload, headers=world.k8_auth)
    assert again.status_code == 200 and again.json()["duplicate"] is True
    # Ветку задачи потом сменили (возврат и новый pair-start): повтор того же итога
    # по-прежнему идемпотентен, а не 422 по белому списку.
    await repo.update_task(world.db, task_id, branch=f"task-{task_id}/renamed")
    await world.db.commit()
    renamed = await world.client.post(url, json=payload, headers=world.k8_auth)
    assert renamed.status_code == 200 and renamed.json()["duplicate"] is True
    await repo.update_task(world.db, task_id, branch=f"task-{task_id}/work")
    await world.db.commit()

    # #1817: report хоста ленту не пишет (итог заказа пишет его закрытие); исход,
    # sha, ветка и заметка сохранены в заказе — их читают плашка и GET заказа.
    assert [
        u
        for u in await _rows(
            world.db, "SELECT * FROM task_updates WHERE task_id=?", (task_id,)
        )
        if "Итог прогона" in u["content"]
    ] == []
    stored = await _order_row(world, order["id"])
    assert (stored["report_outcome"], stored["report_sha"]) == ("submitted", sha)
    assert stored["report_branch"] == f"task-{task_id}/work"
    assert stored["report_note"] == "всё хорошо"
    assert (await repo.get_task(world.db, task_id))["status"] == status_before

    # Другой итог по тому же заказу — 409; чужой заказ — 403.
    changed = await world.client.post(
        url, json={**payload, "outcome": "failed"}, headers=world.k8_auth
    )
    assert changed.status_code == 409
    foreign = await world.client.post(
        f"/api/executor-hosts/k8/orders/{k9_order['id']}/report",
        json=payload,
        headers=world.k8_auth,
    )
    assert foreign.status_code == 403
    # Отчёт заказа, который хосту ещё не выдавали, — рано.
    third = await _task(world, "не выданная")
    await _submit(world, task_id)  # освободить слот k8
    assert (await _launch(world, third)).status_code == 200
    # Сдача принята: закрытие заказа пишет ровно одну итоговую запись, sha в ней виден.
    closing = [
        u["content"]
        for u in await _rows(
            world.db, "SELECT * FROM task_updates WHERE task_id=?", (task_id,)
        )
        if f"Заказ #{order['id']} " in u["content"]
    ]
    assert len(closing) == 1 and sha in closing[0] and "сдана" in closing[0].lower()
    fresh = (
        await _rows(
            world.db, "SELECT id FROM executor_orders WHERE task_id=?", (third,)
        )
    )[0]["id"]
    early = await world.client.post(
        f"/api/executor-hosts/k8/orders/{fresh}/report",
        json={"outcome": "failed"},
        headers=world.k8_auth,
    )
    assert early.status_code == 409
    # Итог принимается и после закрытия заказа сдачей (третий маршрут allowlist),
    # в том числе ПЕРВЫЙ итог по заказу.
    assert (await _order_row(world, order["id"]))["state"] == "closed"
    late_task = await _task(world, "итог после закрытия")
    await world.db.execute(
        "UPDATE executor_orders SET state = 'closed' WHERE host = 'k8' "
        "AND state <> 'closed'"
    )
    await world.db.commit()
    assert (await _launch(world, late_task, "k8")).status_code == 200
    late_order = (await _orders(world, "k8")).json()["orders"][0]
    await _submit(world, late_task)
    assert await orders.sweep_executor_orders(world.db) == 1
    assert (await _order_row(world, late_order["id"]))["state"] == "closed"
    first_after_close = await world.client.post(
        f"/api/executor-hosts/k8/orders/{late_order['id']}/report",
        json={"outcome": "submitted", "sha": sha},
        headers=world.k8_auth,
    )
    assert first_after_close.status_code == 200
    assert first_after_close.json()["duplicate"] is False
    await _failed_entry_part(world)
    await _notes_part(world, k9_order, other)


async def _other_order_code_part(world):
    """AC-11: итог старого заказа не печатает в ленту живой код нового заказа хоста."""
    await world.db.execute(
        "UPDATE executor_orders SET state = 'closed' WHERE host = 'k8' "
        "AND state <> 'closed'"
    )
    await world.db.commit()
    old_task = await _task(world, "старый заказ")
    assert (await _launch(world, old_task, "k8")).status_code == 200
    old_order = (await _orders(world, "k8")).json()["orders"][0]
    await _submit(world, old_task)  # сдача закрывает старый заказ, итога по нему нет
    new_task = await _task(world, "новый заказ")
    assert (await _launch(world, new_task, "k8")).status_code == 200
    new_order = (await _orders(world, "k8")).json()["orders"][0]
    assert (await _order_row(world, old_order["id"]))["state"] == "closed"
    live = _code_of(world, new_order).removeprefix("AH-")
    for body in (
        {"note": live},
        {"branch": live},
        {"note": f"{live[:4]} {live[4:]}"},
    ):
        leaked = await world.client.post(
            f"/api/executor-hosts/k8/orders/{old_order['id']}/report",
            json={"outcome": "failed", **body},
            headers=world.k8_auth,
        )
        assert leaked.status_code == 422, body
    # Сожжённый код (заказ закрыт настоящим ack redeem_failed, строки кода нет)
    # фильтр итога всё равно узнаёт — по хешу, оставшемуся в заказе.
    await world.db.execute(
        "UPDATE executor_orders SET state = 'closed' WHERE host = 'k8' "
        "AND state <> 'closed'"
    )
    await world.db.commit()
    burnt_task = await _task(world, "сожжённый")
    assert (await _launch(world, burnt_task, "k8")).status_code == 200
    burnt_order = (await _orders(world, "k8")).json()["orders"][0]
    burnt = _code_of(world, burnt_order).removeprefix("AH-")
    failed = await world.client.post(
        f"/api/executor-hosts/k8/orders/{burnt_order['id']}/ack",
        json={"result": "redeem_failed"},
        headers=world.k8_auth,
    )
    assert failed.status_code == 200
    assert (
        await _rows(
            world.db,
            "SELECT 1 FROM chat_pair_codes WHERE id = ?",
            ((await _order_row(world, burnt_order["id"]))["pair_code_id"],),
        )
        == []
    )
    for body in ({"note": burnt}, {"branch": burnt}, {"note": burnt.lower() + "z"}):
        again = await world.client.post(
            f"/api/executor-hosts/k8/orders/{burnt_order['id']}/report",
            json={"outcome": "failed", **body},
            headers=world.k8_auth,
        )
        assert again.status_code == 422, body
    clean = await world.client.post(
        f"/api/executor-hosts/k8/orders/{old_order['id']}/report",
        json={"outcome": "failed", "note": "без кода"},
        headers=world.k8_auth,
    )
    assert clean.status_code == 200
    await world.db.execute(
        "UPDATE executor_orders SET state = 'closed' WHERE host = 'k8' "
        "AND state <> 'closed'"
    )
    await world.db.commit()


async def _notes_part(world, order: dict, task_id: int):
    """AC-11: note — без кода и токенов, не длиннее лимита; outcome из списка."""
    code = _code_of(world, order, "k9")
    url = f"/api/executor-hosts/k9/orders/{order['id']}/report"

    async def post(**body):
        return await world.client.post(
            url, json={"outcome": "failed", **body}, headers=world.k9_auth
        )

    raw = code.removeprefix("AH-")
    for body in (
        {"branch": FAKE_SESSION_TOKEN},
        # Обходы эвристики: части короче 32, «хеш» без предела длины, чужая ветка.
        {"branch": FAKE_DOTTED},
        {"branch": FAKE_DASHED},
        {"sha": "a1b2c3d4" * 20},
        {"sha": "A1B2C3D"},
        {"sha": "abcdef1\n"},
        {"branch": f"task-{task_id}/rescue-5\n"},
        {"note": "ключ sk-" + "abcdefgh12345678"},
        {"note": "x-sk-" + "abcdefgh1234"},
        {"note": "mypassword=correcthorsebattery"},
        {"note": "xghp_ab"},
        {"branch": "task-1/xghp_ab"},
        {"note": "x-ahcp_ab"},
        {"note": "x_ahcp_ab"},
        {"note": "ключ=ghp_" + "abcdefgh12345678"},
        {"branch": f"task-{task_id + 1000}/rescue-5"},
        {"branch": f"task-{task_id}/rescue-5x"},
        {"branch": f"task-{task_id}/rescue-"},
        {"branch": f"task-{task_id}/other"},
        {"branch": "task-1712/" + "q" * 40},
        {"branch": "task-1712/" + "Xy" * 20},
        {"sha": "q" * 40},
        {"branch": "task-1712/ok", "sha": "z" * 33},
        {"branch": "task-1/" + FAKE_KEY_TAIL},
        {"branch": raw},
        {"branch": raw.lower()},
        {"sha": raw.lower()},
        {"note": raw + "x"},
        {"note": "x" + raw},
        {"note": f"{raw[:4]} {raw[4:]}"},
        {"note": f"{raw[:3]}-{raw[3:]}"},
        {"branch": f"task-1/{raw}"},
    ):
        leaked = await post(**body)
        assert leaked.status_code == 422, body
    # Код, разорванный по полям, и код ЧУЖОГО (нового) заказа того же хоста.
    for body in (
        {"branch": f"task-1/{raw[:4]}", "note": raw[4:]},
        {"note": raw[4:], "branch": f"task-1/{raw[:4]}"},
        {"sha": raw[:4].lower(), "note": raw[4:]},
    ):
        split = await post(**body)
        assert split.status_code == 422, body
    await _other_order_code_part(world)
    for note in (
        f"код был {code}",
        f"код был {code.removeprefix('AH-')}",
        "сессия ahcp_AbCdEfGhIjKlMnOp1234567890",
        "ключ ochk_AbCdEfGhIjKlMnOp1234567890",
        "Authorization: Bearer abcdefghijkl",
        "token=supersecretvalue",
        "x" * 501,
        "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8",  # pragma: allowlist secret
    ):
        refused = await post(note=note)
        assert refused.status_code == 422, note
    assert (await post(outcome="exploded")).status_code == 422
    assert (await post(sha="not-a-sha")).status_code == 422
    assert (await post(branch="a b;rm")).status_code == 422
    feed = [
        u
        for u in await _rows(
            world.db, "SELECT content FROM task_updates WHERE task_id=?", (task_id,)
        )
        if "Итог прогона" in u["content"]
    ]
    assert feed == []
    # Ветка задачи по виду секрет (слаг задаёт вызывающий pair-start): в ленту не идёт.
    sneaky = f"task-{task_id}/sk-" + "abcdefgh12345678"
    await repo.update_task(world.db, task_id, branch=sneaky)
    await world.db.commit()
    assert (await post(branch=sneaky)).status_code == 422
    await repo.update_task(world.db, task_id, branch=f"task-{task_id}/work")
    await world.db.commit()
    # Ветка со словом «sk-…» внутри (task-refactoring-…) — обычная ветка, а не
    # ключ: токен начинается после разделителя, не внутри слова.
    wordy = "task-refactoring-executor-orders"
    await repo.update_task(world.db, task_id, branch=wordy)
    await world.db.commit()
    # Обычная ветка с длинным слагом и хеш коммита проходят.
    ok = await post(
        note="a" * 500,
        branch=wordy,
        sha="0123456789abcdef0123456789abcdef01234567",  # pragma: allowlist secret
    )
    assert ok.status_code == 200, ok.text
    # failed закрывает заказ и освобождает слот, непотраченный код сожжён.
    closed = await _order_row(world, order["id"])
    assert closed["state"] == "closed" and closed["close_reason"] == "reported_failed"
    assert (
        await _launch(world, await _task(world, "после failed"), "k9")
    ).status_code == 200
    await _rescued_part(world)


async def _rescued_part(world):
    """AC-11: rescued закрывает так же, submitted — нет (закрывает сама сдача)."""
    await world.db.execute(
        "UPDATE executor_orders SET state = 'closed' WHERE host = 'k9' "
        "AND state <> 'closed'"
    )
    await world.db.commit()
    for outcome, closes in (("rescued", True), ("submitted", False)):
        task_id = await _task(world, f"итог {outcome}")
        assert (await _launch(world, task_id, "k9")).status_code == 200
        got = (await _orders(world, "k9")).json()["orders"][0]
        redeemed = await _redeem(world, _code_of(world, got, "k9"))
        assert redeemed.status_code == 200
        token = {"Authorization": f"Bearer {redeemed.json()['token']}"}
        posted = await world.client.post(
            f"/api/executor-hosts/k9/orders/{got['id']}/report",
            json={"outcome": outcome, "branch": f"task-{task_id}/rescue-20261016"}
            if outcome == "rescued"
            else {"outcome": outcome},
            headers=world.k9_auth,
        )
        assert posted.status_code == 200, posted.text
        row = await _order_row(world, got["id"])
        assert (row["state"] == "closed") is closes, (outcome, row["state"])
        if closes:
            assert row["close_reason"] == "reported_rescued"
            # Живая сессия закрытого прогона отозвана: слот не пересечётся с ней.
            assert (
                await world.client.get(f"/api/tasks/{task_id}", headers=token)
            ).status_code == 401
            assert row["code_ciphertext"] == ""
            run = (
                await _rows(
                    world.db,
                    "SELECT outcome FROM executor_runs WHERE id=?",
                    (got["executor_run_id"],),
                )
            )[0]
            assert run["outcome"] == "aborted"
        await world.db.execute(
            "UPDATE executor_orders SET state = 'closed' WHERE host = 'k9' "
            "AND state <> 'closed'"
        )
        await world.db.commit()


# ---------------------------------------------------------------------------
# Остальные места применения
# ---------------------------------------------------------------------------


async def test_the_launch_is_human_only_and_needs_manual_mode(world):
    """Запуск на слот — только человек; проект не в manual — отказ с причиной."""
    task_id = await _task(world)
    agent = await world.client.post(
        f"/api/projects/{world.slug}/executor-launch",
        json={"target": "slot:k8", "task_id": task_id},
        headers=world.agent_auth,
    )
    assert agent.status_code == 403
    host_key = await world.client.post(
        f"/api/projects/{world.slug}/executor-launch",
        json={"target": "slot:k8", "task_id": task_id},
        headers=world.k8_auth,
    )
    assert host_key.status_code == 403
    await repo.update_project(world.db, world.pid, gate_policy=json.dumps({}))
    await world.db.commit()
    off = await _launch(world, task_id)
    assert off.status_code == 409 and "выключен" in off.text
    await repo.update_project(
        world.db, world.pid, gate_policy=json.dumps({"executor_launch": "manual"})
    )
    await world.db.commit()
    for target in ("slot:", "slot:K 8", "grid", "cloud:k8"):
        bad = await world.client.post(
            f"/api/projects/{world.slug}/executor-launch",
            json={"target": target},
            headers=world.human_auth,
        )
        assert bad.status_code == 409, target
    unknown = await _launch(world, task_id, "k77")
    assert unknown.status_code == 409 and "не зарегистрирован" in unknown.text
    cloud_with_task = await world.client.post(
        f"/api/projects/{world.slug}/executor-launch",
        json={"target": "cloud", "task_id": task_id},
        headers=world.human_auth,
    )
    assert cloud_with_task.status_code == 409
    assert await _rows(world.db, "SELECT 1 FROM executor_orders") == []


async def test_a_closed_or_foreign_order_code_cannot_be_redeemed(world):
    """Выкуп проверяет заказ в своей операции: код чужого/закрытого заказа не идёт."""
    task_id = await _task(world)
    assert (await _launch(world, task_id)).status_code == 200
    order = (await _orders(world)).json()["orders"][0]
    code = _code_of(world, order)
    # Закрытие мимо сервиса (как если бы строка закрылась, а код остался):
    # условный UPDATE выкупа всё равно не пропустит.
    await world.db.execute(
        "UPDATE executor_orders SET state='closed' WHERE id=?", (order["id"],)
    )
    await world.db.commit()
    assert (await _redeem(world, code)).status_code == 401
    assert await _rows(world.db, "SELECT 1 FROM chat_pair_sessions") == []
    # И то же при отключённом принципале хоста.
    other = await _task(world, "вторая")
    await world.db.execute("DELETE FROM executor_orders")
    await world.db.commit()
    assert (await _launch(world, other, "k9")).status_code == 200
    order9 = (await _orders(world, "k9")).json()["orders"][0]
    code9 = _code_of(world, order9, "k9")
    await world.db.execute(
        "UPDATE principals SET status='disabled' WHERE username='executor-host-k9'"
    )
    await world.db.commit()
    assert (await _redeem(world, code9)).status_code == 401


async def test_cloud_launch_keeps_its_default_behaviour(world):
    """Облачный запуск без target и task_id — как до #1712: отказ с причиной, провайдер не зван."""
    project = dict(await repo.get_project(world.db, world.pid))
    result = await el.launch_executor(
        world.db, project, issuer_principal_id=world.human_id
    )
    assert not result.launched and result.reason.startswith(el.REASON_NO_OBSERVATION)


async def test_a_slot_launch_without_task_id_takes_the_queue_head(world):
    """Без task_id слот берёт голову очереди F1, как облако; веб-форма проекта — то же."""
    task_id = await _task(world, "кандидат очереди")
    await repo.update_task(
        world.db,
        task_id,
        affected_areas=json.dumps(["hub/x.py"]),
        dor_passed=1,
    )
    await world.db.commit()
    session = await admin_svc.create_browser_session(world.db, world.human_id)
    await world.db.commit()
    cookie = {"Cookie": f"{config.HUB_COOKIE_NAME}={session}", "Origin": HUB_URL}

    pressed = await world.client.post(
        f"/projects/{world.slug}/web-executor-launch",
        data={"target": "slot:k8", "csrf_token": token_for_session_cookie(session)},
        headers=cookie,
        follow_redirects=False,
    )

    assert pressed.status_code == 303, pressed.text
    placed = await _rows(world.db, "SELECT task_id, host FROM executor_orders")
    assert placed == [{"task_id": task_id, "host": "k8"}]
    assert (await _launch(world, None, "k9")).status_code == 409  # очередь пуста


async def test_an_accepted_slot_submission_releases_the_runs_claim(world):
    """#1783 AC-1 (слот): принятая сдача закрывает заказ, и в ТОЙ ЖЕ транзакции
    сессии прогона отозваны, слот закрыт, захват снят по executor_run_id;
    статус review не тронут, автор сдачи (implementer_principal_id) остаётся."""
    task_id = await _task(world, "захват слота после сдачи")
    alive = await _launch_redeem(world, task_id)
    await _claim_and_start(world, task_id, alive["session"])
    held = (await _rows(world.db, "SELECT * FROM tasks WHERE id = ?", (task_id,)))[0]
    run = (
        await _rows(
            world.db, "SELECT id FROM executor_runs WHERE task_id = ?", (task_id,)
        )
    )[0]
    assert held["executor_run_id"] == run["id"] and held["claimed_by"]

    await _submit(world, task_id, generation=1)
    await orders.settle(world.db)
    await world.db.commit()

    after = (await _rows(world.db, "SELECT * FROM tasks WHERE id = ?", (task_id,)))[0]
    assert after["status"] == "review"
    assert (
        after["claimed_by"],
        after["claim_session_id"],
        after["executor_run_id"],
    ) == (None, None, None)
    assert after["implementer_principal_id"] == held["implementer_principal_id"]
    sessions = await _rows(
        world.db,
        "SELECT revoked_at FROM chat_pair_sessions WHERE executor_run_id = ?",
        (run["id"],),
    )
    assert sessions and all(s["revoked_at"] for s in sessions)
    slots = await _rows(
        world.db, "SELECT released_at FROM executor_slots WHERE task_id = ?", (task_id,)
    )
    assert slots and all(s["released_at"] for s in slots)


async def _sessions_of(world, run_id: int) -> list[dict]:
    await world.db.commit()
    return await _rows(
        world.db,
        "SELECT * FROM chat_pair_sessions WHERE executor_run_id = ?",
        (run_id,),
    )


async def _ack(world, order_id: int, result: str):
    return await world.client.post(
        f"/api/executor-hosts/k8/orders/{order_id}/ack",
        json={"result": result},
        headers=world.k8_auth,
    )


async def test_redeem_lost_revokes_unused_session(world):
    """AC-4: redeem_lost до первого использования токена закрывает заказ И отзывает сессию.

    Раньше close_order сжигал только непотраченный код: потраченный код оставлял
    живую сессию при закрытом заказе — слот свободен, исполнитель жив. После
    первого использования токена тот же ack — 409, заказ и сессия не тронуты.
    """
    # До использования: выкуплен, токен ни разу не предъявлен.
    unused_task = await _task(world, "токен не использован")
    unused = await _launch_redeem(world, unused_task)
    run_id = unused["order"]["executor_run_id"]
    before = await _sessions_of(world, run_id)
    assert len(before) == 1 and before[0]["revoked_at"] is None
    lost = await _ack(world, unused["order"]["id"], "redeem_lost")
    assert lost.status_code == 200, lost.text
    order = await _order_row(world, unused["order"]["id"])
    assert order["state"] == "closed" and order["close_reason"] == "redeem_lost"
    after = await _sessions_of(world, run_id)
    assert len(after) == 1 and after[0]["revoked_at"] is not None
    # Слот свободен, и токен действительно недействителен.
    revoked = await world.client.get("/api/whoami", headers=unused["session"])
    assert revoked.status_code == 401
    assert (await _launch(world, await _task(world, "следующая"))).status_code == 200

    # После использования: токен предъявлен (claim) — отказ 409, всё как было.
    used_task = await _task(world, "токен использован")
    used = await _launch_redeem(world, used_task, "k9")
    await _claim_and_start(world, used_task, used["session"])
    used_run = used["order"]["executor_run_id"]
    sessions = await _sessions_of(world, used_run)
    refused = await world.client.post(
        f"/api/executor-hosts/k9/orders/{used['order']['id']}/ack",
        json={"result": "redeem_lost"},
        headers=world.k9_auth,
    )
    assert refused.status_code == 409, refused.text
    kept = await _order_row(world, used["order"]["id"])
    assert kept["state"] == "redeemed" and kept["ack_result"] == ""
    assert await _sessions_of(world, used_run) == sessions
    assert (await repo.get_task(world.db, used_task))["status"] == "running"


async def _phase(world, order_id: int, phase: str, host: str = "k8", auth=None):
    return await world.client.post(
        f"/api/executor-hosts/{host}/orders/{order_id}/phase",
        json={"phase": phase},
        headers=auth or getattr(world, f"{host}_auth"),
    )


async def test_order_phase_contract(world, monkeypatch):
    """#1817 AC-2: фазы хоста — только после redeemed, вперёд, без записей в ленту."""
    from hub import executor_host_api

    task_id = await _task(world, "фазы")
    assert (await _launch(world, task_id)).status_code == 200
    order = (await _orders(world)).json()["orders"][0]  # выдан, не выкуплен
    feed_before = await _rows(world.db, "SELECT 1 FROM task_updates")
    # До фактического redeemed: 409, фаза не записана.
    assert (await _phase(world, order["id"], "vm_starting")).status_code == 409
    redeemed = await _redeem(world, _code_of(world, order))
    assert redeemed.status_code == 200
    # Мусорная фаза — 422.
    assert (await _phase(world, order["id"], "teleport")).status_code == 422
    # Первая, затем вперёд с пропуском.
    first = await _phase(world, order["id"], "vm_starting")
    assert first.status_code == 200 and first.json()["phase"] == "vm_starting"
    skip = await _phase(world, order["id"], "critic")
    assert skip.status_code == 200 and skip.json()["duplicate"] is False
    # Повтор той же — 200 без изменений; назад — 409.
    again = await _phase(world, order["id"], "critic")
    assert again.status_code == 200 and again.json()["duplicate"] is True
    assert (await _phase(world, order["id"], "agent_running")).status_code == 409
    assert (await _order_row(world, order["id"]))["phase"] == "critic"
    # Чужой хост: ключ k9 на пути k8 и заказ k8 по пути k9.
    assert (
        await _phase(world, order["id"], "submitting", "k8", world.k9_auth)
    ).status_code == 403
    assert (await _phase(world, order["id"], "submitting", "k9")).status_code == 403
    # Ни одна фаза не пишет в ленту.
    assert (
        len(await _rows(world.db, "SELECT 1 FROM task_updates"))
        == feed_before.__len__()
    )
    # Квота на хост: 429 сверх лимита, фаза не меняется.
    executor_host_api._phase_limiter._buckets.clear()
    monkeypatch.setattr(executor_host_api, "PHASE_MAX", 2)
    codes = [(await _phase(world, order["id"], "critic")).status_code for _ in range(4)]
    assert codes[:2] == [200, 200] and 429 in codes
    executor_host_api._phase_limiter._buckets.clear()
    # После отчёта и после закрытия — 409.
    done = await world.client.post(
        f"/api/executor-hosts/k8/orders/{order['id']}/report",
        json={"outcome": "failed", "note": "упал"},
        headers=world.k8_auth,
    )
    assert done.status_code == 200, done.text
    assert (await _phase(world, order["id"], "submitting")).status_code == 409


async def _failed_entry_part(world):
    """#1817: failed/rescued — единственная запись закрытия несёт sha и ветку из report."""
    for outcome in ("failed", "rescued"):
        await world.db.execute(
            "UPDATE executor_orders SET state = 'closed' WHERE host = 'k8' "
            "AND state <> 'closed'"
        )
        await world.db.commit()
        task_id = await _task(world, f"{outcome} с sha")
        assert (await _launch(world, task_id, "k8")).status_code == 200
        order = (await _orders(world, "k8")).json()["orders"][0]
        branch = (
            f"task-{task_id}/work"
            if outcome == "failed"
            else f"task-{task_id}/rescue-3"
        )
        sha = "fedcba9876543210fedc"  # pragma: allowlist secret
        done = await world.client.post(
            f"/api/executor-hosts/k8/orders/{order['id']}/report",
            json={"outcome": outcome, "sha": sha, "branch": branch, "note": "итог"},
            headers=world.k8_auth,
        )
        assert done.status_code == 200, done.text
        entries = [
            u["content"]
            for u in await _rows(
                world.db, "SELECT * FROM task_updates WHERE task_id=?", (task_id,)
            )
            if f"Заказ #{order['id']} " in u["content"]
            or "Итог прогона" in u["content"]
        ]
        assert len(entries) == 1, entries
        assert sha in entries[0] and branch in entries[0] and outcome in entries[0]
