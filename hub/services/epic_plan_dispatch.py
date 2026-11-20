"""Server-side dispatcher for the four admitted pilot tasks."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal

import aiosqlite

from hub import config, repository as repo
from hub.db import fetchall, write_transaction
from hub.epic_plan_view import PlanDecision
from hub.services import (
    admin,
    chat_pair,
    epic_plan,
    epic_plan_quota as quota,
    epic_plan_spend as spend,
    epic_plan_transport as transport,
    executor_orders as orders,
    executor_skill_bundle as skills,
    orchestrator_queue,
    project_policy,
)
from hub.services.epic_plan_authority import (
    SpendingGrant,
    WorkBinding,
    configured_source_fingerprint,
    get_grant,
)

HOST: Literal["k8"] = "k8"
_SHA = re.compile(r"^[0-9a-f]{40}$")
_RETRYABLE = frozenset({"vm_failed", "setup_failed", "hub_unavailable", "timeout"})


async def _agent(db: aiosqlite.Connection) -> int | None:
    value = config.env_get("EPIC_PLAN_DISPATCH_PRINCIPAL_ID", "") or ""
    if not value.isascii() or not value.isdecimal() or len(value) > 18:
        return None
    principal_id = int(value)
    principal = await admin.get_principal(db, principal_id)
    if not principal or principal["kind"] != "agent" or principal["status"] != "active":
        return None
    perms = await admin.get_principal_permissions(db, principal_id)
    return (
        principal_id
        if "tasks.update" in perms and "tasks.human_gate" not in perms
        else None
    )


def _routing_refusal(project: Any) -> str:
    from hub.services import executor_launch as launch

    policy = project_policy.gate_policy_of(project)
    if launch.launch_mode_of(policy) != launch.LAUNCH_MANUAL:
        return "pilot_executor_disabled"
    if not project_policy.review_dispatch_enabled(policy):
        return "independent_review_unavailable"
    if not project_policy.verdict_is_delegated(policy):
        return "pilot_verdict_not_delegated"
    return ""


async def preflight(db: aiosqlite.Connection) -> str:
    """Cheap, read-only prerequisite display. The order rechecks under its lock."""
    project = await repo.get_project_by_slug(db, epic_plan.PROJECT)
    if project is None:
        return "pilot_project_moved"
    if reason := _routing_refusal(project):
        return reason
    if await _agent(db) is None:
        return "pilot_issuer_unavailable"
    if HOST not in await orders.enabled_host_names(db):
        return "pilot_host_unavailable"
    if (
        transport.executor_transport(HOST) is None
        or transport.review_transport() is None
    ):
        return "pilot_metered_transport_unavailable"
    if not await checkpoint_available(db, int(project["id"])):
        return "pilot_checkpoint_unavailable"
    return ""


async def review_route_ready(
    db: aiosqlite.Connection, project: Any, excluded: set[int]
) -> bool:
    from hub.integrations import cursor_cloud
    from hub.services import epic_plan_gates, review_dispatch

    if transport.review_transport() is None:
        return False
    routes = await review_dispatch.review_reach(db, project_policy.forge_of(project))
    return (
        "cloud" in routes.ways
        and cursor_cloud.is_configured()
        and bool(project["repo"])
        and await epic_plan_gates.independent_reviewer(
            db, await review_dispatch.reviewer_principal_id(db), excluded
        )
    )


async def _starting_sha(db: aiosqlite.Connection, task: Any) -> str:
    from hub.services.orchestration import plugins, project_git_context

    context = await project_git_context(db, int(task["id"]))
    workspace = context.get("repo")
    if not workspace:
        return ""
    branch = (
        task["branch"] if task["submission_generation"] else context.get("base_branch")
    )
    if not branch:
        return ""
    heads = await plugins.git_ops.remote_heads([branch], repo=workspace)
    sha = heads.get(branch, "") if heads is not None else ""
    if task["submission_generation"] and sha != task["submission_sha"]:
        return ""
    return sha if _SHA.fullmatch(sha) else ""


async def _in_flight(db: aiosqlite.Connection) -> bool:
    rows = await fetchall(
        db,
        """SELECT 1 FROM epic_plan_work w
           JOIN executor_orders o ON o.id=w.executor_order_id
           WHERE o.state<>'closed' LIMIT 1""",
    )
    return bool(rows)


async def _attempt(
    db: aiosqlite.Connection, task_id: int, generation: int, limit: int
) -> tuple[int, str]:
    rows = await fetchall(
        db,
        """SELECT w.*, o.state AS order_state, o.report_outcome, o.report_reason_code
           FROM epic_plan_work w JOIN executor_orders o ON o.id=w.executor_order_id
           WHERE w.task_id=? AND w.generation=? AND w.phase='executor'
           ORDER BY w.attempt DESC LIMIT 1""",
        (task_id, generation),
    )
    if not rows:
        return 1, ""
    last = rows[0]
    if last["order_state"] != "closed":
        return 0, "pilot_order_in_flight"
    if last["charged_cents"] is None or not last["report_outcome"]:
        return 0, "pilot_previous_order_unreconciled"
    if last["attempt"] > limit:
        return 0, "pilot_retry_exhausted"
    if (
        last["report_outcome"] != "failed"
        or last["report_reason_code"] not in _RETRYABLE
    ):
        return 0, "pilot_outcome_needs_decision"
    return int(last["attempt"]) + 1, ""


async def checkpoint_available(db: aiosqlite.Connection, project_id: int) -> bool:
    rows = await fetchall(
        db,
        "SELECT 1 FROM role_host_admissions WHERE host=? AND project_id=? AND role='roadmap'",
        (HOST, project_id),
    )
    return bool(rows)


async def _selection(db: aiosqlite.Connection, project: Any) -> Any:
    # An existing pilot leaf owns WIP through its review and repair rounds.
    for task_id in sorted(epic_plan.LEAVES):
        row = await repo.get_task(db, task_id)
        if (
            row
            and row["status"] == "running"
            and row["review_verdict"] == "changes_requested"
        ):
            return row
    queue = await orchestrator_queue.next_task(
        db, project, exclude_state=True, candidate_ids=epic_plan.LEAVES
    )
    task_id = queue["next_task_id"]
    return await repo.get_task(db, task_id) if task_id in epic_plan.LEAVES else None


async def _locked_refusal(
    db: aiosqlite.Connection, decision: PlanDecision, grant: SpendingGrant
) -> str:
    binding = grant.binding
    if decision.version != binding.plan_version:
        return "plan_version_moved"
    if "launch_bounded_executor" not in decision.snapshot.allowed_actions:
        return "pilot_execution_not_authorized"
    if blocker := await epic_plan.start_blocker(db, binding.task_id):
        return str(blocker["reason"])
    if await _in_flight(db):
        return "pilot_order_in_flight"
    if await _agent(db) != binding.principal_id:
        return "pilot_issuer_unavailable"
    attempt, reason = await _attempt(
        db,
        binding.task_id,
        binding.generation,
        decision.snapshot.terms.technical_retries,
    )
    if reason:
        return reason
    if attempt != binding.attempt:
        return "pilot_attempt_moved"
    return await quota.refusal(db, decision, grant) or await spend.refusal(
        db, decision, grant
    )


async def _repair_causes(
    db: aiosqlite.Connection, task: dict[str, Any]
) -> tuple[str, ...]:
    if task["status"] != "running" or task["review_verdict"] != "changes_requested":
        return ()
    from hub.services.executor_launch import _current_findings

    keys = set()
    for finding in await _current_findings(db, task):
        identity = finding.get("uid") or "\n".join(
            (finding.get("where", ""), finding.get("title", ""))
        )
        if identity.strip():
            keys.add(hashlib.sha256(identity.strip().encode()).hexdigest())
    return tuple(sorted(keys))


async def _repair_limit_reached(
    db: aiosqlite.Connection, binding: WorkBinding, causes: tuple[str, ...], limit: int
) -> bool:
    if not causes:
        return False
    rows = await fetchall(
        db,
        """SELECT generation,repair_causes FROM epic_plan_work
           WHERE task_id=? AND phase='executor' AND generation<?""",
        (binding.task_id, binding.generation),
    )
    return any(
        len({r["generation"] for r in rows if cause in json.loads(r["repair_causes"])})
        >= limit
        for cause in causes
    )


async def _write_order(
    db: aiosqlite.Connection, grant: SpendingGrant
) -> tuple[int | None, str]:
    binding = grant.binding
    backend = transport.executor_transport(binding.host)
    if backend is None or transport.review_transport() is None:
        return None, "pilot_metered_transport_unavailable"
    try:
        async with write_transaction(db):
            decision = await epic_plan.active_decision(db)
            if reason := await _locked_refusal(db, decision, grant):
                return None, reason
            project = await repo.get_project_by_slug(db, epic_plan.PROJECT)
            if project is None or (reason := _routing_refusal(project)):
                return None, reason if project is not None else "pilot_project_moved"
            if not await checkpoint_available(db, int(project["id"])):
                return None, "pilot_checkpoint_unavailable"
            acting = await chat_pair.get_acting_agent(db)
            if acting is None or int(acting["id"]) != binding.actor_principal_id:
                return None, "pilot_acting_agent_unavailable"
            if not await review_route_ready(
                db,
                project,
                {
                    binding.principal_id,
                    binding.actor_principal_id,
                    decision.approved_principal_id,
                },
            ):
                return None, "independent_review_unavailable"
            bundle = await skills.read_active_bundle(db)
            booked = await orders._book(
                db, project, binding.host, binding.task_id, bundle
            )
            if isinstance(booked, str):
                return None, booked
            if int(booked.task["submission_generation"] or 0) + 1 != binding.generation:
                return None, "pilot_generation_moved"
            causes = await _repair_causes(db, booked.task)
            if await _repair_limit_reached(
                db, binding, causes, decision.snapshot.terms.fixes_same_cause
            ):
                return None, "pilot_fixes_exhausted"
            order_id, _ = await backend.issue(db, project, booked, grant)
            await spend.record(
                db,
                grant,
                executor_order_id=order_id,
                repair_causes=causes,
            )
            await quota.resume(db, binding)
            await repo.insert_event(
                db,
                kind="epic_plan_executor_ordered",
                task_id=binding.task_id,
                actor="hub",
                payload={**binding.model_dump(), "order_id": order_id},
            )
    except epic_plan.PlanRefusal as exc:
        return None, exc.reason
    except skills.SkillBundleError:
        return None, "pilot_skill_bundle_unavailable"
    except (orders.HostError, aiosqlite.IntegrityError):
        return None, "pilot_slot_unavailable"
    return order_id, ""


async def start_next(db: aiosqlite.Connection) -> tuple[int | None, str]:
    """One deterministic tick, including after a lost response or restart."""
    try:
        decision = await epic_plan.active_decision(db)
    except epic_plan.PlanRefusal as exc:
        return None, exc.reason
    if "launch_bounded_executor" not in decision.snapshot.allowed_actions:
        return None, "pilot_execution_not_authorized"
    if (
        transport.executor_transport(HOST) is None
        or transport.review_transport() is None
    ):
        return None, "pilot_metered_transport_unavailable"
    project = await repo.get_project_by_slug(db, epic_plan.PROJECT)
    if project is None:
        return None, "pilot_project_moved"
    if reason := _routing_refusal(project):
        return None, reason
    if await _in_flight(db):
        return None, "pilot_order_in_flight"
    task = await _selection(db, project)
    if task is None:
        return None, "pilot_no_candidate"
    return await _prepare_order(db, decision, task)


async def _prepare_order(
    db: aiosqlite.Connection, decision: PlanDecision, task: Any
) -> tuple[int | None, str]:
    from hub.services import epic_plan_escalation

    task_id = int(task["id"])
    generation = int(task["submission_generation"] or 0) + 1
    attempt, reason = await _attempt(
        db, task_id, generation, decision.snapshot.terms.technical_retries
    )
    if not reason:
        binding, reason = await _binding(db, decision, task, generation, attempt)
        if binding is not None:
            if await quota.waiting(db, binding, before_reset=True):
                return None, quota.WAITING
            grant = await get_grant(binding)
            if grant is not None and grant.covers(binding):
                order, reason = await _write_order(db, grant)
                if order is not None:
                    return order, ""
            elif await quota.waiting(db, binding):
                return None, quota.WAITING
            else:
                reason = "pilot_spending_authority_unavailable"
    if reason == quota.WAITING:
        return None, reason
    await epic_plan_escalation.maybe_notify(db, decision, task, reason)
    return None, reason


async def _binding(
    db: aiosqlite.Connection,
    decision: PlanDecision,
    task: Any,
    generation: int,
    attempt: int,
) -> tuple[WorkBinding | None, str]:
    principal_id = await _agent(db)
    if principal_id is None or principal_id == decision.approved_principal_id:
        return None, "pilot_issuer_unavailable"
    acting = await chat_pair.get_acting_agent(db)
    if acting is None:
        return None, "pilot_acting_agent_unavailable"
    if HOST not in await orders.enabled_host_names(db):
        return None, "pilot_host_unavailable"
    authority = decision.snapshot.dispatch_authority
    if authority is None or authority.issuer_principal_id != principal_id:
        return None, "pilot_model_unavailable"
    sha = await _starting_sha(db, task)
    if not sha:
        return None, "pilot_starting_sha_unverified"
    return WorkBinding(
        phase="executor",
        project=epic_plan.PROJECT,
        plan_version=decision.version,
        task_id=int(task["id"]),
        generation=generation,
        sha=sha,
        host=HOST,
        model=authority.executor_model,
        principal_id=principal_id,
        actor_principal_id=int(acting["id"]),
        attempt=attempt,
    ), ""


async def sweep(db: aiosqlite.Connection) -> None:
    unresolved = await fetchall(
        db,
        """SELECT * FROM epic_plan_work WHERE charged_cents IS NULL AND authority_source=?
           AND (last_charge_check IS NULL OR last_charge_check < datetime('now','-1 minute'))
           ORDER BY last_charge_check,id LIMIT 1""",
        (configured_source_fingerprint(),),
    )
    for work in unresolved:
        await spend.settle(db, work)
    from hub.services import epic_plan_escalation

    await epic_plan_escalation.resume(db)
    await _follow_review(db)
    await start_next(db)


async def _follow_review(db: aiosqlite.Connection) -> None:
    """The CI report can arrive after submission; retry its normal review door."""
    from hub.services import review_dispatch

    for task_id in sorted(epic_plan.LEAVES):
        task = await repo.get_task(db, task_id)
        if (
            task is None
            or task["status"] != "review"
            or not await epic_plan._under_pilot(db, task_id)
        ):
            continue
        if await repo.get_review_dispatch_for_generation(
            db, task_id, int(task["submission_generation"] or 0)
        ):
            continue
        await review_dispatch.maybe_dispatch_review(db, task_id)
