"""Навык дисциплины отдаётся локальной зарегистрированной сессии один раз (#1829).

Блок «Правила работы» (#1630) на каждый pair_start нёс текст навыка целиком.
Теперь по записи о выдаче (сессия, навык, версия, sha256 текста) повтор идёт
без текста, с пометкой ``delivered_to_session``. Недоказанная выдача — всегда
полный текст; ``full_rules`` принуждает к тексту.
"""

from __future__ import annotations

import hashlib


from hub import repository as repo
from hub.models import WorkingRulesSkill
from hub.services import working_rules as wr
from tests.working_rules_support import task_in_project

TEXT = "ДИСЦИПЛИНА ИСПОЛНИТЕЛЯ v-1829"


async def _publish(db, content: str) -> int:
    _, version = await repo.create_skill_version(
        db, name=wr.DISCIPLINE_SKILL, content=content
    )
    await repo.activate_skill_version(
        db, wr.DISCIPLINE_SKILL, version, activated_by="test"
    )
    await db.commit()
    return int(version)


async def _register(db, sid: str) -> None:
    await repo.upsert_agent_session(db, session_id=sid, principal_id=None, agent="a")
    await db.commit()


async def _pair(client, task_id: int, sid: str, **extra):
    resp = await client.post(
        f"/api/tasks/{task_id}/pair-start",
        json={
            "plan": "Plan: x",
            "assigned_agent": "a",
            "session_id": sid,
            "git_mode": "remote",
            **extra,
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["working_rules"]["hub_skill"]


async def _deliveries(db) -> list[tuple]:
    rows = await db.execute_fetchall(
        "SELECT session_id, version, content_sha256 FROM skill_deliveries "
        "ORDER BY rowid"
    )
    return [tuple(r) for r in rows]


async def test_the_skill_text_is_sent_once_per_local_session_and_version(client, db):
    version = await _publish(db, TEXT)
    await _register(db, "s-local")
    t1 = await task_in_project(client, db, slug="wr1829-a")
    t2 = await task_in_project(client, db, slug="wr1829-b")
    t3 = await task_in_project(client, db, slug="wr1829-c")
    t4 = await task_in_project(client, db, slug="wr1829-d")
    t5 = await task_in_project(client, db, slug="wr1829-e")
    t6 = await task_in_project(client, db, slug="wr1829-f")

    first = await _pair(client, t1, "s-local")
    assert first["content"] == TEXT and first["delivered_to_session"] is False
    assert await _deliveries(db) == [
        ("s-local", version, hashlib.sha256(TEXT.encode()).hexdigest())
    ]

    second = await _pair(client, t2, "s-local")
    assert second["content"] == ""
    assert second["delivered_to_session"] is True
    assert second["delivered_in_order"] is False
    assert second["version"] == version and second["chars"] == len(TEXT)

    # full_rules: текст снова, запись не множится.
    forced = await _pair(client, t3, "s-local", full_rules=True)
    assert forced["content"] == TEXT and forced["delivered_to_session"] is False
    assert len(await _deliveries(db)) == 1

    # Новая версия с тем же текстом (хеш тот же) — текст снова.
    same = await _publish(db, TEXT)
    assert same != version
    again = await _pair(client, t6, "s-local")
    assert again["content"] == TEXT and again["version"] == same
    version = same

    # Новая версия (и новый хеш) — текст снова.
    new_text = TEXT + "\nправило добавлено"
    new_version = await _publish(db, new_text)
    assert new_version != version
    changed = await _pair(client, t4, "s-local")
    assert changed["content"] == new_text and changed["version"] == new_version

    # Тот же номер версии, но другой текст (хеш) — тоже текст.
    await db.execute(
        "UPDATE skills SET content=? WHERE name=? AND version=?",
        (new_text + "!", wr.DISCIPLINE_SKILL, new_version),
    )
    await db.commit()
    rehashed = await _pair(client, t5, "s-local")
    assert rehashed["content"] == new_text + "!"
    assert rehashed["delivered_to_session"] is False


async def test_mcp_text_and_cli_name_the_session_delivery(capsys):
    from hub import cli
    from hub.mcp_server import _working_rules_note

    block = {
        "mode": "full",
        "preamble": wr.PREAMBLE,
        "hub_skill": {
            "name": wr.DISCIPLINE_SKILL,
            "state": "active",
            "version": 7,
            "chars": 1234,
            "delivered_to_session": True,
        },
        "project_policy": {"state": "unavailable"},
        "repository_rules": {"state": "missing", "path": wr.AGENT_RULES_FILE},
    }
    note = _working_rules_note(1, {"working_rules": block})
    assert f'hub_get_skill("{wr.DISCIPLINE_SKILL}")' in note
    assert "уже выдан этой сессии" in note and "v7" in note
    cli._warn_area_check({"working_rules": block})
    assert "уже выдан этой сессии" in capsys.readouterr().err


async def test_order_sessions_keep_1683_and_unproven_sessions_get_the_text(client, db):
    # Облачный заказ (#1683) — своя ветка, тесты tests/test_cloud_skill_once.py.
    # Здесь: любая недоказанная выдача отдаёт полный текст.
    await _publish(db, TEXT)
    t1 = await task_in_project(client, db, slug="wr1829-u1")
    t2 = await task_in_project(client, db, slug="wr1829-u2")
    t3 = await task_in_project(client, db, slug="wr1829-u3")
    t4 = await task_in_project(client, db, slug="wr1829-u4")

    # Сессия не зарегистрирована: два pair_start подряд — оба с текстом.
    for task_id in (t1, t2):
        skill = await _pair(client, task_id, "s-unregistered")
        assert skill["content"] == TEXT and skill["delivered_to_session"] is False
    assert await _deliveries(db) == []

    # Нет session_id вовсе (человек): текст.
    resp = await client.post(
        f"/api/tasks/{t3}/pair-start",
        json={"plan": "Plan: x", "assigned_agent": "a", "git_mode": "remote"},
    )
    assert resp.json()["working_rules"]["hub_skill"]["content"] == TEXT

    # Ошибка чтения записей выдачи: полный текст, а не молчание.
    await _register(db, "s-broken")
    await db.execute("DROP TABLE skill_deliveries")
    await db.commit()
    skill = await _pair(client, t4, "s-broken")
    assert skill["content"] == TEXT and skill["delivered_to_session"] is False


async def test_the_session_of_another_principal_is_not_trusted(client, db):
    await _publish(db, TEXT)
    await repo.upsert_agent_session(
        db, session_id="s-foreign", principal_id=999, agent="a"
    )
    await db.commit()
    skill = WorkingRulesSkill(
        name=wr.DISCIPLINE_SKILL,
        state="active",
        version=1,
        content=TEXT,
        chars=len(TEXT),
    )
    for _ in range(2):
        removed = await wr.apply_session_delivery(
            db, skill, "s-foreign", principal_id=5
        )
        assert removed is False and skill.content == TEXT


async def test_pair_start_forwards_full_rules_from_cli_and_mcp(monkeypatch):
    from hub import cli, mcp_server
    from hub.models import TaskPairStart

    assert TaskPairStart().full_rules is False
    assert TaskPairStart(full_rules=True).full_rules is True

    sent: list[dict] = []

    def _fake_api(method, path, body=None):
        sent.append(body)
        return {}

    monkeypatch.setattr(cli, "_api", _fake_api)
    for flag, expected in (([], False), (["--full-rules"], True)):
        args = cli.build_parser().parse_args(["pair-start", "1", *flag])
        assert args.func(args) == 0
        assert sent[-1].get("full_rules") == expected

    async def _fake_post(path, body=None):
        sent.append(body)
        return {"status": "running"}

    async def _no_task(task_id):
        return None

    monkeypatch.setattr(mcp_server, "_api_post", _fake_post)
    monkeypatch.setattr(mcp_server, "_read_task", _no_task)
    await mcp_server.hub_pair_start(1, session_id="s", full_rules=True)
    assert sent[-1]["full_rules"] is True
    await mcp_server.hub_pair_start(1, session_id="s")
    assert "full_rules" not in sent[-1]
