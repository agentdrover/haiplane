"""Лимит очереди review на входе новой работы (#1264).

Очередь review гниёт сама: каждая сутки простоявшая сдача рискует конфликтом
с базой, а слияние базы требует пересдачи и обесценивает прочитанное ревью.
Единственный рычаг, который уменьшает очередь без второго ревьюера, — не
открывать новую работу, пока очередь выше лимита: исполнитель, которому не
открыли задачу, берёт пересдачу, исправление или доставку.

Лимит стоит на ВХОДЕ, а не на сдаче: отказ в сдаче оставил бы готовую работу
в running — WIP бы не упал, а спрятался. Поэтому ни один шаг разбора очереди
(пересдача, ревью, вердикт, исправление, доставка) сюда не заглядывает.

Выходы названы, а не подразумеваются:

* ключа ``review_limit`` нет — поведение как до этой задачи;
* ``review_limit_mode=warn`` — вход не держится, лента говорит, кого держал бы;
* ``class_of_service=expedite`` — вход не держится, обход пишется в ленту;
* решение владельца: поднять K или снять ключ в gate_policy проекта.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import aiosqlite
from fastapi import HTTPException

from hub import repository as repo
from hub.mcp_envelope import enrich_error_payload
from hub.services import project_policy

log = logging.getLogger(__name__)

REVIEW_QUEUE_FULL = "review_queue_full"

HELD = "held"
WARNED = "warned"
EXPEDITED = "expedited"


@dataclass(frozen=True)
class ReviewQueueCheck:
    """Что лимит сказал про этот вход: держать, предупредить или пропустить."""

    outcome: str
    limit: int
    queue: tuple[int, ...]
    project_slug: str


async def check_review_queue(
    db: aiosqlite.Connection, task: dict[str, Any]
) -> ReviewQueueCheck | None:
    """``None`` — лимита нет или очередь ниже него; иначе исход проверки."""
    project = await repo.resolve_project_for_task(db, int(task["id"]))
    return await _check_project(db, project, task.get("class_of_service"))


async def check_review_queue_for_new_task(
    db: aiosqlite.Connection, parent_id: int | None, class_of_service: str
) -> ReviewQueueCheck | None:
    """То же для задачи, которой ещё нет: решение до вставки строки.

    Проект новой задачи — проект её родителя (обход вверх до эпика), без
    родителя — default: ровно то, что resolve_project_for_task скажет о ней
    после вставки. Сбой проверки — ``None``: вход не держится.
    """
    try:
        project = (
            await repo.get_project_by_slug(db, "default")
            if parent_id is None
            else await repo.resolve_project_for_task(db, parent_id)
        )
        return await _check_project(db, project, class_of_service)
    except Exception as exc:  # noqa: BLE001 - a gate that raises blocks everything
        log.warning("review queue check for a new task failed: %s", exc)
        return None


async def _check_project(
    db: aiosqlite.Connection, project: Any, class_of_service: Any
) -> ReviewQueueCheck | None:
    if project is None:
        return None
    policy = project_policy.gate_policy_of(project)
    limit = project_policy.review_limit_of(policy)
    if limit is None:
        return None
    queue = await repo.list_review_task_ids_in_project(db, int(project["id"]))
    if len(queue) < limit:
        return None
    if str(getattr(class_of_service, "value", class_of_service)) == "expedite":
        outcome = EXPEDITED
    elif (
        project_policy.review_limit_mode_of(policy) == project_policy.REVIEW_LIMIT_WARN
    ):
        outcome = WARNED
    else:
        outcome = HELD
    return ReviewQueueCheck(outcome, limit, tuple(queue), str(project["slug"]))


def _card_text(check: ReviewQueueCheck) -> str:
    listed = ", ".join(f"#{q}" for q in check.queue)
    head = (
        f"Очередь review проекта «{check.project_slug}» полна: "
        f"{len(check.queue)} при лимите {check.limit} ({listed})."
    )
    if check.outcome == EXPEDITED:
        return f"{head} Лимит обойдён по expedite: задача открыта вне очереди (#1264)."
    if check.outcome == WARNED:
        return (
            f"{head} Режим warn: открытие не удержано, при enforce задача "
            "не открылась бы (#1264)."
        )
    return (
        f"{head} Новая работа не открывается, пока очередь не разобрана (#1264): "
        "возьмите пересдачу, исправление, ревью или доставку из очереди. "
        "Выходы: class_of_service=expedite; решение владельца — поднять "
        "review_limit, снять его или перевести review_limit_mode в warn."
    )


async def _note_once(db: aiosqlite.Connection, task_id: int, text: str) -> None:
    """Запись в карточку; повтор с той же очередью карточку не засоряет."""
    updates = await repo.get_task_updates(db, task_id)
    if any((u["content"] or "") == text for u in updates):
        return
    await repo.add_task_update(db, task_id, "hub", "alert", text)
    await db.commit()


async def refuse_opening_over_review_limit(
    db: aiosqlite.Connection, task_id: int, task: dict[str, Any]
) -> None:
    """Держит вход новой работы при полной очереди review проекта (#1264).

    Стоит до любой записи на входе — плана, ветки, статуса — как отказ #1232
    рядом: задача, отказанная после них, несла бы следы работы, которой не
    начинали. Сбой самой проверки не держит вход: проверка, которая падает,
    остановила бы весь проект.
    """
    try:
        check = await check_review_queue(db, task)
    except Exception as exc:  # noqa: BLE001 - a gate that raises blocks everything
        log.warning("review queue check for #%s failed: %s", task_id, exc)
        return
    if check is None:
        return
    text = _card_text(check)
    await _note_once(db, task_id, text)
    if check.outcome != HELD:
        return
    raise HTTPException(
        409,
        detail=enrich_error_payload(
            {
                "reason": REVIEW_QUEUE_FULL,
                "actor_hint": "agent",
                "current_status": task.get("status", ""),
                "message": (
                    f"Task #{task_id} is not opened: the review queue of project "
                    f"'{check.project_slug}' holds {len(check.queue)} tasks, "
                    f"limit {check.limit}"
                ),
                "hint": text,
                "review_limit": check.limit,
                "in_review": len(check.queue),
                "queue": list(check.queue),
                "task_id": task_id,
            }
        ),
    )


async def run_allowed_by_review_limit(
    db: aiosqlite.Connection, task_id: int, done: str
) -> bool:
    """Может ли approve(run=true) запустить уже одобренную задачу.

    approve переводит задачу в running так же, как start и pair_start, — и
    лимит на нём тот же (#1264, находка a4cf336071a7be3b): что вход только
    человеческий, ничего не меняет, /start тоже только человеческий и
    держится. Вторая половина входа — одобрение — не теряется: задача
    остаётся open, карточка говорит, что запуск удержан. ``done`` — «одобрена».
    Сбой самой проверки запуск не держит.
    """
    row = await repo.get_task(db, task_id)
    if row is None:
        return True
    try:
        check = await check_review_queue(db, dict(row))
    except Exception as exc:  # noqa: BLE001 - a gate that raises blocks everything
        log.warning("review queue check for #%s failed: %s", task_id, exc)
        return True
    await note_run_check(db, task_id, check, done)
    return check is None or check.outcome != HELD


async def note_run_check(
    db: aiosqlite.Connection,
    task_id: int,
    check: ReviewQueueCheck | None,
    done: str,
) -> None:
    """Запись в карточку об исходе лимита на запускающем входе create/approve.

    expedite и warn пишут то же, что на pair_start и start; удержанный запуск
    говорит, что вторая половина входа выполнена, а запуск — нет.
    """
    if check is None:
        return
    if check.outcome != HELD:
        await _note_once(db, task_id, _card_text(check))
        return
    listed = ", ".join(f"#{q}" for q in check.queue)
    await _note_once(
        db,
        task_id,
        (
            f"Запуск удержан лимитом review ({check.limit}, сейчас "
            f"{len(check.queue)}) — задача {done}, но не запущена; выход: "
            f"expedite, режим warn или поднять K. Очередь проекта "
            f"«{check.project_slug}»: {listed} (#1264)."
        ),
    )
