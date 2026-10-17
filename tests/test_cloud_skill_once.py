"""Облачный исполнитель получает навык дисциплины один раз за прогон (#1683).

Заказ вставляет активный навык в промпт целиком; ответ pair-start тот же текст
повторял. Теперь, если заказ СЕССИИ-вызывающего нёс ту же версию, слой
``hub_skill`` отвечает ссылкой (``delivered_in_order``). Привязка идёт только
по токену сессии: executor_runs ← chat_pair_codes ← chat_pair_sessions. Любая
неоднозначность — полный текст: «скрыли навык» хуже, чем «отдали дважды».
"""

from __future__ import annotations

import json
import re
from types import SimpleNamespace

import aiosqlite
import pytest

from hub import config
from hub import repository as repo
from hub.config import TokenIdentity
from hub.integrations import cursor_cloud
from hub.services import admin as admin_svc
from hub.services import chat_pair as cp
from hub.services import executor_launch as el
from tests.test_executor_dispatch import (
    _CREATED,
    _FAKE_KEY,  # noqa: F401 - автофикстура cursor_key ниже
    _launch_config,
    _launch_project,
)

SKILL = "executor-pair-discipline"
AGENT_TOKEN = "agent-token-1683"  # pragma: allowlist secret


@pytest.fixture(autouse=True)
def cursor_key(monkeypatch):
    monkeypatch.setattr(config, "CURSOR_API_KEY", _FAKE_KEY)


@pytest.fixture(autouse=True)
def _clean_limiter():
    cp.chat_pair_limiter._buckets.clear()
    yield
    cp.chat_pair_limiter._buckets.clear()


@pytest.fixture
async def hub(client, db, db_dsn, monkeypatch):
    monkeypatch.setattr(
        config, "HUB_TOKENS", {AGENT_TOKEN: TokenIdentity("bot", "agent")}
    )
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    _launch_config(monkeypatch)
    human = await admin_svc.create_principal(
        db, kind="human", username="owner1683", role_slug="operator"
    )
    key = await admin_svc.create_api_key(db, human["id"], name="laptop")
    return SimpleNamespace(
        client=client,
        db=db,
        db_dsn=db_dsn,
        human_id=int(human["id"]),
        human_auth={"Authorization": f"Bearer {key['plaintext_key']}"},
        agent_auth={"Authorization": f"Bearer {AGENT_TOKEN}"},
        calls=[],
        seen_version_at_call=[],
    )


def _provider(hub, monkeypatch) -> None:
    """Провайдер подменён; в момент вызова читает зафиксированную строку заказа.

    #1689: у писателя нет открытой транзакции, а независимое соединение (WAL)
    видит версию именно строки этого заказа — незакоммиченное оно не увидит.
    """
    order_rows: list[int] = []
    real_set_ref = repo.set_executor_run_starting_ref

    async def _capture(db, row_id, *args, **kw):
        order_rows.append(int(row_id))
        return await real_set_ref(db, row_id, *args, **kw)

    monkeypatch.setattr(repo, "set_executor_run_starting_ref", _capture)

    async def _create(**kw):
        hub.calls.append(kw)
        assert hub.db.in_transaction is False
        assert order_rows, "строка заказа не захвачена"
        async with aiosqlite.connect(hub.db_dsn, uri=True) as reader:
            reader.row_factory = aiosqlite.Row
            rows = await reader.execute_fetchall(
                "SELECT ordered_skill_version FROM executor_runs WHERE id = ?",
                (order_rows[-1],),
            )
        hub.seen_version_at_call.append(rows[0]["ordered_skill_version"])
        return _CREATED, None

    monkeypatch.setattr(cursor_cloud, "create_agent_attempt", _create)


async def _active_version(db: aiosqlite.Connection) -> int:
    return int((await repo.get_active_skill(db, SKILL))["version"])


async def _publish_next_version(db: aiosqlite.Connection) -> int:
    old = dict(await repo.get_active_skill(db, SKILL))
    new = int(old["version"]) + 1
    await db.execute(
        "INSERT INTO skills (name, kind, version, content, status) "
        "VALUES (?, 'skill', ?, ?, 'active')",
        (SKILL, new, old["content"] + "\nДобавка новой версии.\n"),
    )
    await db.commit()
    return new


