"""Ветка, в которую всё вливается, может исчезнуть — и это должно быть видно (#947).

24.08.2026 релизный PR develop→main смёржен squash-ом, и после мержа ветки
develop в репозитории не осталось. Сутки на проекте нельзя было доставить
ничего: GitHub отказывается открывать PR в несуществующую базу, а pair-start
отказывается резать ветку от базы, разошедшейся с origin. Ни одна поверхность
хаба об этом не сказала — проверка клона показывала ``match``, потому что
сверяла ИМЯ ветки в конфиге клона с именем в настройке проекта. Имя совпадало.
Ветки не было.

Здесь держатся три поведения, и каждое отвечает своему AC:

* AC-1 — состояние проекта отличает «ветка на месте» от «ветки нет в remote»,
  и отдельно от «не смог посмотреть». Правило то же, что у CIRunReportState
  (#546) и sha_check (#572): ненаблюдённое не выдаётся за наблюдённое;
* AC-3 — бриф ревью, чья база сравнения указывает на коммит вне remote базовой
  ветки, говорит «база протухла» и не показывает пустой дифф как сходимость;
* AC-2 — релизный поток, смёржив интеграционную ветку в релизную, проверяет,
  что интеграционная жива, и восстанавливает её от релизной, записывая факт.

Тесты работают на НАСТОЯЩИХ репозиториях: bare-remote и клон рядом с ним.
Мокать git здесь нечего — вопрос ровно в том, что отвечает git про remote,
а не в том, как мы пересказываем свой мок.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from hub import git_policy
from hub.integrations.git_ops import GitOpsIntegration
from hub.integrations.registry import plugins
from hub.services import review_evidence


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _remote_with_clone(tmp_path: Path, branch: str) -> tuple[Path, Path]:
    """Bare-репозиторий с веткой ``branch`` и склонированный из него рабочий."""
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", branch)
    _git(seed, "config", "user.email", "t@example.com")
    _git(seed, "config", "user.name", "T")
    (seed / "README").write_text("x")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-qm", "seed")
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", branch, str(remote)],
        check=True,
        capture_output=True,
    )
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "-q", "--no-verify", "origin", branch)

    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "-q", str(remote), str(clone)],
        check=True,
        capture_output=True,
    )
    _git(clone, "config", "user.email", "t@example.com")
    _git(clone, "config", "user.name", "T")
    return remote, clone


def _release_takes_the_branch(remote: Path, clone: Path, branch: str) -> None:
    """Проиграть 24.08: содержимое уезжает в main, ветка исчезает.

    HEAD bare-репозитория переводится на main до удаления — не ради обхода
    защиты, а потому что так выглядит настоящий репозиторий с default main:
    голый репозиторий отказывается удалять ветку, на которую смотрит его HEAD.
    """
    _git(clone, "push", "-q", "--no-verify", "origin", f"{branch}:main")
    _git(remote, "symbolic-ref", "HEAD", "refs/heads/main")
    deleted = _git(clone, "push", "-q", "--no-verify", "origin", "--delete", branch)
    assert deleted.returncode == 0, deleted.stderr
    assert branch not in _git(remote, "branch", "--list", branch).stdout


@pytest.fixture(autouse=True)
def _no_cached_answers():
    """Кеш ответов ls-remote живёт минуту — в тестах remote меняется за миллисекунды."""
    git_policy.forget_remote_presence()
    yield
    git_policy.forget_remote_presence()


# ---------------------------------------------------------------------------
# AC-1 — состояние проекта отвечает про существование ветки, а не про её имя
# ---------------------------------------------------------------------------


def test_a_living_branch_still_reads_as_agreement(tmp_path: Path) -> None:
    _, clone = _remote_with_clone(tmp_path, "develop")
    git_policy.activate_quietly(str(clone), base_branch="develop")

    state = git_policy.branch_sync(str(clone), "develop")

    assert state.state == git_policy.BRANCH_IN_SYNC
    assert state.agrees
    assert "develop" in state.reason


def test_a_branch_deleted_in_the_remote_is_not_agreement(tmp_path: Path) -> None:
    """Сердце дефекта: имя в конфиге совпадает, а ветки в remote больше нет."""
    remote, clone = _remote_with_clone(tmp_path, "develop")
    git_policy.activate_quietly(str(clone), base_branch="develop")
    # Релиз забрал ветку: содержимое уехало в main, develop удалён.
    _release_takes_the_branch(remote, clone, "develop")

    state = git_policy.branch_sync(str(clone), "develop")

    assert state.state == git_policy.BRANCH_MISSING
    assert state.state != git_policy.BRANCH_IN_SYNC
    assert not state.agrees
    assert "нет в remote" in state.reason
    # Имя в клоне всё это время записано верно — то самое совпадение, из-за
    # которого прежняя проверка отвечала match. Держим его в ответе, иначе
    # читатель не поймёт, почему «сходится» вдруг перестало быть правдой.
    assert state.clone_branch == "develop"
    assert state.project_branch == "develop"
    assert _git(
        clone, "config", "--get", git_policy.BASE_BRANCH_KEY
    ).stdout.strip() == ("develop")


def test_a_remote_that_cannot_be_asked_is_not_a_missing_branch(tmp_path: Path) -> None:
    """«Не смог спросить» не превращается ни в «ветки нет», ни в молчаливое «всё хорошо»."""
    _, clone = _remote_with_clone(tmp_path, "develop")
    git_policy.activate_quietly(str(clone), base_branch="develop")
    # Remote уехал: каталога больше нет, ls-remote падает.
    _git(clone, "remote", "set-url", "origin", str(tmp_path / "gone.git"))

    present, why = git_policy.remote_has_branch(str(clone), "develop")
    state = git_policy.branch_sync(str(clone), "develop")

    assert present is None
    assert why
    assert state.state != git_policy.BRANCH_MISSING
    # Ответ остаётся про хук — но не притворяется ответом про ветку.
    assert "не проверено" in state.reason


def test_a_clone_without_origin_is_never_read_as_a_missing_branch(
    tmp_path: Path,
) -> None:
    _, clone = _remote_with_clone(tmp_path, "develop")
    git_policy.activate_quietly(str(clone), base_branch="develop")
    _git(clone, "remote", "remove", "origin")

    present, why = git_policy.remote_has_branch(str(clone), "develop")

    assert present is None
    assert "origin" in why


def test_the_answer_is_cached_but_a_restored_branch_is_seen_at_once(
    tmp_path: Path,
) -> None:
    """Кеш бережёт карточку проекта от сетевого вызова, но не держит устаревший ответ."""
    remote, clone = _remote_with_clone(tmp_path, "develop")
    git_policy.activate_quietly(str(clone), base_branch="develop")
    _release_takes_the_branch(remote, clone, "develop")
    assert git_policy.branch_sync(str(clone), "develop").state == (
        git_policy.BRANCH_MISSING
    )

    # Ветку вернули — но кеш ещё помнит «нет».
    restored = _git(
        clone, "push", "-q", "--no-verify", "origin", "HEAD:refs/heads/develop"
    )
    assert restored.returncode == 0, restored.stderr
    assert git_policy.branch_sync(str(clone), "develop").state == (
        git_policy.BRANCH_MISSING
    ), "кеш обязан быть настоящим кешем, иначе он ничего не бережёт"

    # Тот, кто вернул ветку, забывает кеш — и следующий читатель видит правду.
    git_policy.forget_remote_presence(str(clone), "develop")
    assert git_policy.branch_sync(str(clone), "develop").state == (
        git_policy.BRANCH_IN_SYNC
    )


# ---------------------------------------------------------------------------
# AC-3 — бриф не выдаёт пустой дифф за сходимость, когда база протухла
# ---------------------------------------------------------------------------


async def _freshness(clone: Path, base: str, sha: str) -> tuple[str, str]:
    """Настоящая реализация, не заглушка из conftest: вопрос ровно к git."""
    return await GitOpsIntegration().base_freshness(str(clone), base, sha)


def _sha(repo: Path, ref: str) -> str:
    return _git(repo, "rev-parse", ref).stdout.strip()


@pytest.mark.asyncio
async def test_a_base_the_remote_still_carries_is_current(tmp_path: Path) -> None:
    _, clone = _remote_with_clone(tmp_path, "develop")

    state, detail = await _freshness(clone, "develop", _sha(clone, "develop"))

    assert state == "current", detail


@pytest.mark.asyncio
async def test_a_base_behind_the_remote_is_still_current(tmp_path: Path) -> None:
    """Отставание — не протухание: коммит по-прежнему лежит в ветке remote."""
    _, clone = _remote_with_clone(tmp_path, "develop")
    behind = _sha(clone, "develop")
    (clone / "NEXT").write_text("y")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-qm", "next")
    _git(clone, "push", "-q", "--no-verify", "origin", "develop")

    state, detail = await _freshness(clone, "develop", behind)

    assert state == "current", detail


@pytest.mark.asyncio
async def test_a_squash_release_makes_the_local_base_stale(tmp_path: Path) -> None:
    """Тот самый случай: содержимое то же, а коммита нет в линии remote."""
    remote, clone = _remote_with_clone(tmp_path, "develop")
    local_base = _sha(clone, "develop")
    # Релиз: squash в main и та же линия обратно в develop — содержимое
    # совпадает, родословная расходится.
    _git(clone, "checkout", "-q", "--orphan", "release")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-qm", "release: squash of develop")
    _git(clone, "push", "-q", "--no-verify", "-f", "origin", "release:develop")
    _git(clone, "fetch", "-q", "origin")

    state, detail = await _freshness(clone, "develop", local_base)

    assert state == "stale", detail
    assert local_base[:12] in detail


@pytest.mark.asyncio
async def test_a_base_branch_deleted_upstream_is_stale_not_unverified(
    tmp_path: Path,
) -> None:
    remote, clone = _remote_with_clone(tmp_path, "develop")
    local_base = _sha(clone, "develop")
    _release_takes_the_branch(remote, clone, "develop")
    # Прунинг убирает remote-ссылку — ровно то состояние, в котором клон хаба
    # 25.08 отвечал «resolved» на протухшую базу.
    _git(clone, "fetch", "-q", "--prune", "origin")

    state, detail = await _freshness(clone, "develop", local_base)

    assert state == "stale", detail
    assert "нет в remote" in detail


@pytest.mark.asyncio
async def test_a_workspace_without_origin_cannot_judge_freshness(
    tmp_path: Path,
) -> None:
    _, clone = _remote_with_clone(tmp_path, "develop")
    local_base = _sha(clone, "develop")
    _git(clone, "remote", "remove", "origin")

    state, detail = await _freshness(clone, "develop", local_base)

    assert state == "unverified", detail
    assert state != "stale"


@pytest.mark.asyncio
async def test_the_brief_calls_a_stale_base_stale_and_takes_no_diff(
    tmp_path: Path, monkeypatch
) -> None:
    """Сквозной путь брифа: состояние, причина и снятая команда диффа."""
    remote, clone = _remote_with_clone(tmp_path, "develop")
    _release_takes_the_branch(remote, clone, "develop")
    _git(clone, "fetch", "-q", "--prune", "origin")

    monkeypatch.setattr(
        "hub.services.orchestration.project_git_context",
        AsyncMock(return_value={"repo": str(clone), "base_branch": "develop"}),
    )
    # Заглушка git из conftest ответила бы «git не настроен» на любой вопрос —
    # а проверяется именно то, что бриф спрашивает настоящий git.
    monkeypatch.setattr(plugins, "git_ops", GitOpsIntegration())

    base = await review_evidence.resolve_diff_base(None, 947, "task-947/x")

    assert base["state"] == review_evidence.BASE_STALE
    assert base["state"] != review_evidence.BASE_RESOLVED
    assert "протухла" in base["reason"]
    assert review_evidence.base_blocks_diff(base["state"])
    # Команда диффа не предлагается: предложенная команда читается как
    # приглашение проверить, а эта сравнила бы с несуществующей веткой.
    assert review_evidence.diff_command_for(base, "task-947/x") == ""


@pytest.mark.asyncio
async def test_a_healthy_base_keeps_the_diff_command(
    tmp_path: Path, monkeypatch
) -> None:
    """Обратная сторона: живая база ничего не отключает."""
    _, clone = _remote_with_clone(tmp_path, "develop")

    monkeypatch.setattr(
        "hub.services.orchestration.project_git_context",
        AsyncMock(return_value={"repo": str(clone), "base_branch": "develop"}),
    )
    monkeypatch.setattr(plugins, "git_ops", GitOpsIntegration())

    base = await review_evidence.resolve_diff_base(None, 947, "task-947/x")

    assert base["state"] == review_evidence.BASE_RESOLVED
    assert not review_evidence.base_blocks_diff(base["state"])
    assert review_evidence.diff_command_for(base, "task-947/x") == (
        "git diff develop...task-947/x"
    )


# ---------------------------------------------------------------------------
# AC-2 — релиз не оставляет проект без интеграционной ветки
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_branch_that_survived_the_release_is_left_alone(
    tmp_path: Path,
) -> None:
    _, clone = _remote_with_clone(tmp_path, "develop")
    _git(clone, "push", "-q", "--no-verify", "origin", "develop:main")

    state, detail = await GitOpsIntegration().ensure_remote_branch(
        "develop", "main", repo=str(clone)
    )

    assert state == "present", detail


@pytest.mark.asyncio
async def test_a_branch_the_release_removed_comes_back_from_the_release_branch(
    tmp_path: Path,
) -> None:
    remote, clone = _remote_with_clone(tmp_path, "develop")
    content_before = _sha(clone, "develop")
    _release_takes_the_branch(remote, clone, "develop")

    state, detail = await GitOpsIntegration().ensure_remote_branch(
        "develop", "main", repo=str(clone)
    )

    assert state == "restored", detail
    heads = _git(remote, "branch", "--list", "develop").stdout
    assert "develop" in heads, "ветка обязана снова существовать в remote"
    # Восстановлена от релизной ветки, а не от чего попало: содержимое то же,
    # что уехало релизом.
    assert _sha(remote, "develop") == _sha(remote, "main") == content_before


@pytest.mark.asyncio
async def test_a_workspace_less_project_is_not_told_its_branch_is_fine(
    tmp_path: Path,
) -> None:
    state, detail = await GitOpsIntegration().ensure_remote_branch(
        "develop", "main", repo=None
    )

    assert state == "unavailable"
    assert state not in ("present", "restored")
    assert detail


@pytest.mark.asyncio
async def test_the_release_records_the_rescue_where_a_human_will_see_it(
    db, monkeypatch
) -> None:
    """Восстановление попадает в ленту активности, а не только в лог сервера."""
    from hub import repository as hub_repo
    from hub.services.release import merge_ready_release
    from tests.test_release_policy import _git as _git_plugin
    from tests.test_release_policy import _release_project

    g = _git_plugin(existing_pr=777)
    g.ensure_remote_branch = AsyncMock(return_value=("restored", "a" * 40))
    pid = await _release_project(db, "auto")
    project = await hub_repo.get_project(db, pid)

    merged, reason = await merge_ready_release(db, project)

    assert merged is True
    assert "восстановлена" in reason
    g.ensure_remote_branch.assert_awaited()
    activity = [dict(r) for r in await hub_repo.list_activity(db, limit=10)]
    assert any("восстановлена" in (a.get("summary") or "") for a in activity), activity


@pytest.mark.asyncio
async def test_a_release_that_kept_its_branch_stays_quiet(db) -> None:
    """Обычный релиз ничего не восстанавливает и не пишет в ленту."""
    from hub import repository as hub_repo
    from hub.services.release import merge_ready_release
    from tests.test_release_policy import _git as _git_plugin
    from tests.test_release_policy import _release_project

    g = _git_plugin(existing_pr=777)
    g.ensure_remote_branch = AsyncMock(return_value=("present", "b" * 12))
    pid = await _release_project(db, "auto")
    project = await hub_repo.get_project(db, pid)

    merged, reason = await merge_ready_release(db, project)

    assert merged is True
    assert "восстановлена" not in reason
    activity = [dict(r) for r in await hub_repo.list_activity(db, limit=10)]
    assert not any("ветк" in (a.get("summary") or "") for a in activity), activity


@pytest.mark.asyncio
async def test_an_unverifiable_branch_is_not_reported_as_fine(db) -> None:
    """«Не смог проверить» после релиза остаётся неизвестностью, а не тишиной."""
    from hub import repository as hub_repo
    from hub.services.release import merge_ready_release
    from tests.test_release_policy import _git as _git_plugin
    from tests.test_release_policy import _release_project

    g = _git_plugin(existing_pr=777)
    g.ensure_remote_branch = AsyncMock(return_value=("unavailable", "ls-remote молчит"))
    pid = await _release_project(db, "auto")
    project = await hub_repo.get_project(db, pid)

    merged, reason = await merge_ready_release(db, project)

    assert merged is True
    assert "не проверено" in reason
    activity = [dict(r) for r in await hub_repo.list_activity(db, limit=10)]
    assert any("неизвестно" in (a.get("summary") or "") for a in activity), activity


# ---------------------------------------------------------------------------
# #949 — релиз не удаляет свою head-ветку, а восстановление проходит через хук
# ---------------------------------------------------------------------------


def _arm_the_hook(clone: Path, branch: str) -> None:
    """Вооружить в клоне НАСТОЯЩИЙ pre-push хук репозитория.

    Тесты #947 гоняли восстановление на голых репозиториях без хука — и
    пропустили в прод восстановление, которое хук блокирует (запись #4612).
    Клон без хука проверяет не тот мир: в workspace-клонах хаба хук вооружён
    всегда, это делает сам хаб (#532).
    """
    hooks = clone / ".githooks"
    hooks.mkdir(exist_ok=True)
    hook = hooks / "pre-push"
    repo_root = Path(__file__).resolve().parents[1]
    hook.write_text((repo_root / ".githooks" / "pre-push").read_text())
    hook.chmod(0o755)
    # Хук закоммичен, как в настоящем репозитории: иначе он сам лежит
    # untracked-файлом, и его собственная проверка чистого дерева блокирует
    # любой push — тест мерил бы артефакт фикстуры, а не поведение.
    _git(clone, "add", ".githooks/pre-push")
    _git(clone, "commit", "-qm", "chore: carry the pre-push hook")
    git_policy.activate_quietly(str(clone), base_branch=branch)
    assert git_policy.inspect(str(clone)).enforced, "хук обязан быть вооружён"


@pytest.mark.asyncio
async def test_the_restore_walks_through_an_armed_hook(tmp_path: Path) -> None:
    """AC-2, ровно прод: ветка выгружена в клоне, хук вооружён, remote её потерял."""
    remote, clone = _remote_with_clone(tmp_path, "develop")
    _arm_the_hook(clone, "develop")
    _release_takes_the_branch(remote, clone, "develop")

    state, detail = await GitOpsIntegration().ensure_remote_branch(
        "develop", "main", repo=str(clone)
    )

    assert state == "restored", detail
    assert "develop" in _git(remote, "branch", "--list", "develop").stdout
    assert _sha(remote, "develop") == _sha(remote, "main")


@pytest.mark.asyncio
async def test_the_restore_from_a_sideways_checkout_uses_a_real_branch_name(
    tmp_path: Path,
) -> None:
    """Клон стоит на другой ветке — восстановление идёт через branch -f, не через sha."""
    remote, clone = _remote_with_clone(tmp_path, "develop")
    _arm_the_hook(clone, "develop")
    _release_takes_the_branch(remote, clone, "develop")
    _git(clone, "checkout", "-q", "-b", "task-1/elsewhere")

    state, detail = await GitOpsIntegration().ensure_remote_branch(
        "develop", "main", repo=str(clone)
    )

    assert state == "restored", detail
    assert "develop" in _git(remote, "branch", "--list", "develop").stdout
    # Рабочее дерево не тронуто: клон так и стоит на своей ветке.
    assert (
        _git(clone, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        == "task-1/elsewhere"
    )


@pytest.mark.asyncio
async def test_a_dirty_checked_out_branch_is_refused_not_reset(
    tmp_path: Path,
) -> None:
    """Незакоммиченная работа дороже ветки: честный отказ вместо reset --hard."""
    remote, clone = _remote_with_clone(tmp_path, "develop")
    _arm_the_hook(clone, "develop")
    _release_takes_the_branch(remote, clone, "develop")
    # Правка ОТСЛЕЖИВАЕМОГО файла: только её reset --hard и уничтожил бы.
    # Untracked-файлы (в клонах хаба их полно) грязью не считаются — они
    # переживают reset, и отказ из-за них был бы ложным.
    (clone / "README").write_text("незакоммиченная правка")

    state, detail = await GitOpsIntegration().ensure_remote_branch(
        "develop", "main", repo=str(clone)
    )

    assert state == "unavailable"
    assert "грязное" in detail
    assert (clone / "README").read_text() == "незакоммиченная правка", (
        "дерево не тронуто"
    )
    assert "develop" not in _git(remote, "branch", "--list", "develop").stdout


@pytest.mark.asyncio
async def test_the_release_merge_keeps_its_head_branch(monkeypatch) -> None:
    """AC-1: релизный вызов мержа не несёт --delete-branch, task-вызов несёт."""
    # Точка подмены переехала вместе с вызовом gh: с #1113 его делает адаптер
    # форжа, а не git_ops. Проверяется ровно то же — какие аргументы уходят в
    # gh для task-PR и для релизного.
    from hub.integrations.forge import github as forge_mod

    calls: list[tuple[str, ...]] = []

    async def fake_gh(*args, **kwargs):
        calls.append(args)
        return (0, "", "")

    monkeypatch.setattr(forge_mod, "_gh", fake_gh)
    ops = GitOpsIntegration()

    assert await ops.merge_pr(7, 7, "some task")
    assert "--delete-branch" in calls[-1], "task-PR: ветка удаляется, как раньше"

    assert await ops.merge_pr(8, 0, "release develop → main", delete_branch=False)
    assert "--delete-branch" not in calls[-1], (
        "релизный PR: head — интеграционная ветка, удалять её нельзя"
    )


@pytest.mark.asyncio
async def test_the_release_flow_actually_passes_the_flag(db) -> None:
    """Не только возможность, но и использование: merge_ready_release шлёт False."""
    from hub import repository as hub_repo
    from hub.services.release import merge_ready_release
    from tests.test_release_policy import _git as _git_plugin
    from tests.test_release_policy import _release_project

    g = _git_plugin(existing_pr=777)
    g.ensure_remote_branch = AsyncMock(return_value=("present", "abc"))
    pid = await _release_project(db, "auto")
    project = await hub_repo.get_project(db, pid)

    merged, _ = await merge_ready_release(db, project)

    assert merged is True
    assert g.merge_pr.await_args.kwargs.get("delete_branch") is False, (
        "релизный путь обязан явно запретить удаление head-ветки"
    )
