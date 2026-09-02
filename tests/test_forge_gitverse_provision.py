"""Провижининг уважает объявленный форж (#1118, эпик #1112).

Дефект, ради которого задача заведена, подтверждён не рассуждением, а живым
провижинингом 01.09.2026: проект #8 (snip-portal) переключили на
``forge=gitverse``, провижининг отчитался ``provision_status=ok``,
``clone_branch=match`` — а клон на диске смотрел на
``https://github.com/mrpda/snip-portal.git``. Содержимое совпало лишь потому,
что репозитории пока зеркалят друг друга.

Дыр в шве ДВЕ, и вторая переживает исправление первой: адрес клонирования не
знал форжа, а сверка уже существующего клона сравнивала только ``owner/name`` и
не смотрела на ХОСТ. Почини одно — и уже созданный чужой клон останется принят
навсегда, потому что каталог на месте и новый клон не создаётся.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from hub import brand, config
from hub import repository as repo
from hub import services
from hub.integrations import forge as forge_urls
from hub.integrations.forge.gitverse import GitVerseForge, GitVerseResponse
from hub.integrations.git_ops import GitOpsIntegration
from hub.integrations.registry import plugins
from hub.services import workflow_seed

# --------------------------------------------------------------------------
# АДРЕС КЛОНА
# --------------------------------------------------------------------------


def _ls_remote_targets(calls: list[str]) -> list[str]:
    return [c for c in calls if "ls-remote" in c]


@pytest.mark.parametrize(
    ("forge", "host"),
    [("github", "github.com"), ("gitverse", "gitverse.ru")],
)
async def test_clone_address_follows_the_declared_forge(tmp_path, forge, host):
    """Кандидаты собираются от форжа, а не от литерала github.com.

    Github здесь не «заодно», а контроль: он проверяет, что прежнее поведение
    сохранено до буквы — иначе тест на gitverse доказывал бы лишь, что что-то
    поменялось.
    """
    calls: list[str] = []

    async def fake_run(*cmd, **kw):
        calls.append(" ".join(cmd))
        return (0, "", "")

    with patch("hub.integrations.proc.run", side_effect=fake_run):
        ok, detail = await GitOpsIntegration().clone_repo(
            "mrpda/snip-portal", str(tmp_path / "ws"), "main", forge=forge
        )

    assert ok is True
    probed = _ls_remote_targets(calls)
    assert probed, "провижининг обязан проверить достижимость до клонирования"
    assert f"https://{host}/mrpda/snip-portal.git" in probed[0]
    clone_call = next(c for c in calls if " clone " in f" {c} ")
    assert host in clone_call
    # #1118: по строке видно, откуда взялся репозиторий. До задачи деталь
    # «cloned mrpda/snip-portal (main, https)» одинаково описывала клон с
    # любой площадки.
    assert forge in detail


async def test_ssh_fallback_also_follows_the_forge(tmp_path):
    """Запасной ssh-путь тоже обязан вести на свой хост, а не на github."""
    seen: list[str] = []

    async def fake_run(*cmd, **kw):
        joined = " ".join(cmd)
        seen.append(joined)
        if "ls-remote" in joined and "https://" in joined:
            return (128, "", "fatal: could not read Username")
        return (0, "", "")

    with patch("hub.integrations.proc.run", side_effect=fake_run):
        ok, detail = await GitOpsIntegration().clone_repo(
            "mrpda/snip-portal", str(tmp_path / "ws"), "main", forge="gitverse"
        )

    assert ok is True and "ssh" in detail
    assert any("git@gitverse.ru:mrpda/snip-portal.git" in c for c in seen)
    assert all("github.com" not in c for c in seen), (
        "ни один вызов не должен уходить на github для gitverse-проекта"
    )


async def test_explicit_url_still_wins_over_the_forge(tmp_path):
    """Полный адрес в repo — по-прежнему закон: форж его не переписывает."""
    calls: list[str] = []

    async def fake_run(*cmd, **kw):
        calls.append(" ".join(cmd))
        return (0, "", "")

    with patch("hub.integrations.proc.run", side_effect=fake_run):
        ok, _ = await GitOpsIntegration().clone_repo(
            "git@gitlab.local:team/x.git", str(tmp_path / "ws"), forge="gitverse"
        )

    assert ok is True
    assert all("gitverse.ru" not in c for c in calls)


# --------------------------------------------------------------------------
# СВЕРКА СУЩЕСТВУЮЩЕГО КЛОНА — вторая дыра
# --------------------------------------------------------------------------


def _clone_with_origin(path: Path, origin: str) -> None:
    """Настоящий клон на диске: сверка идёт по каталогу .git, а не по флагу."""
    path.mkdir(parents=True)
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(path)], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(path), "remote", "add", "origin", origin],
        check=True,
        capture_output=True,
    )


def _runner(origin: str, *, record: list[str] | None = None):
    """Фейк ``proc.run``, честно отвечающий на чтение origin.

    Без этого ``git remote get-url`` возвращал бы пустую строку и сверка
    происхождения проверялась бы на пустоте — то есть не проверялась бы вовсе.
    """

    async def fake_run(*cmd, **kw):
        joined = " ".join(cmd)
        if record is not None:
            record.append(joined)
        if "remote" in joined and "get-url" in joined:
            return (0, origin, "")
        return (0, "", "")

    return fake_run


async def test_clone_from_another_forge_is_never_healthy(tmp_path):
    """AC-4. Расхождение ХОСТА названо, и статус не читается как здоровый.

    Воспроизводит состояние проекта #8 на проде: forge=gitverse, а origin
    смотрит на github. До #1118 сверка сравнивала только owner/name, и
    «mrpda/snip-portal» содержится в github-адресе ровно так же, как в
    gitverse-адресе — поэтому чужой клон проходил как годный и прошёл бы даже
    после того, как клонирование научили форжу.
    """
    ws = tmp_path / "snip-portal"
    origin = "https://github.com/mrpda/snip-portal.git"
    _clone_with_origin(ws, origin)
    seen: list[str] = []

    with patch("hub.integrations.proc.run", side_effect=_runner(origin, record=seen)):
        ok, detail = await GitOpsIntegration().clone_repo(
            "mrpda/snip-portal", str(ws), "main", forge="gitverse"
        )

    assert ok is False, "клон с чужой площадки не может быть «ok»"
    assert not any("fetch" in c for c in seen), (
        "до чужого клона дело доходить не должно: отказ обязан случиться на "
        "сверке происхождения, а не после успешного fetch"
    )
    assert "github" in detail and "gitverse" in detail, (
        "деталь обязана назвать ОБЕ стороны расхождения: без них строка не "
        "отличается от любого другого отказа доступа"
    )


async def test_a_clone_from_the_declared_forge_is_accepted(tmp_path):
    """Контроль: свой клон проходит сверку и дело доходит до fetch.

    Без этой пары предыдущий тест доказывал бы лишь, что сверка отказывает
    всегда.
    """
    ws = tmp_path / "snip-portal"
    origin = "git@gitverse.ru:mrpda/snip-portal.git"
    _clone_with_origin(ws, origin)
    seen: list[str] = []

    with patch("hub.integrations.proc.run", side_effect=_runner(origin, record=seen)):
        ok, detail = await GitOpsIntegration().clone_repo(
            "mrpda/snip-portal", str(ws), "main", forge="gitverse"
        )

    assert ok is True, detail
    assert any("fetch" in c for c in seen), (
        "свой клон обязан быть дофетчен, а не отвергнут"
    )


async def test_an_unknown_host_is_not_accused(tmp_path):
    """«Не смог узнать» ≠ «чужой» (#725).

    Самоподнятый git, зеркало на своём домене и локальный путь в тесте не
    принадлежат ни одному объявленному форжу. Отказать им значило бы сломать
    каждый стенд, который клонирует из временного каталога, и объявить чужим
    то, о чём мы просто ничего не знаем.
    """
    ws = tmp_path / "local"
    origin = str(tmp_path / "bare" / "mrpda" / "snip-portal.git")
    _clone_with_origin(ws, origin)

    with patch("hub.integrations.proc.run", side_effect=_runner(origin)):
        ok, detail = await GitOpsIntegration().clone_repo(
            "mrpda/snip-portal", str(ws), "main", forge="gitverse"
        )

    assert ok is True, detail


# --------------------------------------------------------------------------
# ИМЕНОВАННЫЕ ПРИЧИНЫ ОТКАЗА
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stderr", "cause"),
    [
        ("git@gitverse.ru: Permission denied (publickey).", "no_deploy_key"),
        ("Host key verification failed.", "host_key_unpinned"),
        ("fatal: could not read Username for 'https://gitverse.ru'", "no_git_creds"),
        ("remote: Repository not found.", "repo_not_found_or_no_access"),
        ("ssh: Could not resolve hostname gitverse.ru", "host_unreachable"),
        ("fatal: something nobody has ever written down", "cause_unnamed"),
    ],
)
async def test_provision_failures_name_their_cause(tmp_path, stderr, cause):
    """AC-3. Каждый случай даёт СВОЮ причину, а не общее «не удалось клонировать».

    Различие не косметическое: «ключа нет», «ключ есть, хост не пинован»,
    «репозитория не видно» и «до хоста не дошли» — четыре разные руки. Общая
    строка отправляет человека проверять всё подряд, начиная обычно не с того.

    Последний случай проверяет честность: причину, которой мы не знаем, нельзя
    называть придуманным именем — но и молчать о ней нельзя, поэтому текст git
    остаётся в детали рядом.
    """

    async def fake_run(*cmd, **kw):
        if "ls-remote" in " ".join(cmd):
            return (128, "", stderr)
        return (0, "", "")

    with patch("hub.integrations.proc.run", side_effect=fake_run):
        ok, detail = await GitOpsIntegration().clone_repo(
            "mrpda/snip-portal", str(tmp_path / "ws"), "main", forge="gitverse"
        )

    assert ok is False
    assert cause.replace("no_git_creds", "no_git_credentials") in detail, detail
    assert stderr[:30] in detail, "текст git обязан остаться рядом с именем причины"
    assert "gitverse" in detail, "отказ называет, КАКОЙ форж отказал"


# --------------------------------------------------------------------------
# ПОСЕВ WORKFLOW
# --------------------------------------------------------------------------


def _repo_on(tmp_path: Path, branch: str = "main") -> Path:
    work = tmp_path / "work"
    work.mkdir()
    for args in (
        ("init", "-q", "-b", branch),
        ("config", "user.name", "Tester"),
        ("config", "user.email", "tester@example.com"),
    ):
        subprocess.run(["git", "-C", str(work), *args], check=True, capture_output=True)
    (work / "README.md").write_text("x\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(work), "add", "README.md"], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(work), "commit", "-q", "-m", "initial"],
        check=True,
        capture_output=True,
    )
    # Настоящий origin, а не заглушка: посев пушит, и без него тест проверял бы
    # отказ пуша вместо каталога, куда файлы легли.
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", branch, str(origin)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(work), "remote", "add", "origin", str(origin)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(work), "push", "-q", "-u", "origin", branch],
        check=True,
        capture_output=True,
    )
    return work


@pytest.mark.parametrize(
    ("forge", "directory"),
    [("github", ".github/workflows"), ("gitverse", ".gitverse/workflows")],
)
def test_seed_lands_in_the_forge_own_directory(tmp_path, forge, directory):
    """Хаб сеет в РОДНОЙ каталог форжа, и это измерение, а не вкус.

    Измерено 02.09.2026 по истории mrpda/snip-portal, без единого пуша:
    в коммите d02d83c8 каталога ``.gitverse/workflows`` не было — посеянный
    ``haiplane-ci.yml`` прогнался и был зелёным; в коммите af8aaac6 каталог
    появился, и с этого момента прогоняется ТОЛЬКО он, а гитхабовский каталог
    не читается вовсе. То есть ``.github/workflows`` на GitVerse — запасной
    путь, который гасится чужим коммитом молча.

    Шаблон при этом один на оба форжа: синтаксис Actions раннер GitVerse
    понимает, и это тоже измерено, а не выведено (прогон 1442906, success).
    """
    work = _repo_on(tmp_path)

    result = workflow_seed.seed_project_workflows(
        str(work),
        base_branch="main",
        release_branch="main",
        push=False,
        forge=forge,
    )

    assert result.written, result.detail
    written_dir = work / directory
    assert written_dir.is_dir(), f"посев не создал {directory}: {result.detail}"
    assert sorted(p.name for p in written_dir.iterdir()) == sorted(result.written)
    other = ".gitverse/workflows" if forge == "github" else ".github/workflows"
    assert not (work / other).exists(), (
        f"файлы легли и в {other} тоже — хаб владеет одним каталогом, не двумя"
    )
    # Каталог назван в детали: на GitVerse от него зависит, прогонится файл
    # или тихо пролежит мёртвым.
    assert directory.replace("/", "/") in result.detail.replace("\\", "/")


def test_native_gitverse_ci_is_recognised_as_foreign(tmp_path):
    """Репозиторий со своим CI в ``.gitverse/workflows`` не засевается.

    Это и есть дефект, случившийся на проде. Правило посева — «писать только в
    репозиторий, у которого своего CI нет» — проверялось взглядом в
    ``.github/workflows``. У snip-portal свой CI лежит в ``.gitverse/workflows``,
    хаб туда не смотрел, увидел пустоту и посеял в репозиторий, у которого CI
    БЫЛ. Хуже: с момента, когда нативный каталог приехал в main, посеянные
    файлы стали мёртвыми, продолжая выглядеть рабочими.
    """
    work = _repo_on(tmp_path)
    native = work / ".gitverse" / "workflows"
    native.mkdir(parents=True)
    (native / "ci.yaml").write_text("name: theirs\non: [push]\n", encoding="utf-8")

    result = workflow_seed.seed_project_workflows(
        str(work),
        base_branch="main",
        release_branch="main",
        push=False,
        forge="gitverse",
    )

    assert not result.written, "хаб не должен дописывать CI к чужому пайплайну"
    assert "already carries workflows" in result.detail
    assert ".gitverse/workflows/ci.yaml" in result.detail.replace("\\", "/"), (
        "деталь обязана назвать ПУТЬ, а не имя: ci.yaml в двух каталогах — "
        "разные ответы на вопрос, чей это CI, и по имени они неразличимы"
    )
    assert not (work / ".github").exists()


def test_foreign_ci_is_seen_across_forges(tmp_path):
    """Сканируются оба каталога независимо от форжа проекта.

    Иначе правило проверяет не то, что заявляет: github-проект, чей CI кто-то
    положил в ``.gitverse/workflows``, снова получил бы вторую пару файлов.
    """
    work = _repo_on(tmp_path)
    native = work / ".gitverse" / "workflows"
    native.mkdir(parents=True)
    (native / "ci.yaml").write_text("name: theirs\non: [push]\n", encoding="utf-8")

    assert workflow_seed.existing_workflows(str(work)) == [
        str(Path(".gitverse") / "workflows" / "ci.yaml")
    ]


# --------------------------------------------------------------------------
# ДВА CREDENTIAL'А, И ПРОВИЖИНИНГ НАЗЫВАЕТ, КАКОГО НЕ ХВАТАЕТ
# --------------------------------------------------------------------------


async def _gitverse_project(db, tmp_path) -> int:
    project_id = await repo.create_project(db, slug="snip-portal", name="Snip")
    await repo.update_project(
        db,
        project_id,
        repo="mrpda/snip-portal",
        workspace_path=str(tmp_path / "ws"),
        default_branch="main",
        forge="gitverse",
    )
    await db.commit()
    return project_id


async def test_provision_refuses_without_the_api_token(db, tmp_path, monkeypatch):
    """AC-3, первый случай: токена нет — сказано, какого именно.

    Отказ ДО клонирования намеренно. Репозиторий прекрасно склонировался бы по
    deploy key и без токена, провижининг отчитался бы ok — а гейт потом не
    открыл бы PR и не прочёл бы CI, и выяснилось бы это на первой же задаче,
    когда работа уже сделана.
    """
    monkeypatch.setattr(config, "GITVERSE_TOKEN", "")
    project_id = await _gitverse_project(db, tmp_path)
    cloned = AsyncMock(return_value=(True, "cloned"))
    monkeypatch.setattr(plugins.git_ops, "clone_repo", cloned)

    result = await services.provision_project(db, project_id)

    assert result["provision_status"] == "error"
    assert "gitverse_token_missing" in result["provision_detail"]
    assert f"{brand.ENV_PREFIX}GITVERSE_TOKEN" in result["provision_detail"], (
        "названо должно быть имя настройки, а не «нет доступа»"
    )
    cloned.assert_not_awaited()


@pytest.mark.parametrize(
    ("status", "cause"),
    [
        (401, "gitverse_token_invalid"),
        (403, "gitverse_token_lacks_rights"),
        (404, "gitverse_repo_not_found_or_no_access"),
    ],
)
async def test_provision_names_which_api_answer_refused(
    db, tmp_path, monkeypatch, status, cause
):
    """AC-3, остальные случаи API: три ответа — три разные руки.

    401 чинит владелец токена, 403 — владелец репозитория, 404 может означать и
    опечатку в ``repo``. Свести их в одно «нет доступа» значит отправить
    человека проверять всё подряд, начиная обычно не с того.
    """
    monkeypatch.setattr(config, "GITVERSE_TOKEN", "t" * 20)
    project_id = await _gitverse_project(db, tmp_path)

    class _Refusing(GitVerseForge):
        async def _request(self, method, path, **kw):  # type: ignore[override]
            return GitVerseResponse(status)

    monkeypatch.setattr(forge_urls, "client_for", lambda forge: _Refusing())
    monkeypatch.setattr(
        plugins.git_ops, "clone_repo", AsyncMock(return_value=(True, "cloned"))
    )

    result = await services.provision_project(db, project_id)

    assert result["provision_status"] == "error"
    assert result["provision_detail"] == cause


async def test_an_unreachable_api_is_not_read_as_no_access(db, tmp_path, monkeypatch):
    """«До сервера не дошли» ≠ «доступа нет» (#725).

    Разница практическая: первое лечится ожиданием, второе — правкой прав.
    Сведи их — и человек пойдёт выписывать новый токен из-за упавшей сети.
    """
    monkeypatch.setattr(config, "GITVERSE_TOKEN", "t" * 20)
    project_id = await _gitverse_project(db, tmp_path)

    class _Silent(GitVerseForge):
        async def _request(self, method, path, **kw):  # type: ignore[override]
            return GitVerseResponse(None, reason="timeout")

    monkeypatch.setattr(forge_urls, "client_for", lambda forge: _Silent())
    monkeypatch.setattr(
        plugins.git_ops, "clone_repo", AsyncMock(return_value=(True, "cloned"))
    )

    result = await services.provision_project(db, project_id)

    assert "unreachable" in result["provision_detail"]
    assert "token" not in result["provision_detail"], (
        "молчание сети не должно называться проблемой с токеном"
    )


async def test_github_projects_are_not_asked_for_an_api_credential(
    db, tmp_path, monkeypatch
):
    """Контроль: поведение github-проектов не меняется.

    Авторизация ``gh`` — предположение уровня хоста, которое хаб делает в
    каждом гейтовом вызове. Сделать провижининг единственным местом, которое её
    стережёт, значило бы поменять поведение всех существующих проектов ради
    задачи, которая обязана его сохранить.
    """
    monkeypatch.setattr(config, "GITVERSE_TOKEN", "")
    project_id = await repo.create_project(db, slug="gh-proj", name="GH")
    await repo.update_project(
        db,
        project_id,
        repo="mrPDA/gh-proj",
        workspace_path=str(tmp_path / "ws"),
        default_branch="main",
    )
    await db.commit()

    def _explode(forge):  # pragma: no cover - вызов означал бы провал теста
        raise AssertionError("github-проект не должен спрашивать API на провижининге")

    monkeypatch.setattr(forge_urls, "client_for", _explode)
    monkeypatch.setattr(
        plugins.git_ops, "clone_repo", AsyncMock(return_value=(True, "cloned"))
    )

    result = await services.provision_project(db, project_id)

    assert result["provision_status"] == "ok"


async def test_provisioning_carries_the_forge_all_the_way_down(
    db, tmp_path, monkeypatch
):
    """AC-1, машинная половина: форж доезжает и до клона, и до посева.

    Живую половину — настоящий клон с gitverse.ru — этот тест не изображает и
    изображать не должен: подделанный remote доказывал бы только то, что
    подделка работает. Он проверяет ровно то, что было сломано, — что значение
    колонки ВООБЩЕ куда-то едет. До #1118 не ехало никуда.
    """
    monkeypatch.setattr(config, "GITVERSE_TOKEN", "t" * 20)
    work = _repo_on(tmp_path)
    project_id = await repo.create_project(db, slug="snip", name="Snip")
    await repo.update_project(
        db,
        project_id,
        repo="mrpda/snip-portal",
        workspace_path=str(work),
        default_branch="main",
        forge="gitverse",
    )
    await db.commit()

    class _Allowing(GitVerseForge):
        async def _request(self, method, path, **kw):  # type: ignore[override]
            return GitVerseResponse(200, data={"name": "snip-portal"})

    monkeypatch.setattr(forge_urls, "client_for", lambda forge: _Allowing())
    cloned = AsyncMock(return_value=(True, "cloned mrpda/snip-portal from gitverse"))
    monkeypatch.setattr(plugins.git_ops, "clone_repo", cloned)

    result = await services.provision_project(db, project_id)

    assert result["provision_status"] == "ok", result["provision_detail"]
    assert cloned.await_args.kwargs.get("forge") == "gitverse", (
        "клонирование обязано получить объявленный форж, иначе адрес снова "
        "склеится из github.com"
    )
    assert "gitverse" in result["provision_detail"], (
        "по строке должно быть видно, откуда взялся репозиторий"
    )
    seeded = work / ".gitverse" / "workflows"
    assert seeded.is_dir(), (
        f"посев ушёл не в родной каталог форжа: {result['provision_detail']}"
    )
    assert not (work / ".github").exists()
