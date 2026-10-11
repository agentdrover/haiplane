"""Контракт сдачи (#1436): model, summary и мутация на каждый AC с тестом.

Облачный исполнитель дважды подряд сдал без обязательного по промпту: SID-8
(#1383) без модели, SID-9 (#1384) — без модели, без описания и без мутаций.
Промпт этого не удерживает; удерживает политика проекта ``submission_contract``:
``off`` — как было, ``warn`` — сдача принята и нарушения названы одной записью
в карточке, ``require`` — отказ до записи поколения.
"""

from __future__ import annotations

import json
import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest
from httpx import AsyncClient

from hub import cli
from hub import repository as repo
from hub import services
from hub.integrations.noop import NoopGitOps
from hub.integrations.registry import plugins
from hub.models import AcceptanceCriterion, TaskRefine, validated_gate_policy
from hub.services.project_policy import (
    SUBMISSION_CONTRACT_KEY,
    submission_contract_of,
)

_TIP = "c" * 40
_AREAS = ["hub/example.py", "tests/test_example.py"]
_REF_1 = "tests/test_example.py::test_one"
_REF_2 = "tests/test_example.py::test_two"
_VIOLATION_MARK = "Контракт сдачи нарушен"

_FULL_MUTATIONS = [
    {"ac": "AC-1", "mutation": "return None в one()", "failed_test": _REF_1},
    {"ac": "AC-2", "mutation": "убрана ветка в two()", "failed_test": _REF_2},
]


class _PinnedGitOps(NoopGitOps):
    async def fetch_base(self, repo: str, base: str):
        return True, ""

    async def head_sha(self, repo: str, base: str) -> str:
        return _TIP

    async def branch_diff_paths(self, branch, base_branch=None, repo=None):
        return list(_AREAS)


async def _node(
    db: aiosqlite.Connection, *, title: str, task_type: str, parent_id: int | None
) -> int:
    return await repo.create_task(
        db,
        title=title,
        description="",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status="open",
        auto_review=False,
        task_type=task_type,
        parent_id=parent_id,
        priority="medium",
    )


def _ac(ac_id: str, verifiable_by: str, test_ref: str | None) -> AcceptanceCriterion:
    return AcceptanceCriterion(
        id=ac_id,
        given="дано",
        when="когда",
        then="тогда",
        verifiable_by=verifiable_by,
        test_ref=test_ref,
    )


async def _running_task(
    db: aiosqlite.Connection, slug: str, contract: str | None
) -> int:
    """Pair-задача в running: два AC с тестом и один ручной (мутация не нужна)."""
    pid = await repo.create_project(
        db, slug=slug, name=slug.title(), workspace_path="/tmp/ws"
    )
    if contract is not None:
        await repo.update_project(
            db, pid, gate_policy=json.dumps({SUBMISSION_CONTRACT_KEY: contract})
        )
    epic = await _node(db, title="epic", task_type="epic", parent_id=None)
    await repo.update_task(db, epic, project_id=pid)
    feature = await _node(db, title="feature", task_type="feature", parent_id=epic)
    task_id = await _node(db, title="probe", task_type="task", parent_id=feature)
    await repo.add_task_update(db, task_id, "dev", "status", "Plan: work")
    await repo.update_task_structured(db, task_id, TaskRefine(affected_areas=_AREAS))
    await repo.add_acceptance_criterion(db, task_id, _ac("AC-1", "test", _REF_1))
    await repo.add_acceptance_criterion(db, task_id, _ac("AC-2", "test", _REF_2))
    await repo.add_acceptance_criterion(db, task_id, _ac("AC-3", "manual", None))
    await db.commit()

    plugins.git_ops = _PinnedGitOps()
    started = await services.pair_start_task(db, task_id, caller="dev-agent")
    assert started.status.value == "running"
    return task_id


async def _feed(db: aiosqlite.Connection, task_id: int) -> list[dict[str, Any]]:
    return [dict(u) for u in await repo.get_task_updates(db, task_id)]


def _detail(resp) -> dict[str, Any]:
    body = resp.json()
    detail = body.get("detail", body)
    assert isinstance(detail, dict), resp.text
    return detail


# --------------------------------------------------------------------------
# Политика: ключ, чтение, запись
# --------------------------------------------------------------------------


def test_the_policy_reads_off_by_default_and_warn_when_unreadable():
    assert submission_contract_of({}) == "off"
    assert submission_contract_of(None) == "off"  # type: ignore[arg-type]
    assert submission_contract_of({SUBMISSION_CONTRACT_KEY: "off"}) == "off"
    assert submission_contract_of({SUBMISSION_CONTRACT_KEY: "warn"}) == "warn"
    assert submission_contract_of({SUBMISSION_CONTRACT_KEY: "require"}) == "require"
    # Нечитаемое значение — warn: ключ поставлен, значит контракт хотели,
    # но отказывать по опечатке нельзя.
    assert submission_contract_of({SUBMISSION_CONTRACT_KEY: "requier"}) == "warn"
    assert submission_contract_of({SUBMISSION_CONTRACT_KEY: True}) == "warn"


