"""Неизменяемый снимок дельты на сдаче (#2218).

Единственное поле дельты — ``behavior_delta``. На сдаче хаб фиксирует записи,
поколение и head_sha и дальше отдаёт этот снимок как был. Список id для
контекста считается из записей и заново не пишется.
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

import pytest
from httpx import AsyncClient

from hub import repository as repo
from hub.integrations.git_ops import GitOpsIntegration
from hub.integrations.registry import plugins
from hub.mcp_server import hub_get_review_brief, hub_submit_for_review
from hub.services import spec_index
from hub.services.spec_format import parse_spec_file, requirement_revision
from tests.cli_support import _run_main
from tests.state_support import make_state_task
from tests.test_behavior_spec_index import AUTH_SPEC, BRANCH, Origin
from tests.test_mcp_server import _mcp_structured, _mcp_text

S1 = "a" * 40
S2 = "b" * 40
HEAD_FILE = "specs/auth/spec.md"


def _scenario(ref: str) -> dict[str, str]:
    return {
        "given": "a registered user",
        "when": "the user logs in",
        "then": "the hub answers from the delta",
        "verifiable_by": "test",
        "test_ref": ref,
    }


def _record(
    op: str,
    requirement_id: str,
    *,
    revision: str = "",
    new_requirement_id: str | None = None,
    obligation: str = "The hub records the behavior the submission approved.",
    ref: str = "tests/test_behavior_spec_snapshot.py::test_submit_freezes_delta_with_generation_and_sha",
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "requirement_id": requirement_id,
        "op": op,
        "base_spec_revision": revision,
        "obligation": obligation,
        "scenarios": [_scenario(ref)],
    }
    if new_requirement_id is not None:
        body["new_requirement_id"] = new_requirement_id
    return body


def _block_hash(requirement_id: str, record: dict[str, Any]) -> str:
    """sha256 канонического блока ``{id, obligation, scenarios}``."""
    from hub.models import SpecRequirement, SpecScenario

    block = SpecRequirement(
        id=requirement_id,
        obligation=record["obligation"],
        scenarios=[SpecScenario(**s) for s in record["scenarios"]],
    )
    return requirement_revision(block)


@pytest.fixture
def origin(tmp_path) -> Origin:
    return Origin(tmp_path / "git")


@pytest.fixture
async def project_id(db, origin: Origin, monkeypatch) -> int:
    monkeypatch.setattr(plugins, "git_ops", GitOpsIntegration())
    pid = await repo.create_project(
        db,
        slug="snap-proj",
        name="Snap",
        workspace_path=str(origin.clone),
        default_branch=BRANCH,
    )
    await db.commit()
    return pid


def _pin(monkeypatch, sha: str) -> None:
    async def tip(*_args, **_kwargs) -> tuple[str, str]:
        return sha, ""

    monkeypatch.setattr("hub.services.lifecycle.resolve_branch_tip", tip)


async def _indexed_revision(db, origin: Origin, project_id: int) -> str:
    origin.publish({HEAD_FILE: AUTH_SPEC})
    indexed = await spec_index.index_project(db, project_id)
    assert indexed.state == "indexed", indexed.detail
    return requirement_revision(parse_spec_file(AUTH_SPEC)[0])


async def _task(
    client: AsyncClient, db, project_id: int | None, **refine: object
) -> int:
    created = await client.post("/api/tasks", json={"title": "snapshot"})
    assert created.status_code == 200, created.text
    task_id = int(created.json()["id"])
    if project_id is not None:
        await repo.update_task(db, task_id, project_id=project_id)
        await db.commit()
    if refine:
        patched = await client.post(f"/api/tasks/{task_id}/refine", json=refine)
        assert patched.status_code == 200, patched.text
    return task_id


async def _pair(client: AsyncClient, task_id: int) -> None:
    planned = await client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": "dev", "kind": "status", "content": "Plan: freeze the delta"},
    )
    assert planned.status_code == 200, planned.text
    resp = await client.post(
        f"/api/tasks/{task_id}/pair-start",
        json={
            "assigned_agent": "dev",
            "session_id": "snap-2218",
            "git_mode": "remote",
            "branch_slug": "snapshot-on-submit",
        },
    )
    assert resp.status_code == 200, resp.text


async def _submit(client: AsyncClient, task_id: int) -> Any:
    return await client.post(f"/api/tasks/{task_id}/submit-review", json={})


async def _snapshot_row(db, task_id: int, generation: int) -> dict[str, Any] | None:
    rows = await db.execute_fetchall(
        "SELECT behavior_snapshot FROM submissions WHERE task_id=? AND generation=?",
        (task_id, generation),
    )
    if not rows:
        return None
    raw = dict(rows[0])["behavior_snapshot"]
    if not raw:
        return None
    return json.loads(raw)


def _detail(resp) -> dict[str, Any]:
    body = resp.json()
    detail = body.get("detail", body)
    return detail if isinstance(detail, dict) else {"message": detail}


async def test_submit_freezes_delta_with_generation_and_sha(
    client: AsyncClient, db, origin: Origin, project_id: int, monkeypatch
):
    """AC-1: сдача фиксирует запись, поколение и head_sha; чтение дальше то же."""
    revision = await _indexed_revision(db, origin, project_id)
    record = _record("modified", "REQ-1", revision=revision)
    task_id = await _task(client, db, project_id, behavior_delta=[record])
    await _pair(client, task_id)
    _pin(monkeypatch, S1)

    submitted = await _submit(client, task_id)
    assert submitted.status_code == 200, submitted.text
    body = submitted.json()
    assert body["submission_generation"] == 1

    frozen = await _snapshot_row(db, task_id, 1)
    assert frozen is not None
    assert frozen["generation"] == 1
    assert frozen["head_sha"] == S1
    assert len(frozen["records"]) == 1
    assert frozen["records"][0]["requirement_id"] == "REQ-1"
    assert frozen["records"][0]["op"] == "modified"
    assert frozen["records"][0]["content_hash"] == _block_hash("REQ-1", record)

    await client.post(
        f"/api/tasks/{task_id}/refine",
        json={"problem_statement": "text that is not the delta"},
    )
    again = await _snapshot_row(db, task_id, 1)
    assert again == frozen


async def test_patch_after_freeze_writes_next_draft(
    client: AsyncClient, db, origin: Origin, project_id: int, monkeypatch
):
    """AC-2: PATCH не трогает снимок; новая дельта — черновик, старое одобрение не завершает."""
    revision = await _indexed_revision(db, origin, project_id)
    first = _record(
        "modified", "REQ-1", revision=revision, obligation="First obligation."
    )
    task_id = await _task(client, db, project_id, behavior_delta=[first])
    await _pair(client, task_id)
    _pin(monkeypatch, S1)
    assert (await _submit(client, task_id)).status_code == 200
    frozen = await _snapshot_row(db, task_id, 1)

    approved = await client.post(
        f"/api/tasks/{task_id}/review-verdict",
        json={"verdict": "approved", "agent": "reviewer"},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "running"
    assert approved.json()["review_approved_current"] is True

    nxt = _record("modified", "REQ-1", revision=revision, obligation="Next draft.")
    patched = await client.post(
        f"/api/tasks/{task_id}/refine", json={"behavior_delta": [nxt]}
    )
    assert patched.status_code == 200, patched.text
    assert await _snapshot_row(db, task_id, 1) == frozen
    card = (await client.get(f"/api/tasks/{task_id}")).json()
    assert card["behavior_delta"][0]["obligation"] == "Next draft."
    assert card["review_approved_current"] is False
    assert card["status"] != "completed"

    done = await client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": "dev", "kind": "done", "content": "ship it"},
    )
    assert done.status_code == 200, done.text
    after = (await client.get(f"/api/tasks/{task_id}")).json()
    assert after["status"] != "completed"
    assert await _snapshot_row(db, task_id, 1) == frozen


async def test_delta_schema_rejects_bad_base_revision(
    client: AsyncClient, db, origin: Origin, project_id: int, monkeypatch
):
    """AC-3: added без ревизии принимается, modified с пустой ревизией — нет."""
    task_id = await _task(client, db, project_id)
    added = await client.post(
        f"/api/tasks/{task_id}/refine",
        json={"behavior_delta": [_record("added", "REQ-3", revision="")]},
    )
    assert added.status_code == 200, added.text
    stored = added.json()["behavior_delta"]
    assert stored[0]["op"] == "added"
    assert stored[0]["base_spec_revision"] == ""

    rejected = await client.post(
        f"/api/tasks/{task_id}/refine",
        json={"behavior_delta": [_record("modified", "REQ-1", revision="")]},
    )
    assert rejected.status_code == 422, rejected.text
    still = (await client.get(f"/api/tasks/{task_id}")).json()["behavior_delta"]
    assert still[0]["op"] == "added"

    revision = await _indexed_revision(db, origin, project_id)
    origin.publish({HEAD_FILE: "# Auth\n\nNo requirements remain.\n"})
    removed = _record("removed", "REQ-1", revision=revision, obligation="gone")
    accepted = await client.post(
        f"/api/tasks/{task_id}/refine", json={"behavior_delta": [removed]}
    )
    assert accepted.status_code == 200, accepted.text
    await _pair(client, task_id)
    _pin(monkeypatch, S1)
    submitted = await _submit(client, task_id)
    assert submitted.status_code == 200, submitted.text
    frozen = await _snapshot_row(db, task_id, 1)
    assert frozen is not None
    assert frozen["records"][0]["content_hash"] == revision
    assert frozen["records"][0]["content_hash"] != _block_hash("REQ-1", removed)


async def test_review_brief_shows_frozen_snapshot(
    client: AsyncClient, db, origin: Origin, project_id: int, monkeypatch, capsys
):
    """AC-4: бриф показывает зафиксированный снимок одним пунктом в REST, CLI и MCP."""
    revision = await _indexed_revision(db, origin, project_id)
    record = _record("modified", "REQ-1", revision=revision)
    task_id = await _task(client, db, project_id, behavior_delta=[record])
    await _pair(client, task_id)
    _pin(monkeypatch, S1)
    assert (await _submit(client, task_id)).status_code == 200

    rest = (await client.get(f"/api/tasks/{task_id}/review-brief")).json()
    snap = rest["behavior_snapshot"]
    assert snap["generation"] == 1
    assert snap["head_sha"] == S1
    assert snap["records"][0]["requirement_id"] == "REQ-1"
    assert set(snap) >= {"generation", "head_sha", "records"}

    rc, _api = _run_main(["review-brief", str(task_id)], api_result=rest)
    assert rc == 0
    cli_out = capsys.readouterr().out
    assert f'"head_sha": "{S1}"' in cli_out or f'"head_sha":"{S1}"' in cli_out
    from hub.services import review_brief_projection as projection

    cli_payload = projection.brief_result(rest, "", 0, "").structuredContent or {}
    cli_brief = cli_payload.get("brief") or cli_payload
    assert cli_brief["behavior_snapshot"] == snap

    async def fake_get(path: str, timeout: float | None = None) -> dict:
        if path.startswith(f"/api/tasks/{task_id}/review-brief"):
            return rest
        return {}

    monkeypatch.setattr("hub.mcp_server._api_get", fake_get)
    result = await hub_get_review_brief(task_id)
    structured = _mcp_structured(result) or {}
    mcp_brief = structured.get("brief") or structured
    assert mcp_brief["behavior_snapshot"] == snap
    assert "behavior_snapshot" in _mcp_text(result) or snap["head_sha"] in _mcp_text(
        result
    )

    empty_id = await _task(client, db, project_id, behavior_delta="no_delta")
    await _pair(client, empty_id)
    assert (await _submit(client, empty_id)).status_code == 200
    empty = (await client.get(f"/api/tasks/{empty_id}/review-brief")).json()
    assert empty["behavior_snapshot"]["records"] == []
    assert empty["behavior_snapshot"]["explicit_no_delta"] is True
    assert empty["behavior_snapshot"]["head_sha"] == S1


async def test_renamed_requires_new_requirement_id(
    client: AsyncClient, db, project_id: int
):
    """AC-5: renamed без new_requirement_id отвергнут, с ним — принят."""
    revision = "c" * 64
    task_id = await _task(client, db, project_id)
    missing = await client.post(
        f"/api/tasks/{task_id}/refine",
        json={"behavior_delta": [_record("renamed", "REQ-1", revision=revision)]},
    )
    assert missing.status_code == 422, missing.text
    card = (await client.get(f"/api/tasks/{task_id}")).json()
    assert card["behavior_delta"] == "unspecified"

    renamed = _record("renamed", "REQ-1", revision=revision, new_requirement_id="REQ-8")
    accepted = await client.post(
        f"/api/tasks/{task_id}/refine", json={"behavior_delta": [renamed]}
    )
    assert accepted.status_code == 200, accepted.text
    stored = accepted.json()["behavior_delta"][0]
    assert stored["new_requirement_id"] == "REQ-8"
    assert stored["op"] == "renamed"


async def test_requirement_ids_come_only_from_delta(
    client: AsyncClient, db, origin: Origin, project_id: int, capsys, monkeypatch
):
    """AC-6: чтение id берёт только behavior_delta; старый список отброшен и не пишется."""
    cols = [row[1] for row in await db.execute_fetchall("PRAGMA table_info(tasks)")]
    assert "requirement_ids" not in cols

    origin.publish({HEAD_FILE: AUTH_SPEC})
    assert (await spec_index.index_project(db, project_id)).state == "indexed"
    record = _record("added", "REQ-1", obligation="The hub refuses a login.")
    task_id = await _task(client, db, project_id)
    await db.execute("ALTER TABLE tasks ADD COLUMN requirement_ids TEXT")
    await db.execute(
        "UPDATE tasks SET requirement_ids=? WHERE id=?",
        (json.dumps(["REQ-9"]), task_id),
    )
    await db.commit()
    patched = await client.post(
        f"/api/tasks/{task_id}/refine",
        json={"requirement_ids": ["REQ-2"], "behavior_delta": [record]},
    )
    assert patched.status_code == 200, patched.text
    assert "requirement_ids" not in patched.json()

    rest = (await client.get(f"/api/tasks/{task_id}/context")).json()
    ids = [item["id"] for item in rest["requirements"]]
    assert ids == ["REQ-1"]
    assert "REQ-9" not in rest["context_text"]
    assert "REQ-2" not in rest["context_text"]

    rc, _api = _run_main(["context", str(task_id)], api_result=rest)
    assert rc == 0
    printed = capsys.readouterr().out
    assert "REQ-1" in printed
    assert "REQ-9" not in printed

    async def fake_get(path: str, timeout: float | None = None) -> dict:
        return rest

    monkeypatch.setattr("hub.mcp_server._api_get", fake_get)
    from hub.mcp_server import hub_my_context

    result = await hub_my_context(task_id)
    text = _mcp_text(result)
    structured = _mcp_structured(result) or {}
    ctx = structured.get("context") or structured
    assert [item["id"] for item in ctx.get("requirements") or []] == ["REQ-1"]
    assert "REQ-9" not in text


async def _refuse_on_surface(client: AsyncClient, task_id: int, monkeypatch) -> None:
    rest = await _submit(client, task_id)
    assert rest.status_code == 422, rest.text
    assert _detail(rest).get("reason") == "behavior_unspecified"

    loop = asyncio.get_running_loop()

    def cli_api(method: str, path: str, body: Any = None, **_kwargs: Any) -> Any:
        async def call() -> Any:
            resp = await client.request(method, path, json=body)
            if resp.status_code >= 400:
                print(resp.text, file=sys.stderr)
                return {"_refused": True, "text": resp.text}
            return resp.json()

        result = asyncio.run_coroutine_threadsafe(call(), loop).result()
        if isinstance(result, dict) and result.get("_refused"):
            raise SystemExit(1)
        return result

    monkeypatch.setattr("hub.cli._api", cli_api)
    rc = await asyncio.to_thread(
        __import__("hub.cli", fromlist=["main"]).main if False else _cli_main,
        ["submit-review", str(task_id)],
    )
    assert rc == 1

    async def post(path: str, body: Any = None, timeout: float | None = None) -> Any:
        resp = await client.post(path, json=body or {})
        if resp.status_code >= 400:
            from hub.mcp_server import HubApiError, _parse_api_error

            raise HubApiError(_parse_api_error(resp, resp.status_code))
        return resp.json()

    monkeypatch.setattr("hub.mcp_server._api_post", post)
    refused = await hub_submit_for_review(task_id)
    assert "behavior_unspecified" in str(refused)


def _cli_main(argv: list[str]) -> int:
    from hub import cli

    old = sys.argv
    sys.argv = ["oc-hub", *argv]
    try:
        return int(cli.main())
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1
    finally:
        sys.argv = old


async def test_unspecified_submit_does_not_freeze(
    client: AsyncClient, db, origin: Origin, project_id: int, monkeypatch
):
    """AC-7: внутри границы «не сказано» сдачу отвергает и снимок не создаёт."""
    await _indexed_revision(db, origin, project_id)
    task_id = await _task(client, db, project_id)
    await _pair(client, task_id)
    _pin(monkeypatch, S1)
    await _refuse_on_surface(client, task_id, monkeypatch)
    assert await _snapshot_row(db, task_id, 1) is None
    card = (await client.get(f"/api/tasks/{task_id}")).json()
    assert card["status"] == "running"
    assert card["submission_generation"] == 0


async def test_new_head_sha_voids_approval(
    client: AsyncClient, db, origin: Origin, project_id: int, monkeypatch
):
    """AC-8: новый head_sha не меняет снимок, одобрение недействительно, слияние блокируется."""
    from hub.services import orchestration

    revision = await _indexed_revision(db, origin, project_id)
    record = _record("modified", "REQ-1", revision=revision)
    task_id = await _task(client, db, project_id, behavior_delta=[record])
    await _pair(client, task_id)
    _pin(monkeypatch, S1)
    assert (await _submit(client, task_id)).status_code == 200
    frozen = await _snapshot_row(db, task_id, 1)
    approved = await client.post(
        f"/api/tasks/{task_id}/review-verdict",
        json={"verdict": "approved", "agent": "reviewer"},
    )
    assert approved.status_code == 200, approved.text

    _pin(monkeypatch, S2)
    assert await _snapshot_row(db, task_id, 1) == frozen
    task = dict(await repo.get_task(db, task_id))
    await repo.update_task(db, task_id, pr_number=77)
    await db.commit()
    task = dict(await repo.get_task(db, task_id))
    refusal, _note = await orchestration._approved_code_check(db, task, 77)
    assert refusal.startswith("stale_approval"), refusal
    assert S1[:12] in refusal and S2[:12] in refusal

    _pin(monkeypatch, S2)
    again = await _submit(client, task_id)
    assert again.status_code == 200, again.text
    assert again.json()["submission_generation"] == 2
    assert again.json()["review_approved_current"] is False
    assert await _snapshot_row(db, task_id, 1) == frozen
    nxt = await _snapshot_row(db, task_id, 2)
    assert nxt is not None and nxt["head_sha"] == S2 and nxt["generation"] == 2


async def test_unspecified_outside_gate_is_not_refused(
    client: AsyncClient, db, origin: Origin, project_id: int, monkeypatch
):
    """AC-9: без индекса, а также epic и feature, «не сказано» сдачу не отвергает и снимок не создаёт."""
    _pin(monkeypatch, S1)
    bare = await _task(client, db, project_id)
    await _pair(client, bare)
    accepted = await _submit(client, bare)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "review"
    assert await _snapshot_row(db, bare, 1) is None

    await _indexed_revision(db, origin, project_id)
    epic_id = 0
    for task_type in ("epic", "feature"):
        body = {"title": task_type, "task_type": task_type, "project": "snap-proj"}
        if task_type == "feature":
            body = {"title": task_type, "task_type": task_type, "parent_id": epic_id}
        created = await client.post("/api/tasks", json=body)
        assert created.status_code == 200, created.text
        item_id = int(created.json()["id"])
        if task_type == "epic":
            epic_id = item_id
        await _pair(client, item_id)
        resp = await _submit(client, item_id)
        assert resp.status_code != 422 or _detail(resp).get("reason") != (
            "behavior_unspecified"
        ), resp.text
        generation = (
            int(resp.json().get("submission_generation") or 0)
            if resp.status_code == 200
            else 1
        )
        assert await _snapshot_row(db, item_id, generation) is None


async def test_bare_delta_inside_gate_is_refused(
    client: AsyncClient, db, origin: Origin, project_id: int, monkeypatch
):
    """Голое ``delta`` внутри границы отвергается как «не сказано», снимка нет."""
    await _indexed_revision(db, origin, project_id)
    task_id = await _task(client, db, project_id, behavior_delta="delta")
    await _pair(client, task_id)
    _pin(monkeypatch, S1)
    await _refuse_on_surface(client, task_id, monkeypatch)
    assert await _snapshot_row(db, task_id, 1) is None
    card = (await client.get(f"/api/tasks/{task_id}")).json()
    assert card["status"] == "running"
    assert card["submission_generation"] == 0


async def test_state_task_unspecified_inside_gate_is_refused(
    client: AsyncClient, db, origin: Origin, project_id: int, monkeypatch
):
    """result_kind=state не обходит отказ «не сказано» ни в REST, ни в CLI, ни в MCP."""
    await _indexed_revision(db, origin, project_id)
    task_id = await make_state_task(
        db, project_id=project_id, status="running", plan=True
    )
    await db.commit()
    assert dict(await repo.get_task(db, task_id))["result_kind"] == "state"
    _pin(monkeypatch, S1)
    await _refuse_on_surface(client, task_id, monkeypatch)
    assert await _snapshot_row(db, task_id, 1) is None
    card = (await client.get(f"/api/tasks/{task_id}")).json()
    assert card["status"] == "running"
    assert card["submission_generation"] == 0


async def test_bare_delta_outside_gate_is_not_refused(
    client: AsyncClient, db, origin: Origin, project_id: int, monkeypatch
):
    """Вне границы голое ``delta`` сдачу не отвергает и снимка не создаёт."""
    _pin(monkeypatch, S1)
    bare = await _task(client, db, project_id, behavior_delta="delta")
    await _pair(client, bare)
    accepted = await _submit(client, bare)
    assert accepted.status_code == 200, accepted.text
    assert await _snapshot_row(db, bare, 1) is None

    await _indexed_revision(db, origin, project_id)
    created = await client.post(
        "/api/tasks",
        json={"title": "epic", "task_type": "epic", "project": "snap-proj"},
    )
    assert created.status_code == 200, created.text
    epic_id = int(created.json()["id"])
    await repo.update_task(db, epic_id, behavior_delta="delta")
    await db.commit()
    await _pair(client, epic_id)
    resp = await _submit(client, epic_id)
    assert resp.status_code != 422 or _detail(resp).get("reason") != (
        "behavior_unspecified"
    ), resp.text
    generation = (
        int(resp.json().get("submission_generation") or 0)
        if resp.status_code == 200
        else 1
    )
    assert await _snapshot_row(db, epic_id, generation) is None


async def test_no_delta_inside_gate_still_freezes_empty_snapshot(
    client: AsyncClient, db, origin: Origin, project_id: int, monkeypatch
):
    """Явное ``no_delta`` внутри границы по-прежнему даёт снимок без записей."""
    await _indexed_revision(db, origin, project_id)
    task_id = await _task(client, db, project_id, behavior_delta="no_delta")
    await _pair(client, task_id)
    _pin(monkeypatch, S1)
    submitted = await _submit(client, task_id)
    assert submitted.status_code == 200, submitted.text
    frozen = await _snapshot_row(db, task_id, 1)
    assert frozen is not None
    assert frozen["records"] == []
    assert frozen["explicit_no_delta"] is True
    assert frozen["head_sha"] == S1
