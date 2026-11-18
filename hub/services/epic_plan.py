"""Owner admission for the explicitly bounded receipt-split pilot."""

from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from typing import Any, Literal, NoReturn

import aiosqlite
from fastapi import HTTPException

from hub import config, repository as repo
from hub.db import deserialize_str_list, fetchall, write_transaction
from hub.epic_plan_view import (
    PlanDecision,
    PlanDispatchAuthority,
    PlanNode,
    PlanSnapshot,
    PlanState,
    PlanTerms,
    PlanTerminate,
    PlanView,
)
from hub.models import ACTIVE_STATUSES, QUEUED_STATUSES
from hub.services import policy_change, project_policy
from hub.services.delivery_state import with_cached_delivery
from hub.services.refinement import row_to_ac
from hub.services.statement_generation import statement_fingerprint

EPIC_ID = 2373
PROJECT: Literal["receipt-split-calculator"] = "receipt-split-calculator"
GROUPS = frozenset({2374, 2375, 2376})
LEAVES = frozenset({2377, 2378, 2379, 2380})
INCLUDED = GROUPS | LEAVES | {EPIC_ID}
TERMS = PlanTerms()
ACTIONS = ["approve_statement", "prepare_leaf_for_dispatch"]
PAID_ACTIONS = ["launch_bounded_executor", "order_bounded_independent_review"]
EXCEPTIONS = [
    "live_acceptance_2381",
    "publication",
    "signing_credentials",
    "changed_scope_or_permissions",
    "material_review_dispute",
    "unverified_paid_execution",
]
_TREE_QUERY = """
WITH RECURSIVE plan_tree(id) AS (
    SELECT id FROM tasks WHERE id=?
    UNION
    SELECT t.id FROM tasks t JOIN plan_tree ON t.parent_id=plan_tree.id
)
SELECT t.* FROM tasks t JOIN plan_tree USING(id) ORDER BY t.id
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _owner(db: aiosqlite.Connection) -> int | None:
    value = config.env_get("EPIC_PLAN_OWNER_PRINCIPAL_ID", "") or ""
    try:
        principal_id = int(value)
    except ValueError:
        return None
    rows = await fetchall(
        db,
        "SELECT id FROM principals WHERE id=? AND kind='human' AND status='active'",
        (principal_id,),
    )
    return principal_id if rows else None


def can_decide(identity: config.TokenIdentity, principal_id: int | None) -> bool:
    return (
        principal_id is not None
        and identity.principal_id == principal_id
        and identity.is_human
        and identity.auth_source in {"db_api_key", "db_session"}
        and identity.chat_pair_kind is None
        and identity.chat_pair_task_id is None
        and identity.has_permission("tasks.human_gate")
        and not identity.scopes_damaged
        and (identity.scopes is None or PROJECT in identity.scopes)
    )


class PlanRefusal(HTTPException):
    def __init__(
        self, reason: str, status: int = 409, details: dict[str, Any] | None = None
    ):
        self.reason = reason
        self.details = details or {"reason": reason}
        super().__init__(status, detail=self.details)


def _refuse(reason: str, status: int = 409) -> NoReturn:
    raise PlanRefusal(reason, status)


def _digest(data: Any) -> str:
    payload = json.dumps(data, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def _version(snapshot: PlanSnapshot) -> str:
    excluded = {"dispatch_authority"} if snapshot.dispatch_authority is None else set()
    return _digest(snapshot.model_dump(exclude=excluded))


def _execution_target(project: aiosqlite.Row) -> str:
    # The URI and workspace can be sensitive. Approve their exact binding
    # without publishing their raw values in the card or stored snapshot.
    return _digest(
        {
            "repository": project["repo"],
            "workspace": project["workspace_path"],
            "forge": project_policy.forge_of(project),
            "base_branch": project_policy.base_branch_of(project),
            "release_branch": project_policy.release_base_of(project),
        }
    )


async def _check_project(
    db: aiosqlite.Connection, task_id: int, project_id: int
) -> None:
    project = await repo.resolve_project_for_task(db, task_id)
    if project is None or int(project["id"]) != project_id:
        _refuse("plan_cross_project")


async def _node(
    db: aiosqlite.Connection, row: aiosqlite.Row, project_id: int
) -> PlanNode:
    task_id = int(row["id"])
    await _check_project(db, task_id, project_id)
    deps = await repo.list_task_dependencies(db, task_id)
    blockers = [int(blocker["task_id"]) for blocker in deps["blocked_by"]]
    for blocker_id in blockers:
        await _check_project(db, blocker_id, project_id)
    ac_rows = await repo.list_acceptance_criteria(db, task_id)
    return PlanNode(
        task_id=task_id,
        parent_id=row["parent_id"],
        project_id=project_id,
        task_type=row["task_type"],
        title=row["title"],
        revision=int(row["statement_generation"] or 0),
        fingerprint=await statement_fingerprint(db, task_id),
        areas=deserialize_str_list(row["affected_areas"]),
        scope_in=deserialize_str_list(row["scope_in"]),
        scope_out=deserialize_str_list(row["scope_out"]),
        acceptance_criteria=[row_to_ac(ac) for ac in ac_rows],
        blocked_by=blockers,
        allowed=task_id in INCLUDED and not row["archived"],
        leaf=task_id in LEAVES,
    )


def _project_caps(project: aiosqlite.Row) -> tuple[float | None, int | None]:
    from hub.services.executor_dispatch import task_cents_ceiling_of

    policy = project_policy.gate_policy_of(project)
    cents = task_cents_ceiling_of(policy)
    return cents if math.isfinite(
        cents
    ) and cents > 0 else None, project_policy.wip_limit_of(policy)


def _dispatch_authority() -> PlanDispatchAuthority | None:
    from hub.services.epic_plan_authority import configured_source_fingerprint

    source = configured_source_fingerprint()
    model = (config.env_get("EPIC_PLAN_EXECUTOR_MODEL", "") or "").strip()
    issuer = config.env_get("EPIC_PLAN_DISPATCH_PRINCIPAL_ID", "") or ""
    if (
        not source
        or not model
        or not issuer.isascii()
        or not issuer.isdecimal()
        or len(issuer) > 18
    ):
        return None
    return PlanDispatchAuthority(
        host="k8",
        executor_model=model,
        issuer_principal_id=int(issuer),
        budget_source_fingerprint=source,
        conditions=[
            "project_executor_and_review_policy",
            "current_hard_spending_grant_and_verified_checkout",
            "independent_reviewer_and_exact_trusted_CI_before_verdict",
            "live_plan_at_every_new_effect",
        ],
    )


def _allowed_actions(
    project: aiosqlite.Row, dispatch: PlanDispatchAuthority | None
) -> list[str]:
    if dispatch is None:
        return ACTIONS
    actions = [*ACTIONS, *PAID_ACTIONS]
    if project_policy.verdict_is_delegated(project_policy.gate_policy_of(project)):
        actions.append("delegate_clean_r0_r2_verdict")
    return actions


async def snapshot(db: aiosqlite.Connection, epic_id: int) -> PlanSnapshot:
    if epic_id != EPIC_ID:
        _refuse("pilot_not_found", 404)
    project = await repo.resolve_project_for_task(db, epic_id)
    if (
        project is None
        or project["slug"] != PROJECT
        or project["status"] != "active"
        or project["archived"]
    ):
        _refuse("pilot_project_moved", 404)
    private = await fetchall(
        db,
        "SELECT 1 FROM private_project_security WHERE project_id=?",
        (project["id"],),
    )
    if private:
        _refuse("pilot_project_moved", 404)
    rows = await fetchall(db, _TREE_QUERY, (epic_id,))
    if not rows:
        _refuse("pilot_not_found", 404)
    leaf_ceiling, wip_limit = _project_caps(project)
    dispatch = _dispatch_authority()
    return PlanSnapshot(
        schema_version="2" if dispatch else "1",
        epic_id=epic_id,
        project=PROJECT,
        owner_principal_id=await _owner(db),
        project_policy_version=policy_change.policy_version(project),
        execution_target_fingerprint=_execution_target(project),
        project_leaf_ceiling_cents=leaf_ceiling,
        project_wip_limit=wip_limit,
        terms=TERMS,
        allowed_actions=_allowed_actions(project, dispatch),
        dispatch_authority=dispatch,
        exceptions=EXCEPTIONS,
        nodes=[await _node(db, row, int(project["id"])) for row in rows],
    )


async def _decision_row(
    db: aiosqlite.Connection,
    epic_id: int,
    version: str | None = None,
) -> aiosqlite.Row | None:
    if version is None:
        rows = await fetchall(
            db,
            "SELECT * FROM epic_plan_decisions WHERE epic_id=? ORDER BY id DESC LIMIT 1",
            (epic_id,),
        )
    else:
        rows = await fetchall(
            db,
            "SELECT * FROM epic_plan_decisions WHERE epic_id=? AND version=?",
            (epic_id, version),
        )
    return rows[0] if rows else None


def _decision_state(
    row: aiosqlite.Row,
    current: str,
    now: datetime,
) -> tuple[PlanState, str]:
    if row["terminal_state"] == "revoked":
        return "revoked", "plan_revoked"
    if row["terminal_state"] == "stopped":
        return "stopped", "plan_stopped"
    if datetime.fromisoformat(row["expires_at"]) <= now:
        return "expired", "plan_expired"
    if row["version"] != current:
        return "stopped", "plan_changed"
    return "active", "plan_active"


def _decision(row: aiosqlite.Row, current: str) -> PlanDecision:
    state, reason = _decision_state(row, current, _now())
    return PlanDecision(
        version=row["version"],
        snapshot=PlanSnapshot.model_validate_json(row["snapshot"]),
        state=state,
        reason=row["terminal_reason"] or reason,
        approved_principal_id=row["approved_principal_id"],
        approved_at=row["approved_at"],
        expires_at=row["expires_at"],
        terminal_principal_id=row["terminal_principal_id"],
        terminal_at=row["terminal_at"],
    )


async def _dispatch_blocker(
    db: aiosqlite.Connection, current: PlanSnapshot, decision: PlanDecision | None
) -> str:
    if current.dispatch_authority is None:
        return "pilot_spending_authority_unavailable"
    if decision is None or decision.state != "active":
        return "pilot_dispatch_signature_required"
    from hub.services.epic_plan_dispatch import preflight

    return await preflight(db)


async def read_plan(
    db: aiosqlite.Connection,
    epic_id: int,
    identity: config.TokenIdentity,
) -> PlanView:
    current = await snapshot(db, epic_id)
    version = _version(current)
    row = await _decision_row(db, epic_id)
    decision = _decision(row, version) if row else None
    return PlanView(
        version=version,
        snapshot=current,
        state=decision.state if decision else "draft",
        reason=(
            "owner_unassigned"
            if current.owner_principal_id is None
            else decision.reason
            if decision
            else "plan_not_approved"
        ),
        can_decide=can_decide(identity, current.owner_principal_id),
        dispatch_blocker=await _dispatch_blocker(db, current, decision),
        decision=decision,
    )


def _check_scope(current: PlanSnapshot) -> None:
    included = [node for node in current.nodes if node.allowed]
    if {node.task_id for node in included} != INCLUDED:
        _refuse("plan_scope_changed")
    parents = {node.parent_id for node in current.nodes}
    if any(
        node.leaf
        and (node.task_id in parents or node.task_type not in {"task", "subtask"})
        for node in included
    ):
        _refuse("plan_leaf_changed")
    if any(node.task_id in GROUPS and node.task_type != "feature" for node in included):
        _refuse("plan_group_changed")
    if any(node.task_id == EPIC_ID and node.task_type != "epic" for node in included):
        _refuse("plan_root_changed")


async def _authorize(db: aiosqlite.Connection, identity: config.TokenIdentity) -> int:
    owner = await _owner(db)
    if owner is None:
        _refuse("owner_unassigned", 403)
    if not can_decide(identity, owner):
        _refuse("owner_required", 403)
    return owner


async def approve(
    db: aiosqlite.Connection,
    epic_id: int,
    version: str,
    identity: config.TokenIdentity,
) -> PlanDecision:
    from hub.services.epic_plan_admission import prepare_drafts, open_prepared_drafts

    await _authorize(db, identity)
    proposed = await snapshot(db, epic_id)
    if existing := await _decision_row(db, epic_id, version):
        return _decision(existing, _version(proposed))
    if version != _version(proposed):
        _refuse("plan_version_moved")
    _check_scope(proposed)
    drafts = await prepare_drafts(db, proposed)
    async with write_transaction(db):
        owner = await _authorize(db, identity)
        current = await snapshot(db, epic_id)
        current_version = _version(current)
        if existing := await _decision_row(db, epic_id, version):
            return _decision(existing, current_version)
        if version != current_version:
            _refuse("plan_version_moved")
        at = _now()
        await db.execute(
            "UPDATE epic_plan_decisions SET terminal_state='stopped',terminal_principal_id=?,"
            "terminal_at=?,terminal_reason='plan_superseded' WHERE epic_id=? AND terminal_state IS NULL",
            (owner, at.isoformat(), epic_id),
        )
        await db.execute(
            "INSERT INTO epic_plan_decisions "
            "(epic_id,version,snapshot,approved_principal_id,approved_at,expires_at) VALUES (?,?,?,?,?,?)",
            (
                epic_id,
                version,
                current.model_dump_json(),
                owner,
                at.isoformat(),
                (at + timedelta(days=current.terms.duration_days)).isoformat(),
            ),
        )
        await open_prepared_drafts(db, current, version, drafts)
        await repo.insert_event(
            db,
            kind="epic_plan_approved",
            task_id=epic_id,
            actor="human",
            payload={
                "principal_id": owner,
                "version": version,
                "task_ids": sorted(INCLUDED),
            },
        )
        row = await _decision_row(db, epic_id, version)
        if row is None:
            raise RuntimeError("Inserted plan decision is missing")
        return _decision(row, version)


async def terminate(
    db: aiosqlite.Connection,
    epic_id: int,
    body: PlanTerminate,
    identity: config.TokenIdentity,
) -> PlanDecision:
    async with write_transaction(db):
        owner = await _authorize(db, identity)
        current = await snapshot(db, epic_id)
        row = await _decision_row(db, epic_id)
        if row is None or row["version"] != body.version:
            _refuse("plan_version_moved")
        if row["terminal_state"] == body.state:
            return _decision(row, _version(current))
        if row["terminal_state"] == "revoked":
            _refuse("plan_revoked")
        await db.execute(
            "UPDATE epic_plan_decisions SET terminal_state=?,terminal_principal_id=?,terminal_at=?,"
            "terminal_reason=? WHERE id=?",
            (body.state, owner, _now().isoformat(), body.reason, row["id"]),
        )
        await repo.insert_event(
            db,
            kind=f"epic_plan_{body.state}",
            task_id=epic_id,
            actor="human",
            payload={
                "principal_id": owner,
                "version": body.version,
                "reason": body.reason,
            },
        )
        fresh = await _decision_row(db, epic_id)
        if fresh is None:
            raise RuntimeError("Terminated plan decision is missing")
        return _decision(fresh, _version(current))


async def _under_pilot(db: aiosqlite.Connection, task_id: int) -> bool:
    if task_id not in INCLUDED:
        rows = await fetchall(
            db,
            """WITH RECURSIVE ancestry(id, parent_id) AS (
                SELECT id, parent_id FROM tasks WHERE id=?
                UNION SELECT t.id, t.parent_id FROM tasks t JOIN ancestry a ON t.id=a.parent_id
            ) SELECT 1 FROM ancestry WHERE id=?""",
            (task_id, EPIC_ID),
        )
        if not rows:
            return False
    project = await repo.resolve_project_for_task(db, EPIC_ID)
    if project and project["slug"] == PROJECT:
        return True
    return task_id in INCLUDED and await _decision_row(db, EPIC_ID) is not None


async def _in_progress(db: aiosqlite.Connection, task_id: int) -> list[int]:
    active = {status.value for status in ACTIVE_STATUSES - QUEUED_STATUSES}
    return [
        eligible_id
        for eligible_id in sorted(LEAVES - {task_id})
        if (task := await repo.get_task(db, eligible_id)) and task["status"] in active
    ]


async def start_blocker(
    db: aiosqlite.Connection, task_id: int
) -> dict[str, Any] | None:
    """Read admission under the caller's reservation lock before #2383 starts work."""
    if not await _under_pilot(db, task_id):
        return None
    return await _pilot_start_blocker(db, task_id)


