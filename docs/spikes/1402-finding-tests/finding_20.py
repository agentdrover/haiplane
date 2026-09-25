"""#1167: historical diff must not silently use today's project base."""

from __future__ import annotations

from hub import repository as repo
from hub.integrations.registry import plugins
from hub.services.steward_evidence import CorpusExclusion, build_historical_packet


class _CaptureBaseGitOps:
    def __init__(self) -> None:
        self.base: str | None = None

    async def commit_exists(self, repo_path, sha):
        return True

    async def branch_diff_paths(self, branch, base_branch=None, repo=None):
        self.base = base_branch
        return ["hub/services/steward_evidence.py"]


async def test_historical_diff_does_not_use_todays_base_when_ledger_has_none(
    db, monkeypatch
) -> None:
    today = "main-today"
    sha = "c" * 40
    project_id = await repo.create_project(
        db,
        slug="hist-silent-base",
        name="hist-silent-base",
        workspace_path="/tmp/ws",
        default_branch=today,
    )
    task_id = await repo.create_task(
        db,
        title="историческая сдача без базы в леджере",
        description="",
        runtime="auto",
        source="agent",
        assigned_agent="pda_claude",
        rationale="",
        status="done",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await repo.update_task(
        db,
        task_id,
        project_id=project_id,
        submission_generation=1,
        submission_sha=sha,
    )
    await db.commit()

    git = _CaptureBaseGitOps()
    monkeypatch.setattr(plugins, "git_ops", git)

    try:
        await build_historical_packet(db, task_id, 1, "2099-12-31 23:59:59")
    except CorpusExclusion:
        assert git.base != today
    else:
        assert git.base != today, (
            "historical diff silently used today's project base "
            "because the ledger had none"
        )
