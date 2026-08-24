"""Демо-данные для холодного старта Haiplane Hub (#944).

Наполняет пустой хаб небольшим демонстрационным набором — проект ``demo``,
эпик, фича и задачи во всех ключевых статусах, — чтобы после
``docker compose up`` дашборд показывал живой процесс, а не пустую доску.

Два правила:

* **Opt-in.** Без ``HAIPLANE_DEMO_SEED=1`` функция не пишет ничего и
  возвращает ``False`` — случайный запуск на настоящей базе безвреден.
* **Идемпотентность.** Маркер — slug проекта ``demo``: если он уже есть,
  повторный запуск ничего не создаёт.

Запуск как скрипта: ``python scripts/demo_seed.py`` — открывает БД по
``HAIPLANE_HUB_DB`` тем же путём, что и сам хаб.
"""

from __future__ import annotations

import asyncio
import os

import aiosqlite

from hub import repository
from hub.db import serialize_str_list
from hub.models import AcceptanceCriterion, ACVerifiableBy

_FLAG = "HAIPLANE_DEMO_SEED"
_PROJECT_SLUG = "demo"


async def _create_demo_task(
    db: aiosqlite.Connection,
    *,
    title: str,
    description: str,
    status: str,
    task_type: str,
    parent_id: int | None,
    project_id: int,
    position: int = 0,
) -> int:
    """Одна демо-запись в tasks с привязкой к проекту demo.

    Финальный статус проставляется через ``update_task``, а не в INSERT:
    так переход стампует ``status_entered_at``/``completed_at`` тем же
    кодом, что и живой хаб, и демо-строки не отличаются формой от настоящих.
    """
    task_id = await repository.create_task(
        db,
        title=title,
        description=description,
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status="draft" if status == "draft" else "open",
        auto_review=True,
        task_type=task_type,
        parent_id=parent_id,
        priority="medium",
        position=position,
    )
    await repository.update_task(db, task_id, project_id=project_id)
    if status not in ("draft", "open"):
        await repository.update_task(db, task_id, status=status)
    return task_id


async def seed_demo(db: aiosqlite.Connection, *, enabled: bool | None = None) -> bool:
    """Засеять демо-набор. Возвращает True, только если что-то создано."""
    if enabled is None:
        enabled = os.environ.get(_FLAG, "") == "1"
    if not enabled:
        return False
    if await repository.get_project_by_slug(db, _PROJECT_SLUG) is not None:
        return False

    project_id = await repository.create_project(
        db,
        slug=_PROJECT_SLUG,
        name="Demo",
        repo_name="",
        workspace_path="",
    )

    epic_id = await _create_demo_task(
        db,
        title="Первые шаги пользователя в продукте",
        description="Демонстрационный эпик: путь нового пользователя "
        "от регистрации до первой ценности.",
        status="open",
        task_type="epic",
        parent_id=None,
        project_id=project_id,
    )
    feature_id = await _create_demo_task(
        db,
        title="Онбординг новых пользователей",
        description="Демонстрационная фича внутри эпика: письма, подсказки "
        "и первые экраны.",
        status="open",
        task_type="feature",
        parent_id=epic_id,
        project_id=project_id,
    )

    # Драфт с заполненным DoR: форма feature-профиля целиком плюс один AC.
    draft_id = await _create_demo_task(
        db,
        title="Онбординг: приветственное письмо",
        description="Отправлять новое приветственное письмо в течение часа "
        "после регистрации.",
        status="draft",
        task_type="task",
        parent_id=feature_id,
        project_id=project_id,
        position=0,
    )
    await repository.update_task(
        db,
        draft_id,
        work_type="feature",
        size="S",
        wip_tag="feature_work",
        user_story="Как новый пользователь, я хочу получить приветственное "
        "письмо, чтобы понять первые шаги в продукте.",
        problem_statement="Новые пользователи не получают никакого письма "
        "после регистрации и теряются на первом экране.",
        business_value="Ожидаем рост доли пользователей, дошедших до первого "
        "действия, за счёт понятного старта.",
        scope_in=serialize_str_list(
            ["Шаблон письма", "Триггер отправки после регистрации"]
        ),
        scope_out=serialize_str_list(["Рассылки повторной активации"]),
        validation_commands=serialize_str_list(["pytest tests/ -q"]),
    )
    await repository.add_acceptance_criterion(
        db,
        draft_id,
        AcceptanceCriterion(
            id="AC-1",
            given="новый пользователь завершил регистрацию",
            when="проходит не более часа",
            then="на его адрес отправлено приветственное письмо",
            verifiable_by=ACVerifiableBy.test,
        ),
    )

    await _create_demo_task(
        db,
        title="Поиск: опечатки в запросах",
        description="Показывать подсказку «возможно, вы имели в виду…» при "
        "опечатке в поисковом запросе.",
        status="open",
        task_type="task",
        parent_id=feature_id,
        project_id=project_id,
        position=1,
    )
    await _create_demo_task(
        db,
        title="Профиль: загрузка аватара",
        description="Дать пользователю загрузить аватар на странице профиля.",
        status="running",
        task_type="task",
        parent_id=feature_id,
        project_id=project_id,
        position=2,
    )
    await _create_demo_task(
        db,
        title="Уведомления: недельный дайджест",
        description="Раз в неделю присылать сводку активности по проекту.",
        status="review",
        task_type="task",
        parent_id=feature_id,
        project_id=project_id,
        position=3,
    )
    await _create_demo_task(
        db,
        title="Справка: страница частых вопросов",
        description="Собрать страницу FAQ из вопросов первой недели.",
        status="completed",
        task_type="task",
        parent_id=feature_id,
        project_id=project_id,
        position=4,
    )

    await db.commit()
    return True


async def _amain() -> int:
    from hub.db import get_db

    db = await get_db()
    try:
        created = await seed_demo(db)
    finally:
        await db.close()
    if created:
        print("demo seed: created project 'demo' with sample tasks")
    else:
        print("demo seed: skipped (flag off or already seeded)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_amain()))