async def _launch(hub, project) -> tuple[int, str]:
    """Заказ → (id строки прогона, код implementer из промпта)."""
    result = await el.launch_executor(hub.db, project, issuer_principal_id=hub.human_id)
    assert result.launched, result
    code = re.search(r"код ([A-Z0-9-]{8,})", hub.calls[-1]["prompt_text"]).group(1)
    return int(result.row_id), code


async def _redeem(hub, code: str) -> dict[str, str]:
    redeemed = await hub.client.post(
        "/api/auth/chat-pair/redeem",
        json={"code": code},
        headers={"x-forwarded-for": "203.0.113.83"},
    )
    assert redeemed.status_code == 200, redeemed.text
    return {"Authorization": f"Bearer {redeemed.json()['token']}"}


async def _pair_start(hub, task_id: int, headers: dict[str, str]):
    return await hub.client.post(
        f"/api/tasks/{task_id}/pair-start",
        json={
            "plan": "Plan: x",
            "assigned_agent": config.CHAT_PAIR_AGENT,
            "session_id": "s-1683",
            "git_mode": "remote",
        },
        headers=headers,
    )


async def _facts(db: aiosqlite.Connection, task_id: int) -> list[dict]:
    rows = await db.execute_fetchall(
        "SELECT detail FROM activity_log WHERE kind='task_pair_started' "
        "AND summary LIKE ?",
        (f"Task #{task_id} %",),
    )
    return [json.loads(r["detail"]) for r in rows]


# ---------------------------------------------------------------------------
# AC-1
# ---------------------------------------------------------------------------


async def test_cloud_pair_start_references_the_skill_already_in_the_order(
    hub, monkeypatch
):
    _provider(hub, monkeypatch)
    project, task_id = await _launch_project(hub.db, slug="once-1")
    version = await _active_version(hub.db)
    skill_chars = len(str((await repo.get_active_skill(hub.db, SKILL))["content"]))

    row_id, code = await _launch(hub, project)
    # Версия записана ДО вызова провайдера, из того же объекта, что ушёл в промпт.
    assert hub.seen_version_at_call == [version]

    session = await _redeem(hub, code)
    resp = await _pair_start(hub, task_id, session)
    assert resp.status_code == 200, resp.text
    rules = resp.json()["working_rules"]
    skill = rules["hub_skill"]
    assert rules["mode"] == "full"
    assert skill["state"] == "active"
    assert skill["delivered_in_order"] is True
    assert skill["version"] == version and skill["chars"] == skill_chars
    assert skill["content"] == ""
    assert rules["project_policy"]["state"] == "available"
    assert rules["project_policy"]["text"]
    assert rules["repository_rules"]["path"] == ".hub/AGENT_RULES.md"

    facts = await _facts(hub.db, task_id)
    assert len(facts) == 1
    assert facts[0]["executor_run_id"] == row_id
    assert facts[0]["skill_version"] == version
    assert facts[0]["delivered_in_order"] is True
    assert facts[0]["omitted_chars"] == skill_chars

    # Повторный pair-start после старта — по-прежнему 400.
    again = await _pair_start(hub, task_id, session)
    assert again.status_code == 400, again.text


# ---------------------------------------------------------------------------
# AC-2
# ---------------------------------------------------------------------------


