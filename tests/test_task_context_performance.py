"""Regression coverage for the task context path calculation (#2424)."""

import re

import aiosqlite
import pytest
from httpx import ASGITransport, AsyncClient

from hub import repository as repo
from hub.db import fetchall
from hub.services import orchestrator_queue as queue, project_path
from tests.test_chat_pair import _clean_limiters as _clean_limiters
from tests.test_chat_pair import _implementer_session
from tests.test_chat_pair import audit_probe as audit_probe
from tests.test_chat_pair import hub as hub
from tests.test_project_path import _dep, _epic, _project, _rows, _task


def _capture_queue(monkeypatch):
    original = queue.next_task
    observed = []

    async def read(*args, **kwargs):
        answer = await original(*args, **kwargs)
        observed.append(answer)
        return answer

    monkeypatch.setattr(queue, "next_task", read)
    return observed


async def _trace_path(db, project):
    statements = []
    await db.set_trace_callback(statements.append)
    try:
        result = await project_path.compute(db, project)
    finally:
        await db.set_trace_callback(None)
    reads = [
        sql
        for raw in statements
        if (
            "from tasks" in (sql := " ".join(raw.lower().split()))
            and "parent_id" in sql.partition(" where ")[2]
            and "archived" in sql.partition(" where ")[2]
            and "status not in" in sql.partition(" where ")[2]
        )
    ]
    return result, reads


@pytest.mark.parametrize("candidates", [3, 30])
async def test_path_reuses_one_open_children_projection(db, monkeypatch, candidates):
    pid = await _project(db, "batch")
    other = await _project(db, "other")
    epic = await _epic(db, pid)
    parent = await _task(db, pid, "parent", parent=epic, priority="critical")
    first_child = await _task(
        db, pid, "child", parent=parent, task_type="subtask", dor=False
    )
    cross_project_child = await _task(
        db, pid, "foreign child", parent=parent, task_type="subtask", dor=False
    )
    await repo.update_task(db, cross_project_child, project_id=other)
    await _task(
        db,
        pid,
        "completed child",
        parent=parent,
        task_type="subtask",
        status="completed",
        dor=False,
    )
    archived = await _task(
        db, pid, "archived child", parent=parent, task_type="subtask", dor=False
    )
    await repo.update_task(db, archived, archived=1)
    leaf = await _task(db, pid, "first leaf", parent=epic)
    for index in range(candidates - 2):
        await _task(db, pid, f"leaf {index}", parent=epic)

    project = await repo.get_project_by_slug(db, "batch")
    baseline = await queue.next_task(db, project)
    expected_reason = (
        f"не лист: незавершённые подзадачи #{first_child}, #{cross_project_child}"
    )
    assert (
        await queue.not_leaf_reason(db, dict(await repo.get_task(db, parent)))
        == expected_reason
    )
    assert baseline["next_task_id"] == leaf

    observed = _capture_queue(monkeypatch)
    path, child_reads = await _trace_path(db, project)
    assert observed[0]["skipped"] == baseline["skipped"]
    assert (
        observed[0]["next_task_id"]
        == baseline["next_task_id"]
        == path["next"]["task_id"]
    )
    assert _rows(path)[parent]["group"] == project_path.GROUP_BLOCKED
    assert (
        _rows(path)[parent]["reason"]
        == f"ждёт подзадачи #{first_child}, #{cross_project_child}"
    )
    assert not child_reads, child_reads


async def test_path_checks_candidate_outside_its_graph_snapshot(db, monkeypatch):
    pid = await _project(db, "late")
    epic = await _epic(db, pid)
    stable = await _task(db, pid, "stable", parent=epic)
    read_graph = project_path._load_graph
    late = None
    child = None

    async def read_then_add(*args, **kwargs):
        nonlocal late, child
        graph = await read_graph(*args, **kwargs)
        late = await _task(db, pid, "late parent", parent=epic, priority="critical")
        child = await _task(
            db, pid, "late child", parent=late, task_type="subtask", dor=False
        )
        assert late not in graph.tasks
        return graph

    monkeypatch.setattr(project_path, "_load_graph", read_then_add)
    observed = _capture_queue(monkeypatch)
    project = await repo.get_project_by_slug(db, "late")
    data, child_reads = await _trace_path(db, project)
    assert observed[0]["skipped"][0] == {
        "task_id": late,
        "reason": queue.SKIP_NOT_LEAF,
        "detail": f"не лист: незавершённые подзадачи #{child}",
    }
    assert observed[0]["next_task_id"] == data["next"]["task_id"] == stable
    assert len(child_reads) == 1
    assert re.search(rf"parent_id\s*=\s*{late}\b", child_reads[0])


