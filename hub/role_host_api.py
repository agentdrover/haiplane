"""Маршруты хоста для заказов ролей k8 (#1973).

Вынесены из ``hub/app.py`` (бюджет размера) и из модуля маршрутов хостов
исполнителя. Три маршрута своего хоста, на которые allowlist ключа хоста
(``SLOT_RUNNER_ALLOWLIST``) пускает по имени из пути:

* ``GET /api/executor-hosts/{name}/role-orders?wait=60`` - long-poll: lease
  свободного допущенного заказа или текущий lease того же хоста;
* ``POST .../role-orders/{id}/started`` - хост начал работу;
* ``POST .../role-orders/{id}/report`` - итог работы.

И один человеческий маршрут: допуск хоста к роли в проекте. Агент и ключ хоста
сюда не проходят. Владелец и ``enabled`` хоста перепроверяются внутри пишущей
транзакции службы, а не только здесь: long-poll мог идти дольше, чем хост был
включён.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from hub import repository as repo
from hub.auth import current_identity
from hub.executor_host_api import _db, _host_caller, _human
from hub.services import role_orders

router = APIRouter()

#: Потолок long-poll: как у заказов исполнителя и /api/events.
WAIT_MAX = 60
#: Одновременных long-poll одного хоста (запас на перезапуск клиента).
POLL_MAX = 3
_polling: dict[str, int] = {}


class RoleOrderStarted(BaseModel):
    lease_id: str = Field(min_length=1, max_length=64)


class RoleOrderReport(BaseModel):
    lease_id: str = Field(min_length=1, max_length=64)
    outcome: str = Field(max_length=16)
    note: str = Field(default="", max_length=4000)


class RoleAdmission(BaseModel):
    role: str = Field(max_length=32)
    project: str = Field(max_length=128)


def _as_http(exc: role_orders.RoleOrderError) -> HTTPException:
    return HTTPException(exc.status, detail=exc.detail)


async def _poll(db: Any, name: str, principal_id: int | None, wait: int) -> list:
    deadline = time.monotonic() + min(wait, WAIT_MAX)
    while True:
        await role_orders.check_host(db, name, principal_id)
        if await role_orders.has_work(db, name):
            order = await role_orders.lease(db, name, principal_id)
            if order:
                return [order]
        if time.monotonic() >= deadline:
            return []
        await asyncio.sleep(1.0)


@router.get("/api/executor-hosts/{name}/role-orders")
async def api_role_orders(
    name: str,
    request: Request,
    wait: int = Query(default=0, ge=0, description="Long-poll секунд (до 60)"),
) -> dict:
    """Заказ роли хосту: lease или пусто. Пока работы нет, write-лок не берётся."""
    await _host_caller(request, name)
    if _polling.get(name, 0) >= POLL_MAX:
        raise HTTPException(
            429,
            detail={
                "reason": "too_many_polls",
                "message": f"не больше {POLL_MAX} одновременных опросов хоста",
            },
        )
    _polling[name] = _polling.get(name, 0) + 1
    try:
        orders = await _poll(
            _db(request), name, current_identity(request).principal_id, wait
        )
    except role_orders.RoleOrderError as exc:
        raise _as_http(exc) from exc
    finally:
        _polling[name] -= 1
        if not _polling[name]:
            del _polling[name]
    return {"host": name, "orders": orders}


@router.post("/api/executor-hosts/{name}/role-orders/{order_id}/started")
async def api_role_order_started(
    name: str, order_id: int, body: RoleOrderStarted, request: Request
) -> dict:
    """Хост начал работу по своему lease; повтор того же lease - 200 без изменений."""
    await _host_caller(request, name)
    try:
        return await role_orders.started(
            _db(request),
            name,
            current_identity(request).principal_id,
            order_id,
            body.lease_id,
        )
    except role_orders.RoleOrderError as exc:
        raise _as_http(exc) from exc


@router.post("/api/executor-hosts/{name}/role-orders/{order_id}/report")
async def api_role_order_report(
    name: str, order_id: int, body: RoleOrderReport, request: Request
) -> dict:
    """Итог работы роли: done или failed; повтор того же итога - 200, иной - 409."""
    await _host_caller(request, name)
    try:
        return await role_orders.report(
            _db(request),
            name,
            current_identity(request).principal_id,
            order_id,
            body.lease_id,
            body.outcome,
            body.note,
        )
    except role_orders.RoleOrderError as exc:
        raise _as_http(exc) from exc


async def _admission_change(
    request: Request, name: str, body: RoleAdmission, revoke: bool
) -> dict:
    identity = await _human(request, "role admission")
    db = _db(request)
    project = await repo.get_project_by_slug(db, body.project)
    if project is None:
        raise HTTPException(404, detail={"reason": "project_unknown"})
    change = role_orders.revoke_host if revoke else role_orders.admit_host
    try:
        await change(
            db,
            name,
            body.role,
            int(project["id"]),
            by=identity.principal_id,
            actor=identity.username,
        )
    except role_orders.RoleOrderError as exc:
        raise _as_http(exc) from exc
    return {
        "host": name,
        "role": body.role,
        "project": body.project,
        "admitted": not revoke,
    }


@router.post("/api/admin/executor-hosts/{name}/role-admissions")
async def api_role_admission(name: str, body: RoleAdmission, request: Request) -> dict:
    """Допустить хост к роли в проекте. Только человек (CSRF для cookie); аудит всегда."""
    return await _admission_change(request, name, body, revoke=False)


@router.post("/api/admin/executor-hosts/{name}/role-admissions/revoke")
async def api_role_admission_revoke(
    name: str, body: RoleAdmission, request: Request
) -> dict:
    """Отозвать допуск: не начатые lease хоста возвращаются в open. Только человек."""
    return await _admission_change(request, name, body, revoke=True)
