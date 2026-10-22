"""The pre-push hook runs the clone's fast check, only when the clone agreed (#2000).

Real git, a real bare remote and the repository's own ``.githooks/pre-push`` copied
in: the question is whether a push is actually refused or let through, and only
git can answer it. The "check" is a tiny shell command that appends to a marker
file, so a test can tell "ran once", "ran twice" and "did not run".
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_HOOK = REPO_ROOT / ".githooks" / "pre-push"
KEY = "haiplane.prePushCheck"


def git(repo: Path, *args: str, env: dict[str, str] | None = None, check: bool = True):
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@e",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@e",
            **(env or {}),
        },
    )
    if check:
        assert proc.returncode == 0, proc.stderr
    return proc


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    repo = tmp_path / "clone"
    subprocess.run(["git", "init", "-q", "-b", "develop", str(repo)], check=True)
    git(repo, "remote", "add", "origin", str(remote))
    hooks = repo / ".githooks"
    hooks.mkdir()
    shutil.copy2(REAL_HOOK, hooks / "pre-push")
    (hooks / "pre-push").chmod(0o755)
    git(repo, "config", "core.hooksPath", ".githooks")
    (repo / "file.txt").write_text("x\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "init")
    git(repo, "push", "-q", "origin", "develop")
    git(repo, "checkout", "-q", "-b", "task-1/x")
    return repo


def marker_command(marker: Path, body: str = "") -> str:
    """A check that appends one line to ``marker`` and then runs ``body``."""
    return f'sh -c "echo ran >> {marker}; {body or "true"}"'


def runs(marker: Path) -> int:
    return len(marker.read_text().splitlines()) if marker.exists() else 0


def push(repo: Path, *refspec: str, env: dict[str, str] | None = None):
    return git(
        repo, "push", "origin", *(refspec or ("task-1/x",)), env=env, check=False
    )


def remote_has(repo: Path, branch: str) -> bool:
    out = git(repo, "ls-remote", "--heads", "origin", branch).stdout
    return bool(out.strip())


def test_a_failing_check_refuses_the_push(clone: Path, tmp_path: Path) -> None:
    marker = tmp_path / "marker"
    git(clone, "config", KEY, marker_command(marker, "echo FAILMARK >&2; exit 3"))
    proc = push(clone)
    assert proc.returncode != 0
    assert "Blocked push of task-1/x" in proc.stderr
    assert "rc=3" in proc.stderr
    assert "FAILMARK" in proc.stderr  # the command's own output reaches the person
    assert not remote_has(clone, "task-1/x")


def test_a_passing_check_lets_the_push_through(clone: Path, tmp_path: Path) -> None:
    marker = tmp_path / "marker"
    git(clone, "config", KEY, marker_command(marker))
    proc = push(clone)
    assert proc.returncode == 0, proc.stderr
    assert runs(marker) == 1
    assert remote_has(clone, "task-1/x")


def test_no_key_means_no_check_even_with_a_make_target(
    clone: Path, tmp_path: Path
) -> None:
    """Consent is the key; a Makefile with ``check-fast`` consents to nothing."""
    marker = tmp_path / "marker"
    (clone / "Makefile").write_text(f"check-fast:\n\techo ran >> {marker}\n")
    git(clone, "add", "-A")
    git(clone, "commit", "-qm", "makefile")
    proc = push(clone)
    assert proc.returncode == 0, proc.stderr
    assert runs(marker) == 0


def test_deletes_base_and_bot_branches_do_not_run_it(
    clone: Path, tmp_path: Path
) -> None:
    marker = tmp_path / "marker"
    git(clone, "config", KEY, marker_command(marker))
    assert push(clone).returncode == 0
    assert runs(marker) == 1
    assert push(clone, "--delete", "task-1/x").returncode == 0  # a delete
    git(clone, "checkout", "-q", "develop")
    (clone / "base.txt").write_text("a new tree for the base branch\n")
    git(clone, "add", "-A")
    git(clone, "commit", "-q", "-m", "on base")
    assert push(clone, "develop").returncode == 0  # the base branch
    git(clone, "checkout", "-q", "-b", "dependabot/uv/pkg-1.0")
    (clone / "bot.txt").write_text("a new tree for the bot branch\n")
    git(clone, "add", "-A")
    git(clone, "commit", "-q", "-m", "bot")
    assert push(clone, "dependabot/uv/pkg-1.0").returncode == 0  # a bot branch
    assert runs(marker) == 1  # only the first push of task-1/x ran the check


def test_a_commit_that_is_not_the_checked_out_head_is_not_counted(
    clone: Path, tmp_path: Path
) -> None:
    """The check runs on the working copy; another branch's commit is not covered."""
    marker = tmp_path / "marker"
    git(clone, "config", KEY, marker_command(marker))
    git(clone, "checkout", "-q", "-b", "task-2/y")
    git(clone, "commit", "-q", "--allow-empty", "-m", "y")
    git(clone, "checkout", "-q", "task-1/x")
    proc = push(clone, "task-2/y")
    assert proc.returncode == 0, proc.stderr
    assert "NOT run for task-2/y" in proc.stderr
    assert runs(marker) == 0
    # Two refs in one push: the one that is HEAD is checked, the other warned about.
    proc = push(clone, "task-1/x", "task-2/y")
    assert proc.returncode == 0, proc.stderr
    assert runs(marker) == 1


