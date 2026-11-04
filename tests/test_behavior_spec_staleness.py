"""Рассинхрон файла и индекса меняет ответ контекста (#2216).

Индекс построен на ревизии файла A, база уже на ревизии B. Контекст называет
обе ревизии и отдаёт текст обязательства из файла на B, помеченный как текст
файла; текст индекса действующим не называется. Когда ревизии совпадают,
текст берётся из индекса. Один и тот же ответ у REST, CLI и MCP. Переиндексации
здесь нет: это #2223.
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
from tests.test_behavior_spec_context import _context, _delta, _ids, _task
from tests.test_behavior_spec_index import AUTH_SPEC, BRANCH, Origin
from tests.test_mcp_server import _mcp_structured, _mcp_text

INDEXED_TEXT = "The hub refuses a login with an empty password."
FILE_TEXT = "The hub refuses a login with an empty or a blank password."
FILE_SPEC = AUTH_SPEC.replace(
    "Obligation: " + INDEXED_TEXT, "Obligation: " + FILE_TEXT, 1
)
PATH = "specs/auth/spec.md"


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


async def _indexed_task(
    client: AsyncClient, db, origin: Origin, project_id: int
) -> tuple[int, str]:
    """Индекс на файле A (возвращается его blob SHA), задача ссылается на REQ-1."""
    origin.publish({PATH: AUTH_SPEC})
    indexed = await spec_index.index_project(db, project_id)
    assert indexed.state == "indexed", indexed.detail
    task_id = await _task(client, db, project_id, behavior_delta=_delta("REQ-1"))
    return task_id, origin.blob(PATH)


async def _mcp_context(rest: dict, task_id: int, monkeypatch) -> tuple[str, dict]:
    async def fake_get(path: str, timeout: float | None = None) -> dict:
        assert path.startswith(f"/api/tasks/{task_id}/context")
        return rest

    monkeypatch.setattr("hub.mcp_server._api_get", fake_get)
    result = await hub_my_context(task_id)
    structured = _mcp_structured(result) or {}
    return _mcp_text(result), structured.get("context") or structured


def _surfaces_agree(
    text: str, *, shown: list[str], hidden: list[str], label: str
) -> None:
    for needle in shown:
        assert needle in text, f"{label} lacks {needle!r}"
    for needle in hidden:
        assert needle not in text, f"{label} still names {needle!r}"


async def test_stale_context_returns_file_text_and_both_revisions(
    client: AsyncClient, db, origin: Origin, project_id: int, capsys, monkeypatch
):
    """AC-1: индекс на файле A, база на B - обе ревизии и текст файла на B."""
    task_id, rev_a = await _indexed_task(client, db, origin, project_id)
    origin.publish({PATH: FILE_SPEC}, message="spec moved on")
    rev_b = origin.blob(PATH)
    assert rev_a != rev_b

    rest = await _context(client, task_id)

    assert _ids(rest) == ["REQ-1"]
    item = rest["requirements"][0]
    assert item["source"] == "file"
    assert item["obligation"] == FILE_TEXT
    assert item["index_file_revision"] == rev_a
    assert item["file_revision"] == rev_b
    shown, hidden = [rev_a, rev_b, FILE_TEXT, "file"], [INDEXED_TEXT]
    _surfaces_agree(rest["context_text"], shown=shown, hidden=hidden, label="REST")

    rc, _api = _run_main(["context", str(task_id)], api_result=rest)
    assert rc == 0
    _surfaces_agree(capsys.readouterr().out, shown=shown, hidden=hidden, label="CLI")

    text, ctx = await _mcp_context(rest, task_id, monkeypatch)
    _surfaces_agree(text, shown=shown, hidden=hidden, label="MCP text")
    mcp_item = ctx["requirements"][0]
    assert mcp_item["source"] == "file"
    assert mcp_item["obligation"] == FILE_TEXT
    assert mcp_item["index_file_revision"] == rev_a
    assert mcp_item["file_revision"] == rev_b


async def test_fresh_context_returns_requirement_text(
    client: AsyncClient, db, origin: Origin, project_id: int, capsys, monkeypatch
):
    """AC-2: ревизии совпадают - текст требования берётся из индекса."""
    task_id, rev_a = await _indexed_task(client, db, origin, project_id)

    rest = await _context(client, task_id)

    item = rest["requirements"][0]
    assert item["source"] == "index"
    assert item["obligation"] == INDEXED_TEXT
    assert item["index_file_revision"] == item["file_revision"] == rev_a
    assert INDEXED_TEXT in rest["context_text"]
    assert "read from file" not in rest["context_text"]

    rc, _api = _run_main(["context", str(task_id)], api_result=rest)
    assert rc == 0
    assert INDEXED_TEXT in capsys.readouterr().out
    text, ctx = await _mcp_context(rest, task_id, monkeypatch)
    assert INDEXED_TEXT in text
    assert ctx["requirements"][0]["source"] == "index"


async def test_requirement_gone_from_newer_file_is_not_served_from_index(
    client: AsyncClient, db, origin: Origin, project_id: int
):
    """Файл на B больше не содержит REQ-1: текст индекса не выдаётся за действующий."""
    task_id, rev_a = await _indexed_task(client, db, origin, project_id)
    only_req_2 = AUTH_SPEC.split("## REQ-2", 1)
    origin.publish({PATH: "# Auth\n\n## REQ-2" + only_req_2[1]})
    rev_b = origin.blob(PATH)

    rest = await _context(client, task_id)

    item = rest["requirements"][0]
    assert item["source"] == "file_unavailable"
    assert item["obligation"] == ""
    assert item["index_file_revision"] == rev_a
    assert item["file_revision"] == rev_b
    assert INDEXED_TEXT not in rest["context_text"]
    assert rev_a in rest["context_text"]
    assert rev_b in rest["context_text"]
