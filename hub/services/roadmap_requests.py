"""Explicit roadmap orders under a task-level owner mandate."""

from __future__ import annotations

import json
from collections.abc import Sequence
from contextlib import asynccontextmanager
from secrets import compare_digest, token_hex
from typing import Any, NoReturn

import aiosqlite
from fastapi import HTTPException

from hub import repository as repo
from hub.config import TokenIdentity
from hub.db import fetchall, write_transaction
from hub.services import admin, roadmap_decisions

SOURCE = "steward_request"
_ACTIVE_PLAN_STATES = ("open", "claimed", "running", "needs_info", "needs_decision")


def _conflict(reason: str, message: str) -> NoReturn:
    raise HTTPException(409, {"reason": reason, "message": message})


async def _one(
    db: aiosqlite.Connection, sql: str, params: Sequence[Any]
) -> aiosqlite.Row | None:
    rows = await fetchall(db, sql, params)
    return rows[0] if rows else None


async def _descends_from(db: aiosqlite.Connection, task_id: int, plan_id: int) -> bool:
    for _ in range(20):
        if task_id == plan_id:
            return True
        task = await repo.get_task(db, task_id)
        if task is None or task["archived"] or task["parent_id"] is None:
            break
        task_id = int(task["parent_id"])
    return False


async def _plan_snapshot(
    db: aiosqlite.Connection,
    task_id: int,
    plan_id: int,
    pinned_approval: int = 0,
) -> tuple[int, int, int, int] | None:
    plan = await repo.get_task(db, plan_id)
    task = await repo.get_task(db, task_id)
    if (
        plan is None
        or plan["archived"]
        or plan["status"] not in _ACTIVE_PLAN_STATES
        or task is None
        or task["archived"]
        or task["status"] not in _ACTIVE_PLAN_STATES
    ):
        return None
    project = await repo.resolve_bound_project(db, task_id)
    plan_project = await repo.resolve_bound_project(db, plan_id)
    if (
        project is None
        or project["status"] != "active"
        or plan_project is None
        or project["id"] != plan_project["id"]
        or not await _descends_from(db, task_id, plan_id)
    ):
        return None
    if await _one(
        db,
        "SELECT 1 FROM private_project_security WHERE project_id = ?",
        (project["id"],),
    ):
        return None
    approval = await _one(
        db,
        "SELECT COALESCE(MAX(id), ?) AS id FROM events "
        "WHERE task_id = ? AND kind = 'task_approved' AND actor = 'human' AND id > ?",
        (pinned_approval, plan_id, pinned_approval),
    )
    if approval is None or not approval["id"]:
        return None
    return (
        int(project["id"]),
        int(approval["id"]),
        int(plan["statement_generation"]),
        int(task["statement_generation"]),
    )


async def _active_principal(
    db: aiosqlite.Connection, principal_id: int, role: str
) -> bool:
    return (
        await _one(
            db,
            "SELECT 1 FROM principals p JOIN principal_roles r ON r.principal_id = p.id "
            "JOIN roles k ON k.id = r.role_id "
            "WHERE p.id = ? AND p.status = 'active' AND k.slug = ?",
            (principal_id, role),
        )
        is not None
    )


async def _active_grant(db: aiosqlite.Connection, task_id: int) -> aiosqlite.Row | None:
    return await _one(
        db,
        "SELECT * FROM roadmap_grants WHERE task_id = ? AND revoked_at IS NULL",
        (task_id,),
    )


async def _audit(
    db: aiosqlite.Connection, who: TokenIdentity, verb: str, task_id: int, detail: str
) -> None:
    await admin.write_audit(
        db,
        actor_id=who.principal_id,
        action=verb,
        target_type="task",
        target_id=str(task_id),
        summary=f"{who.username}: {verb} for task #{task_id}",
        detail=detail,
        commit=False,
    )


async def _signal_task_is_current(
    db: aiosqlite.Connection,
    project_id: int,
    source_task_id: int,
) -> bool:
    task = await repo.get_task(db, source_task_id)
    project = await repo.resolve_bound_project(db, source_task_id)
    return (
        task is not None
        and not task["archived"]
        and project is not None
        and project["id"] == project_id
    )


