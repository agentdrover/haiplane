"""Tests for hub.integrations.git_ops pair/PR helpers."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from hub import config
from hub.integrations.git_ops import (
    GitOpsIntegration,
    PairBranchConflictError,
    WorkspaceBranchMismatchError,
    WorkspaceNotReadyError,
    _worktree_path,
)


@pytest.fixture
def git_ops() -> GitOpsIntegration:
    return GitOpsIntegration()


async def test_create_pr_uses_pair_base_branch(git_ops: GitOpsIntegration) -> None:
    with patch(
        "hub.integrations.git_ops._gh",
        new_callable=AsyncMock,
        return_value=(0, "https://github.com/org/repo/pull/99\n", ""),
    ) as mock_gh:
        pr = await git_ops.create_pr(
            7,
            "Example task",
            "desc",
            "task-7/example",
        )

    assert pr == 99
    args = mock_gh.await_args.args
    assert "--base" in args
    base_idx = args.index("--base")
    assert args[base_idx + 1] == "develop"


async def test_pair_prepare_branch_raises_on_failed_base_checkout(
    git_ops: GitOpsIntegration,
) -> None:
    async def fake_git(*cmd: str, **kwargs):
        if cmd[:2] == ("status", "--porcelain"):
            return 0, "", ""
        if cmd[:2] == ("branch", "--show-current"):
            return 0, "feature-x", ""
        if cmd[:2] == ("rev-parse", "--verify"):
            return 1, "", ""
        if cmd[:2] == ("checkout", "develop"):
            return 1, "", "pathspec 'develop' did not match"
        return 0, "", ""

    with (
        patch("hub.integrations.git_ops._git", side_effect=fake_git),
        patch("hub.integrations.git_ops._repo_root", return_value="/tmp/repo"),
    ):
        with pytest.raises(PairBranchConflictError, match="Failed to checkout base"):
            await git_ops.pair_prepare_branch(
                99,
                "Broken base",
                branch_slug="slug",
            )


async def test_pair_prepare_branch_raises_when_not_on_base_after_checkout(
    git_ops: GitOpsIntegration,
) -> None:
    calls: list[tuple[str, ...]] = []

    async def fake_git(*cmd: str, **kwargs):
        calls.append(cmd)
        if cmd[:2] == ("status", "--porcelain"):
            return 0, "", ""
        if cmd[:2] == ("branch", "--show-current"):
            if len(calls) == 2:
                return 0, "main", ""
            return 0, "develop", ""
        if cmd[:2] == ("rev-parse", "--verify"):
            return 1, "", ""
        if cmd[:2] == ("checkout", "develop"):
            return 0, "", ""
        if cmd[:2] == ("checkout", "-b"):
            return 0, "", ""
        if cmd[:3] == ("pull", "origin", "develop"):
            return 0, "", ""
        return 0, "", ""

    with (
        patch("hub.integrations.git_ops._git", side_effect=fake_git),
        patch("hub.integrations.git_ops._repo_root", return_value="/tmp/repo"),
    ):
        branch = await git_ops.pair_prepare_branch(
            5,
            "Good base",
            branch_slug="good",
        )

    assert branch == "task-5/good"


# --- clone_repo transport selection (#377) ---


def _clone_runner(responses):
    """Fake _run keyed by (command marker) → (rc, out, err)."""

    async def fake_run(*cmd, **kw):
        joined = " ".join(cmd)
        for marker, resp in responses:
            if marker in joined:
                return resp
        return (0, "", "")

    return fake_run


async def test_clone_repo_public_https_no_key(tmp_path):
    # AC-1: https ls-remote ok → clone over https, ssh never needed.
    from hub.integrations.git_ops import GitOpsIntegration

    calls = []

    async def fake_run(*cmd, **kw):
        calls.append(" ".join(cmd))
        return (0, "", "")

    with patch("hub.integrations.git_ops._run", side_effect=fake_run):
        ok, detail = await GitOpsIntegration().clone_repo(
            "mrPDA/pub-repo", str(tmp_path / "ws"), "main"
        )
    assert ok is True
    assert "https" in detail
    ls_calls = [c for c in calls if "ls-remote" in c]
    assert len(ls_calls) == 1
    assert "https://github.com/mrPDA/pub-repo.git" in ls_calls[0]
    clone_call = next(c for c in calls if " clone " in f" {c} ")
    assert "https://github.com/mrPDA/pub-repo.git" in clone_call


async def test_clone_repo_private_falls_back_to_ssh(tmp_path):
    # AC-2: https refused → ssh candidate succeeds.
    from hub.integrations.git_ops import GitOpsIntegration

    async def fake_run(*cmd, **kw):
        joined = " ".join(cmd)
        if "ls-remote" in joined and "https://" in joined:
            return (128, "", "fatal: could not read Username")
        return (0, "", "")

    with patch("hub.integrations.git_ops._run", side_effect=fake_run):
        ok, detail = await GitOpsIntegration().clone_repo(
            "mrPDA/priv-repo", str(tmp_path / "ws"), "master"
        )
    assert ok is True
    assert "ssh" in detail


async def test_clone_repo_both_transports_fail_lists_reasons(tmp_path):
    # AC-3: detail carries BOTH failed attempts.
    from hub.integrations.git_ops import GitOpsIntegration

    async def fake_run(*cmd, **kw):
        joined = " ".join(cmd)
        if "https://" in joined:
            return (128, "", "could not read Username")
        return (128, "", "Permission denied (publickey)")

    with patch("hub.integrations.git_ops._run", side_effect=fake_run):
        ok, detail = await GitOpsIntegration().clone_repo(
            "mrPDA/locked", str(tmp_path / "ws")
        )
    assert ok is False
    assert "could not read Username" in detail
    assert "Permission denied" in detail


async def test_clone_repo_explicit_url_untouched(tmp_path):
    # Explicit URLs bypass candidate substitution.
    from hub.integrations.git_ops import GitOpsIntegration

    calls = []

    async def fake_run(*cmd, **kw):
        calls.append(" ".join(cmd))
        return (0, "", "")

    with patch("hub.integrations.git_ops._run", side_effect=fake_run):
        ok, _ = await GitOpsIntegration().clone_repo(
            "git@gitlab.local:team/x.git", str(tmp_path / "ws")
        )
    assert ok is True
    assert all("github.com" not in c for c in calls)


# --- git_ops registration and default-workspace degradation (#378) ---


async def test_register_plugins_gitops_without_workspace(monkeypatch, tmp_path):
    # AC-1: git binary present, WORKSPACE_REPO_LINK absent → real GitOpsIntegration.
    from pathlib import Path

    from hub import config
    from hub.app import _register_plugins
    from hub.integrations.git_ops import GitOpsIntegration
    from hub.integrations.registry import plugins

    monkeypatch.setattr(
        config, "WORKSPACE_REPO_LINK", Path(tmp_path / "does-not-exist")
    )
    orig = plugins.git_ops
    try:
        _register_plugins()
        assert isinstance(plugins.git_ops, GitOpsIntegration)
    finally:
        plugins.git_ops = orig


async def test_pair_prepare_branch_readable_error_without_workspace(
    monkeypatch, tmp_path
):
    # AC-2: default workspace is not a git repo → readable conflict error,
    # not a raw 'not a git repository' traceback.
    from pathlib import Path

    import hub.integrations.git_ops as git_ops_module
    from hub.integrations.git_ops import GitOpsIntegration, PairBranchConflictError

    monkeypatch.setattr(
        git_ops_module, "WORKSPACE_REPO_LINK", Path(tmp_path / "empty-dir")
    )
    (tmp_path / "empty-dir").mkdir()

    with pytest.raises(PairBranchConflictError) as exc:
        await GitOpsIntegration().pair_prepare_branch(1, "Test task")
    assert "OPENCLAW_WORKSPACE_REPO" in str(exc.value)


async def test_create_branch_readable_error_without_workspace(monkeypatch, tmp_path):
    from pathlib import Path

    import hub.integrations.git_ops as git_ops_module
    from hub.integrations.git_ops import GitOpsIntegration

    monkeypatch.setattr(git_ops_module, "WORKSPACE_REPO_LINK", Path(tmp_path / "nope"))
    branch = await GitOpsIntegration().create_branch(2, "Another task")
    assert branch == ""  # degraded, no exception


async def test_pair_prepare_branch_explicit_repo_skips_default_guard(tmp_path):
    # AC-3: project workspaces (repo=...) are untouched by the guard.
    from unittest.mock import AsyncMock, patch

    from hub.integrations.git_ops import GitOpsIntegration

    with patch(
        "hub.integrations.git_ops._git",
        new_callable=AsyncMock,
        return_value=(0, "", ""),
    ):
        branch = await GitOpsIntegration().pair_prepare_branch(
            3, "Proj task", branch_slug="proj-task", repo=str(tmp_path)
        )
    assert branch == "task-3/proj-task"


# ---- Typed CI probe outcomes (#419) ----


@pytest.mark.parametrize(
    "rc, out, expected_outcome, expected_reason",
    [
        (0, '[{"name": "build", "state": "SUCCESS"}]', "pass", "checks_passed"),
        (0, '[{"name": "build", "state": "NEUTRAL"}]', "pass", "checks_passed"),
        (0, '[{"name": "build", "state": "FAILURE"}]', "fail", "checks_failed"),
        (0, '[{"name": "build", "state": "IN_PROGRESS"}]', "pending", "checks_running"),
        (
            0,
            '[{"name": "a", "state": "SUCCESS"}, {"name": "b", "state": "QUEUED"}]',
            "pending",
            "checks_running",
        ),
        (0, "[]", "absent", "no_checks"),
        (1, "", "unavailable", "gh_error"),
        (0, "not json at all", "unavailable", "invalid_json"),
        (0, '[{"name": "x", "state": "WEIRD"}]', "unavailable", "unknown_state"),
    ],
)
async def test_check_pr_ci_typed_outcomes(
    git_ops: GitOpsIntegration, rc, out, expected_outcome, expected_reason
):
    # AC-1 (#419): every observable gh response maps to a distinct outcome with
    # a stable, non-empty reason — running checks, an empty set, a gh error and
    # unparseable output are no longer all "pending".
    with patch(
        "hub.integrations.git_ops._gh",
        new_callable=AsyncMock,
        return_value=(rc, out, "boom" if rc else ""),
    ):
        result = await git_ops.check_pr_ci(42)
    assert result.outcome.value == expected_outcome
    assert result.reason == expected_reason
    assert result.reason  # never empty


# ---- Project repo context in CI/review calls (#420) ----


@pytest.mark.parametrize(
    "gh_repo",
    ["mrPDA/calc-kids", None],
)
async def test_check_pr_ci_targets_project_repo(git_ops: GitOpsIntegration, gh_repo):
    # AC-1/AC-2 (#420): --repo is the resolved project gh_repo (calc-kids), or
    # the default REPO_NAME when none is given; the workspace is the cwd.
    from hub.config import REPO_NAME

    with patch(
        "hub.integrations.git_ops._gh",
        new_callable=AsyncMock,
        return_value=(0, "[]", ""),
    ) as mock_gh:
        await git_ops.check_pr_ci(7, repo="/ws/proj", gh_repo=gh_repo)

    args = list(mock_gh.await_args.args)
    assert args[args.index("--repo") + 1] == (gh_repo or REPO_NAME)
    assert mock_gh.await_args.kwargs.get("repo") == "/ws/proj"


async def test_merge_pr_targets_project_repo(git_ops: GitOpsIntegration):
    # AC-2/AC-3 (#420): merge targets the resolved project repo and workspace,
    # never the global default.
    with patch(
        "hub.integrations.git_ops._gh",
        new_callable=AsyncMock,
        return_value=(0, "", ""),
    ) as mock_gh:
        await git_ops.merge_pr(
            7, 1, "feat: x", repo="/ws/proj", gh_repo="mrPDA/calc-kids"
        )

    args = list(mock_gh.await_args.args)
    assert args[args.index("--repo") + 1] == "mrPDA/calc-kids"
    assert mock_gh.await_args.kwargs.get("repo") == "/ws/proj"


async def test_get_ci_failure_logs_targets_project_repo(git_ops: GitOpsIntegration):
    # AC-2 (#420): failure-log lookup also uses the project repo.
    with patch(
        "hub.integrations.git_ops._gh",
        new_callable=AsyncMock,
        return_value=(0, "[]", ""),
    ) as mock_gh:
        await git_ops.get_ci_failure_logs(
            7, "task-x/b", repo="/ws/proj", gh_repo="mrPDA/calc-kids"
        )
    args = list(mock_gh.await_args.args)
    assert args[args.index("--repo") + 1] == "mrPDA/calc-kids"


# --- pair workspace auto-switch and restore (#451) ---


async def test_pair_prepare_branch_auto_switches_from_pushed_other_task(
    git_ops: GitOpsIntegration,
) -> None:
    calls: list[tuple[str, ...]] = []
    on_base = False

    async def fake_git(*cmd: str, **kwargs):
        nonlocal on_base
        calls.append(cmd)
        if cmd[:2] == ("status", "--porcelain"):
            return 0, "", ""
        if cmd[:2] == ("branch", "--show-current"):
            return 0, "develop" if on_base else "task-1/old-work", ""
        if (
            cmd[:2] == ("rev-parse", "--verify")
            and len(cmd) > 2
            and cmd[2].startswith("origin/task-1/old-work")
        ):
            return 0, "abc123", ""
        if (
            cmd[:2] == ("rev-list", "--count")
            and len(cmd) > 2
            and "origin/task-1/old-work..HEAD" in cmd[2]
        ):
            return 0, "0", ""
        if (
            cmd[:2] == ("rev-parse", "--verify")
            and len(cmd) > 2
            and cmd[2] == "task-2/new-work"
        ):
            return 1, "", ""
        if cmd[:2] == ("checkout", "develop"):
            on_base = True
            return 0, "", ""
        if cmd[:2] == ("checkout", "-b"):
            return 0, "", ""
        if cmd[:3] == ("pull", "origin", "develop"):
            return 0, "", ""
        return 0, "", ""

    with (
        patch("hub.integrations.git_ops._git", side_effect=fake_git),
        patch("hub.integrations.git_ops._repo_root", return_value="/tmp/repo"),
    ):
        branch = await git_ops.pair_prepare_branch(
            2,
            "Next task",
            branch_slug="new-work",
        )

    assert branch == "task-2/new-work"
    assert ("checkout", "develop") in calls


async def test_pair_prepare_branch_rejects_dirty_workspace(
    git_ops: GitOpsIntegration,
) -> None:
    async def fake_git(*cmd: str, **kwargs):
        if cmd[:2] == ("status", "--porcelain"):
            return 0, " M file.py", ""
        return 0, "", ""

    with (
        patch("hub.integrations.git_ops._git", side_effect=fake_git),
        patch("hub.integrations.git_ops._repo_root", return_value="/srv/ws"),
        patch("hub.integrations.git_ops._hostname", return_value="hub-server"),
    ):
        with pytest.raises(PairBranchConflictError) as exc:
            await git_ops.pair_prepare_branch(9, "Dirty")

    detail = exc.value.to_detail()
    assert detail["reason"] == "pair_branch_dirty"
    assert detail["workspace_path"] == "/srv/ws"
    assert detail["hostname"] == "hub-server"
    assert "hub_pair_start" in detail["hint"]


async def test_pair_prepare_branch_rejects_unpushed_other_task_branch(
    git_ops: GitOpsIntegration,
) -> None:
    async def fake_git(*cmd: str, **kwargs):
        if cmd[:2] == ("status", "--porcelain"):
            return 0, "", ""
        if cmd[:2] == ("branch", "--show-current"):
            return 0, "task-1/unpushed", ""
        if (
            cmd[:2] == ("rev-parse", "--verify")
            and len(cmd) > 2
            and cmd[2].startswith("origin/task-1/unpushed")
        ):
            return 1, "", ""
        return 0, "", ""

    with (
        patch("hub.integrations.git_ops._git", side_effect=fake_git),
        patch("hub.integrations.git_ops._repo_root", return_value="/srv/ws"),
        patch("hub.integrations.git_ops._hostname", return_value="prod"),
    ):
        with pytest.raises(PairBranchConflictError) as exc:
            await git_ops.pair_prepare_branch(2, "Blocked")

    detail = exc.value.to_detail()
    assert detail["reason"] == "pair_branch_unpushed"
    assert "git push" in detail["hint"]
    assert detail["workspace_path"] == "/srv/ws"


async def test_pair_restore_workspace_base_checks_out_develop(
    git_ops: GitOpsIntegration,
) -> None:
    calls: list[tuple[str, ...]] = []

    async def fake_git(*cmd: str, **kwargs):
        calls.append(cmd)
        if cmd[:2] == ("status", "--porcelain"):
            return 0, "", ""
        if cmd[:2] == ("branch", "--show-current"):
            return 0, "task-5/my-branch", ""
        if cmd[:2] == ("checkout", "develop"):
            return 0, "", ""
        return 0, "", ""

    with patch("hub.integrations.git_ops._git", side_effect=fake_git):
        ok = await git_ops.pair_restore_workspace_base(5, repo="/srv/ws")

    assert ok is True
    assert ("checkout", "develop") in calls


async def test_pair_restore_workspace_base_skips_dirty_tree(
    git_ops: GitOpsIntegration,
) -> None:
    async def fake_git(*cmd: str, **kwargs):
        if cmd[:2] == ("status", "--porcelain"):
            return 0, "?? tmp.txt", ""
        if cmd[:2] == ("branch", "--show-current"):
            return 0, "task-5/my-branch", ""
        return 0, "", ""

    with patch("hub.integrations.git_ops._git", side_effect=fake_git):
        ok = await git_ops.pair_restore_workspace_base(5, repo="/srv/ws")

    assert ok is False


# --- pair workspace: base-ahead guard + forward switch (#457) ---


async def test_pair_prepare_branch_rejects_base_ahead_of_origin(
    git_ops: GitOpsIntegration,
) -> None:
    # AC-3 (#457): local base ahead of origin/base → structured 422, no branch cut.
    async def fake_git(*cmd: str, **kwargs):
        if cmd[:2] == ("status", "--porcelain"):
            return 0, "", ""
        if cmd[:2] == ("branch", "--show-current"):
            return 0, "develop", ""
        if (
            cmd[:2] == ("rev-parse", "--verify")
            and len(cmd) > 2
            and cmd[2] == "task-7/new"
        ):
            return 1, "", ""  # target branch does not exist yet
        if (
            cmd[:2] == ("rev-parse", "--verify")
            and len(cmd) > 2
            and cmd[2].startswith("origin/develop")
        ):
            return 0, "abc123", ""  # origin/develop known
        if (
            cmd[:2] == ("rev-list", "--count")
            and len(cmd) > 2
            and "origin/develop..develop" in cmd[2]
        ):
            return 0, "3", ""  # local develop is 3 commits ahead
        return 0, "", ""

    with (
        patch("hub.integrations.git_ops._git", side_effect=fake_git),
        patch("hub.integrations.git_ops._repo_root", return_value="/srv/ws"),
        patch("hub.integrations.git_ops._hostname", return_value="prod"),
    ):
        with pytest.raises(PairBranchConflictError) as exc:
            await git_ops.pair_prepare_branch(7, "New", branch_slug="new")

    detail = exc.value.to_detail()
    assert detail["reason"] == "pair_base_ahead_of_origin"
    assert detail["workspace_path"] == "/srv/ws"
    assert "origin/develop..develop" in detail["hint"]


async def test_pair_prepare_branch_allows_base_in_sync_with_origin(
    git_ops: GitOpsIntegration,
) -> None:
    # Guard is silent when local base matches origin (count 0).
    async def fake_git(*cmd: str, **kwargs):
        if cmd[:2] == ("status", "--porcelain"):
            return 0, "", ""
        if cmd[:2] == ("branch", "--show-current"):
            return 0, "develop", ""
        if (
            cmd[:2] == ("rev-parse", "--verify")
            and len(cmd) > 2
            and cmd[2] == "task-8/new"
        ):
            return 1, "", ""
        if (
            cmd[:2] == ("rev-list", "--count")
            and len(cmd) > 2
            and "origin/develop..develop" in cmd[2]
        ):
            return 0, "0", ""
        return 0, "", ""

    with (
        patch("hub.integrations.git_ops._git", side_effect=fake_git),
        patch("hub.integrations.git_ops._repo_root", return_value="/srv/ws"),
    ):
        branch = await git_ops.pair_prepare_branch(8, "New", branch_slug="new")
    assert branch == "task-8/new"


async def test_pair_switch_to_task_branch_switches_from_base(
    git_ops: GitOpsIntegration,
) -> None:
    # AC-1 (#457): clean tree on base → checkout the task branch.
    calls: list[tuple[str, ...]] = []

    async def fake_git(*cmd: str, **kwargs):
        calls.append(cmd)
        if cmd[:2] == ("branch", "--show-current"):
            return 0, "develop", ""
        if cmd[:2] == ("status", "--porcelain"):
            return 0, "", ""
        return 0, "", ""

    with patch("hub.integrations.git_ops._git", side_effect=fake_git):
        ok = await git_ops.pair_switch_to_task_branch(5, "task-5/x", repo="/srv/ws")
    assert ok is True
    assert ("checkout", "task-5/x") in calls


async def test_pair_switch_to_task_branch_skips_dirty(
    git_ops: GitOpsIntegration,
) -> None:
    # AC-2 (#457): dirty tree → no switch, no data loss.
    async def fake_git(*cmd: str, **kwargs):
        if cmd[:2] == ("branch", "--show-current"):
            return 0, "develop", ""
        if cmd[:2] == ("status", "--porcelain"):
            return 0, " M file.py", ""
        return 0, "", ""

    with patch("hub.integrations.git_ops._git", side_effect=fake_git):
        ok = await git_ops.pair_switch_to_task_branch(5, "task-5/x", repo="/srv/ws")
    assert ok is False


async def test_pair_switch_to_task_branch_noop_off_base(
    git_ops: GitOpsIntegration,
) -> None:
    # Safety: never yank a different task's branch — only leave the base.
    calls: list[tuple[str, ...]] = []

    async def fake_git(*cmd: str, **kwargs):
        calls.append(cmd)
        if cmd[:2] == ("branch", "--show-current"):
            return 0, "task-9/other", ""
        return 0, "", ""

    with patch("hub.integrations.git_ops._git", side_effect=fake_git):
        ok = await git_ops.pair_switch_to_task_branch(5, "task-5/x", repo="/srv/ws")
    assert ok is False
    assert ("checkout", "task-5/x") not in calls


# --- origin reachability health-check (#455) ---


async def test_origin_reachable_true(git_ops: GitOpsIntegration) -> None:
    async def fake_git(*cmd: str, **kwargs):
        if cmd[:1] == ("ls-remote",):
            return 0, "abc123\trefs/heads/develop", ""
        return 0, "", ""

    with patch("hub.integrations.git_ops._git", side_effect=fake_git):
        assert await git_ops.origin_reachable(repo="/srv/ws") is True


async def test_origin_reachable_false(git_ops: GitOpsIntegration) -> None:
    async def fake_git(*cmd: str, **kwargs):
        if cmd[:1] == ("ls-remote",):
            return 128, "", "Could not read from remote repository"
        return 0, "", ""

    with patch("hub.integrations.git_ops._git", side_effect=fake_git):
        assert await git_ops.origin_reachable(repo="/srv/ws") is False


# --- worktree-per-task isolation (#459) ---


def _git_setup(repo):
    repo.mkdir()
    for args in (
        ("init", "-q", "-b", "develop"),
        ("config", "user.email", "t@example.com"),
        ("config", "user.name", "Test"),
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    (repo / "f.txt").write_text("x")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-qm", "init"], cwd=repo, check=True, capture_output=True
    )


def _current_branch(path):
    return subprocess.run(
        ["git", "branch", "--show-current"], cwd=path, capture_output=True, text=True
    ).stdout.strip()


async def test_worktree_parallel_isolation(tmp_path, git_ops):
    # AC-1 (#459): two prepares → two independent worktrees; main clone stays on base.
    repo = tmp_path / "main"
    _git_setup(repo)

    b1 = await git_ops.pair_prepare_worktree(
        1, "Task one", repo=str(repo), base_branch="develop"
    )
    b2 = await git_ops.pair_prepare_worktree(
        2, "Task two", repo=str(repo), base_branch="develop"
    )
    assert b1.startswith("task-1/") and b2.startswith("task-2/")

    wt1, wt2 = _worktree_path(1, str(repo)), _worktree_path(2, str(repo))
    assert os.path.isdir(wt1) and os.path.isdir(wt2)
    # Each worktree on its own branch, both alive at once, main clone untouched.
    assert _current_branch(wt1) == b1
    assert _current_branch(wt2) == b2
    assert _current_branch(repo) == "develop"


async def test_worktree_remove_clean(tmp_path, git_ops):
    # AC-2 (#459): cleanup removes a clean worktree; main clone stays on base.
    repo = tmp_path / "main"
    _git_setup(repo)
    await git_ops.pair_prepare_worktree(
        3, "Cleanup", repo=str(repo), base_branch="develop"
    )
    wt = _worktree_path(3, str(repo))
    assert os.path.isdir(wt)

    assert await git_ops.pair_remove_worktree(3, repo=str(repo)) is True
    assert not os.path.isdir(wt)
    assert _current_branch(repo) == "develop"


async def test_worktree_remove_skips_dirty(tmp_path, git_ops):
    # AC-3 (#459): a dirty worktree is not removed — no data loss.
    repo = tmp_path / "main"
    _git_setup(repo)
    await git_ops.pair_prepare_worktree(
        4, "Dirty", repo=str(repo), base_branch="develop"
    )
    wt = _worktree_path(4, str(repo))
    (Path(wt) / "wip.txt").write_text("unsaved")

    assert await git_ops.pair_remove_worktree(4, repo=str(repo)) is False
    assert os.path.isdir(wt)


async def test_worktree_reuse_and_stale(tmp_path, git_ops):
    # AC-4 (#459): re-prepare reuses the branch and prunes a stale registration.
    repo = tmp_path / "main"
    _git_setup(repo)
    b = await git_ops.pair_prepare_worktree(
        5, "Reuse", repo=str(repo), base_branch="develop"
    )
    wt = _worktree_path(5, str(repo))

    # Reuse: same call returns same branch, no error.
    assert (
        await git_ops.pair_prepare_worktree(
            5, "Reuse", repo=str(repo), base_branch="develop"
        )
        == b
    )

    # Stale: delete the worktree dir out from under git, then re-prepare recreates.
    shutil.rmtree(wt)
    b2 = await git_ops.pair_prepare_worktree(
        5, "Reuse", repo=str(repo), base_branch="develop"
    )
    assert b2 == b
    assert os.path.isdir(wt)
    assert _current_branch(wt) == b


async def test_worktree_reuse_dirty_guard(tmp_path, git_ops):
    # AC-3 / #459 review MEDIUM: switching a dirty reused worktree to another
    # branch is refused with a structural error — uncommitted work is preserved.
    repo = tmp_path / "main"
    _git_setup(repo)
    await git_ops.pair_prepare_worktree(
        6, "Task A", repo=str(repo), base_branch="develop", branch_slug="a"
    )
    wt = _worktree_path(6, str(repo))
    (Path(wt) / "wip.txt").write_text("unsaved")

    with pytest.raises(PairBranchConflictError) as exc:
        await git_ops.pair_prepare_worktree(
            6, "Task A", repo=str(repo), base_branch="develop", branch_slug="b"
        )
    detail = exc.value.to_detail()
    assert detail["reason"] == "pair_worktree_dirty"
    assert detail["workspace_path"] == wt
    assert (Path(wt) / "wip.txt").exists()  # data preserved


async def test_worktree_base_ahead_guard(tmp_path, git_ops):
    # #459 review MEDIUM: pair_prepare_worktree must reject cutting a new branch
    # when local base is ahead of origin/base (broken fetch → foreign commits).
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "develop", str(origin)],
        check=True,
        capture_output=True,
    )
    repo = tmp_path / "main"
    subprocess.run(
        ["git", "clone", str(origin), str(repo)], check=True, capture_output=True
    )
    for args in (
        ("config", "user.email", "t@example.com"),
        ("config", "user.name", "Test"),
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    (repo / "f.txt").write_text("x")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-qm", "init"], cwd=repo, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "push", "-q", "origin", "develop"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    # Local develop now diverges ahead of origin/develop (commit, no push).
    (repo / "g.txt").write_text("y")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-qm", "local-only"],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    with pytest.raises(PairBranchConflictError) as exc:
        await git_ops.pair_prepare_worktree(
            7, "Ahead", repo=str(repo), base_branch="develop", branch_slug="new"
        )
    assert exc.value.to_detail()["reason"] == "pair_base_ahead_of_origin"


async def test_worktree_reuse_switches_branch_cleanly(tmp_path, git_ops):
    # #459 review HIGH: reusing a clean worktree for a new slug must actually
    # create+switch the branch, not silently return one it never checked out.
    repo = tmp_path / "main"
    _git_setup(repo)
    await git_ops.pair_prepare_worktree(
        6, "Task", repo=str(repo), base_branch="develop", branch_slug="a"
    )
    wt = _worktree_path(6, str(repo))

    b = await git_ops.pair_prepare_worktree(
        6, "Task", repo=str(repo), base_branch="develop", branch_slug="b"
    )
    assert b == "task-6/b"
    assert _current_branch(wt) == "task-6/b"  # truly switched, no false success


async def test_worktree_reuse_base_ahead_guard(tmp_path, git_ops):
    # #459 review: the base-ahead guard on the REUSE path (new slug on an existing
    # worktree) must also fire, not just the fresh-worktree path.
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "develop", str(origin)],
        check=True,
        capture_output=True,
    )
    repo = tmp_path / "main"
    subprocess.run(
        ["git", "clone", str(origin), str(repo)], check=True, capture_output=True
    )
    for args in (
        ("config", "user.email", "t@example.com"),
        ("config", "user.name", "Test"),
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    (repo / "f.txt").write_text("x")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-qm", "init"], cwd=repo, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "push", "-q", "origin", "develop"],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    # Existing clean worktree on task-6/a.
    await git_ops.pair_prepare_worktree(
        6, "Task", repo=str(repo), base_branch="develop", branch_slug="a"
    )
    # Local develop now diverges ahead of origin/develop.
    (repo / "g.txt").write_text("y")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-qm", "local-only"],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    # Reuse path cutting a new branch task-6/b must hit the guard.
    with pytest.raises(PairBranchConflictError) as exc:
        await git_ops.pair_prepare_worktree(
            6, "Task", repo=str(repo), base_branch="develop", branch_slug="b"
        )
    assert exc.value.to_detail()["reason"] == "pair_base_ahead_of_origin"


async def test_worktree_remove_unregistered_is_idempotent(tmp_path, git_ops):
    # #459 review: removing a worktree for a task that has none prunes and
    # returns True (idempotent cleanup), never raises or returns False.
    repo = tmp_path / "main"
    _git_setup(repo)
    assert await git_ops.pair_remove_worktree(99, repo=str(repo)) is True


# --- Shared-workspace safety on the headless path (#361) -------------------
#
# These run against a real git repository on purpose. The defects they cover
# are destructive filesystem behaviour, and a mocked _git would have let the
# original code "pass" while still deleting a person's work.


def _run_git(*args: str, cwd) -> None:
    import subprocess

    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


def _seed_repo(tmp_path):
    repo = tmp_path / "shared-workspace"
    repo.mkdir()
    _run_git("git", "init", "-b", "main", cwd=repo)
    _run_git("git", "config", "user.email", "t@example.com", cwd=repo)
    _run_git("git", "config", "user.name", "t", cwd=repo)
    (repo / "app.py").write_text("original\n")
    _run_git("git", "add", "-A", cwd=repo)
    _run_git("git", "commit", "-m", "init", cwd=repo)
    return repo


async def test_create_branch_refuses_dirty_workspace_instead_of_cleaning(
    git_ops: GitOpsIntegration, tmp_path
) -> None:
    # #361 I2. Reproduced before the fix: the tracked edit was reverted to
    # 'original' and the untracked file was deleted outright, because
    # create_branch ran `checkout .` + `clean -fd` on a dirty tree.
    repo = _seed_repo(tmp_path)
    (repo / "app.py").write_text("a person is editing this\n")
    (repo / "notes.txt").write_text("draft, not staged yet\n")

    # Refusal must RAISE, not return "": an empty string also means "no git
    # integration configured", and collapsing the two turned an unconfigured
    # hub into a blocked one (caught by test_start_task_dispatch_failure_*).
    with pytest.raises(WorkspaceNotReadyError):
        await git_ops.create_branch(1, "headless task", repo=str(repo))

    assert (repo / "app.py").read_text() == "a person is editing this\n"
    assert (repo / "notes.txt").exists()


async def test_create_branch_still_works_on_clean_workspace(
    git_ops: GitOpsIntegration, tmp_path
) -> None:
    # The refusal must not cost the normal headless flow.
    repo = _seed_repo(tmp_path)
    # A real workspace has the base branch the PRs target; since #362 that is
    # the branch create_branch cuts from, so the fixture must model it.
    _run_git("git", "checkout", "-b", config.PAIR_BASE_BRANCH, cwd=repo)
    _run_git("git", "checkout", "main", cwd=repo)

    branch = await git_ops.create_branch(7, "normal task", repo=str(repo))

    assert branch == "task-7/normal-task"
    import subprocess

    current = subprocess.run(
        ["git", "branch", "--show-current"], cwd=repo, capture_output=True, text=True
    ).stdout.strip()
    assert current == branch


async def test_auto_commit_refuses_when_checkout_left_a_foreign_branch(
    git_ops: GitOpsIntegration, tmp_path
) -> None:
    # #361 I1, second half. The done-pipeline checks out the task branch and
    # never inspects the result; a failed checkout used to leave the commit
    # landing on whatever branch was still current.
    repo = _seed_repo(tmp_path)
    _run_git("git", "checkout", "-b", "someone-elses-branch", cwd=repo)
    (repo / "task_file.py").write_text("task work\n")

    # Raises rather than returning False, which auto_commit also returns for
    # the innocent "nothing to commit" — the collapse made the guard inert.
    with pytest.raises(WorkspaceBranchMismatchError):
        await git_ops.auto_commit(
            2, title="work", repo=str(repo), expected_branch="task-2/work"
        )

    import subprocess

    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=repo, capture_output=True, text=True
    ).stdout
    assert log.count("\n") == 1, "no commit may land on the foreign branch"


async def test_auto_commit_commits_on_the_expected_branch(
    git_ops: GitOpsIntegration, tmp_path
) -> None:
    repo = _seed_repo(tmp_path)
    _run_git("git", "checkout", "-b", "task-3/work", cwd=repo)
    (repo / "task_file.py").write_text("task work\n")

    committed = await git_ops.auto_commit(
        3, title="work", repo=str(repo), expected_branch="task-3/work"
    )

    assert committed is True
    import subprocess

    files = subprocess.run(
        ["git", "show", "--name-only", "--format=", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
    ).stdout.split()
    assert files == ["task_file.py"]


async def test_pair_prepare_branch_refuses_when_head_is_not_on_base(
    git_ops: GitOpsIntegration,
) -> None:
    """The honest version of a test that used to assert the opposite.

    test_pair_prepare_branch_raises_when_not_on_base_after_checkout is named for
    this guard but simulates BEING on base and asserts the happy path. Deleting
    the guard left every test green — found by mutating the pair path before
    copying it to the headless one.
    """

    async def fake_git(*cmd: str, **kwargs):
        if cmd[:2] == ("status", "--porcelain"):
            return 0, "", ""
        if cmd[:2] == ("branch", "--show-current"):
            # checkout reports success but HEAD is somewhere else entirely
            return 0, "some-other-branch", ""
        if cmd[:2] == ("rev-parse", "--verify"):
            return 1, "", ""
        return 0, "", ""

    with (
        patch("hub.integrations.git_ops._git", side_effect=fake_git),
        patch("hub.integrations.git_ops._repo_root", return_value="/tmp/repo"),
    ):
        with pytest.raises(PairBranchConflictError, match="Expected base branch"):
            await git_ops.pair_prepare_branch(11, "Wrong branch", branch_slug="slug")


async def test_create_branch_refuses_when_head_is_not_on_base(
    git_ops: GitOpsIntegration,
) -> None:
    """The headless twin of the guard whose pair-mode version had no test.

    On a real repository a successful `git checkout base` always leaves HEAD on
    base, so this divergence is only reachable through a mock — which is
    precisely why the pair-mode guard went untested and why mutating it changed
    nothing. Caught here by mutating my own guard right after writing it.
    """

    async def fake_git(*cmd: str, **kwargs):
        if cmd[:2] == ("status", "--porcelain"):
            return 0, "", ""
        if cmd[:2] == ("checkout", "main"):
            return 0, "", ""  # reports success...
        if cmd[:2] == ("branch", "--show-current"):
            return 0, "somewhere-else", ""  # ...but HEAD is elsewhere
        return 0, "", ""

    with patch("hub.integrations.git_ops._git", side_effect=fake_git):
        with pytest.raises(WorkspaceNotReadyError):
            await git_ops.create_branch(42, "task", repo="/tmp/repo")


# --- #361 AC-1: commit-scope gate ---


def test_parse_porcelain_handles_renames_and_quotes() -> None:
    from hub.commit_scope import parse_porcelain_paths

    out = parse_porcelain_paths(
        '?? notes.txt\n M hub/app.py\nR  old/a.py -> new/b.py\n M "has space.py"\n'
    )
    assert out == ["notes.txt", "hub/app.py", "old/a.py", "new/b.py", "has space.py"]


def test_foreign_paths_flags_only_what_is_outside_declared_areas() -> None:
    from hub.commit_scope import foreign_paths

    areas = ["hub/integrations/git_ops.py", "tests"]
    dirty = ["hub/integrations/git_ops.py", "tests/test_git_ops.py", "notes.txt"]

    assert foreign_paths(dirty, areas) == ["notes.txt"]


def test_foreign_paths_reports_nothing_when_no_areas_declared() -> None:
    """Absence of a declared scope is "cannot check", not "checked and clean".

    The caller must say so out loud; this function only reports what it can
    actually compare.
    """
    from hub.commit_scope import foreign_paths

    assert foreign_paths(["anything.py"], []) == []


def test_foreign_paths_does_not_match_a_partial_directory_name() -> None:
    from hub.commit_scope import foreign_paths

    # "hub" must not swallow "hubris/"; prefix matching without the separator
    # would let a foreign file pass as in-scope.
    assert foreign_paths(["hubris/x.py"], ["hub"]) == ["hubris/x.py"]


async def test_dirty_paths_reads_the_real_worktree(
    git_ops: GitOpsIntegration, tmp_path
) -> None:
    repo = _seed_repo(tmp_path)
    (repo / "app.py").write_text("edited\n")
    (repo / "brand_new.txt").write_text("new\n")

    paths = await git_ops.dirty_paths(repo=str(repo))

    assert sorted(paths) == ["app.py", "brand_new.txt"]


# --- #362 I4: branch base must equal PR target ---


def test_resolve_base_is_the_only_default_for_branch_and_pr() -> None:
    """create_branch defaulted to "main" while create_pr defaulted to develop.

    Two independent defaults that must agree is the defect; asserting they are
    equal by value would let them drift apart again, so this asserts both call
    sites read the SAME function.
    """
    import inspect

    from hub.integrations.git_ops import _resolve_base

    assert _resolve_base(None) == config.PAIR_BASE_BRANCH
    assert _resolve_base("release/x") == "release/x"

    src = inspect.getsource(GitOpsIntegration.create_branch)
    assert "_resolve_base(base_branch)" in src
    assert '"main"' not in src, "no second default may reappear in create_branch"
    assert "_resolve_base(base_branch)" in inspect.getsource(
        GitOpsIntegration.create_pr
    )


async def test_headless_branch_is_cut_from_the_pr_target(
    git_ops: GitOpsIntegration, tmp_path
) -> None:
    """#362 I4, reproduced before the fix on a live repository.

    The default project's git context is empty, so base_branch is never passed
    and the old default applied every time — not in a corner case.
    """
    repo = _seed_repo(tmp_path)  # seeds main
    _run_git("git", "checkout", "-b", config.PAIR_BASE_BRANCH, cwd=repo)
    (repo / "landed_in_base.py").write_text("work that is already in the base\n")
    _run_git("git", "add", "-A", cwd=repo)
    _run_git("git", "commit", "-m", "base moves ahead of main", cwd=repo)
    _run_git("git", "checkout", "main", cwd=repo)

    branch = await git_ops.create_branch(362, "headless work", repo=str(repo))

    assert branch
    import subprocess

    merge_base = subprocess.run(
        ["git", "merge-base", config.PAIR_BASE_BRANCH, "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
    ).stdout.strip()
    base_head = subprocess.run(
        ["git", "rev-parse", config.PAIR_BASE_BRANCH],
        cwd=repo,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert merge_base == base_head, "task branch must start at the PR target's tip"
    assert (repo / "landed_in_base.py").exists(), "task must see work already in base"


async def test_stale_base_no_longer_conflicts_on_untouched_lines(
    git_ops: GitOpsIntegration, tmp_path
) -> None:
    """The concrete damage: a conflict on a line the task never edited.

    Before the fix the task branch was cut from main, so a change the base made
    to line 1 collided with the task branch's *unmodified* line 1 — the merge
    base was too old to tell they were the same. Reproduced by hand before
    writing this test.
    """
    import subprocess

    repo = _seed_repo(tmp_path)
    (repo / "shared.py").write_text("line1\nline2\n")
    _run_git("git", "add", "-A", cwd=repo)
    _run_git("git", "commit", "-m", "shared file", cwd=repo)
    _run_git("git", "checkout", "-b", config.PAIR_BASE_BRANCH, cwd=repo)
    (repo / "shared.py").write_text("line1 REFACTORED IN BASE\nline2\n")
    _run_git("git", "commit", "-am", "base refactors line 1", cwd=repo)
    _run_git("git", "checkout", "main", cwd=repo)

    branch = await git_ops.create_branch(362, "task work", repo=str(repo))
    (repo / "shared.py").write_text("line1 REFACTORED IN BASE\nline2 CHANGED BY TASK\n")
    _run_git("git", "commit", "-am", "task edits line 2 only", cwd=repo)

    _run_git("git", "checkout", config.PAIR_BASE_BRANCH, cwd=repo)
    merged = subprocess.run(
        ["git", "merge", "--no-commit", "--no-ff", branch],
        cwd=repo,
        capture_output=True,
        text=True,
    )

    assert merged.returncode == 0, (
        "merging the task branch into its PR target must not conflict on a line "
        f"the task never touched:\n{merged.stdout}{merged.stderr}"
    )


async def test_create_branch_refuses_when_the_base_branch_is_missing(
    git_ops: GitOpsIntegration, tmp_path
) -> None:
    """Behaviour change introduced by #362, deliberate and covered here.

    Aligning the branch base with the PR target means a repository without that
    base can no longer be branched from. Falling back to another branch is
    exactly the defect being fixed — the work would be cut from one base and
    proposed against another — so this refuses, and the message names the
    branch so the operator can create it.
    """
    repo = _seed_repo(tmp_path)  # main only, no PAIR_BASE_BRANCH

    with pytest.raises(WorkspaceNotReadyError) as excinfo:
        await git_ops.create_branch(9, "task", repo=str(repo))

    assert config.PAIR_BASE_BRANCH in str(excinfo.value)
    assert str(repo) in str(excinfo.value)


def test_no_second_base_default_survives_anywhere() -> None:
    """The set check from the review harness, as an executable rule.

    Fixing create_branch's "main" was not enough: enumerating every place that
    computed a base default found four more — two literal "develop" parameter
    defaults in git_ops and a duplicated expression in orchestration. A claim
    that the default lives in one place has to be enforced, not asserted.
    """
    import re
    from pathlib import Path

    src = Path("hub/integrations/git_ops.py").read_text()
    # _resolve_base itself is the one legitimate reference to the config value.
    body = src.replace("    return base_branch or PAIR_BASE_BRANCH\n", "")
    assert "base_branch or PAIR_BASE_BRANCH" not in body
    assert not re.search(r'base_branch: str = "develop"', src)

    # Machine review caught this test being green for the wrong reason: it only
    # looked for the two spellings the fix happened to remove, so squash_branch
    # — which named its base "origin/main" inline, with no base_branch
    # parameter to grep for — stayed invisible while the docstring claimed no
    # second default survived anywhere. A base named by literal counts.
    for func in (
        "squash_branch",
        "create_branch",
        "create_pr",
        "pull_main",
        "delete_branch",
    ):
        start = src.index(f"async def {func}(")
        end = src.index("\n    async def ", start + 1)
        # Strip the docstring: prose is allowed to discuss the literal, code is not.
        body = re.sub(r'"""(?:.|\n)*?"""', "", src[start:end])
        # #552 added pull_main and delete_branch, which named the base as plain
        # "main" rather than "origin/main" — the shape the guard knew about.
        # A guard written alongside its fix only knows the spellings that fix
        # removed, so it has to be re-checked against members it never covered.
        for literal in ('"origin/main"', '"main"'):
            assert literal not in body, (
                f"{func} must resolve its base, not name a branch outright"
            )

    orch = Path("hub/services/orchestration.py").read_text()
    assert "PAIR_BASE_BRANCH" not in orch, (
        "orchestration must ask git_ops for the base default, not recompute it"
    )


async def test_squash_measures_from_the_branchs_own_base(tmp_path) -> None:
    """#362, found by machine review: the squash point is a third base.

    squash_branch is called on every headless task by the done-pipeline and is
    followed by a force-push, yet every test in the suite replaced it with a
    mock — its git logic had never executed. Moving the branch cut to the
    integration branch while the squash still reckoned from origin/main
    re-created the exact stale-merge-base damage this task set out to remove.
    Reproduced by hand on a real repository before this test was written.
    """
    import subprocess

    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True
    )
    repo = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(origin), str(repo)], check=True)
    _run_git("git", "config", "user.email", "t@example.com", cwd=repo)
    _run_git("git", "config", "user.name", "t", cwd=repo)

    (repo / "app.py").write_text("v1\n")
    _run_git("git", "add", "-A", cwd=repo)
    _run_git("git", "commit", "-m", "init main", cwd=repo)
    _run_git("git", "push", "-q", "origin", "main", cwd=repo)

    # The integration branch moves ahead of main — the normal state here.
    _run_git("git", "checkout", "-b", config.PAIR_BASE_BRANCH, cwd=repo)
    (repo / "landed.py").write_text("work already merged into the base\n")
    _run_git("git", "add", "-A", cwd=repo)
    _run_git("git", "commit", "-m", "base gains a commit", cwd=repo)
    _run_git("git", "push", "-q", "origin", config.PAIR_BASE_BRANCH, cwd=repo)

    git_ops = GitOpsIntegration()
    branch = await git_ops.create_branch(362, "task work", repo=str(repo))
    for n in (1, 2):
        (repo / f"task{n}.py").write_text(f"task commit {n}\n")
        _run_git("git", "add", "-A", cwd=repo)
        _run_git("git", "commit", "-m", f"task {n}", cwd=repo)

    assert await git_ops.squash_branch(362, "task work", branch, repo=str(repo))

    base_tip = subprocess.run(
        ["git", "rev-parse", f"origin/{config.PAIR_BASE_BRANCH}"],
        cwd=repo,
        capture_output=True,
        text=True,
    ).stdout.strip()
    parent = subprocess.run(
        ["git", "rev-parse", "HEAD^"], cwd=repo, capture_output=True, text=True
    ).stdout.strip()
    assert parent == base_tip, "the squashed commit must sit directly on the base"

    changed = subprocess.run(
        ["git", "diff", "--name-only", f"origin/{config.PAIR_BASE_BRANCH}...HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
    ).stdout.split()
    assert sorted(changed) == ["task1.py", "task2.py"], (
        f"the PR must show the task's work only, not the base's own commits: {changed}"
    )


# --- #363 I5: a hung subprocess must not escape, and must not survive ---


async def test_run_returns_a_controlled_error_on_timeout() -> None:
    """#363 I5. This used to raise straight out of _run.

    The poller wraps its whole tick in one try/except, so the escaping
    TimeoutError abandoned every later stage of that tick — and did so again
    every tick until the cause cleared.
    """
    import time

    from hub.integrations.git_ops import _TIMEOUT_RC, _run

    started = time.monotonic()
    rc, out, err = await _run("sleep", "30", timeout=1)
    elapsed = time.monotonic() - started

    assert rc == _TIMEOUT_RC, "a timeout must be reported, not raised"
    assert out == ""
    assert "timed out after 1s" in err and "sleep" in err
    assert elapsed < 10, f"timeout took {elapsed:.1f}s — it waited for the command"


async def test_run_kills_the_process_it_gave_up_on() -> None:
    """asyncio cancels the read, not the process — the child outlived us.

    Reproduced before the fix on a real subprocess: the command was still
    running after _run returned. The shape here is deliberate. A one-command
    ``sh -c`` execs itself away, taking the marker out of the command line and
    leaving nothing for pgrep to find — that first draft passed with the kill
    removed. Two statements keep the shell alive as a parent, and the unique
    sleep duration identifies the grandchild, so this fails both when the kill
    is dropped and when start_new_session is, which downgrades the group kill
    to a single-pid kill that never reaches the grandchild.
    """
    import asyncio
    import subprocess
    import uuid

    from hub.integrations.git_ops import _run

    marker = f"hub-probe-{uuid.uuid4().hex[:8]}"
    duration = f"30.{uuid.uuid4().int % 9000 + 1000}"

    def survivors() -> list[str]:
        out = subprocess.run(
            ["pgrep", "-f", f"sleep {duration}"], capture_output=True, text=True
        ).stdout.split()
        return [p for p in out if p.strip()]

    import time

    started = time.monotonic()
    rc, _, _ = await _run("sh", "-c", f"sleep {duration}; : {marker}", timeout=1)
    elapsed = time.monotonic() - started
    assert rc != 0
    # A timeout that still waits for the command is not a timeout. Without
    # start_new_session the group kill refuses to fire — it would signal the
    # hub's own group — and the fallback reaches only the shell; the orphaned
    # grandchild keeps the inherited pipes open, so the reap blocks for the
    # command's full duration. Measured at 30s against 1s here.
    assert elapsed < 10, f"timeout took {elapsed:.1f}s — it waited for the command"

    for _ in range(20):  # the kill is a signal; give the reaper a moment
        if not survivors():
            break
        await asyncio.sleep(0.1)

    left = survivors()
    for pid in left:  # never leave a stray behind, even when asserting
        subprocess.run(["kill", "-9", pid], capture_output=True)
    assert not left, f"process survived the timeout: {left}"


# --- #552: post-merge tidying works on the task's base, not on "main" ---


def _seed_repo_with_origin(tmp_path, *, with_main: bool):
    """A clone whose base is PAIR_BASE_BRANCH, optionally without main at all.

    A repository with no main is not exotic: it is what any project gets whose
    integration branch is the only long-lived one, and it is the state that
    exposed both defects here.
    """
    import subprocess

    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", config.PAIR_BASE_BRANCH, str(origin)],
        check=True,
    )
    repo = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(origin), str(repo)], check=True)
    _run_git("git", "config", "user.email", "t@example.com", cwd=repo)
    _run_git("git", "config", "user.name", "t", cwd=repo)
    _run_git("git", "checkout", "-q", "-b", config.PAIR_BASE_BRANCH, cwd=repo)
    (repo / "app.py").write_text("v1\n")
    _run_git("git", "add", "-A", cwd=repo)
    _run_git("git", "commit", "-m", "init", cwd=repo)
    _run_git("git", "push", "-q", "origin", config.PAIR_BASE_BRANCH, cwd=repo)
    if with_main:
        _run_git("git", "branch", "main", cwd=repo)
    return repo


async def test_pull_main_returns_the_workspace_to_the_task_base(
    git_ops: GitOpsIntegration, tmp_path
) -> None:
    """#552, reproduced before the fix on a repository whose base is not main.

    The clone was left sitting on the task branch, so the next task started
    from the wrong place. Every mention of pull_main in the suite was a mock —
    its git logic had never run.
    """
    import subprocess

    repo = _seed_repo_with_origin(tmp_path, with_main=False)
    _run_git("git", "checkout", "-q", "-b", "task-1/work", cwd=repo)
    (repo / "w.py").write_text("task work\n")
    _run_git("git", "add", "-A", cwd=repo)
    _run_git("git", "commit", "-m", "work", cwd=repo)

    assert await git_ops.pull_main(repo=str(repo))

    current = subprocess.run(
        ["git", "branch", "--show-current"], cwd=repo, capture_output=True, text=True
    ).stdout.strip()
    assert current == config.PAIR_BASE_BRANCH


async def test_delete_branch_removes_the_branch_and_says_so(
    git_ops: GitOpsIntegration, tmp_path
) -> None:
    """It could not step off the branch it was asked to delete, so git refused
    and the branches piled up — while the log claimed success regardless."""
    import subprocess

    repo = _seed_repo_with_origin(tmp_path, with_main=False)
    _run_git("git", "checkout", "-q", "-b", "task-2/work", cwd=repo)
    (repo / "w.py").write_text("task work\n")
    _run_git("git", "add", "-A", cwd=repo)
    _run_git("git", "commit", "-m", "work", cwd=repo)

    assert await git_ops.delete_branch("task-2/work", repo=str(repo)) is True

    gone = subprocess.run(
        ["git", "rev-parse", "--verify", "task-2/work"], cwd=repo, capture_output=True
    ).returncode
    assert gone != 0, "the branch must actually be gone"


async def test_delete_branch_reports_failure_instead_of_logging_success(
    git_ops: GitOpsIntegration, tmp_path
) -> None:
    """The old code logged "Deleted local branch" whatever happened."""
    repo = _seed_repo_with_origin(tmp_path, with_main=False)

    assert await git_ops.delete_branch("never-existed", repo=str(repo)) is False


async def test_pull_main_refuses_when_it_cannot_reach_the_base(
    git_ops: GitOpsIntegration, tmp_path
) -> None:
    """A workspace still on someone else's branch must be distinguishable from
    one returned to base. The old code discarded the checkout rc entirely."""
    repo = _seed_repo_with_origin(tmp_path, with_main=False)
    _run_git("git", "checkout", "-q", "-b", "someones-branch", cwd=repo)
    (repo / "app.py").write_text("their uncommitted edit\n")
    _run_git("git", "commit", "-am", "their commit", cwd=repo)
    (repo / "app.py").write_text("dirty edit that blocks the checkout\n")

    with pytest.raises(WorkspaceNotReadyError) as excinfo:
        await git_ops.pull_main(repo=str(repo))

    assert config.PAIR_BASE_BRANCH in str(excinfo.value)


async def test_squash_branch_refuses_when_it_cannot_check_out_the_branch(
    git_ops: GitOpsIntegration, tmp_path
) -> None:
    """Everything after the checkout rewrites history — on whatever branch stayed.

    The obvious version of this test, squashing a branch that does not exist,
    is green with the guard removed: merge-base fails for the same reason and
    the return value is False either way. Verified by mutation. It has to be a
    state where the checkout fails while everything after it would have
    succeeded — a dirty tree blocking the switch, and a real branch to squash.
    """
    import subprocess

    repo = _seed_repo_with_origin(tmp_path, with_main=False)
    _run_git("git", "checkout", "-q", "-b", "task-9/work", cwd=repo)
    for n in (1, 2):
        (repo / f"t{n}.py").write_text(f"task {n}\n")
        _run_git("git", "add", "-A", cwd=repo)
        _run_git("git", "commit", "-m", f"task {n}", cwd=repo)

    _run_git("git", "checkout", "-q", "-b", "someones-branch", cwd=repo)
    (repo / "app.py").write_text("their committed change\n")
    _run_git("git", "commit", "-am", "their commit", cwd=repo)
    (repo / "app.py").write_text("their uncommitted edit blocks the switch\n")

    def head() -> str:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True
        ).stdout.strip()

    def current() -> str:
        return subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=repo,
            capture_output=True,
            text=True,
        ).stdout.strip()

    before = head()
    assert await git_ops.squash_branch(9, "t", "task-9/work", repo=str(repo)) is False
    assert head() == before, "reset --soft must not run on the branch that stayed"
    assert current() == "someones-branch"
