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

    Код выкупается агентом, пока хаб вставляет строку заказа. Выкуп после
    привязки копирует её в сессию тем же INSERT (``chat_pair._spend_code``);
    выкуп до привязки оставил сессию без заказа - её догоняет второй UPDATE по
    ``code_id`` (сессия именно из ЭТОГО кода, чужие не задеваются). Не
    коммитит: транзакция вставки заказа общая. Пустой код (открытый режим,
    отозванный токен) - привязывать нечего.
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
        "UPDATE chat_pair_sessions SET dispatch_id = ? WHERE dispatch_id IS NULL "
        "AND code_id = (SELECT id FROM chat_pair_codes WHERE code_hash = ?)",
        (dispatch_id, code_hash),
    )
    # Отчёты, принятые от этого кода до привязки, тоже отвечают заказу и
    # несут его профиль (при приёме профиль взят у последнего заказа).
    caught = await db.execute(
        "UPDATE machine_reviews SET dispatch_id = ?, "
        "profile = (SELECT profile FROM review_dispatches WHERE id = ?) "
        "WHERE dispatch_id IS NULL "
        "AND code_id = (SELECT id FROM chat_pair_codes WHERE code_hash = ?)",
        (dispatch_id, dispatch_id, code_hash),
    )
    if caught.rowcount:  # отчёт заказа-отказа есть: агент существует (#1408)
        await db.execute(
            "UPDATE review_dispatches SET status = 'active' WHERE id = ? "
            "AND agent_id = '' AND channel = 'cloud' AND status = 'failed'",
            (dispatch_id,),
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
    db: aiosqlite.Connection,
    review_id: int,
    dispatch_id: int | None,
    code_id: int | None = None,
) -> None:
    """Записать на отчёте заказ, которому он отвечает, и код его сессии.

    Код нужен, пока заказ неизвестен: поздняя привязка этого кода догонит и
    отчёт (``bind_code_to_dispatch``). Не коммитит.
    """
    if dispatch_id is None and code_id is None:
        return
    if dispatch_id is None:
        # Привязка могла лечь между чтением личности и этой записью.
        late = await fetchall(
            db,
            "SELECT d.id, d.profile FROM chat_pair_codes c JOIN review_dispatches d "
            "ON d.id = c.dispatch_id WHERE c.id = ?",
            (code_id,),
        )
        if late:
            await db.execute(
                "UPDATE machine_reviews SET dispatch_id = ?, code_id = ?, "
                "profile = ? WHERE id = ?",
                (late[0]["id"], code_id, late[0]["profile"], int(review_id)),
            )
            return
    await db.execute(
        "UPDATE machine_reviews SET dispatch_id = ?, code_id = ? WHERE id = ?",
        (dispatch_id, code_id, int(review_id)),
    )


async def reopen_if_refused(
    db: aiosqlite.Connection, dispatch: dict[str, Any]
) -> dict[str, Any]:
    """Отчёт пришёл к заказу, записанному отказом (failed, без агента): оживить (#1408).

    Привязка доказывает, что агент этого кода существует, поэтому адресуется
    конкретный id, а не «последняя заглушка поколения». Не коммитит.
    """
    if (
        dispatch.get("agent_id") == ""
        and dispatch.get("channel") == "cloud"
        and dispatch.get("status") in ("failed", "done")
    ):
        await db.execute(
            "UPDATE review_dispatches SET status='active' WHERE id = ?",
            (int(dispatch["id"]),),
        )
        return {**dispatch, "status": "active"}
    return dispatch


async def is_addressed(db: aiosqlite.Connection, dispatch_id: int) -> bool:
    """На заказ указывает код, сессия или отчёт: строку нельзя удалять (#1946)."""
    rows = await fetchall(
        db,
        "SELECT 1 FROM chat_pair_codes WHERE dispatch_id = ? UNION ALL "
        "SELECT 1 FROM chat_pair_sessions WHERE dispatch_id = ? UNION ALL "
        "SELECT 1 FROM machine_reviews WHERE dispatch_id = ? LIMIT 1",
        (dispatch_id, dispatch_id, dispatch_id),
    )
    return bool(rows)


async def dispatch_of_report(
    db: aiosqlite.Connection, review_id: int | None
) -> dict[str, Any] | None:
    """Заказ, к которому отчёт ПРИВЯЗАН, или None - привязки нет (или отчёта нет)."""
    if review_id is None:
        return None
    rows = await fetchall(
        db,
        "SELECT d.* FROM machine_reviews r JOIN review_dispatches d "
        "ON d.id = r.dispatch_id WHERE r.id = ?",
        (int(review_id),),
    )
    return dict(rows[0]) if rows else None


async def order_chain(
    db: aiosqlite.Connection, dispatch_id: int
) -> tuple[set[int] | None, set[int]]:
    """(принципалы, id) заказа и всех его замен (``replaces_dispatch_id``).

    Принципалы None - любой: заказ без принципала (открытый режим, история) или
    не найденный в таблице отчёту не сопоставить, фильтра нет, и это сторона
    «отчёт был». id - цепочка заказов: отчёт, привязанный (#1946) к любому из
    них, - отчёт поступка.
    """
    seen = {dispatch_id}
    principals: set[int] = set()
    unpinned = False
    frontier = [dispatch_id]
    while frontier:
        marks = ",".join("?" for _ in frontier)
        rows = await fetchall(
            db,
            "SELECT id, reviewer_principal_id FROM review_dispatches "  # nosec B608 - только "?"
            f"WHERE id IN ({marks}) OR replaces_dispatch_id IN ({marks})",
            (*frontier, *frontier),
        )
        frontier = []
        for row in rows:
            if row["reviewer_principal_id"] is None:
                unpinned = True  # фильтра по принципалу нет, но цепочку досматриваем
            else:
                principals.add(int(row["reviewer_principal_id"]))
            if int(row["id"]) not in seen:
                seen.add(int(row["id"]))
                frontier.append(int(row["id"]))
    return (None if unpinned or not principals else principals), seen
