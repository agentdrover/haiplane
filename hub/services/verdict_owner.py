"""Владелец вердикта сдачи: его ПИШЕТ тот, кто сложил с себя решение (#2052).

Маршрут вердикта (``verdict_route``) пересчитывает «кто решает» по шести
источникам, и каждый исход стюарда, который читатель забыл, молча оставлял
задачу в ``review`` вне входящих владельца. Здесь наоборот: актор, у которого
решение закончилось (стюард, советник, автопилот, суточный потолок, сторож),
записывает владельца в ``tasks`` в той же транзакции, что и свой исход, и
маршрут читает запись ПЕРВОЙ. Пересчёт остаётся прогнозом, пока записи нет.

Запись действует только на своём поколении сдачи: ``verdict_owner_generation``
должно равняться ``submission_generation``. Пересдача поднимает поколение, и
владелец сам становится пустым - отдельного сброса нет, значит и забыть его
нельзя. Прежние колонки остаются историей, их показывает событие.

Все функции пишут БЕЗ коммита: коммитит вызывающий вместе со своим исходом.
"""

from __future__ import annotations

import logging
from typing import Any

import aiosqlite

from hub import repository as repo
from hub.db import fetchall

log = logging.getLogger(__name__)

OWNER_POLICY = "policy"
OWNER_STEWARD = "steward"
OWNER_HUMAN = "human"
OWNER_NONE = "none"
OWNERS = frozenset({OWNER_POLICY, OWNER_STEWARD, OWNER_HUMAN, OWNER_NONE})

EVENT_CHANGED = "verdict_owner_changed"

#: Сколько минут review без живого исполнителя терпит сторож (с входа в review).
WATCHDOG_GRACE_MINUTES = 30

_REASON_MAX = 500


def reason_of(code: str, detail: str = "") -> str:
    """Причина записи: ``код: подробность`` - код же становится кодом маршрута."""
    return f"{code}: {detail}" if detail else code


def written_owner(task: dict[str, Any]) -> tuple[str, str]:
    """``(владелец, причина)`` записи текущей сдачи; пусто, если её нет или она старая."""
    owner = str(task.get("verdict_owner") or "")
    generation = int(task.get("submission_generation") or 0)
    if not owner or generation <= 0:
        return "", ""
    if int(task.get("verdict_owner_generation") or 0) != generation:
        return "", ""
    return owner, str(task.get("verdict_owner_reason") or "")


async def write_owner(
    db: aiosqlite.Connection,
    task_id: int,
    owner: str,
    reason: str,
    *,
    generation: int | None = None,
) -> bool:
    """Записать владельца вердикта и одно событие ``verdict_owner_changed``.

    ``generation`` - сдача, о которой знает актор; запись другой сдачи
    отбрасывается (актор опоздал, задачу уже пересдали). Тот же владелец на
    том же поколении ничего не пишет: причина остаётся первой, событие - одно.
    Условие стоит в самом UPDATE, поэтому два тика не запишут дважды.
    ``True`` - владелец записан этим вызовом.
    """
    if owner not in OWNERS:
        raise ValueError(f"unknown verdict owner {owner!r}")
    row = await repo.get_task(db, task_id)
    if row is None:
        return False
    task = dict(row)
    current = int(task.get("submission_generation") or 0)
    if current <= 0 or (generation is not None and int(generation) != current):
        return False
    text = reason.strip()[:_REASON_MAX]
    cursor = await db.execute(
        "UPDATE tasks SET verdict_owner=?, verdict_owner_reason=?, "
        "verdict_owner_since=strftime('%Y-%m-%d %H:%M:%f', 'now'), "
        "verdict_owner_generation=? WHERE id=? AND submission_generation=? "
        "AND NOT (verdict_owner=? AND verdict_owner_generation=?)",
        (owner, text, current, task_id, current, owner, current),
    )
    if cursor.rowcount != 1:
        return False
    await repo.insert_event(
        db,
        kind=EVENT_CHANGED,
        task_id=task_id,
        project_id=task.get("project_id"),
        actor="hub",
        payload={"owner": owner, "reason": text, "generation": current},
    )
    return True


