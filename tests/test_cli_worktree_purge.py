"""hp-hub worktree prune/purge/restore (#2038, раунд 5): маркер, refs, возврат."""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any

import pytest

from hub import cli, cli_worktree_prune as prune
from hub import cli_worktree_purge as purge
from hub import cli_worktree_trash as trash
from tests.worktree_prune_support import World, _lines, _sh

DAY = 86400


def _stamp(epoch: float) -> str:
    return time.strftime("%Y%m%dT%H%M%S", time.gmtime(epoch))


def _admin(w: World, path: Path) -> Path:
    return Path(_sh(path, "rev-parse", "--absolute-git-dir"))


def _quarantined(w: World, tid: int, age_days: float = 8.0, **marker: Any) -> Path:
    """Копия в .trash так, как её оставляет prune: имя, маркер и защитные refs."""
    epoch = int(time.time() - age_days * DAY)
    name = f"task-{tid}-{_stamp(epoch)}"
    w.trash().mkdir(parents=True, exist_ok=True)
    path = w.trash() / name
    branch = f"task-{tid}/old"
    _sh(w.clone, "worktree", "add", "-q", "-b", branch, str(path), "develop")
    _sh(path, "switch", "-q", "--detach")
    data = trash.make_marker(
        tid, str(w.wt(tid)), branch, _sh(path, "rev-parse", "HEAD"), name
    )
    data["moved_epoch"] = epoch
    data["moved_at"] = _stamp(epoch)
    data.update(marker)
    if data.get("_drop"):
        return path
    assert trash.write_marker(str(_admin(w, path)), data) == ""
    refs, err = trash.protect(prune._git, str(w.clone), str(path), name)
    assert err == "" and refs
    return path


def _refs(w: World, name: str = "") -> list[str]:
    out = _sh(
        w.clone, "for-each-ref", "--format=%(refname)", f"refs/haiplane-trash/{name}"
    )
    return out.split()


def _registered(w: World) -> list[str]:
    out = _sh(w.clone, "worktree", "list", "--porcelain")
    return [
        str(Path(x[9:]).resolve())
        for x in out.splitlines()
        if x.startswith("worktree ")
    ]


# --- P1: маркер и защитные refs пишет prune -------------------------------------
def test_prune_writes_the_marker_and_protective_refs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = World(tmp_path)
    w.add(101)
    w.commit(101, "a.txt")
    w.commit(101, "b.txt")
    w.tasks[101]["submission_sha"] = _sh(w.wt(101), "rev-parse", "HEAD")
    _sh(w.wt(101), "update-ref", "ORIG_HEAD", _sh(w.wt(101), "rev-parse", "HEAD~2"))
    entries = _sh(w.wt(101), "reflog", "--format=%H", "HEAD").split()
    head = entries[0]
    assert (
        w.run("prune", "--apply", "--repo", str(w.clone), monkeypatch=monkeypatch) == 0
    )
    dest = next(p for p in w.trash().iterdir() if p.name.startswith("task-101-"))
    marker = json.loads((_admin(w, dest) / trash.MARKER).read_text())
    assert marker["task_id"] == 101 and marker["name"] == dest.name
    assert marker["head"] == head and marker["old_path"] == str(w.wt(101))
    assert marker["branch"] == w.tasks[101]["branch"]
    assert abs(marker["moved_epoch"] - time.time()) < 120
    refs = _refs(w, dest.name)
    pinned = {_sh(w.clone, "rev-parse", r) for r in refs}
    assert (
        set(entries)
        | {_sh(w.wt(101) if w.wt(101).exists() else dest, "rev-parse", "ORIG_HEAD")}
        <= pinned
    )
    assert all(r.startswith(f"refs/haiplane-trash/{dest.name}/") for r in refs)


