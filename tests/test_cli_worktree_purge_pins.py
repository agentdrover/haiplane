"""purge: работа, появившаяся в карантине, закрепляется и держит копию (#2038)."""

from __future__ import annotations

from pathlib import Path

from typing import Any

import pytest

from hub import cli_worktree_purge as purge
from tests.test_cli_worktree_purge import _admin, _quarantined, _refs
from tests.worktree_prune_support import World, _sh


def _late_refs(w: World, name: str) -> list[str]:
    return [r for r in _refs(w, name) if "/late-" in r]


def _fetch_head_commit(w: World, path: Path) -> str:
    tree = _sh(path, "rev-parse", "HEAD^{tree}")
    sha = _sh(path, "commit-tree", tree, "-m", "found in quarantine")
    (_admin(w, path) / "FETCH_HEAD").write_text(f"{sha}\t\tbranch 'x' of somewhere\n")
    return sha


def _commit_then_reset_away(path: Path) -> str:
    (path / "late.txt").write_text("late work")
    _sh(path, "add", "late.txt")
    _sh(path, "commit", "-q", "-m", "late")
    late = _sh(path, "rev-parse", "HEAD")
    _sh(path, "reset", "-q", "--hard", "HEAD~1")
    return late


def test_purge_pins_a_pseudo_ref_commit_and_keeps_the_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    w = World(tmp_path)
    path = _quarantined(w, 101, age_days=8)
    sha = _fetch_head_commit(w, path)
    assert (
        w.run("purge", "--apply", "--repo", str(w.clone), monkeypatch=monkeypatch) == 1
    )
    out = capsys.readouterr().out
    assert path.is_dir()
    pinned = {_sh(w.clone, "rev-parse", r) for r in _late_refs(w, path.name)}
    assert pinned == {sha}
    assert any(
        sha[:12] in x and path.name in x and "FETCH_HEAD" in x for x in out.splitlines()
    ), out


def test_purge_still_keeps_the_copy_after_the_reflog_forgets_the_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    w = World(tmp_path)
    path = _quarantined(w, 101, age_days=8)
    late = _commit_then_reset_away(path)
    assert (
        w.run("purge", "--apply", "--repo", str(w.clone), monkeypatch=monkeypatch) == 1
    )
    assert len(_late_refs(w, path.name)) == 1
    _sh(path, "reflog", "expire", "--expire=now", "--expire-unreachable=now", "--all")
    capsys.readouterr()
    assert (
        w.run("purge", "--apply", "--repo", str(w.clone), monkeypatch=monkeypatch) == 1
    )
    out = capsys.readouterr().out
    assert path.is_dir() and _sh(path, "cat-file", "-t", late) == "commit"
    assert any("после карантина" in x and path.name in x for x in out.splitlines()), out


def test_dry_run_and_apply_give_the_same_verdict_for_unpinned_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    w = World(tmp_path)
    path = _quarantined(w, 101, age_days=8)
    sha = _fetch_head_commit(w, path)
    assert w.run("purge", "--repo", str(w.clone), monkeypatch=monkeypatch) == 1
    dry = capsys.readouterr().out
    line = next(x for x in dry.splitlines() if path.name in x)
    assert line.startswith("оставить") and sha[:12] in line, dry
    assert not any(x.startswith("удалить") for x in dry.splitlines())
    assert _late_refs(w, path.name) == [], "dry-run ничего не закрепляет"
    assert (
        w.run("purge", "--apply", "--repo", str(w.clone), monkeypatch=monkeypatch) == 1
    )
    assert path.is_dir() and len(_late_refs(w, path.name)) == 1


def test_dry_run_keeps_a_copy_that_already_has_late_work_pinned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    w = World(tmp_path)
    path = _quarantined(w, 101, age_days=8)
    _commit_then_reset_away(path)
    assert (
        w.run("purge", "--apply", "--repo", str(w.clone), monkeypatch=monkeypatch) == 1
    )
    _sh(path, "reflog", "expire", "--expire=now", "--expire-unreachable=now", "--all")
    capsys.readouterr()
    assert w.run("purge", "--repo", str(w.clone), monkeypatch=monkeypatch) == 1
    line = next(x for x in capsys.readouterr().out.splitlines() if path.name in x)
    assert line.startswith("оставить") and "после карантина" in line


