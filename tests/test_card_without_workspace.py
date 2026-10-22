"""#2002: карточка задачи в review открывается, когда каталога проекта нет на диске.

Дефект (#1996, GUI-стенд): ``task_diff.commit_exists`` вызывал git в
несуществующем cwd, ``FileNotFoundError`` шёл вверх до веб-слоя, и страница,
на которой ставят вердикт, отвечала 500. Тест держит каталог физически
отсутствующим и реальный ``GitOpsIntegration`` (``MockGitOps`` из conftest
вообще не трогает диск и ничего бы не доказал); без ``submission_sha`` код
выходит раньше ``commit_exists`` — тоже не доказательство, поэтому sha и ветка
заданы.
"""

from __future__ import annotations

import logging
import os

import pytest
from httpx import AsyncClient

from hub import repository as repo
from hub.integrations.git_ops import GitOpsIntegration
from hub.integrations.registry import plugins
from tests.web_support import _web_task_in_review

UNAVAILABLE = "дифф недоступен: рабочий каталог проекта не найден"
SHA = "a" * 40


@pytest.fixture
def missing_workspace(tmp_path) -> str:
    """Путь, которого нет физически."""
    path = str(tmp_path / "gone-2002" / "workspace-marker-2002")
    assert not os.path.exists(path)
    return path


async def _review_task(client: AsyncClient, db, workspace: str, slug: str) -> int:
    task_id = await _web_task_in_review(client)
    project_id = await repo.create_project(
        db,
        slug=slug,
        name=slug,
        workspace_path=workspace,
        default_branch="develop",
        status="active",
    )
    await repo.update_task(
        db,
        task_id,
        project_id=project_id,
        branch=f"task-{task_id}/no-workspace",
        submission_sha=SHA,
    )
    await db.commit()
    return task_id


def _real_git(monkeypatch) -> None:
    """Настоящий git — после создания задач: pair-start на моке не трогает диск."""
    monkeypatch.setattr(plugins, "git_ops", GitOpsIntegration())


async def _status(client: AsyncClient, task_id: int) -> str:
    return (await client.get(f"/api/tasks/{task_id}")).json()["status"]


async def test_review_card_renders_without_a_workspace(
    client: AsyncClient, db, missing_workspace, caplog, monkeypatch
):
    caplog.set_level(logging.INFO, logger="hub")
    approve_id = await _review_task(client, db, missing_workspace, "gone-approve")
    return_id = await _review_task(client, db, missing_workspace, "gone-return")
    _real_git(monkeypatch)

    card = await client.get(f"/tasks/{approve_id}")
    fragment = await client.get(f"/tasks/{approve_id}/diff")

    assert card.status_code == 200, card.text[:400]
    assert fragment.status_code == 200, fragment.text[:400]
    assert UNAVAILABLE in fragment.text
    assert UNAVAILABLE in card.text, "карта изменений на карточке называет причину"
    assert "Дифф не показан" not in fragment.text, "надпись не дублируется префиксом"
    for page in (card.text, fragment.text):
        assert "workspace-marker-2002" not in page
        assert "gone-2002" not in page
    assert "workspace-marker-2002" in caplog.text, "путь остаётся в логе"

    approved = await client.post(
        f"/tasks/{approve_id}/web-review-verdict",
        data={"verdict": "approved", "comments": "с карточки без каталога"},
        follow_redirects=False,
    )
    assert approved.status_code == 303, approved.text[:400]
    assert await _status(client, approve_id) != "review"

    returned = await client.post(
        f"/tasks/{return_id}/web-return-to-work",
        data={"reason": "вернуть с карточки без каталога"},
        follow_redirects=False,
    )
    assert returned.status_code == 303, returned.text[:400]
    assert await _status(client, return_id) != "review"


async def test_other_diff_readers_survive_a_missing_workspace(
    client: AsyncClient, db, missing_workspace, monkeypatch
):
    task_id = await _review_task(client, db, missing_workspace, "gone-readers")
    _real_git(monkeypatch)

    brief = await client.get(f"/api/tasks/{task_id}/review-brief")
    queue = await client.get("/review-queue")
    prod_state = await client.get("/api/prod-state")

    assert brief.status_code == 200, brief.text[:400]
    assert queue.status_code == 200, queue.text[:400]
    assert prod_state.status_code == 200, prod_state.text[:400]
    for body in (brief.text, queue.text):
        assert "workspace-marker-2002" not in body
        assert "gone-2002" not in body
        assert "No such file" not in body


MARKER = "leak-marker-2002"


