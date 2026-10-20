"""git_history на настоящем git во временном репозитории (#1865)."""

from __future__ import annotations

import subprocess
from functools import partial
from pathlib import Path

import pytest

from hub.integrations import git_history
from hub.integrations.git_ops import _git


def sh(repo: Path, *args: str) -> str:
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
        "HOME": str(repo),
    }
    out = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, env=env
    )
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def commit(repo: Path, name: str, message: str) -> str:
    (repo / name).write_text(name)
    sh(repo, "add", "-A")
    sh(repo, "commit", "-q", "-m", message)
    return sh(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    sh(tmp_path, "init", "-q", "-b", "develop")
    commit(tmp_path, "a", "init")
    # first_parent_after читает origin/<base>: ссылка на себя же.
    sh(tmp_path, "update-ref", "refs/remotes/origin/develop", "HEAD")
    return tmp_path


def runner(repo: Path):
    return partial(_git, repo=str(repo), check=False)


def publish(repo: Path) -> None:
    sh(repo, "update-ref", "refs/remotes/origin/develop", "develop")


async def test_first_parent_after_orders_old_first_and_resets_on_foreign_cursor(repo):
    first = sh(repo, "rev-parse", "HEAD")
    c1 = commit(repo, "b", "one")
    c2 = commit(repo, "c", "two")
    c3 = commit(repo, "d", "three")
    publish(repo)
    run = runner(repo)

    walk = await git_history.first_parent_after(run, "develop", first, 10)
    assert walk == {"head": c3, "reset": False, "shas": [c1, c2, c3]}
    limited = await git_history.first_parent_after(run, "develop", first, 2)
    assert limited["shas"] == [c1, c2], "самые старые, не самые новые"
    assert (await git_history.first_parent_after(run, "develop", c3, 5))["shas"] == []

    # Курсор не предок головы: чужой коммит (rc 1) и несуществующий объект (rc 128).
    sh(repo, "checkout", "-q", "-b", "side", first)
    foreign = commit(repo, "x", "foreign")
    sh(repo, "checkout", "-q", "develop")
    one = await git_history.first_parent_after(run, "develop", foreign, 5)
    gone = await git_history.first_parent_after(run, "develop", "0" * 40, 5)
    assert one == {"head": c3, "reset": True, "shas": []}
    assert gone == {"head": c3, "reset": True, "shas": []}
    assert await git_history.first_parent_after(run, "nope", first, 5) is None


async def test_commit_facts_keep_the_full_body_and_separators(repo):
    target = commit(repo, "b", "feat: b")
    body = f'Revert "feat: b"\n\nThis reverts commit {target}, reversing changes.\n\nmore\x1ftext'
    revert = commit(repo, "c", body)
    facts = await git_history.commit_facts(runner(repo), [target, revert])
    assert facts is not None
    assert facts[revert]["parents"] == [target]
    assert f"This reverts commit {target}" in facts[revert]["message"]
    assert facts[revert]["message"].endswith("more\x1ftext")
    assert facts[target]["message"] == "feat: b"
    assert await git_history.commit_facts(runner(repo), ["f" * 40]) is None
    assert await git_history.commit_facts(runner(repo), []) == {}


async def test_revert_lines_reads_only_revert_lines(repo):
    target = commit(repo, "b", "feat: b")
    base = sh(repo, "rev-parse", "HEAD")
    commit(repo, "c", f'Revert "feat: b"\n\nThis reverts commit {target}.')
    commit(repo, "d", "fixup")
    lines = await git_history.revert_lines(runner(repo), f"{base}..HEAD")
    assert lines == f"This reverts commit {target}."
    assert await git_history.revert_lines(runner(repo), f"{base}..{base}") == ""


async def test_squash_branch_carries_the_revert_line_into_the_body(repo):
    from hub.integrations.git_ops import GitOpsIntegration

    target = commit(repo, "b", "feat: b")
    sh(repo, "update-ref", "refs/remotes/origin/develop", "develop")
    sh(repo, "remote", "add", "origin", str(repo))
    sh(repo, "config", "user.email", "t@t")
    sh(repo, "config", "user.name", "t")
    sh(repo, "checkout", "-q", "-b", "task-9/revert")
    commit(repo, "c", f'Revert "feat: b"\n\nThis reverts commit {target}.')
    commit(repo, "d", "fixup")
    assert await GitOpsIntegration().squash_branch(
        9, "Revert b", "task-9/revert", repo=str(repo), base_branch="develop"
    )
    full = sh(repo, "log", "-1", "--format=%B")
    assert full.startswith("feat(task):") and "(#9)" in full.splitlines()[0]
    assert f"This reverts commit {target}." in full


async def test_revert_on_the_second_parent_of_a_merge_is_read(repo):
    target = commit(repo, "b", "feat: b")
    base = sh(repo, "rev-parse", "HEAD")
    sh(repo, "checkout", "-q", "-b", "pr")
    commit(repo, "c", f'Revert "feat: b"\n\nThis reverts commit {target}.')
    sh(repo, "checkout", "-q", "develop")
    commit(repo, "e", "feat: e")
    sh(repo, "merge", "--no-ff", "-q", "-m", "Merge pull request #5 from pr", "pr")
    merge = sh(repo, "rev-parse", "HEAD")
    publish(repo)
    walk = await git_history.first_parent_after(runner(repo), "develop", base, 10)
    assert walk is not None and walk["shas"][-1] == merge
    facts = await git_history.commit_facts(runner(repo), [merge])
    assert facts is not None
    assert "This reverts" not in facts[merge]["message"]
    assert f"This reverts commit {target}" in facts[merge]["second_parent_message"]
