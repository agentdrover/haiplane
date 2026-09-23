"""Дифф сдачи на карточке: что человек видит и чего он не увидит (#1239).

Модуль ``hub.services.task_diff`` до сих пор не был покрыт ни одним тестом —
это выяснилось при работе над #1239 и записано в сдаче как находка. Здесь
проверяется не рендер, а единственное свойство, ради которого карточка
существует: она не смеет показывать человеку пустой экран под видом
«изменений нет», когда пустота означает совсем другое.

Настоящий git, а не ``MockGitOps`` из conftest: предмет проверки — что
отвечает ``git diff base...sha``, когда коммит уже лежит в истории базы.
"""

from __future__ import annotations

import json
import subprocess

import aiosqlite

from hub import repository as repo
from hub.services.task_diff import (
    MERGED_INTO_BASE,
    READ,
    UNREADABLE,
    submission_diff,
    submission_files,
)


def _run(*args, cwd) -> None:
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


def _clone(tmp_path, *, delivered: bool):
    """Клон и sha сдачи; ``delivered`` — влит ли уже коммит в базу."""
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "develop", str(origin)],
        check=True,
        capture_output=True,
    )
    seed = tmp_path / "seed"
    seed.mkdir()
    _run("git", "clone", str(origin), str(seed), cwd=tmp_path)
    _run("git", "config", "user.email", "t@e", cwd=seed)
    _run("git", "config", "user.name", "t", cwd=seed)
    (seed / "base.txt").write_text("base\n")
    _run("git", "add", "-A", cwd=seed)
    _run("git", "commit", "-qm", "baseline", cwd=seed)
    _run("git", "branch", "-M", "develop", cwd=seed)
    _run("git", "push", "-q", "origin", "develop", cwd=seed)

    branch = "task-9002/card"
    _run("git", "checkout", "-qb", branch, cwd=seed)
    (seed / "mine.txt").write_text("mine\n")
    _run("git", "add", "-A", cwd=seed)
    _run("git", "commit", "-qm", "the submission", cwd=seed)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=seed, capture_output=True, text=True
    ).stdout.strip()
    _run("git", "push", "-q", "origin", branch, cwd=seed)
    if delivered:
        _run("git", "checkout", "-q", "develop", cwd=seed)
        _run("git", "merge", "-q", "--no-ff", "-m", "deliver", branch, cwd=seed)
        _run("git", "push", "-q", "origin", "develop", cwd=seed)

    clone = tmp_path / "hub-clone"
    _run("git", "clone", str(origin), str(clone), cwd=tmp_path)
    return clone, branch, sha


async def _task(db: aiosqlite.Connection, clone, branch: str, sha: str) -> int:
    project_id = await repo.create_project(
        db,
        slug="card-probe",
        name="card probe",
        workspace_path=str(clone),
        default_branch="develop",
        status="active",
    )
    task_id = await repo.create_task(
        db,
        title="сдача на карточке",
        description="",
        runtime="auto",
        source="agent",
        assigned_agent="a",
        rationale="",
        status="review",
        auto_review=False,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await repo.update_task(
        db,
        task_id,
        project_id=project_id,
        branch=branch,
        submission_sha=sha,
        submission_generation=1,
        affected_areas=json.dumps(["mine.txt"]),
    )
    await db.commit()
    return task_id


def _real_git(monkeypatch) -> None:
    from hub.integrations.git_ops import GitOpsIntegration
    from hub.integrations.registry import plugins

    monkeypatch.setattr(plugins, "git_ops", GitOpsIntegration())


async def test_a_delivered_submission_is_not_shown_as_no_changes(
    db: aiosqlite.Connection, tmp_path, monkeypatch
) -> None:
    """AC-1: пустой дифф доставленной сдачи получает названную причину.

    Это единственная ложь, которую гейт показывает человеку прямо в лицо:
    карточку читают, код — нет. До правки состояние было ``read`` с нулём
    файлов, то есть «изменений нет» по сдаче, которая меняла файл.
    """
    _real_git(monkeypatch)
    clone, branch, sha = _clone(tmp_path, delivered=True)
    task_id = await _task(db, clone, branch, sha)

    shown = await submission_diff(db, task_id)

    assert shown["state"] == MERGED_INTO_BASE
    assert sha[:12] in shown["reason"]
    assert "уже лежит в истории базы" in shown["reason"]
    assert shown["files"] == []


async def test_the_change_map_of_a_delivered_submission_names_the_same_cause(
    db: aiosqlite.Connection, tmp_path, monkeypatch
) -> None:
    """AC-1, второй потребитель: карта изменений — своя проверка.

    Карта считается ОТДЕЛЬНЫМ вызовом (``commit_diff_stat``), и «починили
    дифф — значит и карту» ровно та посылка, из-за которой заведена #1239.
    """
    _real_git(monkeypatch)
    clone, branch, sha = _clone(tmp_path, delivered=True)
    task_id = await _task(db, clone, branch, sha)

    shown = await submission_files(db, task_id)

    assert shown["state"] == MERGED_INTO_BASE
    assert shown["files"] == []


async def test_a_live_submission_is_shown_as_before(
    db: aiosqlite.Connection, tmp_path, monkeypatch
) -> None:
    """Сторож не съедает нормальный случай: неслитая сдача читается как прежде."""
    _real_git(monkeypatch)
    clone, branch, sha = _clone(tmp_path, delivered=False)
    task_id = await _task(db, clone, branch, sha)

    shown = await submission_diff(db, task_id)
    listing = await submission_files(db, task_id)

    assert shown["state"] == READ
    assert [f["path"] for f in shown["files"]] == ["mine.txt"]
    assert listing["state"] == READ
    assert [f["path"] for f in listing["files"]] == ["mine.txt"]


async def test_an_unanswerable_question_is_not_an_empty_screen(
    db: aiosqlite.Connection, tmp_path, monkeypatch
) -> None:
    """AC-3 на карточке: «не смогли спросить» не печатается как «пусто».

    Здесь дифф пуст (коммит доставлен), а на вопрос о предке git не
    отвечает. Показать пустоту в этом состоянии значит выдать за измерение
    то, что измерением не является.
    """
    from unittest.mock import AsyncMock

    from hub.integrations.registry import plugins

    _real_git(monkeypatch)
    clone, branch, sha = _clone(tmp_path, delivered=True)
    task_id = await _task(db, clone, branch, sha)
    monkeypatch.setattr(
        plugins.git_ops,
        "commit_in_base_history",
        AsyncMock(return_value=None),
    )

    shown = await submission_diff(db, task_id)

    assert shown["state"] == UNREADABLE
    assert "git не ответил" in shown["reason"]
