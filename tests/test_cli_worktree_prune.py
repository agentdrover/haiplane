"""hp-hub worktree prune (#2038): уборка копий доставленных задач.

Тесты идут на настоящих git-репозиториях в tmp_path: bare origin, клон и
worktree-копии на каноническом пути #459. Хаб подменён заглушкой _api.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any

import aiosqlite
import pytest
from httpx import AsyncClient

from hub import cli
from hub import cli_worktree_prune as prune
from tests.test_accept_without_delivery import _completed_task

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


def test_prune_removes_only_delivered_clean_worktrees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-1: dry-run ничего не удаляет и объясняет; --apply убирает ровно (a), (b)."""
    w = World(tmp_path)
    w.scenario()
    before = _branches(w.clone)
    monkeypatch.chdir(w.wt(111))  # (j) текущая копия: не трогается

    assert w.run("prune", "--repo", str(w.clone), monkeypatch=monkeypatch) == 0
    out = capsys.readouterr().out
    assert all(w.wt(n).is_dir() for n in SLUGS), "dry-run ничего не удаляет"
    for n in (101, 102):
        assert any("удалить" in x for x in _lines(out, n)), (n, out)
    expect = {
        103: "после сдачи", 104: "не чисто", 105: "running", 106: "running",
        107: "pr_open", 108: "detached", 109: "ID", 111: "текущ",
    }  # fmt: skip
    for n, why in expect.items():
        lines = _lines(out, n)
        assert lines and all("оставить" in x and why in x for x in lines), (n, out)
    assert "ничего не удалено" in out

    assert (
        w.run("prune", "--apply", "--repo", str(w.clone), monkeypatch=monkeypatch) == 0
    )
    capsys.readouterr()
    gone = {n for n in SLUGS if not w.wt(n).exists()}
    assert gone == {101, 102}
    assert _branches(w.clone) == before, "локальные ветки целы"
    assert (w.wt(104) / "dirty.txt").read_text() == "wip"

    # hp-hub worktree N работает как раньше: пересоздаёт убранную, находит оставшуюся.
    assert w.run("101", "--repo", str(w.clone), monkeypatch=monkeypatch) == 0
    assert capsys.readouterr().out.strip() == str(w.wt(101).resolve())
    assert w.run("103", "--repo", str(w.clone), monkeypatch=monkeypatch) == 0
    assert capsys.readouterr().out.strip() == str(w.wt(103).resolve())


def test_prune_never_removes_when_the_status_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-2: неизвестный статус или чистота - копия цела, сбор идёт до удаления."""
    # 1. хаб недоступен целиком
    w = World(tmp_path / "down")
    w.add(101)
    w.down = True
    assert (
        w.run("prune", "--apply", "--repo", str(w.clone), monkeypatch=monkeypatch) != 0
    )
    out = capsys.readouterr().out
    assert w.wt(101).is_dir() and "статус не прочитан" in out

    # 2. один ответ - ошибка: его копия цела, остальные ответы собраны ДО удаления
    (tmp_path / "one").mkdir()
    w = World(tmp_path / "one")
    for n in (101, 102, 103):
        w.add(n)
    w.broken.add("/api/tasks/102/delivery")
    seen: list[bool] = []
    for p in ("/api/tasks/101", "/api/tasks/102", "/api/tasks/103/delivery"):
        w.hooks[p] = lambda: seen.append(w.wt(101).is_dir())
    assert (
        w.run("prune", "--apply", "--repo", str(w.clone), monkeypatch=monkeypatch) != 0
    )
    out = capsys.readouterr().out
    assert seen and all(seen), "первое удаление не раньше последнего ответа хаба"
    assert w.wt(102).is_dir() and any(
        "статус не прочитан" in x for x in _lines(out, 102)
    )
    assert not w.wt(101).exists() and not w.wt(103).exists()

    # 3. git status вернул rc != 0
    (tmp_path / "gitrc").mkdir()
    w = World(tmp_path / "gitrc")
    w.add(101)
    w.add(102)
    real_git = prune._git

    def flaky(cwd: Any, *args: str, **kw: Any) -> Any:
        if "status" in args and Path(cwd).name == "task-101":
            return 128, "", "fatal: boom"
        return real_git(cwd, *args, **kw)

    monkeypatch.setattr(prune, "_git", flaky)
    assert (
        w.run("prune", "--apply", "--repo", str(w.clone), monkeypatch=monkeypatch) != 0
    )
    out = capsys.readouterr().out
    assert w.wt(101).is_dir() and not w.wt(102).exists()
    assert any("чистота не определена" in x for x in _lines(out, 101))
    monkeypatch.setattr(prune, "_git", real_git)


def test_prune_rechecks_head_right_before_each_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Коммит появился после проверки, но до remove: копия остаётся."""
    w = World(tmp_path)
    w.add(101)
    w.add(102)
    w.hooks["/api/tasks/102/delivery"] = lambda: w.commit(101, "late.txt")
    assert (
        w.run("prune", "--apply", "--repo", str(w.clone), monkeypatch=monkeypatch) == 0
    )
    out = capsys.readouterr().out
    assert w.wt(101).is_dir() and not w.wt(102).exists()
    assert any("изменилась" in x for x in _lines(out, 101)), out


