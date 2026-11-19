"""Typed plan evidence and decisions within an already approved roadmap grant."""

from __future__ import annotations

import json
import re
from typing import Any

import aiosqlite
from fastapi import HTTPException

from hub import repository as repo
from hub.config import TokenIdentity
from hub.db import fetchall, write_transaction
from hub.services.roadmap_contracts import RoadmapResult

# A plan AC opts in with this exact log_check locator. Never infer a threshold
# or a technical mandate from natural-language plan text or a role's suggestion.
_CHECK_REF = re.compile(r"roadmap:successful_fresh_redeems>=([1-9][0-9]{0,5})")


async def approved_checks(
    db: aiosqlite.Connection, plan_id: int, approval_id: int, generation: int
) -> list[dict[str, Any]]:
    """Extract checks only from the exact statement the human approved."""
    from hub.services.statement_generation import statement_fingerprint

    events = await fetchall(
        db,
        "SELECT payload FROM events WHERE id=? AND task_id=? "
        "AND kind='task_approved' AND actor='human'",
        (approval_id, plan_id),
    )
    if not events:
        return []
    stamp = json.loads(events[0]["payload"])
    if stamp.get("statement_generation") != generation or stamp.get(
        "statement_fingerprint"
    ) != await statement_fingerprint(db, plan_id):
        return []
    criteria = await repo.list_acceptance_criteria(db, plan_id)
    checks = []
    for criterion in criteria:
        if criterion["verifiable_by"] != "log_check":
            continue
        match = _CHECK_REF.fullmatch(criterion["test_ref"] or "")
        if match:
            checks.append(
                {
                    "id": criterion["ac_id"],
                    "metric": "successful_fresh_redeems",
                    "operator": "gte",
                    "target": int(match.group(1)),
                }
            )
    return checks


def decision_context(request: aiosqlite.Row) -> dict[str, Any]:
    """Only call with a request whose grant and assigned signal were verified."""
    checks = json.loads(request["decision_checks_json"])
    signal = None
    if request["source_order_id"] is not None:
        signal = {
            "task_id": request["signal_task_id"],
            "order_id": request["source_order_id"],
            "outcome": "failed",
            "reason": "redeem_failed",
        }
    missing = []
    if signal is None:
        missing.append("signal")
    if not checks:
        missing.append("plan_check")
    return {
        "plan": {
            "task_id": request["plan_id"],
            "approval_id": request["approval_id"],
            "statement_generation": request["plan_generation"],
        },
        "signal": signal,
        "checks": checks,
        "missing": missing,
    }


async def record_result(
    db: aiosqlite.Connection, order_id: int, result: RoadmapResult
) -> None:
    """Called inside a verified started report transaction; closing is atomic."""
    from hub.services import roadmap_requests

    request = await roadmap_requests._request(db, order_id)
    if request is None:
        raise HTTPException(409, "roadmap request is unavailable")
    context = decision_context(request)
    await db.execute(
        "INSERT INTO roadmap_results "
        "(order_id, request_pk, grant_id, context_json, result_json) VALUES (?,?,?,?,?)",
        (
            order_id,
            request["request_pk"],
            request["id"],
            json.dumps(context, sort_keys=True),
            result.model_dump_json(),
        ),
    )


async def pending_order_id(db: aiosqlite.Connection, grant_id: int) -> int | None:
    rows = await fetchall(
        db,
        "SELECT r.order_id FROM roadmap_results r "
        "LEFT JOIN roadmap_technical_decisions d ON d.order_id=r.order_id "
        "WHERE r.grant_id=? AND d.order_id IS NULL ORDER BY r.order_id LIMIT 1",
        (grant_id,),
    )
    return int(rows[0]["order_id"]) if rows else None


def _route(context: dict, result: RoadmapResult) -> tuple[str, str]:
    if result.recommendation == "owner_required":
        return "owner_required", "outside_delegation"
    if context["missing"]:
        return "blocked", "missing_" + context["missing"][0]
    if result.recommendation != "hold_expansion":
        return "blocked", "insufficient_facts"
    basis = result.basis
    if (
        basis is None
        or basis.source_order_id != context["signal"]["order_id"]
        or all(check["id"] != basis.check_id for check in context["checks"])
        or result.risk != "repeat_redeem_failure"
    ):
        return "blocked", "incomplete_basis"
    return "accepted", "hold_expansion"


def _view(
    order_id: int,
    context: dict[str, Any],
    result: RoadmapResult,
    status: str,
    reason: str,
    *,
    repeat: bool,
) -> dict[str, Any]:
    condition = None
    if status == "accepted" and result.basis is not None:
        check = next(
            check for check in context["checks"] if check["id"] == result.basis.check_id
        )
        condition = {**check, "observed": None}
    return {
        "order_id": order_id,
        "status": status,
        "reason": reason,
        "repeat": repeat,
        "recipient": "owner" if status == "owner_required" else "steward",
        "plan": context["plan"],
        "signal": None if reason == "stale_signal" else context["signal"],
        "risk": result.risk,
        "verification_condition": condition,
    }


async def evaluate(
    db: aiosqlite.Connection,
    who: TokenIdentity,
    task_id: int,
    grant_id: int,
    order_id: int,
) -> dict[str, Any]:
    """The bound steward records one decision; evaluation never schedules work."""
    from hub.services import roadmap_requests

    async with write_transaction(db):
        grant = await roadmap_requests._steward_grant(db, who, task_id)
        if grant["id"] != grant_id:
            roadmap_requests._conflict(
                "roadmap_grant_stale", "roadmap grant was replaced"
            )
        request = await roadmap_requests._request(db, order_id)
        if request is None or request["id"] != grant_id or request["state"] != "done":
            raise HTTPException(404, "roadmap result not found")
        rows = await fetchall(
            db,
            "SELECT r.context_json, r.result_json, d.status, d.reason FROM roadmap_results r "
            "LEFT JOIN roadmap_technical_decisions d ON d.order_id=r.order_id "
            "WHERE r.order_id=? AND r.request_pk=? AND r.grant_id=?",
            (order_id, request["request_pk"], grant_id),
        )
        if not rows:
            raise HTTPException(404, "roadmap result not found")
        row = rows[0]
        context = json.loads(row["context_json"])
        result = RoadmapResult.model_validate_json(row["result_json"])
        signal_current = await roadmap_requests._signal_is_current(
            db, grant, request["source_order_id"]
        )
        if row["status"] is not None:
            if not signal_current and row["reason"] != "stale_signal":
                roadmap_requests._conflict(
                    "roadmap_signal_stale", "roadmap signal is stale"
                )
            return _view(
                order_id, context, result, row["status"], row["reason"], repeat=True
            )
        if not signal_current:
            status, reason = "blocked", "stale_signal"
        elif context != decision_context(request):
            status, reason = "blocked", "stale_context"
        else:
            status, reason = _route(context, result)
        view = _view(order_id, context, result, status, reason, repeat=False)
        await db.execute(
            "INSERT INTO roadmap_technical_decisions "
            "(order_id, steward_id, status, reason) VALUES (?,?,?,?)",
            (order_id, who.principal_id, status, reason),
        )
        await roadmap_requests._audit(
            db,
            who,
            "roadmap_decision_recorded",
            task_id,
            json.dumps(
                {
                    "order_id": order_id,
                    "request_pk": request["request_pk"],
                    "grant_id": grant_id,
                    "decision": view,
                    "recommendation": result.recommendation,
                },
                sort_keys=True,
            ),
        )
        return view
