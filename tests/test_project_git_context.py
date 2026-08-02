"""The default project's configuration reaches its consumers (#604).

project_git_context used to return an unconditional empty context for
slug=default — compatibility from the days when default had no real fields.
Once the owner configured a real repo and workspace (#602), that line threw
the configuration away, and three brief mechanisms stayed blind next to a
live clone. The sixth "mechanism right, path not wired" of the streak, and
the first where the data was cut off on its way to the call.
"""

from __future__ import annotations

from httpx import AsyncClient

from hub import repository as repo
from hub.services.orchestration import project_git_context


async def _task_in_default(client: AsyncClient) -> int:
    resp = await client.post("/api/tasks", json={"title": "Sees itself"})
    return resp.json()["id"]


async def _configure_default(db, **fields):
    from hub.db import seed_default_project

    # The in-memory test DB is not seeded by the app lifespan; without the
    # row an UPDATE silently changes nothing and the test passes vacuously.
    await seed_default_project(db)
    row = await repo.get_project_by_slug(db, "default")
    await repo.update_project(db, row["id"], **fields)
    await db.commit()


# ---- AC-1: configured fields reach the context ----


async def test_a_configured_default_project_yields_its_context(client: AsyncClient, db):
    await _configure_default(
        db,
        repo="mrPDA/openclaw-hub-standalone",
        workspace_path="/var/lib/openclaw-hub/workspaces/hub",
        default_branch="develop",
    )
    task_id = await _task_in_default(client)

    ctx = await project_git_context(db, task_id)

    assert ctx == {
        "repo": "/var/lib/openclaw-hub/workspaces/hub",
        "gh_repo": "mrPDA/openclaw-hub-standalone",
        "base_branch": "develop",
    }, "the owner configured these values; dropping any of them is the defect"


# ---- AC-2: an unconfigured default keeps the legacy empty context ----


async def test_an_unconfigured_default_project_keeps_legacy_empty_context(
    client: AsyncClient, db
):
    """A fresh installation has no values — no keys, and git_ops falls back
    to env. That field-wise omission is the whole legacy contract; the
    unconditional special case added nothing but the blindness."""
    await _configure_default(db, repo="", workspace_path="", default_branch="")
    task_id = await _task_in_default(client)

    ctx = await project_git_context(db, task_id)

    assert ctx == {}, (
        "an unconfigured default must contribute no keys, exactly as before"
    )


# ---- AC-3: the brief consumer actually sees the workspace ----


async def test_the_brief_sees_the_default_workspace_once_configured(
    client: AsyncClient, db, monkeypatch, tmp_path
):
    """End to end through the endpoint a reviewer calls: with a configured
    workspace the call-sites section must not claim there is none. Whatever
    unknown remains has to carry a different, honest reason."""
    workspace = tmp_path / "hub-clone"
    workspace.mkdir()
    await _configure_default(
        db,
        repo="mrPDA/openclaw-hub-standalone",
        workspace_path=str(workspace),
        default_branch="develop",
    )

    task_id = await _task_in_default(client)
    await client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": "dev", "kind": "status", "content": "Plan: wire"},
    )
    started = await client.post(
        f"/api/tasks/{task_id}/pair-start", json={"assigned_agent": "dev"}
    )
    assert started.status_code == 200, started.text

    from hub.integrations.noop import NoopGitOps
    from hub.integrations.registry import plugins

    monkeypatch.setattr(plugins, "git_ops", NoopGitOps())

    brief = (await client.get(f"/api/tasks/{task_id}/review-brief")).json()

    section = brief["call_sites"]
    assert "no workspace" not in (section.get("reason") or ""), (
        f"a configured workspace must be seen: {section!r}"
    )


# ---- AC-4: other projects are untouched ----


async def test_other_projects_context_is_untouched(client: AsyncClient, db):
    created = await repo.create_project(db, slug="calc-kids", name="Calc")
    project_id = created if isinstance(created, int) else created["id"]
    await repo.update_project(
        db,
        project_id,
        repo="mrPDA/calc-kids",
        workspace_path="/srv/ws/calc-kids",
        default_branch="master",
        status="active",
    )
    await db.commit()

    resp = await client.post(
        "/api/tasks", json={"title": "Calc epic", "task_type": "epic"}
    )
    epic_id = resp.json()["id"]
    await db.execute("UPDATE tasks SET project_id=? WHERE id=?", (project_id, epic_id))
    await db.commit()

    ctx = await project_git_context(db, epic_id)

    assert ctx == {
        "repo": "/srv/ws/calc-kids",
        "gh_repo": "mrPDA/calc-kids",
        "base_branch": "master",
    }
