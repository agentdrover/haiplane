"""hp-hub worktree prune/purge (#2038, раунд 4): карантин вместо удаления."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from hub import cli_worktree_prune as prune
from tests.worktree_prune_support import World, _lines, _sh


def _gitdir(w: World, task_id: int) -> Path:
    return Path(_sh(w.wt(task_id), "rev-parse", "--absolute-git-dir"))


def _registered(w: World) -> list[str]:
    out = _sh(w.clone, "worktree", "list", "--porcelain")
    return [x[9:] for x in out.splitlines() if x.startswith("worktree ")]


def _real(p: Path) -> str:
    return str(p.resolve())


def test_prune_apply_moves_the_copy_to_the_trash_and_keeps_it_registered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    w = World(tmp_path)
    w.add(101)
    w.commit(101, "work.txt")
    w.tasks[101]["submission_sha"] = _sh(w.wt(101), "rev-parse", "HEAD")
    sha = _sh(w.wt(101), "rev-parse", "HEAD")
    assert (
        w.run("prune", "--apply", "--repo", str(w.clone), monkeypatch=monkeypatch) == 0
    )
    out = capsys.readouterr().out
    assert not w.wt(101).exists()
    moved = [p for p in w.trash().iterdir() if p.name.startswith("task-101-")]
    assert len(moved) == 1, out
    assert (moved[0] / "work.txt").read_text() == "work.txt"
    assert (moved[0] / "f.txt").exists()
    assert _real(moved[0]) in [_real(Path(p)) for p in _registered(w)]
    assert _sh(moved[0], "rev-parse", "HEAD") == sha
    assert "в карантине:" in out and str(moved[0]) in out
    # ветка цела, копия отцеплена от неё: hp-hub worktree N заведёт новую
    assert w.tasks[101]["branch"] in _sh(w.clone, "branch", "--format=%(refname:short)")
    assert _sh(moved[0], "branch", "--show-current") == ""


# --- раунд 4: пункты 2a-2d ------------------------------------------------------
def _unreachable_commit(w: World, task_id: int) -> str:
    wt = w.wt(task_id)
    tree = _sh(wt, "rev-parse", "HEAD^{tree}")
    head = _sh(wt, "rev-parse", "HEAD")
    return _sh(wt, "commit-tree", tree, "-p", head, "-m", "only here")


@pytest.mark.parametrize("name", ["ORIG_HEAD", "FETCH_HEAD", "AUTO_MERGE"])
def test_prune_keeps_a_copy_whose_pseudo_ref_pins_work_only_it_has(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    name: str,
) -> None:
    w = World(tmp_path)
    w.add(101)
    sha = _unreachable_commit(w, 101)
    if name == "FETCH_HEAD":
        (_gitdir(w, 101) / name).write_text(f"{sha}\t\tbranch 'x' of somewhere\n")
    else:
        _sh(w.wt(101), "update-ref", name, sha)
    assert (_gitdir(w, 101) / name).exists()
    assert (
        w.run("prune", "--apply", "--repo", str(w.clone), monkeypatch=monkeypatch) == 0
    )
    out = capsys.readouterr().out
    assert w.wt(101).is_dir()
    assert any(name in x and "только в этой копии" in x for x in _lines(out, 101)), out


def test_prune_accepts_a_pseudo_ref_that_points_at_delivered_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = World(tmp_path)
    w.add(101)
    _sh(w.wt(101), "update-ref", "ORIG_HEAD", w.tasks[101]["submission_sha"])
    (_gitdir(w, 101) / "FETCH_HEAD").write_text(
        f"{w.tasks[101]['submission_sha']}\t\tbranch 'x' of somewhere\n"
    )
    assert (
        w.run("prune", "--apply", "--repo", str(w.clone), monkeypatch=monkeypatch) == 0
    )
    assert not w.wt(101).exists()


def test_prune_fingerprint_sees_a_new_file_outside_the_allowlisted_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """refs/worktree/wip появился во время долгого обхода: HEAD и reflog прежние."""
    w = World(tmp_path)
    w.add(101)
    real = prune._nested_reason
    calls: list[int] = []

    def slow_walk(v: Any) -> str:
        why = real(v)
        calls.append(1)
        if len(calls) == 2:
            target = _gitdir(w, 101) / "refs" / "worktree" / "wip"
            target.parent.mkdir(parents=True)
            target.write_text(_sh(w.wt(101), "rev-parse", "HEAD") + "\n")
        return why

    monkeypatch.setattr(prune, "_nested_reason", slow_walk)
    assert (
        w.run("prune", "--apply", "--repo", str(w.clone), monkeypatch=monkeypatch) == 0
    )
    out = capsys.readouterr().out
    assert w.wt(101).is_dir() and len(calls) == 2
    assert any("отпечаток" in x for x in _lines(out, 101)), out


def test_prune_treats_an_unreadable_gitdir_subdirectory_as_unread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    w = World(tmp_path)
    w.add(101)
    hidden = _gitdir(w, 101) / "refs" / "worktree"
    hidden.mkdir(parents=True)
    (hidden / "wip").write_text(_sh(w.wt(101), "rev-parse", "HEAD") + "\n")
    hidden.chmod(0)
    try:
        rc = w.run("prune", "--apply", "--repo", str(w.clone), monkeypatch=monkeypatch)
    finally:
        hidden.chmod(0o755)
    out = capsys.readouterr().out
    assert rc != 0 and w.wt(101).is_dir()
    assert any("не прочитан" in x for x in _lines(out, 101)), out


@pytest.mark.parametrize("which", [1, 2])
def test_prune_fingerprint_read_error_is_unread_with_a_nonzero_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    which: int,
) -> None:
    w = World(tmp_path)
    w.add(101)
    real = prune._fingerprint
    calls: list[int] = []

    def flaky(v: Any) -> Any:
        calls.append(1)
        return None if len(calls) == which else real(v)

    monkeypatch.setattr(prune, "_fingerprint", flaky)
    assert (
        w.run("prune", "--apply", "--repo", str(w.clone), monkeypatch=monkeypatch) != 0
    )
    out = capsys.readouterr().out
    assert w.wt(101).is_dir()
    assert any("отпечаток" in x and "не снят" in x for x in _lines(out, 101)), out


def test_prune_keeps_a_copy_with_a_git_directory_inside_a_regenerable_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    w = World(tmp_path)
    w.add(101)
    info = Path(
        _sh(
            w.clone, "rev-parse", "--path-format=absolute", "--git-path", "info/exclude"
        )
    )
    info.parent.mkdir(parents=True, exist_ok=True)
    info.write_text("node_modules/\n")
    nested = w.wt(101) / "node_modules" / "pkg"
    nested.mkdir(parents=True)
    _sh(nested, "init", "-q")
    (nested / "unpublished.txt").write_text("work")
    assert (
        w.run("prune", "--apply", "--repo", str(w.clone), monkeypatch=monkeypatch) == 0
    )
    out = capsys.readouterr().out
    assert (nested / "unpublished.txt").exists() and w.wt(101).is_dir()
    assert any("вложенный git-репозиторий" in x for x in _lines(out, 101)), out
    assert os.path.isdir(nested / ".git")


def test_prune_keeps_a_copy_whose_pseudo_ref_cannot_be_parsed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    w = World(tmp_path)
    w.add(101)
    (_gitdir(w, 101) / "ORIG_HEAD").write_text("garbage, not a sha\n")
    assert (
        w.run("prune", "--apply", "--repo", str(w.clone), monkeypatch=monkeypatch) != 0
    )
    out = capsys.readouterr().out
    assert w.wt(101).is_dir()
    assert any("ORIG_HEAD" in x and "не разобрано" in x for x in _lines(out, 101)), out


def test_prune_treats_an_unreadable_directory_inside_the_copy_as_unread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Каталог внутри .venv нечитаем: обход вложенных .git его не видит - не «пусто»."""
    w = World(tmp_path)
    w.add(101)
    info = Path(
        _sh(
            w.clone, "rev-parse", "--path-format=absolute", "--git-path", "info/exclude"
        )
    )
    info.parent.mkdir(parents=True, exist_ok=True)
    info.write_text(".venv/\n")
    hidden = w.wt(101) / ".venv" / "private"
    hidden.mkdir(parents=True)
    (hidden / "x.py").write_text("x")
    hidden.chmod(0)
    try:
        rc = w.run("prune", "--apply", "--repo", str(w.clone), monkeypatch=monkeypatch)
    finally:
        hidden.chmod(0o755)
    out = capsys.readouterr().out
    assert rc != 0 and w.wt(101).is_dir()
    assert any("обход копии не прочитан" in x for x in _lines(out, 101)), out


def test_prune_fingerprint_listing_error_after_the_checks_is_unread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """gitdir стал нечитаем между проверкой и вторым отпечатком."""
    w = World(tmp_path)
    w.add(101)
    hidden = _gitdir(w, 101) / "refs"
    hidden.mkdir(exist_ok=True)
    real = prune._nested_reason
    calls: list[int] = []

    def late(v: Any) -> str:
        why = real(v)
        calls.append(1)
        if len(calls) == 2:
            hidden.chmod(0)
        return why

    monkeypatch.setattr(prune, "_nested_reason", late)
    try:
        rc = w.run("prune", "--apply", "--repo", str(w.clone), monkeypatch=monkeypatch)
    finally:
        hidden.chmod(0o755)
    out = capsys.readouterr().out
    assert rc != 0 and w.wt(101).is_dir() and len(calls) == 2
    assert any("отпечаток" in x and "не снят" in x for x in _lines(out, 101)), out
