"""Канал слотов: хаб знает, в каком канале и слоте идёт задача (#1434, F7).

Канал (cloud | slot) и имя слота — атрибуты выдачи одноразового кода
implementer. При захвате задачи они ложатся в карточку одной строкой и в
занятость слотов (REST, CLI, MCP). Слот без сдачи и без признаков жизни
дольше порога поллер освобождает — один раз и с названной причиной.
"""

from __future__ import annotations

import argparse
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiosqlite
import pytest
from mcp.types import CallToolResult, TextContent

from hub import cli, config
from hub import repository as repo
from hub import db as hub_db
from hub.config import TokenIdentity
from hub.services import admin as admin_svc
from hub.services import chat_pair as cp
from hub.services import executor_slots

AGENT_TOKEN = "agent-token-1434"  # pragma: allowlist secret


async def _rows(db, sql: str, params: tuple = ()) -> list[dict]:
    cursor = await db.execute(sql, params)
    return [dict(r) for r in await cursor.fetchall()]


@pytest.fixture(autouse=True)
def _clean_limiter():
    cp.chat_pair_limiter._buckets.clear()
    yield
    cp.chat_pair_limiter._buckets.clear()


@pytest.fixture
async def hub(client, db, monkeypatch):
    """Хаб с auth: человек выдаёт коды, агентский токен — для чужих захватов."""
    monkeypatch.setattr(
        config, "HUB_TOKENS", {AGENT_TOKEN: TokenIdentity("bot", "agent")}
    )
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    human = await admin_svc.create_principal(
        db, kind="human", username="steward-owner", role_slug="operator"
    )
    key = await admin_svc.create_api_key(db, human["id"], name="laptop")
    return SimpleNamespace(
        client=client,
        db=db,
        human_auth={"Authorization": f"Bearer {key['plaintext_key']}"},
        agent_auth={"Authorization": f"Bearer {AGENT_TOKEN}"},
    )


