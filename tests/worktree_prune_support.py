"""Общая оснастка тестов hp-hub worktree prune (#2038): клон, копии, заглушка хаба."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from hub import cli

REPO_SLUG = "agentdrover/haiplane"
SLUGS = {
    101: "a-head-eq",
    102: "b-head-ancestor",
    103: "c-commit-after",
    104: "d-dirty",
    105: "e-running",
    106: "f-archived-active",
    107: "g-not-delivered",
    108: "h-detached",
    109: "i-id-mismatch",
    111: "j-current",
}


def _sh(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


class World:
    """Клон с копиями задач, словарь задач хаба и журнал обращений к нему."""

    def __init__(self, tmp_path: Path) -> None:
        origin = tmp_path / "origins" / f"{REPO_SLUG}.git"
        origin.mkdir(parents=True)
        _sh(origin, "init", "-q", "--bare")
        self.clone = tmp_path / "haiplane"
        self.clone.mkdir()
        _sh(self.clone, "init", "-q", "-b", "develop")
        _sh(self.clone, "config", "user.email", "t@example.com")
        _sh(self.clone, "config", "user.name", "Test")
        (self.clone / "f.txt").write_text("x")
        _sh(self.clone, "add", ".")
        _sh(self.clone, "commit", "-q", "-m", "init")
        _sh(self.clone, "remote", "add", "origin", str(origin))
        self.tasks: dict[int, dict[str, Any]] = {}
        self.delivery: dict[int, Any] = {}
        self.calls: list[str] = []
        self.hooks: dict[str, Any] = {}
        self.broken: set[str] = set()
        self.down = False
        self.fresh: set[int] = set()  # копии, чей gitdir не старят

    def wt(self, task_id: int) -> Path:
        return self.clone.parent / ".haiplane-worktrees" / f"task-{task_id}"

    def trash(self) -> Path:
        return self.clone.parent / ".haiplane-worktrees" / ".trash"

    def add(self, task_id: int, *, branch: str | None = None) -> Path:
        path = self.wt(task_id)
        path.parent.mkdir(exist_ok=True)
        branch = branch or f"task-{task_id}/{SLUGS.get(task_id, 'work')}"
        _sh(self.clone, "worktree", "add", "-q", "-b", branch, str(path), "develop")
        _sh(path, "config", "user.email", "t@example.com")
        _sh(path, "config", "user.name", "Test")
        self.tasks[task_id] = {
            "id": task_id,
            "status": "completed",
            "archived": False,
            "branch": branch,
            "submission_sha": _sh(path, "rev-parse", "HEAD"),
            "result_kind": "commit",
            "project": {"id": 5, "slug": "haiplane"},
        }
        self.delivery[task_id] = {"state": "delivered", "reason": "gate"}
        return path

    def commit(self, task_id: int, name: str) -> str:
        path = self.wt(task_id)
        (path / name).write_text(name)
        _sh(path, "add", name)
        _sh(path, "commit", "-q", "-m", name)
        return _sh(path, "rev-parse", "HEAD")

    def age_all(self) -> None:
        """Состарить gitdir копий (HEAD, index, logs/HEAD) на 2 часа."""
        old = time.time() - 7200
        for n in list(self.tasks):
            if n in self.fresh or not self.wt(n).exists():
                continue
            gd = self.wt(n) / ".git"
            if not gd.is_file():
                continue
            gitdir = Path(gd.read_text().split("gitdir:")[1].strip())
            for name in ("HEAD", "index", "logs/HEAD"):
                if (gitdir / name).exists():
                    os.utime(gitdir / name, (old, old))

    def api(self, method: str, path: str, body: Any = None, **kw: Any) -> Any:
        self.calls.append(path)
        hook = self.hooks.get(path)
        if hook:
            hook()
            self.age_all()
        if self.down or path in self.broken:
            raise SystemExit(1)
        if path.startswith("/api/projects"):
            return [
                {
                    "id": 5,
                    "slug": "haiplane",
                    "repo": REPO_SLUG,
                    "default_branch": "develop",
                }
            ]
        parts = path.split("/")
        task = self.tasks.get(int(parts[3])) if len(parts) > 3 else None
        if task is None:
            raise SystemExit(1)
        if path.endswith("/delivery"):
            return self.delivery[task["id"]]
        return task

    def scenario(self) -> dict[str, int]:
        """Сценарии (a)-(i) из AC-1 плюс текущая копия (j)."""
        for n in SLUGS:
            if n == 108:
                path = self.wt(n)
                path.parent.mkdir(exist_ok=True)
                _sh(self.clone, "worktree", "add", "-q", "--detach", str(path))
                self.tasks[n] = {
                    "id": n, "status": "completed", "archived": False,
                    "branch": "", "submission_sha": _sh(path, "rev-parse", "HEAD"),
                    "result_kind": "commit", "project": {"id": 5},
                }  # fmt: skip
                self.delivery[n] = {"state": "delivered", "reason": "gate"}
            elif n == 109:
                self.add(n, branch="task-110/i-id-mismatch")
            else:
                self.add(n)
        # (a) HEAD == submission_sha после коммита работы
        self.tasks[101]["submission_sha"] = self.commit(101, "work.txt")
        # (b) HEAD - предок submission_sha
        self.commit(102, "w1.txt")
        self.tasks[102]["submission_sha"] = self.commit(102, "w2.txt")
        _sh(self.wt(102), "reset", "-q", "--hard", "HEAD~1")
        # (c) локальный коммит после сдачи
        self.tasks[103]["submission_sha"] = self.commit(103, "w3.txt")
        self.commit(103, "after-submit.txt")
        # (d) незакоммиченные правки
        (self.wt(104) / "dirty.txt").write_text("wip")
        # (e) running; (f) archived при активном статусе; (g) доставка не подтверждена
        self.tasks[105]["status"] = "running"
        self.tasks[106]["status"] = "running"
        self.tasks[106]["archived"] = True
        self.delivery[107] = {"state": "pr_open", "reason": "PR открыт"}
        return {"removable": 2}

    def run(self, *argv: str, monkeypatch: pytest.MonkeyPatch) -> int:
        monkeypatch.setattr(cli, "_api", self.api)
        self.age_all()
        parsed = cli.build_parser().parse_args(["worktree", *argv])
        return int(parsed.func(parsed))


def _lines(out: str, task_id: int) -> list[str]:
    needle = f"task-{task_id}"
    return [line for line in out.splitlines() if f"/{needle}" in line]


def _branches(clone: Path) -> set[str]:
    return set(
        _sh(clone, "for-each-ref", "--format=%(refname:short)", "refs/heads").split()
    )
