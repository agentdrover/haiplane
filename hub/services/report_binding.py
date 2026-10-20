"""Привязка отчёта ревью к КОНКРЕТНОМУ заказу (#1946).

До #1946 отчёт сопоставлялся с заказом по порядку: k-й заказ принципала ждал
его k-го отчёта (``review_dispatch._dispatch_report``). Два отчёта одного
принципала на один заказ сдвигали порядок, и свип закрывал следующий заказ
(deep добора) чужим отчётом — раньше, чем тот отчитался.

Теперь у каждого заказа свой одноразовый код доступа ревьюера, и привязка идёт
через него: ``chat_pair_codes.dispatch_id`` -> ``chat_pair_sessions.dispatch_id``
-> ``TokenIdentity.chat_pair_dispatch_id`` -> ``machine_reviews.dispatch_id``.
Принципал остаётся ПРОВЕРКОЙ авторства: привязка принимается, только когда
заказ той же задачи, того же поколения и того же принципала, что в строке
заказа. Иначе отчёт остаётся без привязки (NULL) и сопоставляется по прежнему
порядку — среди непривязанных.

Код выписывается ДО строки заказа (агента создают с кодом в промте), поэтому
привязка кода идёт сразу после вставки строки заказа.
"""

from __future__ import annotations

from typing import Any

import aiosqlite

from hub.db import fetchall
from hub.services import chat_pair


async def bind_code_to_dispatch(
    db: aiosqlite.Connection,
    access_code: str,
    *,
    dispatch_id: int,
    task_id: int,
    generation: int,
    principal_id: int | None,
) -> None:
    """Привязать выписанный заказу код (и уже выкупленную им сессию) к заказу.

    Код выкупается агентом, пока хаб вставляет строку заказа, — тогда сессия уже
    есть: её догоняет второй UPDATE (сессии этого принципала, задачи и поколения,
    рождённые не раньше кода и ещё без заказа). Не коммитит: транзакция вставки
    заказа общая. Пустой код (открытый режим, отозванный токен) — привязывать
    нечего.
    """
    if not access_code or principal_id is None:
        return
    code_hash = chat_pair.hash_pair_code(chat_pair.normalize_pair_code(access_code))
    await db.execute(
        "UPDATE chat_pair_codes SET dispatch_id = ? WHERE code_hash = ? "
        "AND kind = 'reviewer' AND bound_task_id = ? AND bound_generation = ? "
        "AND dispatch_id IS NULL",
        (dispatch_id, code_hash, task_id, generation),
    )
    await db.execute(
        "UPDATE chat_pair_sessions SET dispatch_id = ? WHERE kind = 'reviewer' "
        "AND principal_id = ? AND bound_task_id = ? AND bound_generation = ? "
        "AND dispatch_id IS NULL AND created_at >= "
        "(SELECT created_at FROM chat_pair_codes WHERE code_hash = ?)",
        (dispatch_id, principal_id, task_id, generation, code_hash),
    )


async def accepted_dispatch(
    db: aiosqlite.Connection,
    task_id: int,
    generation: int,
    principal_id: int | None,
    claimed_id: int | None,
) -> dict[str, Any] | None:
    """Строка заказа, которому отчёт вправе отвечать, или None — без привязки.

    ``claimed_id`` приходит из сессии (или из строки заказа при отчёте,
    восстановленном из текста). Принимается, только когда заказ найден, той же
    задачи и поколения, а его принципал — принципал отчёта (когда заказ его
    знает). Сбой чтения не отменяет приём отчёта: привязка остаётся NULL и
    пишется в журнал вызывающим.
    """
    if claimed_id is None:
        return None
    rows = await fetchall(
        db, "SELECT * FROM review_dispatches WHERE id = ?", (int(claimed_id),)
    )
    if not rows:
        return None
    dispatch = dict(rows[0])
    if int(dispatch["task_id"]) != int(task_id):
        return None
    if int(dispatch["submission_generation"]) != int(generation):
        return None
    pinned = dispatch.get("reviewer_principal_id")
    if pinned is not None and pinned != principal_id:
        return None
    return dispatch


async def stamp_report(
    db: aiosqlite.Connection, review_id: int, dispatch_id: int | None
) -> None:
    """Записать на отчёте заказ, которому он отвечает. Не коммитит."""
    if dispatch_id is None:
        return
    await db.execute(
        "UPDATE machine_reviews SET dispatch_id = ? WHERE id = ?",
        (int(dispatch_id), int(review_id)),
    )
