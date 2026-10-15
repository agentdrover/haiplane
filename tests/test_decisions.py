"""Записи ленты kind=decision читаются из хаба, связка n4l через CLI удалена (#1698).

AC-1: читатель, REST /api/decisions, MCP hub_list_decisions и дашборд.
AC-2: record_decision — совместимый no-op; подпроцесс не запускается.
AC-3: модуля, атрибутов конфигурации и плагина нет; /api/integrations/notes — 410.
AC-4: доступ /api/decisions совпадает с доступом /api/dashboard по ролям.
"""

from __future__ import annotations

import importlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from hub import config, mcp_server
from hub import repository as repo
from hub.config import TokenIdentity
from hub.integrations.registry import plugins
from hub.services import admin as admin_svc
from hub.services import chat_pair as cp
from hub.services import decisions as decisions_svc
from tests.state_support import make_project, make_state_task


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _task(db, title, *, parent_id=None, task_type="task", status="open"):
    task_id = await repo.create_task(
        db,
        title=title,
        description="",
        runtime="auto",
        source="human",
        assigned_agent="dev",
        rationale="",
        status=status,
        auto_review=True,
        task_type=task_type,
        parent_id=parent_id,
        priority="medium",
    )
    await db.commit()
    return task_id


async def _decision(
    db,
    task_id,
    content,
    created_at,
    *,
    kind="decision",
    agent="human",
    principal_id=None,
    author_kind="hub",
):
    update_id = await repo.add_task_update(
        db,
        task_id,
        agent,
        kind,
        content,
        principal_id=principal_id,
        author_kind=author_kind,
    )
    await db.execute(
        "UPDATE task_updates SET created_at=? WHERE id=?", (created_at, update_id)
    )
    await db.commit()
    return update_id


async def _world(client, db):
    """Два проекта; подзадача наследует проект эпика; архивная; другой kind."""
    pa = await make_project(db, "proj-a")
    pb = await make_project(db, "proj-b")
    epic_a = await _task(db, "Эпик A", task_type="epic")
    await repo.update_task(db, epic_a, project_id=pa)
    feat_a = await _task(db, "Фича A", parent_id=epic_a, task_type="feature")
    leaf_a = await _task(db, "Подзадача A", parent_id=feat_a)
    epic_b = await _task(db, "Эпик B", task_type="epic")
    await repo.update_task(db, epic_b, project_id=pb)
    leaf_b = await _task(db, "Задача B", parent_id=epic_b)
    archived_a = await _task(db, "Архивная A", parent_id=epic_a)
    await repo.update_task(db, archived_a, archived=1)
    await db.commit()
    return SimpleNamespace(
        epic_a=epic_a, leaf_a=leaf_a, leaf_b=leaf_b, archived_a=archived_a
    )