def test_prune_names_but_never_touches_foreign_copies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Основной клон, копия вне канонического пути, ветка задачи не та - остаются."""
    w = World(tmp_path)
    w.add(101)
    elsewhere = tmp_path / "wt-elsewhere" / "task-102"
    elsewhere.parent.mkdir()
    _sh(w.clone, "worktree", "add", "-q", "-b", "task-102/x", str(elsewhere), "develop")
    w.tasks[102] = {**w.tasks[101], "id": 102, "branch": "task-102/x"}
    w.delivery[102] = {"state": "delivered"}
    w.add(103)
    w.tasks[103]["branch"] = "task-103/other-name"  # task.branch расходится
    standalone = tmp_path / "ai_auditor-task-7"
    standalone.mkdir()
    _sh(standalone, "init", "-q")
    w.tasks[101]["project"] = {"id": 5}
    assert (
        w.run("prune", "--apply", "--repo", str(w.clone), monkeypatch=monkeypatch) == 0
    )
    out = capsys.readouterr().out
    assert not w.wt(101).exists()
    assert elsewhere.is_dir() and w.wt(103).is_dir() and w.clone.is_dir()
    assert str(elsewhere) in out and "канонического" in out
    assert any("ветка задачи" in x for x in _lines(out, 103))
    assert str(w.clone) in out and "основной" in out
    assert str(standalone) in out and standalone.is_dir()


def test_prune_refuses_a_clone_of_another_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    w = World(tmp_path)
    w.add(101)
    _sh(
        w.clone,
        "remote",
        "set-url",
        "origin",
        str(tmp_path / "o" / "other" / "repo.git"),
    )
    assert (
        w.run("prune", "--apply", "--repo", str(w.clone), monkeypatch=monkeypatch) != 0
    )
    assert w.wt(101).is_dir()


def test_worktree_number_form_and_prune_flags_do_not_mix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = World(tmp_path)
    w.add(101)
    assert w.run("101", "--apply", "--repo", str(w.clone), monkeypatch=monkeypatch) != 0
    assert w.run("nonsense", "--repo", str(w.clone), monkeypatch=monkeypatch) != 0


async def test_delivery_endpoint_reads_task_delivery(
    client: AsyncClient, db: aiosqlite.Connection
) -> None:
    gated = await _completed_task(
        db, client, title="Gated", pr=901, merged_by_gate=True
    )
    resp = await client.get(f"/api/tasks/{gated}/delivery")
    assert resp.status_code == 200 and resp.json()["state"] == "delivered"
    bare = await _completed_task(db, client, title="No PR", pr=902)
    await db.execute("UPDATE tasks SET pr_number=NULL WHERE id=?", (bare,))
    await db.commit()
    assert (await client.get(f"/api/tasks/{bare}/delivery")).json()[
        "state"
    ] == "unknown"
    assert (await client.get("/api/tasks/99999/delivery")).status_code == 404


def test_delivery_route_is_outside_chat_pair_allowlists() -> None:
    from hub import auth

    for table in (
        auth.CHAT_PAIR_ALLOWLIST,
        auth.CHAT_PAIR_IMPLEMENTER_ALLOWLIST,
        auth.CHAT_PAIR_REVIEWER_ALLOWLIST,
        auth.STEWARD_ALLOWLIST,
    ):
        assert ("GET", "/api/tasks/{task_id}/delivery") not in table


def test_prune_removes_with_plain_git_worktree_remove_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Без --force, без удаления веток: ровно ``git worktree remove <путь>``."""
    w = World(tmp_path)
    w.add(101)
    seen: list[tuple[str, ...]] = []
    real_git = prune._git

    def spy(cwd: Any, *args: str, **kw: Any) -> Any:
        if args[:1] in (("worktree",), ("branch",), ("update-ref",)):
            seen.append(args)
        return real_git(cwd, *args, **kw)

    monkeypatch.setattr(prune, "_git", spy)
    assert (
        w.run("prune", "--apply", "--repo", str(w.clone), monkeypatch=monkeypatch) == 0
    )
    removes = [a for a in seen if a[:2] == ("worktree", "remove")]
    assert removes == [("worktree", "remove", str(w.wt(101)))], seen
    assert not [a for a in seen if a[0] in ("branch", "update-ref")]