_SIGNAL_FILTER = (
    "o.task_id = ? AND o.state = 'closed' AND o.close_reason = 'redeem_failed' "
    "AND o.report_outcome = 'failed' AND o.report_reason_code = 'redeem_failed'"
)


# A one-shot pilot can consult only the executor order recorded in its
# activation receipt. The ordinary administrative grant has no such receipt.
_PILOT_ORDER_SCOPE = (
    "NOT EXISTS (SELECT 1 FROM roadmap_activation_receipts a WHERE a.grant_id=? "
    "AND (a.executor_order_id IS NULL OR a.executor_order_id<>o.id "
    "OR o.id<=a.executor_high_water))"
)


async def _signal_is_current(
    db: aiosqlite.Connection,
    grant: aiosqlite.Row,
    source_order_id: int | None,
) -> bool:
    source_task = grant["signal_task_id"]
    if source_task is None or source_order_id is None:
        return source_task is None and source_order_id is None
    return (
        await _signal_task_is_current(db, grant["project_id"], source_task)
        and await _one(
            db,
            "SELECT 1 FROM executor_orders o WHERE o.id = ? AND "  # nosec B608
            + _SIGNAL_FILTER
            + " AND "
            + _PILOT_ORDER_SCOPE,
            (source_order_id, source_task, grant["id"]),
        )
        is not None
    )


_SOURCE_ALREADY_CONSULTED = (
    "SELECT 1 FROM roadmap_requests r "
    "JOIN roadmap_grants previous ON previous.id = r.grant_id "
    "JOIN roadmap_grants existing ON existing.id = ? "
    "WHERE previous.project_id = existing.project_id AND previous.plan_id = existing.plan_id "
    "AND previous.task_id = existing.task_id AND previous.approval_id = existing.approval_id "
    "AND r.source_order_id = "
)


async def _source_already_consulted(
    db: aiosqlite.Connection,
    grant_id: int,
    source_order_id: int,
) -> bool:
    return (
        await _one(db, _SOURCE_ALREADY_CONSULTED + "?", (grant_id, source_order_id))
        is not None
    )


async def _signals(
    db: aiosqlite.Connection,
    grant: aiosqlite.Row,
    after_order_id: int,
) -> list[dict[str, Any]]:
    task_id = grant["signal_task_id"]
    if task_id is None:
        return []
    if not await _signal_task_is_current(db, grant["project_id"], task_id):
        raise HTTPException(409, "the grant signal task is stale")
    rows = await fetchall(
        db,
        "SELECT o.task_id, o.id AS executor_order_id, o.state, "  # nosec B608
        "o.report_outcome AS outcome, o.report_reason_code AS reason_code "
        "FROM executor_orders o WHERE "
        + _SIGNAL_FILTER
        + " AND o.id > ? AND "
        + _PILOT_ORDER_SCOPE
        + " AND NOT EXISTS ("
        + _SOURCE_ALREADY_CONSULTED
        + "o.id) ORDER BY o.id LIMIT 20",
        (task_id, after_order_id, grant["id"], grant["id"]),
    )
    return [dict(row) for row in rows]


async def _require_grantor(
    db: aiosqlite.Connection,
    who: TokenIdentity,
    project_id: int,
) -> None:
    if (
        who.role not in ("admin", "super_admin")
        or who.auth_source not in ("db_api_key", "db_session", "env")
        or not await _key_in_project(db, who, project_id)
    ):
        raise HTTPException(
            403, "roadmap grant requires an authenticated administrator for the project"
        )


async def _require_task_grantor(
    db: aiosqlite.Connection, who: TokenIdentity, task_id: int
) -> None:
    project = await repo.resolve_bound_project(db, task_id)
    if project is None:
        raise HTTPException(404, "roadmap project not found")
    await _require_grantor(db, who, project["id"])