async def test_pair_start_sends_the_full_skill_unless_its_own_order_carried_the_same_version(
    hub, monkeypatch
):
    _provider(hub, monkeypatch)

    # (а) версия сменилась между заказом и pair-start.
    project, task_id = await _launch_project(hub.db, slug="once-2a")
    row_id, code = await _launch(hub, project)
    ordered = await _active_version(hub.db)
    now = await _publish_next_version(hub.db)
    resp = await _pair_start(hub, task_id, await _redeem(hub, code))
    assert resp.status_code == 200, resp.text
    skill = resp.json()["working_rules"]["hub_skill"]
    assert skill["delivered_in_order"] is False
    assert skill["content"] and skill["version"] == now
    assert skill["ordered_version"] == ordered and skill["version_changed"] is True
    fact = (await _facts(hub.db, task_id))[0]
    assert fact["executor_run_id"] == row_id and fact["delivered_in_order"] is False
    assert fact["omitted_chars"] == 0

    # (б) два заказа одного поколения с разными версиями; старая выкупленная
    # сессия первого получает решение по СВОЕМУ заказу, а не по последнему.
    project, task_b = await _launch_project(hub.db, slug="once-2b")
    first_id, first_code = await _launch(hub, project)
    old_session = await _redeem(hub, first_code)
    await hub.db.execute(
        "UPDATE executor_runs SET outcome='failed', finished_at=datetime('now'), "
        "tokens=1, cents=1 "
        "WHERE id=?",
        (first_id,),
    )
    await hub.db.commit()
    await _publish_next_version(hub.db)  # активная версия теперь == заказу №2
    second_id, _ = await _launch(hub, project)
    assert second_id != first_id
    versions = [
        r["ordered_skill_version"]
        for r in await hub.db.execute_fetchall(
            "SELECT ordered_skill_version FROM executor_runs WHERE task_id=? "
            "ORDER BY id",
            (task_b,),
        )
    ]
    assert versions[0] != versions[1]
    # #1783: старая сессия закрытого прогона не берёт задачу поверх живого нового —
    # новый прогон здесь закончен, чтобы проверялось только решение по заказу.
    await hub.db.execute(
        "UPDATE executor_runs SET outcome='failed', finished_at=datetime('now') "
        "WHERE id=?",
        (second_id,),
    )
    await hub.db.commit()
    resp = await _pair_start(hub, task_b, old_session)
    assert resp.status_code == 200, resp.text
    skill = resp.json()["working_rules"]["hub_skill"]
    assert skill["delivered_in_order"] is False and skill["content"]
    assert (await _facts(hub.db, task_b))[0]["executor_run_id"] == first_id

    # (в) ручной implementer без заказа.
    _, task_c = await _launch_project(hub.db, slug="once-2c")
    issued = await hub.client.post(
        "/api/auth/chat-pair/start",
        json={"kind": "implementer", "task_id": task_c},
        headers={**hub.human_auth, "x-forwarded-for": "203.0.113.85"},
    )
    resp = await _pair_start(hub, task_c, await _redeem(hub, issued.json()["code"]))
    assert resp.status_code == 200, resp.text
    skill = resp.json()["working_rules"]["hub_skill"]
    assert skill["delivered_in_order"] is False and skill["content"]
    assert skill["version_changed"] is False


@pytest.mark.parametrize(
    "spoil",
    [
        "UPDATE executor_runs SET ordered_skill_version=NULL",  # (г) старая строка
        "UPDATE executor_runs SET submission_generation=2",  # поколение чужое
        "UPDATE executor_runs SET task_id={other}",  # заказ другой задачи
        "UPDATE chat_pair_sessions SET executor_run_id=NULL",  # связи нет
        "UPDATE chat_pair_sessions SET executor_run_id=987654",  # связь висит в пустоту
        "UPDATE chat_pair_sessions SET bound_generation=NULL",  # поколение сессии не названо
    ],
)
async def test_a_stale_or_broken_link_gives_the_full_text(hub, monkeypatch, spoil):
    _provider(hub, monkeypatch)
    project, task_id = await _launch_project(hub.db, slug="once-2d")
    _, code = await _launch(hub, project)
    session = await _redeem(hub, code)
    from hub import services
    from hub.models import TaskCreate

    other = (await services.create_task(hub.db, TaskCreate(title="соседняя"))).id
    await hub.db.execute(spoil.format(other=other))
    await hub.db.commit()
    if "SET executor_run_id" in spoil:
        # #1783: сессия без доказательства — ручная; чужой живой прогон ей не мешает,
        # только когда он закончен (иначе её пустили бы поверх брони хаба).
        await hub.db.execute(
            "UPDATE executor_runs SET outcome='failed', finished_at=datetime('now')"
        )
        await hub.db.commit()

    resp = await _pair_start(hub, task_id, session)
    assert resp.status_code == 200, resp.text
    skill = resp.json()["working_rules"]["hub_skill"]
    assert skill["delivered_in_order"] is False and skill["content"]


# ---------------------------------------------------------------------------
# AC-3
# ---------------------------------------------------------------------------