async def _make_task(hub, title: str = "задача в слот") -> int:
    resp = await hub.client.post(
        "/api/tasks", json={"title": title}, headers=hub.human_auth
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


async def _implementer(hub, task_id: int, **channel: str) -> dict[str, str]:
    """Код implementer (с каналом или без) → сессия слота."""
    issued = await hub.client.post(
        "/api/auth/chat-pair/start",
        json={"kind": "implementer", "task_id": task_id, **channel},
        headers={**hub.human_auth, "x-forwarded-for": "203.0.113.34"},
    )
    assert issued.status_code == 200, issued.text
    redeemed = await hub.client.post(
        "/api/auth/chat-pair/redeem",
        json={"code": issued.json()["code"]},
        headers={"x-forwarded-for": "203.0.113.34"},
    )
    assert redeemed.status_code == 200, redeemed.text
    return {"Authorization": f"Bearer {redeemed.json()['token']}"}


async def _claim(hub, task_id: int, session: dict[str, str]) -> None:
    claimed = await hub.client.post(
        f"/api/tasks/{task_id}/claim",
        json={"agent": config.CHAT_PAIR_AGENT, "session_id": f"slot-{task_id}"},
        headers=session,
    )
    assert claimed.status_code == 200, claimed.text


async def _pair_start(hub, task_id: int, session: dict[str, str]) -> None:
    started = await hub.client.post(
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


async def _card(hub, task_id: int) -> list[dict]:
    return await _rows(
        hub.db,
        "SELECT agent, kind, content FROM task_updates WHERE task_id = ? ORDER BY id",
        (task_id,),
    )


async def _occupancy(hub) -> dict:
    resp = await hub.client.get("/api/executor-slots", headers=hub.agent_auth)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _mcp_text(result: CallToolResult) -> str:
    return "\n".join(b.text for b in result.content if isinstance(b, TextContent))


async def _mcp_view(payload: dict) -> str:
    from hub import mcp_server

    with patch.object(mcp_server, "_api_get", AsyncMock(return_value=payload)) as get:
        text = _mcp_text(await mcp_server.hub_executor_slots())
    assert get.await_args.args[0] == "/api/executor-slots"
    return text


def _cli_view(payload: dict, capsys) -> str:
    with patch.object(cli, "_api", return_value=payload) as api:
        rc = cli.cmd_slots(argparse.Namespace(json=False))
    assert rc == 0
    assert api.call_args.args[:2] == ("GET", "/api/executor-slots")
    return capsys.readouterr().out


async def _age_everything(hub, task_id: int, minutes: int) -> None:
    """Сдвинуть в прошлое всё, что считается признаком жизни задачи."""
    shift = f"-{minutes} minutes"
    await hub.db.execute(
        "UPDATE executor_slots SET captured_at = datetime('now', ?) WHERE task_id = ?",
        (shift, task_id),
    )
    await hub.db.execute(
        "UPDATE tasks SET updated_at = datetime('now', ?) WHERE id = ?",
        (shift, task_id),
    )
    await hub.db.execute(
        "UPDATE task_updates SET created_at = datetime('now', ?) WHERE task_id = ?",
        (shift, task_id),
    )
    await hub.db.execute(
        "UPDATE agent_sessions SET last_seen_at = datetime('now', ?) "
        "WHERE current_task_id = ?",
        (shift, task_id),
    )
    await hub.db.commit()


# ---------------------------------------------------------------------------
# AC-1 — канал и слот записываются при захвате и видны на трёх поверхностях
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claim_records_channel_and_slot(hub, capsys):
    task_id = await _make_task(hub)
    session = await _implementer(hub, task_id, channel="slot", slot="slot-2")

    # Захват и затем pair-start той же сессией — строка в карточке одна.
    await _claim(hub, task_id, session)
    after_claim = [u for u in await _card(hub, task_id) if "канал:" in u["content"]]
    assert [u["content"] for u in after_claim] == ["канал: slot-2"], (
        "канал пишет сам захват, а не только pair-start"
    )
    await _pair_start(hub, task_id, session)

    lines = [u for u in await _card(hub, task_id) if "канал:" in u["content"]]
    assert [u["content"] for u in lines] == ["канал: slot-2"]

    view = await _occupancy(hub)
    slots = view["slots"]
    assert len(slots) == 1
    assert slots[0]["slot"] == "slot-2"
    assert slots[0]["task_id"] == task_id
    assert slots[0]["channel"] == "slot"
    assert slots[0]["since"], "время захвата обязано быть названо"
    assert "last_sign_of_life" in slots[0]
    in_work = {row["task_id"]: row for row in view["tasks"]}
    assert in_work[task_id]["label"] == "slot-2"

    mcp_text = await _mcp_view(view)
    assert "slot-2" in mcp_text and f"#{task_id}" in mcp_text
    cli_text = _cli_view(view, capsys)
    assert "slot-2" in cli_text and f"#{task_id}" in cli_text


@pytest.mark.asyncio
async def test_cloud_channel_is_one_card_line(hub):
    task_id = await _make_task(hub)
    session = await _implementer(hub, task_id, channel="cloud")
    await _pair_start(hub, task_id, session)

    lines = [
        u["content"] for u in await _card(hub, task_id) if "канал:" in u["content"]
    ]
    assert lines == ["канал: cloud"]
    view = await _occupancy(hub)
    assert view["slots"] == [], "облако — не слот"
    assert {r["task_id"]: r["label"] for r in view["tasks"]}[task_id] == "cloud"


@pytest.mark.asyncio
@pytest.mark.parametrize("capture", ["claim", "pair_start"])
async def test_capture_takes_the_channel_of_the_capturing_session(hub, capture):
    """Две живые сессии: A со slot-2, B без канала. Захват B — канал не назван."""
    task_id = await _make_task(hub)
    await _implementer(hub, task_id, channel="slot", slot="slot-2")
    session_b = await _implementer(hub, task_id)
    await (_claim if capture == "claim" else _pair_start)(hub, task_id, session_b)

    assert not [u for u in await _card(hub, task_id) if "канал:" in u["content"]]
    view = await _occupancy(hub)
    assert view["slots"] == []
    assert {r["task_id"]: r["label"] for r in view["tasks"]}[task_id] == "не назван"


@pytest.mark.asyncio
async def test_the_hub_dispatcher_code_names_the_cloud(hub):
    """Код, который выписывает диспетчер хаба (#1439), — канал cloud."""
    task_id = await _make_task(hub)
    await cp.issue_run_code(hub.db, task_id, 1, issued_by_principal_id=None)
    rows = await _rows(
        hub.db,
        "SELECT channel, slot FROM chat_pair_codes WHERE bound_task_id = ?",
        (task_id,),
    )
    assert rows == [{"channel": "cloud", "slot": ""}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"channel": "slot"},  # слот без имени
        {"channel": "cloud", "slot": "slot-1"},  # облако с именем слота
        {"channel": "ftp"},  # чужой канал
        {"channel": "slot", "slot": "slot 2; rm"},  # имя не по форме
    ],
)
async def test_a_malformed_channel_is_refused_before_a_code(hub, body):
    task_id = await _make_task(hub)
    resp = await hub.client.post(
        "/api/auth/chat-pair/start",
        json={"kind": "implementer", "task_id": task_id, **body},
        headers=hub.human_auth,
    )
    assert resp.status_code == 422, resp.text
    assert await _rows(hub.db, "SELECT id FROM chat_pair_codes") == []