async def test_graph_keeps_merge_counts_with_grouped_load(db):
    pid = await _project(db, "counts")
    other = await _project(db, "away")
    no_merges = await _task(db, pid, "none")
    one_merge = await _task(db, pid, "one")
    several = await _task(db, pid, "multiple")
    foreign = await _task(db, other, "foreign")
    for pr, task_id in enumerate((one_merge, several, several, None, -1), start=1):
        await repo.record_pipeline_merge(
            db, pr_number=pr, merge_sha=f"merge-{pr}", project_id=pid, task_id=task_id
        )

    project = await repo.get_project_by_slug(db, "counts")
    statements = []
    await db.set_trace_callback(statements.append)
    try:
        graph = await project_path._load_graph(db, project)
    finally:
        await db.set_trace_callback(None)
    expected = {
        row["id"]: row["merges"]
        for row in await fetchall(
            db,
            "SELECT id, (SELECT COUNT(*) FROM pipeline_merges m "
            "WHERE m.task_id=t.id) AS merges FROM tasks t",
        )
    }
    assert {tid: row["merges"] for tid, row in graph.tasks.items()} == expected
    assert [expected[tid] for tid in (no_merges, one_merge, several)] == [0, 1, 2]
    assert graph.own == {no_merges, one_merge, several}
    assert graph.slug_of[foreign] == "away"

    (sql,) = [s for s in statements if "pipeline_merges" in s and "FROM tasks t" in s]
    details = [row["detail"] for row in await fetchall(db, "EXPLAIN QUERY PLAN " + sql)]
    assert not any("CORRELATED" in detail.upper() for detail in details), details
    assert (
        sum(
            bool(
                re.search(
                    r"\b(?:SCAN|SEARCH)\s+(?:TABLE\s+)?pipeline_merges\b", d, re.I
                )
            )
            for d in details
        )
        == 1
    ), details


async def test_optimized_task_context_preserves_path_and_visibility(hub):
    pid = await _project(hub.db, "target")
    outside_pid = await _project(hub.db, "outside")
    epic = await _epic(hub.db, pid, "visible epic")
    outside_epic = await _epic(hub.db, outside_pid, "external epic")
    first = await _task(hub.db, pid, "first", parent=epic, size="M")
    second = await _task(hub.db, pid, "second", parent=epic, size="M")
    mine = await _task(hub.db, pid, "mine", parent=epic, size="XS", priority="low")
    foreign = await _task(
        hub.db, outside_pid, "external dependency", parent=outside_epic, size="L"
    )
    ready_epic = await _epic(hub.db, pid, "other open epic")
    ready = await _task(
        hub.db, pid, "other ready", parent=ready_epic, priority="critical"
    )
    cycle_a = await _task(hub.db, pid, "cycle a", parent=ready_epic, dor=False)
    cycle_b = await _task(hub.db, pid, "cycle b", parent=ready_epic, dor=False)
    await _dep(hub.db, second, first)
    await _dep(hub.db, first, foreign)
    await _dep(hub.db, cycle_a, cycle_b)
    await hub.db.execute(
        "INSERT INTO task_dependencies (task_id, depends_on_task_id) VALUES (?, ?)",
        (cycle_b, cycle_a),
    )
    await hub.db.commit()

    path_url = "/api/projects/target/path"
    result = await hub.client.get(path_url, headers=hub.agent_auth)
    assert result.status_code == 200
    path = result.json()
    assert path["next"]["task_id"] == ready
    assert {cycle_a, cycle_b} == set(path["cycles"][0]["tasks"])
    own = next(entry for entry in path["epics"] if entry["epic_id"] == epic)
    assert [step["task_id"] for step in own["chain"]] == [foreign, first, second]
    assert own["chain"][0]["foreign"]
    assert f"#{foreign}" in _rows(path)[first]["reason"]

    session = await _implementer_session(hub, mine)
    assert (await hub.client.get(path_url, headers=session)).status_code == 403
    for mode in ("summary", "full"):
        url = f"/api/tasks/{mine}/context?mode={mode}"
        public = await hub.client.get(url, headers=hub.agent_auth)
        restricted = await hub.client.get(url, headers=session)
        assert public.status_code == restricted.status_code == 200
        ordinary = public.json()["path_brief"]
        scoped = restricted.json()["path_brief"]
        assert ordinary["status"] == scoped["status"] == "ok"
        assert f"Next task: #{ready} " in ordinary["lines"][0]
        assert f"#{foreign} (проект outside) → #{first} → #{second}" in "\n".join(
            ordinary["lines"]
        )
        hidden_text = "\n".join(scoped["lines"])
        assert f"Next task: {project_path.HIDDEN_NEXT}" in hidden_text
        assert f"{project_path.HIDDEN_STEP} → #{first} → #{second}" in hidden_text
        assert "outside" not in hidden_text
        for hidden in (foreign, outside_epic, ready, ready_epic, cycle_a, cycle_b):
            assert not re.search(rf"#{hidden}\b", hidden_text)


async def test_context_reports_path_unavailable_when_graph_read_fails(
    client, db, monkeypatch
):
    from hub.app import app

    pid = await _project(db, "path-unavailable")
    task_id = await _task(db, pid, "preserved task")

    async def graph_failure(*_args):
        raise aiosqlite.OperationalError("cannot read graph")

    monkeypatch.setattr(project_path, "_load_graph", graph_failure)
    response = await client.get(f"/api/tasks/{task_id}/context")
    assert response.status_code == 200
    body = response.json()
    assert body["task"]["id"] == task_id
    assert "preserved task" in body["context_text"]
    assert body["path_brief"]["status"] == "unavailable"
    assert "OperationalError" in body["path_brief"]["lines"][0]
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as strict:
        assert (
            await strict.get("/api/projects/path-unavailable/path")
        ).status_code == 500
