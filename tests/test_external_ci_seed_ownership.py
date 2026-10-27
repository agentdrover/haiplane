"""The seed tells the hub's own pair from somebody's CI by path and bytes (#2027).

A file is the hub's when its path is the owned one and its bytes are a current
render of ANY file of the pair, not only of the branch being written: the sender
left on develop by an earlier seed is still ours (machine review 1220).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import aiosqlite

from hub import repository as repo
from hub import services
from hub.integrations.registry import plugins
from tests.test_external_ci_trust_path import (
    PRODUCER,
    SENDER,
    WORKFLOWS,
    _git,
    _repo_pair,
    _tree,
)


async def test_a_reseed_after_an_unread_default_branch_lays_the_sender_on_main(
    db: aiosqlite.Connection, tmp_path: Path, monkeypatch
):
    """Our own sender left on develop is not somebody's CI: the retry puts it on main.

    The first seed could not read the default branch and fell back to develop,
    so the whole pair landed there. Once main is known, the sender on develop is
    still byte-for-byte our render and must not block the push (review 1220).
    """
    from hub.services import external_ci_seed

    work, origin = _repo_pair(tmp_path)
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
    real_default = external_ci_seed.default_branch
    monkeypatch.setattr(
        external_ci_seed, "default_branch", lambda workspace, base: ("develop", False)
    )

    await services.provision_project(db, project_id)

    assert _tree(origin, "develop") == sorted([PRODUCER, SENDER, "haiplane-stale.yml"])
    assert _tree(origin, "main") == []
    monkeypatch.setattr(external_ci_seed, "default_branch", real_default)
    # The shared clone caught up with origin: its own copy now shows our files.
    _git(work, "pull", "--ff-only", "origin", "develop")
    assert (work / WORKFLOWS / SENDER).exists()

    second = await services.provision_project(db, project_id)

    assert _tree(origin, "main") == [SENDER]
    assert "none added" not in second["provision_detail"], second["provision_detail"]
    assert _git(work, "status", "--porcelain") == ""

    # The pair is whole now, the leftover sender on develop included: nothing more.
    heads = (_git(origin, "rev-parse", "develop"), _git(origin, "rev-parse", "main"))
    third = await services.provision_project(db, project_id)
    assert "the hub's own pair" in third["provision_detail"], third["provision_detail"]
    assert (
        _git(origin, "rev-parse", "develop"),
        _git(origin, "rev-parse", "main"),
    ) == heads


def test_our_sender_under_another_workflows_dir_is_somebody_else_s(tmp_path: Path):
    """Same name and bytes, other directory: not the hub's file, nothing written."""
    from hub.services import external_ci_seed

    sender = external_ci_seed._sender()
    other = tmp_path / ".gitverse" / "workflows"
    other.mkdir(parents=True)
    (other / SENDER).write_text(sender, encoding="utf-8")

    result = external_ci_seed._commit_files(
        str(tmp_path), "main", "hub-seed-x", {SENDER: sender}, "m", {SENDER: sender}
    )

    assert result.state == external_ci_seed.PRESENT, result
    assert f".gitverse/workflows/{SENDER}" in result.detail
    assert not (tmp_path / WORKFLOWS).exists()
