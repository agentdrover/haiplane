"""Merged is not running (#497).

On 21.08.2026 task #823 sat ``completed`` with its PR merged into develop while
the deploy job was skipped — deployment runs from main. Nothing in the hub could
tell the two apart; it took reading GitHub's logs. These tests hold the
comparison that closes that gap, and above all the third answer: "we could not
check" must never print as "not deployed".
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import aiosqlite
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
