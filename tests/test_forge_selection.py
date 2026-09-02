"""Форж из колонки становится действующим (#1146, эпик #1112).

До этой задачи форж был свойством ИНСТАНСА хаба: ``plugins.forge`` выбирался
один раз при старте приложения и обслуживал все проекты, а колонку
``projects.forge`` (#1114) не читал никто. Пять доставленных фич эпика —
протокол (#1113), клиент GitVerse (#1115), доставка (#1116), CI-проба (#1117),
провижининг (#1118) — были кодом, который никто не вызывает: смена форжа
меняла только текст в карточке проекта.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from hub import repository as repo
from hub.integrations import forge as forge_registry
from hub.integrations.git_ops import GitOpsIntegration
from hub.integrations.noop import NoopForge
from hub.services.orchestration import project_git_context


class _Recorder:
    """Адаптер, который только запоминает, что позвали ЕГО."""

    can_merge_via_api = True

    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[int] = []

    async def pr_state(self, pr_number, *, repo=None, gh_repo=None) -> str:
        self.calls.append(int(pr_number))
        return "open"


async def test_each_project_gets_its_own_adapter(monkeypatch):
    """AC-1. Проверяется, КТО получил вызов, а не совпали ли ответы.

    Совпадение ответов ничего бы не доказало: оба адаптера на такой вопрос
    отвечают строкой, и дефект «всё уходит в GitHub» выглядел бы зелёным.
    """
    configured = _Recorder("github")
    elsewhere = _Recorder("gitverse")
    monkeypatch.setattr(
        forge_registry, "client_for", lambda name: elsewhere, raising=True
    )
    ops = GitOpsIntegration(forge=configured)

    assert await ops.pr_state(11, forge="github") == "open"
    assert await ops.pr_state(22, forge="gitverse") == "open"

    assert configured.calls == [11], "github-проект обязан остаться у своего адаптера"
    assert elsewhere.calls == [22], (
        "gitverse-проект обязан уйти к адаптеру GitVerse; до #1146 оба вызова "
        "получал один адаптер, выбранный при старте процесса"
    )


async def test_the_forge_travels_from_the_project_to_the_call(db, client):
    """Вторая половина AC-1: значение колонки доезжает до вызова.

    Резолвер можно написать верно и не подать ему ничего — тогда все вызовы
    молча уйдут к настроенному адаптеру, и предыдущий тест этого не увидит.
    Здесь проверяется сам перенос: контекст задачи несёт площадку её проекта.
    """
    contexts = {}
    for slug, forge in (("on-github", "github"), ("on-gitverse", "gitverse")):
        project_id = await repo.create_project(db, slug=slug, name=slug)
        await repo.update_project(
            db,
            project_id,
            repo=f"owner/{slug}",
            workspace_path=f"/srv/{slug}",
            default_branch="main",
            forge=forge,
        )
        task_id = (await client.post("/api/tasks", json={"title": slug})).json()["id"]
        await db.execute(
            "UPDATE tasks SET project_id=? WHERE id=?", (project_id, task_id)
        )
        await db.commit()
        contexts[forge] = await project_git_context(db, task_id)

    assert contexts["github"]["forge"] == "github"
    assert contexts["gitverse"]["forge"] == "gitverse", (
        "площадка проекта обязана ехать вместе с repo и base_branch — она "
        "величина той же природы и того же времени жизни"
    )


async def test_github_projects_notice_nothing(monkeypatch):
    """AC-2. Настроенный адаптер отвечает за свой форж сам.

    Это не украшение, а условие сохранности подмены: тест, вложивший свой
    адаптер, обязан продолжать получать вызовы. Резолви хаб «github» через
    реестр — каждый такой тест получал бы свежий настоящий GitHubForge, и
    двадцать семь проверок гейта разом перестали бы проверять то, что
    проверяют.
    """
    configured = _Recorder("github")
    fresh = _Recorder("github-from-registry")
    monkeypatch.setattr(forge_registry, "client_for", lambda name: fresh)
    ops = GitOpsIntegration(forge=configured)

    await ops.pr_state(1, forge="github")
    await ops.pr_state(2, forge="")

    assert configured.calls == [1, 2]
    assert fresh.calls == [], "реестр не должен подменять настроенный адаптер"


async def test_unresolvable_project_falls_back_without_raising():
    """AC-3. Незнакомый форж — github, и ни один вызов не падает исключением.

    То же умолчание и по той же причине, что у единственного читателя (#1114):
    неизвестного форжа не бывает, бывает необъявленный. Уронить доставку из-за
    строки в базе было бы хуже вдвойне.
    """
    ops = GitOpsIntegration()
    for name in ("gitlab", "", "  ", "ГитВерс"):
        assert ops._forge_for(name).name in ("github", "gitverse")
    assert ops._forge_for("gitlab").name == "github"


async def test_an_unconfigured_hub_is_not_upgraded_by_the_resolver():
    """Ненастроенный хаб остаётся noop — резолв его не «дообустраивает».

    ``NoopForge`` означает «хостинга нет вовсе», и подставить туда GitHub
    значило бы включить интеграцию, которую никто не настраивал, заменив
    честное «спросить не удалось» содержательным ответом. Ровно тот дефект,
    который разбирали #419 и #725, только на новом шве.
    """
    ops = GitOpsIntegration(forge=NoopForge())
    for name in ("github", "gitverse", ""):
        assert ops._forge_for(name).name == "noop"


# --- сторож: ни одно место вызова не забыто ------------------------------

#: Методы ``git_ops``, которые обращаются к форжу. Вызов такого метода без
#: ``forge`` молча уходит к адаптеру, настроенному при старте.
_FORGE_REACHING = (
    "branch_ci_runs",
    "check_pr_ci",
    "check_pr_mergeable",
    "create_pr",
    "get_ci_failure_logs",
    "mark_pr_ready",
    "merge_commit_sha",
    "merge_pr",
    "merge_pr_with_detail",
    "open_release_pr",
    "pr_for_branch",
    "pr_is_draft",
    "pr_state",
    "release_range",
    "return_release_into_base",
)


def _calls_without_forge(source: str) -> list[str]:
    """Вызовы форж-методов, в которых не передан ``forge``."""
    missing = []
    pattern = re.compile(r"git_ops\.(" + "|".join(_FORGE_REACHING) + r")\(")
    for m in pattern.finditer(source):
        depth, i = 0, m.end() - 1
        while i < len(source):
            if source[i] == "(":
                depth += 1
            elif source[i] == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        call = source[m.start() : i + 1]
        if "forge=" not in call:
            missing.append(call.splitlines()[0])
    return missing


@pytest.mark.parametrize(
    "path",
    sorted(
        str(p)
        for p in Path("hub").rglob("*.py")
        if p.name not in {"git_ops.py", "noop.py", "protocols.py"}
    ),
)
def test_every_forge_reaching_call_names_its_forge(path):
    """Проверка по ИСХОДНИКУ, и это осознанный выбор приёма.

    Дефект здесь — не неверная строка, а НОВАЯ строка, написанная по-старому:
    кто-то добавит вызов ``check_pr_ci`` без ``forge``, и тот молча уйдёт к
    адаптеру, настроенному при старте. Для github-проектов это совпадёт с
    правильным ответом, поэтому ни один тест поведения не покраснеет — пока
    кто-нибудь не заведёт GitVerse-проект и не потеряет на нём доставку.

    Двадцать девять мест вызова я правил вручную; пропустить одно было легко,
    и цена пропуска — тихий неверный адаптер, ровно тот класс отказа, ради
    которого написан весь эпик.
    """
    missing = _calls_without_forge(Path(path).read_text(encoding="utf-8"))
    assert not missing, (
        f"{path}: вызов форжа без явного forge=: {missing}. Такой вызов уйдёт к "
        f"адаптеру, выбранному при старте процесса, а не к форжу проекта."
    )
