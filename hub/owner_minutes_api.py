"""Маршруты оценки минут владельца (#1874).

Вынесены из ``hub/app.py``: у того бюджет размера (#1669) без запаса. Маршруты
- тонкая обёртка над ``hub.services.owner_minutes``; число не пересчитывается.
"""

from __future__ import annotations

from datetime import date as date_type

from fastapi import APIRouter, Depends, HTTPException, Request

from hub.auth import current_identity, require_human_or_admin
from hub.owner_minutes_view import OwnerMinutesDayView, OwnerMinutesView
from hub.services import owner_minutes

router = APIRouter()


def _conn(request: Request):
    return getattr(request.state, "db", None) or request.app.state.db


@router.get("/api/tasks/{task_id}/owner-minutes", response_model=OwnerMinutesView)
async def api_task_owner_minutes(task_id: int, request: Request):
    """Оценка минут владельца по задаче - и по уже удалённой тоже.

    Журнал не зависит от задачи: сумма читается по ``task_id`` из начислений,
    поэтому 404 здесь нет.
    """
    return await owner_minutes.task_estimate(_conn(request), task_id)


@router.get("/api/owner-minutes/day", response_model=OwnerMinutesDayView)
async def api_owner_minutes_day(
    request: Request,
    principal_id: int | None = None,
    date: str | None = None,
    _identity=Depends(require_human_or_admin),
):
    """Оценка минут принципала за локальный день (по всем проектам).

    День - локальная дата UTC-времени начисления в часовом поясе настройки хаба
    (``HAIPLANE_OWNER_MINUTES_TIMEZONE``); применённый пояс есть в ответе.
    Без ``principal_id`` - вызывающий; без ``date`` - сегодня.
    """
    who = (
        principal_id
        if principal_id is not None
        else current_identity(request).principal_id
    )
    if who is None:
        raise HTTPException(
            422, "principal_id обязателен: у вызывающего нет принципала в хабе"
        )
    try:
        day = date_type.fromisoformat(date) if date else owner_minutes.today_local()
    except ValueError as exc:
        raise HTTPException(422, "date: ожидается YYYY-MM-DD") from exc
    try:
        return await owner_minutes.day_estimate(_conn(request), int(who), day)
    except owner_minutes.DayOutOfRange as exc:
        raise HTTPException(422, str(exc)) from exc
