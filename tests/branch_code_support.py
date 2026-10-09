"""A real git clone whose task branch carries code that must never run (#1650).

The hub reads a task's branch to judge it, and a branch is whatever an agent
pushed. ``conftest.py`` in that branch writes a marker file OUTSIDE the tree of
the pytest that runs the test, so the marker is the proof of execution: a
collector that imported the branch's conftest leaves the file behind, and one
that only read it with git does not.

Nothing here is faked: the repository is real, the hub's GitOps is the real
adapter, and the collector is not replaced. Only the spawn of a process is
watched, and still performed.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import subprocess
from pathlib import Path

import pytest

BRANCH = "task-42/work"
TEST_FILE = "tests/test_a.py"
TEST_SOURCE = "def test_ok():\n    assert True\n"


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(cwd),
        },
    ).stdout.strip()


def make_clone(
    tmp_path: Path,
    marker: Path,
    *,
    test_source: str = TEST_SOURCE,
    on_branch: bool = True,
) -> tuple[Path, str]:
    """``(workspace, tip_sha)``: a clone whose HEAD carries the marker conftest.

    ``on_branch`` puts the code on the task branch and leaves HEAD there, which
    matches what the old collector asked for (HEAD == task branch). Without it
    the code sits on ``main`` — the shared clone of an epic that has no branch.
    pyproject.toml is there so ``uv run`` has a project, and the conftest.py
    would leave ``marker`` behind the moment anything imported it.
    """
    origin = tmp_path / "origin.git"
    origin.mkdir()
    git(origin, "init", "--bare", "-b", "main", ".")
    work = tmp_path / "workspace"
    work.mkdir()
    git(work, "clone", str(origin), ".")
    git(work, "checkout", "-b", "main")
    (work / "README.md").write_text("base\n")
    git(work, "add", ".")
    git(work, "commit", "-m", "base")
    git(work, "push", "-q", "origin", "main")
    if on_branch:
        git(work, "checkout", "-b", BRANCH)
    (work / "pyproject.toml").write_text(
        '[project]\nname = "branchcode"\nversion = "0"\nrequires-python = ">=3.10"\n'
    )
    (work / "conftest.py").write_text(
        "import pathlib\n"
        f"pathlib.Path({str(marker)!r}).write_text('the branch code ran')\n"
    )
    (work / "tests").mkdir()
    (work / TEST_FILE).write_text(test_source)
    git(work, "add", ".")
    git(work, "commit", "-m", "work")
    git(work, "push", "-q", "-u", "origin", BRANCH if on_branch else "main")
    return work, git(work, "rev-parse", "HEAD")


def _tokens(argv: list[str]) -> list[str]:
    out: list[str] = []
    for part in argv:
        try:
            out.extend(shlex.split(part) if " " in part else [part])
        except ValueError:
            out.extend(part.split())
    return out


def _is_pytest(token: str) -> bool:
    return os.path.basename(token) in ("pytest", "py.test")


class SpawnSpy:
    """Every process the hub starts, recorded and still started."""

    def __init__(self) -> None:
        self.argv: list[list[str]] = []

    def pytest_runs(self) -> list[list[str]]:
        """Starts of pytest: a token that IS the command, in any argv or shell line.

        A path that merely contains the word (pytest's own tmp dirs do) is not
        a start, so a token counts only when its basename is the command.
        """
        return [a for a in self.argv if any(_is_pytest(t) for t in _tokens(a))]


@pytest.fixture
def spawn_spy(monkeypatch: pytest.MonkeyPatch) -> SpawnSpy:
    spy = SpawnSpy()
    real_exec = asyncio.create_subprocess_exec
    real_shell = asyncio.create_subprocess_shell

    async def watched_exec(program, *args, **kwargs):
        spy.argv.append([str(program), *map(str, args)])
        return await real_exec(program, *args, **kwargs)

    async def watched_shell(cmd, *args, **kwargs):
        spy.argv.append([str(cmd)])
        return await real_shell(cmd, *args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", watched_exec)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", watched_shell)
    return spy
