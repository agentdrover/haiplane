"""The release a task is compared with is its own project's (#1927).

The newest deploy of ANY project used to be the yardstick for every task:
after spike-bo deployed last, «Раскатано» of the hub showed spike-bo's sha and
default tasks read as not_in_prod/unknown against a foreign commit.
"""

from __future__ import annotations

import json
from pathlib import Path

import aiosqlite
from httpx import AsyncClient

from hub import repository as repo
from hub.integrations.git_ops import GitOpsIntegration
from hub.integrations.registry import plugins
from hub.services.delivery_state import IN_PROD, NOT_IN_PROD, UNKNOWN, delivery_state
from tests.conftest import _git_in
from tests.test_delivery_state import _task_merged_at, _use_real_git


def _orphan_commit(root: str, name: str = "other") -> str:
    """A commit that shares no history with the fixture repo's branches."""
    work = Path(root)
    _git_in(work, "checkout", "-q", "--orphan", name)
    (work / f"{name}.txt").write_text(name)
    _git_in(work, "add", f"{name}.txt")
    _git_in(work, "commit", "-q", "-m", name)
    sha = _git_in(work, "rev-parse", "HEAD").strip()
    _git_in(work, "checkout", "-q", "main")
    return sha


async def _default_project_id(db: aiosqlite.Connection) -> int:
    """The seeded default project; a bare test database has none yet."""
    row = await repo.get_project_by_slug(db, "default")
    if row is None:
        await repo.create_project(db, slug="default", name="Default")
        await db.commit()
        row = await repo.get_project_by_slug(db, "default")
    return int(row["id"])


async def _foreign_project(db: aiosqlite.Connection, slug: str = "spike-bo") -> int:
    pid = await repo.create_project(db, slug=slug, name=slug)
    await db.commit()
    return pid


async def test_task_compared_with_its_own_project_release(
    client: AsyncClient, db: aiosqlite.Connection, history, monkeypatch
):
    # AC-2 (#1927): the newest deploy of ANY project used to be the yardstick
    # for every task. After spike-bo deployed last, a default task whose merge
    # is inside the default release read as not_in_prod/unknown.
    _use_real_git(monkeypatch, history["repo"])
    default_id = await _default_project_id(db)
    task_id = await _task_merged_at(client, db, history["shipped"])
    await repo.record_release(
        db,
        deployed_sha=history["released"],
        project_id=default_id,
        ref="main",
        source="ci",
    )
    await repo.record_release(
        db,
        deployed_sha=_orphan_commit(history["repo"]),
        project_id=await _foreign_project(db),
        ref="main",
        source="spike-bo-ci",
    )

    answer = await delivery_state(db, task_id)

    assert answer["state"] == IN_PROD, answer
    assert answer["deployed_sha"] == history["released"]


async def test_legacy_null_project_release_counts_for_default(
    client: AsyncClient, db: aiosqlite.Connection, history, monkeypatch
):
    # AC-3 (#1927): 240 rows written before 28.09 carry no project_id; by the
    # #915 rule they ARE the default project's history, also when another
    # project deployed after them.
    _use_real_git(monkeypatch, history["repo"])
    await _default_project_id(db)  # as on a real install: default exists
    task_id = await _task_merged_at(client, db, history["shipped"])
    await repo.record_release(
        db, deployed_sha=history["released"], project_id=None, ref="main", source="ci"
    )
    await repo.record_release(
        db,
        deployed_sha=_orphan_commit(history["repo"]),
        project_id=await _foreign_project(db),
        ref="main",
        source="spike-bo-ci",
    )

    answer = await delivery_state(db, task_id)

    assert answer["state"] == IN_PROD, answer
    assert answer["deployed_sha"] == history["released"]


async def test_other_project_task_is_compared_with_that_projects_release(
    client: AsyncClient, db: aiosqlite.Connection, history, monkeypatch
):
    # #1927: the rule is per project, not "default wins": a task of a project
    # that reports its own deploys is compared with THAT deploy, and the
    # default project's newer release is not its yardstick.
    _use_real_git(monkeypatch, history["repo"])
    default_id = await _default_project_id(db)
    pid = await _foreign_project(db)
    epic = (
        await client.post("/api/tasks", json={"title": "E", "task_type": "epic"})
    ).json()["id"]
    await repo.update_task(db, epic, project_id=pid)
    task_id = (
        await client.post(
            "/api/tasks", json={"title": "T", "task_type": "task", "parent_id": epic}
        )
    ).json()["id"]
    await db.execute(
        "INSERT INTO pipeline_merges (project_id, pr_number, task_id, merge_sha) "
        "VALUES (?, ?, ?, ?)",
        (pid, 4900 + task_id, task_id, history["shipped"]),
    )
    await db.commit()
    await repo.record_release(
        db,
        deployed_sha=history["released"],
        project_id=pid,
        ref="main",
        source="spike-bo-ci",
    )
    await repo.record_release(
        db,
        deployed_sha=_orphan_commit(history["repo"]),
        project_id=default_id,
        ref="main",
        source="ci",
    )

    answer = await delivery_state(db, task_id)

    assert answer["state"] == IN_PROD, answer
    assert answer["deployed_sha"] == history["released"]


