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

from hub import cli, config, mcp_server
from hub import poller
from hub import repository as repo
from hub.auth import chat_pair_route_allowed
from hub.integrations import git_spec
from hub.integrations.git_ops import GitOpsIntegration
from hub.integrations.registry import plugins
from hub.mcp_server import HubApiError, hub_reindex_spec
from hub.services import chat_pair, spec_index, spec_reindex
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


def _removed() -> dict[str, Any]:
    """Запись снимка: REQ-1 снят; блок снятия - хеш прежнего текста (как в снимке хаба)."""
    return {
        "requirement_id": "REQ-1",
        "op": "removed",
        "base_spec_revision": _req1_hash(REQ1_TEXT),
        "new_requirement_id": "",
        "obligation": REQ1_TEXT,
        "scenarios": [REQ1_SCENARIO],
        "content_hash": _req1_hash(REQ1_TEXT),
    }


def _without_req1() -> str:
    """AUTH_SPEC без REQ-1: коммит, который его снимает."""
    return "# Auth\n\n" + AUTH_SPEC[AUTH_SPEC.index("## REQ-2") :]


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


async def test_reindex_does_not_take_an_approved_text_that_was_removed_later(
    client: AsyncClient, db, origin: Origin, merger: Merger, project_id: int
):
    """Порядок снимков в окне: одобренный текст, потом одобренное снятие блока.

    Три слияния в одном окне ``прежний..новый``: одобренный modified REQ-1 на
    текст A, одобренный removed REQ-1, и недоверенный коммит, который возвращает
    в файл тот же текст A. Ранний хеш A снят позже: текст из слитого коммита
    индекс не подхватывает, прежняя ревизия остаётся.
    """
    origin.publish({"specs/auth/spec.md": AUTH_SPEC})
    assert (await spec_index.index_project(db, project_id)).state == "indexed"
    before = {r.id: r for r in await spec_index.indexed_requirements(db, project_id)}
    text_a = "The hub accepts an empty password."

    first = merger.merge({"specs/auth/spec.md": _auth_spec(req1=text_a)}, "approved A")
    second = merger.merge({"specs/auth/spec.md": _without_req1()}, "approved removal")
    merger.merge({"specs/auth/spec.md": _auth_spec(req1=text_a)}, "A comes back")
    await _approve_merge(client, db, project_id, first, [_modified(text_a)])
    await _approve_merge(client, db, project_id, second, [_removed()])

    resp = await _reindex(client, project_id)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [(s["file"], s["reason"].split(":")[0]) for s in body["skipped"]] == [
        ("specs/auth/spec.md", "REQ-1")
    ]
    after = {r.id: r for r in await spec_index.indexed_requirements(db, project_id)}
    assert after["REQ-1"].obligation == REQ1_TEXT
    assert after["REQ-1"].revision == before["REQ-1"].revision


async def test_reindex_takes_only_the_latest_approved_snapshot_of_a_block(
    client: AsyncClient, db, origin: Origin, merger: Merger, project_id: int
):
    """Порядок снимков в окне: одобрены B, затем C, файл откатили на B без снимка.

    Порядок задаёт история git, а не очередность записей слияний: снимок C
    записан в хабе РАНЬШЕ снимка B, но слит позже. Принимается только последний
    одобренный текст блока в окне, откат на B - нет; а C, будь он в файле, прошёл бы.
    """
    origin.publish({"specs/auth/spec.md": AUTH_SPEC})
    assert (await spec_index.index_project(db, project_id)).state == "indexed"
    text_b = "The hub refuses a login with a blank password."
    text_c = "The hub refuses a login with a blank or missing password."

    first = merger.merge({"specs/auth/spec.md": _auth_spec(req1=text_b)}, "approved B")
    second = merger.merge({"specs/auth/spec.md": _auth_spec(req1=text_c)}, "approved C")
    merger.merge({"specs/auth/spec.md": _auth_spec(req1=text_b, preamble="\nr\n")}, "B")
    await _approve_merge(client, db, project_id, second, [_modified(text_c)])
    await _approve_merge(client, db, project_id, first, [_modified(text_b)])

    resp = await _reindex(client, project_id)

    assert resp.status_code == 200, resp.text
    assert [s["reason"].split(":")[0] for s in resp.json()["skipped"]] == ["REQ-1"]
    kept = {r.id: r for r in await spec_index.indexed_requirements(db, project_id)}
    assert kept["REQ-1"].obligation == REQ1_TEXT

    # Тот же порядок, но файл остаётся на последнем одобренном тексте C: принят.
    third = merger.merge({"specs/auth/spec.md": _auth_spec(req1=text_b)}, "approved B2")
    fourth = merger.merge(
        {"specs/auth/spec.md": _auth_spec(req1=text_c)}, "approved C2"
    )
    await _approve_merge(client, db, project_id, fourth, [_modified(text_c)])
    await _approve_merge(client, db, project_id, third, [_modified(text_b)])
    resp = await _reindex(client, project_id)
    assert resp.status_code == 200, resp.text
    assert resp.json()["skipped"] == []
    final = {r.id: r for r in await spec_index.indexed_requirements(db, project_id)}
    assert final["REQ-1"].obligation == text_c


