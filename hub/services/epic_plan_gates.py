"""Fresh signed authority, CI and independent-review gates for pilot work."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

import aiosqlite

from hub import repository as repo
from hub.db import fetchall, write_transaction
from hub.epic_plan_view import PlanDecision
from hub.services import (
    admin,
    chat_pair,
    ci_contract,
    ci_trust,
    epic_plan,
    epic_plan_authority as authority,
    epic_plan_spend as spend,
    epic_plan_transport as transport,
    project_policy,
    verdict_owner,
)


async def _one(db: aiosqlite.Connection, query: str, values: tuple) -> Any:
    rows = await fetchall(db, query, values)
    return rows[0] if rows else None


async def execution(db: aiosqlite.Connection, task: Any) -> Any:
    return await _one(
        db,
        """SELECT * FROM epic_plan_work WHERE task_id=? AND generation=?
           AND phase='executor' ORDER BY attempt DESC LIMIT 1""",
        (task["id"], task["submission_generation"]),
    )


async def managed(db: aiosqlite.Connection, task_id: int) -> bool:
    return task_id in epic_plan.LEAVES and await epic_plan._under_pilot(db, task_id)


async def by_order_refusal(
    db: aiosqlite.Connection, order_id: int, *, enforce_grant: bool = True
) -> str:
    work = await _one(
        db,
        "SELECT plan_version,grant_expires_at,charged_cents FROM epic_plan_work WHERE executor_order_id=?",
        (order_id,),
    )
    if work is None:
        return ""
    try:
        current = await epic_plan.active_decision(db)
    except epic_plan.PlanRefusal as exc:
        return exc.reason
    if current.version != work["plan_version"]:
        return "plan_version_moved"
    if not enforce_grant:
        return ""
    if datetime.fromisoformat(work["grant_expires_at"]) <= datetime.now(UTC):
        return "spending_grant_expired"
    if work["charged_cents"] is not None:
        return "spending_grant_settled"
    return ""


async def _scope(
    db: aiosqlite.Connection, task: Any
) -> tuple[PlanDecision | None, Any, str]:
    try:
        current = await epic_plan.active_decision(db)
    except epic_plan.PlanRefusal as exc:
        return None, None, exc.reason
    if "order_bounded_independent_review" not in current.snapshot.allowed_actions:
        return current, None, "pilot_review_not_authorized"
    work = await execution(db, task)
    if work is None or work["plan_version"] != current.version:
        return current, None, "pilot_execution_not_bound"
    if not task["submission_sha"] or task["submission_sha"] == work["sha"]:
        return current, None, "pilot_new_sha_required"
    return current, work, ""


async def _trusted_ci(db: aiosqlite.Connection, task: Any) -> bool:
    report = await ci_trust.trusted_report(db, int(task["id"]), task["submission_sha"])
    if report is None:
        return False
    contract = await ci_contract.contract_for_task(db, int(task["id"]))
    return ci_trust.green_gap(dict(report), contract) is None


@dataclass(frozen=True)
class ReviewBlocker:
    reason: str
    recipient: str
    next_action: str
    decision: PlanDecision | None = None


async def review_blocker(
    db: aiosqlite.Connection, task: Any, project: Any
) -> ReviewBlocker | None:
    if not await managed(db, int(task["id"])):
        return None
    if not project_policy.review_dispatch_enabled(
        project_policy.gate_policy_of(project)
    ):
        return ReviewBlocker(
            "pilot_review_policy_disabled", "project_owner", "enable_independent_review"
        )
    if transport.review_transport() is None:
        return ReviewBlocker(
            "pilot_metered_transport_unavailable",
            "operator",
            "restore_metered_review_transport",
        )
    decision, _, reason = await _scope(db, task)
    if reason:
        if reason == "pilot_new_sha_required":
            return ReviewBlocker(reason, "author", "submit_new_sha")
        return ReviewBlocker(reason, "project_owner", "check_signed_plan_and_execution")
    if not task["risk_class"]:
        return ReviewBlocker(
            "pilot_risk_unverified", "operator", "verify_submission_risk"
        )
    if task["risk_class"] not in {"R0", "R1", "R2"}:
        return ReviewBlocker(
            "pilot_risk_changed", "plan_owner", "decide_on_changed_risk", decision
        )
    if not await _trusted_ci(db, task):
        return ReviewBlocker("pilot_ci_unverified", "ci", "wait_for_trusted_ci_on_sha")
    return None


async def can_prepare_review(db: aiosqlite.Connection, task: Any) -> bool:
    project = await repo.resolve_project_for_task(db, int(task["id"]))
    return await review_blocker(db, task, project) is None


async def note_review_blocker(
    db: aiosqlite.Connection, task: Any, blocker: ReviewBlocker
) -> None:
    """One actionable notice per pinned submission and cause."""
    if blocker.decision is not None:
        from hub.services import epic_plan_escalation

        await epic_plan_escalation.maybe_notify(
            db,
            blocker.decision,
            task,
            blocker.reason,
            recipient=blocker.recipient,
            next_action=blocker.next_action,
        )
        return
    task_id = int(task["id"])
    generation, sha = task["submission_generation"], task["submission_sha"]
    payload = {
        "generation": generation,
        "sha": sha,
        "reason": blocker.reason,
        "recipient": blocker.recipient,
        "next_action": blocker.next_action,
    }
    async with write_transaction(db):
        live = await repo.get_task(db, task_id)
        if (
            live is None
            or live["status"] != "review"
            or live["submission_generation"] != generation
            or live["submission_sha"] != sha
        ):
            return
        marker = await db.execute(
            """INSERT OR IGNORE INTO epic_plan_review_notice_keys
               (task_id,generation,sha,reason) VALUES (?,?,?,?)""",
            (task_id, generation, sha, blocker.reason),
        )
        if marker.rowcount != 1:
            return
        await repo.insert_event(
            db,
            kind="epic_plan_review_blocked",
            task_id=task_id,
            actor="hub",
            payload=payload,
        )
        await repo.add_task_update(
            db,
            task_id,
            "hub",
            "status" if blocker.recipient == "ci" else "alert",
            f"Ревью пилота ожидает: {blocker.reason}. "
            f"Адресат: {blocker.recipient}. Следующее действие: {blocker.next_action}.",
        )


async def independent_reviewer(
    db: aiosqlite.Connection, principal_id: int | None, excluded: set[int]
) -> bool:
    if principal_id is None or principal_id in excluded:
        return False
    reviewer = await admin.get_principal(db, principal_id)
    if not reviewer or reviewer["kind"] != "agent" or reviewer["status"] != "active":
        return False
    perms = await admin.get_principal_permissions(db, principal_id)
    return "tasks.read" in perms and "tasks.human_gate" not in perms


@dataclass(frozen=True)
class ReviewPermit:
    id: int
    grant: authority.SpendingGrant
    reconcile: bool = False


async def _reviewer_still_independent(
    db: aiosqlite.Connection, decision: PlanDecision, work: Any, reviewer: int
) -> bool:
    acting = await chat_pair.get_acting_agent(db)
    excluded = {
        work["principal_id"],
        work["actor_principal_id"],
        decision.approved_principal_id,
    }
    if acting is not None:
        excluded.add(int(acting["id"]))
    return await independent_reviewer(db, reviewer, excluded)


async def reserve_review(
    db: aiosqlite.Connection,
    task: Any,
    *,
    channel: Literal["cloud", "local"],
    model: str,
    principal_id: int | None,
    attempt: int,
) -> tuple[bool, ReviewPermit | None]:
    """Reserve cap for one identifiable provider attempt, even across restart."""
    if not await managed(db, int(task["id"])):
        return True, None
    if channel != "cloud" or transport.review_transport() is None:
        return False, None
    decision, work, reason = await _scope(db, task)
    if decision is None or reason or not await can_prepare_review(db, task):
        return False, None
    if principal_id is None or not await _reviewer_still_independent(
        db, decision, work, principal_id
    ):
        await note_review_blocker(
            db,
            task,
            ReviewBlocker(
                "pilot_independent_reviewer_unavailable",
                "operator",
                "restore_independent_reviewer_principal",
            ),
        )
        return False, None
    if not 1 <= attempt <= decision.snapshot.terms.technical_retries + 1:
        return False, None
    binding = authority.WorkBinding(
        phase="review",
        project=epic_plan.PROJECT,
        plan_version=decision.version,
        task_id=int(task["id"]),
        generation=int(task["submission_generation"]),
        sha=task["submission_sha"],
        host=channel,
        model=model,
        principal_id=principal_id,
        actor_principal_id=principal_id,
        attempt=attempt,
    )
    grant = await authority.get_grant(binding)
    if grant is None or not grant.covers(binding):
        await note_review_blocker(
            db,
            task,
            ReviewBlocker(
                "pilot_spending_authority_unavailable",
                "operator",
                "restore_spending_authority",
            ),
        )
        return False, None
    return await _write_review_reservation(db, grant)


async def _write_review_reservation(
    db: aiosqlite.Connection, grant: authority.SpendingGrant
) -> tuple[bool, ReviewPermit | None]:
    binding = grant.binding
    async with write_transaction(db):
        task = await repo.get_task(db, binding.task_id)
        if (
            task is None
            or task["status"] != "review"
            or task["submission_sha"] != binding.sha
            or task["submission_generation"] != binding.generation
        ):
            return False, None
        decision, work, reason = await _scope(db, task)
        if (
            decision is None
            or reason
            or task["risk_class"] not in {"R0", "R1", "R2"}
            or not await _trusted_ci(db, task)
            or await _steward_stop(db, task)
            or not await _reviewer_still_independent(
                db, decision, work, binding.principal_id
            )
        ):
            return False, None
        previous = await _one(
            db,
            """SELECT * FROM epic_plan_work WHERE task_id=? AND generation=?
               AND phase='review' ORDER BY attempt DESC LIMIT 1""",
            (binding.task_id, binding.generation),
        )
        if previous is not None:
            if previous["attempt"] == binding.attempt:
                same = (
                    previous["review_dispatch_id"] is None
                    and previous["authority_ref"] == grant.reference
                    and previous["ceiling_cents"] == grant.ceiling_cents
                    and spend.binding_of(previous) == binding
                )
                return (
                    (True, ReviewPermit(previous["id"], grant, reconcile=True))
                    if same
                    else (False, None)
                )
            if (
                previous["attempt"] > binding.attempt
                or previous["charged_cents"] is None
            ):
                return False, None
        if refusal := await spend.refusal(db, decision, grant):
            from hub.services import epic_plan_escalation

            await epic_plan_escalation.maybe_notify(db, decision, task, refusal)
            return False, None
        return True, ReviewPermit(await spend.record(db, grant), grant)


async def attach_dispatch(
    db: aiosqlite.Connection, reservation: ReviewPermit | None, dispatch_id: int
) -> None:
    if reservation is not None:
        await db.execute(
            "UPDATE epic_plan_work SET review_dispatch_id=? WHERE id=?",
            (dispatch_id, reservation.id),
        )


async def _clean_independent_review(db: aiosqlite.Connection, task: Any) -> bool:
    row = await _one(
        db,
        """SELECT m.* FROM machine_reviews m JOIN epic_plan_work w
           ON w.review_dispatch_id=m.dispatch_id AND w.principal_id=m.principal_id
           AND w.task_id=m.task_id AND w.generation=m.submission_generation
           AND w.sha=m.reviewed_sha
           WHERE w.task_id=? AND w.generation=? AND w.sha=?
           ORDER BY m.id DESC LIMIT 1""",
        (task["id"], task["submission_generation"], task["submission_sha"]),
    )
    if row is None or row["incomplete"] != 0:
        return False
    try:
        return (
            json.loads(row["findings_confirmed"]) == []
            and json.loads(row["unresolved"]) == []
        )
    except (TypeError, ValueError):
        return False


async def _steward_stop(db: aiosqlite.Connection, task: Any) -> bool:
    task_id, generation = int(task["id"]), int(task["submission_generation"] or 0)
    judgement = await repo.get_steward_judgement(db, task_id, generation, "verdict")
    if judgement is not None and judgement["verdict"] in {
        "changes_requested",
        "escalate",
    }:
        return True
    advisor = await repo.get_steward_judgement(db, task_id, generation, "advisor")
    return advisor is not None and advisor["verdict"] == "object"


async def approval_refusal(
    db: aiosqlite.Connection, task: Any, *, automatic: bool
) -> str:
    if not await managed(db, int(task["id"])):
        return ""
    decision, _, reason = await _scope(db, task)
    if decision is None or reason:
        return reason
    if not await _trusted_ci(db, task):
        return "pilot_ci_unverified"
    if await _steward_stop(db, task):
        return "pilot_steward_stop"
    if not await _clean_independent_review(db, task):
        return "pilot_review_unavailable"
    if task["risk_class"] not in {"R0", "R1", "R2"}:
        return "pilot_risk_changed"
    if automatic:
        if verdict_owner.written_owner(dict(task))[0] == verdict_owner.OWNER_HUMAN:
            return "pilot_verdict_reserved_for_owner"
        policy = await project_policy.gate_policy_for_task(db, int(task["id"]))
        if (
            not project_policy.verdict_is_delegated(policy)
            or "delegate_clean_r0_r2_verdict" not in decision.snapshot.allowed_actions
        ):
            return "pilot_verdict_not_delegated"
    return ""


async def ready_to_send(
    db: aiosqlite.Connection, reservation: ReviewPermit | None
) -> bool:
    if reservation is None:
        return True
    async with write_transaction(db):
        row = await _one(
            db, "SELECT * FROM epic_plan_work WHERE id=?", (reservation.id,)
        )
        if row is None or row["review_dispatch_id"] is not None:
            return False
        if row["charged_cents"] is not None or datetime.fromisoformat(
            row["grant_expires_at"]
        ) <= datetime.now(UTC):
            return False
        task = await repo.get_task(db, row["task_id"])
        if (
            task is None
            or task["status"] != "review"
            or task["submission_generation"] != row["generation"]
            or task["submission_sha"] != row["sha"]
        ):
            return False
        decision, work, reason = await _scope(db, task)
        if (
            decision is None
            or reason
            or decision.version != row["plan_version"]
            or task["risk_class"] not in {"R0", "R1", "R2"}
            or not await _trusted_ci(db, task)
            or await _steward_stop(db, task)
            or not await _reviewer_still_independent(
                db, decision, work, row["principal_id"]
            )
        ):
            return False
        if reservation.reconcile:
            attempt = await db.execute(
                "UPDATE epic_plan_work SET send_count=2 WHERE id=? AND send_count=1",
                (reservation.id,),
            )
            return attempt.rowcount == 1
        return True
