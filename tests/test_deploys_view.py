"""Release history, project scope and bounded Git reads behind GET /api/deploys."""

import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from hub import repository as repo
from hub.integrations.registry import plugins
from hub.services import deploys_view as deploys
from hub.services import egress_watch
from hub.services.admin import create_api_key, create_principal
from tests.test_private_project_schema import account, project

OLD = "a" * 40
NEW = "b" * 40


@pytest.fixture(autouse=True)
def git_heads(monkeypatch):
    monkeypatch.setattr(deploys, "_head_cache", {})
    remote = AsyncMock(side_effect=lambda branches, **kw: dict.fromkeys(branches, OLD))
    monkeypatch.setattr(plugins.git_ops, "remote_heads", remote)
    return remote


@pytest.fixture
async def default_project(db):
    pid = await repo.create_project(
        db, slug="default", name="Default", default_branch="develop"
    )
    await db.commit()
    return pid


async def _merge(db, project_id, pr, sha, minute):
    await db.execute(
        "INSERT INTO release_merges (project_id, release_pr, release_sha, merged_at) "
        "VALUES (?, ?, ?, ?)",
        (project_id, pr, sha, f"2026-10-20 09:{minute:02}:00"),
    )


async def _release(db, project_id, sha, minute=10, *, status="success"):
    release_id = await repo.record_release(
        db,
        deployed_sha=sha,
        project_id=project_id,
        ref="main",
        status=status,
        source="ci",
        backup="ok",
    )
    await db.execute(
        "UPDATE releases SET deployed_at=? WHERE id=?",
        (f"2026-10-20 09:{minute:02}:00", release_id),
    )
    return release_id


async def _task_merge(db, project_id, pr, released_sha=None, *, task_id=None):
    if task_id is None:
        task_id = await repo.create_task(
            db,
            title=f"Task {pr}",
            description="",
            runtime="auto",
            source="human",
            assigned_agent="",
            rationale="",
            status="completed",
            auto_review=True,
            task_type="task",
            parent_id=None,
            priority="medium",
        )
        await repo.update_task(db, task_id, project_id=project_id)
    await db.execute(
        "INSERT INTO pipeline_merges (project_id, pr_number, task_id, released_sha) "
        "VALUES (?, ?, ?, ?)",
        (project_id, pr, task_id, released_sha),
    )
    return task_id


async def test_release_merged_after_deployed_counts_as_not_deployed(
    db, default_project
):
    await _merge(db, default_project, 1, OLD, 5)
    await _merge(db, default_project, 2, NEW, 5)
    await _release(db, default_project, OLD)
    view = await deploys.deploys_view(db, default_project)
    assert view.pipeline.in_main_not_deployed == 1
    assert view.pipeline.deployed.sha == OLD
    assert view.releases[0].release_pr == 1
    assert view.releases[0].lag_minutes == 5


async def test_unmatched_deployed_sha_is_unknown_not_zero(db, default_project):
    await _merge(db, default_project, 1, OLD[:12], 5)
    await _release(db, default_project, OLD)
    view = await deploys.deploys_view(db, default_project)
    assert view.pipeline.in_main_not_deployed is None
    assert "SHA" in view.pipeline.in_main_not_deployed_reason


async def test_waiting_release_counts_unstamped_completed(db, default_project):
    await _release(db, default_project, OLD)
    unstamped = await _task_merge(db, default_project, 1, OLD)
    await _task_merge(db, default_project, 2, task_id=unstamped)
    await _task_merge(db, default_project, 3, "")
    kept = await _task_merge(db, default_project, 4, f" {OLD.upper()} ")
    view = await deploys.deploys_view(db, default_project)
    assert view.pipeline.waiting_release == 2
    assert view.releases[0].task_ids == [unstamped, kept]


async def test_merge_is_delivery_project_has_no_pipeline_counts(db):
    project = await repo.create_project(db, slug="sibling", name="Sibling")
    await repo.update_project(db, project, gate_policy='{"merge_is_delivery":true}')
    await _task_merge(db, project, 1)
    await _release(db, project, OLD)
    view = await deploys.deploys_view(db, project)
    assert view.pipeline.waiting_release is None
    assert view.pipeline.in_main_not_deployed is None
    assert view.pipeline.note == "мерж = выкат"


async def test_feed_order_and_failed_row_never_deployed(db, default_project):
    first = await _release(db, default_project, OLD, 20)
    failed = await _release(db, default_project, NEW, 20, status="failed")
    earlier = await _release(db, default_project, "c" * 40, 10, status="failed")
    view = await deploys.deploys_view(db, default_project)
    assert [r.id for r in view.releases] == [failed, first, earlier]
    assert [r.status for r in view.releases] == ["failed", "success", "failed"]
    assert view.pipeline.deployed.sha == OLD
    assert view.releases[1].backup == "ok"
    assert len((await deploys.deploys_view(db, default_project, 2)).releases) == 2


async def test_feed_includes_legacy_null_project_rows(db, default_project):
    foreign = await repo.create_project(db, slug="other", name="Other")
    legacy = await _release(db, None, OLD)
    matching = await _task_merge(db, default_project, 1, OLD)
    await _task_merge(db, default_project, 2, OLD[:12])
    assigned_elsewhere = await _task_merge(db, foreign, 3, OLD)
    await _task_merge(db, None, 4, OLD, task_id=assigned_elsewhere)
    inherited_elsewhere = await _task_merge(db, None, 5, OLD)
    await repo.update_task(db, inherited_elsewhere, parent_id=assigned_elsewhere)
    await _task_merge(db, None, 6, task_id=inherited_elsewhere)
    orphan = await _task_merge(db, None, 7, OLD)
    foreign_id = await _release(db, foreign, NEW)
    view = await deploys.deploys_view(db, default_project)
    assert [r.id for r in view.releases] == [legacy]
    assert view.releases[0].task_ids == [matching, orphan]
    assert view.pipeline.waiting_release == 0
    assert [r.id for r in (await deploys.deploys_view(db, foreign)).releases] == [
        foreign_id
    ]