def _vanish(monkeypatch) -> None:
    """Коммит «найден», каталог исчез позже: падает вызов после commit_exists."""
    from unittest.mock import AsyncMock

    _real_git(monkeypatch)
    monkeypatch.setattr(plugins.git_ops, "commit_exists", AsyncMock(return_value=True))


@pytest.mark.parametrize("reader", ["submission_files", "submission_diff"])
@pytest.mark.parametrize("empty", [False, True])
async def test_a_directory_that_vanishes_midway_gets_the_same_answer(
    client: AsyncClient, db, missing_workspace, monkeypatch, reader, empty
):
    """Каждый вызов git за commit_exists закрыт охранником, а не только первый.

    ``empty`` ведёт в ``commit_in_base_history`` — четвёртый вызов git читателей.
    """
    from unittest.mock import AsyncMock

    from hub.services import task_diff

    task_id = await _review_task(
        client, db, missing_workspace, f"gone-{reader}-{empty}"
    )
    _vanish(monkeypatch)
    if empty:
        monkeypatch.setattr(
            plugins.git_ops, "commit_diff_stat", AsyncMock(return_value=[])
        )
        monkeypatch.setattr(plugins.git_ops, "commit_diff", AsyncMock(return_value=""))

    shown = await getattr(task_diff, reader)(db, task_id)

    assert shown["state"] == task_diff.WORKSPACE_MISSING
    assert shown["reason"] == UNAVAILABLE
    assert "workspace-marker-2002" not in str(shown)


async def test_a_present_directory_that_git_cannot_run_in_is_unreadable(
    client: AsyncClient, db, tmp_path, monkeypatch
):
    """Каталог на месте, а запуск git упал: это не «каталога нет»."""
    from unittest.mock import AsyncMock

    from hub.services import task_diff

    present = tmp_path / "present-2002"
    present.mkdir()
    task_id = await _review_task(client, db, str(present), "present-dir")
    monkeypatch.setattr(plugins, "git_ops", GitOpsIntegration())
    monkeypatch.setattr(
        plugins.git_ops,
        "commit_exists",
        AsyncMock(side_effect=PermissionError(f"denied {MARKER}")),
    )

    shown = await task_diff.submission_diff(db, task_id)

    assert shown["state"] == task_diff.UNREADABLE
    assert "PermissionError" in shown["reason"]
    assert MARKER not in str(shown)


async def test_exception_text_never_reaches_a_reason_shown_to_a_person(
    client: AsyncClient, db, missing_workspace, monkeypatch
):
    """Шесть мест, где ``{exc}`` с путём шёл в видимую причину: тип и код, не текст."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from hub import services
    from hub.services import review_brief, review_evidence

    task_id = await _review_task(client, db, missing_workspace, "gone-reasons")
    row = dict(await repo.get_task(db, task_id))
    boom = OSError(f"no such file {MARKER}")
    _real_git(monkeypatch)

    # review_evidence: контекст проекта (116).
    with monkeypatch.context() as patch:
        patch.setattr(services, "project_git_context", AsyncMock(side_effect=boom))
        base = await review_evidence.resolve_diff_base(db, task_id, "b", offline=True)
    assert base["reason"] and MARKER not in base["reason"]

    # review_evidence: разрешение базы (148) — реальный каталог, которого нет.
    base = await review_evidence.resolve_diff_base(db, task_id, "b", offline=True)
    assert base["reason"] and "workspace-marker-2002" not in base["reason"]

    # review_evidence: свежесть базы (168).
    with monkeypatch.context() as patch:
        patch.setattr(
            plugins.git_ops,
            "resolve_ref",
            AsyncMock(return_value=("resolved", "c" * 40)),
        )
        patch.setattr(plugins.git_ops, "base_freshness", AsyncMock(side_effect=boom))
        base = await review_evidence.resolve_diff_base(db, task_id, "b", offline=False)
    assert base["reason"] and MARKER not in base["reason"]

    # review_evidence: объём диффа в отчёте (1481).
    report = await review_evidence.review_report(db, row, offline=True)
    assert report.diff_note and "workspace-marker-2002" not in report.diff_note

    # review_brief: секция вызовов (197).
    view = SimpleNamespace(id=task_id, branch=row["branch"], pr_number=None)
    section = await review_brief.build_call_sites_section(
        db, task_id, view, {"base": "develop"}, observe_only=True
    )
    assert section.reason and "workspace-marker-2002" not in section.reason

    # review_brief: слияние с базой (341).
    monkeypatch.setattr(
        plugins.git_ops, "check_pr_mergeable", AsyncMock(side_effect=boom)
    )
    view = SimpleNamespace(id=task_id, pr_number=7)
    merge = await review_brief._ask_base_merge(db, view)
    assert merge.state == "unknown" and MARKER not in merge.reason
