"""Маршруты хоста критика (#1953): забрать заказы критика и сообщить итог.

Вынесены из ``executor_host_api``: у того свой бюджет размера, а эти два маршрута -
отдельная группа. Дверь та же: ключ роли slot_runner ЭТОГО хоста
(``_host_caller``) и строка в ``SLOT_RUNNER_ALLOWLIST``. Критик не получает кода и
сессии хаба: он читает снимок sha с GitHub и пишет хабу только отчёт.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from hub.executor_host_api import (
    ORDERS_POLL_MAX,
    ORDERS_WAIT_MAX,
    _db,
    _host_caller,
)
from hub.services import critic_orders

router = APIRouter()

#: Одновременных long-poll заказов критика одного хоста; сверх - 429.
_polling: dict[str, int] = {}


class CriticReport(BaseModel):
    outcome: str = Field(max_length=16)
    findings_p1: int = Field(default=0, ge=0, le=1000)
    findings_p2: int = Field(default=0, ge=0, le=1000)
    summary: str = Field(default="", max_length=4000)
    host_ref: str = Field(default="", max_length=80)


@router.get("/api/executor-hosts/{name}/critic-orders")
async def api_critic_orders(
    name: str,
    request: Request,
    wait: int = Query(default=0, ge=0, description="Long-poll секунд (до 60)"),
) -> dict:
    """Живые заказы критика своего хоста; выданный отдаётся повторно до закрытия."""
    await _host_caller(request, name)
    db = _db(request)
    if _polling.get(name, 0) >= ORDERS_POLL_MAX:
        raise HTTPException(
            429,
            detail={
                "reason": "too_many_polls",
                "message": f"не больше {ORDERS_POLL_MAX} одновременных опросов хоста",
            },
        )
    _polling[name] = _polling.get(name, 0) + 1
    try:
        deadline = time.monotonic() + min(wait, ORDERS_WAIT_MAX)
        orders: list[dict[str, Any]] = []
        while True:
            if await critic_orders.has_open(db, name):
                orders = await critic_orders.deliver(db, name)
            if orders or time.monotonic() >= deadline:
                break
            await asyncio.sleep(1.0)
    finally:
        _polling[name] -= 1
        if not _polling[name]:
            del _polling[name]
    return {"host": name, "orders": orders}


@router.post("/api/executor-hosts/{name}/critic-orders/{order_id}/report")
async def api_critic_report(
    name: str, order_id: int, body: CriticReport, request: Request
) -> dict:
    """Итог критика: один раз; повтор идентичного - no-op, изменённого - 409."""
    await _host_caller(request, name)
    return await critic_orders.report(
        _db(request),
        name,
        order_id,
        outcome=body.outcome,
        findings_p1=body.findings_p1,
        findings_p2=body.findings_p2,
        summary=body.summary,
        host_ref=body.host_ref,
    )
