"""Критический путь и очередь с причинами по проекту (#1527).

Один расчёт (``hub.services.project_path``) обслуживает REST, CLI и
hub_my_context. Тесты строят граф ``depends_on`` и проверяют по AC: вес по
размеру и узкое место, шаги человека и чужие проекты на пути, очередь по
группам с причинами и циклом, и одинаковый ответ на всех поверхностях.
"""

from __future__ import annotations

import json
import sys
import time
from unittest.mock import patch

import aiosqlite
from httpx import AsyncClient

from hub import cli, mcp_server
from hub import repository as repo
from hub.services import project_path


async def _project(db: aiosqlite.Connection, slug: str) -> int:
    existing = await repo.get_project_by_slug(db, slug)
    if existing is not None:
        return int(existing["id"])
    pid = await repo.create_project(db, slug=slug, name=slug.title())
    await db.commit()
    return pid


async def _task(
    db: aiosqlite.Connection,
    pid: int,
    title: str,
    *,
    parent: int | None = None,
    status: str = "open",
    size: str | None = None,
    task_type: str = "task",
    areas: list[str] | None = None,
    dor: bool = True,
    waiting_for: str = "",
    waiting_until: str = "",
    review_job: bool = False,
    priority: str = "medium",
) -> int:
    from hub import services
    from hub.models import TaskCreate

    tv = await services.create_task(
        db, TaskCreate(title=title, task_type=task_type, parent_id=parent)
    )
    fields: dict = {
        "affected_areas": json.dumps(areas if areas is not None else [f"hub/{title}"]),
        "dor_passed": 1 if dor else 0,
        "size": size,
        "waiting_for": waiting_for,
        "waiting_until": waiting_until,
        "priority": priority,
    }
    if parent is None:
        fields["project_id"] = pid
    await repo.update_task(db, tv.id, **fields)
    if status != "open":
        await repo.update_task(db, tv.id, status=status)
    if review_job:
        await db.execute("UPDATE tasks SET review_job_id=1 WHERE id=?", (tv.id,))
    await db.commit()
    return tv.id


FUTURE = "2999-01-01 00:00:00"


async def _epic(db: aiosqlite.Connection, pid: int, title: str = "epic") -> int:
    return await _task(db, pid, title, task_type="epic", areas=[])


async def _dep(db: aiosqlite.Connection, task: int, on: int) -> None:
    await repo.add_task_dependency(db, task, on)
    await db.commit()


async def _delivered(db: aiosqlite.Connection, pid: int, task: int, pr: int) -> None:
    await repo.record_pipeline_merge(
        db, pr_number=pr, merge_sha=f"sha{pr}", project_id=pid, task_id=task
    )


async def _compute(db: aiosqlite.Connection, slug: str) -> dict:
    project = await repo.get_project_by_slug(db, slug)
    return await project_path.compute(db, project)


def _chain_ids(data: dict, epic_id: int) -> list[int]:
    epic = next(e for e in data["epics"] if e["epic_id"] == epic_id)
    return [s["task_id"] for s in epic["chain"]]


def _rows(data: dict) -> dict[int, dict]:
    return {
        r["task_id"]: {**r, "group": g["key"]}
        for g in data["queue"]["groups"]
        for r in g["rows"]
    }


async def test_critical_path_and_bottleneck(db):
    """AC-1: самая длинная по ВЕСУ цепочка (две задачи весом 8 длиннее четырёх
    весом 6); узкое место — задача с наибольшим числом транзитивно зависящих;
    вес подписан шагами."""
    pid = await _project(db, "pp1")
    epic = await _epic(db, pid)
    a = await _task(db, pid, "a", parent=epic, size="M")
    heavy = await _task(db, pid, "heavy", parent=epic, size="L")
    x1 = await _task(db, pid, "x1", parent=epic, size="XS")
    x2 = await _task(db, pid, "x2", parent=epic, size="XS")
    x3 = await _task(db, pid, "x3", parent=epic, size="XS")
    await _dep(db, heavy, a)
    await _dep(db, x1, a)
    await _dep(db, x2, x1)
    await _dep(db, x3, x2)

    data = await _compute(db, "pp1")

    assert _chain_ids(data, epic) == [a, heavy]
    entry = data["epics"][0]
    assert entry["weight"] == 8
    assert entry["unit"] == "шагов, не дней"
    assert entry["weight_text"] == "вес 8 = 3+5 шагов, не дней"
    assert entry["bottleneck"]["task_id"] == a
    assert entry["bottleneck"]["waiting"] == 4
    assert [s["weight"] for s in entry["chain"]] == [3, 5]

    # Размера нет — вес 1; размер не выдуман.
    lone = await _epic(db, pid, "lone")
    solo = await _task(db, pid, "solo", parent=lone)
    data = await _compute(db, "pp1")
    assert _chain_ids(data, lone) == [solo]
    lone_entry = next(e for e in data["epics"] if e["epic_id"] == lone)
    assert lone_entry["weight"] == 1
    assert lone_entry["bottleneck"] is None


