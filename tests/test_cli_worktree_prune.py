"""hp-hub worktree prune (#2038): уборка копий доставленных задач.

Тесты идут на настоящих git-репозиториях в tmp_path: bare origin, клон и
worktree-копии на каноническом пути #459. Хаб подменён заглушкой _api.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import aiosqlite
import pytest
from httpx import AsyncClient

from hub import cli_worktree_prune as prune
from tests.test_accept_without_delivery import _completed_task
from tests.worktree_prune_support import SLUGS, World, _branches, _lines, _sh


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
        assert any("в карантин" in x for x in _lines(out, n)), (n, out)
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


def test_prune_apply_only_moves_never_removes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--apply = ровно ``git worktree move <путь> <.trash/...>``; ни remove, ни веток."""
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
    moves = [a for a in seen if a[:2] == ("worktree", "move")]
    assert len(moves) == 1 and moves[0][2] == str(w.wt(101)), seen
    assert Path(moves[0][3]).parent == w.trash(), seen
    assert not [a for a in seen if a[:2] == ("worktree", "remove")]
    assert not [a for a in seen if a[0] == "branch"]
    # update-ref - только защитные refs карантина и только создание
    refs = [a for a in seen if a[0] == "update-ref"]
    assert refs and all(a[1].startswith("refs/haiplane-trash/") for a in refs), refs


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
