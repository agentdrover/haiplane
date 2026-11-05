"""Архив дельты в файл спецификации на ветке задачи (#2220).

Автор правит ``specs/<capability>/spec.md`` на ветке задачи до сдачи. На сдаче
хаб читает diff этого файла на закреплённом head_sha и сверяет блоки со
снимком #2218: id и операции те же, sha256 блока равен хешу записи. Свой
коммит в файл хаб не добавляет. Тесты идут по настоящему git (bare origin и
клон): сверка читает объекты git, а не подменённый плагин.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient, Response

from hub import poller
from hub import repository as repo
from hub.integrations.git_ops import GitOpsIntegration
from hub.integrations.protocols import CIProbeOutcome, CIProbeResult
from hub.integrations.registry import plugins
from hub.mcp_server import HubApiError, _parse_api_error, hub_submit_for_review
from hub.models import SpecRequirement, SpecScenario
from hub.services import orchestration, spec_debt, spec_index
from hub.services.spec_format import parse_spec_file, requirement_revision
from tests.test_behavior_spec_index import AUTH_SPEC, BRANCH, Origin, _git
from tests.test_behavior_spec_snapshot import (
    HEAD_FILE,
    _cli_main,
    _detail,
    _pair,
    _pin,
    _record,
    _snapshot_row,
    _submit,
    _task,
)

BASE = parse_spec_file(AUTH_SPEC)
REF = "tests/test_behavior_spec_archive.py::test_submit_accepts_matching_spec_diff"
OTHER = "The hub says something the snapshot never promised."


def _req(ident: str, record: dict[str, Any]) -> SpecRequirement:
    """Блок файла по записи дельты: тот же id, текст и сценарии."""
    return SpecRequirement(
        id=ident,
        obligation=record["obligation"],
        scenarios=[SpecScenario(**item) for item in record["scenarios"]],
    )


def _render(reqs: list[SpecRequirement]) -> str:
    lines = ["# Auth", ""]
    for req in reqs:
        lines += [f"## {req.id}", f"Obligation: {req.obligation}", ""]
        for sc in req.scenarios:
            lines += [
                "### Scenario",
                f"- Given: {sc.given}",
                f"- When: {sc.when}",
                f"- Then: {sc.then}",
                f"- Verifiable by: {sc.verifiable_by}",
                f"- Test ref: {sc.test_ref}",
                "",
            ]
    return "\n".join(lines)


def _file(
    replace: dict[str, SpecRequirement] | None = None,
    *,
    drop: tuple[str, ...] = (),
    extra: tuple[SpecRequirement, ...] = (),
) -> str:
    """Файл на ветке: база с заменами, удалениями и добавленными блоками."""
    kept = [(replace or {}).get(req.id, req) for req in BASE if req.id not in drop]
    return _render([*kept, *extra])


def _mod(ident: str, revision: str, obligation: str, ref: str = REF) -> dict[str, Any]:
    return _record("modified", ident, revision=revision, obligation=obligation, ref=ref)


def _commit(origin: Origin, branch: str, text: str | None, *, new: bool) -> str:
    """Коммит автора на ветке задачи, push в origin; ``None`` — пустой коммит."""
    clone = origin.clone
    if new:
        _git(clone, "checkout", "-q", "-B", branch, f"origin/{BRANCH}")
    else:
        _git(clone, "checkout", "-q", branch)
    if text is not None:
        (clone / HEAD_FILE).write_text(text, encoding="utf-8")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-q", "--allow-empty", "-m", "task work")
    _git(clone, "push", "-q", "-f", "origin", f"{branch}:{branch}")
    sha = _git(clone, "rev-parse", "HEAD")
    _git(clone, "checkout", "-q", BRANCH)
    return sha


def _merge_outside_hub(origin: Origin, branch: str) -> None:
    """Слияние руками: ветка задачи уходит в базовую мимо гейта."""
    _git(origin.clone, "merge", "-q", "--ff-only", branch)
    _git(origin.clone, "push", "-q", "origin", f"{BRANCH}:{BRANCH}")


def _tip(origin: Origin, ref: str) -> str:
    return _git(origin.bare, "rev-parse", f"refs/heads/{ref}")


@pytest.fixture
def origin(tmp_path) -> Origin:
    return Origin(tmp_path / "git")


@pytest.fixture
async def project_id(db, origin: Origin, monkeypatch) -> int:
    monkeypatch.setattr(plugins, "git_ops", GitOpsIntegration())
    pid = await repo.create_project(
        db,
        slug="archive-proj",
        name="Archive",
        workspace_path=str(origin.clone),
        default_branch=BRANCH,
    )
    await db.commit()
    return pid


class Env:
    """Проект с индексом и ветками задач; одна сдача — один вызов ``attempt``."""

    def __init__(self, client, db, origin: Origin, project_id: int, monkeypatch):
        self.client = client
        self.db = db
        self.origin = origin
        self.project_id = project_id
        self.monkeypatch = monkeypatch

    async def index(self) -> dict[str, str]:
        """Индекс с базовой ветки; ревизии блоков по id."""
        self.origin.publish({HEAD_FILE: AUTH_SPEC})
        indexed = await spec_index.index_project(self.db, self.project_id)
        assert indexed.state == "indexed", indexed.detail
        return {req.id: requirement_revision(req) for req in BASE}

    async def attempt(
        self, delta: Any, text: str | None, *, submit: bool = True
    ) -> tuple[int, str, Response | None]:
        """Задача с дельтой, коммит автора с ``text``, закреплённый head, сдача."""
        task_id = await _task(
            self.client, self.db, self.project_id, behavior_delta=delta
        )
        await _pair(self.client, task_id)
        branch = (await self.client.get(f"/api/tasks/{task_id}")).json()["branch"]
        sha = _commit(self.origin, branch, text, new=True)
        _pin(self.monkeypatch, sha)
        resp = await _submit(self.client, task_id) if submit else None
        return task_id, sha, resp

    async def approve(self, task_id: int, pr_number: int = 77) -> None:
        verdict = await self.client.post(
            f"/api/tasks/{task_id}/review-verdict",
            json={"verdict": "approved", "agent": "reviewer"},
        )
        assert verdict.status_code == 200, verdict.text
        await repo.update_task(self.db, task_id, pr_number=pr_number)
        await self.db.commit()

    async def card(self, task_id: int) -> dict[str, Any]:
        return (await self.client.get(f"/api/tasks/{task_id}")).json()

    async def refused_cleanly(
        self, resp: Response | None, task_id: int
    ) -> dict[str, Any]:
        """422 сдачи: поколения, снимка и перехода нет; возвращает тело отказа."""
        assert resp is not None and resp.status_code == 422, resp and resp.text
        card = await self.card(task_id)
        assert card["status"] == "running"
        assert card["submission_generation"] == 0
        assert await _snapshot_row(self.db, task_id, 1) is None
        return _detail(resp)


@pytest.fixture
def env(client: AsyncClient, db, origin: Origin, project_id: int, monkeypatch) -> Env:
    return Env(client, db, origin, project_id, monkeypatch)


async def test_submit_accepts_matching_spec_diff(env: Env):
    """AC-1: блок в diff равен хешу записи — сдача принята для этого head_sha, хаб не коммитит."""
    revs = await env.index()
    develop_tip = _tip(env.origin, BRANCH)
    develop_file = env.origin.blob(HEAD_FILE)
    record = _mod("REQ-1", revs["REQ-1"], "The hub records the archived delta.")
    text = _file({"REQ-1": _req("REQ-1", record)})
    task_id, sha, resp = await env.attempt([record], text)
    assert resp is not None and resp.status_code == 200, resp and resp.text

    frozen = await _snapshot_row(env.db, task_id, 1)
    assert frozen is not None and frozen["head_sha"] == sha
    assert frozen["records"][0]["content_hash"] == requirement_revision(
        _req("REQ-1", record)
    )
    assert frozen["spec_check"] == {
        "state": "matched",
        "head_sha": sha,
        "base_sha": develop_tip,
    }
    brief = (await env.client.get(f"/api/tasks/{task_id}/review-brief")).json()
    assert brief["behavior_snapshot"]["spec_check"]["head_sha"] == sha

    card = await env.card(task_id)
    branch = card["branch"]
    assert _tip(env.origin, branch) == sha, "хаб не добавил свой коммит в ветку"
    assert _git(env.origin.bare, "rev-list", "--count", f"{BRANCH}..{branch}") == "1"
    assert _tip(env.origin, BRANCH) == develop_tip
    assert env.origin.blob(HEAD_FILE) == develop_file


async def test_submit_refuses_spec_diff_mismatch(env: Env):
    """AC-2: лишний id в diff и пропущенный id называются в отказе."""
    revs = await env.index()
    first = _mod("REQ-1", revs["REQ-1"], "The hub records the archived delta.")
    extra_text = _file(
        {
            "REQ-1": _req("REQ-1", first),
            "REQ-2": _req("REQ-2", _mod("REQ-2", revs["REQ-2"], OTHER)),
        }
    )
    task_id, _sha, resp = await env.attempt([first], extra_text)
    detail = await env.refused_cleanly(resp, task_id)
    assert detail["reason"] == "spec_diff_mismatch"
    assert detail["ids"] == ["REQ-2"]
    assert "REQ-2" in detail["message"] and "REQ-1" not in detail["ids"]

    second = _mod("REQ-2", revs["REQ-2"], "The hub locks the account once.")
    promised = [first, second]
    task_id, _sha, resp = await env.attempt(
        promised, _file({"REQ-1": _req("REQ-1", first)})
    )
    detail = await env.refused_cleanly(resp, task_id)
    assert detail["reason"] == "spec_diff_mismatch"
    assert detail["ids"] == ["REQ-2"]


async def test_no_delta_refuses_spec_file_edit(env: Env):
    """AC-3: «дельты нет» и правка файла specs/ — отказ, путь назван."""
    revs = await env.index()
    edited = _file({"REQ-1": _req("REQ-1", _mod("REQ-1", revs["REQ-1"], OTHER))})
    task_id, _sha, resp = await env.attempt("no_delta", edited)
    detail = await env.refused_cleanly(resp, task_id)
    assert detail["reason"] == "spec_edit_without_delta"
    assert detail["paths"] == [HEAD_FILE]
    assert HEAD_FILE in detail["message"]

    clean_id, clean_sha, clean = await env.attempt("no_delta", None)
    assert clean is not None and clean.status_code == 200, clean and clean.text
    frozen = await _snapshot_row(env.db, clean_id, 1)
    assert frozen is not None and frozen["spec_check"]["state"] == "matched"
    assert frozen["spec_check"]["head_sha"] == clean_sha


async def test_rejection_does_not_change_default_branch_spec(env: Env):
    """AC-4: отклонённая сдача не меняет файл на базовой ветке и ветку автора."""
    revs = await env.index()
    develop_tip = _tip(env.origin, BRANCH)
    develop_file = env.origin.blob(HEAD_FILE)
    record = _mod("REQ-1", revs["REQ-1"], "The hub records the archived delta.")
    task_id, sha, resp = await env.attempt(
        [record], _file({"REQ-1": _req("REQ-1", record)})
    )
    assert resp is not None and resp.status_code == 200, resp and resp.text
    frozen = await _snapshot_row(env.db, task_id, 1)
    assert frozen is not None and frozen["spec_check"]["state"] == "matched"

    rejected = await env.client.post(
        f"/api/tasks/{task_id}/review-verdict",
        json={
            "verdict": "changes_requested",
            "agent": "reviewer",
            "comments": "The scenario does not cover the refusal.",
        },
    )
    assert rejected.status_code == 200, rejected.text
    assert (await env.card(task_id))["status"] == "running"

    assert _tip(env.origin, BRANCH) == develop_tip
    assert env.origin.blob(HEAD_FILE) == develop_file
    assert _tip(env.origin, (await env.card(task_id))["branch"]) == sha
    assert await _snapshot_row(env.db, task_id, 1) == frozen


async def test_submit_refuses_block_hash_mismatch(env: Env):
    """AC-5: id и операция те же, текст блока другой — отказ называет id."""
    revs = await env.index()
    record = _mod("REQ-1", revs["REQ-1"], "The hub records the archived delta.")
    other_obligation = _mod("REQ-1", revs["REQ-1"], OTHER)
    task_id, _sha, resp = await env.attempt(
        [record], _file({"REQ-1": _req("REQ-1", other_obligation)})
    )
    detail = await env.refused_cleanly(resp, task_id)
    assert detail["reason"] == "spec_block_hash_mismatch"
    assert detail["ids"] == ["REQ-1"]
    assert "REQ-1" in detail["message"]

    other_scenario = _mod("REQ-1", revs["REQ-1"], record["obligation"])
    other_scenario["scenarios"][0]["then"] = "the hub answers something else"
    task_id, _sha, resp = await env.attempt(
        [record], _file({"REQ-1": _req("REQ-1", other_scenario)})
    )
    detail = await env.refused_cleanly(resp, task_id)
    assert detail["reason"] == "spec_block_hash_mismatch"
    assert detail["ids"] == ["REQ-1"]


async def test_later_commit_is_not_covered_by_old_check(env: Env):
    """AC-6: сверка S1 не покрывает S2; слияние ждёт новую сдачу."""
    revs = await env.index()
    record = _mod("REQ-1", revs["REQ-1"], "The hub records the archived delta.")
    good = _file({"REQ-1": _req("REQ-1", record)})
    task_id, s1, resp = await env.attempt([record], good)
    assert resp is not None and resp.status_code == 200, resp and resp.text
    await env.approve(task_id)
    branch = (await env.card(task_id))["branch"]

    s2 = _commit(
        env.origin,
        branch,
        _file({"REQ-1": _req("REQ-1", _mod("REQ-1", revs["REQ-1"], OTHER))}),
        new=False,
    )
    _pin(env.monkeypatch, s2)
    task = dict(await repo.get_task(env.db, task_id))
    refusal, _note = await orchestration._approved_code_check(env.db, task, 77)
    assert refusal.startswith("stale_approval"), refusal
    brief = (await env.client.get(f"/api/tasks/{task_id}/review-brief")).json()
    check = brief["behavior_snapshot"]["spec_check"]
    assert check["head_sha"] == s1 and check["head_sha"] != s2

    refused = await _submit(env.client, task_id)
    assert refused.status_code == 422, refused.text
    assert _detail(refused)["reason"] == "spec_block_hash_mismatch"
    assert (await env.card(task_id))["submission_generation"] == 1
    refusal, _note = await orchestration._approved_code_check(env.db, task, 77)
    assert refusal.startswith("stale_approval"), refusal

    s3 = _commit(env.origin, branch, good, new=False)
    _pin(env.monkeypatch, s3)
    again = await _submit(env.client, task_id)
    assert again.status_code == 200, again.text
    assert again.json()["submission_generation"] == 2
    second = await _snapshot_row(env.db, task_id, 2)
    assert second is not None and second["spec_check"]["head_sha"] == s3
    assert (await _snapshot_row(env.db, task_id, 1))["spec_check"]["head_sha"] == s1


async def test_removed_matches_absence_not_a_new_block(env: Env):
    """AC-7: removed — id нет на head_sha, хеш равен блоку базы; id на месте — отказ."""
    revs = await env.index()
    gone = _record(
        "removed", "REQ-2", revision=revs["REQ-2"], obligation="gone", ref=REF
    )
    task_id, sha, resp = await env.attempt([gone], _file(drop=("REQ-2",)))
    assert resp is not None and resp.status_code == 200, resp and resp.text
    frozen = await _snapshot_row(env.db, task_id, 1)
    assert frozen is not None
    assert frozen["records"][0]["content_hash"] == revs["REQ-2"]
    assert frozen["spec_check"]["state"] == "matched"
    assert frozen["spec_check"]["head_sha"] == sha

    still = {"REQ-2": _req("REQ-2", _mod("REQ-2", revs["REQ-2"], OTHER))}
    task_id, _sha, resp = await env.attempt([gone], _file(still))
    detail = await env.refused_cleanly(resp, task_id)
    assert detail["reason"] == "spec_removed_id_present"
    assert detail["ids"] == ["REQ-2"]

    task_id, _sha, resp = await env.attempt([gone], None)
    detail = await env.refused_cleanly(resp, task_id)
    assert detail["reason"] == "spec_removed_id_present"
    assert detail["ids"] == ["REQ-2"]


async def test_renamed_hashes_the_new_id(env: Env):
    """AC-8: старого id нет, блок new_requirement_id хешируется как в снимке."""
    revs = await env.index()
    renamed = _record(
        "renamed",
        "REQ-2",
        revision=revs["REQ-2"],
        new_requirement_id="REQ-5",
        obligation="The hub locks the account after three failed logins.",
        ref=REF,
    )
    block = _req("REQ-5", renamed)
    task_id, sha, resp = await env.attempt(
        [renamed], _file(drop=("REQ-2",), extra=(block,))
    )
    assert resp is not None and resp.status_code == 200, resp and resp.text
    frozen = await _snapshot_row(env.db, task_id, 1)
    assert frozen is not None
    assert frozen["records"][0]["content_hash"] == requirement_revision(block)
    assert frozen["records"][0]["content_hash"] != requirement_revision(
        _req("REQ-2", renamed)
    )
    assert frozen["spec_check"]["head_sha"] == sha

    old_id_kept = {"REQ-2": _req("REQ-2", renamed)}
    task_id, _sha, resp = await env.attempt([renamed], _file(old_id_kept))
    detail = await env.refused_cleanly(resp, task_id)
    assert detail["reason"] == "spec_removed_id_present"
    assert detail["ids"] == ["REQ-2"]

    other = _req("REQ-5", _mod("REQ-5", "", OTHER))
    task_id, _sha, resp = await env.attempt(
        [renamed], _file(drop=("REQ-2",), extra=(other,))
    )
    detail = await env.refused_cleanly(resp, task_id)
    assert detail["reason"] == "spec_block_hash_mismatch"
    assert detail["ids"] == ["REQ-5"]


def _forge_says_merged(env: Env, merged_sha: str) -> None:
    """Форж говорит MERGED на ``merged_sha``, следа гейта у PR нет."""

    async def state(_pr, **_kw) -> str:
        return "merged"

    async def head(_pr, **_kw) -> str:
        return merged_sha

    async def merge_commit(_pr, **_kw) -> str:
        return _tip(env.origin, BRANCH)

    for name, fn in (
        ("pr_state", state),
        ("pr_head_sha", head),
        ("merge_commit_sha", merge_commit),
    ):
        env.monkeypatch.setattr(plugins.git_ops, name, fn)


async def _merged_outside(env: Env, task_id: int, merged_sha: str) -> tuple[bool, str]:
    """Хаб натыкается на PR, слитый не им: форж говорит MERGED, следа гейта нет."""
    ctx = {"repo": str(env.origin.clone), "gh_repo": "", "forge": ""}
    _forge_says_merged(env, merged_sha)
    task = dict(await repo.get_task(env.db, task_id))
    return await orchestration._refused_merge_outcome(env.db, task, ctx, "")


async def test_outside_merge_records_debt_and_does_not_count_as_archive(env: Env):
    """AC-9: слитый чужой SHA или несовпавшие блоки — долг по id, индекс их не берёт."""
    revs = await env.index()
    clean = _mod("REQ-2", revs["REQ-2"], "The hub locks the account once.")
    clean_id, clean_sha, resp = await env.attempt(
        [clean], _file({"REQ-2": _req("REQ-2", clean)})
    )
    assert resp is not None and resp.status_code == 200, resp and resp.text
    await env.approve(clean_id)
    _merge_outside_hub(env.origin, (await env.card(clean_id))["branch"])
    merged, detail = await _merged_outside(env, clean_id, clean_sha)
    assert merged is False and "merged_outside_gate" in detail
    assert await spec_debt.open_debts(env.db, env.project_id) == []
    indexed = await spec_index.index_project(env.db, env.project_id)
    assert indexed.state == "indexed", indexed.detail
    taken = {
        r.id: r.revision
        for r in await spec_index.indexed_requirements(env.db, env.project_id)
    }
    assert taken["REQ-2"] == requirement_revision(_req("REQ-2", clean))

    record = _mod("REQ-1", revs["REQ-1"], "The hub records the archived delta.")
    task_id, s1, resp = await env.attempt(
        [record], _file({"REQ-1": _req("REQ-1", record), "REQ-2": _req("REQ-2", clean)})
    )
    assert resp is not None and resp.status_code == 200, resp and resp.text
    await env.approve(task_id)
    branch = (await env.card(task_id))["branch"]
    swapped = _mod("REQ-1", revs["REQ-1"], OTHER)
    outside = _commit(
        env.origin,
        branch,
        _file({"REQ-1": _req("REQ-1", swapped), "REQ-2": _req("REQ-2", clean)}),
        new=False,
    )
    _merge_outside_hub(env.origin, branch)

    merged, detail = await _merged_outside(env, task_id, outside)
    assert merged is False and "merged_outside_gate" in detail
    debts = await spec_debt.open_debts(env.db, env.project_id)
    assert [
        (d["requirement_id"], d["task_id"], d["approved_sha"], d["merged_sha"])
        for d in debts
    ] == [("REQ-1", task_id, s1, outside)]
    updates = (await env.client.get(f"/api/tasks/{task_id}")).json()["updates"]
    assert any("REQ-1" in u["content"] and "долг" in u["content"] for u in updates)

    indexed = await spec_index.index_project(env.db, env.project_id)
    assert indexed.state == "indexed", indexed.detail
    taken = {
        r.id: r.revision
        for r in await spec_index.indexed_requirements(env.db, env.project_id)
    }
    assert taken["REQ-1"] == revs["REQ-1"], (
        "текст слит вне проверенного SHA — индекс не берёт"
    )
    assert taken["REQ-1"] != requirement_revision(_req("REQ-1", swapped))
    assert taken["REQ-2"] == requirement_revision(_req("REQ-2", clean))


async def _approved_then_pushed_and_merged_by_hand(
    env: Env, delta: Any, text: str | None
) -> tuple[int, str, str]:
    """Снимок сверен на S1 и одобрен; автор пушит S2 и сливает PR руками.

    Возвращает ``(task_id, S1, S2)``; ветка origin и форж стоят на S2.
    """
    task_id, s1, resp = await env.attempt(delta, text)
    assert resp is not None and resp.status_code == 200, resp and resp.text
    await env.approve(task_id)
    branch = (await env.card(task_id))["branch"]
    swapped = _req("REQ-1", _mod("REQ-1", "", OTHER))
    outside = _commit(env.origin, branch, _file({"REQ-1": swapped}), new=False)
    _merge_outside_hub(env.origin, branch)
    _forge_says_merged(env, outside)
    return task_id, s1, outside


async def _indexed_revisions(env: Env) -> dict[str, str]:
    indexed = await spec_index.index_project(env.db, env.project_id)
    assert indexed.state == "indexed", indexed.detail
    return {
        r.id: r.revision
        for r in await spec_index.indexed_requirements(env.db, env.project_id)
    }


async def _debts(env: Env) -> list[tuple[str, int, str, str]]:
    return [
        (d["requirement_id"], d["task_id"], d["approved_sha"], d["merged_sha"])
        for d in await spec_debt.open_debts(env.db, env.project_id)
    ]


async def test_pair_gate_records_debt_when_the_tip_moved_and_pr_merged_by_hand(
    env: Env,
):
    """AC-9 на боевом пути пары: сдвиг вершины даёт stale_approval, но долг пишется."""
    revs = await env.index()
    record = _mod("REQ-1", revs["REQ-1"], "The hub records the archived delta.")
    task_id, s1, outside = await _approved_then_pushed_and_merged_by_hand(
        env, [record], _file({"REQ-1": _req("REQ-1", record)})
    )
    _pin(env.monkeypatch, outside)  # хаб наблюдает вершину ветки: она на S2
    task = dict(await repo.get_task(env.db, task_id))

    ok, reason = await orchestration.merge_before_completion(env.db, task)

    assert ok is False and reason.startswith("stale_approval")
    assert await _debts(env) == [("REQ-1", task_id, s1, outside)]
    updates = (await env.card(task_id))["updates"]
    assert sum("долг" in u["content"] for u in updates) == 1
    taken = await _indexed_revisions(env)
    assert taken["REQ-1"] == revs["REQ-1"], (
        "слито вне проверенного SHA — индекс не берёт"
    )


async def test_pair_gate_without_hand_merge_records_no_debt(env: Env):
    """Сдвинутая вершина без слитого PR — просто stale_approval, долга нет."""
    revs = await env.index()
    record = _mod("REQ-1", revs["REQ-1"], "The hub records the archived delta.")
    task_id, _s1, resp = await env.attempt(
        [record], _file({"REQ-1": _req("REQ-1", record)})
    )
    assert resp is not None and resp.status_code == 200, resp and resp.text
    await env.approve(task_id)
    branch = (await env.card(task_id))["branch"]
    moved = _commit(env.origin, branch, None, new=False)

    async def state(_pr, **_kw) -> str:
        return "open"

    env.monkeypatch.setattr(plugins.git_ops, "pr_state", state)
    _pin(env.monkeypatch, moved)
    task = dict(await repo.get_task(env.db, task_id))

    ok, reason = await orchestration.merge_before_completion(env.db, task)

    assert ok is False and reason.startswith("stale_approval")
    assert await _debts(env) == []


async def test_headless_poller_records_debt_when_pr_merged_by_hand(env: Env):
    """AC-9 на боевом пути поллера: форж отказал мержу, PR слит вне хаба — долг есть."""
    revs = await env.index()
    record = _mod("REQ-1", revs["REQ-1"], "The hub records the archived delta.")
    task_id, s1, outside = await _approved_then_pushed_and_merged_by_hand(
        env, [record], _file({"REQ-1": _req("REQ-1", record)})
    )

    async def ci(*_a, **_kw) -> CIProbeResult:
        return CIProbeResult(CIProbeOutcome.passed, "checks_pass")

    async def refused(*_a, **_kw) -> tuple[bool, str]:
        return False, "pull request is already merged"

    env.monkeypatch.setattr(plugins.git_ops, "check_pr_ci", ci)
    env.monkeypatch.setattr(plugins.git_ops, "merge_pr_with_detail", refused)
    task = dict(await repo.get_task(env.db, task_id))
    with patch("hub.poller.services.maybe_destroy_vast", new_callable=AsyncMock):
        await poller._deliver_approved_review(env.db, task)

    assert await _debts(env) == [("REQ-1", task_id, s1, outside)]
    taken = await _indexed_revisions(env)
    assert taken["REQ-1"] == revs["REQ-1"], (
        "слито вне проверенного SHA — индекс не берёт"
    )


async def test_project_wide_debt_keeps_the_previous_index(env: Env):
    """Снимок без id: долг на весь проект, прежний индекс остаётся как был."""
    revs = await env.index()
    task_id, s1, outside = await _approved_then_pushed_and_merged_by_hand(
        env, "no_delta", None
    )
    _pin(env.monkeypatch, outside)
    task = dict(await repo.get_task(env.db, task_id))

    ok, reason = await orchestration.merge_before_completion(env.db, task)

    assert ok is False and reason.startswith("stale_approval")
    assert await _debts(env) == [("", task_id, s1, outside)]
    updates = (await env.card(task_id))["updates"]
    assert any("долг на весь проект" in u["content"] for u in updates)
    taken = await _indexed_revisions(env)
    assert taken == revs, "алерт обещает: индекс блоки этого слияния не принимает"


async def test_spec_refusal_reads_the_same_on_cli_and_mcp(env: Env, capsys):
    """Отказ сверки одинаков в REST, CLI и MCP: причина и id названы везде."""
    revs = await env.index()
    first = _mod("REQ-1", revs["REQ-1"], "The hub records the archived delta.")
    stray = {"REQ-2": _req("REQ-2", _mod("REQ-2", revs["REQ-2"], OTHER))}
    text = _file({"REQ-1": _req("REQ-1", first), **stray})
    task_id, _sha, _none = await env.attempt([first], text, submit=False)

    rest = await _submit(env.client, task_id)
    assert rest.status_code == 422, rest.text
    assert _detail(rest)["reason"] == "spec_diff_mismatch"

    loop = asyncio.get_running_loop()

    def cli_api(method: str, path: str, body: Any = None, **_kwargs: Any) -> Any:
        async def call() -> Any:
            resp = await env.client.request(method, path, json=body)
            if resp.status_code >= 400:
                print(resp.text, file=sys.stderr)
                return None
            return resp.json()

        done = asyncio.run_coroutine_threadsafe(call(), loop).result()
        if done is None:
            raise SystemExit(1)
        return done

    env.monkeypatch.setattr("hub.cli._api", cli_api)
    code = await asyncio.to_thread(_cli_main, ["submit-review", str(task_id)])
    assert code == 1
    shown = capsys.readouterr().err
    assert "spec_diff_mismatch" in shown and "REQ-2" in shown

    async def post(path: str, body: Any = None, timeout: float | None = None) -> Any:
        resp = await env.client.post(path, json=body or {})
        if resp.status_code >= 400:
            raise HubApiError(_parse_api_error(resp, resp.status_code))
        return resp.json()

    env.monkeypatch.setattr("hub.mcp_server._api_post", post)
    refused = str(await hub_submit_for_review(task_id))
    assert "spec_diff_mismatch" in refused and "REQ-2" in refused
