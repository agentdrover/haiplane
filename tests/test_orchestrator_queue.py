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

    await oq.run_sweep(db)
    await oq.run_sweep(db)

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
    shadow = await _project(db, "oq-change", {"orchestrator_queue": "shadow"})
    pid = int(shadow["id"])
    first = await _task(db, pid, areas=["hub/c.py"])
    await oq.run_sweep(db)
    await repo.update_task(db, first, status="running")
    await db.commit()
    await oq.run_sweep(db)

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


@pytest.mark.parametrize("entrance", ["claim", "pair_start"])
async def test_concurrent_overlapping_captures_let_exactly_one_through(
    db, entrance: str
):
    """Находка ревью c2af5487: два одновременных захвата пересекающихся open-
    задач в require — проходит ровно один, второй отказан с именем соседа.

    Окно между сверкой и записью перехода закрыто бронью: без неё оба
    захвата читают базу, где начатых ещё нет, и проходят оба. Проверено на
    claim и на pair_start; броней после захвата и отказа не остаётся.
    """
    import asyncio

    project = await _project(db, "cac-race", {"claim_area_check": "require"})
    pid = int(project["id"])
    capture = _ENTRANCES[entrance]
    first = await _task(db, pid, areas=["hub/race/"], title="x")
    second = await _task(db, pid, areas=["hub/race/a.py"], title="y")

    real_transition = repo.transition_status_if

    async def slow_transition(*args, **kwargs):
        # Окно #365 K4 шире, чем даёт планировщик сам: без него бронь,
        # снятая ДО записи перехода, проходила бы тест по везению.
        await asyncio.sleep(0.2)
        return await real_transition(*args, **kwargs)

    async def later(task_id: int):
        # Второй приходит, когда сверка первого уже позади, а переход ещё
        # не записан: ровно то окно, которое держит бронь.
        await asyncio.sleep(0.05)
        return await capture(db, task_id)

    with patch.object(repo, "transition_status_if", slow_transition):
        results = await asyncio.gather(
            capture(db, first), later(second), return_exceptions=True
        )

    passed = [r for r in results if not isinstance(r, BaseException)]
    refused = [r for r in results if isinstance(r, HTTPException)]
    assert len(passed) == 1 and len(refused) == 1, [
        getattr(r, "status", r) for r in results
    ]
    winner, loser = first, second
    assert passed[0].id == winner
    assert refused[0].status_code == 409, refused[0].detail
    assert refused[0].detail["reason"] == "claim_area_overlap"
    assert refused[0].detail["with_task_id"] == winner
    assert await _status(db, loser) == "open"
    assert oq._CAPTURES == {}, oq._CAPTURES


async def test_start_task_checks_areas_too(db):
    """Находка ревью 13906dae: start_task — четвёртый вход, та же сверка."""
    from hub import services
    from hub.models import TaskStart

    project = await _project(db, "cac-start", {"claim_area_check": "require"})
    pid = int(project["id"])
    a = await _task(db, pid, status="running", areas=[_A_PATH])
    b = await _task(db, pid, areas=[_B_PATH])

    with pytest.raises(HTTPException) as caught:
        await services.start_task(db, b, TaskStart(plan="p"))

    assert caught.value.status_code == 409, caught.value.detail
    assert caught.value.detail["reason"] == "claim_area_overlap"
    assert caught.value.detail["with_task_id"] == a
    assert await _status(db, b) == "open"
    assert not await repo.has_plan_updates(db, b), "план записан до отказа"


# ---- #1455: очередь выдаёт только листовые задачи ----


