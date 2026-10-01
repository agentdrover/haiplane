"""Маршрут вердикта (#1440): кто вынесет вердикт текущей сдачи — и без вранья.

Главное здесь — AC-4: маршрут, показанный ЗАРАНЕЕ, совпадает с тем, что
решатели делают на деле. Тесты гоняют настоящие решатели
(``maybe_auto_verdict``, ``order_due_runs`` + ``start_due_runs``,
``apply_self_approval``), а не подставной маршрут: решатель получает ту же
сдачу после того, как маршрут прочитан, и исход сверяется с ним.

Приём: отчёт кладётся при выключенном автовердикте (рубильник off), чтобы
решатель не отработал раньше показа; потом рубильник включается, читается
маршрут, и только затем решатель действует.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import aiosqlite
from httpx import AsyncClient

from hub import cli, config, mcp_server
from hub.mcp_envelope import build_mutation_envelope
from hub.services import auto_verdict, steward_dispatch, steward_shadow
from hub.services.steward_applied import apply_self_approval
from hub.services.verdict_route import verdict_route
from tests.test_auto_verdict import (
    _events,
    _post_review,
    _submitted_task,
)

_FINDING = {
    "locator": "none",
    "title": "real bug",
    "severity": "high",
    "category": "correctness",
}


def _steward_on(monkeypatch, mode: str = "shadow") -> None:
    monkeypatch.setattr(config, "STEWARD_MODE", mode)
    monkeypatch.setattr(config, "STEWARD_MODEL", "gpt-5.3-codex")
    monkeypatch.setattr(config, "STEWARD_DAILY_CAP", 20)
    monkeypatch.setattr(config, "STEWARD_HUB_TOKEN", "steward-token")
    monkeypatch.setattr(config, "CURSOR_API_KEY", "cursor-key")


async def _ready(
    client: AsyncClient,
    db: aiosqlite.Connection,
    monkeypatch,
    slug: str,
    policy: dict | None,
    **review,
) -> int:
    """A submission whose report is in and which no decider has touched yet."""
    monkeypatch.setattr(config, "AUTO_APPROVE_MAX_CLASS", "off")
    task_id = await _submitted_task(client, db, slug, policy)
    await _post_review(client, task_id, **review)
    monkeypatch.setattr(config, "AUTO_APPROVE_MAX_CLASS", "r1")
    return task_id


async def _status(client: AsyncClient, task_id: int) -> dict:
    return (await client.get(f"/api/tasks/{task_id}")).json()


async def _verdicts(db: aiosqlite.Connection, task_id: int) -> list[dict]:
    return await _events(db, "review_verdict_recorded", task_id)


async def test_clean_delegated_submission_routes_to_policy(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
) -> None:
    # AC-1: #1385 поколение 2 — verdict=steward, стюард в тени, сдача с model,
    # чистый отчёт, CI зелёный, дифф в областях.
    _steward_on(monkeypatch)
    task_id = await _ready(
        client, db, monkeypatch, "route-clean", {"verdict": "steward"}
    )

    route = await verdict_route(db, task_id, observe=True)
    assert route.decider == "policy" and route.final == "policy"
    assert route.code == auto_verdict.CODE_APPROVE
    assert "чистая сдача, вердикт делегирован" in route.reason
    assert route.condition == ""

    # Показ без сети называет то, чего не проверял, а не молчит.
    offline = await verdict_route(db, task_id)
    assert offline.decider == "policy" and offline.condition
    assert "branch" in offline.pending

    # Каждый выход несёт тот же ответ.
    card = await _status(client, task_id)
    assert card["verdict_route"]["decider"] == "policy"
    assert card["verdict_route"]["line"].startswith("вердикт: автопилот")
    envelope = build_mutation_envelope(card)
    assert envelope["actor_hint"] != "human"
    assert "Команда вердикта не нужна" in envelope["next_action"]
    brief = (await client.get(f"/api/tasks/{task_id}/review-brief")).json()
    assert brief["verdict_route"]["decider"] == "policy"
    assert brief["verdict_route"]["condition"] == ""
    assert mcp_server._verdict_route_line(card) == card["verdict_route"]["line"]
    rest = (await client.get(f"/api/tasks/{task_id}/verdict-route")).json()
    assert rest["decider"] == "policy"
    page = (await client.get(f"/tasks/{task_id}")).text
    assert card["verdict_route"]["line"] in page

    # AC-4: решатель делает ровно это.
    assert await auto_verdict.maybe_auto_verdict(db, task_id) is True
    verdicts = await _verdicts(db, task_id)
    assert verdicts and verdicts[-1]["actor"] == "policy"


async def test_dirty_or_undeclared_submission_routes_to_human_with_reason(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
) -> None:
    # AC-2: с подтверждёнными находками стюард судит в тени, вердикт за
    # человеком; чистая сдача без model — undeclared_model (#1384).
    _steward_on(monkeypatch)
    dirty = await _ready(
        client,
        db,
        monkeypatch,
        "route-dirty",
        {"verdict": "steward"},
        findings_confirmed=[_FINDING],
    )
    route = await verdict_route(db, dirty, observe=True)
    assert (route.decider, route.mode, route.final) == ("steward", "shadow", "human")
    assert "в тени" in route.reason
    card = await _status(client, dirty)
    assert build_mutation_envelope(card)["actor_hint"] == "human"
    assert "судит steward" in card["verdict_route"]["line"]

    undeclared = await _ready(
        client, db, monkeypatch, "route-undeclared", {"verdict": "steward"}
    )
    await db.execute("UPDATE tasks SET submission_model='' WHERE id=?", (undeclared,))
    await db.commit()
    blind = await verdict_route(db, undeclared, observe=True)
    assert blind.decider == "human" and blind.code == "undeclared_model"
    assert (
        build_mutation_envelope(await _status(client, undeclared))["actor_hint"]
        == "human"
    )

    # AC-4: автопилот молчит на обеих, вердикта нет; стюарда на грязной
    # заказывают и вердикт применять нечем (тень), на слепой он отказывает
    # тем же словом, что показал маршрут.
    for task_id in (dirty, undeclared):
        assert await auto_verdict.maybe_auto_verdict(db, task_id) is False
    assert await steward_dispatch.order_due_runs(db) == 2
    with patch(
        "hub.integrations.cursor_cloud.create_agent_attempt",
        new=AsyncMock(return_value=(None, None)),
    ):
        await steward_shadow.start_due_runs(db)
    rows = await db.execute_fetchall(
        "SELECT status, closed_reason FROM steward_runs WHERE task_id=?",
        (undeclared,),
    )
    assert rows and "undeclared_model" in rows[0]["closed_reason"]
    for task_id in (dirty, undeclared):
        assert await apply_self_approval(db, task_id, 1) is None
        assert not await _verdicts(db, task_id)
        assert (await _status(client, task_id))["status"] == "review"


async def test_default_project_and_unknowns_route_to_human(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
) -> None:
    # AC-3: замок #743, нечитаемая политика, отчёта нет — всегда человек
    # с названной причиной, не автопилот.
    _steward_on(monkeypatch)
    locked = await _ready(client, db, monkeypatch, "route-lock", None)
    # Замок читается по slug проекта: этот и есть «default» (сам хаб).
    await db.execute("UPDATE projects SET slug='default' WHERE slug='route-lock'")
    await db.commit()
    route = await verdict_route(db, locked, observe=True)
    assert route.decider == "human" and route.code == "gate_lock_743"
    assert "#743" in route.reason

    unreadable = await _ready(
        client, db, monkeypatch, "route-garbled", {"verdict": "auto"}
    )
    await db.execute(
        "UPDATE projects SET gate_policy='{not json' WHERE slug='route-garbled'"
    )
    await db.commit()
    garbled = await verdict_route(db, unreadable, observe=True)
    assert garbled.decider == "human" and garbled.code == "policy_unreadable"

    monkeypatch.setattr(config, "AUTO_APPROVE_MAX_CLASS", "off")
    bare = await _submitted_task(client, db, "route-noreport", {"verdict": "auto"})
    monkeypatch.setattr(config, "AUTO_APPROVE_MAX_CLASS", "r1")
    missing = await verdict_route(db, bare, observe=True)
    assert missing.decider == "human" and missing.code == "no_report"

    # AC-4: ни один из них решатель не одобряет.
    for task_id in (locked, unreadable, bare):
        assert await auto_verdict.maybe_auto_verdict(db, task_id) is False
        assert not await _verdicts(db, task_id)
        assert (await _status(client, task_id))["status"] == "review"


async def test_route_matches_what_the_deciders_actually_do(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
) -> None:
    # AC-4, сквозной: для каждого случая маршрут, прочитанный ДО решателя,
    # называет того, кто потом записал вердикт (или никого, если за человеком).
    _steward_on(monkeypatch)
    cases = {
        "clean": ({"verdict": "auto"}, {}, "policy"),
        "finding": ({"verdict": "auto"}, {"findings_confirmed": [_FINDING]}, "human"),
        "human-policy": ({"verdict": "human"}, {}, "human"),
        "no-policy": (None, {}, "human"),
    }
    for slug, (policy, review, expected) in cases.items():
        task_id = await _ready(
            client, db, monkeypatch, f"match-{slug}", policy, **review
        )
        route = await verdict_route(db, task_id, observe=True)
        await auto_verdict.maybe_auto_verdict(db, task_id)
        who = [v["actor"] for v in await _verdicts(db, task_id)]
        assert route.final == expected, slug
        assert (who == ["policy"]) == (expected == "policy"), slug
        assert (not who) == (expected == "human"), slug


async def test_a_task_outside_review_has_no_route(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
) -> None:
    # Конверт для остальных статусов не меняется (scope_out).
    task_id = await _ready(client, db, monkeypatch, "route-after", {"verdict": "auto"})
    await auto_verdict.maybe_auto_verdict(db, task_id)
    card = await _status(client, task_id)
    assert card["status"] != "review" and card["verdict_route"] is None
    assert (await verdict_route(db, task_id)).decider == "none"
    assert build_mutation_envelope(card)["actor_hint"] == "agent"


async def test_cli_prints_the_route_line(capsys, monkeypatch) -> None:
    route = {"decider": "human", "final": "human", "line": "вердикт: человек — x"}
    monkeypatch.setattr(cli, "_api", lambda *a, **k: {"id": 1, "verdict_route": route})
    assert cli.cmd_status(type("A", (), {"task_id": 1})()) == 0
    assert "вердикт: человек — x" in capsys.readouterr().err
    assert json.loads(json.dumps(route))["decider"] == "human"


_ROUTE = {
    "decider": "policy",
    "final": "policy",
    "reason": "чистая сдача, вердикт делегирован",
    "line": "вердикт: автопилот (политика проекта) — чистая сдача, вердикт делегирован",
}


async def test_mutation_envelope_of_a_review_task_reads_the_route(monkeypatch) -> None:
    # Ответ мутации — не обогащённая карточка: маршрут дочитывается, и конверт
    # для задачи в review называет того, кто действует, а не гадает по статусу.
    async def fake_get(path: str, *a, **k) -> dict:
        return {"id": 7, "status": "review", "verdict_route": _ROUTE}

    monkeypatch.setattr(mcp_server, "_api_get", fake_get)
    out = await mcp_server._task_mutation_response(
        7, "сдано", prior_status="running", task={"id": 7, "status": "review"}
    )
    payload = json.loads(out)
    assert payload["status"] == "review" and payload["actor_hint"] == "none"
    assert "Команда вердикта не нужна" in payload["next_action"]


async def test_hub_task_status_prints_the_route_line(monkeypatch) -> None:
    task = {
        "id": 7,
        "title": "t",
        "status": "review",
        "created_at": "2026-01-01",
        "verdict_route": _ROUTE,
    }

    async def fake_get(path: str, *a, **k) -> dict:
        return task

    async def fake_post(path: str, *a, **k) -> dict:
        return {}

    monkeypatch.setattr(mcp_server, "_api_get", fake_get)
    monkeypatch.setattr(mcp_server, "_api_post", fake_post)
    from tests.test_mcp_server import _mcp_text

    text = _mcp_text(await mcp_server.hub_task_status(7))
    assert _ROUTE["line"] in text