def test_a_policy_write_refuses_an_unknown_contract_mode():
    for mode in ("off", "warn", "require"):
        ok = validated_gate_policy({SUBMISSION_CONTRACT_KEY: mode})
        assert ok[SUBMISSION_CONTRACT_KEY] == mode
    with pytest.raises(ValueError, match="submission_contract"):
        validated_gate_policy({SUBMISSION_CONTRACT_KEY: "on"})


# --------------------------------------------------------------------------
# AC-1: require отказывает голой сдаче до записи поколения
# --------------------------------------------------------------------------


async def test_require_refuses_a_bare_submission(client: AsyncClient, db):
    """SID-9 (#1384): model="", summary="", мутаций нет — отказ со всем списком."""
    task_id = await _running_task(db, "req-bare", "require")
    before = dict(await repo.get_task(db, task_id))
    feed_before = len(await _feed(db, task_id))

    with patch(
        "hub.services.review_dispatch.maybe_dispatch_review", new=AsyncMock()
    ) as dispatch:
        resp = await client.post(
            f"/api/tasks/{task_id}/submit-review",
            json={"model": "", "summary": ""},
        )

    assert resp.status_code == 422, resp.text
    detail = _detail(resp)
    assert detail["reason"] == "submission_contract_violated"
    violations = detail["violations"]
    text = " ".join(violations)
    assert "model" in text
    assert "summary" in text
    assert "AC-1" in text and "AC-2" in text
    assert "AC-3" not in text, "ручной AC мутации не требует"
    # Формат поля назван прямо в подсказке — исполнитель без MCP читает REST.
    assert '"failed_test"' in detail["hint"] and '"ac"' in detail["hint"]
    assert "mutations" in detail["hint"]

    after = dict(await repo.get_task(db, task_id))
    assert after["status"] == before["status"] == "running"
    assert after["submission_generation"] == before["submission_generation"]
    assert await repo.get_submission(db, task_id, 1) is None
    dispatch.assert_not_awaited()
    assert len(await _feed(db, task_id)) == feed_before


# --------------------------------------------------------------------------
# AC-2: каждый AC с тестом — своя мутация, failed_test = test_ref этого AC
# --------------------------------------------------------------------------


async def test_every_test_ac_needs_its_own_mutation(client: AsyncClient, db):
    task_id = await _running_task(db, "req-cover", "require")
    base = {"model": "claude-opus-5-5", "summary": "сделано"}

    only_first = await client.post(
        f"/api/tasks/{task_id}/submit-review",
        json={**base, "mutations": [_FULL_MUTATIONS[0]]},
    )
    assert only_first.status_code == 422, only_first.text
    violations = _detail(only_first)["violations"]
    uncovered = [v for v in violations if "AC-2" in v]
    assert uncovered and "не покрыт" in uncovered[0], violations
    assert not any("AC-1" in v for v in violations), violations
    assert not any("model" in v or "summary" in v for v in violations), violations

    # failed_test записи AC-2 — тест ДРУГОГО AC: совпадение с любым test_ref
    # задачи не засчитывается, только с test_ref этого AC.
    wrong_test = [
        _FULL_MUTATIONS[0],
        {"ac": "AC-2", "mutation": "убрана ветка", "failed_test": _REF_1},
    ]
    resp = await client.post(
        f"/api/tasks/{task_id}/submit-review", json={**base, "mutations": wrong_test}
    )
    assert resp.status_code == 422, resp.text
    violations = _detail(resp)["violations"]
    mismatch = [v for v in violations if "AC-2" in v]
    assert mismatch and _REF_2 in mismatch[0] and _REF_1 in mismatch[0], violations
    assert "не покрыт" not in mismatch[0], "запись есть — это несовпадение, не дыра"

    # ac, которого у задачи нет, называется отдельно.
    ghost = [*_FULL_MUTATIONS, {"ac": "AC-9", "mutation": "x", "failed_test": "t"}]
    resp = await client.post(
        f"/api/tasks/{task_id}/submit-review", json={**base, "mutations": ghost}
    )
    assert resp.status_code == 422, resp.text
    assert any("AC-9" in v for v in _detail(resp)["violations"])

    row = dict(await repo.get_task(db, task_id))
    assert row["status"] == "running"
    assert row["submission_generation"] == 0


# --------------------------------------------------------------------------
# AC-3: warn принимает и пишет ровно одну запись с перечнем
# --------------------------------------------------------------------------


