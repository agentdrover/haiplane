"""Действующая политика проекта одной сводкой (#1457).

Сводка не толкует политику сама: каждое значение она берёт у того же читателя,
которым пользуется решатель. Поэтому тесты проверяют три вещи: что каждый ключ
виден со значением и источником на REST, CLI и MCP одинаково, что сервер
показывает запрошенный и эффективный режим стюарда и не выдаёт секретов, и что
новый ключ политики без записи в сводке роняет тест, называя ключ.
"""

from __future__ import annotations

import json
import sys
from unittest.mock import patch

import aiosqlite
import pytest
from httpx import AsyncClient

from hub import cli, config, mcp_server
from hub import repository as repo
from hub.models import GATE_POLICY_KEYS
from hub.services import effective_policy, project_policy

SECRET = "SECRET-MARKER-1457-do-not-print"  # pragma: allowlist secret


async def _project(db: aiosqlite.Connection, slug: str, policy: dict) -> int:
    pid = await repo.create_project(db, slug=slug, name=slug)
    await repo.update_project(db, pid, gate_policy=json.dumps(policy))
    await db.commit()
    return pid


def _message(result) -> str:
    """Человекочитаемая часть MCP-ответа: текст приходит эхом {"message": ...}."""
    return json.loads(result.content[0].text)["message"]


def _by_key(data: dict) -> dict[str, dict]:
    return {row["key"]: row for row in data["keys"]}


async def test_every_policy_key_shown_with_source_on_all_surfaces(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, capsys
):
    # AC-1: каждый известный ключ со значением и источником, время и автор
    # последней правки; REST, CLI и MCP говорят одно и то же.
    policy = {
        "verdict": "human",
        "submission_contract": "require",
        "executor_launch": "manual",
        "mystery_key": 1,
    }
    pid = await _project(db, "spike", policy)
    await repo.insert_event(
        db,
        kind="project_gate_policy_changed",
        project_id=pid,
        actor="human",
        payload={
            "slug": "spike",
            "changed": ["submission_contract"],
            "removed": [],
            "by": "denis",
        },
    )
    await db.commit()

    resp = await client.get("/api/projects/spike/effective-policy")
    assert resp.status_code == 200
    rest = resp.json()
    rows = _by_key(rest)
    assert sorted(rows) == sorted(GATE_POLICY_KEYS), "каждый ключ — ровно одна строка"
    assert rows["verdict"]["value"] == "human"
    assert rows["verdict"]["source"] == "project"
    assert rows["submission_contract"]["value"] == "require"
    assert rows["submission_contract"]["source"] == "project"
    assert rows["executor_launch"]["value"] == "manual"
    assert rows["executor_launch"]["source"] == "project"
    # Нет ключа в проекте: значение даёт читатель, источник — умолчание.
    assert rows["claim_area_check"]["source"] == "default"
    assert rows["claim_area_check"]["value"] == "off"
    assert rows["claim_area_check"]["default"] == "off"
    # Умолчание из серверной настройки называется сервером, а не умолчанием кода.
    assert rows["executor_task_token_ceiling"]["source"] == "server"
    assert rest["unknown_keys"] == {"mystery_key": 1}
    change = rest["last_change"]
    assert change["changed"] == ["submission_contract"]
    assert change["by"] == "denis"
    assert change["actor"] == "human"
    assert change["at"]

    # MCP: те же данные, что отдал REST.
    async def _via_client(path: str, **_: object) -> object:
        return (await client.get(path)).json()

    monkeypatch.setattr(mcp_server, "_api_get", _via_client)
    out = await mcp_server.hub_effective_policy("spike")
    assert out.structuredContent["keys"] == rest["keys"]
    assert out.structuredContent["last_change"] == rest["last_change"]
    mcp_text = _message(out)
    for key in GATE_POLICY_KEYS:
        assert key in mcp_text, key
    assert "submission_contract = require [project]" in mcp_text
    assert "claim_area_check = off [default]" in mcp_text
    assert "denis" in mcp_text

    # CLI: тот же ответ, тот же текст.
    argv = ["oc-hub", "effective-policy", "spike"]
    with (
        patch.object(sys, "argv", argv),
        patch.object(cli, "_api", return_value=rest) as api,
    ):
        assert cli.main() in (0, None)
    assert api.call_args.args[:2] == ("GET", "/api/projects/spike/effective-policy")
    assert capsys.readouterr().out.strip() == mcp_text.strip()
    with (
        patch.object(sys, "argv", argv + ["--json"]),
        patch.object(cli, "_api", return_value=rest),
    ):
        cli.main()
    assert json.loads(capsys.readouterr().out) == rest