async def _pending_removal(client, db, merger: Merger, project_id: int, origin: Origin):
    """Индекс на A (REQ-1, REQ-2); одобренное снятие REQ-1 слито, индекс его не видел."""
    origin.publish({"specs/auth/spec.md": AUTH_SPEC})
    assert (await spec_index.index_project(db, project_id)).state == "indexed"
    old = (await spec_index.index_state(db, project_id))["commit_sha"]
    removal = merger.merge({"specs/auth/spec.md": _without_req1()}, "approved removal")
    await _approve_merge(client, db, project_id, removal, [_removed()])
    return old


async def _assert_index_untouched(db, project_id: int, old: str) -> None:
    state = await spec_index.index_state(db, project_id)
    assert state["commit_sha"] == old
    ids = {r.id for r in await spec_index.indexed_requirements(db, project_id)}
    assert ids == {"REQ-1", "REQ-2"}


@pytest.mark.parametrize("failure", ["raises", "unreadable"])
async def test_unread_commit_window_keeps_the_index_and_the_cursor(
    client: AsyncClient,
    db,
    origin: Origin,
    merger: Merger,
    project_id: int,
    monkeypatch,
    failure: str,
):
    """Окно ``прежний..новый`` не прочиталось: список коммитов - не «одобрений нет».

    Одобренное снятие REQ-1 слито, но git не назвал коммиты окна (исключение или
    ``unreadable``). Индекс не переписывается, курсор стоит, ответ - не чистый
    ``indexed``: 503 с причиной. Когда git снова отвечает, то же окно читается и
    снятие применяется.
    """
    old = await _pending_removal(client, db, merger, project_id, origin)
    real = plugins.git_ops.spec_range

    async def broken(repo: str, a: str, b: str) -> dict[str, Any]:
        if failure == "raises":
            raise OSError("git is gone")
        return {
            "state": "unreadable",
            "specs_changed": True,
            "commits": [],
            "truncated": False,
            "reason": "git rev-list failed",
        }

    monkeypatch.setattr(plugins.git_ops, "spec_range", broken)
    resp = await _reindex(client, project_id)

    assert resp.status_code == 503, resp.text
    assert resp.json()["detail"]["reason"] == "spec_reindex_unreadable"
    await _assert_index_untouched(db, project_id, old)

    monkeypatch.setattr(plugins.git_ops, "spec_range", real)
    resp = await _reindex(client, project_id)
    assert resp.status_code == 200, resp.text
    ids = {r.id for r in await spec_index.indexed_requirements(db, project_id)}
    assert ids == {"REQ-2"}


async def test_truncated_commit_window_keeps_the_index_and_the_poller_retries(
    client: AsyncClient,
    db,
    origin: Origin,
    merger: Merger,
    project_id: int,
    monkeypatch,
):
    """Окно длиннее ``MAX_RANGE_COMMITS``: старые одобрения усечены, индекс не двигается.

    Одобренное снятие - самый старый коммит окна, поверх него ещё коммит. С
    пределом в один коммит снятие выпадает из списка: команда и поллер не
    переписывают индекс и не двигают курсор, а когда окно снова помещается,
    поллер читает его и снимает REQ-1.
    """
    old = await _pending_removal(client, db, merger, project_id, origin)
    merger.merge(
        {"specs/auth/spec.md": _without_req1().replace("# Auth\n", "# Auth\n\nr\n", 1)},
        "tail",
    )
    monkeypatch.setattr(git_spec, "MAX_RANGE_COMMITS", 1)

    resp = await _reindex(client, project_id)
    assert resp.status_code == 503, resp.text
    await _tick(db)
    await _assert_index_untouched(db, project_id, old)

    monkeypatch.setattr(git_spec, "MAX_RANGE_COMMITS", 1000)
    await _tick(db)
    ids = {r.id for r in await spec_index.indexed_requirements(db, project_id)}
    assert ids == {"REQ-2"}


