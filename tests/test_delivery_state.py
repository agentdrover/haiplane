"""Merged is not running (#497).

On 21.08.2026 task #823 sat ``completed`` with its PR merged into develop while
the deploy job was skipped — deployment runs from main. Nothing in the hub could
tell the two apart; it took reading GitHub's logs. These tests hold the
comparison that closes that gap, and above all the third answer: "we could not
check" must never print as "not deployed".
"""

from __future__ import annotations

from pathlib import Path

from unittest.mock import AsyncMock

import aiosqlite
import pytest
from httpx import AsyncClient

from hub import repository as repo
from hub.integrations.git_ops import GitOpsIntegration
from hub.integrations.registry import plugins
from hub.services.delivery_state import IN_PROD, NOT_IN_PROD, UNKNOWN, delivery_state


async def _task_merged_at(client: AsyncClient, db, merge_sha: str) -> int:
    """A task the hub merged at ``merge_sha`` — the fact #534 records."""
    task_id = (await client.post("/api/tasks", json={"title": "Delivered?"})).json()[
        "id"
    ]
    await db.execute(
        "INSERT INTO pipeline_merges (project_id, pr_number, task_id, merge_sha) "
        "VALUES (?, ?, ?, ?)",
        (1, 4000 + task_id, task_id, merge_sha),
    )
    await db.commit()
    return task_id


def _use_real_git(monkeypatch, workspace: str) -> None:
    from hub import app as hub_app

    real = GitOpsIntegration()
    context = AsyncMock(return_value={"repo": workspace, "base_branch": "main"})
    monkeypatch.setattr(hub_app.services, "project_git_context", context)
    monkeypatch.setattr(
        "hub.services.orchestration.project_git_context", context, raising=False
    )
    monkeypatch.setattr(plugins.git_ops, "is_ancestor", real.is_ancestor, raising=False)


async def test_merge_reachable_from_deploy_is_in_prod(
    client: AsyncClient, db: aiosqlite.Connection, history, monkeypatch
):
    # AC-1 (#497): the merge is in the history of what production runs.
    _use_real_git(monkeypatch, history["repo"])
    task_id = await _task_merged_at(client, db, history["shipped"])
    await repo.record_release(
        db, deployed_sha=history["released"], ref="main", source="ci"
    )

    answer = await delivery_state(db, task_id)

    assert answer["state"] == IN_PROD, answer
    assert answer["deployed_sha"] == history["released"]
    assert answer["reason"], "even the good news has to say what it is based on"


async def test_merged_but_not_deployed_is_not_in_prod(
    client: AsyncClient, db: aiosqlite.Connection, history, monkeypatch
):
    # AC-2 (#497): the exact shape of the 21.08 defect — merged, not shipped.
    _use_real_git(monkeypatch, history["repo"])
    task_id = await _task_merged_at(client, db, history["pending"])
    await repo.record_release(
        db, deployed_sha=history["released"], ref="main", source="ci"
    )

    answer = await delivery_state(db, task_id)

    assert answer["state"] == NOT_IN_PROD, answer
    assert "ждёт релиза" in answer["reason"]


async def test_missing_facts_read_as_unknown_not_denial(
    client: AsyncClient, db: aiosqlite.Connection, history, monkeypatch
):
    # AC-3 (#497): three answers, never two. Each absence names itself, and
    # none of them may masquerade as "not deployed".
    _use_real_git(monkeypatch, history["repo"])

    no_merge = await delivery_state(
        db,
        (await client.post("/api/tasks", json={"title": "Never merged"})).json()["id"],
    )
    assert no_merge["state"] == UNKNOWN
    assert "не мержил" in no_merge["reason"]

    task_id = await _task_merged_at(client, db, history["shipped"])
    no_release = await delivery_state(db, task_id)
    assert no_release["state"] == UNKNOWN
    assert "незнание, а не отрицание" in no_release["reason"]

    # git that cannot answer: a sha this checkout does not carry.
    await repo.record_release(db, deployed_sha="f" * 40, ref="main", source="ci")
    unreadable = await delivery_state(db, task_id)
    assert unreadable["state"] == UNKNOWN
    assert "не «не раскатано»" in unreadable["reason"]


async def test_card_names_the_delivery_state(
    client: AsyncClient, db: aiosqlite.Connection, history, monkeypatch
):
    # AC-4 (#497): the answer has to reach the person deciding, in words.
    _use_real_git(monkeypatch, history["repo"])
    task_id = await _task_merged_at(client, db, history["pending"])
    await repo.record_release(
        db, deployed_sha=history["released"], ref="main", source="ci"
    )
    await repo.update_task(db, task_id, status="completed")
    await db.commit()

    page = (await client.get(f"/tasks/{task_id}")).text

    assert "Доставка" in page
    assert "not_in_prod" in page
    assert "ждёт релиза" in page


# ---- #883: git can only answer about objects it has ----
#
# Observed on prod right after #497 shipped: every card answered "could not
# check", because the workspace tracks develop and the deployed commit — made
# on main — was simply not there. The computation was honest and useless.