async def _store_verified_grant(
    db: aiosqlite.Connection,
    snapshot: tuple[int, int, int, int],
    *,
    task_id: int,
    plan_id: int,
    steward_id: int,
    roadmap_id: int,
    signal_task_id: int | None,
    ttl_days: int,
) -> int:
    """Persist a verified mandate inside its caller's write transaction."""
    project_id, approval_id, generation, task_generation = snapshot
    checks = await roadmap_decisions.approved_checks(
        db, plan_id, approval_id, generation
    )
    cursor = await db.execute(
        "INSERT INTO roadmap_grants "
        "(project_id, plan_id, task_id, approval_id, plan_generation, task_generation, steward_id, roadmap_id, signal_task_id, expires_at, decision_checks_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now', ?), ?)",
        (
            project_id,
            plan_id,
            task_id,
            approval_id,
            generation,
            task_generation,
            steward_id,
            roadmap_id,
            signal_task_id,
            f"+{ttl_days} days",
            json.dumps(checks, sort_keys=True),
        ),
    )
    return int(cursor.lastrowid or 0)


async def grant(
    db: aiosqlite.Connection,
    who: TokenIdentity,
    task_id: int,
    plan_id: int,
    steward_id: int,
    roadmap_id: int,
    signal_task_id: int | None,
    ttl_days: int,
) -> dict[str, int]:
    async with write_transaction(db):
        await _require_task_grantor(db, who, task_id)
        snapshot = await _plan_snapshot(db, task_id, plan_id)
        if snapshot is None:
            raise HTTPException(
                409, "task must belong to an active, human-approved plan"
            )
        if not await _active_principal(db, steward_id, "roadmap_steward"):
            raise HTTPException(422, "steward principal is not active")
        if not await _active_principal(db, roadmap_id, "agent"):
            raise HTTPException(422, "roadmap principal is not active")
        project_id, approval_id, generation, task_generation = snapshot
        if signal_task_id is not None and not await _signal_task_is_current(
            db, project_id, signal_task_id
        ):
            raise HTTPException(422, "signal task must belong to the grant project")
        active = await _active_grant(db, task_id)
        if active is not None:
            raise HTTPException(409, "revoke the active grant before granting again")
        grant_id = await _store_verified_grant(
            db,
            snapshot,
            task_id=task_id,
            plan_id=plan_id,
            steward_id=steward_id,
            roadmap_id=roadmap_id,
            signal_task_id=signal_task_id,
            ttl_days=ttl_days,
        )
        await _audit(
            db,
            who,
            "roadmap_granted",
            task_id,
            f"grant={grant_id} project={project_id} plan={plan_id} approval={approval_id} revision={generation} target_revision={task_generation} steward={steward_id} roadmap={roadmap_id} signal_task={signal_task_id} ttl_days={ttl_days}",
        )
        return {
            "grant_id": grant_id,
            "project_id": project_id,
            "plan_id": plan_id,
            "approval_id": approval_id,
        }


async def revoke(
    db: aiosqlite.Connection, who: TokenIdentity, task_id: int
) -> dict[str, bool]:
    async with write_transaction(db):
        await _require_task_grantor(db, who, task_id)
        active = await _active_grant(db, task_id)
        if active is None:
            return {"revoked": False}
        await _require_grantor(db, who, active["project_id"])
        await db.execute(
            "UPDATE roadmap_grants SET revoked_at = datetime('now') WHERE id = ?",
            (active["id"],),
        )
        await _audit(db, who, "roadmap_revoked", task_id, f"grant={active['id']}")
        return {"revoked": True}


async def _current(db: aiosqlite.Connection, row: aiosqlite.Row) -> bool:
    return (
        row["revoked_at"] is None
        and await _one(db, "SELECT 1 WHERE datetime('now') < ?", (row["expires_at"],))
        is not None
        and await _plan_snapshot(db, row["task_id"], row["plan_id"], row["approval_id"])
        == (
            row["project_id"],
            row["approval_id"],
            row["plan_generation"],
            row["task_generation"],
        )
        and await _active_principal(db, row["steward_id"], "roadmap_steward")
        and await _active_principal(db, row["roadmap_id"], "agent")
    )


