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


#: Передачи человеку, которые снимаются сами: суточные сутки кончаются, причина
#: пропуска советника уходит. Прочие причины (прогон кончился, судья ушёл,
#: советник возразил) терминальны: заказ на это поколение уже не вернётся.
TEMPORARY_REASONS = ("steward_daily_cap", "steward_advisor_skipped")


def is_temporary(reason: str) -> bool:
    """Снимется ли передача сама, когда причина исчезнет."""
    return any(
        reason == code or reason.startswith(code + ":") for code in TEMPORARY_REASONS
    )


def _live_actor_sql() -> str:
    """Условие «у сдачи нет живого исполнителя» для строки ``tasks``.

    Один текст на выборку сторожа и на его запись: исполнитель, появившийся
    между ними, отменяет запись. Судья-approve считается живым ровно тогда,
    когда его подберёт тот же запрос, что у советника и применения.
    """
    from hub.services.steward_advisor import PICKED_APPROVE_SQL

    return (
        "COALESCE(tasks.review_job_id, '') = '' "  # nosec B608 - constant fragments only
        "AND COALESCE(tasks.review_verdict_generation, -1) "
        "!= tasks.submission_generation "
        "AND NOT EXISTS (SELECT 1 FROM steward_runs r "
        "                WHERE r.task_id=tasks.id AND r.status='open') "
        "AND NOT EXISTS (SELECT 1 FROM review_dispatches d "
        "                WHERE d.task_id=tasks.id "
        "                AND d.submission_generation = tasks.submission_generation "
        "                AND d.status IN ('active', 'second_door')) "
        "AND NOT EXISTS (SELECT 1 FROM steward_judgements j "
        "                WHERE j.task_id=tasks.id "
        "                AND j.generation = tasks.submission_generation "
        f"                AND {PICKED_APPROVE_SQL} "
        "                AND j.advisor_outcome IN ('', 'applying'))"
    )


def _stalled_sql() -> tuple[str, tuple[str, ...]]:
    """Условие «сторожу пора»: нет живого исполнителя, владельца и свежего события.

    Один текст на выборку и на запись сторожа: передачу человеку (в том числе
    временную, по потолку), записанную после оценки маршрута, и исполнителя,
    появившегося после неё, запись уже не перекроет. Срок отсчитывается от
    входа в review и от последнего ``verdict_owner_changed``.
    """
    window = f"-{WATCHDOG_GRACE_MINUTES} minutes"
    sql = (
        f"{_live_actor_sql()} "  # nosec B608 - constant fragments only
        "AND NOT (tasks.verdict_owner='human' "
        "         AND tasks.verdict_owner_generation = tasks.submission_generation) "
        "AND COALESCE(tasks.status_entered_at, tasks.updated_at) "
        "<= datetime('now', ?) "
        "AND NOT EXISTS (SELECT 1 FROM events e WHERE e.task_id=tasks.id "
        "                AND e.kind=? AND e.created_at > datetime('now', ?))"
    )
    return sql, (window, EVENT_CHANGED, window)


async def write_owner(
    db: aiosqlite.Connection,
    task_id: int,
    owner: str,
    reason: str,
    *,
    generation: int,
    only_without_actor: bool = False,
) -> bool:
    """Записать владельца вердикта и одно событие ``verdict_owner_changed``.

    ``generation`` обязателен: это сдача, о которой судил актор. Запись другой
    сдачи отбрасывается (актор опоздал, задачу уже пересдали); «текущей по
    умолчанию» нет, иначе опоздавший актор отдал бы человеку чужую сдачу.

    Тот же владелец на том же поколении ничего не пишет: причина остаётся
    первой, событие - одно. Исключение одно: терминальная причина заменяет
    временную (``TEMPORARY_REASONS``) с новым событием, а временная терминальную
    не заменяет никогда. Условия стоят в самом UPDATE, поэтому два тика не
    запишут дважды. ``only_without_actor`` добавляет в тот же UPDATE проверку
    «живого исполнителя нет». ``True`` - владелец записан этим вызовом.
    """
    if owner not in OWNERS:
        raise ValueError(f"unknown verdict owner {owner!r}")
    row = await repo.get_task(db, task_id)
    if row is None:
        return False
    task = dict(row)
    current = int(task.get("submission_generation") or 0)
    if current <= 0 or int(generation) != current:
        return False
    text = reason.strip()[:_REASON_MAX]
    stalled_sql, stalled_params = _stalled_sql() if only_without_actor else ("", ())
    temporary = " OR ".join("verdict_owner_reason LIKE ?" for _ in TEMPORARY_REASONS)
    cursor = await db.execute(
        "UPDATE tasks SET verdict_owner=?, verdict_owner_reason=?, "  # nosec B608 - constant fragments only
        "verdict_owner_since=strftime('%Y-%m-%d %H:%M:%f', 'now'), "
        "verdict_owner_generation=? WHERE id=? AND submission_generation=? "
        "AND NOT (verdict_owner=? AND verdict_owner_generation=? "
        f"AND NOT (? AND ({temporary}))) "
        + (f"AND {stalled_sql}" if only_without_actor else ""),
        (
            owner,
            text,
            current,
            task_id,
            current,
            owner,
            current,
            int(not is_temporary(text)),
            *(f"{code}%" for code in TEMPORARY_REASONS),
            *stalled_params,
        ),
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
    generation: int,
    only_without_actor: bool = False,
) -> bool:
    """Актор сложил решение: вердикт этой сдачи за человеком, причина названа."""
    return await write_owner(
        db,
        task_id,
        OWNER_HUMAN,
        reason,
        generation=generation,
        only_without_actor=only_without_actor,
    )


async def reclaim_for_steward(
    db: aiosqlite.Connection, task_id: int, generation: int
) -> bool:
    """Стюарду заказан новый прогон на этой сдаче: временная передача снята.

    Только временные причины (потолок, пропуск советника): заказ возвращается,
    когда причина снимается, и запись «человек» скрыла бы живой заказ. Терминальная
    передача (автопилот, судья, итог прогона) заказом не решается и остаётся.
    """
    row = await repo.get_task(db, task_id)
    if row is None:
        return False
    owner, reason = written_owner(dict(row))
    if owner != OWNER_HUMAN or not is_temporary(reason):
        return False
    return await write_owner(
        db, task_id, OWNER_STEWARD, "run_ordered", generation=generation
    )


async def _stalled_candidates(db: aiosqlite.Connection) -> list[dict[str, Any]]:
    """Задачи в review без живого исполнителя дольше срока и без записи «человек»."""
    sql, params = _stalled_sql()
    rows = await fetchall(
        db,
        "SELECT tasks.* FROM tasks WHERE tasks.status='review' AND tasks.archived=0 "
        "AND tasks.submission_generation > 0 "
        f"AND {sql}",  # nosec B608 - constant fragments only
        params,
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
            only_without_actor=True,
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