async def _pilot_start_blocker(
    db: aiosqlite.Connection, task_id: int
) -> dict[str, Any] | None:
    if task_id not in LEAVES:
        return {"reason": "plan_task_excluded", "task_id": task_id}
    try:
        decision = await active_decision(db)
    except PlanRefusal as exc:
        return {"reason": exc.reason, "task_id": task_id}
    current = decision.snapshot
    blockers = await _dependency_blockers(db, task_id)
    if blockers:
        return {
            "reason": (
                "dependency_delivery_unknown"
                if any(blocker["delivered"] is None for blocker in blockers)
                else "dependency_undelivered"
            ),
            "task_id": task_id,
            "blockers": blockers,
        }
    holders = await _in_progress(db, task_id)
    limit = current.terms.max_in_progress
    if current.project_wip_limit is not None:
        limit = min(limit, current.project_wip_limit)
    if len(holders) >= limit:
        return {"reason": "plan_wip_full", "task_id": task_id, "holders": holders}
    return None


async def active_decision(db: aiosqlite.Connection) -> PlanDecision:
    """Prove the signed plan is still current independently of queue and WIP."""
    current = await snapshot(db, EPIC_ID)
    if current.owner_principal_id is None:
        _refuse("owner_unassigned")
    row = await _decision_row(db, EPIC_ID)
    if row is None:
        _refuse("plan_not_approved")
    version = _version(current)
    state, reason = _decision_state(row, version, _now())
    if state != "active":
        _refuse(reason)
    return _decision(row, version)