async def test_other_readers_and_orders_keep_the_full_skill(hub, monkeypatch, capsys):
    _provider(hub, monkeypatch)
    project, task_id = await _launch_project(hub.db, slug="once-3")
    skill_row = dict(await repo.get_active_skill(hub.db, SKILL))
    _, code = await _launch(hub, project)

    # Заказ по-прежнему содержит навык целиком.
    assert skill_row["content"].strip() in hub.calls[-1]["prompt_text"]

    session = await _redeem(hub, code)
    started = await _pair_start(hub, task_id, session)
    assert started.status_code == 200, started.text
    delivered = started.json()["working_rules"]
    assert delivered["hub_skill"]["delivered_in_order"] is True

    # GET /context той же сессии и hub_my_context(mode=full): навык целиком.
    ctx = (
        await hub.client.get(f"/api/tasks/{task_id}/context", headers=session)
    ).json()
    assert ctx["working_rules"]["hub_skill"]["delivered_in_order"] is False
    assert ctx["working_rules"]["hub_skill"]["content"] == skill_row["content"]
    assert skill_row["content"].strip()[:60] in ctx["context_text"]

    # Прочие двери заказа (repair, continue, submit-only, merge) собирают тот же
    # промпт с полным навыком.
    task = {"id": task_id, "title": "t", "status": "running", "branch": "task-1/x"}
    for kw in ({}, {"branch": "task-1/x"}):
        text = el._prompt(
            task, "develop", "CODE", "http://h", skill_row["content"], **kw
        )
        assert skill_row["content"].strip() in text
    merge = {**task, "status": "needs_decision"}
    assert skill_row["content"].strip() in el._prompt(
        merge, "develop", "CODE", "http://h", skill_row["content"]
    )

    # MCP и CLI показывают пометку доставки; потолки не тронуты.
    from hub import cli
    from hub.mcp_server import _working_rules_note
    from hub.services import working_rules as wr

    note = _working_rules_note(task_id, {"working_rules": delivered})
    assert "уже в заказе этого прогона" in note
    assert f"v{delivered['hub_skill']['version']}" in note
    assert delivered["hub_skill"]["name"] in note
    assert (wr.MCP_SKILL_CAP, wr.MCP_REPO_CAP) == (6000, 4000)
    cli._warn_area_check(started.json())
    assert "уже в заказе этого прогона" in capsys.readouterr().err

    # Человек (локальный pair-start по REST): навык целиком.
    _, task_h = await _launch_project(hub.db, slug="once-3h")
    human = await hub.client.post(
        f"/api/tasks/{task_h}/pair-start",
        json={"plan": "Plan: x", "git_mode": "remote"},
        headers=hub.human_auth,
    )
    assert human.status_code == 200, human.text
    hs = human.json()["working_rules"]["hub_skill"]
    assert hs["delivered_in_order"] is False and hs["content"] == skill_row["content"]

    # Агентский ключ (локальный pair-start): тоже целиком.
    _, task_a = await _launch_project(hub.db, slug="once-3a")
    agent = await hub.client.post(
        f"/api/tasks/{task_a}/pair-start",
        json={
            "plan": "Plan: x",
            "assigned_agent": "bot",
            "session_id": "s-bot",
            "git_mode": "remote",
        },
        headers=hub.agent_auth,
    )
    assert agent.status_code == 200, agent.text
    assert agent.json()["working_rules"]["hub_skill"]["content"] == skill_row["content"]

    # Повторный pair-start после успешного — 400.
    again = await _pair_start(hub, task_id, session)
    assert again.status_code == 400, again.text


# ---------------------------------------------------------------------------
# Миграция существующей БД (R3)
# ---------------------------------------------------------------------------


async def test_migration_adds_nullable_columns_to_an_existing_database(
    db: aiosqlite.Connection,
):
    from hub.db import _migrate

    task_id = await repo.create_task(
        db,
        title="старый прогон",
        description="",
        runtime="auto",
        source="agent",
        assigned_agent="pda_claude",
        rationale="",
        status="running",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    old_run = await repo.create_executor_run(
        db,
        task_id=task_id,
        submission_generation=1,
        agent_id="a",
        run_id="r",
        model="m",
    )
    names = {
        "executor_runs": "ordered_skill_version",
        "chat_pair_codes": "executor_run_id",
        "chat_pair_sessions": "executor_run_id",
    }
    for table, column in names.items():
        await db.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    await db.execute(
        "DELETE FROM _migrations WHERE name IN "
        "('add_executor_runs_ordered_skill_version', "
        "'add_chat_pair_codes_executor_run_id', "
        "'add_chat_pair_sessions_executor_run_id')"
    )
    await db.commit()

    await _migrate(db)

    for table, column in names.items():
        cols = {
            r["name"]: r
            for r in await db.execute_fetchall(f"PRAGMA table_info({table})")
        }
        assert column in cols and cols[column]["notnull"] == 0
    row = (
        await db.execute_fetchall(
            "SELECT ordered_skill_version, agent_id FROM executor_runs WHERE id=?",
            (old_run,),
        )
    )[0]
    assert row["ordered_skill_version"] is None and row["agent_id"] == "a"