async def test_warn_accepts_and_names_violations_once(client: AsyncClient, db):
    task_id = await _running_task(db, "warn-bare", "warn")

    with patch(
        "hub.services.review_dispatch.maybe_dispatch_review", new=AsyncMock()
    ) as dispatch:
        resp = await client.post(
            f"/api/tasks/{task_id}/submit-review",
            json={"model": "", "summary": ""},
        )

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "review"
    assert resp.json()["submission_generation"] == 1
    dispatch.assert_awaited_once()

    marked = [
        u for u in await _feed(db, task_id) if _VIOLATION_MARK in (u["content"] or "")
    ]
    assert len(marked) == 1, marked
    text = marked[0]["content"]
    assert "model" in text and "summary" in text
    assert "AC-1" in text and "AC-2" in text


# --------------------------------------------------------------------------
# AC-4: полная сдача проходит на каждой поверхности; off — без изменений
# --------------------------------------------------------------------------


def _cli_body(task_id: int) -> dict[str, Any]:
    """Тело, которое CLI отправил бы в REST — перехвачено у _api."""
    api = MagicMock(return_value={"id": task_id, "status": "review"})
    argv = [
        "oc-hub",
        "submit-review",
        str(task_id),
        "--summary",
        "сделано через CLI",
        "--model",
        "claude-opus-5-5",
        "--mutations",
        json.dumps(_FULL_MUTATIONS),
    ]
    with patch.object(sys, "argv", argv), patch.object(cli, "_api", api):
        assert cli.main() == 0
    method, path, body = api.call_args.args
    assert (method, path) == ("POST", f"/api/tasks/{task_id}/submit-review")
    return body


async def _mcp_body(task_id: int) -> dict[str, Any]:
    """Тело, которое MCP-инструмент отправил бы в REST."""
    from hub.mcp_server import hub_submit_for_review

    with (
        patch("hub.mcp_server._api_get", new=AsyncMock(return_value={})),
        patch(
            "hub.mcp_server._api_post",
            new=AsyncMock(return_value={"id": task_id, "status": "review"}),
        ) as post,
    ):
        await hub_submit_for_review(
            task_id,
            summary="сделано через MCP",
            model="claude-opus-5-5",
            mutations=_FULL_MUTATIONS,
        )
    path, body = post.call_args.args
    assert path == f"/api/tasks/{task_id}/submit-review"
    return body


async def test_a_complete_submission_passes_on_every_surface(client: AsyncClient, db):
    rest = {
        "model": "claude-opus-5-5",
        "summary": "сделано через REST",
        "mutations": _FULL_MUTATIONS,
    }
    for surface in ("rest", "cli", "mcp"):
        task_id = await _running_task(db, f"req-{surface}", "require")
        if surface == "rest":
            sent = rest
        elif surface == "cli":
            sent = _cli_body(task_id)
        else:
            sent = await _mcp_body(task_id)
        assert sent["mutations"] == _FULL_MUTATIONS, surface
        assert sent["model"] == "claude-opus-5-5", surface
        resp = await client.post(f"/api/tasks/{task_id}/submit-review", json=sent)
        assert resp.status_code == 200, (surface, resp.text)
        assert resp.json()["status"] == "review", surface

        feed = await _feed(db, task_id)
        assert not any(_VIOLATION_MARK in (u["content"] or "") for u in feed)
        submission = [
            u["content"]
            for u in feed
            if (u["content"] or "").startswith(repo.SUBMISSION_UPDATE_PREFIX)
        ]
        assert submission, surface
        for m in _FULL_MUTATIONS:
            assert m["mutation"] in submission[-1], (surface, submission[-1])
            assert m["failed_test"] in submission[-1], (surface, submission[-1])

        brief = await client.get(f"/api/tasks/{task_id}/review-brief")
        assert brief.status_code == 200, brief.text
        summary = brief.json()["latest_submission_summary"]
        for m in _FULL_MUTATIONS:
            assert m["mutation"] in summary and m["failed_test"] in summary, surface

    # off: голая сдача SID-9 проходит как раньше, без записи о контракте.
    for slug, contract in (("off-explicit", "off"), ("off-absent", None)):
        task_id = await _running_task(db, slug, contract)
        resp = await client.post(
            f"/api/tasks/{task_id}/submit-review",
            json={"model": "", "summary": ""},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "review"
        feed = await _feed(db, task_id)
        assert not any(_VIOLATION_MARK in (u["content"] or "") for u in feed)


def test_the_cli_refuses_broken_mutations_before_the_network(capsys):
    """Битый --mutations — отказ CLI до запроса, а не тихая сдача без них."""
    api = MagicMock()
    for raw in ("{не json", '{"ac": "AC-1"}'):
        argv = ["oc-hub", "submit-review", "7", "--mutations", raw]
        with patch.object(sys, "argv", argv), patch.object(cli, "_api", api):
            assert cli.main() == 2
        assert cli.MUTATIONS_JSON_ERROR in capsys.readouterr().err
    api.assert_not_called()