async def test_human_steps_and_foreign_dependencies_stay_on_the_path(db):
    """AC-2: ждущая вердикта задача — шаг человека с именем действия; задача
    чужого проекта показана с проектом, и путь через неё не обрывается."""
    pid = await _project(db, "pp2")
    other = await _project(db, "elsewhere")
    foreign = await _task(db, other, "foreign", status="running", size="S")
    epic = await _epic(db, pid)
    work = await _task(db, pid, "work", parent=epic, status="running", size="M")
    verdict = await _task(db, pid, "verdict", parent=epic, status="review", size="S")
    await _dep(db, work, foreign)
    await _dep(db, verdict, work)

    data = await _compute(db, "pp2")

    assert _chain_ids(data, epic) == [foreign, work, verdict]
    steps = {s["task_id"]: s for s in data["epics"][0]["chain"]}
    assert steps[verdict]["human_step"] == "вердикт ревью"
    assert steps[verdict]["state"] == "ваш шаг"
    assert steps[work]["human_step"] == ""
    assert steps[foreign]["project"] == "elsewhere"
    assert steps[foreign]["foreign"] is True
    assert steps[work]["foreign"] is False
    # Машинное ревью — не шаг человека.
    machine = await _task(
        db, pid, "machine", parent=epic, status="review", review_job=True
    )
    data = await _compute(db, "pp2")
    chain_all = {r["task_id"]: r for r in _rows(data).values()}
    assert chain_all[machine]["group"] == "in_progress"
    # Доставленный предшественник в цепочку не входит.
    done = await _task(db, other, "done", status="completed")
    await _delivered(db, other, done, 7)
    await _dep(db, foreign, done)
    data = await _compute(db, "pp2")
    assert done not in _chain_ids(data, epic)
    assert data["epics"][0]["delivered_before"] == [done]


