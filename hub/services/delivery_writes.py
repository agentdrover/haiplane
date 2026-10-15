"""Тексты и записи исходов доставки — то, что отдаётся ``record_delivery_outcome`` (#1706).

Сами по себе эти функции одобрение НЕ проверяют: их вызывает только
``orchestration.record_delivery_outcome``, который читает одобрение заново
и пишет что-либо, лишь если оно ещё действует. Вызов мимо него — дефект, и
матричный тест ``tests/test_verdict_replay_guard.py`` ловит такую запись.
Вынесены из hub/poller.py: файл на потолке бюджета размера, а замыкания в
свипе доставки считались в его сложность.
"""

from __future__ import annotations

import logging
from typing import Any

import aiosqlite

from hub import repository as repo
from hub.services import watch_notices

log = logging.getLogger("hub.delivery_writes")


async def stop_on_stacking(
    db: aiosqlite.Connection, task: dict[str, Any], pr_num: Any, stacked: str
) -> None:
    """Headless-доставка остановлена основанием, которого никто не доставит."""
    await repo.add_task_update(
        db,
        task["id"],
        "hub",
        "blocker",
        f"Доставка остановлена: PR #{pr_num} — {stacked}. "
        "Ожидание здесь ничего не решит, поэтому задача передана "
        "человеку (REST POST /api/tasks/<id>/decide).",
    )
    await repo.update_task(db, task["id"], status="needs_decision")
    await watch_notices.emit_needs_decision(
        db,
        task_id=task["id"],
        actor="hub",
        payload={"reason": "stacking", "detail": stacked, "pr": pr_num},
    )
    await db.commit()
    log.warning(
        "Poll: task #%d delivery stopped, human needed (%s)", task["id"], stacked
    )


async def note_ci_failed(
    db: aiosqlite.Connection, task: dict[str, Any], pr_num: Any
) -> None:
    log.warning("Poll: task #%d CI failed on PR #%d", task["id"], pr_num)
    await repo.add_task_update(
        db,
        task["id"],
        "hub",
        "alert",
        f"CI failed on PR #{pr_num}. Manual check required.",
    )


async def hand_headless_to_human(
    db: aiosqlite.Connection, task: dict[str, Any], pr_num: Any, reason: str
) -> None:
    """Одобрено, но PR не влит: мерж без слияния не засчитывается (#363)."""
    await repo.add_task_update(
        db,
        task["id"],
        "hub",
        "blocker",
        f"Ревью одобрено, но PR #{pr_num} не влит "
        f"({reason}). Задача не может считаться "
        "выполненной, пока работа не в базовой ветке. "
        "Решите через REST POST /api/tasks/<id>/decide.",
    )
    await repo.update_task(db, task["id"], status="needs_decision")
    await watch_notices.emit_needs_decision(
        db,
        task_id=task["id"],
        actor="hub",
        payload={"reason": reason, "pr": pr_num},
    )
    await db.commit()
    log.warning(
        "Poll: task #%d approved but PR #%d not merged (%s) → needs_decision",
        task["id"],
        pr_num,
        reason,
    )


async def hand_pair_to_human(
    db: aiosqlite.Connection,
    task_id: int,
    pr_num: Any,
    detail: str,
    reason: str,
    resolver_reason: str,
    resubmit_hint: str,
) -> None:
    """Отказ гейта на pair-свипе: терминально, к человеку, с причиной."""
    if resolver_reason:
        await repo.add_task_update(db, task_id, "hub", "alert", resolver_reason)
    await repo.update_task(db, task_id, status="needs_decision")
    await repo.add_task_update(
        db,
        task_id,
        "hub",
        "alert",
        f"Ревью одобрено, но PR #{pr_num} не доставлен — {detail}. "
        "Задача не может считаться выполненной, пока работа не в базовой "
        "ветке. Решение за человеком (REST POST /api/tasks/<id>/decide)."
        + resubmit_hint,
    )
    await watch_notices.emit_needs_decision(
        db,
        task_id=task_id,
        actor="hub",
        payload={"reason": reason, "detail": detail, "via": "poller"},
    )
    await db.commit()
    log.info(
        "Poll: task #%d → needs_decision: merge gate refused (%s)", task_id, detail
    )
