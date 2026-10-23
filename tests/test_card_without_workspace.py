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


@pytest.fixture(params=[True, False], ids=["test-ac", "no-ac"])
def with_ac(request) -> bool:
    """Задача с критерием verifiable_by=test (активен блок локаторов) и без него."""
    return bool(request.param)


async def _review_task(
    client: AsyncClient, db, workspace: str, slug: str, with_ac: bool = True
) -> int:
    task_id = await _web_task_in_review(client)
    if with_ac:
        refined = await client.post(
            f"/api/tasks/{task_id}/refine",
            json={
                "acceptance_criteria": [
                    {
                        "id": "AC-1",
                        "given": "workspace отсутствует",
                        "when": "открыта карточка",
                        "then": "карточка открывается",
                        "verifiable_by": "test",
                        "test_ref": "tests/test_card_without_workspace.py::test_x",
                    }
                ]
            },
        )
        assert refined.status_code == 200, refined.text
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
    client: AsyncClient, db, missing_workspace, caplog, monkeypatch, with_ac
):
    caplog.set_level(logging.INFO, logger="hub")
    approve_id = await _review_task(
        client, db, missing_workspace, "gone-approve", with_ac
    )
    return_id = await _review_task(
        client, db, missing_workspace, "gone-return", with_ac
    )
    _real_git(monkeypatch)

    card = await client.get(f"/tasks/{approve_id}")
    fragment = await client.get(f"/tasks/{approve_id}/diff")

    assert card.status_code == 200, card.text[:400]
    assert fragment.status_code == 200, fragment.text[:400]
    assert UNAVAILABLE in fragment.text
    assert UNAVAILABLE in card.text, "карта изменений на карточке называет причину"
    assert "бриф не собран" not in card.text, "блок доказательств собран"
    assert "Дифф не показан" not in fragment.text, "надпись не дублируется префиксом"
    for page in (card.text, fragment.text):
        assert "workspace-marker-2002" not in page
        assert "gone-2002" not in page
    assert any(
        "could not run" in r.getMessage() and "workspace-marker-2002" in r.getMessage()
        for r in caplog.records
    ), "путь остаётся в логе читателя диффа"

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
    client: AsyncClient, db, missing_workspace, monkeypatch, with_ac
):
    task_id = await _review_task(client, db, missing_workspace, "gone-readers", with_ac)
    _real_git(monkeypatch)

    brief = await client.get(f"/api/tasks/{task_id}/review-brief")
    queue = await client.get("/review-queue")
    prod_state = await client.get("/api/prod-state")

    assert brief.status_code == 200, brief.text[:400]
    assert queue.status_code == 200, queue.text[:400]
    assert prod_state.status_code == 200, prod_state.text[:400]
    if with_ac:
        rows = brief.json()["locator_resolution"]
        assert [r["status"] for r in rows] == ["unknown"]
        assert "working directory was not found" in rows[0]["reason"]
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

    from hub.services import review_brief, review_evidence

    task_id = await _review_task(client, db, missing_workspace, "gone-reasons")
    row = dict(await repo.get_task(db, task_id))
    boom = OSError(f"no such file {MARKER}")
    _real_git(monkeypatch)

    # review_evidence: контекст проекта (116).
    with monkeypatch.context() as patch:
        patch.setattr(
            "hub.services.orchestration.project_git_context",
            AsyncMock(side_effect=boom),
        )
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


async def test_a_directory_that_is_not_a_repository_leaks_no_path(
    client: AsyncClient, db, tmp_path, monkeypatch
):
    """Codex #2002 P2: ``resolve_ref`` отвечает ``unavailable`` БЕЗ исключения.

    Каталог на месте, но это не репозиторий; настоящий git. Деталь такого
    ответа несёт путь клона, и раньше она шла прямо в причину брифа.
    """
    plain = tmp_path / "plain-2002" / "not-a-repo-marker-2002"
    plain.mkdir(parents=True)
    task_id = await _review_task(client, db, str(plain), "plain-dir")
    _real_git(monkeypatch)

    brief = await client.get(f"/api/tasks/{task_id}/review-brief")
    from hub.services import review_evidence, task_diff

    base = await review_evidence.resolve_diff_base(db, task_id, "b", offline=True)
    shown = await task_diff.submission_diff(db, task_id)

    assert brief.status_code == 200, brief.text[:400]
    assert base["state"] == review_evidence.BASE_UNVERIFIED and base["reason"]
    for body in (brief.text, str(base), str(shown)):
        assert "not-a-repo-marker-2002" not in body
        assert "plain-2002" not in body