def test_prune_leaves_the_copy_when_a_protective_ref_cannot_be_made(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    w = World(tmp_path)
    w.add(101)
    w.commit(101, "a.txt")
    w.tasks[101]["submission_sha"] = _sh(w.wt(101), "rev-parse", "HEAD")
    real = prune._git
    made: list[int] = []

    def flaky(cwd: Any, *args: str, **kw: Any) -> Any:
        if args[:1] == ("update-ref",) and "-d" not in args:
            made.append(1)
            if len(made) == 2:
                return 1, "", "fatal: boom"
        return real(cwd, *args, **kw)

    monkeypatch.setattr(prune, "_git", flaky)
    assert (
        w.run("prune", "--apply", "--repo", str(w.clone), monkeypatch=monkeypatch) != 0
    )
    out = capsys.readouterr().out
    assert w.wt(101).is_dir() and len(made) == 2
    assert _refs(w) == [], "созданный ref откачен"
    assert not w.trash().exists() or not any(w.trash().iterdir())
    assert any("защитные refs не созданы" in x for x in _lines(out, 101)), out


def test_prune_does_not_look_inside_the_trash_and_keeps_quarantined_copies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    w = World(tmp_path)
    w.add(101)
    assert (
        w.run("prune", "--apply", "--repo", str(w.clone), monkeypatch=monkeypatch) == 0
    )
    capsys.readouterr()
    assert (
        w.run("prune", "--apply", "--repo", str(w.clone), monkeypatch=monkeypatch) == 0
    )
    out = capsys.readouterr().out
    quarantined = [p for p in w.trash().iterdir()]
    assert len(quarantined) == 1
    assert any(
        str(quarantined[0]) in x and "канонического" in x for x in out.splitlines()
    )


# --- P1: purge верит маркеру, а не имени ----------------------------------------
def test_purge_removes_only_proven_old_copies_and_drops_their_refs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    w = World(tmp_path)
    old = _quarantined(w, 101, age_days=8)
    young = _quarantined(w, 102, age_days=0.5)
    junk = w.trash() / "junk"
    _sh(w.clone, "worktree", "add", "-q", "-b", "task-9/junk", str(junk), "develop")
    outside = w.wt(103).parent / f"task-103-{_stamp(time.time() - 9 * DAY)}"
    outside.parent.mkdir(exist_ok=True)
    _sh(
        w.clone,
        "worktree",
        "add",
        "-q",
        "-b",
        "task-103/outside",
        str(outside),
        "develop",
    )
    old_refs = _refs(w, old.name)
    assert old_refs and _refs(w, young.name)

    assert w.run("purge", "--repo", str(w.clone), monkeypatch=monkeypatch) == 0
    dry = capsys.readouterr().out
    assert all(p.is_dir() for p in (old, young, junk, outside)), (
        "dry-run ничего не трогает"
    )
    assert any(
        "удалить" in x and old.name in x and "сут" in x for x in dry.splitlines()
    ), dry

    assert (
        w.run("purge", "--apply", "--repo", str(w.clone), monkeypatch=monkeypatch) == 0
    )
    out = capsys.readouterr().out
    assert not old.exists() and young.is_dir() and junk.is_dir() and outside.is_dir()
    assert str(old.resolve()) not in _registered(w)
    assert _refs(w, old.name) == [] and _refs(w, young.name) != []
    assert any("вне" in x and str(outside) in x for x in out.splitlines()), out
    assert any("шаблону" in x and "junk" in x for x in out.splitlines()), out

    # порог 1 сутки: молодая (0.5) остаётся, 2 суток уходят
    two = _quarantined(w, 104, age_days=2)
    assert w.run("purge", "--apply", "--older-than-days", "1", "--repo", str(w.clone), monkeypatch=monkeypatch) == 0  # fmt: skip
    assert not two.exists() and young.is_dir()


def _hand_made_old_name(w: World) -> Path:
    name = f"task-105-{_stamp(time.time() - 30 * DAY)}"
    w.trash().mkdir(parents=True, exist_ok=True)
    path = w.trash() / name
    _sh(w.clone, "worktree", "add", "-q", "-b", "task-105/x", str(path), "develop")
    (path / "unsaved.txt").write_text("fresh work")
    return path


def test_purge_does_not_trust_a_name_without_a_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    w = World(tmp_path)
    path = _hand_made_old_name(w)
    assert (
        w.run("purge", "--apply", "--repo", str(w.clone), monkeypatch=monkeypatch) != 0
    )
    out = capsys.readouterr().out
    assert (path / "unsaved.txt").read_text() == "fresh work"
    assert any("нет маркера" in x and path.name in x for x in out.splitlines()), out


@pytest.mark.parametrize(
    "case, why",
    [
        ("task", "id задачи или имя"),
        ("name", "id задачи или имя"),
        ("time", "время в имени"),
        ("future", "из будущего"),
        ("junk", "маркер не разобран"),
        ("incomplete", "маркер неполный"),
        ("head", "HEAD копии"),
    ],
)
def test_purge_keeps_a_copy_whose_marker_does_not_prove_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: str,
    why: str,
) -> None:
    w = World(tmp_path)
    over: dict[str, Any] = {
        "task": {"task_id": 999},
        "name": {"name": "task-101-other"},
        "time": {"moved_epoch": int(time.time())},
    }.get(case, {})
    if case == "future":
        # и имя, и маркер говорят «через час»: возраст из будущего
        epoch = int(time.time() + 3600)
        over = {"moved_epoch": epoch}
    path = _quarantined(w, 101, age_days=(-1 / 24) if case == "future" else 8, **over)
    gd = _admin(w, path) / trash.MARKER
    if case == "junk":
        gd.write_text("{not json")
    if case == "incomplete":
        gd.write_text(json.dumps({"moved_epoch": 1}))
    if case == "head":
        (path / "late.txt").write_text("x")
        _sh(path, "add", "late.txt")
        _sh(path, "commit", "-q", "-m", "late")
    assert (
        w.run("purge", "--apply", "--repo", str(w.clone), monkeypatch=monkeypatch) != 0
    )
    out = capsys.readouterr().out
    assert path.is_dir() and _refs(w, path.name)
    assert any(why in x and path.name in x for x in out.splitlines()), out