def test_prune_keeps_a_copy_whose_task_has_no_submission_sha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    w = World(tmp_path)
    w.add(101)
    w.tasks[101]["submission_sha"] = ""
    assert (
        w.run("prune", "--apply", "--repo", str(w.clone), monkeypatch=monkeypatch) == 0
    )
    assert w.wt(101).is_dir()
    assert "submission_sha" in capsys.readouterr().out


def test_prune_leaves_locked_and_lost_copies_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import shutil

    w = World(tmp_path)
    w.add(101)
    w.add(102)
    _sh(w.clone, "worktree", "lock", str(w.wt(101)))
    shutil.rmtree(w.wt(102))  # git видит путь как prunable
    assert (
        w.run("prune", "--apply", "--repo", str(w.clone), monkeypatch=monkeypatch) == 0
    )
    out = capsys.readouterr().out
    assert w.wt(101).is_dir()
    assert any("заблокирован" in x for x in _lines(out, 101)), out
    assert any("prunable" in x for x in _lines(out, 102)), out


def test_prune_keeps_a_copy_whose_branch_changed_after_the_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Ветку сменили между проверкой и remove (HEAD и чистота те же): копия цела."""
    w = World(tmp_path)
    w.add(101)
    w.add(102)
    w.hooks["/api/tasks/102/delivery"] = lambda: _sh(
        w.wt(101), "switch", "-q", "-c", "task-101/elsewhere"
    )
    assert (
        w.run("prune", "--apply", "--repo", str(w.clone), monkeypatch=monkeypatch) == 0
    )
    out = capsys.readouterr().out
    assert w.wt(101).is_dir() and not w.wt(102).exists()
    assert any("ветка копии другая" in x for x in _lines(out, 101)), out


def test_prune_keeps_a_path_that_now_holds_another_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Путь в списке клона, но в нём уже самостоятельный клон: common-dir чужой."""
    import shutil

    w = World(tmp_path)
    w.add(101)
    shutil.rmtree(w.wt(101))
    _sh(tmp_path, "clone", "-q", str(w.clone), str(w.wt(101)))
    assert (
        w.run("prune", "--apply", "--repo", str(w.clone), monkeypatch=monkeypatch) == 0
    )
    out = capsys.readouterr().out
    assert w.wt(101).is_dir()
    assert any("чужой клон" in x for x in _lines(out, 101)), out


def test_prune_sees_untracked_files_even_when_git_is_told_to_hide_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """status.showUntrackedFiles=no прячет файлы и от git worktree remove."""
    w = World(tmp_path)
    w.add(101)
    _sh(w.clone, "config", "status.showUntrackedFiles", "no")
    (w.wt(101) / "new_source.py").write_text("work")
    assert (
        w.run("prune", "--apply", "--repo", str(w.clone), monkeypatch=monkeypatch) == 0
    )
    out = capsys.readouterr().out
    assert (w.wt(101) / "new_source.py").read_text() == "work"
    assert any("не чисто" in x for x in _lines(out, 101)), out