async def test_stuck_lists_release_block_and_deploy_watch_reason(db, default_project):
    prefix = {"project_id": default_project, "actor": "hub"}
    await repo.insert_event(
        db, kind="release_blocked", payload={"reason": "CI красный"}, **prefix
    )
    for item, kind, reason in (
        (4, "release_not_in_prod", "Релиз ещё не доехал"),
        (5, "release_deploy_unobserved", "Уже восстановилось"),
        (5, "release_deployed_late", "Доехал позже"),
    ):
        await repo.insert_event(
            db,
            kind=kind,
            payload={"release_merge_id": item, "reason": reason},
            **prefix,
        )
    view = await deploys.deploys_view(db, default_project)
    assert view.pipeline.stuck == ["CI красный", "Релиз ещё не доехал"]
    assert view.pipeline.note == "выкаты не наблюдаются"


@pytest.mark.timeout(8)
async def test_hung_ls_remote_bounded_to_three_seconds(
    client, db, default_project, git_heads
):
    cancelled = asyncio.Event()

    async def hung(*args, **kwargs):
        try:
            await asyncio.sleep(30)
        finally:
            cancelled.set()

    git_heads.side_effect = hung
    await _release(db, default_project, OLD)
    await _task_merge(db, default_project, 1)
    await db.commit()
    started = time.monotonic()
    response = await client.get("/api/deploys")
    assert time.monotonic() - started < 4
    assert cancelled.is_set()
    assert response.status_code == 200
    pipeline = response.json()["pipeline"]
    assert pipeline["heads"]["source"] == "unknown"
    assert "3 с" in pipeline["heads"]["reason"]
    assert pipeline["waiting_release"] == 1
    assert pipeline["deployed"]["sha"] == OLD


async def test_egress_down_skips_network(db, default_project, monkeypatch, git_heads):
    monkeypatch.setattr(
        egress_watch, "egress_status", AsyncMock(return_value={"state": "down"})
    )
    tracked_heads = AsyncMock(side_effect=["local-develop", "local-main"])
    monkeypatch.setattr(plugins.git_ops, "head_sha", tracked_heads)
    view = await deploys.deploys_view(db, default_project)
    git_heads.assert_not_called()
    assert view.pipeline.heads.heads == {
        "develop": "local-develop",
        "main": "local-main",
    }
    assert view.pipeline.heads.source == "last_fetch"
    assert "по последнему fetch" in deploys.format_deploys_view(view)


async def test_scoped_ci_key_refused_on_get_deploys(ci_runner_hub, scoped_ci_key):
    hub = ci_runner_hub
    await repo.create_project(hub.db, slug="deploy-fixture", name="Deploy fixture")
    await hub.db.commit()
    credentials = await scoped_ci_key('["project:deploy-fixture"]')
    params = {"project": "deploy-fixture"}
    read = await hub.client.get("/api/deploys", params=params, headers=credentials)
    assert read.status_code == 403
    write = await hub.client.post(
        "/api/deploys",
        json={**params, "sha": NEW, "ref": "main"},
        headers=credentials,
    )
    assert write.status_code == 200, write.text


async def test_remote_lookup_is_cached_for_five_minutes(
    db, default_project, git_heads, monkeypatch
):
    now = 0
    monkeypatch.setattr(deploys, "time", type("Clock", (), {"monotonic": lambda: now}))
    await deploys.pipeline(db, default_project)
    now = 299
    await deploys.pipeline(db, default_project)
    git_heads.assert_awaited_once()
    now = 300
    await deploys.pipeline(db, default_project)
    assert git_heads.await_count == 2


async def test_private_feed_requires_project_membership(ci_runner_hub, git_heads):
    hub = ci_runner_hub
    owner = hub.human_principal["id"]
    link = await account(hub.db, owner, 917)
    project_id = await project(hub.db, owner, link)
    slug = (await repo.get_project(hub.db, project_id))["slug"]
    await _release(hub.db, project_id, OLD)
    root = await _task_merge(hub.db, project_id, 1, OLD)
    visible = [root]
    parent = root
    for pr in range(2, 22):
        descendant = await _task_merge(hub.db, project_id, pr, OLD)
        await repo.update_task(hub.db, descendant, project_id=None, parent_id=parent)
        parent = descendant
        if pr < 21:
            visible.append(descendant)
    waiting = await _task_merge(hub.db, project_id, 22)
    await repo.update_task(hub.db, waiting, project_id=None, parent_id=root)
    outsider = await create_principal(
        hub.db, kind="human", username="outside-project", role_slug="operator"
    )
    key = await create_api_key(hub.db, outsider["id"], name="test")
    await hub.db.commit()
    params = {"project": slug}
    denied = await hub.client.get(
        "/api/deploys",
        params=params,
        headers={"Authorization": f"Bearer {key['plaintext_key']}"},
    )
    assert denied.status_code == 404
    allowed = await hub.client.get("/api/deploys", params=params, headers=hub.human)
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["releases"][0]["sha"] == OLD
    assert allowed.json()["releases"][0]["task_ids"] == visible
    assert allowed.json()["pipeline"]["waiting_release"] == 1
    assert allowed.json()["pipeline"]["heads"]["source"] == "unknown"
    git_heads.assert_not_called()