async def test_decisions_come_from_task_feed_with_origin(client, db):
    w = await _world(client, db)
    # 12 записей проекта A: на эпике и на подзадаче, время растёт по минуте.
    ids = []
    for i in range(12):
        task_id = w.epic_a if i % 2 == 0 else w.leaf_a
        ids.append(
            await _decision(db, task_id, f"Решение {i}", f"2026-10-01 10:{i:02d}:00")
        )
    # Две записи с ОДИНАКОВЫМ временем: больший id идёт первым.
    tie_low = await _decision(db, w.leaf_a, "Ничья-1", "2026-10-01 11:00:00")
    tie_high = await _decision(db, w.leaf_a, "Ничья-2", "2026-10-01 11:00:00")
    # Запись агента с происхождением.
    agent_update = await _decision(
        db,
        w.leaf_a,
        "От агента",
        "2026-10-01 12:00:00",
        agent="ci-bot",
        principal_id=42,
        author_kind="principal",
    )
    # Чужой проект, архивная задача и запись другого kind с тем же текстом.
    await _decision(db, w.leaf_b, "Чужой проект", "2026-10-01 13:00:00")
    await _decision(db, w.archived_a, "Архивная", "2026-10-01 13:01:00")
    await _decision(
        db, w.leaf_a, "От агента", "2026-10-01 13:02:00", kind="status", agent="x"
    )

    resp = await client.get("/api/decisions?project=proj-a&limit=10")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source"] == "hub"
    got = body["decisions"]
    assert len(got) == 10
    # Порядок created_at DESC, id DESC: агент, ничья (id по убыванию), затем 11, 10…
    assert [d["id"] for d in got] == [
        agent_update,
        tie_high,
        tie_low,
        ids[11],
        ids[10],
        ids[9],
        ids[8],
        ids[7],
        ids[6],
        ids[5],
    ]
    assert {d["task_id"] for d in got} <= {w.epic_a, w.leaf_a}
    assert all(d["project"] == "proj-a" for d in got)
    assert all(d["content"] for d in got)
    first = got[0]
    assert set(first) == {
        "id",
        "task_id",
        "task_title",
        "project",
        "content",
        "agent",
        "principal_id",
        "author_kind",
        "created_at",
    }
    assert (first["author_kind"], first["principal_id"], first["agent"]) == (
        "principal",
        42,
        "ci-bot",
    )
    assert got[1]["author_kind"] == "hub"

    # Лимит: 1..50, по умолчанию 10.
    assert (
        len((await client.get("/api/decisions?project=proj-a")).json()["decisions"])
        == 10
    )
    assert (await client.get("/api/decisions?limit=0")).status_code == 422
    assert (await client.get("/api/decisions?limit=51")).status_code == 422
    # Неизвестный проект — пусто, а не «все проекты».
    nope = await client.get("/api/decisions?project=nope")
    assert nope.json() == {"decisions": [], "source": "hub"}

    # Дашборд проекта: 8 записей, тот же порядок и тот же проект.
    dash = (await client.get("/api/dashboard")).json()["recent_decisions"]
    assert len(dash) == 8
    from hub.services.dashboard import get_dashboard_data

    scoped = (await get_dashboard_data(db, project="proj-a")).recent_decisions
    assert len(scoped) == 8
    assert [d["id"] for d in scoped] == [d["id"] for d in got[:8]]

    # MCP: единый ответ {decisions, source: hub}; для пустого случая тоже.
    async def via_client(path, *a, **k):
        return (await client.get(path)).json()

    with patch("hub.mcp_server._api_get", new=AsyncMock(side_effect=via_client)):
        out = await mcp_server.hub_list_decisions(limit=10)
        structured = out.structuredContent
        assert structured["source"] == "hub"
        assert len(structured["decisions"]) == 10
        assert "notes_available" not in structured
        assert "notes_status" not in structured
        await db.execute("UPDATE task_updates SET kind='status' WHERE kind='decision'")
        await db.commit()
        empty = await mcp_server.hub_list_decisions(limit=10)
        assert empty.structuredContent["decisions"] == []
        assert empty.structuredContent["source"] == "hub"
        assert "notes_available" not in empty.structuredContent


async def test_real_decide_flows_land_in_the_feed(client, db):
    """decide accept и rework, commit- и state-задача, с summary и без."""
    pid = await make_project(db, "flows")
    cases = []
    for action in ("accept", "rework"):
        for summary in ("", "по существу"):
            state = await make_state_task(
                db, project_id=pid, under_feature=True, status="needs_decision"
            )
            epic = await _task(db, f"Эпик {action}", task_type="epic")
            await repo.update_task(db, epic, project_id=pid)
            commit = await _task(
                db, f"Commit {action}", parent_id=epic, status="needs_decision"
            )
            for task_id in (state, commit):
                resp = await client.post(
                    f"/api/tasks/{task_id}/decide",
                    json={
                        "action": action,
                        "decision_summary": summary,
                        "instructions": "поправить" if action == "rework" else "",
                    },
                )
                assert resp.status_code == 200, (action, resp.text)
                cases.append((task_id, summary))
    got = (await client.get("/api/decisions?project=flows&limit=50")).json()
    by_task = {d["task_id"]: d for d in got["decisions"]}
    for task_id, summary in cases:
        assert task_id in by_task, task_id
        assert by_task[task_id]["project"] == "flows"
        if summary:
            assert f"Decision: {summary}" in by_task[task_id]["content"]