async def _queue_order(db: aiosqlite.Connection, row: aiosqlite.Row) -> int:
    from hub.services import role_orders

    if await _one(
        db,
        "SELECT 1 FROM role_orders WHERE task_id = ? AND source_key LIKE 'steward_request:%' "
        "AND state IN ('open', 'leased', 'started')",
        (row["task_id"],),
    ):
        _conflict("roadmap_target_busy", "task already has an active roadmap request")
    result = await role_orders.order_role(
        db,
        role=role_orders.ROLE_ROADMAP,
        source_key=f"{SOURCE}:{token_hex(16)}",
        task_id=row["task_id"],
        project_id=row["project_id"],
    )
    if result["status"] == "quota":
        raise HTTPException(429, "roadmap order quota exhausted")
    return int(result["id"])


async def _make_order(
    db: aiosqlite.Connection,
    row: aiosqlite.Row,
    request_id: str,
    question: str,
    source_order_id: int | None,
) -> int:
    order_id = await _queue_order(db, row)
    await db.execute(
        "INSERT INTO roadmap_requests (grant_id, request_id, question, source_order_id, order_id) "
        "VALUES (?, ?, ?, ?, ?)",
        (row["id"], request_id, question, source_order_id, order_id),
    )
    return order_id


async def _key_in_project(
    db: aiosqlite.Connection, who: TokenIdentity, project_id: int
) -> bool:
    if who.scopes_damaged:
        return False
    if not who.scopes:
        return True
    project = await repo.get_project(db, project_id)
    return project is not None and project["slug"] in who.scopes


async def _steward_grant(
    db: aiosqlite.Connection,
    who: TokenIdentity,
    task_id: int,
) -> aiosqlite.Row:
    if (
        who.role != "roadmap_steward"
        or who.auth_source != "db_api_key"
        or who.principal_id is None
    ):
        raise HTTPException(403, "a dedicated roadmap steward credential is required")
    row = await _active_grant(db, task_id)
    if row is None or row["steward_id"] != who.principal_id:
        raise HTTPException(404, "roadmap grant not found")
    if not await _key_in_project(db, who, row["project_id"]):
        raise HTTPException(404, "roadmap grant not found")
    if not await _current(db, row):
        _conflict("roadmap_grant_stale", "roadmap grant is stale")
    return row


@asynccontextmanager
async def _read_snapshot(db: aiosqlite.Connection):
    if db.in_transaction:
        yield
        return
    await db.execute("BEGIN")
    try:
        yield
    finally:
        await db.rollback()


async def grant_signal(
    db: aiosqlite.Connection,
    who: TokenIdentity,
    task_id: int,
    after_order_id: int,
) -> dict[str, Any]:
    async with _read_snapshot(db):
        row = await _steward_grant(db, who, task_id)
        project = await repo.get_project(db, row["project_id"])
        if project is None:
            raise HTTPException(404, "roadmap project not found")
        return {
            "grant_id": row["id"],
            "project": project["slug"],
            "plan_id": row["plan_id"],
            "approval_id": row["approval_id"],
            "task_id": task_id,
            "signals": await _signals(db, row, after_order_id),
            "pending_decision_order_id": await roadmap_decisions.pending_order_id(
                db, row["id"]
            ),
        }