@pytest.fixture
def clone_missing_the_deploy(tmp_path: Path, history) -> dict[str, str]:
    """A clone that has the merge but NOT the released commit.

    Built by cloning at the older commit and leaving origin behind: exactly
    the prod shape, where the workspace sits on one branch and the deploy came
    from another.
    """
    from tests.conftest import _git_in

    clone = tmp_path / "clone"
    # Two flags, both load-bearing, both found by the precondition below
    # failing: --single-branch, because a plain clone copies every ref; and
    # --no-local, because a clone from a path hardlinks the WHOLE object
    # database and the "missing" commit arrives anyway.
    _git_in(
        tmp_path,
        "clone",
        "--quiet",
        "--no-local",
        "--single-branch",
        "--branch",
        "later",
        history["repo"],
        str(clone),
    )
    return {**history, "clone": str(clone)}


async def test_present_commit_costs_no_network(
    client: AsyncClient, db: aiosqlite.Connection, history, monkeypatch
):
    # AC-4 (#883): the fetch is a repair, not a routine. A workspace that
    # already carries the commit must not pay for the network on every render.
    _use_real_git(monkeypatch, history["repo"])
    calls: list[str] = []

    async def _refuse_to_fetch(*args, **kwargs):
        calls.append("fetch")
        return (False, "should not have been called")

    monkeypatch.setattr(
        plugins.git_ops, "fetch_commit", _refuse_to_fetch, raising=False
    )
    monkeypatch.setattr(
        plugins.git_ops,
        "commit_exists",
        GitOpsIntegration().commit_exists,
        raising=False,
    )
    task_id = await _task_merged_at(client, db, history["shipped"])
    await repo.record_release(
        db, deployed_sha=history["released"], ref="main", source="ci"
    )

    answer = await delivery_state(db, task_id)

    assert answer["state"] == IN_PROD
    assert calls == [], "the commit was here — nothing should have been fetched"


async def test_read_path_fetches_once_for_older_releases(
    client: AsyncClient, db: aiosqlite.Connection, clone_missing_the_deploy, monkeypatch
):
    # AC-2 (#883): releases recorded before this task left their commit behind.
    # One repair attempt on read turns "could not check" into a real answer.
    workspace = clone_missing_the_deploy["clone"]
    _use_real_git(monkeypatch, workspace)
    real = GitOpsIntegration()
    for name in ("commit_exists", "fetch_commit"):
        monkeypatch.setattr(plugins.git_ops, name, getattr(real, name), raising=False)
    task_id = await _task_merged_at(client, db, clone_missing_the_deploy["shipped"])
    await repo.record_release(
        db,
        deployed_sha=clone_missing_the_deploy["released"],
        ref="main",
        source="ci",
    )

    answer = await delivery_state(db, task_id)

    assert answer["state"] in (IN_PROD, NOT_IN_PROD), answer
    assert answer["state"] == IN_PROD, "the merge is in the released history"


async def test_failed_fetch_stays_unknown_never_denial(
    client: AsyncClient, db: aiosqlite.Connection, clone_missing_the_deploy, monkeypatch
):
    # AC-3 (#883): no network, no answer — and "no answer" must never be
    # spelled "not deployed". This is the same line #839 and #497 hold.
    workspace = clone_missing_the_deploy["clone"]
    _use_real_git(monkeypatch, workspace)
    monkeypatch.setattr(
        plugins.git_ops,
        "commit_exists",
        GitOpsIntegration().commit_exists,
        raising=False,
    )

    async def _no_network(*args, **kwargs):
        return (False, "origin unreachable")

    monkeypatch.setattr(plugins.git_ops, "fetch_commit", _no_network, raising=False)
    task_id = await _task_merged_at(client, db, clone_missing_the_deploy["shipped"])
    await repo.record_release(
        db,
        deployed_sha=clone_missing_the_deploy["released"],
        ref="main",
        source="ci",
    )

    answer = await delivery_state(db, task_id)

    assert answer["state"] == UNKNOWN
    assert "подтянуть" in answer["reason"]
    assert "origin unreachable" in answer["reason"], "the cause travels to the reader"


async def test_recording_a_deploy_fetches_its_commit(
    db: aiosqlite.Connection, clone_missing_the_deploy, monkeypatch
):
    # AC-1 (#883): the repair happens once per deploy, where the deploy is
    # recorded — not once per card render.
    from hub.services.delivery_state import ensure_commit_available

    workspace = clone_missing_the_deploy["clone"]
    real = GitOpsIntegration()
    for name in ("commit_exists", "fetch_commit"):
        monkeypatch.setattr(plugins.git_ops, name, getattr(real, name), raising=False)
    # The in-memory database has no seeded project, so the row is created
    # rather than updated: an UPDATE that matches nothing fails silently and
    # the helper would answer "no workspace" for a reason unrelated to fetching.
    await db.execute(
        "INSERT INTO projects (slug, name, workspace_path) VALUES (?, ?, ?) "
        "ON CONFLICT(slug) DO UPDATE SET workspace_path = excluded.workspace_path",
        ("default", "Default", workspace),
    )
    await db.commit()
    released = clone_missing_the_deploy["released"]
    assert await real.commit_exists(workspace, released) is False, (
        "fixture precondition"
    )

    assert await ensure_commit_available(db, released, "main") is True
    assert await real.commit_exists(workspace, released) is True