async def _pair_headers(
    client: AsyncClient, db, kind: str, task_id: int
) -> dict[str, str]:
    """Заголовок сессии chat-pair настоящего вида: код выписан и обменян по-настоящему."""
    await db.execute(
        "INSERT OR IGNORE INTO principals (id, kind, username, status) "
        "VALUES (47, 'agent', 'reindex-pair', 'active')"
    )
    code, _ = await chat_pair.issue_code(
        db, 47, kind=kind, bound_task_id=task_id, bound_generation=1
    )
    await db.commit()
    resp = await client.post(
        "/api/auth/chat-pair/redeem",
        json={"code": code},
        headers={
            "x-forwarded-for": "203.0.113."
            + str({"implementer": 47, "reviewer": 48}.get(kind, 49))
        },
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


async def test_chat_pair_implementer_reaches_the_reindex_command(
    client: AsyncClient, db, origin: Origin, project_id: int, monkeypatch
):
    """AC-2 через настоящий допуск: токены включены, команду зовёт сессия исполнителя.

    Тест MCP подменяет ``_api_post`` и допуск не задевает; здесь запрос идёт
    через ``AuthMiddleware``. Исполнитель chat-pair доходит до обработчика
    только для проекта своей задачи: у дочерней задачи ``project_id`` пуст, проект
    наследуется от эпика. Чужой проект, ревьюер и intake получают 403;
    человеческий ключ вне chat-pair чужой проект переиндексирует как прежде.
    """
    origin.publish({"specs/auth/spec.md": AUTH_SPEC})
    assert (await spec_index.index_project(db, project_id)).state == "indexed"
    foreign = await repo.create_project(db, slug="foreign-specs", name="Foreign")
    epic = await client.post(
        "/api/tasks", json={"title": "pair epic", "task_type": "epic"}
    )
    epic_id = int(epic.json()["id"])
    await repo.update_task(db, epic_id, project_id=project_id)
    created = await client.post(
        "/api/tasks", json={"title": "pair reindex", "parent_id": epic_id}
    )
    task_id = int(created.json()["id"])
    second = await client.post(
        "/api/tasks", json={"title": "pair review", "parent_id": epic_id}
    )
    review_task = int(second.json()["id"])
    await repo.update_task(db, review_task, status="review", submission_generation=1)
    await db.commit()
    assert (await repo.get_task(db, task_id))["project_id"] is None
    monkeypatch.setattr(
        config,
        "HUB_TOKENS",
        {
            "agent-2223-x": config.TokenIdentity("bot", "agent"),
            "human-2223-x": config.TokenIdentity("owner", "human"),
        },
    )
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    chat_pair.chat_pair_limiter._buckets.clear()
    implementer = await _pair_headers(client, db, "implementer", task_id)
    reviewer = await _pair_headers(client, db, "reviewer", review_task)
    intake = await _pair_headers(client, db, "intake", task_id)
    path = f"/api/projects/{project_id}/spec/reindex"
    foreign_path = f"/api/projects/{foreign}/spec/reindex"

    for headers in (reviewer, intake):
        refused = await client.post(path, headers=headers)
        assert refused.status_code == 403, refused.text
    for target in (foreign_path, "/api/projects/99/spec/reindex"):
        refused = await client.post(target, headers=implementer)
        assert refused.status_code == 403, refused.text
        assert refused.json()["detail"]["reason"] == "chat_pair_gate_forbidden"
    reached = await client.post(path, headers=implementer)
    assert reached.status_code == 200, reached.text
    assert reached.json()["state"] == "indexed"
    human = await client.post(
        foreign_path, headers={"Authorization": "Bearer human-2223-x"}
    )
    assert human.status_code != 403, human.text


def test_reindex_route_names_the_project_of_the_bound_task():
    """Без проекта задачи сессии допуск исполнителя на reindex закрыт.

    ``{project_id}`` сверяется так же, как ``{task_id}``: задача 7, POST на
    проект 99 — отказ, пока middleware не назвал проект задачи равным 99.
    """
    session = config.TokenIdentity(
        "bot",
        "agent",
        auth_source="chat_pair",
        chat_pair_kind="implementer",
        chat_pair_task_id=7,
    )
    path = "/api/projects/99/spec/reindex"
    assert not chat_pair_route_allowed("POST", path, session)
    assert not chat_pair_route_allowed("POST", path, session, bound_project_id=5)
    assert chat_pair_route_allowed("POST", path, session, bound_project_id=99)
    assert chat_pair_route_allowed("POST", "/api/tasks/7/claim", session)
    for kind in ("reviewer", "intake"):
        other = config.TokenIdentity(
            "bot",
            "agent",
            auth_source="chat_pair",
            chat_pair_kind=kind,
            chat_pair_task_id=7,
        )
        assert not chat_pair_route_allowed("POST", path, other, bound_project_id=99)