async def test_list_filters_run_before_limit(db):
    """Фильтры проекта и архива — в запросе, до LIMIT (#627)."""
    pa = await make_project(db, "p-first")
    epic = await _task(db, "Эпик", task_type="epic")
    await repo.update_task(db, epic, project_id=pa)
    mine = await _task(db, "Моя", parent_id=epic)
    other = await _task(db, "Чужая")
    await _decision(db, mine, "старая моя", "2026-10-01 08:00:00")
    for i in range(5):
        await _decision(db, other, f"новая чужая {i}", f"2026-10-01 09:0{i}:00")
    got = await decisions_svc.list_decisions(db, project_id=pa, limit=1)
    assert [d["content"] for d in got] == ["старая моя"]
    await repo.update_task(db, mine, archived=1)
    await db.commit()
    assert await decisions_svc.list_decisions(db, project_id=pa, limit=1) == []


async def test_record_decision_is_a_compatible_noop(client, db):
    spawn = AsyncMock()
    for flag in (True, False):
        task_id = await _task(db, f"Решить {flag}", status="needs_decision")
        with patch("asyncio.create_subprocess_exec", new=spawn):
            resp = await client.post(
                f"/api/tasks/{task_id}/decide",
                json={
                    "action": "accept",
                    "decision_summary": "Принято, замечания косметические.",
                    "record_decision": flag,
                },
            )
        assert resp.status_code == 200, resp.text
        detail = (await client.get(f"/api/tasks/{task_id}")).json()
        feed = [u for u in detail["updates"] if u["kind"] == "decision"]
        assert len(feed) == 1
        assert "Decision: Принято, замечания косметические." in feed[0]["content"]
    # Исключение lifecycle проглатывает — поэтому проверяем сам вызов.
    spawn.assert_not_called()

    task_id = await _task(db, "Форма", status="needs_decision")
    page = await client.get(f"/tasks/{task_id}")
    assert page.status_code == 200
    assert 'name="decision_summary"' in page.text
    assert "Save decision to notes" not in page.text
    assert 'name="record_decision"' not in page.text


async def test_cli_notes_link_is_gone(client):
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("hub.integrations.notes")
    assert not hasattr(config, "N4L_BIN")
    assert not hasattr(config, "N4L_SPACE_ID")
    assert not hasattr(plugins, "notes")
    spawn = AsyncMock()
    with patch("asyncio.create_subprocess_exec", new=spawn):
        resp = await client.get("/api/integrations/notes")
    assert resp.status_code == 410
    assert "#1505" in json.dumps(resp.json(), ensure_ascii=False)
    assert "n4l" in json.dumps(resp.json(), ensure_ascii=False)
    spawn.assert_not_called()


# --- AC-4: доступ ----------------------------------------------------------


@pytest.fixture
async def hub(client, db, monkeypatch):
    monkeypatch.setattr(
        config,
        "HUB_TOKENS",
        {
            "env-human": TokenIdentity("env-h", "human"),
            "env-admin": TokenIdentity("env-ad", "admin"),
            "env-agent": TokenIdentity("env-a", "agent"),
            "env-steward": TokenIdentity("env-s", "steward"),
        },
    )
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    op = await admin_svc.create_principal(
        db, kind="human", username="op", role_slug="operator"
    )
    return SimpleNamespace(client=client, db=db, op_id=op["id"])


async def _role_headers(hub, role):
    principal = await admin_svc.create_principal(
        hub.db, kind="agent", username=f"p-{role}", role_slug=role
    )
    key = await admin_svc.create_api_key(hub.db, principal["id"], name=role)
    return _bearer(key["plaintext_key"])