def test_the_bypass_variable_skips_only_this_check(clone: Path, tmp_path: Path) -> None:
    marker = tmp_path / "marker"
    git(clone, "config", KEY, marker_command(marker, "exit 1"))
    proc = push(clone, env={"HAIPLANE_SKIP_PREPUSH": "1"})
    assert proc.returncode == 0, proc.stderr
    assert "HAIPLANE_SKIP_PREPUSH=1" in proc.stderr
    assert runs(marker) == 0
    # The branch policy still applies: a dirty tree is refused as before.
    git(clone, "commit", "-q", "--allow-empty", "-m", "something new to push")
    (clone / "file.txt").write_text("dirty\n")
    proc = push(clone, env={"HAIPLANE_SKIP_PREPUSH": "1"})
    assert proc.returncode != 0 and "dirty working tree" in proc.stderr


def test_make_without_uv_warns_and_is_not_remembered(
    clone: Path, tmp_path: Path
) -> None:
    """The default command needs make and uv; without them the push goes through."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "make").write_text("#!/bin/sh\nexit 0\n")
    (bin_dir / "make").chmod(0o755)
    path = f"{bin_dir}:/usr/bin:/bin"
    if shutil.which("uv", path=path):
        pytest.skip("uv is on the minimal PATH of this machine")
    git(clone, "config", KEY, "make check-fast")
    proc = push(clone, env={"PATH": path})
    assert proc.returncode == 0, proc.stderr
    assert "make or uv is not available" in proc.stderr
    cache = (
        clone
        / git(clone, "rev-parse", "--git-path", "haiplane/prepush-ok").stdout.strip()
    )
    assert not cache.exists()


def test_any_other_failing_command_blocks_even_when_the_shell_cannot_find_it(
    clone: Path,
) -> None:
    """Only make/uv are looked up; a command the shell cannot find is a failure."""
    git(clone, "config", KEY, "no-such-tool-xyz check-fast")
    proc = push(clone)
    assert proc.returncode != 0
    assert "rc=127" in proc.stderr and "did not find a command" in proc.stderr


@pytest.mark.parametrize(
    "command",
    [
        ' "sh" -c "exit 7"',
        's"h" -c "exit 7"',
        'CI=1 sh -c "exit 7"',
        "  sh -c 'exit 7'",
    ],
)
def test_quoting_and_assignments_in_the_command_are_the_shells_business(
    clone: Path, command: str
) -> None:
    git(clone, "config", KEY, command)
    proc = push(clone)
    assert proc.returncode != 0
    assert "rc=7" in proc.stderr


def test_the_same_tree_is_checked_once(clone: Path, tmp_path: Path) -> None:
    marker = tmp_path / "marker"
    git(clone, "config", KEY, marker_command(marker))
    assert push(clone).returncode == 0
    assert runs(marker) == 1
    git(clone, "commit", "-q", "--allow-empty", "-m", "same tree, new commit")
    proc = push(clone)
    assert proc.returncode == 0, proc.stderr
    assert "already passed for this tree" in proc.stderr
    assert runs(marker) == 1
    # Another tree is another question: the cache key includes the tree.
    (clone / "changed.txt").write_text("a new tree\n")
    git(clone, "add", "-A")
    git(clone, "commit", "-q", "-m", "a different tree")
    assert push(clone).returncode == 0
    assert runs(marker) == 2
    # Another command is another question: the cache key includes it.
    git(clone, "config", KEY, marker_command(marker, "true "))
    git(clone, "commit", "-q", "--allow-empty", "-m", "again")
    assert push(clone).returncode == 0
    assert runs(marker) == 3


def test_a_dirty_tree_is_never_cached(clone: Path, tmp_path: Path) -> None:
    marker = tmp_path / "marker"
    git(clone, "config", KEY, marker_command(marker))
    env = {"ALLOW_DIRTY_PUSH": "1"}
    (clone / "file.txt").write_text("dirty\n")
    assert push(clone, env=env).returncode == 0
    git(clone, "commit", "-q", "--allow-empty", "-m", "next")
    assert push(clone, env=env).returncode == 0
    assert runs(marker) == 2  # the second push did not trust a pass for a dirty tree


def test_the_cache_works_when_dot_git_is_a_file(clone: Path, tmp_path: Path) -> None:
    """A worktree has ``.git`` as a file: the cache path comes from git itself."""
    marker = tmp_path / "marker"
    git(clone, "config", KEY, marker_command(marker))
    tree = tmp_path / "wt"
    git(clone, "worktree", "add", "-q", "-b", "task-3/z", str(tree), "develop")
    assert (tree / ".git").is_file()
    assert push(tree, "task-3/z").returncode == 0
    assert runs(marker) == 1
    git(tree, "commit", "-q", "--allow-empty", "-m", "same tree")
    proc = push(tree, "task-3/z")
    assert proc.returncode == 0, proc.stderr
    assert "already passed for this tree" in proc.stderr
    assert runs(marker) == 1


def test_a_check_cannot_swallow_the_refs_git_sends(clone: Path, tmp_path: Path) -> None:
    """The command runs with a closed stdin: the second ref is still processed."""
    marker = tmp_path / "marker"
    git(clone, "config", KEY, marker_command(marker, "cat >/dev/null"))
    git(clone, "branch", "badname")  # a name the branch policy refuses
    proc = push(clone, "task-1/x", "badname")
    # The second ref line reached the hook (a command that ate stdin would have
    # hidden it, and the refused name would have gone through).
    assert proc.returncode != 0
    assert "Blocked push from branch 'badname'" in proc.stderr
    assert runs(marker) == 1


def test_a_global_config_cannot_switch_the_check_on(
    clone: Path, tmp_path: Path
) -> None:
    """Only the clone's own config consents: a server-side clone reads the same global."""
    marker = tmp_path / "marker"
    global_config = tmp_path / "global.gitconfig"
    global_config.write_text(f"[haiplane]\n\tprePushCheck = {marker_command(marker)}\n")
    env = {"GIT_CONFIG_GLOBAL": str(global_config)}
    proc = push(clone, env=env)
    assert proc.returncode == 0, proc.stderr
    assert runs(marker) == 0