def test_purge_failed_remove_keeps_the_protective_refs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    w = World(tmp_path)
    old = _quarantined(w, 101)
    real = purge._git

    def failing(cwd: Any, *args: str, **kw: Any) -> Any:
        if args[:2] == ("worktree", "remove"):
            return 1, "", "error: boom"
        return real(cwd, *args, **kw)

    monkeypatch.setattr(purge, "_git", failing)
    assert (
        w.run("purge", "--apply", "--repo", str(w.clone), monkeypatch=monkeypatch) != 0
    )
    assert old.is_dir() and _refs(w, old.name) != []
    assert "ошибка" in capsys.readouterr().out


def test_purge_removes_with_force_only_for_proven_quarantine_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = World(tmp_path)
    _quarantined(w, 101)
    seen: list[tuple[str, ...]] = []
    real = purge._git

    def spy(cwd: Any, *args: str, **kw: Any) -> Any:
        if args[:2] == ("worktree", "remove"):
            seen.append(args)
        return real(cwd, *args, **kw)

    monkeypatch.setattr(purge, "_git", spy)
    assert (
        w.run("purge", "--apply", "--repo", str(w.clone), monkeypatch=monkeypatch) == 0
    )
    assert len(seen) == 1 and seen[0][:3] == ("worktree", "remove", "--force")
    assert Path(seen[0][3]).parent.name == ".trash"


def test_purge_never_touches_canonical_copies_or_the_main_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = World(tmp_path)
    w.add(101)
    assert w.run("purge", "--apply", "--older-than-days", "1", "--repo", str(w.clone), monkeypatch=monkeypatch) == 0  # fmt: skip
    assert w.wt(101).is_dir() and w.clone.is_dir()


def test_purge_refuses_a_trash_path_that_is_a_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    w = World(tmp_path)
    link = _quarantined(w, 101)
    real_dir = tmp_path / "elsewhere-101"
    admin = _admin(w, link)
    shutil.move(str(link), str(real_dir))
    link.symlink_to(real_dir)
    (admin / "gitdir").write_text(f"{link}/.git\n")
    assert (
        w.run("purge", "--apply", "--repo", str(w.clone), monkeypatch=monkeypatch) == 0
    )
    assert real_dir.is_dir()
    assert any("симлинк" in x for x in capsys.readouterr().out.splitlines())


