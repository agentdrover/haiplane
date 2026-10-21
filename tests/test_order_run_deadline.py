"""Срок прогона заказа хосту по размеру задачи (#1960).

Срок — снимок ``executor_orders.run_seconds`` при выпуске заказа; TTL сессии
слота берётся из него при выкупе. Остальные коды TTL не меняют. Время сдвигаем
в метках SQLite (``datetime('now')``), а не в часах Python.
"""

from __future__ import annotations

import logging
import aiosqlite
import pytest

from hub import brand, config
from hub import repository as repo
from hub.db import _SCHEMA, _migrate
from hub.services import chat_pair as cp
from hub.services import effective_policy
from hub.services import executor_orders as orders
from hub.services import launch_feedback
from tests import test_executor_orders as _eo
from tests.test_executor_orders import (
    _claim_and_start,
    _code_of,
    _launch,
    _order_row,
    _orders,
    _redeem,
    _rows,
    _task,
)

world = _eo.world
keys = _eo.keys
_clean_limiter = _eo._clean_limiter
_no_cloud = _eo._no_cloud

ENV = brand.ENV_PREFIX + "EXECUTOR_RUN_SECONDS"
TABLE = "XS=7200,S=7200,M=10800,L=14400,XL=14400"
SIZES = ["XS", "S", "M", "L", "XL", None]


async def _sized_task(world, size: str | None, title: str = "срок") -> int:
    task_id = await _task(world, f"{title} {size}")
    if size:
        await repo.update_task(world.db, task_id, size=size)
        await world.db.commit()
    return task_id


async def _free_slots(world) -> None:
    await world.db.execute("UPDATE executor_orders SET state = 'closed'")
    await world.db.commit()


async def _issue(world, size: str | None, host: str = "k8") -> dict:
    """Заказ на слот для задачи размера ``size``: строка заказа."""
    await _free_slots(world)
    task_id = await _sized_task(world, size)
    resp = await _launch(world, task_id, host)
    assert resp.status_code == 200, resp.text
    rows = await _rows(
        world.db, "SELECT * FROM executor_orders WHERE task_id = ?", (task_id,)
    )
    return rows[0]


async def _span(world, order_id: int, start: str, end: str) -> int:
    """Секунды между двумя метками: колонка заказа и колонка сессии."""
    await world.db.commit()
    rows = await _rows(
        world.db,
        f"SELECT CAST(ROUND((julianday(s.expires_at) - julianday(o.{start})) * 86400) "
        "AS INTEGER) AS span, s.expires_at AS e FROM executor_orders o "
        "JOIN chat_pair_sessions s ON s.id = o.session_id WHERE o.id = ?",
        (order_id,),
    )
    assert end == "expires_at"
    return rows[0]["span"]


# ---------------------------------------------------------------------------
# AC-1 .. AC-3: таблица и разбор env
# ---------------------------------------------------------------------------


async def test_default_table_is_7200_for_every_size(world, monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    for size in SIZES:
        row = await _issue(world, size)
        assert row["run_seconds"] == 7200, size
    data = (
        await world.client.get(
            f"/api/projects/{world.slug}/effective-policy", headers=world.human_auth
        )
    ).json()
    run = data["server"]["executor_run"]
    assert run["source"] == "default"
    assert set(run["table"].values()) == {7200}


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        ("XS", 7200),
        ("S", 7200),
        ("M", 10800),
        ("L", 14400),
        ("XL", 14400),
        (None, 7200),
    ],
)
async def test_env_table_by_size(world, monkeypatch, size, expected):
    monkeypatch.setenv(ENV, TABLE)
    row = await _issue(world, size)
    assert row["run_seconds"] == expected
    data = (
        await world.client.get(
            f"/api/projects/{world.slug}/effective-policy", headers=world.human_auth
        )
    ).json()
    assert data["server"]["executor_run"]["source"] == "env"


BAD = [
    "M=99999",
    "M=0",
    "M=-5",
    "M=3599",
    "M=abc",
    "Q=7200",
    "M=10800,M=7200",
    "M=10800,",
    "garbage",
]


@pytest.mark.parametrize("raw", BAD)
async def test_env_parsing_rules(world, monkeypatch, caplog, raw):
    from hub.services import order_deadline

    monkeypatch.setenv(ENV, raw)
    with caplog.at_level(logging.WARNING):
        table = order_deadline.run_table()
    assert table.source == "default" and table.error
    assert set(table.seconds.values()) == {7200}
    assert any("EXECUTOR_RUN_SECONDS" in r.getMessage() for r in caplog.records)
    row = await _issue(world, "M")  # запуск не отказывает
    assert row["run_seconds"] == 7200
    data = (
        await world.client.get(
            f"/api/projects/{world.slug}/effective-policy", headers=world.human_auth
        )
    ).json()
    assert data["server"]["executor_run"]["error"]
    for value in table.seconds.values():
        assert 3600 <= value <= 14400


