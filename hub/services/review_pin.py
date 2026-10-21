"""Сверка пина отчёта ревью со сдачей (#1985).

Три шага, все в транзакции записи отчёта: взять write-лок ДО чтения задачи
(``under_pin_lock``), сверить заявленные поколение и sha с текущим поколением
задачи и строкой ``submissions`` (``verify``), записать результат рядом с
отчётом (``stamp``). Пересдача пишет ``tasks`` и ``submissions`` под тем же
локом, поэтому она либо ждёт отчёта, либо отчёт видит новое поколение и
получает 409 - окна между проверкой и записью нет.

Отчёт без пина проходит как раньше: ``pin_verified`` остаётся 0, ``reviewed_sha``
пуст. Значение sha в сообщения об отказе не попадает.
"""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from typing import ParamSpec, TypeVar

import aiosqlite
from fastapi import HTTPException

from hub import db as db_module
from hub.models import MachineReviewSubmit

PIN_MISMATCH = "review_pin_mismatch"
_MCP_ORIGIN = "mcp"

P = ParamSpec("P")
R = TypeVar("R")


def under_pin_lock(fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    """Пинованный отчёт принимается под write-локом, взятым до первого чтения."""

    @functools.wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        db: aiosqlite.Connection = args[0]  # type: ignore[assignment]
        body = kwargs.get("body", args[2] if len(args) > 2 else None)
        if getattr(body, "reviewed_sha", None) is None:
            return await fn(*args, **kwargs)
        async with db_module.write_transaction(db):
            return await fn(*args, **kwargs)

    return wrapper


def for_origin(body: MachineReviewSubmit, origin: str) -> MachineReviewSubmit:
    """Пин принимается только у отчёта, поданного по контракту (REST/MCP)."""
    if origin == _MCP_ORIGIN or body.reviewed_sha is None:
        return body
    return body.model_copy(update={"expected_generation": None, "reviewed_sha": None})


async def verify(
    db: aiosqlite.Connection,
    task_id: int,
    current_generation: int,
    body: MachineReviewSubmit,
) -> None:
    """409 review_pin_mismatch, если заявленная сдача не текущая; без пина - ничего."""
    if body.reviewed_sha is None:
        return
    cur = await db.execute(
        "SELECT sha FROM submissions WHERE task_id=? AND generation=?",
        (task_id, body.expected_generation),
    )
    row = await cur.fetchone()
    recorded = (row["sha"] if row else "") or ""
    if (
        body.expected_generation == current_generation
        and recorded.lower() == body.reviewed_sha
    ):
        return
    raise HTTPException(
        409,
        detail={
            "reason": PIN_MISMATCH,
            "message": (
                f"отчёт выписан на сдачу #{body.expected_generation}, а текущая "
                f"сдача задачи - #{current_generation}"
                + (
                    ""
                    if body.expected_generation != current_generation
                    else ", и заявленный sha не совпал с закреплённым на ней"
                )
                + ". Отчёт не записан: он не доказывает ревью текущего кода."
            ),
        },
    )


async def stamp(
    db: aiosqlite.Connection, review_id: int, body: MachineReviewSubmit
) -> None:
    """Записать заявленный sha и признак сверки - после ``verify`` в той же транзакции."""
    if body.reviewed_sha is None:
        return
    await db.execute(
        "UPDATE machine_reviews SET reviewed_sha=?, pin_verified=1 WHERE id=?",
        (body.reviewed_sha, review_id),
    )
