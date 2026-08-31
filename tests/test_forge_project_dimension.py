"""Форж как объявленное измерение проекта (#1114, эпик #1112)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from hub import repository as repo
from hub import services
from hub.models import DEFAULT_FORGE, FORGES, ProjectCreate, ProjectPatch, TaskCreate
from hub.services import project_policy


async def test_migration_defaults_existing_projects_to_github(db):
    """AC-1. Миграция описывает то, что уже есть, и ничего не меняет.

    Проекты, заведённые до #1114, все на GitHub, и колонка обязана сказать
    это за них. Значение, которое кто-то должен проставить задним числом, —
    это не миграция, а незаполненное поле, о котором узнают на первой
    доставке.
    """
    # Строка, вставленная БЕЗ упоминания forge — ровно как её вставили бы до
    # появления колонки.
    await db.execute(
        "INSERT INTO projects (slug, name, repo, default_branch) "
        "VALUES ('legacy', 'Legacy', 'agentdrover/haiplane', 'develop')"
    )
    await db.commit()

    row = await repo.get_project_by_slug(db, "legacy")

    assert row["forge"] == "github"
    assert project_policy.forge_of(row) == "github"


async def test_creating_a_project_without_saying_forge_puts_it_on_github(db):
    """Не назвал форж — проект на github, и это записано, а не подразумевается."""
    pid = await repo.create_project(db, slug="quiet", name="Quiet")
    await db.commit()

    row = await repo.get_project(db, pid)

    assert row["forge"] == "github"


def test_unknown_forge_is_refused_by_name():
    """AC-2. Незнакомый форж отказывается с перечислением допустимых.

    Правило то же, которым ``default_branch_policy`` ловит опечатку
    ``releaseBase`` (#886): принять и проигнорировать — значит оставить
    владельца смотреть на своё значение и верить, что оно применилось.
    Отказ случается в момент записи, пока тот, кто ошибся, ещё здесь.
    """
    with pytest.raises(ValidationError) as created:
        ProjectCreate(slug="p", name="P", forge="gitlab")
    assert "gitlab" in str(created.value)
    assert "github" in str(created.value) and "gitverse" in str(created.value)

    with pytest.raises(ValidationError) as patched:
        ProjectPatch(forge="gitlab")
    assert "allowed" in str(patched.value)


def test_known_forges_are_accepted_in_any_case():
    """Регистр и пробелы — не повод отказать, это та же строка."""
    assert ProjectCreate(slug="p", name="P", forge="GitVerse").forge == "gitverse"
    assert ProjectPatch(forge=" github ").forge == "github"


def test_forge_defaults_without_being_asked():
    """Умолчание объявлено, а не подразумевается вызывающим."""
    assert ProjectCreate(slug="p", name="P").forge == DEFAULT_FORGE
    assert DEFAULT_FORGE in FORGES
    # None у патча — «не прислали», а не «сбросить в умолчание».
    assert ProjectPatch().forge is None


def test_forge_reader_never_raises_and_never_invents():
    """Читатель терпит всё, что может прийти, и не выдумывает третий форж.

    Незнакомое значение сводится к github НЕ вместо валидации на записи, а
    вслед за ней: чтение случается в гейте, где отказать некому, и подставить
    туда несуществующий адаптер значит уронить доставку из-за строки в базе.
    """
    assert project_policy.forge_of(None) == DEFAULT_FORGE
    assert project_policy.forge_of({"forge": ""}) == DEFAULT_FORGE
    assert project_policy.forge_of({"forge": "gitlab"}) == DEFAULT_FORGE
    assert project_policy.forge_of({}) == DEFAULT_FORGE
    assert project_policy.forge_of({"forge": "GitVerse"}) == "gitverse"


async def test_forge_for_task_walks_up_to_the_project(db):
    """Задача не несёт форж — его несёт проект, и читатель туда доходит."""
    pid = await repo.create_project(
        db,
        slug="on-gitverse",
        name="On GitVerse",
        repo_name="mrpda/hub",
        default_branch="master",
        forge="gitverse",
    )
    await db.commit()
    epic = await services.create_task(db, TaskCreate(title="Эпик", task_type="epic"))
    await repo.update_task(db, epic.id, project_id=pid)
    feature = await services.create_task(
        db, TaskCreate(title="Фича", task_type="feature", parent_id=epic.id)
    )
    await db.commit()

    assert await project_policy.forge_for_task(db, feature.id) == "gitverse"
    # Задача вне проекта не остаётся без ответа: умолчание, а не исключение.
    orphan = await services.create_task(db, TaskCreate(title="Сирота"))
    await db.commit()
    assert await project_policy.forge_for_task(db, orphan.id) == DEFAULT_FORGE


async def test_api_reports_and_accepts_the_forge(client, db):
    """AC-3. Форж виден в API и меняется через него же."""
    created = await client.post(
        "/api/projects",
        json={"slug": "gv-proj", "name": "GV", "forge": "gitverse"},
    )
    assert created.status_code == 200, created.text
    assert created.json()["forge"] == "gitverse"

    quiet = await client.post("/api/projects", json={"slug": "gh-proj", "name": "GH"})
    assert quiet.status_code == 200, quiet.text

    listed = await client.get("/api/projects")
    by_slug = {p["slug"]: p for p in listed.json()}
    assert by_slug["gv-proj"]["forge"] == "gitverse"
    assert by_slug["gh-proj"]["forge"] == "github", "не назвали — значит github"

    pid = created.json()["id"]
    patched = await client.patch(f"/api/projects/{pid}", json={"forge": "github"})
    assert patched.status_code == 200, patched.text
    assert patched.json()["forge"] == "github"

    refused = await client.patch(f"/api/projects/{pid}", json={"forge": "gitlab"})
    assert refused.status_code == 422