@pytest.mark.asyncio
async def test_intake_code_cannot_carry_a_channel(hub):
    resp = await hub.client.post(
        "/api/auth/chat-pair/start",
        json={"channel": "slot", "slot": "slot-1"},
        headers=hub.human_auth,
    )
    assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------------
# AC-2 — код без канала работает как раньше
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_code_without_channel_behaves_as_before(hub, capsys):
    task_id = await _make_task(hub)
    session = await _implementer(hub, task_id)
    await _claim(hub, task_id, session)
    await _pair_start(hub, task_id, session)

    task = (await _rows(hub.db, "SELECT status FROM tasks WHERE id=?", (task_id,)))[0]
    assert task["status"] == "running"
    assert not [u for u in await _card(hub, task_id) if "канал:" in u["content"]]
    assert await _rows(hub.db, "SELECT id FROM executor_slots") == []

    view = await _occupancy(hub)
    assert view["slots"] == []
    row = {r["task_id"]: r for r in view["tasks"]}[task_id]
    assert row["channel"] == ""
    assert row["label"] == "не назван"
    assert row["since"], "время захвата задачи без канала тоже названо"
    assert "не назван" in await _mcp_view(view)
    assert "не назван" in _cli_view(view, capsys)


# ---------------------------------------------------------------------------
# AC-3 — умерший слот освобождается один раз и с причиной
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_dead_slot_is_released_once_with_a_reason(hub, monkeypatch):
    monkeypatch.setattr(config, "EXECUTOR_SLOT_DEAD_MINUTES", 60)
    task_id = await _make_task(hub)
    session = await _implementer(hub, task_id, channel="slot", slot="slot-2")
    await _pair_start(hub, task_id, session)

    # Живой слот (признаки жизни свежие) поллер не трогает.
    await executor_slots.sweep_executor_slots(hub.db)
    assert [s["slot"] for s in (await _occupancy(hub))["slots"]] == ["slot-2"]

    await _age_everything(hub, task_id, 90)
    for _ in range(3):
        await executor_slots.sweep_executor_slots(hub.db)

    released = [u for u in await _card(hub, task_id) if "освобождён" in u["content"]]
    assert len(released) == 1, released
    assert released[0]["content"].startswith(
        "слот slot-2 освобождён: нет признаков жизни 9"
    ), released[0]["content"]
    assert "мин" in released[0]["content"]

    view = await _occupancy(hub)
    assert view["slots"] == [], "освобождённый слот свободен"
    abandoned = {r["task_id"]: r for r in view["abandoned"]}
    assert abandoned[task_id]["slot"] == "slot-2"
    assert abandoned[task_id]["channel"] == "slot"
    assert task_id not in {r["task_id"] for r in view["tasks"]}, (
        "брошенная задача — только в abandoned, не «не назван» в tasks"
    )
    assert "нет признаков жизни" in abandoned[task_id]["reason"]
    events = await _rows(
        hub.db,
        "SELECT id FROM events WHERE kind='executor_slot_released' AND task_id=?",
        (task_id,),
    )
    assert len(events) == 1


@pytest.mark.asyncio
async def test_a_heartbeat_keeps_the_slot_alive(hub, monkeypatch):
    """Признак жизни — не только запись в карточке, но и heartbeat сессии."""
    monkeypatch.setattr(config, "EXECUTOR_SLOT_DEAD_MINUTES", 60)
    task_id = await _make_task(hub)
    session = await _implementer(hub, task_id, channel="slot", slot="slot-3")
    await _pair_start(hub, task_id, session)
    await _age_everything(hub, task_id, 90)
    await hub.db.execute(
        "INSERT INTO agent_sessions (session_id, agent, current_task_id) "
        "VALUES ('slot-3-live', 'cloud', ?)",
        (task_id,),
    )
    await hub.db.commit()

    await executor_slots.sweep_executor_slots(hub.db)

    assert [s["slot"] for s in (await _occupancy(hub))["slots"]] == ["slot-3"]
    assert not [u for u in await _card(hub, task_id) if "освобождён" in u["content"]]


@pytest.mark.asyncio
async def test_the_project_policy_names_the_threshold(hub, monkeypatch):
    monkeypatch.setattr(config, "EXECUTOR_SLOT_DEAD_MINUTES", 60)
    task_id = await _make_task(hub)
    await hub.db.execute(
        "INSERT INTO projects (slug, name, gate_policy) VALUES ('slots', 'Slots', ?)",
        (json.dumps({"slot_dead_minutes": 240}),),
    )
    await hub.db.execute(
        "UPDATE tasks SET project_id = (SELECT id FROM projects WHERE slug='slots') "
        "WHERE id = ?",
        (task_id,),
    )
    await hub.db.commit()
    project = await repo.resolve_project_for_task(hub.db, task_id)
    assert project is not None and project["slug"] == "slots"
    await hub.db.commit()
    session = await _implementer(hub, task_id, channel="slot", slot="slot-1")
    await _pair_start(hub, task_id, session)
    await _age_everything(hub, task_id, 90)

    await executor_slots.sweep_executor_slots(hub.db)

    assert [s["slot"] for s in (await _occupancy(hub))["slots"]] == ["slot-1"]