async def test_env_parsing_partial_valid_table(world, monkeypatch):
    from hub.services import order_deadline

    monkeypatch.setenv(ENV, "M=10800")
    table = order_deadline.run_table()
    assert table.source == "env" and not table.error
    assert table.seconds["M"] == 10800
    assert {v for k, v in table.seconds.items() if k != "M"} == {7200}
    assert (await _issue(world, "M"))["run_seconds"] == 10800
    assert (await _issue(world, "S"))["run_seconds"] == 7200


# ---------------------------------------------------------------------------
# AC-4: не-слотовые сессии
# ---------------------------------------------------------------------------


async def _ttl_of_last_session(world) -> int:
    rows = await _rows(
        world.db,
        "SELECT CAST(ROUND((julianday(expires_at) - julianday(created_at)) * 86400)"
        " AS INTEGER) AS span FROM chat_pair_sessions ORDER BY id DESC LIMIT 1",
    )
    return rows[0]["span"]


async def test_non_slot_sessions_keep_default_ttl(world, monkeypatch):
    monkeypatch.setenv(ENV, TABLE)
    task_id = await _sized_task(world, "M", "не слот")
    cloud, _ = await cp.issue_run_code(
        world.db, task_id, 1, issued_by_principal_id=world.human_id
    )
    await world.db.commit()
    intake, _ = await cp.issue_code(world.db, world.human_id)
    manual, _ = await cp.issue_code(
        world.db,
        world.human_id,
        kind="implementer",
        bound_task_id=task_id,
        bound_generation=1,
    )
    for name, code in (("cloud", cloud), ("intake", intake), ("manual", manual)):
        resp = await _redeem(world, code)
        assert resp.status_code == 200, (name, resp.text)
        span = await _ttl_of_last_session(world)
        assert abs(span - config.CHAT_PAIR_TTL_SECONDS) <= 5, (name, span)


# ---------------------------------------------------------------------------
# AC-5, AC-6: снимок и оба маршрута выкупа
# ---------------------------------------------------------------------------


async def test_run_seconds_is_snapshotted_at_issue(world, monkeypatch):
    monkeypatch.setenv(ENV, TABLE)
    row = await _issue(world, "M")
    assert row["run_seconds"] == 10800
    await repo.update_task(world.db, row["task_id"], size="S")
    await world.db.commit()
    monkeypatch.delenv(ENV)
    order = (await _orders(world)).json()["orders"][0]
    assert (await _redeem(world, _code_of(world, order))).status_code == 200
    span = await _span(world, row["id"], "first_redeemed_at", "expires_at")
    assert abs(span - 10800) <= 5


@pytest.mark.parametrize(("stored", "shared_ttl"), [(99999, 20000), (100, 1000)])
async def test_a_broken_order_row_falls_back_to_the_old_slot_term(
    world, monkeypatch, stored, shared_ttl
):
    """Битый run_seconds в заказе даёт 7200, а не общий TTL вне границ."""
    monkeypatch.setattr(config, "CHAT_PAIR_TTL_SECONDS", shared_ttl)
    row = await _issue(world, "M")
    await world.db.execute(
        "UPDATE executor_orders SET run_seconds = ? WHERE id = ?", (stored, row["id"])
    )
    await world.db.commit()
    order = (await _orders(world)).json()["orders"][0]
    assert (await _redeem(world, _code_of(world, order))).status_code == 200
    span = await _span(world, row["id"], "first_redeemed_at", "expires_at")
    assert abs(span - 7200) <= 5, span


async def _span_retry(world, order_id: int) -> int:
    rows = await _rows(
        world.db,
        "SELECT CAST(ROUND((julianday(retry_until) - julianday(first_redeemed_at)) "
        "* 86400) AS INTEGER) AS span FROM executor_orders WHERE id = ?",
        (order_id,),
    )
    return rows[0]["span"]


