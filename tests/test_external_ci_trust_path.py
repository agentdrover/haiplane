"""Внешний проект получает подтверждаемый путь CI или явное предупреждение (#2027).

Наблюдено 22-23.10 на olymp-helper и my-home: провижининг клал только
``haiplane-ci.yml`` с прямой отправкой ключом записи; такой отчёт хаб никогда не
подтверждал (no_provenance), а стюард ждал verified по 120 минут на каждой сдаче.

Тесты идут через настоящие места: настоящий git с bare origin (AC-1), настоящий
приём отчёта ``accept_ci_run_report`` и настоящие строки ``ci_run_reports`` (AC-2,
AC-3), а не руками собранные строки читателя.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock

import aiosqlite
import yaml
from httpx import AsyncClient

from hub import config
from hub import repository as repo
from hub import services
from hub.integrations.registry import plugins
from hub.services import ci_contract, ci_trust
from hub.services.ci_report import accept_ci_run_report
from hub.services.steward_ci_wait import ci_wait_view
from hub.services.steward_review_gate import review_wait_gate
from tests.ci_trust_support import SAT_CONTRACT, install_fake_github
from tests.test_steward_ci_wait import SHA, _ready_task
from tests.test_steward_dispatch import _project, _submitted_task

PRODUCER = "haiplane-ci.yml"
SENDER = "haiplane-ci-report.yml"
WORKFLOWS = ".github/workflows"


# --------------------------------------------------------------------------
# AC-1 - provisioning lays the pair, the sender into the default branch
# --------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


def _repo_pair(tmp_path: Path) -> tuple[Path, Path]:
    """Origin whose default branch is main, a clone standing on develop."""
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(origin)],
        check=True,
        capture_output=True,
    )
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.name", "Tester")
    _git(work, "config", "user.email", "tester@example.com")
    (work / "README.md").write_text("x\n", encoding="utf-8")
    _git(work, "add", "README.md")
    _git(work, "commit", "-m", "initial")
    _git(work, "remote", "add", "origin", str(origin))
    _git(work, "push", "-u", "origin", "main")
    _git(work, "checkout", "-b", "develop")
    _git(work, "push", "-u", "origin", "develop")
    return work, origin


def _tree(origin: Path, branch: str) -> list[str]:
    out = _git(origin, "ls-tree", "-r", "--name-only", branch, "--", WORKFLOWS)
    return sorted(Path(p).name for p in out.splitlines() if p)


def _show(origin: Path, branch: str, name: str) -> dict:
    return yaml.safe_load(_git(origin, "show", f"{branch}:{WORKFLOWS}/{name}"))


async def test_provisioning_lays_the_verifiable_pair(
    db: aiosqlite.Connection, tmp_path: Path, monkeypatch
):
    """AC-1: producer in develop, sender in main; the detail names both and the owner's list."""
    work, origin = _repo_pair(tmp_path)
    project_id = await repo.create_project(db, slug="ext", name="Ext")
    await repo.update_project(
        db,
        project_id,
        repo="agentdrover/ext",
        workspace_path=str(work),
        default_branch="develop",
        default_branch_policy=json.dumps({"release_base": "main"}),
    )
    await db.commit()
    monkeypatch.setattr(
        plugins.git_ops, "clone_repo", AsyncMock(return_value=(True, "verified"))
    )

    result = await services.provision_project(db, project_id)

    assert result["provision_status"] == "ok", result["provision_detail"]
    assert _tree(origin, "develop") == [PRODUCER, "haiplane-stale.yml"]
    assert _tree(origin, "main") == [SENDER]
    producer = _show(origin, "develop", PRODUCER)
    sender = _show(origin, "main", SENDER)
    assert producer["name"] == "Haiplane CI"
    assert sender[True]["workflow_run"]["workflows"] == ["Haiplane CI"]
    send_step = sender["jobs"]["report"]["steps"][-1]["with"]
    assert send_step["workflow-path"] == f"{WORKFLOWS}/{PRODUCER}"
    assert sender["jobs"]["report"]["environment"] == "hub-report"
    # The clone is not moved off its branch and stays clean.
    assert _git(work, "rev-parse", "--abbrev-ref", "HEAD") == "develop"
    assert _git(work, "status", "--porcelain") == ""

    stored = (await repo.get_project(db, project_id))["provision_detail"]
    for text in (
        PRODUCER,
        SENDER,
        "ожидает настройки владельцем",
        "ci_reader",
        "ci_runner",
        "hub-report",
        "ci_contract",
    ):
        assert text in stored, (text, stored)

    # A second provision adds nothing, in either branch.
    heads = (_git(origin, "rev-parse", "develop"), _git(origin, "rev-parse", "main"))
    await services.provision_project(db, project_id)
    assert (
        _git(origin, "rev-parse", "develop"),
        _git(origin, "rev-parse", "main"),
    ) == heads


