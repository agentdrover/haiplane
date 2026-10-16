"""Кнопка «Запустить на <хост>» на карточке задачи (#1712).

Вынесена из ``hub/web.py``: у того бюджет размера (#1669). Та же дверь, что у
REST: человек, cookie-сессия — с верным CSRF, запуск выписывает код ОТ ИМЕНИ
нажавшего и отдаёт его хосту сам; человек код не видит и не переносит.
"""

from __future__ import annotations

from typing import Any

import aiosqlite
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from hub import repository as repo
from hub.auth import current_identity
from hub.services import executor_launch, executor_orders, project_policy

router = APIRouter()


async def card_hosts(db: aiosqlite.Connection, task_id: int) -> list[str]:
    """Включённые хосты, на которые карточка предлагает запуск.

    Только у проекта в режиме manual: в off запуск всё равно откажет, и кнопка,
    которая гарантированно ответит отказом, — шум на карточке.
    """
    policy = await project_policy.gate_policy_for_task(db, task_id)
    if executor_launch.launch_mode_of(policy) != executor_launch.LAUNCH_MANUAL:
        return []
    return await executor_orders.enabled_host_names(db)


@router.post("/tasks/{task_id}/web-executor-slot", response_class=HTMLResponse)
async def web_executor_slot(
    task_id: int,
    request: Request,
    host: str = Form(default=""),
    csrf_token: str = Form(default=""),
) -> Any:
    """Запустить исполнителя на ЭТУ задачу и выбранный хост."""
    # Лениво: hub.web собирается раньше и импортирует этот модуль по необходимости.
    from hub.web import _check_web_csrf, _db, _require_human_web, _web_task_detail_page

    _require_human_web(request)
    if not _check_web_csrf(request, csrf_token):
        return await _web_task_detail_page(
            request,
            task_id,
            implementer_error="Форма устарела. Обновите страницу и попробуйте снова.",
            status_code=403,
        )
    identity = current_identity(request)
    if identity.principal_id is None:
        return await _web_task_detail_page(
            request,
            task_id,
            implementer_error=(
                "Код исполнителю выписывается от имени принципала хаба; вход по "
                "env-токену для этого не подходит."
            ),
        )
    db = _db(request)
    project = await repo.resolve_project_for_task(db, task_id)
    if project is None:
        return await _web_task_detail_page(
            request,
            task_id,
            implementer_error="Проект задачи не найден.",
            status_code=404,
        )
    result = await executor_launch.launch_executor(
        db,
        project,
        issuer_principal_id=int(identity.principal_id),
        target=f"slot:{host}",
        task_id=task_id,
    )
    if not result.launched:
        return await _web_task_detail_page(
            request,
            task_id,
            implementer_error=f"Запуск на слот {host} не выполнен: {result.reason}",
            status_code=409,
        )
    return RedirectResponse(f"/tasks/{task_id}", status_code=303)
