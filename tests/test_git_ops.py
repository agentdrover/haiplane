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
