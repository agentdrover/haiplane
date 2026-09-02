"""Адреса следуют форжу проекта, а ревью не зовётся туда, где его нет (#1119)."""

from __future__ import annotations

from pathlib import Path

import pytest

from hub.integrations import forge as forge_urls
from hub.integrations.dispatch import DispatchIntegration
from hub.services.review_dispatch import CLOUD_REVIEW_FORGES


def test_repo_url_follows_project_forge():
    """AC-1. Адрес репозитория берётся у форжа, а не у литерала."""
    assert (
        forge_urls.repo_url("github", "agentdrover/haiplane")
        == "https://github.com/agentdrover/haiplane"
    )
    assert (
        forge_urls.repo_url("gitverse", "mrpda/snip-portal")
        == "https://gitverse.ru/mrpda/snip-portal"
    )


def test_pr_path_differs_between_forges():
    """У GitHub /pull/, у GitVerse /pulls/ — во множественном числе.

    Снято с живого html_url (mrpda/snip-portal, 01.09.2026), а не выведено по
    аналогии. Одного лишнего символа хватает, чтобы ссылка в сообщении агенту
    выглядела рабочей и вела в никуда.
    """
    assert forge_urls.pr_url("github", "own/rep", 7).endswith("/pull/7")
    assert forge_urls.pr_url("gitverse", "own/rep", 7).endswith("/pulls/7")
    assert (
        forge_urls.pr_url("gitverse", "mrpda/snip-portal", 1)
        == "https://gitverse.ru/mrpda/snip-portal/pulls/1"
    )


def test_unknown_forge_falls_back_without_raising():
    """Незнакомый форж — github, как и у читателя (#1114), и без исключения.

    Строится ссылка, а не принимается решение: уронить сборку сообщения из-за
    строки в базе было бы хуже, чем дать ссылку по умолчанию.
    """
    assert forge_urls.repo_url("gitlab", "own/rep").startswith("https://github.com/")
    assert forge_urls.pr_url("", "own/rep", 1).startswith("https://github.com/")


def test_review_message_takes_a_ready_link_and_never_builds_one():
    """Сборщик текста не знает про форжи — ему дают готовый адрес.

    Раньше он собирал ссылку из литерала github.com и глобального REPO_NAME,
    то есть знал и хостинг, и репозиторий, и оба знал неверно для любого
    проекта, кроме одного.
    """
    dispatch = DispatchIntegration()

    with_link = dispatch.build_review_message(
        task_id=1,
        title="t",
        description="d",
        review_cycle=0,
        max_cycles=3,
        pr_number=7,
        pr_url="https://gitverse.ru/mrpda/snip-portal/pulls/7",
    )
    assert "https://gitverse.ru/mrpda/snip-portal/pulls/7" in with_link
    assert "github.com" not in with_link

    # Номер без адреса ссылки не рождает: выдумывать хостинг здесь нечем.
    without = dispatch.build_review_message(
        task_id=1,
        title="t",
        description="d",
        review_cycle=0,
        max_cycles=3,
        pr_number=7,
    )
    assert "github.com" not in without
    assert "PR:" not in without


def test_no_github_literal_survives_in_the_touched_modules():
    """AC-1, вторая половина: литерал не остался ни в одном из двух мест.

    Проверка по исходнику намеренно. Дефект здесь — не неверная строка, а
    НОВАЯ строка, написанная по-старому в модуле, который никто не
    перечитывал; тест на поведение её не увидит, пока кто-то не заведёт
    GitVerse-проект.
    """
    for path in (
        "hub/integrations/dispatch.py",
        "hub/services/review_dispatch.py",
    ):
        source = Path(path).read_text(encoding="utf-8")
        code = "\n".join(
            line for line in source.splitlines() if not line.lstrip().startswith("#")
        )
        assert "github.com" not in code, f"{path} снова собирает адрес литералом"


@pytest.mark.parametrize("forge", ["gitverse", "gitlab", ""])
def test_cloud_review_is_declared_github_only(forge):
    """AC-2. Список форжей ревьюера объявлен, а не выясняется попыткой.

    Измерено 31.08.2026: один и тот же запрос с адресом GitVerse даёт 400 и
    500, с адресом GitHub — 201. Причина в ответах Cursor не названа ни разу,
    а один известный вид ответа называет ЛОЖНУЮ («ветка не найдена»), поэтому
    отказ обязан приниматься до вызова.
    """
    assert CLOUD_REVIEW_FORGES == ("github",)
    assert forge not in CLOUD_REVIEW_FORGES


async def test_dispatch_policy_refused_for_unreachable_forge(client):
    """AC-3. Запрещённое состояние недостижимо ОБЕИМИ дорогами.

    Первая редакция закрывала одну: проверка стояла внутри ветки
    ``gate_policy`` и смотрела только на патч политики. PATCH, менявший ОДИН
    ``forge``, проходил мимо — проект с ``review=dispatch`` переключался на
    GitVerse и сохранял политику, исполнить которую больше нельзя (найдено
    ревью, отчёт #201).

    Это и есть разница между проверкой поля и инвариантом: поле проверяют там,
    где его пишут, а инвариант — везде, откуда в запрещённое состояние можно
    попасть.
    """
    human = {"Authorization": "Bearer human-token"}

    created = await client.post(
        "/api/projects",
        json={"slug": "forge-invariant", "name": "FI", "repo": "mrpda/snip-portal"},
        headers=human,
    )
    assert created.status_code == 200, created.text
    pid = created.json()["id"]

    # Дорога 1: политика на проекте, который УЖЕ на GitVerse.
    assert (
        await client.patch(
            f"/api/projects/{pid}", json={"forge": "gitverse"}, headers=human
        )
    ).status_code == 200
    resp = await client.patch(
        f"/api/projects/{pid}",
        json={"gate_policy": {"review": "dispatch"}},
        headers=human,
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["error"] == "cloud_review_forge_unsupported"

    # Дорога 2 — та, что была открыта: политика стоит, переключают форж.
    assert (
        await client.patch(
            f"/api/projects/{pid}", json={"forge": "github"}, headers=human
        )
    ).status_code == 200
    assert (
        await client.patch(
            f"/api/projects/{pid}",
            json={"gate_policy": {"review": "dispatch"}},
            headers=human,
        )
    ).status_code == 200
    resp = await client.patch(
        f"/api/projects/{pid}", json={"forge": "gitverse"}, headers=human
    )
    assert resp.status_code == 422, (
        "переключение форжа обязано отказать, пока политика просит облачного "
        "ревьюера: иначе в базе останется политика, которую нельзя исполнить"
    )
    assert resp.json()["detail"]["error"] == "cloud_review_forge_unsupported"

    # Дорога 3: оба поля одним вызовом — проскочить между проверками нечем.
    relax = await client.patch(
        f"/api/projects/{pid}",
        json={"gate_policy": {"review": "off"}},
        headers=human,
    )
    assert relax.status_code == 200, relax.text
    resp = await client.patch(
        f"/api/projects/{pid}",
        json={"forge": "gitverse", "gate_policy": {"review": "dispatch"}},
        headers=human,
    )
    assert resp.status_code == 422, resp.text
