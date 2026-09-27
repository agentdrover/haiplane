"""Очередь исполнения в тени: хаб называет следующую задачу проекта (#1274).

Выбор детерминирован и объясним: кандидат — open, dor_passed, все
зависимости ДОСТАВЛЕНЫ (#484, тот же читатель, что у готовности), объявленные
области не пересекаются с задачами в running/review, WIP проекта ниже лимита.
Каждая причина пропуска названа — номер зависимости, пересекающийся путь и
задача, лимит и занимающие его задачи. В тени хаб только пишет событие и ни
одной задачи не трогает.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from httpx import AsyncClient

from hub import repository as repo
from hub.db import fetchall
from hub.services import chat_pair as cp
from hub.services import orchestrator_queue as oq
from hub.services import project_policy


async def _project(db, slug: str, policy: dict) -> dict:
    existing = await repo.get_project_by_slug(db, slug)
    if existing is not None:
        pid = int(existing["id"])
    else:
        pid = await repo.create_project(db, slug=slug, name=slug.title())
    await repo.update_project(db, pid, gate_policy=json.dumps(policy))
    await db.commit()
    return dict(await repo.get_project(db, pid))


async def _task(
    db,
    project_id: int,
    *,
    status: str = "open",
    areas: list[str] | None = None,
    priority: str = "medium",
    dor: bool = True,
    title: str = "t",
) -> int:
    from hub import services
    from hub.models import TaskCreate

    tv = await services.create_task(db, TaskCreate(title=title))
    await repo.update_task(
        db,
        tv.id,
        project_id=project_id,
        affected_areas=json.dumps(areas or []),
        priority=priority,
        dor_passed=1 if dor else 0,
    )
    if status != "open":
        await repo.update_task(db, tv.id, status=status)
    await db.commit()
    return tv.id


def _skip(answer: dict, task_id: int) -> dict:
    found = [s for s in answer["skipped"] if s["task_id"] == task_id]
    assert found, f"#{task_id} не назван среди пропущенных: {answer['skipped']}"
    return found[0]


async def test_the_next_task_skips_undelivered_dependencies_and_overlapping_areas(db):
    """AC-1: зависимость не доставлена — пропуск с её номером; пересечение
    областей с задачей в review — пропуск с путём и задачей; третья выбрана.

    Первые две выше по приоритету: выбор третьей объясняется только правилами
    пропуска, а не порядком.
    """
    project = await _project(db, "oq-ac1", {"orchestrator_queue": "shadow"})
    pid = int(project["id"])

    dep = await _task(db, pid, status="completed", areas=["docs/x.md"])
    delivered_dep = await _task(db, pid, status="completed", areas=["docs/y.md"])
    await repo.record_pipeline_merge(
        db, pr_number=1, merge_sha="abc", project_id=pid, task_id=delivered_dep
    )
    in_review = await _task(db, pid, status="review", areas=["hub/services/"])

    blocked = await _task(db, pid, areas=["hub/a.py"], priority="critical")
    await repo.add_task_dependency(db, blocked, dep)
    overlapping = await _task(db, pid, areas=["hub/services/x.py"], priority="high")
    free = await _task(db, pid, areas=["hub/c.py"], priority="low")
    # Доставленная зависимость не держит: снята по доставке, а не по статусу.
    await repo.add_task_dependency(db, free, delivered_dep)
    await db.commit()

    answer = await oq.next_task(db, project)

    assert answer["next_task_id"] == free, answer
    dep_skip = _skip(answer, blocked)
    assert dep_skip["reason"] == oq.SKIP_DEPENDENCY
    assert [b["task_id"] for b in dep_skip["blockers"]] == [dep]
    overlap = _skip(answer, overlapping)
    assert overlap["reason"] == oq.SKIP_OVERLAP
    assert overlap["path"] == "hub/services/x.py"
    assert overlap["with_task_id"] == in_review
    assert overlap["with_path"] == "hub/services/"
    assert f"#{free}" in answer["summary"]


async def test_a_broad_declaration_overlaps_both_ways(db):
    """Обход широкой декларацией (#1147): «hub/» в работе держит «hub/x.py»,
    а «hub/» у кандидата пересекается с узким путём задачи в работе."""
    project = await _project(db, "oq-broad", {})
    pid = int(project["id"])
    running = await _task(db, pid, status="running", areas=["hub/poller.py"])
    broad = await _task(db, pid, areas=["hub/"])
    sibling = await _task(db, pid, areas=["hubble/x.py"])

    answer = await oq.next_task(db, project)

    assert _skip(answer, broad)["with_task_id"] == running
    assert answer["next_task_id"] == sibling, "общий префикс строки — не общий путь"


async def test_a_full_wip_names_the_limit_and_the_tasks_holding_it(db):
    """AC-2: WIP-лимит 2, две задачи в running/review — следующей нет."""
    project = await _project(
        db, "oq-ac2", {"orchestrator_queue": "shadow", "wip_limit": 2}
    )
    pid = int(project["id"])
    holders = [
        await _task(db, pid, status="running", areas=["hub/a.py"]),
        await _task(db, pid, status="review", areas=["hub/b.py"]),
    ]
    await _task(db, pid, areas=["hub/c.py"])

    answer = await oq.next_task(db, project)

    assert answer["next_task_id"] is None
    assert answer["wip_full"] is True
    assert answer["wip_limit"] == 2
    assert answer["in_progress"] == sorted(holders)
    assert "2" in answer["summary"]
    assert all(f"#{h}" in answer["summary"] for h in holders)


async def test_below_the_wip_limit_the_queue_picks(db):
    project = await _project(db, "oq-wip-ok", {"wip_limit": 2})
    pid = int(project["id"])
    await _task(db, pid, status="running", areas=["hub/a.py"])
    free = await _task(db, pid, areas=["hub/c.py"])

    answer = await oq.next_task(db, project)

    assert answer["wip_full"] is False
    assert answer["next_task_id"] == free


async def _snapshot(db) -> list[tuple]:
    """Вся строка каждой задачи: статус, claim, ветка, воркспейс, updated_at."""
    rows = await fetchall(db, "SELECT * FROM tasks ORDER BY id")
    return [tuple(r) for r in rows]


async def _candidate_events(db, project_id: int) -> list[dict]:
    rows = await fetchall(
        db,
        "SELECT payload, task_id FROM events WHERE kind=? AND project_id=? ORDER BY id",
        (oq.EVENT_NEXT_CANDIDATE, project_id),
    )
    return [json.loads(r["payload"]) for r in rows]


async def test_shadow_mode_writes_one_event_and_touches_no_task(db):
    """AC-3: два прохода подряд без изменений — одно событие, задачи не тронуты.

    Проект в режиме off и проект с нечитаемым режимом (#835) событий не
    получают вовсе.
    """
    from hub import poller

    shadow = await _project(db, "oq-ac3", {"orchestrator_queue": "shadow"})
    off = await _project(db, "oq-off", {})
    bogus = await _project(db, "oq-bogus", {})
    # Мимо API — запись такое значение не пропустит, а читатель обязан
    # прочесть его как off.
    await repo.update_project(
        db, int(bogus["id"]), gate_policy=json.dumps({"orchestrator_queue": "on"})
    )
    await db.commit()
    await _task(db, int(shadow["id"]), status="review", areas=["hub/a.py"])
    free = await _task(db, int(shadow["id"]), areas=["hub/c.py"])
    await _task(db, int(off["id"]), areas=["hub/d.py"])
    await _task(db, int(bogus["id"]), areas=["hub/e.py"])
    before = await _snapshot(db)
    updates_before = await fetchall(db, "SELECT COUNT(*) AS n FROM task_updates")

    await poller._sweep_orchestrator_queue(db)
    await poller._sweep_orchestrator_queue(db)

    events = await _candidate_events(db, int(shadow["id"]))
    assert len(events) == 1, events
    assert events[0]["next_task_id"] == free
    assert f"#{free}" in events[0]["summary"]
    assert await _candidate_events(db, int(off["id"])) == []
    assert await _candidate_events(db, int(bogus["id"])) == []
    assert await _snapshot(db) == before, "тень не меняет ни одной задачи"
    updates_after = await fetchall(db, "SELECT COUNT(*) AS n FROM task_updates")
    assert updates_after[0]["n"] == updates_before[0]["n"]


async def test_shadow_mode_writes_again_when_the_answer_changes(db):
    from hub import poller

    shadow = await _project(db, "oq-change", {"orchestrator_queue": "shadow"})
    pid = int(shadow["id"])
    first = await _task(db, pid, areas=["hub/c.py"])
    await poller._sweep_orchestrator_queue(db)
    await repo.update_task(db, first, status="running")
    await db.commit()
    await poller._sweep_orchestrator_queue(db)

    events = await _candidate_events(db, pid)
    assert [e["next_task_id"] for e in events] == [first, None]


async def test_an_undeclared_area_is_skipped_not_assumed_free(db):
    """AC-4: пустые affected_areas — пропуск «область не объявлена»."""
    project = await _project(db, "oq-ac4", {})
    pid = int(project["id"])
    undeclared = await _task(db, pid, areas=[], priority="critical")
    declared = await _task(db, pid, areas=["hub/c.py"], priority="low")

    answer = await oq.next_task(db, project)

    assert answer["next_task_id"] == declared
    skip = _skip(answer, undeclared)
    assert skip["reason"] == oq.SKIP_UNDECLARED
    assert "область не объявлена" in skip["detail"]


async def test_order_is_priority_then_position_then_id(db):
    project = await _project(db, "oq-order", {})
    pid = int(project["id"])
    low = await _task(db, pid, areas=["a/1"], priority="low")
    later = await _task(db, pid, areas=["a/2"], priority="high")
    earlier = await _task(db, pid, areas=["a/3"], priority="high")
    await repo.update_task(db, later, position=5)
    await repo.update_task(db, earlier, position=1)
    await db.commit()

    answer = await oq.next_task(db, project)

    assert answer["next_task_id"] == earlier
    assert [c["task_id"] for c in answer["candidates"]] == [earlier, later, low]


async def test_not_ready_and_foreign_tasks_are_not_candidates(db):
    project = await _project(db, "oq-scope", {})
    other = await _project(db, "oq-other", {})
    await _task(db, int(project["id"]), areas=["a/1"], dor=False)
    await _task(db, int(other["id"]), areas=["a/2"])

    answer = await oq.next_task(db, project)

    assert answer["next_task_id"] is None
    assert answer["candidates"] == []


async def test_rest_answers_one_project_and_every_shadow_project(
    client: AsyncClient, db
):
    shadow = await _project(db, "oq-rest", {"orchestrator_queue": "shadow"})
    await _project(db, "oq-rest-off", {})
    free = await _task(db, int(shadow["id"]), areas=["hub/c.py"])
    before = await _snapshot(db)

    one = await client.get("/api/orchestrator/next?project=oq-rest-off")
    assert one.status_code == 200, one.text
    assert one.json()["projects"][0]["mode"] == "off"

    every = await client.get("/api/orchestrator/next")
    assert every.status_code == 200, every.text
    slugs = [p["project"] for p in every.json()["projects"]]
    assert slugs == ["oq-rest"], "без проекта — только проекты в тени"
    assert every.json()["projects"][0]["next_task_id"] == free

    missing = await client.get("/api/orchestrator/next?project=nope")
    assert missing.status_code == 404
    assert await _snapshot(db) == before, "чтение ничего не пишет"
    assert await _candidate_events(db, int(shadow["id"])) == []


async def test_project_status_shows_the_shadow_queue() -> None:
    """MCP: ответ очереди виден в hub_project_status — без нового инструмента."""
    from hub.mcp_server import hub_project_status

    queue = {
        "projects": [
            {
                "project": "default",
                "mode": "shadow",
                "next_task_id": 7,
                "summary": "Следующей взял бы #7.",
                "skipped": [
                    {"task_id": 8, "reason": "area_undeclared", "detail": "x"},
                ],
            }
        ]
    }

    async def fake_get(path: str, **_kw):
        return queue if path.startswith("/api/orchestrator/next") else {}

    with patch("hub.mcp_server._api_get", new=AsyncMock(side_effect=fake_get)):
        out = await hub_project_status()

    text = json.loads(out.content[0].text)["message"]
    assert "## Next Task (orchestrator queue, shadow)" in text
    assert "Следующей взял бы #7." in text
    assert "#8" in text
    assert out.structuredContent["orchestrator_queue"] == queue["projects"]


def test_cli_next_task_prints_the_answer() -> None:
    import argparse
    from io import StringIO
    from unittest.mock import MagicMock

    from hub import cli

    payload = {
        "projects": [
            {"project": "default", "mode": "shadow", "summary": "Следующей взял бы #7."}
        ]
    }
    mock_api = MagicMock(return_value=payload)
    out = StringIO()
    with patch.object(cli, "_api", mock_api), patch("sys.stdout", new=out):
        rc = cli.cmd_next_task(argparse.Namespace(project="default", json=False))
    assert rc == 0
    mock_api.assert_called_once_with("GET", "/api/orchestrator/next?project=default")
    assert "Следующей взял бы #7." in out.getvalue()


async def test_every_started_status_holds_areas_and_wip(db):
    """Находка ревью (1): ci_check и needs_decision — начатая работа.

    Их ветку ещё будут править или доставлять: область занята, слот WIP тоже.
    Множество выведено из hub/models.py, а не перечислено руками.
    """
    from hub.models import ACTIVE_STATUSES, QUEUED_STATUSES

    assert oq.STARTED_STATUSES == {s.value for s in ACTIVE_STATUSES - QUEUED_STATUSES}
    assert {"claimed", "ci_check", "needs_decision", "pending_report"} <= (
        oq.STARTED_STATUSES
    )
    assert not {"open", "draft", "completed"} & oq.STARTED_STATUSES

    for status in ("ci_check", "needs_decision"):
        project = await _project(db, f"oq-{status}", {"wip_limit": 2})
        pid = int(project["id"])
        holder = await _task(db, pid, status=status, areas=["hub/services/"])
        overlapping = await _task(db, pid, areas=["hub/services/x.py"])
        free = await _task(db, pid, areas=["hub/c.py"])

        answer = await oq.next_task(db, project)

        skip = _skip(answer, overlapping)
        assert skip["reason"] == oq.SKIP_OVERLAP, status
        assert skip["with_task_id"] == holder, status
        assert answer["in_progress"] == [holder], status
        assert answer["next_task_id"] == free, status

        await _task(db, pid, status=status, areas=["docs/z.md"])
        full = await oq.next_task(db, project)
        assert full["wip_full"] is True, f"{status} держит слот WIP"


async def test_an_undeclared_area_in_progress_blocks_the_candidate(db):
    """Находка ревью (3): незнание с той стороны — не «не пересекается»."""
    project = await _project(db, "oq-blind", {})
    pid = int(project["id"])
    blind = await _task(db, pid, status="running", areas=[])
    candidate = await _task(db, pid, areas=["hub/c.py"])

    answer = await oq.next_task(db, project)

    assert answer["next_task_id"] is None
    skip = _skip(answer, candidate)
    assert skip["reason"] == oq.SKIP_ACTIVE_UNDECLARED
    assert skip["with_task_ids"] == [blind]
    assert f"#{blind}" in skip["detail"]
    assert "не объявлена" in skip["detail"]


async def test_project_status_names_a_failed_queue_read() -> None:
    """Находка ревью (2): сбой чтения очереди назван, обзор не падает."""
    from hub.mcp_server import HubApiError, hub_project_status

    async def fake_get(path: str, **_kw):
        if path.startswith("/api/orchestrator/next"):
            raise HubApiError({"message": "очередь недоступна"})
        return {}

    with patch("hub.mcp_server._api_get", new=AsyncMock(side_effect=fake_get)):
        out = await hub_project_status()

    text = json.loads(out.content[0].text)["message"]
    assert "Next Task: не удалось прочитать очередь" in text
    assert "очередь недоступна" in text
    assert out.structuredContent["orchestrator_queue"] == []
    assert "очередь недоступна" in out.structuredContent["orchestrator_queue_error"]


# ---------------------------------------------------------------------------
# #1433: захват задачи сверяет области тем же правилом, что и очередь
# ---------------------------------------------------------------------------

_A_PATH = "hub/services/review_dispatch.py"
_B_PATH = "hub/services/"


async def _claim(db, task_id: int):
    from hub import services
    from hub.models import TaskClaim

    return await services.claim_task(db, task_id, TaskClaim(agent="exec-b"))


async def _pair_start(db, task_id: int, agent: str = ""):
    from hub import services
    from hub.models import PairGitMode, TaskPairStart

    return await services.pair_start_task(
        db,
        task_id,
        TaskPairStart(plan="p", assigned_agent=agent, git_mode=PairGitMode.remote),
    )


async def _redeem(db, task_id: int):
    acting = await cp.get_acting_agent(db)
    assert acting is not None, "исполнитель по коду не заведён — тест бы лгал"
    code, _ = await cp.issue_code(
        db, int(acting["id"]), kind="implementer", bound_task_id=task_id
    )
    return await cp.redeem_code(db, code)


_ENTRANCES = {"claim": _claim, "pair_start": _pair_start, "code": _redeem}


async def _status(db, task_id: int) -> str:
    return str(dict(await repo.get_task(db, task_id))["status"])


async def _overlap_records(db, task_id: int) -> list[str]:
    return [
        str(u["content"])
        for u in await repo.get_task_updates(db, task_id)
        if str(u["content"]).startswith(project_policy.CLAIM_AREA_MARK)
    ]


async def test_claim_refuses_overlap_on_every_entrance(db):
    """AC-1: require — claim, pair_start и вход по коду отказаны одинаково,
    ошибка называет задачу A и путь, B остаётся open."""
    project = await _project(db, "cac-require", {"claim_area_check": "require"})
    pid = int(project["id"])
    a = await _task(db, pid, status="running", areas=[_A_PATH])

    for entrance, capture in _ENTRANCES.items():
        b = await _task(db, pid, areas=[_B_PATH], title=f"b-{entrance}")
        with pytest.raises(HTTPException) as caught:
            await capture(db, b)
        assert caught.value.status_code == 409, (entrance, caught.value.detail)
        detail = caught.value.detail
        assert isinstance(detail, dict), (entrance, detail)
        assert detail["reason"] == "claim_area_overlap", (entrance, detail)
        assert detail["with_task_id"] == a, (entrance, detail)
        assert detail["path"] == _B_PATH, (entrance, detail)
        assert detail["with_path"] == _A_PATH, (entrance, detail)
        assert f"#{a}" in detail["hint"] and _A_PATH in detail["hint"], entrance
        assert await _status(db, b) == "open", entrance
        assert await _overlap_records(db, b) == [], entrance


async def test_claim_warns_once_on_overlap(db):
    """AC-2: warn — захват проходит, в карточке ровно одна запись с A и путём,
    хотя проверка встретилась дважды (claim, затем pair_start)."""
    project = await _project(db, "cac-warn", {"claim_area_check": "warn"})
    pid = int(project["id"])
    a = await _task(db, pid, status="running", areas=[_A_PATH])
    b = await _task(db, pid, areas=[_B_PATH])

    claimed = await _claim(db, b)
    assert claimed.status == "claimed"
    assert claimed.area_check is not None
    assert claimed.area_check["with_task_id"] == a
    started = await _pair_start(db, b, agent="exec-b")
    assert started.status == "running"

    records = await _overlap_records(db, b)
    assert len(records) == 1, records
    assert f"#{a}" in records[0] and _A_PATH in records[0] and _B_PATH in records[0]

    # Вход по коду в warn тоже проходит и пишет ту же запись — одну.
    coded = await _task(db, pid, areas=[_B_PATH], title="b-code")
    assert await _redeem(db, coded) is not None
    assert len(await _overlap_records(db, coded)) == 1


async def test_claim_without_overlap_or_off_is_unchanged(db):
    """AC-3: off при пересечении, require без пересечения и повторный
    pair_start своей же задачи — захват как до #1433, записей нет."""
    off = await _project(db, "cac-off", {})
    off_id = int(off["id"])
    await _task(db, off_id, status="running", areas=[_A_PATH])
    for entrance, capture in _ENTRANCES.items():
        b = await _task(db, off_id, areas=[_B_PATH], title=f"off-{entrance}")
        result = await capture(db, b)
        assert result is not None, entrance
        assert await _overlap_records(db, b) == [], entrance
        if entrance != "code":
            assert result.area_check is None, entrance

    strict = await _project(db, "cac-free", {"claim_area_check": "require"})
    sid = int(strict["id"])
    await _task(db, sid, status="running", areas=[_A_PATH])
    for entrance, capture in _ENTRANCES.items():
        free = await _task(
            db, sid, areas=[f"docs/{entrance}.md"], title=f"free-{entrance}"
        )
        assert await capture(db, free) is not None, entrance
        assert await _overlap_records(db, free) == [], entrance

    # Своя задача в claimed — начатая, но сама с собой не пересекается.
    own = await _task(db, sid, areas=["docs/own.md"], title="own")
    assert (await _claim(db, own)).status == "claimed"
    assert (await _pair_start(db, own, agent="exec-b")).status == "running"
    assert await _overlap_records(db, own) == []


async def test_claim_treats_undeclared_areas_as_unknown(db):
    """AC-4: пустые области у B или у начатой A — «область не объявлена»."""
    project = await _project(db, "cac-blind-b", {"claim_area_check": "require"})
    pid = int(project["id"])
    await _task(db, pid, status="running", areas=[_A_PATH])
    blind_b = await _task(db, pid, areas=[])
    with pytest.raises(HTTPException) as caught:
        await _claim(db, blind_b)
    assert caught.value.status_code == 409
    assert caught.value.detail["reason"] == "claim_area_undeclared"
    assert "не объявлена" in caught.value.detail["hint"]
    assert await _status(db, blind_b) == "open"

    other = await _project(db, "cac-blind-a", {"claim_area_check": "require"})
    oid = int(other["id"])
    blind_a = await _task(db, oid, status="running", areas=[])
    b = await _task(db, oid, areas=[_B_PATH])
    with pytest.raises(HTTPException) as caught:
        await _pair_start(db, b)
    assert caught.value.detail["reason"] == "claim_area_undeclared"
    assert caught.value.detail["with_task_ids"] == [blind_a]
    assert f"#{blind_a}" in caught.value.detail["hint"]
    assert await _status(db, b) == "open"


def test_claim_area_check_reads_off_by_default_and_warn_when_unreadable():
    """Нет ключа — off; опечатка — warn: хотели проверку, отказ по опечатке нельзя."""
    read = project_policy.claim_area_check_of
    assert read({}) == project_policy.CLAIM_AREA_OFF
    assert read({"claim_area_check": "require"}) == project_policy.CLAIM_AREA_REQUIRE
    assert read({"claim_area_check": "requre"}) == project_policy.CLAIM_AREA_WARN
    assert read({"claim_area_check": 1}) == project_policy.CLAIM_AREA_WARN


_WARNED = {
    "mode": "warn",
    "reason": "area_overlap",
    "detail": f"{_B_PATH} пересекается с {_A_PATH} задачи #5 в работе",
    "path": _B_PATH,
    "with_task_id": 5,
    "with_path": _A_PATH,
}


def test_cli_claim_and_pair_start_name_the_overlap() -> None:
    """Контракт #1433 в CLI: пересечение warn уходит в stderr строкой."""
    import argparse
    from io import StringIO
    from unittest.mock import MagicMock

    from hub import cli

    view = {"id": 9, "status": "claimed", "area_check": _WARNED}
    for cmd, ns in (
        (cli.cmd_claim, argparse.Namespace(task_id=9, agent="a", session_id="s")),
        (
            cli.cmd_pair_start,
            argparse.Namespace(
                task_id=9,
                plan="",
                agent="",
                branch_slug="",
                session_id="",
                git_mode="",
            ),
        ),
    ):
        err = StringIO()
        with (
            patch.object(cli, "_api", MagicMock(return_value=view)),
            patch("sys.stdout", new=StringIO()),
            patch("sys.stderr", new=err),
        ):
            assert cmd(ns) == 0
        assert "#5" in err.getvalue() and _A_PATH in err.getvalue(), cmd


async def test_mcp_claim_and_pair_start_name_the_overlap() -> None:
    """Контракт #1433 в MCP: пересечение warn — строкой в ответе захвата."""
    from hub import mcp_server

    view = {"id": 9, "status": "claimed", "area_check": _WARNED}
    with (
        patch("hub.mcp_server._api_post", new=AsyncMock(return_value=view)),
        patch("hub.mcp_server._read_task", new=AsyncMock(return_value=view)),
    ):
        for out in (
            await mcp_server.hub_claim_task(9, "a", "s"),
            await mcp_server.hub_pair_start(9),
        ):
            text = out if isinstance(out, str) else out.content[0].text
            message = json.loads(text)["message"]
            assert "claim_area_check=warn" in message, message
            assert "#5" in message and _A_PATH in message, message