async def test_project_without_own_release_is_unknown_not_borrowed(
    client: AsyncClient, db: aiosqlite.Connection, history, monkeypatch
):
    # #1927: a project with no deploy of its own must not borrow the default
    # project's release — that would print another project's sha as its fact.
    _use_real_git(monkeypatch, history["repo"])
    pid = await _foreign_project(db)
    epic = (
        await client.post("/api/tasks", json={"title": "E", "task_type": "epic"})
    ).json()["id"]
    await repo.update_task(db, epic, project_id=pid)
    task_id = (
        await client.post(
            "/api/tasks", json={"title": "T", "task_type": "task", "parent_id": epic}
        )
    ).json()["id"]
    await db.execute(
        "INSERT INTO pipeline_merges (project_id, pr_number, task_id, merge_sha) "
        "VALUES (?, ?, ?, ?)",
        (pid, 4900 + task_id, task_id, history["shipped"]),
    )
    await db.commit()
    await repo.record_release(
        db, deployed_sha=history["released"], project_id=None, ref="main", source="ci"
    )

    answer = await delivery_state(db, task_id)

    assert answer["state"] == UNKNOWN, answer
    assert "deployed_sha" not in answer or not answer.get("deployed_sha")


# --- the repository of the project is not the hub's (#1927, Codex P2) -------


async def _two_repo_world(client, db, history, monkeypatch, *, merge_sha, policy=None):
    """Hub project and spike-bo in DIFFERENT repositories, real project context.

    ``project_git_context`` is NOT replaced: the repo name and workspace come
    from the project rows, as in production. A stub that returns no ``gh_repo``
    hid exactly this defect.
    """
    from hub import config

    monkeypatch.setattr(config, "REPO_NAME", "agentdrover/haiplane")
    real = GitOpsIntegration()
    for name in ("is_ancestor", "commit_exists", "commit_with_same_tree"):
        monkeypatch.setattr(plugins.git_ops, name, getattr(real, name), raising=False)
    if await repo.get_project_by_slug(db, "default") is None:
        await repo.create_project(
            db,
            slug="default",
            name="Default",
            repo_name="agentdrover/haiplane",
            workspace_path=history["repo"],
            default_branch="main",
        )
    pid = await repo.create_project(
        db,
        slug="spike-bo",
        name="spike-bo",
        repo_name="agentdrover/spike-bo",
        workspace_path=history["repo"],
        default_branch="main",
    )
    if policy is not None:
        await repo.update_project(db, pid, gate_policy=json.dumps(policy))
    epic = (
        await client.post("/api/tasks", json={"title": "E", "task_type": "epic"})
    ).json()["id"]
    await repo.update_task(db, epic, project_id=pid)
    task_id = (
        await client.post(
            "/api/tasks", json={"title": "T", "task_type": "task", "parent_id": epic}
        )
    ).json()["id"]
    await db.execute(
        "INSERT INTO pipeline_merges (project_id, pr_number, task_id, merge_sha) "
        "VALUES (?, ?, ?, ?)",
        (pid, 4600 + task_id, task_id, merge_sha),
    )
    await db.commit()
    await repo.record_release(
        db,
        deployed_sha=history["released"],
        project_id=pid,
        ref="main",
        source="spike-bo-ci",
    )
    # the hub's own newer deploy, so that "newest of any" would be wrong too
    await repo.record_release(
        db,
        deployed_sha=_orphan_commit(history["repo"]),
        project_id=await _default_project_id(db),
        ref="main",
        source="ci",
    )
    return task_id


async def test_project_in_other_repo_with_own_release_is_in_prod(
    client: AsyncClient, db: aiosqlite.Connection, history, monkeypatch
):
    # #1927 P2: gh_repo of the project differs from the hub's, the project has
    # its own deploy and the merge is an ancestor of it — in_prod, not the
    # «релиз хаба не применим» unknown that the old repo cut-off produced.
    task_id = await _two_repo_world(
        client, db, history, monkeypatch, merge_sha=history["shipped"]
    )

    answer = await delivery_state(db, task_id)

    assert answer["state"] == IN_PROD, answer
    assert answer["deployed_sha"] == history["released"]


async def test_project_in_other_repo_waiting_is_not_in_prod(
    client: AsyncClient, db: aiosqlite.Connection, history, monkeypatch
):
    # #1927 P2: the same world, a merge no deploy of the project carries.
    task_id = await _two_repo_world(
        client, db, history, monkeypatch, merge_sha=history["pending"]
    )

    answer = await delivery_state(db, task_id)

    assert answer["state"] == NOT_IN_PROD, answer
    assert answer["deployed_sha"] == history["released"]


async def test_merge_is_delivery_project_keeps_its_own_release_answer(
    client: AsyncClient, db: aiosqlite.Connection, history, monkeypatch
):
    # #1927: a project that declared «мерж = доставка» (#1572) is compared with
    # its own deploy exactly like any other; the declaration changes nothing here.
    task_id = await _two_repo_world(
        client,
        db,
        history,
        monkeypatch,
        merge_sha=history["shipped"],
        policy={"merge_is_delivery": True},
    )

    answer = await delivery_state(db, task_id)

    assert answer["state"] == IN_PROD, answer
    assert answer["deployed_sha"] == history["released"]