async def test_queue_never_hands_out_a_task_with_open_children(db, monkeypatch):
    """AC-1: фича F (dor_passed, выше в очереди) с незавершённой подзадачей T —
    F пропущена с названной причиной, выдана T; провайдер на F не вызван.

    Инцидент 28.09 (#1455): кнопка запуска выдала облачному исполнителю фичу,
    родителя шести подзадач, потому что очередь отсеивала только эпики. Здесь
    же задача типа task с незавершённой (даже draft) подзадачей и эпик — тоже
    не кандидаты, каждый с причиной.
    """
    from hub.integrations import cursor_cloud
    from hub.services import executor_launch as el
    from tests.test_executor_dispatch import (
        _CREATED,
        _creator,
        _human,
        _launch_config,
        _launch_project,
    )

    _launch_config(monkeypatch)
    calls = _creator(monkeypatch, [_CREATED])
    project, stock = await _launch_project(db, slug="oq-leaf")
    await repo.update_task(db, stock, dor_passed=0)
    pid = int(project["id"])

    feature = await _task(db, pid, areas=["hub/f.py"], priority="critical")
    await repo.update_task(db, feature, task_type="feature")
    child = await _task(db, pid, areas=["hub/t.py"], priority="low")
    await repo.update_task(db, child, task_type="subtask", parent_id=feature)
    parent = await _task(db, pid, areas=["hub/p.py"], priority="high")
    kid = await _task(db, pid, status="draft", areas=["hub/k.py"], dor=False)
    await repo.update_task(db, kid, task_type="subtask", parent_id=parent)
    epic = await _task(db, pid, areas=["hub/e.py"], priority="critical")
    await repo.update_task(db, epic, task_type="epic")
    await db.commit()

    answer = await oq.next_task(db, project)

    assert answer["next_task_id"] == child, answer
    feature_skip = _skip(answer, feature)
    assert feature_skip["reason"] == oq.SKIP_NOT_LEAF
    assert f"#{child}" in feature_skip["detail"]
    parent_skip = _skip(answer, parent)
    assert parent_skip["reason"] == oq.SKIP_NOT_LEAF
    assert f"#{kid}" in parent_skip["detail"]
    assert _skip(answer, epic)["reason"] == oq.SKIP_NOT_LEAF
    assert f"#{feature}" in answer["summary"]

    human = await _human(db)
    result = await el.launch_executor(db, project, issuer_principal_id=human)
    assert result.launched and result.task_id == child, result
    assert [c["name"] for c in calls] == [
        cursor_cloud.agent_marker("executor", child, 1, 1)
    ]

    # Подзадача закрыта: у фичи незавершённых детей нет, но фича — не лист
    # для исполнителя; родитель-задача всё ещё держит draft-подзадачу.
    await repo.update_task(db, child, status="completed")
    await db.commit()
    answer = await oq.next_task(db, project)
    assert answer["next_task_id"] is None, answer
    assert _skip(answer, feature)["reason"] == oq.SKIP_NOT_LEAF
    result = await el.launch_executor(db, project, issuer_principal_id=human)
    assert not result.launched
    assert result.reason.startswith(el.REASON_NO_CANDIDATE), result
    assert len(calls) == 1, "провайдер на фичу не вызван"


async def test_a_deferred_task_is_skipped_until_the_wait_lapses(db):
    """#1527: отложенную задачу (действующее ожидание #957) очередь не берёт —
    ни показ, ни запуск исполнителя; просроченное ожидание задачу не держит."""
    project = await _project(db, "oq-deferred", {"orchestrator_queue": "shadow"})
    pid = int(project["id"])
    deferred = await _task(db, pid, areas=["hub/d.py"], priority="critical")
    free = await _task(db, pid, areas=["hub/f.py"], priority="low")
    await db.execute(
        "UPDATE tasks SET waiting_for='ответ', waiting_until='2999-01-01 00:00:00' "
        "WHERE id=?",
        (deferred,),
    )
    await db.commit()

    answer = await oq.next_task(db, project)
    assert answer["next_task_id"] == free, answer
    assert _skip(answer, deferred)["reason"] == oq.SKIP_DEFERRED

    await db.execute(
        "UPDATE tasks SET waiting_until='2000-01-01 00:00:00' WHERE id=?", (deferred,)
    )
    await db.commit()
    assert (await oq.next_task(db, project))["next_task_id"] == deferred


# ---------------------------------------------------------------------------
# #1647: задача-состояние не область кода
# ---------------------------------------------------------------------------


async def _as_state(db, task_id: int) -> int:
    from hub.models import TaskRefine

    await repo.update_task_structured(
        db, task_id, TaskRefine(result_kind="state", rollback="вернуть как было")
    )
    await db.commit()
    return task_id