async def test_effective_policy_unknown_project_is_404(client: AsyncClient):
    resp = await client.get("/api/projects/no-such-project/effective-policy")
    assert resp.status_code == 404


async def test_my_context_carries_the_policy_block(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # hub_my_context: задача называет проект, а блок политики идёт следом.
    await _project(db, "spike", {"verdict": "human", "claim_area_check": "warn"})

    async def _fake_get(path: str, **_: object) -> object:
        if path.startswith("/api/tasks/7/context"):
            return {
                "context_text": "Task #7",
                "task": {"project": {"id": 2, "slug": "spike"}},
            }
        return (await client.get(path)).json()

    monkeypatch.setattr(mcp_server, "_api_get", _fake_get)
    out = await mcp_server.hub_my_context(task_id=7)
    text = _message(out)
    assert "Policy of project spike" in text
    assert "claim_area_check = warn [project]" in text
    assert "hub_effective_policy" in text


async def test_server_steward_mode_and_locks_without_secrets(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, capsys
):
    # AC-2: act запрошен, замеры его не дают — виден и запрошенный, и
    # эффективный режим с причинами отказа; замок #743 на default; секретов нет.
    monkeypatch.setattr(config, "STEWARD_MODE", "act")
    monkeypatch.setattr(config, "STEWARD_HUB_TOKEN", SECRET)
    monkeypatch.setattr(config, "CURSOR_REVIEWER_HUB_TOKEN", SECRET)
    monkeypatch.setattr(config, "LOCAL_REVIEWER_HUB_TOKEN", SECRET)
    monkeypatch.setattr(config, "EXECUTOR_MODEL", "composer-test-model")
    await _project(db, "spike", {})
    await _project(db, "default", {"deep_daily_cap": 4})

    rest = (await client.get("/api/projects/default/effective-policy")).json()
    steward = rest["steward"]
    assert steward["requested"] == "act"
    assert steward["effective"] == "shadow"
    assert steward["act_refusals"], "причины отказа названы"
    assert all(r["code"] and r["detail"] for r in steward["act_refusals"])
    locks = {lock["id"]: lock for lock in rest["locks"]}
    assert locks["#743"]["applies"] is True
    assert sorted(locks["#743"]["gates"]) == ["dor", "verdict"]
    assert rest["server"]["executor_model"] == "composer-test-model"
    assert rest["server"]["steward_model"] == config.STEWARD_MODEL
    assert rest["server"]["secrets_configured"]["steward_hub_token"] is True

    other = (await client.get("/api/projects/spike/effective-policy")).json()
    assert {lock["id"]: lock for lock in other["locks"]}["#743"]["applies"] is False

    # Чтение сводки не пишет в ленту: GET не должен оставлять следов.
    refused = await repo.list_events(db, kinds=["steward_act_refused"])
    assert refused == []

    async def _via_client(path: str, **_: object) -> object:
        return (await client.get(path)).json()

    monkeypatch.setattr(mcp_server, "_api_get", _via_client)
    out = await mcp_server.hub_effective_policy("default")
    with (
        patch.object(sys, "argv", ["oc-hub", "effective-policy", "default"]),
        patch.object(cli, "_api", return_value=rest),
    ):
        cli.main()
    surfaces = [
        json.dumps(rest, ensure_ascii=False),
        json.dumps(other, ensure_ascii=False),
        out.content[0].text,
        _message(out),
        json.dumps(out.structuredContent, ensure_ascii=False, default=str),
        capsys.readouterr().out,
    ]
    for blob in surfaces:
        assert SECRET not in blob
    text = _message(out)
    assert "requested act" in text and "effective shadow" in text
    assert "#743" in text


def test_new_policy_key_without_summary_entry_fails(monkeypatch):
    # AC-3: сводка не отстаёт от правил. Перечень берётся из project_policy и
    # GATE_POLICY_KEYS, поэтому новый *_KEY без записи роняет тест и назван.
    assert effective_policy.unsummarised_keys() == [], (
        f"ключи политики без записи в сводке: {effective_policy.unsummarised_keys()}"
    )

    monkeypatch.setattr(
        project_policy, "BRAND_NEW_THING_KEY", "brand_new_policy_key", raising=False
    )
    assert effective_policy.unsummarised_keys() == ["brand_new_policy_key"]

    # Ключ, что умеет принять запись, но не умеет показать сводка.
    monkeypatch.undo()
    monkeypatch.setattr(
        "hub.models.GATE_POLICY_KEYS", (*GATE_POLICY_KEYS, "another_new_key")
    )
    assert effective_policy.unsummarised_keys() == ["another_new_key"]

    # И обратное: запись в сводке, которой нет в правилах, — тоже расхождение.
    monkeypatch.undo()
    monkeypatch.setitem(
        effective_policy.REGISTRY,
        "ghost_key",
        effective_policy.PolicyEntry(lambda policy: None, "nowhere"),
    )
    assert effective_policy.unsummarised_keys() == ["ghost_key"]
    with pytest.raises(AssertionError, match="ghost_key"):
        effective_policy.assert_summary_complete()


async def test_a_policy_patch_shows_up_in_the_summary_with_time_and_author(
    client: AsyncClient,
):
    # Правка через PATCH пишет событие с автором, и сводка называет время правки.
    resp = await client.post("/api/projects", json={"slug": "spike", "name": "Spike"})
    pid = resp.json()["id"]
    patched = await client.patch(
        f"/api/projects/{pid}", json={"gate_policy": {"claim_area_check": "warn"}}
    )
    assert patched.status_code == 200, patched.text
    data = (await client.get("/api/projects/spike/effective-policy")).json()
    change = data["last_change"]
    assert change["changed"] == ["claim_area_check"]
    assert change["at"]
    assert change["by"], "автор правки назван именем из identity"
    assert _by_key(data)["claim_area_check"]["value"] == "warn"


async def test_review_derived_from_a_delegated_verdict_names_its_source(
    client: AsyncClient, db: aiosqlite.Connection
):
    # Находка 5197fd78dd0e3efb: review=dispatch, выведенный из verdict=auto,
    # не должен читаться как умолчание или как сохранённое значение проекта.
    await _project(db, "derived-a", {"verdict": "auto"})
    await _project(db, "derived-b", {"verdict": "auto", "review": "off"})
    await _project(db, "derived-c", {"verdict": "auto", "review": "dispatch"})
    await _project(db, "derived-d", {"verdict": "human"})
    rows = {}
    for slug in ("derived-a", "derived-b", "derived-c", "derived-d"):
        data = (await client.get(f"/api/projects/{slug}/effective-policy")).json()
        rows[slug] = (_by_key(data)["review"], data)
    a, _ = rows["derived-a"]
    assert (a["value"], a["source"], a["derived_from"]) == (
        "dispatch",
        "derived",
        "verdict=auto",
    )
    assert a["default"] == "off"
    b, data_b = rows["derived-b"]
    assert (b["value"], b["source"], b["stored"]) == ("dispatch", "derived", "off")
    assert b["derived_from"] == "verdict=auto"
    text = "\n".join(effective_policy.format_effective_policy(data_b))
    assert "review = dispatch [derived from verdict=auto] (stored off)" in text
    c, _ = rows["derived-c"]
    assert (c["source"], "derived_from" in c) == ("project", False)
    d, _ = rows["derived-d"]
    assert (d["value"], d["source"]) == ("off", "default")
    # Других ключей с выводом из соседа нет: источник derived только у review.
    for _slug, (_row, data) in rows.items():
        derived = [r["key"] for r in data["keys"] if r["source"] == "derived"]
        assert derived in ([], ["review"])


async def test_my_context_names_an_unreadable_policy_instead_of_dropping_it(
    monkeypatch,
):
    # Находка e17305020f78121a: ошибка чтения не должна убирать блок молча.
    async def _fake_get(path: str, **_: object) -> object:
        if path.startswith("/api/tasks/7/context"):
            return {
                "context_text": "Task #7",
                "task": {"project": {"id": 2, "slug": "spike"}},
            }
        raise mcp_server.HubApiError({"message": "policy backend exploded"})

    monkeypatch.setattr(mcp_server, "_api_get", _fake_get)
    out = await mcp_server.hub_my_context(task_id=7)
    text = _message(out)
    assert "политика проекта не прочитана" in text
    assert "policy backend exploded" in text