async def test_queue_groups_reasons_and_cycles(db):
    """AC-3: топологический порядок по группам, одна причина у каждой строки,
    первая готовая — следующая; цикл назван, расчёт не падает."""
    pid = await _project(db, "pp3")
    epic = await _epic(db, pid)
    draft = await _task(db, pid, "draft", parent=epic, status="draft")
    working = await _task(db, pid, "working", parent=epic, status="running")
    ready1 = await _task(db, pid, "ready1", parent=epic)
    ready2 = await _task(db, pid, "ready2", parent=epic)
    blocked = await _task(db, pid, "blocked", parent=epic, priority="critical")
    # Отложенная выше по priority: «следующей» ей не быть (#1527, находка ревью).
    deferred = await _task(
        db,
        pid,
        "deferred",
        parent=epic,
        waiting_for="ответ вендора",
        waiting_until=FUTURE,
        priority="critical",
    )
    parent = await _task(db, pid, "parent", parent=epic)
    child = await _task(db, pid, "child", parent=parent, task_type="subtask")
    await _dep(db, blocked, working)
    await _dep(db, ready2, ready1)  # ready2 ждёт ready1, пока не доставлена
    c1 = await _task(db, pid, "c1", parent=epic)
    c2 = await _task(db, pid, "c2", parent=epic)
    # Цикл нельзя завести через API (#483): пишем ребро напрямую, как оно
    # могло прийти из старых данных.
    await _dep(db, c2, c1)
    await db.execute(
        "INSERT INTO task_dependencies (task_id, depends_on_task_id) VALUES (?, ?)",
        (c1, c2),
    )
    await db.commit()

    data = await _compute(db, "pp3")

    rows = _rows(data)
    assert rows[draft]["group"] == "waiting_you"
    assert rows[working]["group"] == "in_progress"
    assert rows[ready1]["group"] == "ready"
    assert rows[blocked]["group"] == "blocked"
    assert rows[ready2]["group"] == "blocked"
    assert rows[deferred]["group"] == "deferred"
    # Родитель с открытой подзадачей не стартуем: ждёт её, в ready не считается.
    assert rows[parent]["group"] == "blocked"
    assert f"#{child}" in rows[parent]["reason"]
    ready_group = next(g for g in data["queue"]["groups"] if g["key"] == "ready")
    assert parent not in [r["task_id"] for r in ready_group["rows"]]
    assert ready_group["count"] == len(ready_group["rows"])
    assert "ответ вендора" in rows[deferred]["reason"]
    assert f"#{working}" in rows[blocked]["reason"]
    assert all(
        isinstance(r["reason"], str) and r["reason"].strip() for r in rows.values()
    )
    groups = [g["key"] for g in data["queue"]["groups"]]
    assert groups == [
        "waiting_you",
        "in_progress",
        "ready",
        "blocked",
        "deferred",
    ]
    assert all(g["count"] == len(g["rows"]) for g in data["queue"]["groups"])
    # Порядок по depends_on: зависимость раньше зависящей.
    order = [r["task_id"] for g in data["queue"]["groups"] for r in g["rows"]]
    assert order.index(working) < order.index(blocked)
    assert order.index(ready1) < order.index(ready2)
    # Следующая — первая готовая, помечена и в данных, и в строке.
    # Выше по priority, но ждёт недоставленную зависимость: следующей быть не
    # может, и это правило очереди оркестратора, а не вторая копия.
    assert data["next"]["task_id"] == ready1
    assert rows[ready1]["is_next"] is True
    assert not any(r["is_next"] for r in rows.values() if r["task_id"] != ready1)
    # Цикл назван и рассечён с причиной; расчёт дошёл до конца.
    assert len(data["cycles"]) == 1
    cycle = data["cycles"][0]
    assert {c1, c2} <= set(cycle["tasks"])
    assert cycle["tasks"][0] == cycle["tasks"][-1]
    assert cycle["reason"]
    assert data["epics"][0]["cycles"] == data["cycles"]
    assert c1 in rows and c2 in rows


async def test_empty_project_and_graph_of_200_tasks(db):
    """Пусто — не ошибка; расчёт на графе из 200 задач укладывается в секунды."""
    await _project(db, "pp-empty")
    data = await _compute(db, "pp-empty")
    assert data["epics"] == []
    assert data["next"]["task_id"] is None
    assert data["next"]["reason"]

    pid = await _project(db, "pp-big")
    epic = await _epic(db, pid)
    previous = None
    for i in range(200):
        task = await _task(db, pid, f"n{i}", parent=epic, size="XS", areas=[f"a/{i}"])
        if previous is not None:
            await _dep(db, task, previous)
        previous = task
    started = time.monotonic()
    data = await _compute(db, "pp-big")
    assert time.monotonic() - started < 30
    assert len(data["epics"][0]["chain"]) == 200
    assert data["epics"][0]["weight"] == 200