async def test_both_redeem_routes_and_rotation_keep_expiry(world, monkeypatch):
    monkeypatch.setenv(ENV, TABLE)
    public = await _issue(world, "M", "k8")
    listed = (await _orders(world, "k8")).json()["orders"][0]
    assert listed["run_seconds"] == 10800
    assert (await _redeem(world, _code_of(world, listed))).status_code == 200
    assert (
        abs(await _span(world, public["id"], "first_redeemed_at", "expires_at") - 10800)
        <= 5
    )
    assert abs(await _span_retry(world, public["id"]) - 600) <= 5

    hostrow = await _issue(world, "M", "k9")
    got = (await _orders(world, "k9")).json()["orders"][0]
    assert got["run_seconds"] == 10800
    code = _code_of(world, got, "k9")
    url = f"/api/executor-hosts/k9/orders/{hostrow['id']}/redeem"
    first = await world.client.post(url, json={"code": code}, headers=world.k9_auth)
    assert first.status_code == 200, first.text
    assert (
        abs(
            await _span(world, hostrow["id"], "first_redeemed_at", "expires_at") - 10800
        )
        <= 5
    )
    assert abs(await _span_retry(world, hostrow["id"]) - 600) <= 5
    before = await _order_row(world, hostrow["id"])
    sess_before = (
        await _rows(
            world.db,
            "SELECT * FROM chat_pair_sessions WHERE id = ?",
            (before["session_id"],),
        )
    )[0]
    again = await world.client.post(url, json={"code": code}, headers=world.k9_auth)
    assert again.status_code == 200, again.text
    after = await _order_row(world, hostrow["id"])
    sess_after = (
        await _rows(
            world.db,
            "SELECT * FROM chat_pair_sessions WHERE id = ?",
            (after["session_id"],),
        )
    )[0]
    assert after["retry_until"] == before["retry_until"]
    assert sess_after["expires_at"] == sess_before["expires_at"]
    assert sess_after["token_version"] == sess_before["token_version"] + 1


# ---------------------------------------------------------------------------
# AC-7, AC-8: живой токен после двух часов, смерть в срок, rescued после срока
# ---------------------------------------------------------------------------


async def _redeemed_run(world, size: str = "M", host: str = "k8") -> dict:
    row = await _issue(world, size, host)
    got = (await _orders(world, host)).json()["orders"][0]
    redeemed = await _redeem(world, _code_of(world, got, host))
    assert redeemed.status_code == 200, redeemed.text
    session = {"Authorization": f"Bearer {redeemed.json()['token']}"}
    await _claim_and_start(world, row["task_id"], session)
    return {"row": row, "session": session, "task_id": row["task_id"]}


async def _task_row(world, task_id: int) -> dict:
    await world.db.commit()
    return (await _rows(world.db, "SELECT * FROM tasks WHERE id = ?", (task_id,)))[0]


async def _pass(world) -> tuple[int, list[int]]:
    closed = await orders.sweep_executor_orders(world.db)
    released = await cp.release_expired_implementer_tasks(world.db)
    await world.db.commit()
    return closed, released


async def test_token_lives_past_two_hours_and_dies_at_its_deadline(world, monkeypatch):
    monkeypatch.setenv(ENV, TABLE)
    run = await _redeemed_run(world)
    task_id, oid = run["task_id"], run["row"]["id"]
    # с выкупа прошло 7300 с: осталось 3500 из 10800
    await world.db.execute(
        "UPDATE executor_orders SET first_redeemed_at = datetime('now', '-7300 seconds') "
        "WHERE id = ?",
        (oid,),
    )
    await world.db.execute(
        "UPDATE chat_pair_sessions SET created_at = datetime('now', '-7300 seconds'), "
        "expires_at = datetime('now', '+3500 seconds') WHERE executor_run_id = ?",
        (run["row"]["executor_run_id"],),
    )
    await world.db.commit()
    alive = await world.client.get(f"/api/tasks/{task_id}", headers=run["session"])
    assert alive.status_code == 200, alive.text
    assert await _pass(world) == (0, [])
    assert (await _order_row(world, oid))["state"] == "redeemed"
    held = await _task_row(world, task_id)
    assert held["status"] == "running" and held["claimed_by"]

    await world.db.execute(
        "UPDATE chat_pair_sessions SET expires_at = datetime('now', '-1 second') "
        "WHERE executor_run_id = ?",
        (run["row"]["executor_run_id"],),
    )
    await world.db.commit()
    dead = await world.client.get(f"/api/tasks/{task_id}", headers=run["session"])
    assert dead.status_code == 401
    # сначала жнец один: сессия истекла сама (не отозвана) - задача уходит в open
    assert await cp.release_expired_implementer_tasks(world.db) == [task_id]
    await world.db.commit()
    assert (await _task_row(world, task_id))["status"] == "open"
    await orders.sweep_executor_orders(world.db)
    await world.db.commit()
    row = await _order_row(world, oid)
    assert row["state"] == "closed" and row["close_reason"] == "session_gone"
    after = await _task_row(world, task_id)
    assert after["status"] == "open" and not after["claimed_by"]
    assert await _pass(world) == (0, [])