def test_purge_pins_every_unpinned_sha_under_its_own_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = World(tmp_path)
    path = _quarantined(w, 101, age_days=8)
    tree = _sh(path, "rev-parse", "HEAD^{tree}")
    first = _sh(path, "commit-tree", tree, "-m", "one")
    second = _sh(path, "commit-tree", tree, "-m", "two")
    (_admin(w, path) / "FETCH_HEAD").write_text(f"{first}\t\tbranch 'x' of somewhere\n")
    _sh(path, "update-ref", "ORIG_HEAD", second)
    assert (
        w.run("purge", "--apply", "--repo", str(w.clone), monkeypatch=monkeypatch) == 1
    )
    pinned = {_sh(w.clone, "rev-parse", r) for r in _late_refs(w, path.name)}
    assert pinned == {first, second} and len(_late_refs(w, path.name)) == 2


def test_purge_keeps_a_copy_when_its_late_refs_cannot_be_listed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    w = World(tmp_path)
    path = _quarantined(w, 101, age_days=8)
    real = purge._git

    def failing(cwd: Any, *args: str, **kw: Any) -> Any:
        if args[:1] == ("for-each-ref",) and any("late-" in a for a in args):
            return 1, "", "fatal: boom"
        return real(cwd, *args, **kw)

    monkeypatch.setattr(purge, "_git", failing)
    assert (
        w.run("purge", "--apply", "--repo", str(w.clone), monkeypatch=monkeypatch) == 1
    )
    assert path.is_dir()
    assert "защитные refs не прочитаны" in capsys.readouterr().out


def _unreachable(path: Path, msg: str) -> str:
    return _sh(path, "commit-tree", _sh(path, "rev-parse", "HEAD^{tree}"), "-m", msg)


def test_every_purge_pass_pins_new_work_even_when_late_refs_already_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = World(tmp_path)
    path = _quarantined(w, 101, age_days=8)
    a = _commit_then_reset_away(path)
    assert (
        w.run("purge", "--apply", "--repo", str(w.clone), monkeypatch=monkeypatch) == 1
    )
    assert len(_late_refs(w, path.name)) == 1
    b = _unreachable(path, "b")
    (_admin(w, path) / "FETCH_HEAD").write_text(f"{b}\t\tbranch 'x' of somewhere\n")
    (path / "c.txt").write_text("c")
    _sh(path, "add", "c.txt")
    _sh(path, "commit", "-q", "-m", "c")
    c = _sh(path, "rev-parse", "HEAD")
    _sh(path, "reset", "-q", "--hard", "HEAD~1")
    assert (
        w.run("purge", "--apply", "--repo", str(w.clone), monkeypatch=monkeypatch) == 1
    )
    pinned = {_sh(w.clone, "rev-parse", r) for r in _late_refs(w, path.name)}
    assert pinned == {a, b, c}
    assert all(
        r.rsplit("late-", 1)[1] == _sh(w.clone, "rev-parse", r)
        for r in _late_refs(w, path.name)
    )
    (_admin(w, path) / "FETCH_HEAD").unlink()
    _sh(path, "reflog", "expire", "--expire=now", "--expire-unreachable=now", "--all")
    _sh(w.clone, "prune", "--expire=now")
    for sha in (a, b, c):
        assert _sh(w.clone, "cat-file", "-t", sha) == "commit"
    assert (
        w.run("purge", "--apply", "--repo", str(w.clone), monkeypatch=monkeypatch) == 1
    )
    assert path.is_dir()


def test_a_failed_late_pin_rolls_back_only_this_passes_refs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    w = World(tmp_path)
    path = _quarantined(w, 101, age_days=8)
    a = _commit_then_reset_away(path)
    assert (
        w.run("purge", "--apply", "--repo", str(w.clone), monkeypatch=monkeypatch) == 1
    )
    before = _late_refs(w, path.name)
    b, c = _unreachable(path, "b"), _unreachable(path, "c")
    (_admin(w, path) / "FETCH_HEAD").write_text(
        f"{b}\t\tbranch 'x' of somewhere\n{c}\t\tbranch 'y' of somewhere\n"
    )
    real = purge._git
    made: list[int] = []

    def flaky(cwd: Any, *args: str, **kw: Any) -> Any:
        if args[:1] == ("update-ref",) and "/late-" in args[1] and "-d" not in args:
            made.append(1)
            if len(made) == 2:
                return 1, "", "fatal: boom"
        return real(cwd, *args, **kw)

    monkeypatch.setattr(purge, "_git", flaky)
    capsys.readouterr()
    assert (
        w.run("purge", "--apply", "--repo", str(w.clone), monkeypatch=monkeypatch) == 1
    )
    out = capsys.readouterr().out
    assert path.is_dir() and len(made) >= 2
    assert _late_refs(w, path.name) == before, "ref этого прохода откатан, прежний цел"
    assert _sh(w.clone, "cat-file", "-t", a) == "commit"
    line = next(x for x in out.splitlines() if path.name in x)
    assert b[:12] in line and c[:12] in line and "откат" in line, out
