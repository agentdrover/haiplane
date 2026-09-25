"""#1239: пустой дифф без workspace — дыра, а не зелёная поверхность."""

from __future__ import annotations

import json

import aiosqlite
from hub import repository as repo
from hub.integrations.noop import NoopGitOps
from hub.integrations.registry import plugins
from hub.services.steward_evidence import build_evidence_packet
from hub.services.task_diff import NO_WORKSPACE, submission_diff


class _EmptyThreeDot(NoopGitOps):
    """Как GitOpsIntegration при repo=None: падает на _repo_root() и даёт []."""

    async def branch_diff_paths(self, branch, base_branch=None, repo=None):
        repo = repo or "/hub-clone-fallback"
        return []


async def test_empty_diff_without_workspace_is_not_a_measured_surface(
    db: aiosqlite.Connection, monkeypatch
) -> None:
    """Нет workspace_path — сторож обязан оставить дыру, а не present/ok.

    ``_resolve_branch_diff`` зовёт ``branch_diff_paths`` с ``repo=None``.
    Пустой трёхточечный дифф чужого клона приезжает как ``[]``.
    ``_guard_collapsed_diff`` на пустом workspace возвращает этот ``[]``
    без дыры, и судья видит ``within_declared=True``. Карточка в том же
    состоянии отвечает ``NO_WORKSPACE``, не пустым READ.
    """
    monkeypatch.setattr(plugins, "git_ops", _EmptyThreeDot())

    project_id = await repo.create_project(
        db,
        slug="no-workspace-surface",
        name="no workspace",
        workspace_path="",
        default_branch="develop",
        status="active",
    )
    task_id = await repo.create_task(
        db,
        title="сдача без копии",
        description="",
        runtime="auto",
        source="agent",
        assigned_agent="a",
        rationale="",
        status="review",
        auto_review=False,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await repo.update_task(
        db,
        task_id,
        project_id=project_id,
        branch="task-1239/no-ws",
        submission_sha="a" * 40,
        affected_areas=json.dumps(["hub/base.py"]),
        risk_class="R1",
    )
    await db.commit()

    card = await submission_diff(db, task_id)
    assert card["state"] == NO_WORKSPACE, card

    packet = await build_evidence_packet(db, task_id)
    assert packet is not None
    surface = packet.fact("diff_vs_areas")
    assert surface.is_absent, (
        "без workspace пустой дифф не измерен: "
        f"state={surface.state} within_declared="
        f"{(surface.value or {}).get('within_declared')!r} "
        f"detail={surface.detail!r}"
    )
