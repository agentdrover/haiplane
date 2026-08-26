"""pair-start tells unpushed work from an unfetched ref (#954).

The gate asked one question — "does ``origin/<branch>`` exist in this clone?"
— and answered two different ones with it. On 25.08 the start of #953 was
blocked by the branch of #951: zero commits of its own, already on the remote,
simply never fetched into the shared clone. The message claimed commits were
at risk of being lost; none existed.

Since #966 the same false reading no longer blocks — it makes the hub PUSH a
stranger's branch on a premise that is not true. Both readings come from the
same missing question, so both are checked here.
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, patch

from hub.integrations.git_ops import GitOpsIntegration, PairBranchConflictError


@pytest.fixture
def git_ops() -> GitOpsIntegration:
    return GitOpsIntegration()


def _clone(
    calls: list[tuple[str, ...]],
    *,
    current: str = "task-1/foreign",
    on_remote: bool = True,
    fetched: bool = False,
    own_commits: int = 0,
    push_rc: int = 0,
):
    """A shared clone parked on another task's branch.

    ``on_remote`` — the branch exists on the server; ``fetched`` — this clone
    already knows it (``refs/remotes/origin/<branch>``). The pair is the whole
    point: the two are independent, and the gate used to conflate them.
    """
    known = {"fetched": fetched}

    async def fake_git(*cmd: str, **kwargs):
        calls.append(cmd)
        if cmd[:2] == ("status", "--porcelain"):
            return 0, "", ""
        if cmd[:2] == ("branch", "--show-current"):
            return 0, current, ""
        if cmd[:2] == ("rev-parse", "--verify") and cmd[2].startswith(
            f"origin/{current}"
        ):
            return (0, "abc123", "") if known["fetched"] else (1, "", "")
        if cmd[:1] == ("fetch",):
            if on_remote:
                known["fetched"] = True
                return 0, "", ""
            return 1, "", f"couldn't find remote ref {current}"
        if cmd[:2] == ("rev-list", "--count"):
            return 0, str(own_commits), ""
        if cmd[:1] == ("push",):
            return push_rc, "", "" if push_rc == 0 else "Permission denied (publickey)"
        return 0, "", ""

    return fake_git


def _pushes(calls: list[tuple[str, ...]]) -> list[tuple[str, ...]]:
    return [c for c in calls if c[:1] == ("push",)]


async def test_missing_upstream_is_not_unpushed(git_ops: GitOpsIntegration) -> None:
    """AC-1: on the remote, no commits of its own, just not fetched here.

    Nothing is at risk, so nothing is done to the branch — and above all the
    start of an unrelated task is not made to wait on it.
    """
    calls: list[tuple[str, ...]] = []
    notify = AsyncMock()

    with (
        patch(
            "hub.integrations.git_ops._git",
            side_effect=_clone(calls, on_remote=True, fetched=False, own_commits=0),
        ),
        patch("hub.integrations.git_ops._repo_root", return_value="/srv/ws"),
    ):
        branch = await git_ops.pair_prepare_branch(
            2, "Next", branch_slug="next", notify=notify
        )

    assert branch == "task-2/next"
    assert not _pushes(calls), "a branch already on origin must not be pushed again"
    notify.assert_not_awaited()
    # The verdict was refreshed rather than read off a stale local cache.
    assert [c for c in calls if c[:1] == ("fetch",)]


async def test_real_unpushed_commits_still_block(git_ops: GitOpsIntegration) -> None:
    """AC-2: real commits that exist nowhere else still stop the start.

    Since #966 the hub tries to publish them itself first; the refusal is what
    is left when that fails. It now names how much work is at stake, which the
    caller could not tell from the old text.
    """
    calls: list[tuple[str, ...]] = []

    with (
        patch(
            "hub.integrations.git_ops._git",
            side_effect=_clone(
                calls, on_remote=True, fetched=True, own_commits=3, push_rc=1
            ),
        ),
        patch("hub.integrations.git_ops._repo_root", return_value="/srv/ws"),
        patch("hub.integrations.git_ops._hostname", return_value="prod"),
    ):
        with pytest.raises(PairBranchConflictError) as exc:
            await git_ops.pair_prepare_branch(2, "Blocked")

    detail = exc.value.to_detail()
    assert detail["reason"] == "pair_branch_unpushed"
    assert "3" in detail["message"], "the refusal must name the commits at stake"
    assert "Permission denied" in detail["hint"]
    assert _pushes(calls), "#966: the hub tries to publish before it refuses"


async def test_absent_from_remote_says_so(git_ops: GitOpsIntegration) -> None:
    """A branch the server has never seen is a different case, and says so.

    'Not on the remote' and 'on the remote but unknown here' need different
    advice; one word for both is what sent the 25.08 investigation astray.
    """
    calls: list[tuple[str, ...]] = []

    with (
        patch(
            "hub.integrations.git_ops._git",
            side_effect=_clone(
                calls, on_remote=False, fetched=False, own_commits=2, push_rc=1
            ),
        ),
        patch("hub.integrations.git_ops._repo_root", return_value="/srv/ws"),
        patch("hub.integrations.git_ops._hostname", return_value="prod"),
    ):
        with pytest.raises(PairBranchConflictError) as exc:
            await git_ops.pair_prepare_branch(2, "Blocked")

    message = exc.value.to_detail()["message"]
    assert "remote" in message.lower()
    assert "unpushed commits" not in message, (
        "an unfetched branch and an absent one must not share one sentence"
    )


async def test_foreign_branch_does_not_block(git_ops: GitOpsIntegration) -> None:
    """AC-3: another task's branch, in good order, is not my start's business.

    The observed case: the clone sits on #951's branch while #953 starts. The
    start proceeds and the stranger's branch is left exactly as it was.
    """
    calls: list[tuple[str, ...]] = []

    with (
        patch(
            "hub.integrations.git_ops._git",
            side_effect=_clone(
                calls,
                current="task-951/pending-ci-waits",
                on_remote=True,
                fetched=False,
                own_commits=0,
            ),
        ),
        patch("hub.integrations.git_ops._repo_root", return_value="/srv/ws"),
    ):
        branch = await git_ops.pair_prepare_branch(
            953, "Public README", branch_slug="readme"
        )

    assert branch == "task-953/readme"
    assert not _pushes(calls)
    assert not [c for c in calls if c[0] in ("reset", "clean") or "--force" in c]