async def test_git_details_with_paths_never_reach_a_reason(
    client: AsyncClient, db, missing_workspace, monkeypatch
):
    """Остальные места, где деталь git/форджа шла в причину: свежесть, missing, проба слияния."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from hub.services import review_brief, review_evidence

    task_id = await _review_task(client, db, missing_workspace, "gone-details")
    _real_git(monkeypatch)
    leak = f"/srv/clones/{MARKER}"

    cases = [
        ("resolve_ref", ("missing", f"neither b nor origin/b exists in {leak}"), False),
        (
            "resolve_ref",
            ("unavailable", f"{leak} is not a readable git repository"),
            True,
        ),
    ]
    for name, answer, offline in cases:
        with monkeypatch.context() as patch:
            patch.setattr(plugins.git_ops, name, AsyncMock(return_value=answer))
            base = await review_evidence.resolve_diff_base(
                db, task_id, "b", offline=offline
            )
        assert base["reason"] and MARKER not in base["reason"], answer

    with monkeypatch.context() as patch:
        patch.setattr(
            plugins.git_ops,
            "resolve_ref",
            AsyncMock(return_value=("resolved", "c" * 40)),
        )
        patch.setattr(
            plugins.git_ops,
            "base_freshness",
            AsyncMock(return_value=("unverified", f"ls-remote не ответил: {leak}")),
        )
        base = await review_evidence.resolve_diff_base(db, task_id, "b", offline=False)
    assert base["reason"] and MARKER not in base["reason"]

    from hub.integrations.protocols import MergeabilityOutcome

    patch_target = AsyncMock(return_value=(MergeabilityOutcome.unknown, leak))
    monkeypatch.setattr(plugins.git_ops, "check_pr_mergeable", patch_target)
    merge = await review_brief._ask_base_merge(
        db, SimpleNamespace(id=task_id, pr_number=7)
    )
    assert merge.state == "unknown" and MARKER not in merge.reason


async def test_an_untraversable_parent_is_unreadable_not_missing(
    client: AsyncClient, db, tmp_path, monkeypatch
):
    """Codex #2002 P3: ``isdir`` глотал PermissionError и называл это «каталога нет»."""
    from hub.services import task_diff

    if os.geteuid() == 0:
        pytest.skip("root проходит сквозь chmod 000")
    parent = tmp_path / "locked-2002"
    workspace = parent / "workspace-marker-2002"
    workspace.mkdir(parents=True)
    task_id = await _review_task(client, db, str(workspace), "locked-parent")
    _real_git(monkeypatch)
    parent.chmod(0)
    try:
        shown = await task_diff.submission_diff(db, task_id)
    finally:
        parent.chmod(0o755)

    assert shown["state"] == task_diff.UNREADABLE
    assert shown["state"] != task_diff.WORKSPACE_MISSING
    assert "workspace-marker-2002" not in str(shown)


def test_public_reason_cuts_paths_and_remotes_and_keeps_the_rest() -> None:
    from hub.services.brief_failure import public_reason

    sha = "0123456789ab"
    keep = (
        f"{sha} task-1/x hub/web.py origin/develop и/или (tests/a.py:12) "
        "ветка task-2002/card-without-workspace /tasks/12 /api/tasks/3/diff ~5 минут "
        "https://github.com/o/r/pull/12 https://github.com/o/r/actions/runs/99, ок"
    )
    assert public_reason(keep) == keep
    assert public_reason("нет /srv/clones/x/.git и ~/work/r") == "нет <путь> и <путь>"
    assert public_reason("в ~alice/private/clone") == "в <путь>"
    assert public_reason("fatal: '/srv/private clone/.git' не репозиторий") == (
        "fatal: <путь> не репозиторий"
    )
    assert (
        public_reason('fatal: "/home/a b/c" и `/tmp/x y`') == "fatal: <путь> и <путь>"
    )
    assert (
        public_reason("fatal: 'https://h.io/o/r.git' down") == "fatal: '<remote>' down"
    )
    assert public_reason("https://u:t@github.com/o/r/pull/1") == "<remote>"
    for password in ("p;secret", "p,secret", "p%40secret", "p:q"):
        url = f"https://user:{password}@host.example/org/repo.git"
        assert public_reason(f"fatal: {url} down") == "fatal: <remote> down", password
    assert public_reason("https://evil.io/o/r/pull/1") == "<remote>"
    assert public_reason("git@github.com:org/r.git не отвечает") == (
        "<remote> не отвечает"
    )
    assert public_reason("ssh://git@h/r и /var/x") == "<remote> и <путь>"
    assert public_reason("") == ""


