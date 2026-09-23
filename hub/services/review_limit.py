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
    task_id = int(task["id"])
    project = await repo.resolve_project_for_task(db, task_id)
    if project is None:
        return None
    policy = project_policy.gate_policy_of(project)
    limit = project_policy.review_limit_of(policy)
    if limit is None:
        return None
    queue = await repo.list_review_task_ids_in_project(db, int(project["id"]))
    if len(queue) < limit:
        return None
    if task.get("class_of_service") == "expedite":
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
