"""Сигнал «ревьюер недоступен» в ящик steward@<проект> (#1826).

Событие ``reviewer_unavailable`` поднимает поллер (#1262). Здесь оно получает
адресата: стюарды проектов с ожидающими задачами узнают о подъёме, о смене
своего набора и о снятии из канала #1679 (``watch_notices.notify_steward``),
а не из ленты. Контракт канала не меняется: версия 1, закрытые поля,
транзакция вызывающего, собственного commit нет — commit остаётся в свипе и
стоит после уведомлений, поэтому событие, ключи и сообщения коммитятся вместе.

Три понятия.

* Инцидент — id САМОГО РАННЕГО сохранившегося события подъёма после
  последнего снятия. ``signal_state`` и ``clearing`` не меняются: сигнал по-
  прежнему живёт в ``events``. Граница (scope_out): ретенция событий (14 дн.)
  удаляет первое событие, идентификатор сдвигается на следующее сохранившееся,
  и проект получает повтор на каждый сдвиг; если удалены ВСЕ события сигнала,
  он считается не поднятым, как и до этой задачи. Недоступность дольше
  ретенции видна и без уведомления.
* Картина проекта — набор его ожидающих сдач с причинами (как у сигнала).
  Проект получает сообщение, когда хеш картины отличается от хеша ПОСЛЕДНЕГО
  уведомления ему в инциденте; последнее — максимальный числовой event_id.
* Получатели снятия — проекты из ключей этого инцидента: ключи вечные, новой
  таблицы нет.

Ключи: ``reviewer_unavailable:<инцидент>:<проект>:<событие>:<хеш>`` и
``reviewer_unavailable:<инцидент>:<проект>:cleared``. Повод у подъёма и снятия
один — ``notify_steward`` не знает второго; различает их ``source_event_id``.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import aiosqlite

from hub import config
from hub import repository as repo
from hub.db import fetchall
from hub.services import review_availability as ra
from hub.services import watch_notices

REASON = "reviewer_unavailable"
_CLEARED = "cleared"


async def incident_id(db: aiosqlite.Connection) -> int | None:
    """Самое раннее сохранившееся событие подъёма после последнего снятия."""
    rows = await fetchall(
        db,
        "SELECT MIN(id) AS id FROM events WHERE kind = ? AND id > "
        "COALESCE((SELECT MAX(id) FROM events WHERE kind = ?), 0)",
        (ra.REVIEWER_UNAVAILABLE, ra.REVIEWER_SIGNAL_CLEARED),
    )
    return int(rows[0]["id"]) if rows and rows[0]["id"] is not None else None


async def notified_projects(
    db: aiosqlite.Connection, incident: int
) -> dict[int, tuple[int, str]]:
    """Проект -> (event_id, хеш) ПОСЛЕДНЕГО уведомления о картине в инциденте.

    Префикс сравнивается точно (``substr``), а не ``LIKE``: подчёркивание в
    имени повода для LIKE — шаблон. Двоеточие после номера инцидента не даёт
    инциденту 1 зацепить 12. Ключ снятия в картины не входит.
    """
    prefix = f"{REASON}:{incident}:"
    rows = await fetchall(
        db,
        "SELECT key FROM watch_notices WHERE substr(key, 1, ?) = ?",
        (len(prefix), prefix),
    )
    last: dict[int, tuple[int, str]] = {}
    for row in rows:
        parts = str(row["key"])[len(prefix) :].split(":")
        if len(parts) == 2 and parts[1] == _CLEARED:
            last.setdefault(int(parts[0]), (-1, ""))
            continue
        if len(parts) != 3:
            continue
        project_id, event_id = int(parts[0]), int(parts[1])
        if event_id > last.get(project_id, (-1, ""))[0]:
            last[project_id] = (event_id, parts[2])
    return last


def _digest(picture: list[tuple[int, str]]) -> str:
    return hashlib.sha1(json.dumps(sorted(picture)).encode()).hexdigest()[:12]


async def _pictures(
    db: aiosqlite.Connection, waiting: list[dict[str, Any]]
) -> dict[int, list[tuple[int, str]]]:
    """Ожидающие задачи, сгруппированные по проекту (наследование, иначе default)."""
    groups: dict[int, list[tuple[int, str]]] = {}
    for row in waiting:
        project, _ = await repo.resolve_project_with_source(db, int(row["task_id"]))
        if project is None:
            continue
        groups.setdefault(int(project["id"]), []).append(
            (int(row["task_id"]), str(row.get("reason") or ""))
        )
    return groups


async def _notify_changed(
    db: aiosqlite.Connection, event_id: int, waiting: list[dict[str, Any]]
) -> None:
    incident = await incident_id(db)
    if incident is None:
        return
    groups = await _pictures(db, waiting)
    last = await notified_projects(db, incident)
    for project_id in sorted(groups.keys() | last.keys()):
        digest = _digest(groups.get(project_id, []))
        if project_id in last and last[project_id][1] == digest:
            continue
        await watch_notices.notify_steward(
            db,
            REASON,
            project_id=project_id,
            source_key=f"{REASON}:{incident}:{project_id}:{event_id}:{digest}",
            source_event_id=event_id,
        )


async def record_raised(db: aiosqlite.Connection, outage: ra.Observation) -> int:
    """Событие подъёма/обновления и уведомления проектам; commit — у вызывающего."""
    event_id = await repo.insert_event(
        db,
        kind=ra.REVIEWER_UNAVAILABLE,
        actor="hub",
        payload={
            "provider": ra.PROVIDER,
            "since": outage.since,
            "refused_tasks": outage.refused_tasks,
            "waiting_count": len(outage.waiting),
            "waiting": outage.waiting,
            "threshold_hours": config.REVIEWER_UNAVAILABLE_HOURS,
            "observed": outage.kind,
        },
    )
    await _notify_changed(db, event_id, outage.waiting)
    return event_id


async def record_cleared(db: aiosqlite.Connection, why: dict[str, Any]) -> int:
    """Событие снятия и уведомления всем проектам инцидента; commit — у вызывающего."""
    incident = await incident_id(db)  # до события: после него инцидента уже нет
    event_id = await repo.insert_event(
        db,
        kind=ra.REVIEWER_SIGNAL_CLEARED,
        actor="hub",
        payload={"provider": ra.PROVIDER, **why},
    )
    if incident is not None:
        for project_id in sorted(await notified_projects(db, incident)):
            await watch_notices.notify_steward(
                db,
                REASON,
                project_id=project_id,
                source_key=f"{REASON}:{incident}:{project_id}:{_CLEARED}",
                source_event_id=event_id,
            )
    return event_id