def test_public_reason_is_linear_and_never_cuts_its_input(monkeypatch) -> None:
    """Объём работы линеен: каждый шаблон идёт по ВСЕМУ тексту один раз, без обрезки."""
    from hub.services import brief_failure

    seen: list[int] = []

    class Spy:
        def __init__(self, inner):
            self.inner = inner

        def sub(self, repl, text):
            seen.append(len(text))
            return self.inner.sub(repl, text)

    names = (
        "_CRED_CHUNK",
        "_URL",
        "_SCP",
        "_JSON_PATH",
        "_QUOTED_PATH",
        "_OPEN_QUOTE_PATH",
        "_ABS_PATH",
    )
    for name in names:
        monkeypatch.setattr(brief_failure, name, Spy(getattr(brief_failure, name)))

    text = "a" * 100_000
    assert brief_failure.public_reason(text) == text, "вход не режется"
    assert seen == [100_000] * len(names), "один проход каждого шаблона по всему тексту"


def _strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for key, value in node.items():
            yield from _strings(key)
            yield from _strings(value)
    elif isinstance(node, (list, tuple)):
        for value in node:
            yield from _strings(value)


async def test_no_reason_source_of_the_brief_publishes_a_path_or_a_remote(
    client: AsyncClient, db, missing_workspace, monkeypatch
):
    """Охранный тест третьего круга: путь и URL подставлены В КАЖДЫЙ источник причин.

    Источники по одному ловились три раунда подряд; граница одна —
    ``public_reason`` на выходе брифа. Обход рекурсивный по всем строкам.
    """
    from unittest.mock import AsyncMock

    from hub import services
    from hub.integrations.protocols import MergeabilityOutcome
    from hub.services.review_brief import build_review_brief
    from hub.services.task_diff import submission_diff, submission_files

    path, url, scp = (
        "/tmp/leak-2002/clone",
        "https://h.io/o/r.git",
        "git@h.io:o/r",
    )
    leak = f"{path} {url} {scp}"
    task_id = await _review_task(client, db, missing_workspace, "gone-guard")
    await repo.update_task(db, task_id, pr_number=7)
    await db.commit()
    _real_git(monkeypatch)
    git = plugins.git_ops
    monkeypatch.setattr(
        services, "resolve_branch_tip", AsyncMock(return_value=("", f"fetch: {leak}"))
    )
    monkeypatch.setattr(
        git, "resolve_ref", AsyncMock(return_value=("unavailable", leak))
    )
    monkeypatch.setattr(
        git, "base_freshness", AsyncMock(return_value=("unverified", leak))
    )
    monkeypatch.setattr(
        git,
        "check_pr_mergeable",
        AsyncMock(return_value=(MergeabilityOutcome.unknown, leak)),
    )
    monkeypatch.setattr(git, "branch_diff", AsyncMock(side_effect=OSError(leak)))
    monkeypatch.setattr(git, "branch_diff_local", AsyncMock(side_effect=OSError(leak)))
    monkeypatch.setattr(git, "commit_exists", AsyncMock(side_effect=OSError(leak)))
    # statement_freshness: ошибка чтения базы несёт путь (не через граничную правку поля).
    monkeypatch.setattr(
        "hub.services.statement_freshness.fetchall",
        AsyncMock(side_effect=OSError(leak)),
    )
    # evidence_coverage собирается из сырых причин diff_base и base_merge.
    raw_base = {
        "base": "develop",
        "source": "x",
        "state": "unverified",
        "reason": leak,
        "sha": "",
    }
    monkeypatch.setattr(
        "hub.services.review_evidence.resolve_diff_base",
        AsyncMock(return_value=raw_base),
    )

    brief = await build_review_brief(db, task_id, observe_only=False)
    assert brief is not None
    dump = brief.model_dump(mode="json")
    assert dump["sha_check_reason"], "источник tip_reason дошёл до ответа"
    shown = [await submission_diff(db, task_id), await submission_files(db, task_id)]
    pages = [
        (await client.get(f"/tasks/{task_id}")).text,
        (await client.get(f"/tasks/{task_id}/diff")).text,
    ]
    assert dump["statement_freshness"]["reason"], "источник freshness дошёл до ответа"
    assert dump["evidence_coverage"]["checks_missing"], (
        "coverage собран из сырых причин"
    )
    for text in [*_strings(dump), *_strings(shown)]:
        for bad in ("/tmp", "/var", "https://", "git@"):
            assert bad not in text, (bad, text[:200])
    for (
        page
    ) in pages:  # страницы несут свои https:// (шрифты, ссылки): ищем подстановку
        assert "leak-2002" not in page and "h.io" not in page


