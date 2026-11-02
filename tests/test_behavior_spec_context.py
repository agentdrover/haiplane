"""Контекст задачи отдаёт требования индекса по записям дельты (#2215, #2218).

Id берутся из behavior_delta. Контекст показывает обязательство из индекса
только для такого id и только если индекс его знает. Один и тот же набор
у REST, CLI и MCP. Пути affected_areas требования не выбирают.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import AsyncClient

from hub import repository as repo
from hub.integrations.git_ops import GitOpsIntegration
from hub.integrations.registry import plugins
from hub.mcp_server import hub_my_context
from hub.services import spec_index
from tests.cli_support import _run_main
from tests.test_behavior_spec_index import AUTH_SPEC, BRANCH, Origin
from tests.test_mcp_server import _mcp_structured, _mcp_text


@pytest.fixture
def origin(tmp_path: Path) -> Origin:
    return Origin(tmp_path / "git")


@pytest.fixture
async def project_id(db, origin: Origin, monkeypatch) -> int:
    monkeypatch.setattr(plugins, "git_ops", GitOpsIntegration())
    pid = await repo.create_project(
        db,
        slug="specs-proj",
        name="Specs",
        workspace_path=str(origin.clone),
        default_branch=BRANCH,
    )
    await db.commit()
    return pid


def _ids(payload: dict) -> list[str]:
    return [item["id"] for item in payload["requirements"]]


def _delta(*ids: str) -> list[dict[str, object]]:
    return [
        {
            "requirement_id": req_id,
            "op": "added",
            "base_spec_revision": "",
            "obligation": "linked from the delta",
            "scenarios": [
                {
                    "given": "a user",
                    "when": "the context is read",
                    "then": "the indexed obligation is shown",
                    "verifiable_by": "test",
                    "test_ref": (
                        "tests/test_behavior_spec_context.py::"
                        "test_context_returns_linked_requirement_only"
                    ),
                }
            ],
        }
        for req_id in ids
    ]


async def _task(client: AsyncClient, db, project_id: int, **refine: object) -> int:
    created = await client.post("/api/tasks", json={"title": "context"})
    assert created.status_code == 200, created.text
    task_id = int(created.json()["id"])
    await repo.update_task(db, task_id, project_id=project_id)
    await db.commit()
    if refine:
        patched = await client.post(f"/api/tasks/{task_id}/refine", json=refine)
        assert patched.status_code == 200, patched.text
    return task_id


async def _context(client: AsyncClient, task_id: int) -> dict:
    resp = await client.get(f"/api/tasks/{task_id}/context")
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _same_set(rest: dict, task_id: int, capsys, monkeypatch) -> None:
    """CLI печатает тот же текст, MCP отдаёт тот же список id."""
    ids = _ids(rest)
    rc, _api = _run_main(["context", str(task_id)], api_result=rest)
    assert rc == 0
    printed = capsys.readouterr().out
    for req_id in ids:
        assert req_id in printed
    for req_id in ("REQ-1", "REQ-2", "REQ-9"):
        if req_id not in ids:
            assert req_id not in printed

    async def fake_get(path: str, timeout: float | None = None) -> dict:
        assert path.startswith(f"/api/tasks/{task_id}/context")
        return rest

    monkeypatch.setattr("hub.mcp_server._api_get", fake_get)
    result = await hub_my_context(task_id)
    text = _mcp_text(result)
    structured = _mcp_structured(result) or {}
    ctx = structured.get("context") or structured
    mcp_ids = [item["id"] for item in ctx.get("requirements") or []]
    assert mcp_ids == ids
    for req_id in ids:
        assert req_id in text
    for req_id in ("REQ-1", "REQ-2", "REQ-9"):
        if req_id not in ids:
            assert req_id not in text


async def test_context_returns_linked_requirement_only(
    client: AsyncClient, db, origin: Origin, project_id: int, capsys, monkeypatch
):
    """AC-1: в индексе REQ-1 и REQ-2 на одном пути, в списке задачи только REQ-1."""
    origin.publish({"specs/auth/spec.md": AUTH_SPEC})
    indexed = await spec_index.index_project(db, project_id)
    assert indexed.state == "indexed", indexed.detail
    task_id = await _task(client, db, project_id, behavior_delta=_delta("REQ-1"))

    rest = await _context(client, task_id)

    assert _ids(rest) == ["REQ-1"]
    assert rest["requirements"][0]["obligation"].startswith("The hub refuses")
    assert "REQ-1" in rest["context_text"]
    assert "REQ-2" not in rest["context_text"]
    await _same_set(rest, task_id, capsys, monkeypatch)


async def test_context_without_ids_adds_no_requirements(
    client: AsyncClient, db, origin: Origin, project_id: int, capsys, monkeypatch
):
    """AC-2: без списка id контекст молчит, даже если область покрывает файл."""
    origin.publish({"specs/auth/spec.md": AUTH_SPEC})
    assert (await spec_index.index_project(db, project_id)).state == "indexed"
    task_id = await _task(client, db, project_id, affected_areas=["specs/auth/spec.md"])

    rest = await _context(client, task_id)

    assert rest["requirements"] == []
    assert "REQ-1" not in rest["context_text"]
    assert "REQ-2" not in rest["context_text"]
    await _same_set(rest, task_id, capsys, monkeypatch)


async def test_unknown_id_is_not_a_current_obligation(
    client: AsyncClient, db, origin: Origin, project_id: int, capsys, monkeypatch
):
    """AC-3: REQ-9 в списке, но не в индексе, действующим обязательством не становится."""
    origin.publish({"specs/auth/spec.md": AUTH_SPEC})
    assert (await spec_index.index_project(db, project_id)).state == "indexed"
    task_id = await _task(
        client, db, project_id, behavior_delta=_delta("REQ-1", "REQ-9")
    )

    rest = await _context(client, task_id)

    assert _ids(rest) == ["REQ-1"]
    assert "REQ-9" not in rest["context_text"]
    await _same_set(rest, task_id, capsys, monkeypatch)


async def test_child_reads_requirements_from_the_epic_project(
    client: AsyncClient, db, origin: Origin, project_id: int, capsys, monkeypatch
):
    """Дочерняя задача не носит project_id: проект живёт на эпике.

    Подтверждённая находка 42476eccb3198d48. Индекс проекта эпика виден
    потомку. Колонка project_id у потомка пустая — это штатная форма, не дыра.
    """
    origin.publish({"specs/auth/spec.md": AUTH_SPEC})
    assert (await spec_index.index_project(db, project_id)).state == "indexed"
    epic = await client.post(
        "/api/tasks",
        json={"title": "epic", "task_type": "epic", "project": "specs-proj"},
    )
    assert epic.status_code == 200, epic.text
    epic_id = int(epic.json()["id"])
    child = await client.post(
        "/api/tasks", json={"title": "child", "parent_id": epic_id}
    )
    assert child.status_code == 200, child.text
    child_id = int(child.json()["id"])
    stored = dict(await repo.get_task(db, child_id))
    assert stored["project_id"] is None
    patched = await client.post(
        f"/api/tasks/{child_id}/refine", json={"behavior_delta": _delta("REQ-1")}
    )
    assert patched.status_code == 200, patched.text

    rest = await _context(client, child_id)

    assert _ids(rest) == ["REQ-1"]
    assert "REQ-2" not in rest["context_text"]
    await _same_set(rest, child_id, capsys, monkeypatch)