def test_prune_keeps_a_copy_with_a_nested_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """.claude/worktrees/ внутри копии игнорируется git, но удаление его сотрёт."""
    w = World(tmp_path)
    w.add(101)
    ignore = w.wt(101) / ".git"
    info = Path(_sh(w.wt(101), "rev-parse", "--git-path", "info/exclude"))
    info = info if info.is_absolute() else w.wt(101) / info
    info.parent.mkdir(parents=True, exist_ok=True)
    info.write_text(".claude/worktrees/\n")
    assert ignore.exists()
    inner = w.wt(101) / ".claude" / "worktrees" / "other"
    inner.parent.mkdir(parents=True)
    _sh(w.clone, "worktree", "add", "-q", "-b", "task-999/inner", str(inner), "develop")
    (inner / "wip.txt").write_text("uncommitted")
    assert _sh(w.wt(101), "status", "--porcelain") == ""
    assert (
        w.run("prune", "--apply", "--repo", str(w.clone), monkeypatch=monkeypatch) == 0
    )
    out = capsys.readouterr().out
    assert (inner / "wip.txt").read_text() == "uncommitted"
    assert any("внутри копии" in x for x in _lines(out, 101)), out


def test_prune_keeps_a_copy_with_a_nested_repository_not_in_the_worktree_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    w = World(tmp_path)
    w.add(101)
    info = Path(_sh(w.wt(101), "rev-parse", "--git-path", "info/exclude"))
    info = info if info.is_absolute() else w.wt(101) / info
    info.parent.mkdir(parents=True, exist_ok=True)
    info.write_text("scratch/\n")
    nested = w.wt(101) / "scratch" / "repo"
    nested.mkdir(parents=True)
    _sh(nested, "init", "-q")
    (nested / "x.txt").write_text("keep")
    assert (
        w.run("prune", "--apply", "--repo", str(w.clone), monkeypatch=monkeypatch) == 0
    )
    assert (nested / "x.txt").read_text() == "keep"
    assert "вложенный git-репозиторий" in capsys.readouterr().out


