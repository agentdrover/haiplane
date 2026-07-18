"""Tests for hub.integrations.git_ops pair/PR helpers."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from hub.integrations.git_ops import GitOpsIntegration, PairBranchConflictError


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