async def _dependency_blockers(
    db: aiosqlite.Connection, task_id: int
) -> list[dict[str, Any]]:
    dependencies = await repo.list_task_dependencies(db, task_id)
    blockers = await with_cached_delivery(db, dependencies["blocked_by"])
    return [
        {
            "task_id": int(row["task_id"]),
            "delivered": row["delivered"],
            "reason": row.get("reason") or "",
        }
        for row in blockers
        if row["delivered"] is not True
    ]


def format_start_blocker(blocker: dict[str, Any]) -> str:
    """Keep the reason code and delivery evidence on text-only launch surfaces."""
    reason = blocker["reason"]
    dependencies = blocker.get("blockers", [])
    if not dependencies:
        return reason
    details = ", ".join(
        f"#{dependency['task_id']} ({dependency['reason']})"
        if dependency["reason"]
        else f"#{dependency['task_id']}"
        for dependency in dependencies
    )
    return f"{reason}: {details}"


async def refuse_legacy_start(db: aiosqlite.Connection, task_id: int) -> None:
    """Legacy launch doors do not supply a verified pilot budget reservation."""
    if not await _under_pilot(db, task_id):
        return
    blocker = await _pilot_start_blocker(db, task_id)
    if blocker is None:
        blocker = {"reason": "plan_budget_unverified", "task_id": task_id}
    raise PlanRefusal(blocker["reason"], details=blocker)
