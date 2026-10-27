"""hp-hub worktree prune (#2038): скрытое состояние копии - белый список (раунды 2-3)."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import pytest

from hub import cli_worktree_prune as prune
from tests.worktree_prune_support import World, _lines, _sh


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
    from hub import cli_worktree_trash as trash

    w = World(tmp_path)
    w.add(101)
    monkeypatch.setattr(
        trash, "reflog_shas", lambda gitdir: ([], "logs/HEAD не прочитан (test)")
    )
    assert (
        w.run("prune", "--apply", "--repo", str(w.clone), monkeypatch=monkeypatch) != 0
    )
    assert w.wt(101).is_dir()
    assert "logs/HEAD не прочитан" in capsys.readouterr().out


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


# --- раунд 3: белый список вместо запретов ------------------------------------
def _exclude(w: World, *patterns: str) -> None:
    """Игнорируемые пути клона: info/exclude общего каталога."""
    info = Path(
        _sh(
            w.clone, "rev-parse", "--path-format=absolute", "--git-path", "info/exclude"
        )
    )
    info.parent.mkdir(parents=True, exist_ok=True)
    info.write_text("\n".join(patterns) + "\n")


def _gitdir(w: World, task_id: int) -> Path:
    return Path(_sh(w.wt(task_id), "rev-parse", "--absolute-git-dir"))


def test_prune_keeps_ignored_files_but_not_regenerable_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """.env и .claude/settings.local.json - в копии, git их игнорирует: копия остаётся."""
    w = World(tmp_path)
    for n in (101, 102, 103):
        w.add(n)
    _exclude(
        w, ".env", ".claude/", ".venv/", "__pycache__/", "node_modules/", ".coverage"
    )
    (w.wt(101) / ".env").write_text("SECRET=1")
    (w.wt(102) / ".claude").mkdir()
    (w.wt(102) / ".claude" / "settings.local.json").write_text("{}")
    for rel in (".venv/lib/x.py", "pkg/__pycache__/a.pyc", "node_modules/m/i.js"):
        (w.wt(103) / rel).parent.mkdir(parents=True, exist_ok=True)
        (w.wt(103) / rel).write_text("x")
    (w.wt(103) / ".coverage").write_text("x")
    assert (
        w.run("prune", "--apply", "--repo", str(w.clone), monkeypatch=monkeypatch) == 0
    )
    out = capsys.readouterr().out
    assert (w.wt(101) / ".env").read_text() == "SECRET=1"
    assert (w.wt(102) / ".claude" / "settings.local.json").exists()
    assert not w.wt(103).exists(), out
    assert any(".env" in x and "не чисто" in x for x in _lines(out, 101)), out
    assert any(".claude" in x and "не чисто" in x for x in _lines(out, 102)), out


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
def test_prune_sees_edits_hidden_by_index_flags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    flag: str,
) -> None:
    w = World(tmp_path)
    w.add(101)
    _sh(w.wt(101), "update-index", flag, "f.txt")
    (w.wt(101) / "f.txt").write_text("edited and hidden from status")
    assert _sh(w.wt(101), "status", "--porcelain") == ""
    assert (
        w.run("prune", "--apply", "--repo", str(w.clone), monkeypatch=monkeypatch) == 0
    )
    out = capsys.readouterr().out
    assert (w.wt(101) / "f.txt").read_text() == "edited and hidden from status"
    assert any("assume-unchanged" in x and "f.txt" in x for x in _lines(out, 101)), out


@pytest.mark.parametrize(
    "name",
    [
        "refs/worktree/wip",
        "refs/bisect/bad",
        "logs/refs/worktree/wip",
        "MERGE_HEAD",
        "CHERRY_PICK_HEAD",
        "BISECT_LOG",
        "rebase-merge/head-name",
        "sequencer/todo",
    ],
)
def test_prune_keeps_a_copy_whose_gitdir_holds_anything_unlisted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    name: str,
) -> None:
    w = World(tmp_path)
    w.add(101)
    target = _gitdir(w, 101) / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_sh(w.wt(101), "rev-parse", "HEAD") + "\n")
    assert (
        w.run("prune", "--apply", "--repo", str(w.clone), monkeypatch=monkeypatch) == 0
    )
    out = capsys.readouterr().out
    assert target.exists() and w.wt(101).is_dir()
    assert any("gitdir" in x and name.split("/")[0] in x for x in _lines(out, 101)), out


def test_prune_keeps_a_worktree_private_ref_made_with_update_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = World(tmp_path)
    w.add(101)
    (w.wt(101) / "wip.txt").write_text("wip")
    _sh(w.wt(101), "add", "wip.txt")
    stash = _sh(w.wt(101), "stash", "create")
    _sh(w.wt(101), "reset", "-q", "--hard")
    _sh(w.wt(101), "clean", "-qfd")
    _sh(w.wt(101), "update-ref", "refs/worktree/wip", stash)
    assert (_gitdir(w, 101) / "refs" / "worktree" / "wip").exists()
    assert (
        w.run("prune", "--apply", "--repo", str(w.clone), monkeypatch=monkeypatch) == 0
    )
    assert w.wt(101).is_dir()


def test_prune_takes_a_second_snapshot_right_before_remove(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Коммит в detached HEAD появился ПОСЛЕ первого снимка, во время долгой проверки."""
    w = World(tmp_path)
    w.add(101)
    real = prune._nested_reason
    calls: list[int] = []

    def slow_walk(v: Any) -> str:
        why = real(v)
        calls.append(1)
        if len(calls) == 2:  # вторая проверка - финальная, перед remove
            _sh(w.wt(101), "switch", "-q", "--detach")
            w.commit(101, "raced.txt")
            _sh(w.wt(101), "switch", "-q", w.tasks[101]["branch"])
            w.age_all()
        return why

    monkeypatch.setattr(prune, "_nested_reason", slow_walk)
    assert (
        w.run("prune", "--apply", "--repo", str(w.clone), monkeypatch=monkeypatch) == 0
    )
    out = capsys.readouterr().out
    assert w.wt(101).is_dir() and len(calls) == 2
    assert any("изменилась" in x for x in _lines(out, 101)), out


