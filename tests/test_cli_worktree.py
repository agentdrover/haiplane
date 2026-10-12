"""CLI: hp-hub worktree (#1515)."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from hub import cli


# --- hp-hub worktree (#1515) -------------------------------------------------
def _sh(cwd: Path, *args: str) -> str:
    import subprocess

    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _clone(tmp_path: Path, name: str, origin: str) -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _sh(repo, "init", "-q", "-b", "develop")
    _sh(repo, "config", "user.email", "t@example.com")
    _sh(repo, "config", "user.name", "Test")
    (repo / "f.txt").write_text("x")
    _sh(repo, "add", ".")
    _sh(repo, "commit", "-q", "-m", "init")
    _sh(repo, "remote", "add", "origin", origin)
    return repo


def _bare_origin(tmp_path: Path, slug: str) -> str:
    """A local bare repo whose path ends in ``owner/name``: a no-network origin."""
    path = tmp_path / "origins" / f"{slug}.git"
    path.mkdir(parents=True)
    _sh(path, "init", "-q", "--bare")
    return str(path)


def _fake_hub(monkeypatch, *, repo_name: str = "agentdrover/Spike_bo") -> str:
    branch = "task-1429/spike-thing"

    def fake_api(method: str, path: str, body: Any = None, **kw: Any) -> Any:
        if path == "/api/tasks/1429":
            return {
                "id": 1429,
                "title": "Spike thing",
                "branch": branch,
                "project": {"id": 5, "slug": "spike"},
            }
        if path.startswith("/api/projects"):
            return [
                {
                    "id": 5,
                    "slug": "spike",
                    "repo": repo_name,
                    "default_branch": "develop",
                }
            ]
        raise AssertionError(f"unexpected call {method} {path}")

    monkeypatch.setattr(cli, "_api", fake_api)
    return branch


def _run_worktree(repo: Path) -> int:
    return cli.cmd_worktree(argparse.Namespace(task_id=1429, repo=str(repo)))


def test_worktree_command_creates_then_reuses_rule_path(
    tmp_path, monkeypatch, capsys
) -> None:
    # AC-2 (#1515): the path is built from the ACTUAL clone folder (spike_bo),
    # created on the canonical branch, and reused on the second call.
    branch = _fake_hub(monkeypatch)
    clone = _clone(tmp_path, "spike_bo", _bare_origin(tmp_path, "agentdrover/Spike_bo"))
    expected = tmp_path / ".spike_bo-worktrees" / "task-1429"

    assert _run_worktree(clone) == 0
    first = capsys.readouterr().out.strip()
    assert first == str(expected)
    assert _sh(expected, "branch", "--show-current") == branch

    assert _run_worktree(clone) == 0
    assert capsys.readouterr().out.strip() == first
    assert _sh(expected, "branch", "--show-current") == branch


def test_worktree_command_refuses_dirty_foreign_or_wrong_repo(
    tmp_path, monkeypatch, capsys
) -> None:
    # AC-3 (#1515): refusal names the reason and touches nothing.
    branch = _fake_hub(monkeypatch)
    clone = _clone(tmp_path, "spike_bo", "git@github.com:AgentDrover/spike_bo.git")
    wt = tmp_path / ".spike_bo-worktrees" / "task-1429"
    wt.parent.mkdir()

    # foreign branch, clean: refused, branch unchanged
    _sh(clone, "worktree", "add", "-q", "-b", "other/work", str(wt), "develop")
    assert _run_worktree(clone) == 1
    err = capsys.readouterr().err
    assert "other/work" in err and branch in err
    assert _sh(wt, "branch", "--show-current") == "other/work"

    # foreign branch with uncommitted edits: refused, files intact
    (wt / "f.txt").write_text("edited")
    (wt / "new.txt").write_text("untracked")
    assert _run_worktree(clone) == 1
    err = capsys.readouterr().err
    assert "f.txt" in err
    assert _sh(wt, "branch", "--show-current") == "other/work"
    assert (wt / "f.txt").read_text() == "edited"
    assert (wt / "new.txt").read_text() == "untracked"

    # origin that is not the project repo: refused, no copy created
    other = _clone(tmp_path, "elsewhere", "https://github.com/someone/else.git")
    assert _run_worktree(other) == 1
    err = capsys.readouterr().err
    assert "someone/else" in err and "agentdrover/spike_bo" in err.lower()
    assert not (tmp_path / ".elsewhere-worktrees").exists()


def test_worktree_command_refuses_unregistered_path_missing_repo_and_non_clone(
    tmp_path, monkeypatch, capsys
) -> None:
    # #1515: other refusals leave the disk as they found it.
    _fake_hub(monkeypatch)
    clone = _clone(tmp_path, "spike_bo", "https://github.com/agentdrover/Spike_bo")
    squatter = tmp_path / ".spike_bo-worktrees" / "task-1429"
    squatter.mkdir(parents=True)
    (squatter / "mine.txt").write_text("keep")
    assert _run_worktree(clone) == 1
    assert "не копия клона" in capsys.readouterr().err
    assert (squatter / "mine.txt").read_text() == "keep"

    # project without a repo: nothing to compare the origin with
    _fake_hub(monkeypatch, repo_name="")
    assert _run_worktree(clone) == 1
    assert "не задан repo" in capsys.readouterr().err

    # clone without origin
    _fake_hub(monkeypatch)
    bare = _clone(tmp_path, "noorigin", "https://github.com/x/y")
    _sh(bare, "remote", "remove", "origin")
    assert _run_worktree(bare) == 1
    assert "нет remote origin" in capsys.readouterr().err

    # not a git clone at all
    plain = tmp_path / "plain"
    plain.mkdir()
    assert _run_worktree(plain) == 1
    assert "не git-клон" in capsys.readouterr().err
    assert _run_worktree(tmp_path / "missing") == 1
    assert "не git-клон" in capsys.readouterr().err
    assert not (tmp_path / ".noorigin-worktrees").exists()


def test_worktree_command_from_inside_a_worktree_uses_the_main_clone(
    tmp_path, monkeypatch, capsys
) -> None:
    # #1515: asked from inside a copy, the rule path is still beside the clone.
    _fake_hub(monkeypatch)
    clone = _clone(tmp_path, "spike_bo", _bare_origin(tmp_path, "agentdrover/Spike_bo"))
    assert _run_worktree(clone) == 0
    path = capsys.readouterr().out.strip()
    assert _run_worktree(Path(path)) == 0
    assert capsys.readouterr().out.strip() == path


def test_worktree_command_takes_the_task_branch_from_origin(
    tmp_path, monkeypatch, capsys
) -> None:
    # #1515: a task branch that exists only on origin is checked out with its
    # commits (tracking it), not recreated empty from the base.
    branch = _fake_hub(monkeypatch)
    origin = _bare_origin(tmp_path, "agentdrover/Spike_bo")
    clone = _clone(tmp_path, "spike_bo", origin)
    _sh(clone, "push", "-q", "origin", "develop")
    other = tmp_path / "other_clone"
    _sh(tmp_path, "clone", "-q", origin, str(other))
    _sh(other, "config", "user.email", "t@example.com")
    _sh(other, "config", "user.name", "Test")
    _sh(other, "checkout", "-q", "-b", branch)
    (other / "work.txt").write_text("pushed work")
    _sh(other, "add", ".")
    _sh(other, "commit", "-q", "-m", "task work")
    _sh(other, "push", "-q", "origin", branch)
    pushed = _sh(other, "rev-parse", "HEAD")

    assert _run_worktree(clone) == 0
    wt = Path(capsys.readouterr().out.strip())
    assert _sh(wt, "branch", "--show-current") == branch
    assert _sh(wt, "rev-parse", "HEAD") == pushed
    assert (wt / "work.txt").read_text() == "pushed work"
    assert _sh(wt, "rev-parse", "--abbrev-ref", f"{branch}@{{upstream}}") == (
        f"origin/{branch}"
    )


def test_worktree_command_names_a_failed_fetch_and_still_creates(
    tmp_path, monkeypatch, capsys
) -> None:
    # #1515: a network failure is a warning, not a refusal.
    branch = _fake_hub(monkeypatch)
    clone = _clone(
        tmp_path, "spike_bo", str(tmp_path / "gone" / "agentdrover" / "Spike_bo")
    )
    assert _run_worktree(clone) == 0
    captured = capsys.readouterr()
    assert f"предупреждение: fetch origin {branch} не удался" in captured.err
    assert "предупреждение: fetch origin develop не удался" in captured.err
    assert _sh(Path(captured.out.strip()), "branch", "--show-current") == branch


def _fetch_failing_git(monkeypatch, stderr: str) -> None:
    """Make every ``git fetch`` of a worktree request fail with ``stderr``."""
    from hub.integrations import git_ops

    real = git_ops._git

    async def fake(*args: str, **kw: Any):
        if args and args[0] == "fetch":
            return 128, "", stderr
        return await real(*args, **kw)

    monkeypatch.setattr(git_ops, "_git", fake)


def test_worktree_absent_branch_gives_no_note_under_a_foreign_locale(
    tmp_path, monkeypatch, capsys
) -> None:
    # #1515: "branch absent on origin" is asked of ls-remote, not read from the
    # text of git's stderr, which is localised. A fetch of the branch must not
    # even be attempted: the only fetch that may fail here is the base's.
    branch = _fake_hub(monkeypatch)
    origin = _bare_origin(tmp_path, "agentdrover/Spike_bo")
    clone = _clone(tmp_path, "spike_bo", origin)
    _sh(clone, "push", "-q", "origin", "develop")
    _fetch_failing_git(monkeypatch, "fatal: не удалось найти удалённую ссылку")
    assert _run_worktree(clone) == 0
    captured = capsys.readouterr()
    assert f"fetch origin {branch}" not in captured.err
    assert "предупреждение" in captured.err  # the base fetch failure only
    assert "fetch origin develop" in captured.err


def test_worktree_real_fetch_failure_of_an_existing_branch_is_named(
    tmp_path, monkeypatch, capsys
) -> None:
    branch = _fake_hub(monkeypatch)
    origin = _bare_origin(tmp_path, "agentdrover/Spike_bo")
    clone = _clone(tmp_path, "spike_bo", origin)
    _sh(clone, "push", "-q", "origin", f"develop:refs/heads/{branch}")
    _fetch_failing_git(monkeypatch, "fatal: boom")
    assert _run_worktree(clone) == 0
    assert f"предупреждение: fetch origin {branch} не удался" in (
        capsys.readouterr().err
    )
