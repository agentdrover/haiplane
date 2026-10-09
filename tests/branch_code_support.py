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


class FakeGit:
    """An in-memory git: named refs point at shas, a sha holds a set of files.

    Both the old reads (tree + file) and the new ones (read_file_at_ref,
    resolve_ref) are served, so one fake can drive the code before and after a
    change. ``moves`` re-points a ref after that many reads, which is how a
    branch that advances between two reads is modelled.
    """

    def __init__(
        self,
        files: dict[str, str | bytes],
        *,
        refs: dict[str, str] | None = None,
        trees: dict[str, dict[str, str | bytes]] | None = None,
    ) -> None:
        self.tip = "a" * 40
        self.trees = {self.tip: files, **(trees or {})}
        self.refs = {"origin/task-42/work": self.tip, **(refs or {})}
        self.refs_read: list[str] = []
        self.bytes_served = 0
        self.unreadable: set[str] = set()
        self.reads = 0
        self.moves: dict[int, tuple[str, str]] = {}

    def _sha(self, ref: str) -> str:
        return self.refs.get(ref) or (ref if ref in self.trees else "")

    def _tree(self, ref: str) -> dict[str, str | bytes] | None:
        self.refs_read.append(ref)
        self.reads += 1
        if self.reads in self.moves:
            name, sha = self.moves[self.reads]
            self.refs[name] = sha
        sha = self._sha(ref)
        return self.trees.get(sha) if sha else None

    async def head_sha(self, repo: str, base: str) -> str:
        return self.refs.get(f"origin/{base}", "")

    async def resolve_ref(self, name: str, repo: str) -> tuple[str, str]:
        sha = self._sha(name) or self._sha(f"origin/{name}")
        return ("resolved", sha) if sha else ("missing", name)

    async def files_at_ref(self, repo: str, ref: str):
        tree = self._tree(ref)
        return None if tree is None else set(tree)

    async def file_at_ref(self, repo: str, ref: str, path: str):
        tree = self._tree(ref)
        if tree is None or path not in tree:
            return None
        value = tree[path]
        return value.decode() if isinstance(value, bytes) else value

    async def read_file_at_ref(
        self, repo: str, ref: str, path: str, *, limit_chars: int = 30000
    ) -> dict:
        tree = self._tree(ref)
        out = {
            "state": "unreadable",
            "path": path,
            "ref": ref,
            "sha": self._sha(ref),
            "content": "",
            "truncated": False,
            "size": 0,
            "chars": 0,
            "reason": "",
        }
        if tree is None:
            out["reason"] = f"ref {ref!r} not read"
            return out
        if path not in tree:
            out["state"] = "missing"
            return out
        if path in self.unreadable:
            out["reason"] = "io error"
            return out
        raw = tree[path]
        data = raw if isinstance(raw, bytes) else raw.encode()
        text = data.decode(errors="replace")
        out.update(
            state="present",
            size=len(data),
            chars=len(text),
            truncated=len(text) > limit_chars,
            content=text[:limit_chars],
        )
        self.bytes_served += len(out["content"].encode())
        return out