async def test_state_task_is_not_an_undeclared_code_area(db):
    """AC-9: claim_area_check=require — state стартует, соседи не блокируются.

    Активная state-задача без affected_areas не считается неизвестной областью
    кода (active_area_undeclared) для свободной commit-задачи; сама state не
    сверяется по путям и стартует рядом с активной commit-задачей.
    """
    project = await _project(
        db,
        "state-area",
        {"claim_area_check": "require", "orchestrator_queue": "shadow"},
    )
    pid = int(project["id"])

    active_state = await _as_state(db, await _task(db, pid, status="running", areas=[]))
    commit_free = await _task(db, pid, areas=["docs/free.md"], title="commit")

    answer = await oq.next_task(db, project)
    assert answer["next_task_id"] == commit_free, answer
    assert not [
        s for s in answer["skipped"] if s["reason"] == oq.SKIP_ACTIVE_UNDECLARED
    ], answer["skipped"]
    claimed = await _claim(db, commit_free)
    assert claimed.status == "claimed", (
        "commit-задача не блокируется как active_area_undeclared"
    )

    # Обратное направление: state старует рядом с активной commit-задачей.
    other = await _project(
        db,
        "state-area-2",
        {"claim_area_check": "require", "orchestrator_queue": "shadow"},
    )
    oid = int(other["id"])
    await _task(db, oid, status="running", areas=[_A_PATH], title="active commit")
    candidate = await _as_state(db, await _task(db, oid, areas=[], title="state cand"))
    assert (await _claim(db, candidate)).status == "claimed"
    assert (await _pair_start(db, candidate, agent="exec-b")).status == "running"
    assert await _overlap_records(db, candidate) == []
    assert await _status(db, active_state) == "running"


# ---------------------------------------------------------------------------
# #1674: сессия объявляет проекты; захват задачи чужого проекта предупреждается
# ---------------------------------------------------------------------------


async def _session(db, session_id: str, projects: list[str] | None = None) -> None:
    from hub import services
    from hub.models import SessionRegister

    for slug in projects or []:
        await _project(db, slug, {})
    await services.register_session(
        db,
        SessionRegister(session_id=session_id, projects=projects or []),
        agent="exec-b",
        principal_id=None,
    )


async def _claim_as(db, task_id: int, session_id: str):
    from hub import services
    from hub.models import TaskClaim

    return await services.claim_task(
        db, task_id, TaskClaim(agent="exec-b", session_id=session_id)
    )


async def _pair_start_as(db, task_id: int, session_id: str):
    from hub import services
    from hub.models import PairGitMode, TaskPairStart

    return await services.pair_start_task(
        db,
        task_id,
        TaskPairStart(
            plan="p",
            assigned_agent="exec-b",
            session_id=session_id,
            git_mode=PairGitMode.remote,
        ),
    )


async def _project_records(db, task_id: int) -> list[str]:
    return [
        str(u["content"])
        for u in await repo.get_task_updates(db, task_id)
        if str(u["content"]).startswith(project_policy.CLAIM_PROJECT_MARK)
    ]


def _warned(task_project: str) -> dict:
    return {
        "mode": "warn",
        "task_project": task_project,
        "session_projects": ["pc-home"],
    }


async def _through_rest(client, monkeypatch):
    """MCP-инструменты поверх настоящего REST: ответ захвата — тот, что отдал сервер."""
    from hub import mcp_server

    async def post(path: str, body=None, **_kw):
        resp = await client.post(path, json=body)
        assert resp.status_code < 400, resp.text
        return resp.json()

    async def get(path: str, **_kw):
        return (await client.get(path)).json()

    monkeypatch.setattr(mcp_server, "_api_post", post)
    monkeypatch.setattr(mcp_server, "_api_get", get)
    return mcp_server


