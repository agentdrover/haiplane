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
from hub.services import (
    executor_launch,
    executor_orders,
    launch_feedback,
    project_policy,
    rework_admission,
)

router = APIRouter()


async def card_hosts(db: aiosqlite.Connection, task: dict[str, Any]) -> list[str]:
    """Включённые хосты, на которые карточка предлагает запуск.

    Только у проекта в режиме manual: в off запуск всё равно откажет, и кнопка,
    которая гарантированно ответит отказом, — шум на карточке. У running
    (круг починки после changes_requested, #1998) — ещё и только там, где его
    допускает общее правило ``rework_admission.refusal``, иначе кнопка ответила бы отказом.
    """
    status = task.get("status")
    if status not in ("open", "running"):
        return []
    task_id = int(task["id"])
    policy = await project_policy.gate_policy_for_task(db, task_id)
    if executor_launch.launch_mode_of(policy) != executor_launch.LAUNCH_MANUAL:
        return []
    if status == "running" and await rework_admission.refusal(db, task):
        return []
    return await executor_orders.enabled_host_names(db)


async def card_capacity(db: Any) -> dict[str, str]:
    """#2107: «N/slots» хоста для кнопки запуска — из того же счёта незакрытых заказов, что /api/executor-slots."""
    from hub.services.executor_slots import host_capacity

    return {h["name"]: f"{h['open']}/{h['slots']}" for h in await host_capacity(db)}


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
        issuer_human=identity.is_human,
    )
    if not result.launched:
        return await _web_task_detail_page(
            request,
            task_id,
            implementer_error=f"Запуск на слот {host} не выполнен: {result.reason}",
            status_code=409,
        )
    return RedirectResponse(f"/tasks/{task_id}", status_code=303)


@router.get("/partials/launch-plaque/{task_id}", response_class=HTMLResponse)
async def web_launch_plaque(task_id: int, request: Request) -> Any:
    """Фрагмент плашки запуска (#1817): HTMX обновляет только его, не страницу."""
    from hub.web import TEMPLATES, _db

    plaque = await launch_feedback.plaque(_db(request), task_id)
    response = TEMPLATES.TemplateResponse(
        request,
        "partials/launch_plaque.html",
        {"plaque": plaque, "task_id": task_id},
    )
    if request.headers.get("hx-request") and not (plaque and plaque["active"]):
        # Запуск кончился: кнопка запуска вне фрагмента, и страница
        # перезагружается один раз (закрытая плашка сама уже не опрашивает).
        # Не HX-Refresh: htmx исполняет его до htmx:beforeSwap и стёр бы
        # недописанный ввод; обработчик в base.html (#1861) ждёт чистой формы.
        response.headers["HX-Trigger"] = "hub-launch-ended"
    return response