@pytest.mark.parametrize("mode", ["refused", "moved_anyway", "both", "vanished"])
def test_prune_reports_a_failed_move_as_what_it_is(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mode: str,
) -> None:
    import shutil

    w = World(tmp_path)
    w.add(101)
    real = prune._git

    def failing(cwd: Any, *args: str, **kw: Any) -> Any:
        if args[:2] == ("worktree", "move"):
            dest = Path(args[3])
            if mode == "moved_anyway":
                real(cwd, *args, **kw)
            elif mode == "both":
                dest.mkdir(parents=True)
            elif mode == "vanished":
                shutil.rmtree(w.wt(101))
            return 1, "", "error: boom"
        return real(cwd, *args, **kw)

    monkeypatch.setattr(prune, "_git", failing)
    assert (
        w.run("prune", "--apply", "--repo", str(w.clone), monkeypatch=monkeypatch) != 0
    )
    last = _lines(capsys.readouterr().out, 101)[-1]
    assert "оставить" not in last, last
    expect = {
        "refused": "копия на месте",
        "moved_anyway": "результат неизвестен: копия уже в",
        "both": "оба существуют",
        "vanished": "копии нет ни на месте",
    }
    assert expect[mode] in last, last


@pytest.mark.parametrize(
    "answer",
    [{}, {"state": ""}, {"state": "weird"}, {"state": "unknown", "reason": "gh down"}],
)
def test_prune_counts_anything_but_delivered_as_not_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    answer: dict,
) -> None:
    w = World(tmp_path)
    w.add(101)
    w.delivery[101] = answer
    assert (
        w.run("prune", "--apply", "--repo", str(w.clone), monkeypatch=monkeypatch) != 0
    )
    out = capsys.readouterr().out
    assert w.wt(101).is_dir()
    assert any("доставка не прочитана" in x for x in _lines(out, 101)), out
    if answer.get("reason"):
        assert "gh down" in out


def test_prune_pr_open_is_a_plain_keep_with_exit_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = World(tmp_path)
    w.add(101)
    w.delivery[101] = {"state": "pr_open", "reason": "PR открыт"}
    assert (
        w.run("prune", "--apply", "--repo", str(w.clone), monkeypatch=monkeypatch) == 0
    )
    assert w.wt(101).is_dir()


def test_prune_snapshot_sees_a_change_that_touches_only_the_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """После всех проверок меняется только index (HEAD и reflog те же): копия цела."""
    w = World(tmp_path)
    w.add(101)
    real = prune._tree_reason
    calls: list[int] = []

    def last_check(v: Any) -> str:
        why = real(v)
        calls.append(1)
        if len(calls) == 2:
            _sh(w.wt(101), "update-index", "--assume-unchanged", "f.txt")
            w.age_all()
        return why

    monkeypatch.setattr(prune, "_tree_reason", last_check)
    assert (
        w.run("prune", "--apply", "--repo", str(w.clone), monkeypatch=monkeypatch) == 0
    )
    out = capsys.readouterr().out
    assert w.wt(101).is_dir() and len(calls) == 2
    assert any("отпечаток" in x for x in _lines(out, 101)), out