async def test_rescued_report_after_expiry_keeps_time_reason(world, monkeypatch):
    monkeypatch.setenv(ENV, TABLE)
    for reason_code, host in (("critic_time_exhausted", "k8"), ("timeout", "k9")):
        run = await _redeemed_run(world, "M", host)
        task_id, oid = run["task_id"], run["row"]["id"]
        await world.db.execute(
            "UPDATE chat_pair_sessions SET expires_at = datetime('now', '-1 second') "
            "WHERE executor_run_id = ?",
            (run["row"]["executor_run_id"],),
        )
        await world.db.commit()
        await _pass(world)
        assert (await _order_row(world, oid))["close_reason"] == "session_gone"
        branch = f"task-{task_id}/rescue-20261021"
        resp = await world.client.post(
            f"/api/executor-hosts/{host}/orders/{oid}/report",
            json={
                "outcome": "rescued",
                "branch": branch,
                "reason_code": reason_code,
                "sha": "0123456789abcdef0123456789abcdef01234567",  # pragma: allowlist secret
            },
            headers=getattr(world, f"{host}_auth"),
        )
        assert resp.status_code == 200, resp.text
        row = await _order_row(world, oid)
        assert row["report_reason_code"] == reason_code
        assert row["report_branch"] == branch
        # #1817: report ленту не пишет; причину показывает плашка/вид заказа
        # (close_reason_text по сохранённому коду хоста).
        shown = launch_feedback.close_reason_text(
            row["close_reason"], row["report_reason_code"]
        )
        assert launch_text(reason_code) in shown and "session" not in shown
        live = await _rows(
            world.db,
            "SELECT 1 FROM chat_pair_sessions WHERE executor_run_id = ? "
            "AND revoked_at IS NULL AND expires_at > datetime('now')",
            (run["row"]["executor_run_id"],),
        )
        assert not live


def launch_text(code: str) -> str:
    from hub.services import launch_feedback

    return launch_feedback.reason_code_text(code)


# ---------------------------------------------------------------------------
# AC-9: миграция
# ---------------------------------------------------------------------------

STEP = "add_executor_orders_run_seconds"


async def _cols(conn) -> dict:
    rows = await conn.execute_fetchall("PRAGMA table_info(executor_orders)")
    return {r["name"]: dict(r) for r in rows}


async def test_migration_old_and_fresh_schema():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    try:
        await conn.executescript(_SCHEMA)
        await _migrate(conn)
        cols = await _cols(conn)
        assert cols["run_seconds"]["notnull"] == 1
        assert cols["run_seconds"]["dflt_value"] == "7200"
        # старая схема: колонки нет, шаг не применён, есть заказ и живая сессия
        await conn.execute("PRAGMA foreign_keys = OFF")
        await conn.execute("ALTER TABLE executor_orders DROP COLUMN run_seconds")
        await conn.execute("DELETE FROM _migrations WHERE name = ?", (STEP,))
        await conn.execute(
            "INSERT INTO executor_orders (host, task_id, generation, executor_run_id, "
            "expires_at) VALUES ('k8', 1, 1, 1, datetime('now'))"
        )
        await conn.execute(
            "INSERT INTO chat_pair_sessions (principal_id, token_hash, expires_at) "
            "VALUES (1, 'h', '2030-01-01 00:00:00')"
        )
        await conn.commit()
        assert "run_seconds" not in await _cols(conn)
        for _ in range(2):
            await _migrate(conn)
            rows = await conn.execute_fetchall(
                "SELECT run_seconds FROM executor_orders"
            )
            assert [r["run_seconds"] for r in rows] == [7200]
            live = await conn.execute_fetchall(
                "SELECT expires_at FROM chat_pair_sessions"
            )
            assert live[0]["expires_at"] == "2030-01-01 00:00:00"
        applied = await conn.execute_fetchall(
            "SELECT name FROM _migrations WHERE name = ?", (STEP,)
        )
        assert len(applied) == 1
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# AC-10, AC-11: сводка и лента
# ---------------------------------------------------------------------------


async def test_effective_policy_server_block_shows_run_table(world, monkeypatch):
    monkeypatch.setenv(ENV, TABLE)
    resp = await world.client.get(
        f"/api/projects/{world.slug}/effective-policy", headers=world.human_auth
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    run = data["server"]["executor_run"]
    assert run["table"] == {
        "XS": 7200,
        "S": 7200,
        "M": 10800,
        "L": 14400,
        "XL": 14400,
        "none": 7200,
    }
    assert (run["ceiling"], run["floor"], run["source"]) == (14400, 3600, "env")
    text = "\n".join(effective_policy.format_effective_policy(data))
    assert "M 3 ч" in text and "L 4 ч" in text
    assert effective_policy.unsummarised_keys() == []


async def test_order_feed_line_names_deadline(world, monkeypatch):
    monkeypatch.setenv(ENV, TABLE)
    row = await _issue(world, "M")
    lines = await _rows(
        world.db,
        "SELECT content FROM task_updates WHERE task_id = ?",
        (row["task_id"],),
    )
    assert any("срок прогона 3 ч" in r["content"] for r in lines), lines
