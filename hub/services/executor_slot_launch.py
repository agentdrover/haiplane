"""Slot launch orchestration over the existing audited order transport."""

from __future__ import annotations

from typing import Any

import aiosqlite
from fastapi import HTTPException

from hub.db import write_transaction
from hub.services import epic_plan, project_policy
from hub.services import executor_skill_bundle as skills


async def launch_on_slot(
    db: aiosqlite.Connection,
    project: Any,
    *,
    host: str,
    task_id: int | None,
    issuer_principal_id: int,
    issuer_human: bool = False,
) -> Any:
    """Запуск исполнителя на слот хоста по нажатию человека (#1712).

    Проект в режиме manual; облако (github, наблюдение прав токена, модель)
    слоту не нужно. Прогон, код и заказ — ОДНА write-транзакция без внутренних
    commit: любой сбой до вставки заказа откатывает и код, и прогон.
    """
    from hub.services import chat_pair, executor_launch as el, executor_orders as orders

    if el.launch_mode_of(project_policy.gate_policy_of(project)) != el.LAUNCH_MANUAL:
        return el._refused(el.REASON_OFF)
    if await chat_pair.get_acting_agent(db) is None:
        return el._refused(f"{el.REASON_NO_ACTING_AGENT} (CHAT_PAIR_AGENT)")
    try:
        async with write_transaction(db):
            bundle = await skills.read_active_bundle(db)
            booked = await orders._book(db, project, host, task_id, bundle)
            if isinstance(booked, str):
                return el._refused(booked, task_id)
            await epic_plan.refuse_legacy_start(db, int(booked.task["id"]))
            order_id, row_id = await orders._issue_atomically(
                db,
                project,
                booked,
                issuer_principal_id=issuer_principal_id,
                issuer_human=issuer_human,
            )
    except skills.SkillBundleError as exc:
        return el._refused(str(exc), task_id)
    except aiosqlite.IntegrityError:
        # Второе соединение заняло последнее место раньше: триггер слотов хоста.
        return el._refused(f"{orders.REASON_SLOT_BUSY}: {host}", task_id)
    except orders.HostError as exc:
        # Сохранённый ключ хоста не годится: код не выписан, ничего не осталось.
        return el._refused(f"{orders.REASON_HOST_OFF}: {exc.detail}", task_id)
    except epic_plan.PlanRefusal as exc:
        return el._refused(
            epic_plan.format_start_blocker(exc.details),
            exc.details.get("task_id", task_id),
        )
    except HTTPException as exc:
        # Задача ушла из running между проверкой и возвратом: откат уже сделан.
        return el._refused(f"{orders.REASON_NOT_OPEN}: {exc.detail}", task_id)
    return el.LaunchResult(
        True, "", int(booked.task["id"]), row_id=row_id, order_id=order_id, host=host
    )