async def test_the_pair_is_not_laid_for_a_repository_with_its_own_ci(
    db: aiosqlite.Connection, tmp_path: Path, monkeypatch
):
    """The default branch carries somebody's workflow: the hub adds nothing to it."""
    work, origin = _repo_pair(tmp_path)
    _git(work, "checkout", "main")
    (work / WORKFLOWS).mkdir(parents=True)
    (work / WORKFLOWS / "theirs.yml").write_text("name: theirs\non: [push]\n", "utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "their CI")
    _git(work, "push", "origin", "main")
    _git(work, "checkout", "develop")
    project_id = await repo.create_project(db, slug="ext2", name="Ext2")
    await repo.update_project(
        db,
        project_id,
        repo="agentdrover/ext2",
        workspace_path=str(work),
        default_branch="develop",
        default_branch_policy=json.dumps({"release_base": "main"}),
    )
    await db.commit()
    monkeypatch.setattr(
        plugins.git_ops, "clone_repo", AsyncMock(return_value=(True, "verified"))
    )

    result = await services.provision_project(db, project_id)

    assert _tree(origin, "main") == ["theirs.yml"]
    assert "ожидает настройки владельцем" not in result["provision_detail"]


# --------------------------------------------------------------------------
# AC-2 - a project whose reports are never confirmed is named
# --------------------------------------------------------------------------


async def _report_without_provenance(db, task_id: int, sha: str) -> None:
    await accept_ci_run_report(
        db,
        task_id,
        head_sha=sha,
        ac_results={},
        checks={"tests": "pass"},
        validation_status="pass",
        reported_by="hub-ci-report",
    )


async def _submission(db, task_id: int, sha: str, generation: int = 1) -> None:
    await db.execute(
        "INSERT INTO submissions (task_id, generation, sha, base_branch) "
        "VALUES (?, ?, ?, 'develop')",
        (task_id, generation, sha),
    )
    await db.commit()


async def test_a_project_without_verified_ci_is_named(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """AC-2: reports exist and none is verified: the warning with the cause."""
    install_fake_github(monkeypatch)
    monkeypatch.setattr(config, "REPO_NAME", "own/rep")
    proj = await _project(db, "ext-warn", steward=True)
    await repo.update_project(
        db, proj, gate_policy=json.dumps({"ci_contract": SAT_CONTRACT})
    )
    task_id = await _submitted_task(db, proj)
    await _submission(db, task_id, SHA)
    await _report_without_provenance(db, task_id, SHA)

    body = (await client.get("/api/projects/ext-warn/effective-policy")).json()

    block = body["ci_trust"]
    assert block["warning"] is True
    assert block["state"] == "unverified"
    assert "no_provenance" in block["message"]
    assert "docs/satellite-ci-report.md" in block["message"]
    page = (await client.get("/projects")).text
    assert "no_provenance" in page, "the project card names the cause too"

    # No reports at all is another sentence, and not a malfunction.
    quiet = await _project(db, "ext-quiet", steward=True)
    quiet_task = await _submitted_task(db, quiet)
    await _submission(db, quiet_task, SHA)
    other = (await client.get("/api/projects/ext-quiet/effective-policy")).json()
    assert other["ci_trust"]["state"] == "no_reports"
    assert other["ci_trust"]["warning"] is False
    assert other["ci_trust"]["message"] != block["message"]
    assert "не подтвержд" not in other["ci_trust"]["message"]

    # One verified report on the pinned commit clears the warning.
    await db.execute(
        "UPDATE ci_run_reports SET trust='verified', trust_reason='' WHERE task_id=?",
        (task_id,),
    )
    await db.commit()
    await db.execute(
        "UPDATE ci_run_reports SET contract_fp=? WHERE task_id=?",
        (await ci_contract.fingerprint_for_task(db, task_id), task_id),
    )
    await db.commit()
    fixed = (await client.get("/api/projects/ext-warn/effective-policy")).json()
    assert fixed["ci_trust"]["warning"] is False
    assert fixed["ci_trust"]["state"] == "verified"


# --------------------------------------------------------------------------
# AC-3 - the steward does not wait for a report that is final no_provenance
# --------------------------------------------------------------------------


async def test_steward_does_not_wait_for_a_report_that_cannot_be_verified(
    db: aiosqlite.Connection, monkeypatch
):
    """AC-3: through the real intake; a changed contract is still waited for."""
    install_fake_github(monkeypatch)
    task_id = await _ready_task(db, "ext-wait")
    task = dict(await repo.get_task(db, task_id))

    assert (await ci_wait_view(db, task)).waiting is True, "no report yet: wait"

    await _report_without_provenance(db, task_id, SHA)
    stored = dict(await repo.get_ci_run_report(db, task_id, SHA))
    assert stored["trust_reason"] == ci_trust.NO_PROVENANCE, stored

    view = await ci_wait_view(db, task)
    assert view.waiting is False, view
    assert "отчёт неподтверждаем: no_provenance" in view.state
    gate = await review_wait_gate(db, task)
    assert not gate.blocker
    assert "отчёт неподтверждаем: no_provenance" in gate.ci_note

    # A contract change makes the old verdict stale: the re-check is waited for.
    project = await repo.resolve_project_for_task(db, task_id)
    await repo.update_project(
        db, int(project["id"]), gate_policy=json.dumps({"ci_contract": SAT_CONTRACT})
    )
    await db.commit()
    assert (await ci_wait_view(db, task)).waiting is True, "contract changed: wait"
    await repo.update_project(db, int(project["id"]), gate_policy=json.dumps({}))
    await db.commit()
    assert (await ci_wait_view(db, task)).waiting is False


async def test_a_new_current_attempt_without_a_report_is_still_waited_for(
    db: aiosqlite.Connection, monkeypatch
):
    """The narrowing must not touch a keyed attempt whose report has not come yet."""
    fake = install_fake_github(monkeypatch)
    task_id = await _ready_task(db, "ext-newer")
    task = dict(await repo.get_task(db, task_id))
    sent = fake.attempt(task_id, SHA, run_id="7001", conclusion="failure")
    sent["provenance"]["artifact_digest"] = "sha256:" + "0" * 64
    await accept_ci_run_report(db, task_id, **sent)
    settled = dict(await repo.get_ci_run_report(db, task_id, SHA))
    assert settled["trust"] != "verified", settled
    assert (await ci_wait_view(db, task)).waiting is False, "final, keyed: no wait"

    fake.attempt(task_id, SHA, run_id="9001")
    await ci_trust.refresh_quietly(db, task_id, SHA)

    assert (await ci_wait_view(db, task)).waiting is True


async def test_only_a_final_no_provenance_of_the_projection_is_not_waited_for(
    db: aiosqlite.Connection, monkeypatch
):
    """The narrowing is exactly: final attempt row AND the projection says no_provenance."""
    install_fake_github(monkeypatch)
    task_id = await _ready_task(db, "ext-narrow")
    task = dict(await repo.get_task(db, task_id))
    await _report_without_provenance(db, task_id, SHA)
    assert (await ci_wait_view(db, task)).waiting is False

    await db.execute(
        "UPDATE ci_report_attempts SET trust_final=0 WHERE task_id=?", (task_id,)
    )
    await db.commit()
    assert (await ci_wait_view(db, task)).waiting is True, "not final yet: wait"

    await db.execute(
        "UPDATE ci_report_attempts SET trust_final=1 WHERE task_id=?", (task_id,)
    )
    await db.execute(
        "UPDATE ci_run_reports SET trust_reason='forge_unavailable' WHERE task_id=?",
        (task_id,),
    )
    await db.commit()
    assert (await ci_wait_view(db, task)).waiting is True, "other reason: wait"


async def test_an_old_no_provenance_report_does_not_hide_a_new_attempt(
    db: aiosqlite.Connection, monkeypatch
):
    """A completed GitHub attempt with no report is waited for, unlike a final no_provenance alone."""
    fake = install_fake_github(monkeypatch)
    task_id = await _ready_task(db, "ext-hidden")
    task = dict(await repo.get_task(db, task_id))
    await _report_without_provenance(db, task_id, SHA)
    assert (await ci_wait_view(db, task)).waiting is False

    fake.attempt(task_id, SHA, run_id="9101", status="in_progress")
    await ci_trust.refresh_quietly(db, task_id, SHA)
    assert (await ci_wait_view(db, task)).waiting is True, "in progress: wait"

    fake.runs[("9101", "1")].update(status="completed", conclusion="success")
    await ci_trust.refresh_quietly(db, task_id, SHA)
    assert (await ci_wait_view(db, task)).waiting is True, "completed, no report: wait"

    sent = fake.attempt(task_id, SHA, run_id="9102")
    fake.runs.pop(("9101", "1"))
    await accept_ci_run_report(db, task_id, **sent)
    assert (await ci_wait_view(db, task)).waiting is False, "its report is final"


async def test_a_retry_after_a_partial_seed_finishes_the_pair(
    db: aiosqlite.Connection, tmp_path: Path, monkeypatch
):
    """The sender reached main, the producer push failed: the retry adds the producer."""
    work, origin = _repo_pair(tmp_path)
    project_id = await repo.create_project(db, slug="ext3", name="Ext3")
    await repo.update_project(
        db,
        project_id,
        repo="agentdrover/ext3",
        workspace_path=str(work),
        default_branch="develop",
        default_branch_policy=json.dumps({"release_base": "main"}),
    )
    await db.commit()
    monkeypatch.setattr(
        plugins.git_ops, "clone_repo", AsyncMock(return_value=(True, "verified"))
    )
    hook = origin / "hooks" / "pre-receive"
    hook.write_text(
        '#!/bin/sh\nwhile read o n ref; do [ "$ref" = refs/heads/develop ] && exit 1; done\nexit 0\n'
    )
    hook.chmod(0o755)

    first = await services.provision_project(db, project_id)

    assert "could not push" in first["provision_detail"]
    assert _tree(origin, "main") == [SENDER]
    assert _tree(origin, "develop") == []
    hook.unlink()

    await services.provision_project(db, project_id)

    assert _tree(origin, "develop") == [PRODUCER, "haiplane-stale.yml"]
    assert _tree(origin, "main") == [SENDER]
    assert _git(work, "status", "--porcelain") == ""


async def _statements_for(db, projects) -> int:
    from hub.services import ci_trust_warning

    seen: list[str] = []
    await db.set_trace_callback(seen.append)
    try:
        blocks = await ci_trust_warning.blocks_by_project(db, projects)
    finally:
        await db.set_trace_callback(None)
    assert all(b is not None for b in blocks.values())
    return len(seen)


async def test_the_project_list_costs_the_same_for_one_project_and_for_many(
    db: aiosqlite.Connection, monkeypatch
):
    """No N+1: the statement count does not grow with the number of projects."""
    install_fake_github(monkeypatch)
    rows = []
    for i in range(6):
        proj = await _project(db, f"ext-n{i}", steward=True)
        task_id = await _submitted_task(db, proj)
        sha = f"{i + 1:x}" * 40
        await _submission(db, task_id, sha)
        await _report_without_provenance(db, task_id, sha)
        rows.append(await repo.get_project(db, proj))

    few = await _statements_for(db, rows[:2])
    many = await _statements_for(db, rows)

    assert few == many, (few, many)
    from hub.services import ci_trust_warning

    blocks = await ci_trust_warning.blocks_by_project(db, rows)
    assert {b["state"] for b in blocks.values()} == {"unverified"}


async def test_a_foreign_file_under_the_sender_name_is_left_alone(
    db: aiosqlite.Connection, tmp_path: Path, monkeypatch
):
    """Only the hub's own sender (name AND mark) is ours; the same name with other content is not."""
    work, origin = _repo_pair(tmp_path)
    _git(work, "checkout", "main")
    (work / WORKFLOWS).mkdir(parents=True)
    (work / WORKFLOWS / SENDER).write_text("name: theirs\non: [push]\n", "utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "their file")
    _git(work, "push", "origin", "main")
    _git(work, "checkout", "develop")
    project_id = await repo.create_project(db, slug="ext4", name="Ext4")
    await repo.update_project(
        db,
        project_id,
        repo="agentdrover/ext4",
        workspace_path=str(work),
        default_branch="develop",
        default_branch_policy=json.dumps({"release_base": "main"}),
    )
    await db.commit()
    monkeypatch.setattr(
        plugins.git_ops, "clone_repo", AsyncMock(return_value=(True, "verified"))
    )

    await services.provision_project(db, project_id)

    assert _show(origin, "main", SENDER) == {"name": "theirs", True: ["push"]}
    assert _tree(origin, "develop") == []


async def test_a_sender_that_listens_to_another_workflow_is_not_ours(
    db: aiosqlite.Connection, tmp_path: Path, monkeypatch
):
    """Template-made but `workflows: [Existing tests]`: foreign, nothing is added."""
    from hub.services import satellite_ci

    work, origin = _repo_pair(tmp_path)
    _git(work, "checkout", "main")
    (work / WORKFLOWS).mkdir(parents=True)
    other = satellite_ci.render_sender("Existing tests", f"{WORKFLOWS}/{PRODUCER}")
    (work / WORKFLOWS / SENDER).write_text(other, "utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "sender of another workflow")
    _git(work, "push", "origin", "main")
    _git(work, "checkout", "develop")
    project_id = await repo.create_project(db, slug="ext5", name="Ext5")
    await repo.update_project(
        db,
        project_id,
        repo="agentdrover/ext5",
        workspace_path=str(work),
        default_branch="develop",
        default_branch_policy=json.dumps({"release_base": "main"}),
    )
    await db.commit()
    monkeypatch.setattr(
        plugins.git_ops, "clone_repo", AsyncMock(return_value=(True, "verified"))
    )

    result = await services.provision_project(db, project_id)

    assert "none added" in result["provision_detail"]
    assert _show(origin, "main", SENDER)[True]["workflow_run"]["workflows"] == [
        "Existing tests"
    ]
    assert _tree(origin, "develop") == []


async def test_a_branch_switch_of_the_shared_clone_does_not_misplace_the_producer(
    db: aiosqlite.Connection, tmp_path: Path, monkeypatch
):
    """Between the preflight and the write someone checks out another branch."""
    from hub.services import external_ci_seed

    work, origin = _repo_pair(tmp_path)
    _git(work, "branch", "elsewhere")
    real = external_ci_seed.default_branch

    def switching(workspace: str, fallback: str):
        found = real(workspace, fallback)
        _git(work, "checkout", "elsewhere")  # the network step is the window
        return found

    monkeypatch.setattr(external_ci_seed, "default_branch", switching)
    elsewhere_before = _git(work, "rev-parse", "elsewhere")
    project_id = await repo.create_project(db, slug="ext6", name="Ext6")
    await repo.update_project(
        db,
        project_id,
        repo="agentdrover/ext6",
        workspace_path=str(work),
        default_branch="develop",
        default_branch_policy=json.dumps({"release_base": "main"}),
    )
    await db.commit()
    monkeypatch.setattr(
        plugins.git_ops, "clone_repo", AsyncMock(return_value=(True, "verified"))
    )

    await services.provision_project(db, project_id)

    assert _git(work, "rev-parse", "elsewhere") == elsewhere_before
    assert _tree(origin, "develop") == [PRODUCER, "haiplane-stale.yml"]
    assert _tree(origin, "main") == [SENDER]


def test_the_seeded_templates_say_what_is_true_of_how_they_arrive():
    """Provisioning lays the pair down itself now: no 'not laid down' / 'copy' claim."""
    from hub.services.workflow_seed import read_template

    for name in ("satellite-ci-report.yml", "satellite-ci-producer.yml"):
        text = read_template(name)
        assert "NOT laid down by provisioning" not in text, name
        assert "Copy this file" not in text, name
        assert "no more, no less" not in text, name
    assert "provisioning of a NEW GitHub project lays this file down" in (
        read_template("satellite-ci-report.yml")
    )


async def test_the_warning_words_follow_whether_the_steward_still_waits(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """Final: 'не подтверждаются ... судит без CI'; not final: 'подтверждение ожидается'."""
    install_fake_github(monkeypatch)
    proj = await _project(db, "ext-words", steward=True)
    task_id = await _submitted_task(db, proj)
    await _submission(db, task_id, SHA)
    await _report_without_provenance(db, task_id, SHA)

    final = (await client.get("/api/projects/ext-words/effective-policy")).json()
    text = final["ci_trust"]["message"]
    assert "не подтверждаются: no_provenance" in text
    assert "стюард судит без CI" in text
    assert "ожидается" not in text and "впустую" not in text

    await db.execute(
        "UPDATE ci_report_attempts SET trust_final=0 WHERE task_id=?", (task_id,)
    )
    await db.commit()
    waiting = (await client.get("/api/projects/ext-words/effective-policy")).json()
    text = waiting["ci_trust"]["message"]
    assert "подтверждение ожидается" in text
    assert "судит без CI" not in text

    # A final verdict made under another contract is not final now.
    await db.execute(
        "UPDATE ci_report_attempts SET trust_final=1 WHERE task_id=?", (task_id,)
    )
    await repo.update_project(
        db, proj, gate_policy=json.dumps({"ci_contract": SAT_CONTRACT})
    )
    await db.commit()
    moved = (await client.get("/api/projects/ext-words/effective-policy")).json()
    assert "подтверждение ожидается" in moved["ci_trust"]["message"]


async def test_a_read_failure_is_not_shown_as_confirmed_ci(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """The card and effective-policy say 'не прочитано', not nothing and not None."""
    from hub.services import ci_trust_warning

    await _project(db, "ext-unread", steward=True)

    async def broken(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(ci_trust_warning, "_batch", broken)

    page = (await client.get("/projects")).text
    assert "Состояние CI не прочитано" in page
    body = (await client.get("/api/projects/ext-unread/effective-policy")).json()
    assert body["ci_trust"]["state"] == "unknown"
    assert body["ci_trust"]["warning"] is False
    assert body["ci_trust"] is not None


async def test_a_push_that_did_not_move_the_remote_branch_is_not_success(
    db: aiosqlite.Connection, tmp_path: Path, monkeypatch
):
    """The remote branch must be at the hub's own commit after the push."""
    from hub.services import workflow_seed

    work, origin = _repo_pair(tmp_path)
    real = workflow_seed._git

    def lying(workspace: str, *args: str):
        rc, out = real(workspace, *args)
        if args[:1] == ("ls-remote",) and args[-1] == "refs/heads/develop":
            return rc, "0" * 40 + "\trefs/heads/develop"
        return rc, out

    monkeypatch.setattr(workflow_seed, "_git", lying)
    project_id = await repo.create_project(db, slug="ext7", name="Ext7")
    await repo.update_project(
        db,
        project_id,
        repo="agentdrover/ext7",
        workspace_path=str(work),
        default_branch="develop",
        default_branch_policy=json.dumps({"release_base": "main"}),
    )
    await db.commit()
    monkeypatch.setattr(
        plugins.git_ops, "clone_repo", AsyncMock(return_value=(True, "verified"))
    )

    result = await services.provision_project(db, project_id)

    assert "the remote branch is not at" in result["provision_detail"]
    assert "ожидает настройки владельцем" not in result["provision_detail"]


async def _provision_ext(db, work: Path, slug: str, monkeypatch) -> dict:
    project_id = await repo.create_project(db, slug=slug, name=slug.title())
    await repo.update_project(
        db,
        project_id,
        repo=f"agentdrover/{slug}",
        workspace_path=str(work),
        default_branch="develop",
        default_branch_policy=json.dumps({"release_base": "main"}),
    )
    await db.commit()
    monkeypatch.setattr(
        plugins.git_ops, "clone_repo", AsyncMock(return_value=(True, "verified"))
    )
    return await services.provision_project(db, project_id)


async def test_foreign_ci_on_the_remote_is_seen_even_when_the_clone_is_behind(
    db: aiosqlite.Connection, tmp_path: Path, monkeypatch
):
    """The decision comes from origin/<branch>, not from the clone's working tree."""
    work, origin = _repo_pair(tmp_path)
    other = tmp_path / "other"
    subprocess.run(
        ["git", "clone", "-q", "-b", "develop", str(origin), str(other)], check=True
    )
    _git(other, "config", "user.name", "T")
    _git(other, "config", "user.email", "t@example.com")
    (other / WORKFLOWS).mkdir(parents=True)
    (other / WORKFLOWS / "theirs.yml").write_text("name: theirs\non: [push]\n", "utf-8")
    _git(other, "add", "-A")
    _git(other, "commit", "-m", "their CI")
    _git(other, "push", "origin", "develop")
    heads = (_git(origin, "rev-parse", "develop"), _git(origin, "rev-parse", "main"))
    assert not (work / ".github").exists(), "the clone is behind"

    result = await _provision_ext(db, work, "ext8", monkeypatch)

    assert (
        _git(origin, "rev-parse", "develop"),
        _git(origin, "rev-parse", "main"),
    ) == heads
    assert _tree(origin, "main") == []
    assert "develop" in result["provision_detail"]
    assert "theirs.yml" in result["provision_detail"]
    assert "ожидает настройки владельцем" not in result["provision_detail"]


async def test_provisioning_never_changes_the_shared_clone(
    db: aiosqlite.Connection, tmp_path: Path, monkeypatch
):
    """Neither HEAD nor the working tree of the clone moves, checkout injected or not."""
    from hub.services import external_ci_seed

    work, origin = _repo_pair(tmp_path)
    _git(work, "branch", "elsewhere")
    real = external_ci_seed.default_branch

    def switching(workspace: str, fallback: str):
        found = real(workspace, fallback)
        _git(work, "checkout", "elsewhere")
        return found

    monkeypatch.setattr(external_ci_seed, "default_branch", switching)
    before_develop = _git(work, "rev-parse", "develop")

    await _provision_ext(db, work, "ext9", monkeypatch)

    assert _git(work, "rev-parse", "--abbrev-ref", "HEAD") == "elsewhere"
    assert _git(work, "rev-parse", "develop") == before_develop, (
        "develop not fast-forwarded"
    )
    assert _git(work, "rev-parse", "elsewhere") == before_develop
    assert _git(work, "status", "--porcelain") == ""
    assert not (work / ".github").exists()
    assert _tree(origin, "develop") == [PRODUCER, "haiplane-stale.yml"]


async def test_a_submission_without_a_report_keeps_the_wording_waiting(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    """The steward still waits for a submission with no report row in the window."""
    install_fake_github(monkeypatch)
    proj = await _project(db, "ext-gap", steward=True)
    reported = await _submitted_task(db, proj)
    await _submission(db, reported, SHA)
    await _report_without_provenance(db, reported, SHA)
    silent = await _submitted_task(db, proj)
    await _submission(db, silent, "b" * 40)

    body = (await client.get("/api/projects/ext-gap/effective-policy")).json()

    assert body["ci_trust"]["state"] == "unverified"
    assert "подтверждение ожидается" in body["ci_trust"]["message"]
    assert "судит без CI" not in body["ci_trust"]["message"]


async def test_provisioning_leaves_the_clone_behind_the_remote_untouched(
    db: aiosqlite.Connection, tmp_path: Path, monkeypatch
):
    """No fast-forward after the push: HEAD and the working tree stay as they were."""
    work, origin = _repo_pair(tmp_path)
    before = (
        _git(work, "rev-parse", "HEAD"),
        _git(work, "rev-parse", "--abbrev-ref", "HEAD"),
        _git(work, "status", "--porcelain"),
    )

    await _provision_ext(db, work, "ext10", monkeypatch)

    assert _tree(origin, "develop") == [PRODUCER, "haiplane-stale.yml"]
    assert (
        _git(work, "rev-parse", "HEAD"),
        _git(work, "rev-parse", "--abbrev-ref", "HEAD"),
        _git(work, "status", "--porcelain"),
    ) == before
    assert not (work / ".github").exists()