async def _chat_pair_session(hub, kind):
    task_id = await _task(hub.db, f"привязка {kind}")
    await repo.record_submission(
        hub.db, task_id=task_id, generation=1, sha="a" * 40, base_branch="main"
    )
    await repo.update_task(
        hub.db,
        task_id,
        status="fix_requested" if kind == "implementer" else "review",
        submission_generation=1,
        submission_sha="a" * 40,
    )
    if kind == "implementer":
        code, _ = await cp.issue_run_code(
            hub.db, task_id, 2, issued_by_principal_id=hub.op_id
        )
    elif kind == "intake":
        code, _ = await cp.issue_code(hub.db, hub.op_id, kind="intake")
    else:
        code, _ = await cp.issue_code(
            hub.db,
            hub.op_id,
            kind="reviewer",
            bound_task_id=task_id,
            bound_generation=1,
        )
    await hub.db.commit()
    resp = await hub.client.post(
        "/api/auth/chat-pair/redeem",
        json={"code": code},
        headers={"x-forwarded-for": "203.0.113.9"},
    )
    assert resp.status_code == 200, resp.text
    return _bearer(resp.json()["token"])


async def test_decisions_access_matches_dashboard(hub):
    w = await _world(hub.client, hub.db)
    await _decision(hub.db, w.leaf_a, "Видно", "2026-10-01 10:00:00")

    who = {
        "env-human": _bearer("env-human"),
        "env-admin": _bearer("env-admin"),
        "env-agent": _bearer("env-agent"),
        "env-steward": _bearer("env-steward"),
        "operator": await _role_headers(hub, "operator"),
        "agent": await _role_headers(hub, "agent"),
        "watcher": await _role_headers(hub, "watcher"),
        "steward": await _role_headers(hub, "steward"),
        "ci_reader": await _role_headers(hub, "ci_reader"),
        "ci_auditor": await _role_headers(hub, "ci_auditor"),
        "anonymous": {},
        "chat-pair intake": await _chat_pair_session(hub, "intake"),
        "chat-pair implementer": await _chat_pair_session(hub, "implementer"),
        "chat-pair reviewer": await _chat_pair_session(hub, "reviewer"),
    }
    allowed = {"env-human", "env-admin", "env-agent", "operator", "agent", "watcher"}
    for name, headers in who.items():
        dash = await hub.client.get("/api/dashboard", headers=headers)
        dec = await hub.client.get("/api/decisions", headers=headers)
        assert dec.status_code == dash.status_code, (
            name,
            dash.status_code,
            dec.status_code,
        )
        if name in allowed:
            assert dec.status_code == 200, (name, dec.text)
            assert dec.json()["decisions"], name
        else:
            assert dec.status_code in (401, 403), (name, dec.status_code)

    # MCP ходит в REST с ПРОБЛЕМНЫМ bearer вызывающего: тот же отказ и тот же успех.
    for name, headers in who.items():

        async def call(path, *a, _h=headers, **k):
            resp = await hub.client.get(path, headers=_h)
            if resp.status_code != 200:
                raise mcp_server.HubApiError(
                    {"message": resp.text, "status": resp.status_code}
                )
            return resp.json()

        with patch("hub.mcp_server._api_get", new=AsyncMock(side_effect=call)):
            if name in allowed:
                out = await mcp_server.hub_list_decisions()
                assert out.structuredContent["source"] == "hub", name
            else:
                with pytest.raises(mcp_server.HubApiError):
                    await mcp_server.hub_list_decisions()


async def test_origin_is_shown_next_to_the_signature(client, db):
    """Подпись agent — слова клиента; проверенное авторство показывается рядом."""
    task_id = await _task(db, "Подпись")
    await _decision(
        db,
        task_id,
        "Решил агент",
        "2026-10-01 10:00:00",
        agent="human",
        principal_id=42,
        author_kind="principal",
    )
    page = await client.get("/")
    assert page.status_code == 200
    assert "human (principal #42)" in page.text

    async def via_client(path, *a, **k):
        return (await client.get(path)).json()

    with patch("hub.mcp_server._api_get", new=AsyncMock(side_effect=via_client)):
        text = json.loads(
            "".join(b.text for b in (await mcp_server.hub_list_decisions()).content)
        )["message"]
    assert "(human, principal #42)" in text