async def test_path_is_the_same_on_every_surface(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, capsys
):
    """AC-4: REST, CLI и hub_my_context называют одну следующую задачу и путь."""
    pid = await _project(db, "pp4")
    epic = await _epic(db, pid)
    first = await _task(db, pid, "first", parent=epic, size="M")
    second = await _task(db, pid, "second", parent=epic, size="S")
    await _dep(db, second, first)
    # Отложенная open+DoR выше по priority не должна гасить «следующую».
    await _task(
        db,
        pid,
        "later",
        parent=epic,
        waiting_for="релиз",
        waiting_until=FUTURE,
        priority="critical",
    )

    resp = await client.get("/api/projects/pp4/path")
    assert resp.status_code == 200
    rest = resp.json()
    assert rest["next"]["task_id"] == first
    rows = _rows(rest)
    assert rows[first]["is_next"] is True
    assert sum(1 for r in rows.values() if r["is_next"]) == 1
    assert [s["task_id"] for s in rest["epics"][0]["chain"]] == [first, second]
    assert (await client.get("/api/projects/no-such/path")).status_code == 404

    async def _via_client(path: str, **_: object) -> object:
        if path.startswith("/api/tasks/9/context"):
            return {
                "context_text": "Task #9",
                "task": {"project": {"id": pid, "slug": "pp4"}},
            }
        return (await client.get(path)).json()

    monkeypatch.setattr(mcp_server, "_api_get", _via_client)
    ctx = json.loads((await mcp_server.hub_my_context(task_id=9)).content[0].text)
    text = ctx["message"]
    assert f"Next task: #{first}" in text
    assert f"#{first} → #{second}" in text

    argv = ["oc-hub", "path", "pp4"]
    with (
        patch.object(sys, "argv", argv),
        patch.object(cli, "_api", return_value=rest) as api,
    ):
        cli.main()
    assert api.call_args.args[:2] == ("GET", "/api/projects/pp4/path")
    printed = capsys.readouterr().out
    assert f"#{first} → #{second}" in printed
    assert f"#{first}" in printed.splitlines()[0]
    with (
        patch.object(sys, "argv", argv + ["--json"]),
        patch.object(cli, "_api", return_value=rest),
    ):
        cli.main()
    assert json.loads(capsys.readouterr().out) == rest
    # Строка «что дальше» одна на все поверхности.
    assert project_path.format_path_brief(rest)[0] in text


async def test_path_block_is_shown_once_for_a_regular_agent(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch, capsys
):
    """AC-3 (#1643): обычный агент видит блок «что дальше» ровно один раз, с тем
    же смыслом, что у /path; /path и CLI path не изменились, а запроса к /path
    ради блока нет."""
    pid = await _project(db, "pp43")
    epic = await _epic(db, pid)
    first = await _task(db, pid, "first", parent=epic, size="M")
    second = await _task(db, pid, "second", parent=epic, size="S")
    await _dep(db, second, first)
    # get_readiness пересчитывает DoR прочитанной задачи, поэтому читаем не
    # first, а свою задачу ниже по priority: «следующая» остаётся first.
    mine = await _task(db, pid, "mine", parent=epic, size="XS", priority="low")
    rest = (await client.get("/api/projects/pp43/path")).json()
    calls: list[str] = []

    async def _via_client(path: str, **_: object) -> object:
        calls.append(path)
        return (await client.get(path)).json()

    monkeypatch.setattr(mcp_server, "_api_get", _via_client)
    out = await mcp_server.hub_my_context(task_id=mine, mode="full")
    text = json.loads(out.content[0].text)["message"]
    assert text.count("Next task:") == 1
    assert text.count(f"#{first} → #{second}") == 1
    assert project_path.format_path_brief(rest)[0] in text
    assert not [c for c in calls if "/path" in c], calls
    # Второе представление блока не тащит: он один и в тексте.
    assert "path_brief" not in json.dumps(out.structuredContent, ensure_ascii=False)

    # CLI context: тот же блок один раз из того же поля /context.
    ctx_json = (await client.get(f"/api/tasks/{mine}/context")).json()
    assert ctx_json["path_brief"]["status"] == "ok"
    with (
        patch.object(sys, "argv", ["oc-hub", "context", str(mine)]),
        patch.object(cli, "_api", return_value=ctx_json),
    ):
        cli.main()
    printed = capsys.readouterr().out
    assert printed.count("Next task:") == 1
    assert f"#{first} → #{second}" in printed

    # oc-hub path и REST /path — без изменений.
    with (
        patch.object(sys, "argv", ["oc-hub", "path", "pp43", "--json"]),
        patch.object(cli, "_api", return_value=rest),
    ):
        cli.main()
    assert json.loads(capsys.readouterr().out) == rest