async def hand_to_human(
    db: aiosqlite.Connection,
    task_id: int,
    reason: str,
    *,
    generation: int | None = None,
) -> bool:
    """Актор сложил решение: вердикт этой сдачи за человеком, причина названа."""
    return await write_owner(db, task_id, OWNER_HUMAN, reason, generation=generation)


async def reclaim_for_steward(
    db: aiosqlite.Connection, task_id: int, generation: int
) -> bool:
    """Стюарду заказан новый прогон на этой сдаче: прежняя передача человеку снята.

    Передача по суточному потолку или пропуску советника временная: заказ
    возвращается, когда причина снимается. Запись «человек» пережила бы это и
    скрыла живой заказ. Пустую запись или чужого владельца не трогает.
    """
    row = await repo.get_task(db, task_id)
    if row is None or written_owner(dict(row))[0] != OWNER_HUMAN:
        return False
    return await write_owner(
        db, task_id, OWNER_STEWARD, "run_ordered", generation=generation
    )


async def _stalled_candidates(db: aiosqlite.Connection) -> list[dict[str, Any]]:
    """Задачи в review без живого исполнителя дольше срока и без записи «человек»."""
    rows = await fetchall(
        db,
        "SELECT t.* FROM tasks t WHERE t.status='review' AND t.archived=0 "
        "AND t.submission_generation > 0 "
        "AND COALESCE(t.review_job_id, '') = '' "
        "AND COALESCE(t.review_verdict_generation, -1) != t.submission_generation "
        "AND NOT (t.verdict_owner='human' "
        "         AND t.verdict_owner_generation = t.submission_generation) "
        "AND COALESCE(t.status_entered_at, t.updated_at) <= datetime('now', ?) "
        "AND NOT EXISTS (SELECT 1 FROM steward_runs r "
        "                WHERE r.task_id=t.id AND r.status='open') "
        "AND NOT EXISTS (SELECT 1 FROM review_dispatches d "
        "                WHERE d.task_id=t.id "
        "                AND d.submission_generation = t.submission_generation "
        "                AND d.status IN ('active', 'second_door')) "
        "AND NOT EXISTS (SELECT 1 FROM steward_judgements j "
        "                WHERE j.task_id=t.id AND j.generation = t.submission_generation "
        "                AND j.kind='verdict' AND j.verdict='approve' "
        "                AND j.advisor_outcome IN ('', 'applying')) "
        "AND NOT EXISTS (SELECT 1 FROM events e WHERE e.task_id=t.id "
        "                AND e.kind=? AND e.created_at > datetime('now', ?))",
        (
            f"-{WATCHDOG_GRACE_MINUTES} minutes",
            EVENT_CHANGED,
            f"-{WATCHDOG_GRACE_MINUTES} minutes",
        ),
    )
    return [dict(row) for row in rows]


async def hand_over_stalled_reviews(db: aiosqlite.Connection) -> int:
    """Сторож: маршрут ждёт автопилота или стюарда, а живого исполнителя нет.

    Берёт только задачи, где прогноз маршрута называет НЕ человека: там, где
    человек и так назван, сторожу нечего исправлять, а его причина (замок,
    выключенный стюард) точнее общей. Исполнитель живой, если есть открытый
    заказ стюарда, ждущий применения approve судьи или метка ``applying``,
    активный заказ ревью или идущее ревью: тогда тишина - ожидание.
    Повторный тик ничего не дублирует: владелец «человек» этой сдачи уже записан.
    """
    from hub.services.verdict_route import DECIDER_HUMAN, verdict_route

    handed = 0
    for task in await _stalled_candidates(db):
        task_id = int(task["id"])
        route = await verdict_route(db, task_id)
        if route.final == DECIDER_HUMAN or route.decider == "none":
            continue
        reason = (
            f"no_actor: маршрут ждал {route.final} ({route.code}), живого "
            f"исполнителя нет уже {WATCHDOG_GRACE_MINUTES} мин"
        )
        if await hand_to_human(
            db,
            task_id,
            reason,
            generation=int(task["submission_generation"]),
        ):
            await repo.add_task_update(
                db,
                task_id,
                "hub",
                "alert",
                "Вердикт сдачи передан человеку: " + reason + ".",
                author_kind="hub",
            )
            handed += 1
    if handed:
        await db.commit()
    return handed
