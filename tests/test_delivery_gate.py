"""Work that never left the branch, named when it is called done (#498).

The hub learned today to tell "merged" from "running in production" (#497).
This is the earlier loss: a task finished with commits on its branch and no
pull request at all — delivery never started, and the report read exactly like
a delivered one.

The tests are as much about the silences as about the warning: a task without a
branch, without a workspace, or with a git that will not answer must produce
nothing. An accusation made out of ignorance is worse than saying nothing.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import aiosqlite
from httpx import AsyncClient

from hub import repository as repo
from hub.services.delivery_gate import undelivered_warning


async def _running_task(client: AsyncClient, title: str = "Undelivered?") -> int:
    task_id = (await client.post("/api/tasks", json={"title": title})).json()["id"]
    await client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": "dev", "kind": "status", "content": "Plan: work"},
    )
    await client.post(
        f"/api/tasks/{task_id}/pair-start", json={"assigned_agent": "dev"}
    )
    return task_id


def _workspace_with_changes(monkeypatch, changed: list[str] | None) -> None:
    """Point the check at a workspace whose branch reports ``changed``.

    ``None`` stands for "git would not answer" — the case that must stay quiet.
    """
    from hub import app as hub_app
    from hub.integrations.registry import plugins

    monkeypatch.setattr(
        hub_app.services,
        "project_git_context",
        AsyncMock(return_value={"repo": "/srv/ws", "base_branch": "develop"}),
    )
    monkeypatch.setattr(
        "hub.services.orchestration.project_git_context",
        AsyncMock(return_value={"repo": "/srv/ws", "base_branch": "develop"}),
        raising=False,
    )
    monkeypatch.setattr(
        plugins.git_ops, "branch_diff_paths", AsyncMock(return_value=changed)
    )


async def test_commits_without_pr_warn_on_done(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-1 (#498): changes on the branch, no PR, no merge — delivery never
    # started, and the report must say so instead of reading like any other.
    _workspace_with_changes(monkeypatch, ["hub/web.py", "tests/test_web.py"])
    task_id = await _running_task(client)

    resp = await client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": "dev", "kind": "done", "content": "Готово"},
    )

    assert resp.status_code == 200, resp.text
    warnings = resp.json()["warnings"]
    assert warnings, "undelivered work must be named on the report itself"
    assert "не начала доставляться" in warnings[0]
    body = (await client.get(f"/api/tasks/{task_id}")).json()
    assert body["status"] in ("completed", "review", "ci_check"), (
        "the warning is advisory — completion must not be blocked"
    )


async def test_delivered_work_is_not_warned_about(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-2 (#498): a PR (or a merge the hub performed) means delivery started.
    # Warning here would train people to ignore the warning.
    _workspace_with_changes(monkeypatch, ["hub/web.py"])
    task_id = await _running_task(client)
    await repo.update_task(db, task_id, pr_number=4242)
    await db.commit()

    with_pr = await undelivered_warning(db, dict(await repo.get_task(db, task_id)))

    await repo.update_task(db, task_id, pr_number=None)
    await db.execute(
        "INSERT INTO pipeline_merges (project_id, pr_number, task_id, merge_sha) "
        "VALUES (?, ?, ?, ?)",
        (1, 4243, task_id, "merged-somewhere"),
    )
    await db.commit()
    with_merge = await undelivered_warning(db, dict(await repo.get_task(db, task_id)))

    assert with_pr == ""
    assert with_merge == ""


async def test_unknown_delivery_stays_silent(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-3 (#498): three ways of not knowing, three silences. This is the same
    # line #839, #497 and #883 hold, turned around: there an absence could not
    # be printed as denial, here it cannot be printed as fault.
    task_id = await _running_task(client)
    task = dict(await repo.get_task(db, task_id))

    # git will not answer
    _workspace_with_changes(monkeypatch, None)
    assert await undelivered_warning(db, task) == ""

    # the branch changes nothing
    _workspace_with_changes(monkeypatch, [])
    assert await undelivered_warning(db, task) == ""

    # no branch at all: research and decisions were never meant to leave one
    await repo.update_task(db, task_id, branch=None)
    await db.commit()
    _workspace_with_changes(monkeypatch, ["hub/web.py"])
    assert await undelivered_warning(db, dict(await repo.get_task(db, task_id))) == ""


async def test_warning_reaches_the_task_feed(
    client: AsyncClient, db: aiosqlite.Connection, monkeypatch
):
    # AC-4 (#498): the agent reads the response, the owner reads the feed.
    # A warning in only one of the two reaches nobody who can act on it — the
    # defect #826 found in review findings.
    _workspace_with_changes(monkeypatch, ["hub/web.py"])
    task_id = await _running_task(client)

    await client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": "dev", "kind": "done", "content": "Готово"},
    )

    updates = (await client.get(f"/api/tasks/{task_id}/updates")).json()
    alerts = [u for u in updates if u["kind"] == "alert"]
    assert any("не начала доставляться" in u["content"] for u in alerts), (
        "the owner reads the feed, not the agent's response"
    )