# --- P2: --older-than-days -------------------------------------------------------
@pytest.mark.parametrize("bad", ["nan", "inf", "-inf", "0", "0.5", "-3", "abc", ""])
def test_older_than_days_rejects_non_finite_and_small_values(bad: str) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.build_parser().parse_args(["worktree", "purge", "--older-than-days", bad])
    assert exc.value.code == 2


def test_older_than_days_accepts_one_and_the_default_is_seven() -> None:
    parser = cli.build_parser()
    assert parser.parse_args(["worktree", "purge"]).older_than_days == 7.0
    assert (
        parser.parse_args(
            ["worktree", "purge", "--older-than-days", "1"]
        ).older_than_days
        == 1.0
    )


# --- P2: restore -----------------------------------------------------------------
def test_restore_returns_the_copy_with_its_branch_and_clears_the_traces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    w = World(tmp_path)
    w.add(101)
    w.commit(101, "work.txt")
    w.tasks[101]["submission_sha"] = _sh(w.wt(101), "rev-parse", "HEAD")
    branch = w.tasks[101]["branch"]
    assert (
        w.run("prune", "--apply", "--repo", str(w.clone), monkeypatch=monkeypatch) == 0
    )
    dest = next(p for p in w.trash().iterdir())
    capsys.readouterr()
    assert (
        w.run("restore", dest.name, "--repo", str(w.clone), monkeypatch=monkeypatch)
        == 0
    )
    assert "восстановлена" in capsys.readouterr().out
    assert (w.wt(101) / "work.txt").read_text() == "work.txt"
    assert _sh(w.wt(101), "branch", "--show-current") == branch
    assert not dest.exists()
    assert _refs(w, dest.name) == []
    assert not (_admin(w, w.wt(101)) / trash.MARKER).exists()
    assert str(w.wt(101).resolve()) in _registered(w)