async def test_the_boundary_cleans_reasons_even_when_a_source_does_not(
    client: AsyncClient, db, missing_workspace, monkeypatch
):
    """Граница не опирается на чистоту источников: сырой путь в готовой секции режется."""
    from unittest.mock import AsyncMock

    from hub.models import BaseMergeState, CallSiteSection
    from hub.services import review_brief, review_evidence, task_diff

    leak = "/tmp/leak-2002/clone https://h.io/o/r.git"
    task_id = await _review_task(client, db, missing_workspace, "gone-boundary")
    _real_git(monkeypatch)
    raw_base = {
        "base": "develop",
        "source": "x",
        "state": review_evidence.BASE_UNVERIFIED,
        "reason": leak,
        "sha": "",
    }
    monkeypatch.setattr(
        review_evidence, "resolve_diff_base", AsyncMock(return_value=raw_base)
    )
    monkeypatch.setattr(
        review_brief,
        "base_merge_section",
        AsyncMock(return_value=BaseMergeState(state="unknown", reason=leak)),
    )
    monkeypatch.setattr(
        review_brief,
        "build_call_sites_section",
        AsyncMock(return_value=CallSiteSection(status="unknown", reason=leak)),
    )

    brief = await review_brief.build_review_brief(db, task_id, observe_only=True)

    assert brief is not None
    assert brief.diff_base.reason and brief.base_merge.reason
    assert brief.call_sites.reason
    for text in (
        brief.diff_base.reason,
        brief.base_merge.reason,
        brief.call_sites.reason,
    ):
        assert "/tmp" not in text and "https://" not in text, text
    blank = task_diff._publish(task_diff._blank(task_diff.UNREADABLE, leak))
    assert "/tmp" not in blank["reason"] and "https://" not in blank["reason"]


async def test_app_routes_in_a_reason_are_not_filesystem_paths(
    client: AsyncClient, db, missing_workspace, monkeypatch
):
    """Белый список корней: ``/tasks/12`` в причине цел (граница не «любой слэш»)."""
    from unittest.mock import AsyncMock

    from hub.models import BaseMergeState
    from hub.services import review_brief

    task_id = await _review_task(client, db, missing_workspace, "gone-routes")
    monkeypatch.setattr(
        review_brief,
        "base_merge_section",
        AsyncMock(
            return_value=BaseMergeState(
                state="unknown", reason="см. /tasks/12 и /srv/clone"
            )
        ),
    )

    brief = await review_brief.build_review_brief(db, task_id, observe_only=True)

    assert brief is not None
    assert brief.base_merge.reason == "см. /tasks/12 и <путь>"


async def test_the_review_queue_view_is_cleaned_as_a_whole(
    client: AsyncClient, db, missing_workspace, monkeypatch
):
    """Очередь ревью: путь в любой строке готового вида режется, названия не трогаются."""
    from hub.services import review_evidence, review_queue

    task_id = await _review_task(client, db, missing_workspace, "gone-queue")
    leak = "/tmp/leak-2002/clone https://h.io/o/r.git"
    monkeypatch.setattr(
        review_evidence, "sha_check_of", lambda *a, **k: ("unknown", leak)
    )
    view = await review_queue.review_queue(db)

    rows = [r for r in view.rows if r.task_id == task_id]
    assert rows, "задача в очереди"
    for text in _strings(view.model_dump(mode="json")):
        assert "/tmp" not in text and "h.io" not in text, text


def _brief_with_authored_text(path: str):
    from hub.models import (
        LiveCheckState,
        PathNoticeItem,
        PathNoticesView,
        ReviewBrief,
        TaskStatus,
        ValidationStanding,
    )

    return ReviewBrief(
        task_id=1,
        title=f"t {path}",
        status=TaskStatus.review,
        ci_integrity={"note": f"запустите {path}", "reason": f"сбой {path}"},
        validation=ValidationStanding(
            author_run_lines=[f"$ {path}"], headline=f"цитата «{path}»"
        ),
        live_check=LiveCheckState(observation=f"видел {path}", reason=f"нет {path}"),
        path_notices=PathNoticesView(
            generation=1,
            state="matched",
            text=f"Скопировать в {path}",
            reason=f"нет {path}",
            notices=[PathNoticeItem(text=f"Скопировать в {path}", paths=[path])],
        ),
    )