def test_prune_keeps_a_copy_with_a_detached_commit_only_it_has(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Коммит сделан и потерян из виду (checkout --detach, затем назад): reflog помнит."""
    w = World(tmp_path)
    w.add(101)
    _sh(w.wt(101), "switch", "-q", "--detach")
    w.commit(101, "lost.txt")
    _sh(w.wt(101), "switch", "-q", w.tasks[101]["branch"])
    assert _sh(w.wt(101), "status", "--porcelain") == ""
    assert (
        w.run("prune", "--apply", "--repo", str(w.clone), monkeypatch=monkeypatch) == 0
    )
    out = capsys.readouterr().out
    assert w.wt(101).is_dir()
    assert any("есть только в этой копии" in x for x in _lines(out, 101)), out


def test_prune_accepts_a_reflog_commit_that_is_on_a_remote_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = World(tmp_path)
    w.add(101)
    extra = w.commit(101, "pushed.txt")
    _sh(w.wt(101), "reset", "-q", "--hard", "HEAD~1")
    # extra живёт только в reflog копии, но уже есть на удалённой ветке
    _sh(w.clone, "update-ref", "refs/remotes/origin/pushed-only", extra)
    assert (
        w.run("prune", "--apply", "--repo", str(w.clone), monkeypatch=monkeypatch) == 0
    )
    assert not w.wt(101).exists()


def test_prune_keeps_a_recently_touched_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    w = World(tmp_path)
    w.add(101)
    w.add(102)
    w.fresh.add(101)  # gitdir 101 только что менялся
    assert (
        w.run("prune", "--apply", "--repo", str(w.clone), monkeypatch=monkeypatch) == 0
    )
    out = capsys.readouterr().out
    assert w.wt(101).is_dir() and not w.wt(102).exists()
    assert any("менялась" in x and "мин назад" in x for x in _lines(out, 101)), out
    # порог - параметр: с нулём та же копия уходит
    assert (
        w.run(
            "prune", "--apply", "--min-idle-minutes", "0", "--repo", str(w.clone),
            monkeypatch=monkeypatch,
        )
        == 0
    )  # fmt: skip
    assert not w.wt(101).exists()


@pytest.mark.parametrize("name", ["HEAD", "index", "logs/HEAD"])
def test_prune_keeps_a_copy_when_any_gitdir_file_is_fresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    w = World(tmp_path)
    w.add(101)
    w.age_all()
    gitdir = Path(_sh(w.wt(101), "rev-parse", "--absolute-git-dir"))
    os.utime(gitdir / name, None)  # только этот файл свежий
    monkeypatch.setattr(w, "age_all", lambda: None)  # run() не должен его состарить
    assert (
        w.run("prune", "--apply", "--repo", str(w.clone), monkeypatch=monkeypatch) == 0
    )
    assert w.wt(101).is_dir()


def test_prune_looking_does_not_make_a_copy_look_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """git status без --no-optional-locks переписывает index и сам освежает простой."""
    w = World(tmp_path)
    w.add(101)
    w.age_all()
    future = time.time() + 5
    os.utime(w.wt(101) / "f.txt", (future, future))  # index станет устаревшим
    gitdir = Path(_sh(w.wt(101), "rev-parse", "--absolute-git-dir"))
    before = (gitdir / "index").stat().st_mtime
    monkeypatch.setattr(w, "age_all", lambda: None)
    w.run(
        "prune",
        "--min-idle-minutes",
        "0",
        "--repo",
        str(w.clone),
        monkeypatch=monkeypatch,
    )
    assert (gitdir / "index").stat().st_mtime == before


def test_prune_keeps_a_copy_when_the_reflog_cannot_be_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    w = World(tmp_path)
    w.add(101)
    real_git = prune._git

    def flaky(cwd: Any, *args: str, **kw: Any) -> Any:
        if "reflog" in args:
            return 128, "", "fatal: boom"
        return real_git(cwd, *args, **kw)

    monkeypatch.setattr(prune, "_git", flaky)
    assert (
        w.run("prune", "--apply", "--repo", str(w.clone), monkeypatch=monkeypatch) != 0
    )
    assert w.wt(101).is_dir()
    assert "reflog HEAD не прочитан" in capsys.readouterr().out


def test_prune_keeps_a_copy_around_a_registered_worktree_without_dot_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Вложенная запись в списке клона, а её .git-файл пропал: обход его не увидит."""
    w = World(tmp_path)
    w.add(101)
    info = Path(
        _sh(
            w.wt(101),
            "rev-parse",
            "--path-format=absolute",
            "--git-path",
            "info/exclude",
        )
    )
    info.parent.mkdir(parents=True, exist_ok=True)
    info.write_text("nested/\n")
    inner = w.wt(101) / "nested" / "other"
    inner.parent.mkdir()
    _sh(w.clone, "worktree", "add", "-q", "-b", "task-998/inner", str(inner), "develop")
    (inner / ".git").unlink()
    (inner / "wip.txt").write_text("keep")
    assert (
        w.run("prune", "--apply", "--repo", str(w.clone), monkeypatch=monkeypatch) == 0
    )
    out = capsys.readouterr().out
    assert (inner / "wip.txt").read_text() == "keep"
    assert any("другой worktree клона" in x for x in _lines(out, 101)), out


def test_prune_does_not_take_an_external_worktree_behind_a_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Канонический task-N - симлинк на внешнюю зарегистрированную копию."""
    w = World(tmp_path)
    w.add(102)  # чтобы существовал каталог .haiplane-worktrees
    ext = tmp_path / "personal" / "task-101"
    ext.parent.mkdir()
    _sh(w.clone, "worktree", "add", "-q", "-b", "task-101/work", str(ext), "develop")
    w.tasks[101] = {**w.tasks[102], "id": 101, "branch": "task-101/work"}
    w.tasks[101]["submission_sha"] = _sh(ext, "rev-parse", "HEAD")
    w.delivery[101] = {"state": "delivered"}
    w.wt(101).symlink_to(ext)
    assert (
        w.run("prune", "--apply", "--repo", str(w.clone), monkeypatch=monkeypatch) == 0
    )
    out = capsys.readouterr().out
    assert ext.is_dir()
    assert any(str(ext) in x and "канонического" in x for x in out.splitlines()), out


def test_prune_does_not_take_a_canonical_path_that_is_itself_a_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import shutil

    w = World(tmp_path)
    w.add(101)
    moved = tmp_path / "moved-101"
    shutil.move(str(w.wt(101)), str(moved))
    w.wt(101).symlink_to(moved)
    gitdir_file = w.clone / ".git" / "worktrees" / "task-101" / "gitdir"
    gitdir_file.write_text(f"{w.wt(101)}/.git\n")
    assert (
        w.run("prune", "--apply", "--repo", str(w.clone), monkeypatch=monkeypatch) == 0
    )
    out = capsys.readouterr().out
    assert moved.is_dir()
    assert "симлинк" in out, out
