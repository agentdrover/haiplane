"""Доставка на форже без API-мержа (#1116, эпик #1112).

Тесты гоняют НАСТОЯЩИЙ git — локальный bare-репозиторий вместо GitVerse, —
потому что предмет проверки здесь именно git: мерж, push, достижимость
коммита. Мок git проверял бы, что мы правильно позвали функцию, которую сами
же и подменили.

Форж подменяется мокой: его роль в этой фиче сведена к трём ответам —
какие у PR ветки, попал ли SHA в базу, закрылся ли PR.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from hub.integrations.git_ops import GitOpsIntegration
from hub.integrations.protocols import MergeabilityOutcome


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(cwd),
            "GIT_AUTHOR_NAME": "T",
            "GIT_AUTHOR_EMAIL": "t@e",
            "GIT_COMMITTER_NAME": "T",
            "GIT_COMMITTER_EMAIL": "t@e",
        },
    ).stdout.strip()


@pytest.fixture
def repo_pair(tmp_path: Path) -> tuple[Path, Path]:
    """Bare-«форж» и рабочий клон с ветками main и task."""
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    work = tmp_path / "work"
    subprocess.run(
        ["git", "clone", "-q", str(bare), str(work)], check=True, capture_output=True
    )
    _git(work, "config", "user.email", "t@e")
    _git(work, "config", "user.name", "T")
    _git(work, "checkout", "-q", "-B", "main")
    (work / "README").write_text("base\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "base")
    _git(work, "push", "-q", "origin", "main")

    _git(work, "checkout", "-q", "-b", "task-1/x")
    (work / "feature.txt").write_text("feature\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "feat: работа задачи")
    _git(work, "push", "-q", "origin", "task-1/x")
    _git(work, "checkout", "-q", "main")
    return bare, work


def _forge(*, contains: bool | None = True, closed: bool = True) -> AsyncMock:
    forge = AsyncMock()
    forge.can_merge_via_api = False
    forge.pr_refs.return_value = ("main", "task-1/x")
    forge.pr_mergeability.return_value = (
        MergeabilityOutcome.unavailable,
        "форж не умеет",
    )
    forge.branch_contains.return_value = contains
    forge.close_pr.return_value = closed
    return forge


async def test_delivery_is_proven_by_the_base_branch_not_the_pr(repo_pair):
    """AC-1. Доставка подтверждается коммитом в базе, а не ответом про PR.

    Измерено на живом GitVerse 01.09.2026: после настоящего merge --no-ff и
    push'а PR остаётся open, merged=False, а GET /pulls/{n}/merge отвечает 404
    и ДО, и ПОСЛЕ мержа. Подтверждение через PR объявило бы доставленную
    работу недоставленной, поэтому спрашивается база.
    """
    bare, work = repo_pair
    forge = _forge()
    ops = GitOpsIntegration(forge=forge)

    ok, detail = await ops.merge_pr_by_push(
        7, "feat(task): работа (#1)", repo=str(work)
    )

    assert ok, detail
    # Работа ДЕЙСТВИТЕЛЬНО в базовой ветке удалённого репозитория.
    log = _git(work, "log", "--oneline", "origin/main")
    _git(work, "fetch", "-q", "origin")
    assert "feat(task): работа (#1)" in _git(
        work, "log", "--format=%s", "-3", "origin/main"
    ), log
    assert _git(work, "cat-file", "-t", f"{detail}") == "commit", (
        "деталь успеха — это SHA мержа"
    )
    # И подтверждение спрашивалось у базы, а не у PR.
    forge.branch_contains.assert_awaited_once()
    assert forge.branch_contains.await_args.args[0] == "main"


async def test_delivered_pr_is_closed_explicitly(repo_pair):
    """AC-2. После доставки PR закрывается — иначе висит открытым вечно.

    GitVerse не замечает мержа пушем, и открытый PR на доставленной ветке
    заставит pr_for_branch находить его снова, а гейт — открывать доставку
    заново.
    """
    bare, work = repo_pair
    forge = _forge()
    ops = GitOpsIntegration(forge=forge)

    ok, _ = await ops.merge_pr_by_push(7, "feat(task): работа (#1)", repo=str(work))

    assert ok
    forge.close_pr.assert_awaited_once()
    assert forge.close_pr.await_args.args[0] == 7


async def test_closing_happens_only_after_the_proof(repo_pair):
    """Порядок: сначала доказательство, потом закрытие.

    Незакрытый PR неприятен, но закрытый без мержа врёт сильнее: он выглядит
    как решённый вопрос там, где работа не доставлена.
    """
    bare, work = repo_pair
    forge = _forge(contains=False)
    ops = GitOpsIntegration(forge=forge)

    ok, detail = await ops.merge_pr_by_push(
        7, "feat(task): работа (#1)", repo=str(work)
    )

    assert ok is False
    assert "не появился" in detail
    forge.close_pr.assert_not_awaited()


async def test_unconfirmed_is_not_the_same_as_not_delivered(repo_pair):
    """«Спросить не удалось» и «не доставлено» — разные ответы (#725).

    Оба не дают засчитать доставку, но ведут к разному: первое — спросить
    снова, второе — разбираться, почему push не долетел. Поэтому деталь
    обязана их различать, иначе поллер будет искать несуществующую поломку.
    """
    bare, work = repo_pair
    ops = GitOpsIntegration(forge=_forge(contains=None))

    ok, detail = await ops.merge_pr_by_push(
        7, "feat(task): работа (#1)", repo=str(work)
    )

    assert ok is False
    # Метка МАШИННАЯ, а не только словесная: по ней гейт классифицирует
    # состояние как временное. Без префикса «спросите снова» читал человек,
    # а гейт читал обычный отказ и уводил в needs_decision работу, которая
    # уже лежит в базовой ветке.
    from hub.integrations.git_ops import MERGE_UNCONFIRMED
    from hub.services.orchestration import TRANSIENT_GATE_PREFIXES

    assert detail.startswith(MERGE_UNCONFIRMED)
    assert detail.startswith(TRANSIENT_GATE_PREFIXES), (
        "неподтверждённый мерж обязан читаться гейтом как временное состояние"
    )


async def test_protected_base_names_the_cause(repo_pair):
    """AC-4. Закрытая база — это НАЗВАННАЯ причина, а не «форж отказал».

    Хук на стороне «форжа» отвергает push, как это сделала бы защита ветки.
    Отказ обязан назвать именно её: конфликт, отозванный токен и защита
    чинятся разными руками (#970).
    """
    bare, work = repo_pair
    hook = bare / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho 'protected branch: main' >&2\nexit 1\n")
    hook.chmod(0o755)
    ops = GitOpsIntegration(forge=_forge())

    ok, detail = await ops.merge_pr_by_push(
        7, "feat(task): работа (#1)", repo=str(work)
    )

    assert ok is False
    assert "закрыта от прямого push" in detail
    assert "main" in detail


async def test_conflict_names_the_files(repo_pair):
    """Конфликт называет файлы, а не только факт (#970)."""
    bare, work = repo_pair
    # Обе ветки трогают один файл по-разному — гарантированный конфликт.
    _git(work, "checkout", "-q", "main")
    (work / "feature.txt").write_text("другое\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "чужая правка того же файла")
    _git(work, "push", "-q", "origin", "main")
    ops = GitOpsIntegration(forge=_forge())

    ok, detail = await ops.merge_pr_by_push(
        7, "feat(task): работа (#1)", repo=str(work)
    )

    assert ok is False
    assert "не сливается" in detail
    assert "feature.txt" in detail, "конфликт без имён файлов никуда не ведёт"


async def test_merge_pr_routes_by_declared_capability(repo_pair):
    """Ветвление по ОБЪЯВЛЕННОЙ способности, а не по попытке.

    Форж, умеющий мержить, зовётся напрямую; неумеющий не зовётся вовсе — его
    merge_pr существует только чтобы громко пожаловаться, если кто-то забыл
    прочитать флаг.
    """
    bare, work = repo_pair

    api_forge = AsyncMock()
    api_forge.can_merge_via_api = True
    api_forge.merge_pr.return_value = True
    ops = GitOpsIntegration(forge=api_forge)
    assert await ops.merge_pr(7, 1, "работа", repo=str(work)) is True
    api_forge.merge_pr.assert_awaited_once()

    push_forge = _forge()
    ops = GitOpsIntegration(forge=push_forge)
    assert await ops.merge_pr(7, 1, "работа", repo=str(work)) is True
    push_forge.merge_pr.assert_not_awaited()


async def test_mergeability_by_trial_keeps_the_outcomes_distinct(repo_pair):
    """AC-3. Пробный мерж различает исходы, и unavailable не значит конфликт."""
    bare, work = repo_pair
    ops = GitOpsIntegration(forge=_forge())

    outcome, detail = await ops.check_pr_mergeable(7, repo=str(work))
    assert outcome is MergeabilityOutcome.mergeable, detail

    # Тот же PR после чужой правки того же файла — конфликт с именами.
    _git(work, "checkout", "-q", "main")
    (work / "feature.txt").write_text("другое\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "чужая правка")
    _git(work, "push", "-q", "origin", "main")
    outcome, detail = await ops.check_pr_mergeable(7, repo=str(work))
    assert outcome is MergeabilityOutcome.conflicting
    assert "feature.txt" in detail

    # Форж не назвал веток — это «спросить не удалось», НЕ конфликт.
    blind = _forge()
    blind.pr_refs.return_value = ("", "")
    outcome, detail = await GitOpsIntegration(forge=blind).check_pr_mergeable(
        7, repo=str(work)
    )
    assert outcome is MergeabilityOutcome.unavailable


async def test_the_working_clone_is_left_alone(repo_pair):
    """Мерж не трогает рабочий клон: ни ветку, ни дерево.

    Клон общий, может стоять на чужой ветке с незакоммиченной правкой. Мерж
    ради бухгалтерии, испортивший чужую работу, — это #949.
    """
    bare, work = repo_pair
    _git(work, "checkout", "-q", "-b", "someone-elses-work")
    (work / "dirty.txt").write_text("незакоммиченное\n")
    before = _git(work, "rev-parse", "--abbrev-ref", "HEAD")

    ok, _ = await GitOpsIntegration(forge=_forge()).merge_pr_by_push(
        7, "feat(task): работа (#1)", repo=str(work)
    )

    assert ok
    assert _git(work, "rev-parse", "--abbrev-ref", "HEAD") == before
    assert (work / "dirty.txt").read_text() == "незакоммиченное\n"
    assert not list(Path(work).parent.glob(".hub-merge-*")), "временное дерево убрано"


async def test_closed_pr_is_never_read_as_delivered(repo_pair):
    """AC-5. Состояние PR не участвует в решении о доставке вовсе.

    Измерено 01.09.2026: доставленный хабом PR (закрытый нами после пуш-мержа)
    и брошенный PR через API GitVerse НЕРАЗЛИЧИМЫ — оба closed, merged=False.
    Значит pr_state не может быть оракулом доставки ни при каком условии, и
    проверяется здесь именно это: путь доставки его не спрашивает.

    Проверка «чего не звали» выглядит слабой, но она про единственный способ
    ошибиться: спросить состояние PR кажется естественным — на GitHub так и
    делают, — а на GitVerse ответ будет одинаковым для доставленного и
    брошенного.
    """
    bare, work = repo_pair
    forge = _forge()
    forge.pr_state.return_value = "closed"

    ok, _ = await GitOpsIntegration(forge=forge).merge_pr_by_push(
        7, "feat(task): работа (#1)", repo=str(work)
    )

    assert ok
    forge.pr_state.assert_not_awaited()
    forge._pr_merged.assert_not_awaited()


async def test_close_failure_does_not_undo_a_landed_delivery(repo_pair):
    """Работа в базе, а PR не закрылся — доставка ВСЁ РАВНО состоялась.

    Порядок ценностей здесь такой: незакрытый PR — грязь, которую видно и
    можно убрать руками; объявленная недоставленной работа, уже лежащая в
    базовой ветке, — потерянная задача и лишнее решение человека. Поэтому
    неудача закрытия логируется, но исхода не меняет.
    """
    bare, work = repo_pair
    forge = _forge(closed=False)

    ok, detail = await GitOpsIntegration(forge=forge).merge_pr_by_push(
        7, "feat(task): работа (#1)", repo=str(work)
    )

    assert ok is True, detail
    forge.close_pr.assert_awaited_once()


async def test_scratch_worktrees_do_not_collide_between_clones(tmp_path):
    """Два клона с PR №1 у каждого не делят одно временное дерево.

    Путь строился из каталога-родителя и номера PR — а номера у разных
    проектов совпадают сплошь и рядом.
    """
    from hub.integrations.git_ops import _scratch_worktree

    first = _scratch_worktree("/srv/ws/alpha", "merge", 1)
    second = _scratch_worktree("/srv/ws/beta", "merge", 1)

    assert first != second
    assert "alpha" in first and "beta" in second


async def test_access_denial_is_not_reported_as_a_protected_branch(repo_pair):
    """«Permission denied (publickey)» — это ключ, а не защита ветки.

    Слово denied есть в обоих сообщениях, и классификация по нему отправляла
    человека снимать защиту там, где надо чинить доступ. AC-4 требует НАЗВАТЬ
    причину — назвать неверную хуже, чем не назвать никакой.
    """
    bare, work = repo_pair
    hook = bare / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho 'Permission denied (publickey)' >&2\nexit 1\n")
    hook.chmod(0o755)

    ok, detail = await GitOpsIntegration(forge=_forge()).merge_pr_by_push(
        7, "feat(task): работа (#1)", repo=str(work)
    )

    assert ok is False
    assert "по доступу" in detail
    assert "закрыта от прямого push" not in detail


async def test_trial_merge_failure_is_not_a_conflict(repo_pair, monkeypatch):
    """Упавший git при пробе — unavailable, а не conflicting (#970).

    Таймаут и падение самого git лечатся повтором, конфликт — руками
    человека. Схлопнуть их значит поднять ложную тревогу.
    """
    from hub.integrations import git_ops as git_ops_mod

    bare, work = repo_pair
    ops = GitOpsIntegration(forge=_forge())
    real_git = git_ops_mod._git

    async def flaky(*args, **kw):
        if args and args[0] == "merge":
            return (128, "", "fatal: git упал")
        return await real_git(*args, **kw)

    monkeypatch.setattr(git_ops_mod, "_git", flaky)
    outcome, detail = await ops.check_pr_mergeable(7, repo=str(work))

    assert outcome is MergeabilityOutcome.unavailable
    assert outcome is not MergeabilityOutcome.conflicting
    assert "rc=128" in detail