async def test_task_context_sql_does_not_grow_with_foreign_tasks(
    db: aiosqlite.Connection,
):
    """#1643 P2: число запросов блока не растёт с числом задач чужого проекта."""

    async def _statements(tag: str, foreign: int) -> int:
        mine_pid = await _project(db, f"mine-{tag}")
        other_pid = await _project(db, f"other-{tag}")
        epic = await _epic(db, mine_pid)
        await _task(db, mine_pid, "mine", parent=epic, size="XS")
        foreign_epic = await _epic(db, other_pid, "fe")
        for i in range(foreign):
            await _task(db, other_pid, f"f{i}", parent=foreign_epic, size="S")
        seen: list[str] = []
        await db.set_trace_callback(seen.append)
        project = await repo.get_project_by_slug(db, f"mine-{tag}")
        brief = await project_path.task_path_brief(db, project, [], summary=False)
        await db.set_trace_callback(None)
        assert brief["status"] == "ok", brief
        return len(seen)

    few = await _statements("a", 3)
    many = await _statements("b", 40)
    assert many == few, (few, many)


async def test_task_path_brief_statuses_are_distinct(db: aiosqlite.Connection):
    """#1643 P3: нет проекта и сбой расчёта — разные статусы; сбой — «неизвестно»."""
    no_project = await project_path.task_path_brief(db, None, [], summary=False)
    assert no_project["status"] == "no_project"
    assert "нет проекта" in no_project["lines"][0]

    pid = await _project(db, "pp-fail")
    project = await repo.get_project_by_slug(db, "pp-fail")
    with patch.object(project_path, "compute", side_effect=RuntimeError("boom")):
        failed = await project_path.task_path_brief(db, project, [], summary=False)
    assert pid and failed["status"] == "unavailable"
    assert "нет проекта" not in " ".join(failed["lines"])
    assert "не посчитан" in failed["lines"][0]


async def test_mcp_goes_to_path_only_for_replies_without_a_block(monkeypatch):
    """#1643 P3: /path — только когда блока нет; присутствующий блок с пустыми
    строками обрабатывается локально, без запроса к /path."""
    calls: list[str] = []
    path_reply = {
        "project": "pp9",
        "next": {"task_id": 7, "title": "t", "reason": "r"},
        "epics": [],
    }

    def _api(brief):
        async def _get(path: str, **_: object) -> object:
            calls.append(path)
            if path.startswith("/api/tasks/9/context"):
                ctx = {"context_text": "Task #9", "task": {"project": {"slug": "pp9"}}}
                return {**ctx, **brief}
            return path_reply

        return _get

    async def _text(brief: dict) -> str:
        calls.clear()
        monkeypatch.setattr(mcp_server, "_api_get", _api(brief))
        out = await mcp_server.hub_my_context(task_id=9)
        return json.loads(out.content[0].text)["message"]

    legacy = await _text({})
    assert any(c.endswith("/path") for c in calls) and "Next task: #7" in legacy

    unknown = await _text({"path_brief": {"status": "unavailable", "lines": []}})
    assert not any(c.endswith("/path") for c in calls)
    assert "неизвестно" in unknown and "нет проекта" not in unknown

    absent = await _text({"path_brief": {"status": "no_project", "lines": []}})
    assert not any(c.endswith("/path") for c in calls)
    assert "нет проекта" in absent


def test_cli_context_limit_covers_the_final_digest(capsys):
    """#1643 P3: --max-chars режет итоговый текст вместе с блоком; в summary
    блок стоит в начале и переживает усечение."""
    lines = [f"Critical path, epic #{i}: " + "#1 → " * 30 for i in range(5)]
    reply = {
        "context_text": "контекст " * 200,
        "path_brief": {"status": "ok", "project": "p", "lines": lines},
    }
    for mode, limit in (("full", 100), ("summary", 100)):
        argv = ["oc-hub", "context", "9", "--mode", mode, "--max-chars", str(limit)]
        with (
            patch.object(sys, "argv", argv),
            patch.object(cli, "_api", return_value=reply),
        ):
            cli.main()
        printed = capsys.readouterr().out.rstrip("\n")
        assert len(printed) <= limit, (mode, len(printed))
    assert "Critical path" in printed  # summary: блок в начале
