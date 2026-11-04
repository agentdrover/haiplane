"""Переиндексация спецификации с базовой ветки (#2223).

Индекс перечитывает ``specs/<capability>/spec.md`` по ref ``origin/<ветка>``, а
не из рабочей копии. Повод - ход поллера, когда SHA базовой ветки сдвинулся и
среди изменений есть ``specs/``, и явная команда ``POST
/api/projects/{id}/spec/reindex`` (одна и та же в REST, CLI и MCP). Отказ -
ошибка разбора или дубль id - оставляет прежний индекс целиком. Блок
требования из слитого коммита попадает в индекс только по хешу одобренного
снимка сдачи на этом SHA либо если его хеш не менялся.

Тесты идут по настоящему git: bare origin, клон хаба и второй клон, из
которого «сливает» другой человек, пока клон хаба ничего не знает.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from httpx import AsyncClient

from hub import cli, mcp_server
from hub import poller
from hub import repository as repo
from hub.integrations.git_ops import GitOpsIntegration
from hub.integrations.registry import plugins
from hub.mcp_server import HubApiError, hub_reindex_spec
from hub.services import spec_index, spec_reindex
from tests.test_behavior_spec_index import (
    AUTH_SPEC,
    BILLING_SPEC,
    BRANCH,
    Origin,
    _git,
    _revision,
)
from tests.test_mcp_server import _mcp_structured

REQ1_TEXT = "The hub refuses a login with an empty password."
REQ1_SCENARIO = {
    "given": "a registered user",
    "when": "the user logs in with an empty password",
    "then": "the hub answers 401",
    "verifiable_by": "test",
    "test_ref": "tests/test_auth.py::test_empty_password_refused",
}


@pytest.fixture
def origin(tmp_path: Path) -> Origin:
    return Origin(tmp_path / "git")


@pytest.fixture
async def project_id(db, origin: Origin, monkeypatch) -> int:
    monkeypatch.setattr(plugins, "git_ops", GitOpsIntegration())
    monkeypatch.setattr(spec_reindex, "SWEEP_INTERVAL", 0.0)
    pid = await repo.create_project(
        db,
        slug="specs-proj",
        name="Specs",
        workspace_path=str(origin.clone),
        default_branch=BRANCH,
    )
    await db.commit()
    return pid


class Merger:
    """Другой человек: второй клон origin, из которого он вливает в базовую ветку.

    Клон хаба об этих коммитах не знает, пока не сделает fetch: ход поллера
    обязан сам заметить, что SHA базовой ветки сдвинулся.
    """

    def __init__(self, origin: Origin) -> None:
        self.origin = origin
        self.root = origin.clone.parent / "merger"
        _git(origin.clone.parent, "clone", "-q", str(origin.bare), str(self.root))
        _git(self.root, "checkout", "-q", BRANCH)

    def merge(self, files: dict[str, str | None], message: str = "merge") -> str:
        _git(self.root, "fetch", "-q", "origin")
        _git(self.root, "reset", "-q", "--hard", f"origin/{BRANCH}")
        for rel, text in files.items():
            path = self.root / rel
            if text is None:
                path.unlink()
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-q", "-m", message)
        _git(self.root, "push", "-q", "origin", f"{BRANCH}:{BRANCH}")
        return self.base_sha()

    def base_sha(self) -> str:
        return _git(self.origin.bare, "rev-parse", BRANCH)

    def blob(self, rel: str) -> str:
        return _git(self.origin.bare, "rev-parse", f"{BRANCH}:{rel}")


@pytest.fixture
def merger(origin: Origin) -> Merger:
    origin.publish({"README.md": "hub\n"}, message="seed")
    return Merger(origin)


def _auth_spec(req1: str = REQ1_TEXT, preamble: str = "") -> str:
    """AUTH_SPEC с другим обязательством REQ-1 и/или текстом над требованиями."""
    text = AUTH_SPEC.replace(REQ1_TEXT, req1)
    return text.replace("# Auth\n", f"# Auth\n{preamble}", 1)


def _req1_hash(obligation: str) -> str:
    return _revision("REQ-1", obligation, [REQ1_SCENARIO])


async def _snapshot(db, pid: int) -> dict[str, Any]:
    """Всё, что индекс хранит по проекту, - для сравнения «до» и «после»."""
    return {
        "state": await spec_index.index_state(db, pid),
        "files": await spec_index.indexed_files(db, pid),
        "requirements": [
            r.model_dump() for r in await spec_index.indexed_requirements(db, pid)
        ],
    }


async def _approve_merge(
    client: AsyncClient,
    db,
    pid: int,
    merge_sha: str,
    records: list[dict[str, Any]],
) -> int:
    """Задача с одобренной сдачей, чей снимок дельты слит коммитом ``merge_sha``."""
    created = await client.post("/api/tasks", json={"title": f"merged {merge_sha[:7]}"})
    assert created.status_code == 200, created.text
    task_id = int(created.json()["id"])
    snapshot = {
        "generation": 1,
        "head_sha": merge_sha,
        "records": records,
        "explicit_no_delta": False,
        "areas": [],
    }
    await repo.update_task(
        db,
        task_id,
        project_id=pid,
        submission_generation=1,
        review_verdict="approved",
        review_verdict_generation=1,
    )
    await repo.record_submission(
        db,
        task_id=task_id,
        generation=1,
        sha=merge_sha,
        base_branch=BRANCH,
        behavior_snapshot=json.dumps(snapshot, sort_keys=True),
    )
    await repo.record_pipeline_merge(
        db, pr_number=task_id, merge_sha=merge_sha, project_id=pid, task_id=task_id
    )
    await db.commit()
    return task_id


def _modified(obligation: str) -> dict[str, Any]:
    return {
        "requirement_id": "REQ-1",
        "op": "modified",
        "base_spec_revision": _req1_hash(REQ1_TEXT),
        "new_requirement_id": "",
        "obligation": obligation,
        "scenarios": [REQ1_SCENARIO],
        "content_hash": _req1_hash(obligation),
    }


def _reindex_sweep():
    return next(sweep for sweep in poller.SWEEPS if sweep.name == "spec_reindex")


async def _tick(db) -> None:
    """Один ход поллера: настоящий свип из ``SWEEPS``, не вызов функции в обход."""
    await _reindex_sweep().run(db)


async def _reindex(client: AsyncClient, pid: int):
    return await client.post(f"/api/projects/{pid}/spec/reindex")


async def test_poller_reindexes_after_default_branch_moves(
    db, origin: Origin, merger: Merger, project_id: int
):
    """AC-1: ход поллера перечитывает индекс по ref базовой ветки.

    Ревизия файла в индексе равна blob на базовой ветке, а не тому, что лежит в
    рабочей копии клона; ход без изменений в ``specs/`` индекс не трогает.
    """
    origin.publish({"specs/auth/spec.md": AUTH_SPEC})
    assert (await spec_index.index_project(db, project_id)).state == "indexed"
    before = await _snapshot(db, project_id)

    moved = merger.merge(
        {"specs/auth/spec.md": _auth_spec(preamble="\nNotes above the blocks.\n")},
        "docs: describe the auth capability",
    )
    # Рабочая копия клона - не источник: правка без коммита индекс не двигает.
    (origin.clone / "specs/auth/spec.md").write_text("garbage, not a spec\n")

    await _tick(db)

    after = await _snapshot(db, project_id)
    assert after["state"]["commit_sha"] == moved
    assert after["state"]["ref"] == f"origin/{BRANCH}"
    revision = merger.blob("specs/auth/spec.md")
    assert revision != before["files"][0]["revision"]
    assert [f["revision"] for f in after["files"]] == [revision]
    assert {r["file_revision"] for r in after["requirements"]} == {revision}
    # Блоки не менялись: их хеши прежние, одобрения для них не требуется.
    assert [r["revision"] for r in after["requirements"]] == [
        r["revision"] for r in before["requirements"]
    ]

    # База сдвинулась, но specs/ не затронут: повода перечитывать нет.
    merger.merge({"README.md": "hub, second line\n"}, "docs: readme")
    await _tick(db)
    assert await _snapshot(db, project_id) == after


async def test_reindex_command_on_rest_cli_mcp(
    client: AsyncClient,
    db,
    origin: Origin,
    merger: Merger,
    project_id: int,
    monkeypatch,
):
    """AC-2: REST, CLI и MCP вызывают одну и ту же команду переиндексации."""
    origin.publish({"specs/auth/spec.md": AUTH_SPEC})
    assert (await spec_index.index_project(db, project_id)).state == "indexed"
    path = f"/api/projects/{project_id}/spec/reindex"
    calls: list[tuple[str, str]] = []
    loop = asyncio.get_running_loop()

    async def move_base(step: str) -> str:
        return merger.merge(
            {"specs/auth/spec.md": _auth_spec(preamble=f"\n{step}\n")}, step
        )

    async def settled(sha: str) -> dict[str, Any]:
        state = await spec_index.index_state(db, project_id)
        assert state is not None and state["commit_sha"] == sha
        files = await spec_index.indexed_files(db, project_id)
        assert [f["revision"] for f in files] == [merger.blob("specs/auth/spec.md")]
        return {"commit": sha, "files": len(files)}

    # REST.
    sha = await move_base("rest")
    resp = await _reindex(client, project_id)
    calls.append(("POST", path))
    assert resp.status_code == 200, resp.text
    rest = resp.json()
    assert rest["state"] == "indexed"
    assert rest["commit"] == sha
    assert await settled(sha) == {"commit": sha, "files": 1}

    # CLI: настоящий разбор argv, вызов API уходит в тот же ASGI-клиент.
    def bridge(method: str, route: str, body: Any = None, **_kw: Any) -> Any:
        calls.append((method, route))
        reply = asyncio.run_coroutine_threadsafe(
            client.request(method, route, json=body), loop
        ).result(60)
        assert reply.status_code == 200, reply.text
        return reply.json()

    def run_cli() -> int:
        with (
            patch.object(sys, "argv", ["oc-hub", "spec", "reindex", str(project_id)]),
            patch.object(cli, "_api", bridge),
        ):
            return int(cli.main() or 0)

    sha = await move_base("cli")
    assert await asyncio.to_thread(run_cli) == 0
    assert await settled(sha) == {"commit": sha, "files": 1}

    # MCP: инструмент зовёт тот же маршрут.
    async def post(route: str, body: dict[str, Any] | None = None, **_kw: Any) -> Any:
        calls.append(("POST", route))
        reply = await client.post(route, json=body or {})
        if reply.status_code >= 400:
            raise HubApiError({"message": reply.text, "status": reply.status_code})
        return reply.json()

    monkeypatch.setattr(mcp_server, "_api_post", post)
    sha = await move_base("mcp")
    result = await hub_reindex_spec(project_id)
    structured = _mcp_structured(result) or {}
    assert structured["state"] == "indexed"
    assert structured["commit"] == sha
    assert await settled(sha) == {"commit": sha, "files": 1}

    assert calls == [("POST", path)] * 3


async def test_failed_reindex_keeps_previous_index(
    client: AsyncClient, db, origin: Origin, merger: Merger, project_id: int
):
    """AC-3: файл не разобран или дубль id - команда отказана, индекс цел.

    Допустимая правка соседнего файла в том же коммите тоже не пишется: пишется
    всё или ничего.
    """
    origin.publish({"specs/auth/spec.md": AUTH_SPEC})
    assert (await spec_index.index_project(db, project_id)).state == "indexed"
    before = await _snapshot(db, project_id)

    # Битый файл: у REQ-10 нет обязательства.
    merger.merge(
        {
            "specs/auth/spec.md": _auth_spec(preamble="\nA harmless edit.\n"),
            "specs/billing/spec.md": "## REQ-10\n\n### Scenario\n",
        },
        "broken billing spec",
    )
    resp = await _reindex(client, project_id)
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert "specs/billing/spec.md" in detail["message"]
    assert [i["file"] for i in detail["issues"]] == ["specs/billing/spec.md"]
    assert await _snapshot(db, project_id) == before

    # Дубль id: REQ-1 уже объявлен в specs/auth/spec.md.
    duplicate = BILLING_SPEC.replace("REQ-10", "REQ-1")
    merger.merge({"specs/billing/spec.md": duplicate}, "duplicate id")
    resp = await _reindex(client, project_id)
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert "specs/billing/spec.md" in detail["message"]
    assert "REQ-1" in detail["message"]
    assert await _snapshot(db, project_id) == before

    # Поллер на том же коммите молчит: индекс по-прежнему цел.
    await _tick(db)
    assert await _snapshot(db, project_id) == before


async def test_reindex_skips_unapproved_requirement_block(
    client: AsyncClient, db, origin: Origin, merger: Merger, project_id: int
):
    """AC-4: слитый блок, чей хеш не равен одобренному снимку на этом SHA, не берётся.

    Индекс оставляет прежнюю ревизию REQ-1 и не подставляет текст из коммита;
    тот же блок с одобренным хешем на своём SHA проходит.
    """
    origin.publish({"specs/auth/spec.md": AUTH_SPEC})
    assert (await spec_index.index_project(db, project_id)).state == "indexed"
    before = {r.id: r for r in await spec_index.indexed_requirements(db, project_id)}
    smuggled = "The hub accepts an empty password."

    # Текст слит вне проверенного SHA: снимка сдачи на этом коммите нет.
    first = merger.merge({"specs/auth/spec.md": _auth_spec(req1=smuggled)}, "bypass")
    resp = await _reindex(client, project_id)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["state"] == "indexed"
    assert [(s["file"], s["reason"].split(":")[0]) for s in body["skipped"]] == [
        ("specs/auth/spec.md", "REQ-1")
    ]
    after = {r.id: r for r in await spec_index.indexed_requirements(db, project_id)}
    assert after["REQ-1"].obligation == REQ1_TEXT
    assert after["REQ-1"].revision == before["REQ-1"].revision
    assert after["REQ-2"] == before["REQ-2"].model_copy(
        update={"file_revision": merger.blob("specs/auth/spec.md")}
    )
    state = await spec_index.index_state(db, project_id)
    assert state is not None and state["commit_sha"] == first

    # Снимок есть, но на другой текст: хеш не равен - блок по-прежнему не берётся.
    other = "The hub locks every login."
    second = merger.merge(
        {"specs/auth/spec.md": _auth_spec(req1=smuggled, preamble="\nSecond.\n")},
        "again",
    )
    await _approve_merge(client, db, project_id, second, [_modified(other)])
    await _tick(db)
    kept = {r.id: r for r in await spec_index.indexed_requirements(db, project_id)}
    assert kept["REQ-1"].obligation == REQ1_TEXT
    state = await spec_index.index_state(db, project_id)
    assert state is not None and state["commit_sha"] == second

    # Одобренный хеш на своём SHA: блок принимается, ревизия блока новая.
    approved = "The hub refuses a login with a blank or missing password."
    third = merger.merge({"specs/auth/spec.md": _auth_spec(req1=approved)}, "approved")
    await _approve_merge(client, db, project_id, third, [_modified(approved)])
    resp = await _reindex(client, project_id)
    assert resp.status_code == 200, resp.text
    assert resp.json()["skipped"] == []
    final = {r.id: r for r in await spec_index.indexed_requirements(db, project_id)}
    assert final["REQ-1"].obligation == approved
    assert final["REQ-1"].revision == _req1_hash(approved)
