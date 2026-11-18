"""Cap reservations shared by execution and independent review in #2373."""

from __future__ import annotations

import json
from typing import Any

import aiosqlite

from hub.db import fetchall, write_transaction
from hub.epic_plan_view import PlanDecision
from hub.services import epic_plan_authority as authority


async def usage(db: aiosqlite.Connection, task_id: int) -> tuple[int, int, bool]:
    rows = await fetchall(
        db,
        """SELECT
             COALESCE(SUM(COALESCE(charged_cents,ceiling_cents)),0) AS total,
             COALESCE(SUM(CASE WHEN task_id=?
                  THEN COALESCE(charged_cents,ceiling_cents) ELSE 0 END),0) AS leaf,
             COALESCE(MAX(charged_cents>ceiling_cents),0) AS exceeded
           FROM epic_plan_work""",
        (task_id,),
    )
    return int(rows[0]["total"]), int(rows[0]["leaf"]), bool(rows[0]["exceeded"])


async def refusal(
    db: aiosqlite.Connection,
    decision: PlanDecision,
    grant: authority.SpendingGrant,
) -> str:
    """Read spend while holding the caller's reservation lock."""
    if (
        not grant.covers(grant.binding)
        or grant.binding.plan_version != decision.version
    ):
        return "spending_grant_stale"
    total, leaf, exceeded = await usage(db, grant.binding.task_id)
    if exceeded:
        return "spending_cap_breached"
    budget = decision.snapshot
    project_ceiling = budget.project_leaf_ceiling_cents
    if project_ceiling is None:
        return "project_budget_unverified"
    cap = min(budget.terms.leaf_cents, project_ceiling)
    if (
        leaf + grant.ceiling_cents > cap
        or total + grant.ceiling_cents > budget.terms.total_cents
    ):
        return "plan_budget_exhausted"
    return ""


async def record(
    db: aiosqlite.Connection,
    grant: authority.SpendingGrant,
    *,
    executor_order_id: int | None = None,
    actor_principal_id: int | None = None,
    repair_causes: tuple[str, ...] = (),
) -> int:
    binding = grant.binding
    cursor = await db.execute(
        """INSERT INTO epic_plan_work (
            task_id,plan_version,phase,generation,sha,attempt,host,model,
            principal_id,actor_principal_id,authority_source,authority_ref,
            grant_expires_at,ceiling_cents,executor_order_id,repair_causes
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            binding.task_id,
            binding.plan_version,
            binding.phase,
            binding.generation,
            binding.sha,
            binding.attempt,
            binding.host,
            binding.model,
            binding.principal_id,
            actor_principal_id or binding.principal_id,
            authority.configured_source_fingerprint(),
            grant.reference,
            grant.expires_at.isoformat(),
            grant.ceiling_cents,
            executor_order_id,
            json.dumps(repair_causes),
        ),
    )
    if cursor.lastrowid is None:
        raise RuntimeError("missing pilot reservation identifier")
    return int(cursor.lastrowid)


def binding_of(row: Any) -> authority.WorkBinding:
    return authority.WorkBinding(
        task_id=row["task_id"],
        plan_version=row["plan_version"],
        phase=row["phase"],
        generation=row["generation"],
        sha=row["sha"],
        attempt=row["attempt"],
        host=row["host"],
        model=row["model"],
        principal_id=row["principal_id"],
        project="receipt-split-calculator",
    )


async def settle(db: aiosqlite.Connection, row: Any) -> None:
    """Only a final charge from the configured cost authority releases a hold."""
    if (
        row["charged_cents"] is not None
        or not row["authority_source"]
        or row["authority_source"] != authority.configured_source_fingerprint()
    ):
        return
    received = await authority.get_charge(binding_of(row), row["authority_ref"])
    if received is None:
        return
    async with write_transaction(db):
        await db.execute(
            "UPDATE epic_plan_work SET charged_cents=? WHERE id=? AND charged_cents IS NULL",
            (received.charged_cents, row["id"]),
        )