def test_a_multi_line_command_gets_a_cache_hit_only_for_itself(
    clone: Path, tmp_path: Path
) -> None:
    marker = tmp_path / "marker"
    first = f"sh -c 'echo ran >> {marker}\ntrue'"
    git(clone, "config", KEY, first)
    assert push(clone).returncode == 0
    assert runs(marker) == 1
    git(clone, "config", KEY, f"sh -c 'echo ran >> {marker}\ntrue\nfalse'")
    git(clone, "commit", "-q", "--allow-empty", "-m", "same tree")
    proc = push(clone)
    assert "already passed" not in proc.stderr  # the other command is not a hit
    assert runs(marker) == 2


def test_another_comparison_base_is_another_question(
    clone: Path, tmp_path: Path
) -> None:
    """A rebase onto another base keeps the tree but changes what the check selects."""
    marker = tmp_path / "marker"
    git(clone, "config", KEY, marker_command(marker))
    assert push(clone).returncode == 0
    assert runs(marker) == 1
    git(clone, "checkout", "-q", "develop")
    (clone / "moved.txt").write_text("the base moved\n")
    git(clone, "add", "-A")
    git(clone, "commit", "-qm", "base moved")
    git(clone, "push", "-q", "origin", "develop")  # origin/develop is a new commit
    git(clone, "checkout", "-q", "task-1/x")
    git(clone, "commit", "-q", "--allow-empty", "-m", "same tree, other base")
    assert push(clone).returncode == 0
    assert runs(marker) == 2


def test_the_configured_base_and_release_branches_are_not_checked(
    clone: Path, tmp_path: Path
) -> None:
    """``fix/integration`` as the project's base is a base branch, not a work branch."""
    marker = tmp_path / "marker"
    git(clone, "config", KEY, marker_command(marker, "exit 1"))
    git(clone, "config", "haiplane.baseBranch", "fix/integration")
    git(clone, "checkout", "-q", "-b", "fix/integration")
    (clone / "integration.txt").write_text("x\n")
    git(clone, "add", "-A")
    git(clone, "commit", "-qm", "integration")
    proc = push(clone, "fix/integration")
    assert proc.returncode == 0, proc.stderr
    assert runs(marker) == 0


def test_the_same_tree_under_another_merge_base_is_checked_again(
    clone: Path, tmp_path: Path
) -> None:
    """The selection is the diff against the merge-base: the same tree, another base."""
    marker = tmp_path / "marker"
    git(clone, "config", KEY, marker_command(marker))
    start = git(clone, "rev-parse", "develop").stdout.strip()
    git(clone, "checkout", "-q", "develop")
    (clone / "f.txt").write_text("f\n")
    git(clone, "add", "-A")
    git(clone, "commit", "-qm", "develop moves")
    git(clone, "push", "-q", "origin", "develop")
    git(clone, "checkout", "-q", "-b", "task-5/a", start)  # branched BEFORE the move
    (clone / "f.txt").write_text("f\n")
    git(clone, "add", "-A")
    git(clone, "commit", "-qm", "the same file, the same tree")
    assert push(clone, "task-5/a").returncode == 0
    assert runs(marker) == 1
    git(clone, "checkout", "-q", "-b", "task-5/b", "develop")  # branched AFTER it
    git(
        clone,
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "same tree, base is develop itself",
    )
    assert (
        git(clone, "rev-parse", "task-5/a^{tree}").stdout
        == git(clone, "rev-parse", "task-5/b^{tree}").stdout
    )
    assert push(clone, "task-5/b").returncode == 0
    assert runs(marker) == 2