def test_restore_stops_when_the_canonical_path_is_taken(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    w = World(tmp_path)
    w.add(101)
    assert (
        w.run("prune", "--apply", "--repo", str(w.clone), monkeypatch=monkeypatch) == 0
    )
    dest = next(p for p in w.trash().iterdir())
    w.wt(101).mkdir()
    (w.wt(101) / "new.txt").write_text("new copy")
    capsys.readouterr()
    assert (
        w.run("restore", dest.name, "--repo", str(w.clone), monkeypatch=monkeypatch)
        != 0
    )
    assert dest.is_dir() and (w.wt(101) / "new.txt").exists()
    assert _refs(w, dest.name) != [] and (_admin(w, dest) / trash.MARKER).exists()
    assert not (w.wt(101) / dest.name).exists(), (
        "move не вложил карантин в занятый путь"
    )


@pytest.mark.parametrize("name", ["", "nope", "task-101-20200101T000000"])
def test_restore_refuses_unknown_names_and_unproven_copies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    w = World(tmp_path)
    w.add(101)
    hand = _hand_made_old_name(w)
    args = ["restore", name or hand.name] if name != "" else ["restore", hand.name]
    assert w.run(*args, "--repo", str(w.clone), monkeypatch=monkeypatch) != 0
    assert hand.is_dir() and not (w.wt(105)).exists()


def test_prune_pins_a_pseudo_ref_sha_that_is_not_in_the_reflog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ORIG_HEAD на коммит из refs/remotes: проверки пускают, но gc ему не друг."""
    w = World(tmp_path)
    w.add(101)
    tree = _sh(w.wt(101), "rev-parse", "HEAD^{tree}")
    odd = _sh(w.wt(101), "commit-tree", tree, "-m", "remote-only")
    _sh(w.clone, "update-ref", "refs/remotes/origin/elsewhere", odd)
    _sh(w.wt(101), "update-ref", "ORIG_HEAD", odd)
    assert odd not in _sh(w.wt(101), "reflog", "--format=%H", "HEAD").split()
    assert (
        w.run("prune", "--apply", "--repo", str(w.clone), monkeypatch=monkeypatch) == 0
    )
    dest = next(p for p in w.trash().iterdir())
    assert odd in {_sh(w.clone, "rev-parse", r) for r in _refs(w, dest.name)}


def test_prune_reports_a_marker_that_could_not_be_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    w = World(tmp_path)
    w.add(101)
    monkeypatch.setattr(
        trash, "write_marker", lambda gitdir, data: "маркер не записан (test)"
    )
    assert (
        w.run("prune", "--apply", "--repo", str(w.clone), monkeypatch=monkeypatch) != 0
    )
    out = capsys.readouterr().out
    assert any("purge её не тронет" in x for x in _lines(out, 101)), out


@pytest.mark.parametrize("mode", ["refused", "moved_anyway"])
def test_prune_failed_move_rolls_the_protective_refs_back_only_if_the_copy_stayed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    w = World(tmp_path)
    w.add(101)
    real = prune._git

    def failing(cwd: Any, *args: str, **kw: Any) -> Any:
        if args[:2] == ("worktree", "move"):
            if mode == "moved_anyway":
                real(cwd, *args, **kw)
            return 1, "", "error: boom"
        return real(cwd, *args, **kw)

    monkeypatch.setattr(prune, "_git", failing)
    assert (
        w.run("prune", "--apply", "--repo", str(w.clone), monkeypatch=monkeypatch) != 0
    )
    assert (_refs(w) == []) == (mode == "refused")


def test_restore_refuses_a_marker_whose_old_path_is_not_canonical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = World(tmp_path)
    path = _quarantined(w, 101, old_path=str(tmp_path / "somewhere" / "else"))
    assert (
        w.run("restore", path.name, "--repo", str(w.clone), monkeypatch=monkeypatch)
        != 0
    )
    assert path.is_dir() and not w.wt(101).exists()


def test_restore_refuses_a_marked_copy_that_is_not_inside_the_trash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = World(tmp_path)
    inside = _quarantined(w, 101)
    outside = w.wt(103).parent / inside.name
    _sh(w.clone, "worktree", "move", str(inside), str(outside))
    assert (
        w.run("restore", outside.name, "--repo", str(w.clone), monkeypatch=monkeypatch)
        != 0
    )
    assert outside.is_dir() and not w.wt(101).exists()


# --- раунд 6 ------------------------------------------------------------------
def _hand_written_reflog(w: World, lost: str) -> str:
    """logs/HEAD: первая сохранившаяся строка «lost -> HEAD», потом нулевая."""
    head = _sh(w.wt(101), "rev-parse", "HEAD")
    zero = "0" * 40
    ident = "T <t@example.com> 1700000000 +0000"
    log = _admin(w, w.wt(101)) / "logs" / "HEAD"
    log.write_text(f"{lost} {head} {ident}\treset: a\n{zero} {head} {ident}\tx: b\n")
    return head


def _unreachable_commit_of(w: World) -> str:
    tree = _sh(w.wt(101), "rev-parse", "HEAD^{tree}")
    return _sh(w.wt(101), "commit-tree", tree, "-m", "only-in-old-value")


def test_prune_keeps_a_copy_whose_first_reflog_line_old_value_is_only_here(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A -> B, A нигде больше нет: raw logs/HEAD проверяет и старое значение."""
    w = World(tmp_path)
    w.add(101)
    lost = _unreachable_commit_of(w)
    _hand_written_reflog(w, lost)
    assert (
        w.run("prune", "--apply", "--repo", str(w.clone), monkeypatch=monkeypatch) == 0
    )
    out = capsys.readouterr().out
    assert w.wt(101).is_dir() and not w.trash().exists()
    assert any(
        lost[:12] in x and "только в этой копии" in x for x in _lines(out, 101)
    ), out


def test_protective_refs_pin_the_old_value_of_the_first_reflog_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A достижим из refs/remotes: копия уходит, и A закреплён защитным ref."""
    w = World(tmp_path)
    w.add(101)
    lost = _unreachable_commit_of(w)
    _sh(w.clone, "update-ref", "refs/remotes/origin/keepalive", lost)
    head = _hand_written_reflog(w, lost)
    assert (
        w.run("prune", "--apply", "--repo", str(w.clone), monkeypatch=monkeypatch) == 0
    )
    dest = next(p for p in w.trash().iterdir())
    pinned = [_sh(w.clone, "rev-parse", r) for r in _refs(w, dest.name)]
    assert lost in pinned and head in pinned
    assert all(p != "0" * 40 for p in pinned)


def test_prune_keeps_the_copy_when_the_raw_reflog_cannot_be_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    w = World(tmp_path)
    w.add(101)

    def deny(path: Any, *a: Any, **kw: Any) -> Any:
        if str(path).endswith("logs/HEAD"):
            raise PermissionError("denied")
        return open(path, *a, **kw)

    monkeypatch.setattr(trash, "open", deny, raising=False)
    assert (
        w.run("prune", "--apply", "--repo", str(w.clone), monkeypatch=monkeypatch) != 0
    )
    out = capsys.readouterr().out
    assert w.wt(101).is_dir() and _refs(w) == []
    assert any("logs/HEAD" in x and "не прочитан" in x for x in _lines(out, 101)), out


def _branch_one_commit_ahead(w: World, path: Path, branch: str, filename: str) -> None:
    """Ветка уходит вперёд на коммит с файлом filename; копия остаётся на старом HEAD."""
    (path / filename).write_text("tracked on the branch")
    _sh(path, "add", filename)
    _sh(path, "commit", "-q", "-m", "ahead")
    ahead = _sh(path, "rev-parse", "HEAD")
    _sh(path, "reset", "-q", "--hard", "HEAD~1")
    _sh(w.clone, "update-ref", f"refs/heads/{branch}", ahead)


def test_restore_does_not_overwrite_an_ignored_file_and_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    w = World(tmp_path)
    path = _quarantined(w, 101)
    _branch_one_commit_ahead(w, path, "task-101/old", "data.txt")
    info = Path(
        _sh(
            w.clone, "rev-parse", "--path-format=absolute", "--git-path", "info/exclude"
        )
    )
    info.parent.mkdir(parents=True, exist_ok=True)
    info.write_text("data.txt\n")
    (path / "data.txt").write_text("my ignored notes")
    assert (
        w.run("restore", path.name, "--repo", str(w.clone), monkeypatch=monkeypatch)
        == 1
    )
    assert (path / "data.txt").read_text() == "my ignored notes"
    assert path.is_dir() and not w.wt(101).exists()
    assert (_admin(w, path) / trash.MARKER).exists() and _refs(w, path.name) != []
    assert str(path.resolve()) in _registered(w)
    assert "switch" in capsys.readouterr().err


def test_restore_refuses_when_the_branch_is_checked_out_elsewhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    w = World(tmp_path)
    path = _quarantined(w, 101)
    other = tmp_path / "other-checkout"
    _sh(w.clone, "worktree", "add", "-q", str(other), "task-101/old")
    moves: list[tuple[str, ...]] = []
    real = purge._git

    def spy(cwd: Any, *args: str, **kw: Any) -> Any:
        if args[:2] == ("worktree", "move"):
            moves.append(args)
        return real(cwd, *args, **kw)

    monkeypatch.setattr(purge, "_git", spy)
    assert (
        w.run("restore", path.name, "--repo", str(w.clone), monkeypatch=monkeypatch)
        == 1
    )
    assert moves == [] and path.is_dir() and not w.wt(101).exists()
    assert "task-101/old" in capsys.readouterr().err


def test_restore_moves_the_copy_back_when_the_switch_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    w = World(tmp_path)
    path = _quarantined(w, 101)
    real = purge._git

    def failing(cwd: Any, *args: str, **kw: Any) -> Any:
        if "switch" in args:
            return 1, "", "fatal: boom"
        return real(cwd, *args, **kw)

    monkeypatch.setattr(purge, "_git", failing)
    assert (
        w.run("restore", path.name, "--repo", str(w.clone), monkeypatch=monkeypatch)
        == 1
    )
    assert path.is_dir() and not w.wt(101).exists()
    assert (_admin(w, path) / trash.MARKER).exists() and _refs(w, path.name) != []
    assert str(path.resolve()) in _registered(w)
    assert "возвращена в карантин" in capsys.readouterr().err


def test_purge_names_and_drops_orphan_protective_refs_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    w = World(tmp_path)
    gone = _quarantined(w, 101)
    kept = _quarantined(w, 102, age_days=0.5)
    shutil.rmtree(gone)
    _sh(w.clone, "worktree", "prune")
    orphan_refs, live_refs = _refs(w, gone.name), _refs(w, kept.name)
    assert orphan_refs and live_refs

    assert w.run("purge", "--repo", str(w.clone), monkeypatch=monkeypatch) == 0
    out = capsys.readouterr().out
    assert _refs(w, gone.name) == orphan_refs, "dry-run ничего не трогает"
    assert any("осиротевшие refs" in x and gone.name in x for x in out.splitlines()), (
        out
    )
    assert kept.name not in "\n".join(x for x in out.splitlines() if "осиротевшие" in x)

    assert (
        w.run("purge", "--apply", "--repo", str(w.clone), monkeypatch=monkeypatch) == 0
    )
    assert _refs(w, gone.name) == [] and _refs(w, kept.name) == live_refs
    assert kept.is_dir()


def test_purge_orphan_ref_delete_failure_is_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    w = World(tmp_path)
    gone = _quarantined(w, 101)
    shutil.rmtree(gone)
    _sh(w.clone, "worktree", "prune")
    real = purge._git

    def failing(cwd: Any, *args: str, **kw: Any) -> Any:
        if args[:2] == ("update-ref", "-d"):
            return 1, "", "fatal: boom"
        return real(cwd, *args, **kw)

    monkeypatch.setattr(purge, "_git", failing)
    assert (
        w.run("purge", "--apply", "--repo", str(w.clone), monkeypatch=monkeypatch) != 0
    )
    assert _refs(w, gone.name) != []


def test_purge_reports_an_unreadable_list_of_protective_refs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    w = World(tmp_path)
    real = purge._git

    def failing(cwd: Any, *args: str, **kw: Any) -> Any:
        if args[:1] == ("for-each-ref",):
            return 1, "", "fatal: boom"
        return real(cwd, *args, **kw)

    monkeypatch.setattr(purge, "_git", failing)
    assert w.run("purge", "--repo", str(w.clone), monkeypatch=monkeypatch) != 0
    assert "защитные refs не прочитаны" in capsys.readouterr().out


def _commit_then_reset_away(w: World, path: Path) -> str:
    """Коммит в карантине, затем reset --hard к HEAD маркера: HEAD прежний, reflog помнит."""
    (path / "late.txt").write_text("late work")
    _sh(path, "add", "late.txt")
    _sh(path, "commit", "-q", "-m", "late")
    late = _sh(path, "rev-parse", "HEAD")
    _sh(path, "reset", "-q", "--hard", "HEAD~1")
    return late


def test_purge_keeps_a_copy_with_a_commit_made_and_reset_away_in_quarantine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    w = World(tmp_path)
    path = _quarantined(w, 101, age_days=8)
    late = _commit_then_reset_away(w, path)
    assert (
        w.run("purge", "--apply", "--repo", str(w.clone), monkeypatch=monkeypatch) == 1
    )
    out = capsys.readouterr().out
    assert path.is_dir() and _sh(path, "cat-file", "-t", late) == "commit"
    assert _refs(w, path.name) != []
    assert any(late[:12] in x and path.name in x for x in out.splitlines()), out


def test_purge_accepts_a_quarantine_commit_that_is_on_a_remote_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = World(tmp_path)
    path = _quarantined(w, 101, age_days=8)
    late = _commit_then_reset_away(w, path)
    _sh(w.clone, "update-ref", "refs/remotes/origin/late", late)
    assert (
        w.run("purge", "--apply", "--repo", str(w.clone), monkeypatch=monkeypatch) == 0
    )
    assert not path.exists()


def test_purge_keeps_a_copy_whose_raw_reflog_cannot_be_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    w = World(tmp_path)
    path = _quarantined(w, 101, age_days=8)

    def deny(p: Any, *a: Any, **kw: Any) -> Any:
        if str(p).endswith("logs/HEAD"):
            raise PermissionError("denied")
        return open(p, *a, **kw)

    monkeypatch.setattr(trash, "open", deny, raising=False)
    assert (
        w.run("purge", "--apply", "--repo", str(w.clone), monkeypatch=monkeypatch) == 1
    )
    out = capsys.readouterr().out
    assert path.is_dir() and _refs(w, path.name) != []
    assert any("logs/HEAD" in x and "не прочитан" in x for x in out.splitlines()), out