@pytest.mark.asyncio
async def test_stale_alert_names_the_slot(hub, monkeypatch):
    """Stale-алерт (рубеж 30m) не дублируется, а называет слот."""
    from hub import poller

    monkeypatch.setattr(config, "EXECUTOR_SLOT_DEAD_MINUTES", 600)
    task_id = await _make_task(hub)
    session = await _implementer(hub, task_id, channel="slot", slot="slot-2")
    await _pair_start(hub, task_id, session)
    await _age_everything(hub, task_id, 45)

    await poller._sweep_stale_running(hub.db)

    stale = [
        u["content"] for u in await _card(hub, task_id) if "stale in" in u["content"]
    ]
    assert len(stale) == 1
    assert "слот slot-2" in stale[0]


# ---------------------------------------------------------------------------
# AC-4 — сдача освобождает слот без записи об освобождении по сроку
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submission_frees_the_slot(hub, monkeypatch):
    monkeypatch.setattr(config, "EXECUTOR_SLOT_DEAD_MINUTES", 60)
    task_id = await _make_task(hub)
    session = await _implementer(hub, task_id, channel="slot", slot="slot-2")
    await _pair_start(hub, task_id, session)

    submitted = await hub.client.post(
        f"/api/tasks/{task_id}/submit-review",
        json={"summary": "готово", "model": "gpt-5.3-codex"},
        headers=session,
    )
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["status"] not in ("running", "claimed")

    assert (await _occupancy(hub))["slots"] == [], "сдача освобождает слот сразу"
    await _age_everything(hub, task_id, 90)
    for _ in range(2):
        await executor_slots.sweep_executor_slots(hub.db)

    assert not [u for u in await _card(hub, task_id) if "освобождён" in u["content"]]
    view = await _occupancy(hub)
    assert view["slots"] == [] and view["abandoned"] == []
    row = (
        await _rows(
            hub.db,
            "SELECT outcome, released_at FROM executor_slots WHERE task_id = ?",
            (task_id,),
        )
    )[0]
    assert row["outcome"] == "freed" and row["released_at"]


# ---------------------------------------------------------------------------
# Миграция: чистая база, база с данными, повторный прогон
# ---------------------------------------------------------------------------

_MINE = {
    "add_chat_pair_codes_channel",
    "add_chat_pair_codes_slot",
    "add_chat_pair_sessions_channel",
    "add_chat_pair_sessions_slot",
    "create_executor_slots",
    "idx_executor_slots_active",
}


async def _fresh(migrations) -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys = ON")
    await conn.executescript(hub_db._SCHEMA)
    with patch.object(hub_db, "_MIGRATIONS", migrations):
        await hub_db._migrate(conn)
    return conn


@pytest.mark.asyncio
async def test_migration_is_last_and_idempotent_on_clean_and_filled_db():
    names = [name for name, _ in hub_db._MIGRATIONS]
    assert set(names[-len(_MINE) :]) == _MINE, "миграция #1434 — в конце списка"

    clean = await _fresh(hub_db._MIGRATIONS)
    try:
        await hub_db._migrate(clean)  # повторный прогон
        cols = {
            r["name"]
            for r in await clean.execute_fetchall("PRAGMA table_info(chat_pair_codes)")
        }
        assert {"channel", "slot"} <= cols
    finally:
        await clean.close()

    before = [m for m in hub_db._MIGRATIONS if m[0] not in _MINE]
    filled = await _fresh(before)
    try:
        await filled.execute(
            "INSERT INTO principals (kind, username, status) "
            "VALUES ('human', 'old', 'active')"
        )
        await filled.execute(
            "INSERT INTO chat_pair_codes (principal_id, kind, code_hash, expires_at) "
            "VALUES (1, 'implementer', 'h', datetime('now', '+1 hour'))"
        )
        await filled.commit()
        await hub_db._migrate(filled)
        await hub_db._migrate(filled)
        rows = [
            dict(r)
            for r in await filled.execute_fetchall(
                "SELECT channel, slot FROM chat_pair_codes"
            )
        ]
        assert rows == [{"channel": "", "slot": ""}]
        applied = {
            r[0] for r in await filled.execute_fetchall("SELECT name FROM _migrations")
        }
        assert _MINE <= applied
        assert await filled.execute_fetchall("SELECT * FROM executor_slots") == []
    finally:
        await filled.close()
