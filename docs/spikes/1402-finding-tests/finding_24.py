"""Стюард считает доставку только по pipeline_merges (#1214)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import aiosqlite
from httpx import AsyncClient

from hub import repository as repo
from hub.integrations.git_ops import GitOpsIntegration
from hub.services.steward_evidence import _dependency_fact
from tests.test_delivery_state import (
    _completed_blocker,
    _hermetic_git,
    _real_git_for,
)


def _squash_without_gate(tmp_path: Path) -> dict[str, Any]:
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _hermetic_git(remote, "init", "--bare", "-b", "develop")
    work = tmp_path / "clone"
    _hermetic_git(tmp_path, "clone", str(remote), str(work))
    (work / "base.py").write_text("base = 1\\n")
    _hermetic_git(work, "add", ".")
    _hermetic_git(work, "commit", "-m", "base")
    _hermetic_git(work, "push", "origin", "develop")
    _hermetic_git(work, "checkout", "-q", "-b", "task-1186/w", "develop")
    (work / "feature_1186.py").write_text("answer = 1186\\n")
    _hermetic_git(work, "add", ".")
    _hermetic_git(work, "commit", "-m", "work for #1186")
    sha = _hermetic_git(work, "rev-parse", "HEAD")
    _hermetic_git(work, "checkout", "-q", "develop")
    _hermetic_git(work, "merge", "--squash", "task-1186/w")
    _hermetic_git(work, "commit", "-m", "feat(task): work (#1186)")
    _hermetic_git(work, "push", "origin", "develop")
    _hermetic_git(work, "fetch", "origin")
    return {"repo": str(work), "base": "develop", "sha": sha}


async def test_steward_must_not_treat_squash_unknown_as_undelivered(
    client: AsyncClient,
    db: aiosqlite.Connection,
    tmp_path: Path,
    monkeypatch,
):
    squash = _squash_without_gate(tmp_path)
    real = GitOpsIntegration()
    assert (
        await real.is_ancestor(squash["repo"], squash["sha"], "origin/develop")
        is False
    ), "предусловие: сдаточный коммит не предок базы — как у squash"

    _real_git_for(monkeypatch, squash, "github")
    blocked_id = (await client.post("/api/tasks", json={"title": "waits"})).json()[
        "id"
    ]
    blocker = await _completed_blocker(client, db, squash["sha"])
    await repo.add_task_dependency(db, blocked_id, blocker["task_id"])
    await db.commit()

    merges = await db.execute_fetchall(
        "SELECT 1 FROM pipeline_merges WHERE task_id = ?",
        (blocker["task_id"],),
    )
    assert not merges, "предусловие: строки гейта нет"

    api = (await client.get(f"/api/tasks/{blocked_id}/dependencies")).json()
    api_row = api["blocked_by"][0]
    assert api_row["delivery_path"] == "unknown", api_row

    fact = await _dependency_fact(db, blocked_id)
    steward_row = fact.value["blocked_by"][0]
    assert steward_row.get("delivery_path") == "unknown", (
        "стюард прочитал отсутствие pipeline_merges как «не доставлено», "
        f"тогда как API говорит unknown: {fact.detail!r}, {steward_row!r}"
    )
