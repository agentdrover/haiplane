"""Make pilot stoppages actionable through the existing role/outbox route."""

from __future__ import annotations

from typing import Any

import aiosqlite

from hub import repository as repo
from hub.db import fetchall, write_transaction
from hub.epic_plan_view import PlanDecision
from hub.services import epic_plan, watch_notices

OWNER_REASONS = frozenset(
    {
        "independent_review_unavailable",
        "pilot_outcome_needs_decision",
        "pilot_previous_order_unreconciled",
        "pilot_retry_exhausted",
        "pilot_fixes_exhausted",
        "pilot_risk_changed",
        "pilot_verdict_not_delegated",
        "pilot_review_unavailable",
        "pilot_spending_authority_unavailable",
        "plan_budget_exhausted",
        "spending_cap_breached",
    }
)


async def maybe_notify(
    db: aiosqlite.Connection, decision: PlanDecision, task: Any, reason: str
) -> bool:
    if reason not in OWNER_REASONS or task["status"] not in {
        "open",
        "running",
        "review",
    }:
        return False
    async with write_transaction(db):
        try:
            current = await epic_plan.active_decision(db)
        except epic_plan.PlanRefusal:
            return False
        if current.version != decision.version:
            return False
        task_id = int(task["id"])
        live = await repo.get_task(db, task_id)
        generation = int(task["submission_generation"] or 0)
        sha = str(task["submission_sha"] or "")
        if (
            live is None
            or live["status"] != task["status"]
            or live["submission_generation"] != generation
            or (live["submission_sha"] or "") != sha
        ):
            return False
        cursor = await db.execute(
            """INSERT OR IGNORE INTO epic_plan_escalations
               (task_id,plan_version,generation,sha,reason,resume_status) VALUES (?,?,?,?,?,?)""",
            (task_id, decision.version, generation, sha, reason, task["status"]),
        )
        if cursor.rowcount != 1:
            return False
        payload = {
            "plan_version": decision.version,
            "owner_principal_id": decision.approved_principal_id,
            "task_id": task_id,
            "generation": generation,
            "sha": sha,
            "reason": reason,
            "options": [
                {
                    "action": "retry",
                    "consequence": "Перепроверить ту же задачу и допуск по текущей версии. Действующие ограничения сохраняются.",
                },
                {
                    "action": "stop",
                    "consequence": "Остановить эту работу; сохранённые факты доступны владельцу.",
                },
            ],
            "recommendation": "stop",
        }
        await repo.update_task(db, task_id, status="needs_decision")
        event_id = await watch_notices.emit_needs_decision(
            db,
            task_id=task_id,
            actor="hub",
            payload=payload,
        )
        await db.execute(
            "UPDATE epic_plan_escalations SET event_id=? WHERE id=?",
            (event_id, cursor.lastrowid),
        )
        await repo.add_task_update(
            db,
            task_id,
            "hub",
            "alert",
            f"Пилот приостановил задачу по версии {decision.version[:12]}: {reason}. "
            "Варианты: retry — повторить проверку с действующими ограничениями, "
            "stop — оставить задачу на паузе. Рекомендация: stop.",
        )
    return True


async def owner_for_role_order(
    db: aiosqlite.Connection, order: Any
) -> tuple[bool, int | None]:
    """Use the signed principal for the existing role order sourced by this case."""
    if int(order["task_id"]) not in epic_plan.LEAVES:
        return False, None
    rows = await fetchall(
        db,
        """SELECT * FROM epic_plan_escalations
           WHERE task_id=? AND 'needs_decision:' || event_id=?""",
        (order["task_id"], order["source_key"]),
    )
    if not rows:
        return False, None
    case = rows[0]
    try:
        decision = await epic_plan.active_decision(db)
    except epic_plan.PlanRefusal:
        return True, None
    if (
        case["plan_version"] != decision.version
        or case["generation"] != order["generation"]
    ):
        return True, None
    task = await repo.get_task(db, int(case["task_id"]))
    if task is None or (task["submission_sha"] or "") != case["sha"]:
        return True, None
    return True, decision.approved_principal_id


async def resume(db: aiosqlite.Connection) -> None:
    """Consume one owner response from the durable outbox checkpoint."""
    from hub.services import agent_outbox

    async with write_transaction(db):
        cases = await fetchall(
            db,
            """SELECT e.id AS case_id, e.resume_status, o.* FROM epic_plan_escalations e
               JOIN role_orders r ON 'needs_decision:' || e.event_id=r.source_key
               JOIN agent_outbox o ON o.order_id=r.id AND o.task_id=e.task_id
               WHERE e.answer_event_id IS NULL AND o.answer_event_id IS NOT NULL
               ORDER BY o.answer_event_id LIMIT 1""",
        )
        if not cases:
            return
        answer = cases[0]
        order = await agent_outbox.live_order(db, answer["order_id"], answer["host"])
        if await agent_outbox.continuation_refusal(db, answer, order):
            return
        choice = str(answer["answer"] or "").strip().lower()
        if choice not in {"retry", "stop"}:
            return
        await db.execute(
            "UPDATE epic_plan_escalations SET answer_event_id=? WHERE id=?",
            (answer["answer_event_id"], answer["case_id"]),
        )
        if choice == "retry":
            await repo.update_task(
                db, int(answer["task_id"]), status=answer["resume_status"]
            )
        await repo.insert_event(
            db,
            kind="epic_plan_owner_answer_consumed",
            task_id=int(answer["task_id"]),
            actor="hub",
            payload={"answer_event_id": answer["answer_event_id"], "choice": choice},
        )