def test_only_reason_keys_are_scrubbed_and_enums_stay_enums() -> None:
    from hub.models import TaskStatus
    from hub.services.brief_failure import scrub_strings

    path = "/opt/hub/scripts/deploy.sh"
    brief = scrub_strings(_brief_with_authored_text(path))

    assert isinstance(brief.status, TaskStatus) and brief.status is TaskStatus.review
    assert brief.title == f"t {path}"
    assert brief.ci_integrity["note"] == f"запустите {path}"
    assert brief.validation.author_run_lines == [f"$ {path}"]
    assert brief.validation.headline == f"цитата «{path}»"
    assert brief.live_check.observation == f"видел {path}"
    assert brief.path_notices.text == f"Скопировать в {path}"
    assert brief.path_notices.notices[0].text == f"Скопировать в {path}"
    assert brief.path_notices.notices[0].paths == [path]
    # а причины тех же блоков — режутся, включая будущее поле foo_reason
    assert brief.ci_integrity["reason"] == "сбой <путь>"
    assert brief.live_check.reason == "нет <путь>"
    assert brief.path_notices.reason == "нет <путь>"
    assert scrub_strings({"foo_reason": path, "text": path}) == {
        "foo_reason": "<путь>",
        "text": path,
    }


def test_a_string_enum_under_a_reason_key_stays_an_enum() -> None:
    from hub.models import TaskStatus
    from hub.services.brief_failure import scrub_strings

    kept = scrub_strings({"reason": TaskStatus.review, "n": 3})

    assert kept["reason"] is TaskStatus.review and kept["n"] == 3


async def test_a_reviewers_text_keeps_its_paths_while_hub_reasons_are_cleaned(
    client: AsyncClient, db, missing_workspace, monkeypatch
):
    """Находки ревьюера — содержание: ``/etc/hosts`` в них цел и в брифе, и на карточке."""
    from unittest.mock import AsyncMock

    from hub.services import review_evidence

    task_id = await _review_task(client, db, missing_workspace, "gone-reviewer")
    said = "Writes /etc/hosts instead of /etc/resolv.conf"
    resp = await client.post(
        f"/api/tasks/{task_id}/machine-review",
        json={
            "harness_skill": "lite-diff-review",
            "agent_count": 1,
            "tokens_spent": 1,
            "model": "m",
            "raw_count": 3,
            "incomplete": False,
            "findings_confirmed": [
                {
                    "title": "hosts",
                    "severity": "medium",
                    "file": "a.py",
                    "locator": "file",
                    "detail": said,
                }
            ],
            "findings_rejected": [{"title": "rej", "reason": said}],
            "unresolved": [{"title": "unr", "why": said}],
            "agent": "cursor-cloud-reviewer",
        },
    )
    assert resp.status_code == 200, resp.text
    raw_base = {
        "base": "develop",
        "source": "x",
        "state": review_evidence.BASE_UNVERIFIED,
        "reason": "нет /tmp/leak-2002/clone",
        "sha": "",
    }
    monkeypatch.setattr(
        review_evidence, "resolve_diff_base", AsyncMock(return_value=raw_base)
    )

    brief = (await client.get(f"/api/tasks/{task_id}/review-brief")).json()
    page = (await client.get(f"/tasks/{task_id}")).text

    review = brief["machine_review"]
    assert review["findings_confirmed"][0]["detail"] == said
    assert review["findings_rejected"][0]["reason"] == said
    assert review["unresolved"][0]["why"] == said
    assert brief["diff_base"]["reason"] == "нет <путь>"
    assert "/etc/hosts" in page and "/etc/resolv.conf" in page
    assert "leak-2002" not in page


async def test_a_missing_git_executable_is_not_a_missing_directory(
    client: AsyncClient, db, tmp_path, monkeypatch
):
    """Каталог на месте, git не найден: тот же FileNotFoundError — не «каталога нет»."""
    from unittest.mock import AsyncMock

    from hub.services import review_brief

    present = tmp_path / "present-git-2002"
    present.mkdir()
    task_id = await _review_task(client, db, str(present), "no-git")
    _real_git(monkeypatch)
    no_git = FileNotFoundError(2, "No such file or directory", "git")
    monkeypatch.setattr(
        review_brief, "resolve_locators_at_ref", AsyncMock(side_effect=no_git)
    )

    brief = await client.get(f"/api/tasks/{task_id}/review-brief")

    assert brief.status_code == 200, brief.text
    rows = brief.json()["locator_resolution"]
    assert rows and all(r["status"] == "unknown" for r in rows)
    assert all("git could not run (FileNotFoundError)" in r["reason"] for r in rows)
    assert not any("directory was not found" in r["reason"] for r in rows)