async def test_claim_of_a_foreign_project_task_warns(db, client, monkeypatch):
    """AC-1: warn — захват прошёл, одна alert-запись с обоими slug, REST и
    structured MCP-результат несут project_check; повторный claim — без второй."""
    project = await _project(db, "pc-foreign", {"claim_project_check": "warn"})
    pid = int(project["id"])
    await _session(db, "s-warn", ["pc-home"])

    task = await _task(db, pid, title="foreign-db")
    claimed = await _claim_as(db, task, "s-warn")
    assert claimed.status == "claimed"
    assert claimed.project_check == _warned("pc-foreign")
    records = await _project_records(db, task)
    assert len(records) == 1, records
    assert "pc-home" in records[0] and "pc-foreign" in records[0]
    again = await _claim_as(db, task, "s-warn")
    assert again.status == "claimed"
    assert await _project_records(db, task) == records

    rest_task = await _task(db, pid, title="foreign-rest")
    resp = await client.post(
        f"/api/tasks/{rest_task}/claim",
        json={"agent": "exec-b", "session_id": "s-warn"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["project_check"] == _warned("pc-foreign")
    assert len(await _project_records(db, rest_task)) == 1

    mcp_task = await _task(db, pid, title="foreign-mcp")
    mcp_server = await _through_rest(client, monkeypatch)
    out = json.loads(await mcp_server.hub_claim_task(mcp_task, "exec-b", "s-warn"))
    assert out["project_check"] == _warned("pc-foreign")
    assert "pc-foreign" in out["message"] and "pc-home" in out["message"]
    assert len(await _project_records(db, mcp_task)) == 1


async def test_pair_start_of_a_foreign_project_task_warns(db, client, monkeypatch):
    """AC-2: pair-start из open и из собственного claimed — то же поведение."""
    project = await _project(db, "pc-pair", {"claim_project_check": "warn"})
    pid = int(project["id"])
    await _session(db, "s-pair", ["pc-home"])

    from_open = await _task(db, pid, title="pair-open")
    started = await _pair_start_as(db, from_open, "s-pair")
    assert started.status == "running"
    assert started.project_check == _warned("pc-pair")
    assert len(await _project_records(db, from_open)) == 1

    from_claimed = await _task(db, pid, title="pair-claimed")
    claimed = await _claim_as(db, from_claimed, "s-pair")
    assert claimed.project_check == _warned("pc-pair")
    started = await _pair_start_as(db, from_claimed, "s-pair")
    assert started.status == "running"
    assert started.project_check == _warned("pc-pair")
    assert len(await _project_records(db, from_claimed)) == 1

    rest_task = await _task(db, pid, title="pair-rest")
    resp = await client.post(
        f"/api/tasks/{rest_task}/pair-start",
        json={
            "plan": "p",
            "assigned_agent": "exec-b",
            "session_id": "s-pair",
            "git_mode": "remote",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["project_check"] == _warned("pc-pair")

    mcp_task = await _task(db, pid, title="pair-mcp")
    mcp_server = await _through_rest(client, monkeypatch)
    out = json.loads(
        await mcp_server.hub_pair_start(
            mcp_task,
            plan="p",
            assigned_agent="exec-b",
            session_id="s-pair",
            git_mode="remote",
        )
    )
    assert out["project_check"] == _warned("pc-pair")
    assert len(await _project_records(db, mcp_task)) == 1


async def _activity_count(db) -> int:
    rows = await fetchall(db, "SELECT COUNT(*) FROM activity_log")
    return int(rows[0][0])


async def test_claim_project_check_require_refuses_without_side_effects(db):
    """AC-3: require — 409 project_check_refused до записывающей части
    area-check; статус, claim, план, ветка, job, лента и activity не менялись."""
    project = await _project(
        db,
        "pc-require",
        {"claim_project_check": "require", "claim_area_check": "warn"},
    )
    pid = int(project["id"])
    await _task(db, pid, status="running", areas=[_A_PATH])
    await _session(db, "s-req", ["pc-home"])

    for entrance, capture in (("claim", _claim_as), ("pair_start", _pair_start_as)):
        b = await _task(db, pid, areas=[_B_PATH], title=f"req-{entrance}")
        feed = [dict(u) for u in await repo.get_task_updates(db, b)]
        before = dict(await repo.get_task(db, b))
        activity = await _activity_count(db)
        with pytest.raises(HTTPException) as caught:
            await capture(db, b, "s-req")
        assert caught.value.status_code == 409, entrance
        detail = caught.value.detail
        assert detail["reason"] == "project_check_refused", (entrance, detail)
        assert detail["task_project"] == "pc-require"
        assert detail["session_projects"] == ["pc-home"]
        assert dict(await repo.get_task(db, b)) == before, entrance
        assert [dict(u) for u in await repo.get_task_updates(db, b)] == feed
        assert await _activity_count(db) == activity, entrance
        assert await _overlap_records(db, b) == [], entrance

    # pair-start из собственного claimed: проекты объявлены ПОСЛЕ захвата.
    late = await _task(db, pid, areas=[_B_PATH], title="req-late")
    await _session(db, "s-late")
    assert (await _claim_as(db, late, "s-late")).status == "claimed"
    await _session(db, "s-late", ["pc-home"])
    before = dict(await repo.get_task(db, late))
    feed = [dict(u) for u in await repo.get_task_updates(db, late)]
    with pytest.raises(HTTPException) as caught:
        await _pair_start_as(db, late, "s-late")
    assert caught.value.detail["reason"] == "project_check_refused"
    assert dict(await repo.get_task(db, late)) == before
    assert [dict(u) for u in await repo.get_task_updates(db, late)] == feed


async def test_project_check_is_silent_or_independent(db):
    """AC-4: молчит без регистрации, при пустых projects, для своего проекта и
    при off; не зависит от area-check (off) и в require отказывает."""
    own = await _project(db, "pc-own", {"claim_project_check": "warn"})
    off = await _project(db, "pc-off", {"claim_project_check": "off"})
    await _session(db, "s-empty")
    await _session(db, "s-decl", ["pc-home"])
    await _session(db, "s-own", ["pc-own"])

    cases = [
        (int(own["id"]), "s-unregistered"),
        (int(own["id"]), "s-empty"),
        (int(own["id"]), "s-own"),
        (int(off["id"]), "s-decl"),
    ]
    for n, (project_id, session_id) in enumerate(cases):
        task = await _task(db, project_id, title=f"silent-{n}")
        claimed = await _claim_as(db, task, session_id)
        assert claimed.status == "claimed", cases[n]
        assert claimed.project_check is None or claimed.project_check["mode"] == "off"
        assert await _project_records(db, task) == [], cases[n]

    warn = await _project(
        db, "pc-indep", {"claim_project_check": "warn", "claim_area_check": "off"}
    )
    task = await _task(db, int(warn["id"]), title="indep-warn")
    claimed = await _claim_as(db, task, "s-decl")
    assert claimed.project_check == _warned("pc-indep")
    assert len(await _project_records(db, task)) == 1

    strict = await _project(
        db,
        "pc-indep-req",
        {"claim_project_check": "require", "claim_area_check": "off"},
    )
    task = await _task(db, int(strict["id"]), title="indep-require")
    with pytest.raises(HTTPException) as caught:
        await _claim_as(db, task, "s-decl")
    assert caught.value.status_code == 409
    assert caught.value.detail["reason"] == "project_check_refused"
    assert await _status(db, task) == "open"

    # Нет ключа — warn (умолчание AC-7): проверка включена и там, где её не просили.
    keyless = await _project(db, "pc-keyless", {})
    task = await _task(db, int(keyless["id"]), title="keyless")
    assert (await _claim_as(db, task, "s-decl")).project_check == _warned("pc-keyless")


def test_cli_claim_names_the_foreign_project() -> None:
    """Контракт #1674 в CLI: чужой проект уходит в stderr строкой."""
    import argparse
    from io import StringIO
    from unittest.mock import MagicMock

    from hub import cli

    view = {"id": 9, "status": "claimed", "project_check": _warned("pc-foreign")}
    err = StringIO()
    with (
        patch.object(cli, "_api", MagicMock(return_value=view)),
        patch("sys.stdout", new=StringIO()),
        patch("sys.stderr", new=err),
    ):
        assert (
            cli.cmd_claim(argparse.Namespace(task_id=9, agent="a", session_id="s")) == 0
        )
    assert "pc-foreign" in err.getvalue() and "pc-home" in err.getvalue()


@pytest.mark.parametrize("entrance", ["claim", "pair_start"])
@pytest.mark.parametrize("change", ["session_projects", "task_project"])
async def test_require_is_rechecked_under_the_capture_write_lock(
    db, db_dsn, entrance: str, change: str
):
    """Находка ревью P2-1: конкурентная запись, взявшая write-лок раньше захвата,
    меняет projects сессии или проект задачи — require отказывает по новому
    состоянию, захвата нет. Проверка до транзакции захвата это пропускала."""
    import asyncio

    from hub import db as db_module

    await _session(db, "s-race", ["pc-race-own"])
    own = await _project(db, "pc-race-own", {"claim_project_check": "require"})
    other = await _project(db, "pc-race-other", {"claim_project_check": "require"})
    task = await _task(db, int(own["id"]), title="race")
    capture = {"claim": _claim_as, "pair_start": _pair_start_as}[entrance]

    locked, release = asyncio.Event(), asyncio.Event()

    async def concurrent_writer() -> None:
        conn = await db_module.connect(db_dsn)
        try:
            await conn.execute("BEGIN IMMEDIATE")
            if change == "session_projects":
                await conn.execute(
                    "UPDATE agent_sessions SET projects = ? WHERE session_id = ?",
                    (json.dumps(["pc-race-other"]), "s-race"),
                )
            else:
                await conn.execute(
                    "UPDATE tasks SET project_id = ? WHERE id = ?",
                    (int(other["id"]), task),
                )
            locked.set()
            await release.wait()
            await conn.commit()
        finally:
            await conn.close()

    writer = asyncio.create_task(concurrent_writer())
    await locked.wait()
    before = dict(await repo.get_task(db, task))
    captured = asyncio.create_task(capture(db, task, "s-race"))
    await asyncio.sleep(0.3)
    assert not captured.done(), "захват должен ждать write-лок, а не читать мимо него"
    release.set()
    await writer
    with pytest.raises(HTTPException) as caught:
        await captured
    assert caught.value.status_code == 409
    assert caught.value.detail["reason"] == "project_check_refused"
    after = dict(await repo.get_task(db, task))
    if change == "task_project":
        before["project_id"] = after["project_id"] = None
    assert after == before, "захвата нет: ни claim, ни статуса, ни сессии"
    session = dict(await repo.get_agent_session(db, "s-race"))
    assert session["current_task_id"] is None
    assert await _project_records(db, task) == []


async def test_overlapping_captures_write_one_project_alert(db, db_dsn):
    """Находка ревью P2-2: проверка «есть ли запись» и вставка — одна
    транзакция; два перекрывающихся вызова дают одну alert-запись."""
    import asyncio

    from hub import db as db_module
    from hub.services import session_project_check as spc

    project = await _project(db, "pc-dedup", {})
    task = await _task(db, int(project["id"]), title="dedup")
    check = {"mode": "warn", "task_project": "pc-dedup", "session_projects": ["x"]}
    db2 = await db_module.connect(db_dsn)

    arrived = 0
    both = asyncio.Event()
    real = repo.get_task_updates

    async def meeting(conn, task_id):
        nonlocal arrived
        rows = await real(conn, task_id)
        arrived += 1
        if arrived >= 2:
            both.set()
        try:
            await asyncio.wait_for(both.wait(), 0.5)
        except TimeoutError:
            pass
        return rows

    try:
        with patch.object(repo, "get_task_updates", meeting):
            await asyncio.gather(
                spc.record_foreign_project(db, task, "s-1", check),
                spc.record_foreign_project(db2, task, "s-1", check),
            )
    finally:
        await db2.close()
    assert len(await _project_records(db, task)) == 1


class _SpyGit:
    """git_ops, который смотрит, не открыта ли write-транзакция, и может мешать."""

    def __init__(self, db, *, fail=None, before_return=None) -> None:
        self.db = db
        self.fail = fail
        self.before_return = before_return
        self.in_transaction: list[bool] = []

    async def pair_prepare_branch(
        self,
        task_id,
        title,
        *,
        branch_slug="",
        repo=None,
        base_branch=None,
        notify=None,
    ):
        self.in_transaction.append(self.db.in_transaction)
        if notify is not None:
            await notify("auto-push inside git")  # коммитит: вне транзакции захвата
        if self.fail is not None:
            raise self.fail
        if self.before_return is not None:
            await self.before_return(task_id)
        return f"task-{task_id}/spy"

    def __getattr__(self, name):
        from hub.integrations.registry import plugins

        return getattr(type(plugins.git_ops), name).__get__(self)


async def _hub_pair_start(db, task_id: int, session_id: str):
    from hub import services
    from hub.models import PairGitMode, TaskPairStart

    return await services.pair_start_task(
        db,
        task_id,
        TaskPairStart(
            plan="p",
            assigned_agent="exec-b",
            session_id=session_id,
            git_mode=PairGitMode.hub,
        ),
    )


@pytest.mark.parametrize("mode", ["off", "warn", "require"])
@pytest.mark.parametrize("registered", [True, False])
async def test_pair_start_runs_git_outside_the_write_transaction(
    db, monkeypatch, mode: str, registered: bool
):
    """Блокер ревью P2-A/B: git, сеть и колбэки с commit идут ДО write-лока, при
    любом режиме и с сессией или без — лок короткий и без внешних вызовов."""
    from hub.integrations.registry import plugins

    own = "pc-git-own" if mode == "require" else "pc-git-foreign"
    project = await _project(db, own, {"claim_project_check": mode})
    if registered:
        await _session(db, "s-git", ["pc-git-own"])
        project = await _project(db, own, {"claim_project_check": mode})
    task = await _task(db, int(project["id"]), title="git-outside")
    spy = _SpyGit(db)
    monkeypatch.setattr(plugins, "git_ops", spy)

    started = await _hub_pair_start(db, task, "s-git" if registered else "")
    assert started.status == "running"
    assert spy.in_transaction == [False], "git вызван под открытой транзакцией"
    assert not db.in_transaction


async def test_git_failure_leaves_no_alert_and_no_capture(db, monkeypatch):
    """Находка P2-B: отказ git не оставляет ни alert, ни плана, ни захвата."""
    from hub.integrations.git_ops import PairBranchConflictError
    from hub.integrations.registry import plugins

    project = await _project(db, "pc-gitfail", {"claim_project_check": "warn"})
    await _session(db, "s-gf", ["pc-home"])
    task = await _task(db, int(project["id"]), title="git-fails")
    monkeypatch.setattr(
        plugins, "git_ops", _SpyGit(db, fail=PairBranchConflictError("dirty"))
    )
    before = dict(await repo.get_task(db, task))
    with pytest.raises(HTTPException) as caught:
        await _hub_pair_start(db, task, "s-gf")
    assert caught.value.status_code == 422
    assert dict(await repo.get_task(db, task)) == before
    assert await _project_records(db, task) == []
    assert not [
        u for u in await repo.get_task_updates(db, task) if "Plan:" in u["content"]
    ]
    assert not db.in_transaction


async def test_locked_refusal_after_git_rolls_back_the_alert(db, db_dsn, monkeypatch):
    """Находка P2-B: git прошёл, а короткая проверка под локом отказала (задача
    ушла из open) — alert откатан вместе с остальным, транзакция закрыта."""
    from hub import db as db_module
    from hub.integrations.registry import plugins

    project = await _project(db, "pc-late", {"claim_project_check": "warn"})
    await _session(db, "s-late2", ["pc-home"])
    task = await _task(db, int(project["id"]), title="left-open")

    async def move_task(task_id: int) -> None:
        other = await db_module.connect(db_dsn)
        try:
            await other.execute(
                "UPDATE tasks SET status='failed' WHERE id=?", (task_id,)
            )
            await other.commit()
        finally:
            await other.close()

    spy = _SpyGit(db, before_return=move_task)
    monkeypatch.setattr(plugins, "git_ops", spy)
    with pytest.raises(HTTPException) as caught:
        await _hub_pair_start(db, task, "s-late2")
    assert caught.value.status_code == 409
    assert spy.in_transaction == [False]
    assert not db.in_transaction
    assert await _project_records(db, task) == []
    assert not [
        u for u in await repo.get_task_updates(db, task) if "Plan:" in u["content"]
    ]
    assert dict(await repo.get_task(db, task))["claim_session_id"] is None