async def issue(
    db: aiosqlite.Connection,
    who: TokenIdentity,
    task_id: int,
    plan_id: int,
    grant_id: int,
    request_id: str,
    question: str,
    source_order_id: int | None,
) -> dict[str, Any]:
    async with write_transaction(db):
        row = await _steward_grant(db, who, task_id)
        if row["plan_id"] != plan_id:
            raise HTTPException(404, "roadmap grant not found")
        if row["id"] != grant_id:
            _conflict("roadmap_grant_stale", "roadmap grant was replaced")
        if not await _signal_is_current(db, row, source_order_id):
            _conflict(
                "roadmap_source_invalid",
                "source order is outside the grant or not an eligible technical signal",
            )
        previous = await _one(
            db,
            "SELECT order_id, question, source_order_id FROM roadmap_requests "
            "WHERE grant_id = ? AND request_id = ?",
            (row["id"], request_id),
        )
        if previous is not None:
            if (
                previous["question"] != question
                or previous["source_order_id"] != source_order_id
            ):
                _conflict(
                    "roadmap_request_conflict",
                    "request identifier already used for another request",
                )
            return {"order_id": previous["order_id"], "repeat": True}
        if source_order_id is not None and await _source_already_consulted(
            db,
            grant_id,
            source_order_id,
        ):
            _conflict(
                "roadmap_source_already_requested",
                "source order already has a roadmap request",
            )
        order_id = await _make_order(db, row, request_id, question, source_order_id)
        await _audit(
            db,
            who,
            "roadmap_requested",
            task_id,
            f"grant={row['id']} plan={plan_id} order={order_id} source_order={source_order_id}",
        )
        return {"order_id": order_id, "repeat": False}


async def _request(db: aiosqlite.Connection, order_id: int) -> aiosqlite.Row | None:
    return await _one(
        db,
        "SELECT g.*, r.id AS request_pk, r.order_id AS initial_order_id, "
        "r.question, r.source_order_id, o.state, o.host, o.started_at, o.closed_at, "
        "o.outcome, o.role, o.source_key "
        "FROM roadmap_requests r JOIN roadmap_grants g ON g.id = r.grant_id "
        "JOIN role_orders o ON o.id = ? "
        "WHERE r.id = COALESCE("
        "  (SELECT id FROM roadmap_requests WHERE order_id = o.id),"
        "  (SELECT request_pk FROM roadmap_recoveries WHERE order_id = o.id)"
        ") AND o.role = 'roadmap' AND o.task_id = g.task_id AND o.project_id = g.project_id",
        (order_id,),
    )


async def order_is_current(db: aiosqlite.Connection, order_id: int) -> bool:
    row = await _request(db, order_id)
    return row is not None and await _request_is_current(db, row)


async def _request_is_current(db: aiosqlite.Connection, row: aiosqlite.Row) -> bool:
    return await _current(db, row) and await _signal_is_current(
        db, row, row["source_order_id"]
    )


async def started_question(
    db: aiosqlite.Connection,
    who: TokenIdentity,
    order_id: int,
    source_key: str,
    max_minutes: int,
) -> dict[str, Any]:
    if not source_key.isascii() or len(source_key) != len(SOURCE) + 33:
        raise HTTPException(404, "roadmap order not found")
    async with _read_snapshot(db):
        row = await _request(db, order_id)
        if (
            row is None
            or row["state"] != "started"
            or who.auth_source != "db_api_key"
            or who.role != "agent"
            or row["roadmap_id"] != who.principal_id
            or not compare_digest(row["source_key"], source_key)
            or not await _key_in_project(db, who, row["project_id"])
            or not await _request_is_current(db, row)
        ):
            raise HTTPException(404, "roadmap order not found")
        live = await _one(
            db,
            "SELECT 1 FROM executor_hosts h JOIN principals p ON h.principal_id = p.id "
            "WHERE h.name = ? AND h.enabled = 1 AND p.status = 'active' "
            "AND ? > datetime('now', ?) AND EXISTS (SELECT 1 FROM role_host_admissions "
            "WHERE host = ? AND role = ? AND project_id = ?)",
            (
                row["host"],
                row["started_at"],
                f"-{max_minutes} minutes",
                row["host"],
                row["role"],
                row["project_id"],
            ),
        )
        if live is None:
            raise HTTPException(404, "roadmap order not found")
        project = await repo.get_project(db, row["project_id"])
        if project is None:
            raise HTTPException(404, "roadmap project not found")
        return {
            "order_id": order_id,
            "task_id": row["task_id"],
            "project": project["slug"],
            "source_kind": SOURCE,
            "question": row["question"],
            "decision_context": roadmap_decisions.decision_context(row),
        }
