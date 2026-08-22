"""Task lifecycle transitions: create, approve, reject, start, Q&A, decide, etc."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import contextlib
import sqlite3

import aiosqlite
from fastapi import HTTPException, status

from hub import commit_scope, config
from hub import db as db_module
from hub.actionable_errors import (
    claim_without_session_detail,
    done_report_error_detail,
    hierarchy_error_detail,
    pair_start_claim_mismatch_detail,
    pair_start_session_mismatch_detail,
    self_review_forbidden_detail,
    withdraw_own_draft_error_detail,
)
from hub import repository as repo
from hub.services.sessions import note_session_task
from hub.hub_instance import mutation_activity_detail
from hub.db import (
    deserialize_str_list,
    fetchall,
    log_activity,
    structured_fields_from_row,
)
from hub.integrations.registry import plugins
from hub.services.project_policy import risk_map_for_task
from hub.services.risk_class import derive_risk_class
from hub.models import RiskClass
from hub.mcp_envelope import enrich_error_payload
from hub.services.ci_report import adopt_ci_run_report
from hub.services.outcomes import outcome_status_for_task
from hub.services.task_idempotency import (
    IdempotencyRecord,
    hash_task_create_payload,
    idempotency_conflict_detail,
    normalize_task_create,
    resolve_client_request_id,
)
from hub.models import (
    ACTIVE_STATUSES,
    BatchApprove,
    BatchApproveResult,
    BatchApproveSkipped,
    BulkChildTasksCreate,
    FINAL_STATUSES,
    FindingScope,
    LatestReview,
    ReviewFinding,
    SelfReviewWarning,
    TaskAnswer,
    TaskApprove,
    TaskClaim,
    TaskBreadcrumb,
    TaskChildSummary,
    TaskCreate,
    TaskDecide,
    TaskForceComplete,
    TaskPairStart,
    TaskProgress,
    TaskProjectRef,
    TaskQuestion,
    TaskRefine,
    TaskReject,
    TaskRelease,
    TaskReorder,
    TaskReviewVerdict,
    TaskSource,
    TaskStart,
    TaskSubmitReview,
    TaskType,
    TaskUpdateCreate,
    TaskUpdateView,
    TaskView,
)
from hub.integrations.git_ops import PairBranchConflictError
from hub.services.orchestration import (
    completion_requires_review,
    detect_branch_stacking,
    dispatch_task,
    pair_worktree_info,
    prepare_pair_branch,
    restore_pair_workspace_base,
    review_approved_for_current_submission,
    switch_pair_workspace_to_task,
    transition_after_agent_done,
)
from hub.services.refinement import (
    TaskNotFoundError,
    _guard_ac_locator,
    get_readiness,
    get_write_lock,
    list_acceptance_criteria,
)

log = logging.getLogger("hub")

_ROLLUP_PARENT_TYPES = frozenset({"feature", "epic"})


def _existing_task(row: aiosqlite.Row | None, task_id: int) -> aiosqlite.Row:
    """Строка задачи, которая обязана существовать на этом шаге.

    Инвариант «мы только что её читали/меняли, значит она есть» жил в коде
    сорока подавлениями type: ignore. Подавление молчит и когда инвариант
    держится, и когда он порвался; здесь он проверяется и, если не сошёлся,
    отвечает 404 вместо AttributeError на None (#847).
    """
    if row is None:
        raise HTTPException(404, f"task #{task_id} not found")
    return row


async def _try_restore_pair_workspace(
    db: aiosqlite.Connection,
    task_id: int,
) -> None:
    """Best-effort workspace restore; must not break lifecycle transitions (#451)."""
    try:
        await restore_pair_workspace_base(db, task_id)
    except Exception:
        log.warning(
            "Failed to restore pair workspace base for task #%s",
            task_id,
            exc_info=True,
        )


async def _try_switch_pair_workspace_to_task(
    db: aiosqlite.Connection,
    task_id: int,
) -> None:
    """Best-effort workspace switch to the task branch for rework (#457)."""
    try:
        await switch_pair_workspace_to_task(db, task_id)
    except Exception:
        log.warning(
            "Failed to switch pair workspace to task branch for task #%s",
            task_id,
            exc_info=True,
        )


def compute_lifecycle_hint(task: dict[str, Any]) -> str | None:
    """Human-readable explanation for non-obvious lifecycle waits."""
    status = task.get("status")
    if status != "ci_check":
        return None
    if task.get("job_id"):
        return (
            "Headless task in ci_check: background poller watches PR CI and "
            "dispatches review when checks pass."
        )
    branch = task.get("branch") or ""
    pr = task.get("pr_number")
    if not branch:
        return (
            "Pair task skipped ci_check (no branch). If you still see ci_check, "
            "await human decision or force-complete."
        )
    if not pr:
        return (
            "Awaiting CI conveyor: pair task in ci_check without PR yet. "
            "Poller will create PR from branch or escalate to needs_decision "
            "after retries."
        )
    return (
        f"Awaiting CI conveyor: PR #{pr} on branch {branch}. "
        "Poller advances to review when CI passes."
    )


def _children_allow_rollup(children: list[Any]) -> bool:
    """All direct children terminal, none failed, at least one completed (#742).

    ``rejected`` — "the work is not needed" — does not block: a discarded
    draft must not keep a delivered feature open forever (observed on #579,
    where one rejected smoke draft froze the rollup after all real work
    shipped). ``failed`` — "needed but not done" — DOES block: a failure is
    not grounds to close the parent quietly. Anything non-terminal blocks
    as before.
    """
    if not children:
        return False
    statuses = [c["status"] for c in children]
    if any(s not in {"completed", "rejected"} for s in statuses):
        return False
    return "completed" in statuses


async def maybe_rollup_parent(db: aiosqlite.Connection, child_id: int) -> None:
    """Auto-complete feature/epic when every direct child is terminal (#742)."""
    row = await repo.get_task(db, child_id)
    if not row:
        return
    parent_id = dict(row).get("parent_id")
    if not parent_id:
        return

    parent_row = await repo.get_task(db, parent_id)
    if not parent_row:
        return
    parent = dict(parent_row)
    if parent.get("task_type") not in _ROLLUP_PARENT_TYPES:
        return
    if parent["status"] in {s.value for s in FINAL_STATUSES}:
        return

    children = await db_module.get_children(db, parent_id)
    if not _children_allow_rollup(children):
        return

    if not await repo.transition_status_if(
        db,
        parent_id,
        expected_from=parent["status"],
        new_status="completed",
    ):
        refreshed = await repo.get_task(db, parent_id)
        if refreshed and dict(refreshed)["status"] == "completed":
            await maybe_rollup_parent(db, parent_id)
        return

    await log_activity(
        db,
        "task_completed",
        f"Task #{parent_id} auto-completed: all children done",
    )
    await maybe_rollup_parent(db, parent_id)


async def repair_stale_parent_completions(db: aiosqlite.Connection) -> int:
    """Repair feature/epic rows left open while all children are completed."""
    rows = await fetchall(
        db,
        "SELECT id FROM tasks WHERE archived=0 AND task_type IN ('feature','epic') "
        "AND status NOT IN ('completed','failed','rejected') "
        "ORDER BY CASE task_type WHEN 'feature' THEN 0 ELSE 1 END, id ASC",
    )
    repaired = 0
    for row in rows:
        parent_id = row["id"]
        parent_row = await repo.get_task(db, parent_id)
        if not parent_row:
            continue
        children = await db_module.get_children(db, parent_id)
        if _children_allow_rollup(children):
            await repo.update_task(db, parent_id, status="completed")
            repaired += 1
    if repaired:
        await db.commit()
        log.info("Repaired %d stale parent task(s) to completed", repaired)
    return repaired


def _done_report_error(
    task: dict[str, Any],
    *,
    reason: str,
    hint: str,
    required_status: str,
) -> dict[str, Any]:
    return done_report_error_detail(
        task,
        reason=reason,
        hint=hint,
        required_status=required_status,
    )


def _validate_done_report(task: dict[str, Any]) -> None:
    """Raise HTTPException when a done report must not be recorded."""
    status = task["status"]
    if status == "pending_report":
        return
    if status in ("running", "claimed") and not task.get("job_id"):
        return
    if status in {s.value for s in FINAL_STATUSES}:
        raise HTTPException(
            409,
            detail=_done_report_error(
                task,
                reason="task_already_terminal",
                hint="Task is already finished; no further done report is needed.",
                required_status=status,
            ),
        )
    if status == "open" and not task.get("job_id"):
        raise HTTPException(
            400,
            detail=_done_report_error(
                task,
                reason="pair_start_required",
                hint="Call hub_pair_start (or hub_claim_task then pair-start) before hub_report_done.",
                required_status="running",
            ),
        )
    if status == "ci_check":
        raise HTTPException(
            400,
            detail=_done_report_error(
                task,
                reason="awaiting_ci_conveyor",
                hint="Task is in ci_check; wait for poller or use hub_decide_task / human gate.",
                required_status="ci_check",
            ),
        )
    if status == "needs_decision":
        raise HTTPException(
            400,
            detail=_done_report_error(
                task,
                reason="human_decision_required",
                hint="Task awaits hub_decide_task or human Decision Gate.",
                required_status="needs_decision",
            ),
        )
    raise HTTPException(
        400,
        detail=_done_report_error(
            task,
            reason="invalid_status_for_done",
            hint="Start work via hub_pair_start or hub_start_task before reporting done.",
            required_status="running",
        ),
    )


def ensure_reviewer_independence(
    task: dict[str, Any],
    *,
    is_agent: bool,
    principal_id: int | None,
    username: str,
) -> bool:
    """Raise 403 when the caller implemented the task (#318/#320).

    Shared by the REST endpoint and the web review panel so verdict
    independence has exactly one definition. Principal comparison wins;
    the name-based check is the fallback for env tokens and legacy tasks.
    Humans and the solo opt-out (OPENCLAW_REVIEW_SELF_APPROVE=allow) pass.

    Returns True only when the caller IS the implementer and passed solely
    because of the solo opt-out — so the verdict can be audited as
    self-approved (#434). Independent reviewers and humans return False.
    """
    if not is_agent:
        return False
    if not caller_implemented_task(task, principal_id=principal_id, username=username):
        return False
    if config.REVIEW_SELF_APPROVE == "allow":
        return True
    raise HTTPException(403, detail=self_review_forbidden_detail(username))


def caller_implemented_task(
    task: dict[str, Any],
    *,
    principal_id: int | None,
    username: str,
) -> bool:
    """True when the caller is the implementer of the task (#318/#320).

    Single definition of implementer identity, shared by the verdict gate
    and the review-brief warning (#433). Principal comparison wins; the
    name-based check (assigned_agent/claimed_by) is the fallback for env
    tokens and legacy tasks.
    """
    implementer_pid = task.get("implementer_principal_id")
    if (
        implementer_pid is not None
        and principal_id is not None
        and principal_id == implementer_pid
    ):
        return True
    implementers = {
        (task.get("assigned_agent") or "").strip(),
        (task.get("claimed_by") or "").strip(),
    } - {""}
    return username in implementers


def self_review_brief_warning(
    task: dict[str, Any],
    *,
    is_agent: bool,
    principal_id: int | None,
    username: str,
) -> SelfReviewWarning | None:
    """Fail-fast self-review notice for the review brief (#433).

    Mirrors ensure_reviewer_independence but warns instead of raising: the
    implementer may still read the brief for self-checking, yet must know
    BEFORE spending review effort that hub_submit_review will reject the
    verdict. With OPENCLAW_REVIEW_SELF_APPROVE=allow the warning becomes an
    informational solo-mode note. Humans and non-implementers get None.
    """
    if not is_agent:
        return None
    if not caller_implemented_task(task, principal_id=principal_id, username=username):
        return None
    if config.REVIEW_SELF_APPROVE == "allow":
        return SelfReviewWarning(
            reason="solo_mode_self_review",
            message=(
                f"agent '{username}' implemented this task; solo mode permits "
                "self-review"
            ),
            hint=(
                "OPENCLAW_REVIEW_SELF_APPROVE=allow is active: hub_submit_review "
                "will accept your verdict. This note is informational."
            ),
            required_role=None,
        )
    return SelfReviewWarning(
        reason="self_review_forbidden",
        message=(f"agent '{username}' implemented this task and cannot review it"),
        hint=(
            "Stop before running the review: hub_submit_review will reject "
            "your verdict. The Universal Review Gate requires an independent "
            "reviewer — another agent principal or a human token. You may "
            "still use this brief for self-checking. "
            "Solo mode: set OPENCLAW_REVIEW_SELF_APPROVE=allow."
        ),
        required_role="independent_reviewer",
    )


def parse_review_findings(raw: Any) -> list[ReviewFinding]:
    """Decode the review_findings JSON column into models, failing soft.

    Malformed rows return an empty list rather than breaking every task
    view: findings are advisory review data, not lifecycle-critical state.
    """
    if not raw:
        return []
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
        return [ReviewFinding(**f) for f in data]
    except (ValueError, TypeError):
        log.warning("Malformed review_findings JSON ignored: %.80r", raw)
        return []


def latest_review_projection(task: dict[str, Any]) -> LatestReview | None:
    """Build the latest-review projection for status/context (#308)."""
    verdict = task.get("review_verdict")
    if not verdict:
        return None
    verdict_generation = task.get("review_verdict_generation") or 0
    return LatestReview(
        verdict=verdict,
        submission_generation=verdict_generation,
        is_current=verdict_generation == (task.get("submission_generation") or 0),
        self_approved=bool(task.get("review_self_approved") or 0),
        findings=parse_review_findings(task.get("review_findings")),
    )


def row_to_task(
    row: aiosqlite.Row,
    updates: list[aiosqlite.Row] | None = None,
) -> TaskView:
    """Convert a DB row to a TaskView Pydantic model."""
    d = dict(row)
    log_tail = None
    if d.get("job_id"):
        log_tail = plugins.dispatch.job_log_tail(d["job_id"], max_lines=20)
    upd_list = [TaskUpdateView(**dict(u)) for u in updates] if updates else None
    # Structured task form fields (Epic #32) are stored on tasks row but
    # need JSON-decoding for list columns. structured_fields_from_row
    # returns only the keys present in the row, so absent columns fall
    # back to TaskView defaults — safe for both legacy and new rows.
    structured = structured_fields_from_row(row)
    # Drop None values so model defaults (e.g. "" for str, [] for list)
    # win over a NULL DB column instead of being overwritten with None.
    structured_clean = {k: v for k, v in structured.items() if v is not None}
    return TaskView(
        id=d["id"],
        title=d["title"],
        description=d.get("description", ""),
        status=d["status"],
        task_type=d.get("task_type", "task"),
        parent_id=d.get("parent_id"),
        priority=d.get("priority", "medium"),
        position=d.get("position", 0),
        runtime=d.get("runtime", "auto"),
        source=d.get("source", "human"),
        assigned_agent=d.get("assigned_agent", ""),
        rationale=d.get("rationale", ""),
        human_owner=d.get("human_owner", ""),
        human_reviewer=d.get("human_reviewer", ""),
        job_id=d.get("job_id"),
        exit_code=d.get("exit_code"),
        result_text=d.get("result_text"),
        log_tail=log_tail,
        updates=upd_list,
        review_cycle=d.get("review_cycle", 0),
        ci_fix_cycle=d.get("ci_fix_cycle", 0),
        auto_review=bool(d.get("auto_review", 1)),
        review_job_id=d.get("review_job_id"),
        submission_generation=d.get("submission_generation", 0) or 0,
        submission_sha=d.get("submission_sha") or "",
        submission_model=d.get("submission_model") or "",
        review_verdict=d.get("review_verdict"),
        review_verdict_generation=d.get("review_verdict_generation"),
        review_approved_current=review_approved_for_current_submission(d),
        latest_review=latest_review_projection(d),
        branch=d.get("branch"),
        pr_number=d.get("pr_number"),
        claimed_by=d.get("claimed_by"),
        claim_session_id=d.get("claim_session_id"),
        claimed_at=d.get("claimed_at"),
        created_at=d["created_at"],
        updated_at=d["updated_at"],
        archived=bool(d.get("archived", 0)),
        **structured_clean,
    )


async def enrich_task_view(
    db: aiosqlite.Connection,
    task_view: TaskView,
) -> TaskView:
    """Add breadcrumb, children, and progress to a TaskView."""
    if task_view.parent_id is not None:
        crumbs = await db_module.get_breadcrumb(db, task_view.id)
        task_view.breadcrumb = [
            TaskBreadcrumb(id=c["id"], title=c["title"], task_type=c["task_type"])
            for c in crumbs[:-1]
        ]

    # #485: edges on the single-task read only. Lists of hundreds of tasks
    # must not pay for links most of them do not have.
    edges = await repo.list_task_dependencies(db, task_view.id)
    # #885: same enrichment the gate uses, so the card and the warning cannot
    # disagree about one blocker.
    from hub.services.delivery_state import with_delivery

    edges["blocked_by"] = await with_delivery(db, edges["blocked_by"])
    if edges["blocked_by"] or edges["unblocks"]:
        from hub.models import TaskDependencies, TaskDependencyRef

        task_view.dependencies = TaskDependencies(
            blocked_by=[TaskDependencyRef(**e) for e in edges["blocked_by"]],
            unblocks=[TaskDependencyRef(**e) for e in edges["unblocks"]],
        )

    children = await db_module.get_children(db, task_view.id)
    if children:
        task_view.children = [
            TaskChildSummary(
                id=c["id"],
                title=c["title"],
                task_type=c["task_type"],
                status=c["status"],
                priority=c.get("priority", "medium"),
            )
            for c in children
        ]
        progress_data = await db_module.get_progress(db, task_view.id)
        task_view.progress = TaskProgress(**progress_data)

    acs = await list_acceptance_criteria(db, task_view.id)
    if acs:
        task_view.acceptance_criteria = acs

    row = await repo.get_task(db, task_view.id)
    if row:
        task_dict = dict(row)
        task_view.lifecycle_hint = compute_lifecycle_hint(task_dict)
        task_view.outcome_status = await outcome_status_for_task(db, task_dict)

    project_row = await repo.resolve_project_for_task(db, task_view.id)
    if project_row is not None:
        task_view.project = TaskProjectRef(
            id=project_row["id"], slug=project_row["slug"]
        )

    return task_view


@dataclass(frozen=True)
class CreateTaskOutcome:
    task: TaskView
    is_new: bool = True

    def __getattr__(self, name: str) -> Any:
        # Backward-compat: before idempotency (#20) create_task returned the
        # TaskView directly. Delegate unknown attributes to the wrapped view so
        # callers that treat the result as a TaskView keep working. Guard
        # ``task`` to avoid infinite recursion before the field is set.
        if name == "task":
            raise AttributeError(name)
        return getattr(self.task, name)


async def _load_task_view(db: aiosqlite.Connection, task_id: int) -> TaskView:
    row = await repo.get_task(db, task_id)
    return row_to_task(row)  # type: ignore[arg-type]


async def create_task(
    db: aiosqlite.Connection,
    body: TaskCreate,
    *,
    client_request_id: str | None = None,
) -> CreateTaskOutcome:
    """Create a new task, optionally dispatching it immediately."""
    idem_key = resolve_client_request_id(
        None, client_request_id or body.client_request_id
    )

    err = await db_module.validate_hierarchy(db, body.task_type.value, body.parent_id)
    if err:
        raise HTTPException(
            400,
            detail=hierarchy_error_detail(
                err,
                task_type=body.task_type.value,
                parent_id=body.parent_id,
            ),
        )

    # Bind an epic to a project at creation (#346). Only epics carry
    # project_id — children resolve it by walking up to the root epic.
    project_id: int | None = None
    if body.project:
        if body.task_type != TaskType.epic:
            raise HTTPException(
                422, "project can only be set on epics; children inherit it"
            )
        project_row = await repo.get_project_by_slug(db, body.project)
        if project_row is None:
            raise HTTPException(422, f"unknown project slug: {body.project!r}")
        if project_row["archived"] or project_row["status"] != "active":
            raise HTTPException(
                422,
                f"project {body.project!r} is not active "
                "(pending proposals and archived projects cannot take epics)",
            )
        project_id = project_row["id"]

    initial_status, normalized = normalize_task_create(body)
    request_hash = hash_task_create_payload(normalized) if idem_key else None

    try:
        if idem_key:
            existing = await repo.get_task_idempotency_key(db, idem_key)
            if existing:
                if existing["request_hash"] != request_hash:
                    record = IdempotencyRecord(
                        client_request_id=idem_key,
                        task_id=int(existing["task_id"]),
                        request_hash=existing["request_hash"],
                    )
                    raise HTTPException(
                        409,
                        idempotency_conflict_detail(record),
                    )
                await db.commit()
                task = await _load_task_view(db, int(existing["task_id"]))
                return CreateTaskOutcome(task=task, is_new=False)

        # Structured-aware insert so all fields from TaskCreate (work_type,
        # scope_in/out, user_story, etc.) persist (#46). ``normalized`` carries
        # the lifecycle normalizations that also feed the idempotency hash.
        task_id = await repo.create_task_full(db, normalized, status=initial_status)
        if project_id is not None:
            await repo.update_task(db, task_id, project_id=project_id)

        # Shadow risk class (#582): derived from the declared areas, never
        # from anything the author says about risk. No declared areas → the
        # class honestly stays "not computed" (NULL from the migration).
        risk, risk_reasons = derive_risk_class(
            normalized.affected_areas, await risk_map_for_task(db, task_id)
        )
        if risk is not None:
            await repo.update_task(
                db,
                task_id,
                risk_class=risk.value,
                risk_class_reasons=db_module.serialize_str_list(risk_reasons),
            )

        if idem_key and request_hash is not None:
            await repo.insert_task_idempotency_key(
                db,
                client_request_id=idem_key,
                task_id=task_id,
                request_hash=request_hash,
            )

        await db.commit()
    except HTTPException:
        await db.rollback()
        raise
    except aiosqlite.IntegrityError:
        await db.rollback()
        if not idem_key:
            raise
        existing = await repo.get_task_idempotency_key(db, idem_key)
        if not existing:
            raise
        if existing["request_hash"] != request_hash:
            record = IdempotencyRecord(
                client_request_id=idem_key,
                task_id=int(existing["task_id"]),
                request_hash=existing["request_hash"],
            )
            raise HTTPException(
                409,
                idempotency_conflict_detail(record),
            ) from None
        task = await _load_task_view(db, int(existing["task_id"]))
        return CreateTaskOutcome(task=task, is_new=False)

    result: dict[str, Any] = {}
    if normalized.run_immediately and normalized.source != TaskSource.agent:
        row = await repo.get_task(db, task_id)
        result = await dispatch_task(db, task_id, dict(row))  # type: ignore[arg-type]

    await log_activity(
        db,
        "task_created",
        f"{normalized.task_type.value.capitalize()} #{task_id}: {normalized.title}",
        json.dumps(result, ensure_ascii=False) if result else None,
    )

    task = await _load_task_view(db, task_id)
    return CreateTaskOutcome(task=task, is_new=True)


async def create_subtasks_bulk(
    db: aiosqlite.Connection,
    parent_id: int,
    body: BulkChildTasksCreate,
) -> list[TaskView]:
    """Create multiple child tasks under ``parent_id`` in one transaction."""
    if body.task_type in (TaskType.epic, TaskType.feature):
        raise HTTPException(
            400,
            "bulk create supports task_type task or subtask only",
        )

    err = await db_module.validate_hierarchy(db, body.task_type.value, parent_id)
    if err:
        raise HTTPException(
            400,
            detail=hierarchy_error_detail(
                err,
                task_type=body.task_type.value,
                parent_id=parent_id,
            ),
        )

    if await repo.get_task(db, parent_id) is None:
        raise HTTPException(404, "parent task not found")

    if body.source == TaskSource.agent:
        initial_status = "draft"
    else:
        initial_status = "open"

    auto_review = body.auto_review
    if body.task_type == TaskType.subtask:
        auto_review = False

    # Check every item's locators BEFORE opening the write lock: the batch
    # promises all-or-nothing, and refusing after the first rows are written
    # would rely on the rollback to keep that promise instead of never
    # breaking it. This path writes ACs straight through the repository, so
    # the service-layer guard on add/upsert/replace does not cover it — it is
    # the fifth write path, found in review of submission #1 after I had
    # enumerated four by reading one module instead of following the
    # repository calls (#596).
    for item in body.items:
        if item.acceptance_criteria is not None:
            _guard_ac_locator(item.acceptance_criteria)

    created_ids: list[int] = []
    async with get_write_lock(db):
        await db.execute("SAVEPOINT bulk_child_tasks")
        try:
            for idx, item in enumerate(body.items):
                payload = TaskCreate(
                    title=item.title,
                    description=item.description,
                    task_type=body.task_type,
                    parent_id=parent_id,
                    priority=item.priority,
                    source=body.source,
                    agent=body.agent,
                    auto_review=auto_review,
                    run_immediately=False,
                )
                task_id = await repo.create_task_full(
                    db,
                    payload,
                    status=initial_status,
                    position=idx,
                )
                if item.risks is not None:
                    await repo.update_task_structured(
                        db, task_id, TaskRefine(risks=item.risks)
                    )
                if item.acceptance_criteria is not None:
                    await repo.replace_acceptance_criteria(
                        db, task_id, item.acceptance_criteria
                    )
                created_ids.append(task_id)
        except Exception:
            await db.execute("ROLLBACK TO SAVEPOINT bulk_child_tasks")
            await db.execute("RELEASE SAVEPOINT bulk_child_tasks")
            raise
        else:
            await db.execute("RELEASE SAVEPOINT bulk_child_tasks")
            await db.commit()

    titles = ", ".join(f"#{tid}" for tid in created_ids)
    await log_activity(
        db,
        "tasks_bulk_created",
        f"{body.task_type.value}: {len(created_ids)} under #{parent_id} ({titles})",
    )

    views: list[TaskView] = []
    for task_id in created_ids:
        row = await repo.get_task(db, task_id)
        views.append(row_to_task(row))  # type: ignore[arg-type]
    return views


async def approve_task(
    db: aiosqlite.Connection,
    task_id: int,
    body: TaskApprove | None = None,
) -> TaskView:
    """Approve a draft task, optionally dispatching it.

    The approval is gated by the Definition of Ready (#36). A task with
    unsatisfied required checks cannot be approved unless ``body.force``
    is explicitly set — force-approvals are allowed for genuine edge
    cases (production incidents, exploratory drafts) but are logged as
    ``alert`` updates and tagged in the activity log so the audit trail
    stays intact.
    """
    row = await repo.get_task(db, task_id)
    if not row:
        raise HTTPException(404, "task not found")
    task = dict(row)
    if task["status"] != "draft":
        raise HTTPException(
            400,
            f"can only approve draft tasks, current status: {task['status']}",
        )

    body = body or TaskApprove()

    # --- DoR gate -----------------------------------------------------------
    # Import locally to avoid a circular dependency (services.recommendations
    # imports from .readiness which imports from .dor; lifecycle is called
    # from many modules and should stay lightweight at import time).
    from hub.services.recommendations import (
        calculate_readiness_with_recommendations,
    )

    readiness = await calculate_readiness_with_recommendations(db, task_id)
    dor_override_summary: str | None = None
    if not readiness.dor_passed:
        # Use the report's required-only list. Filtering dor_checks ourselves
        # would mistakenly include checks that failed but aren't required
        # for this work_type (review I1).
        missing = readiness.missing_required
        if not body.force:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "dor_failed",
                    "task_id": task_id,
                    "score": readiness.score,
                    "missing_required": missing,
                    "recommendations": [
                        {
                            "field": r.field,
                            "severity": r.severity,
                            "message": r.message,
                            "expected_score_delta": r.expected_score_delta,
                        }
                        for r in readiness.recommendations
                    ],
                    "hint": "pass force=true to override the DoR gate",
                },
            )
        dor_override_summary = ", ".join(missing) or "<unknown>"
        log.warning(
            "DoR override on approve for task #%s (missing: %s)",
            task_id,
            dor_override_summary,
        )
        override_message = (
            f"Approve override: DoR failed (missing: {dor_override_summary}); "
            f"approved with force=true"
        )
        if body.comment:
            override_message += f". Comment: {body.comment}"
        await repo.add_task_update(db, task_id, "", "alert", override_message)
    elif body.force:
        # DoR passed but the caller still set force=true. Record the
        # explicit human override so post-mortems can spot "we forced
        # this even though we didn't strictly need to" — review I7.
        force_message = (
            "Approve override: force=true requested (DoR was already passing)"
        )
        if body.comment:
            force_message += f". Comment: {body.comment}"
        await repo.add_task_update(db, task_id, "", "alert", force_message)

    if body.comment and dor_override_summary is None and not body.force:
        await repo.add_task_update(
            db, task_id, "", "status", f"Approved: {body.comment}"
        )

    if body.runtime:
        await repo.update_task(db, task_id, runtime=body.runtime.value)
        task["runtime"] = body.runtime.value

    # Atomic conditional transition: a concurrent second approve will see
    # ``rowcount == 0`` and get a 409 instead of being silently double-
    # processed. Even though aiosqlite serializes a shared connection
    # today, this guards us when someone moves to a per-request connection
    # or a pool. Review I5.
    transitioned = await repo.transition_status_if(
        db, task_id, expected_from="draft", new_status="open"
    )
    if transitioned:
        await repo.insert_event(
            db,
            kind="task_approved",
            task_id=task_id,
            actor="human",
            payload={"run": bool(body.run), "force": bool(body.force)},
        )
    await db.commit()
    if not transitioned:
        raise HTTPException(409, "task is no longer draft (concurrent approve?)")

    if body.run:
        task["status"] = "open"
        await dispatch_task(db, task_id, task)

    activity_suffix = ""
    if body.run:
        activity_suffix = f" (run={body.run})"
    if dor_override_summary is not None:
        activity_suffix += f" (force=true, missing={dor_override_summary})"
    elif body.force:
        activity_suffix += " (force=true)"
    await log_activity(
        db,
        "task_approved",
        f"Task #{task_id} approved{activity_suffix}",
        detail=mutation_activity_detail(),
    )

    row = await repo.get_task(db, task_id)
    updates = await repo.get_task_updates(db, task_id)
    return row_to_task(row, updates=updates)  # type: ignore[arg-type]


async def batch_approve_tasks(
    db: aiosqlite.Connection,
    body: BatchApprove,
) -> BatchApproveResult:
    """Approve many drafts with per-task guards and partial success (#252).

    Каждая задача проверяется независимо: не-draft, непройденный DoR,
    низкий readiness или high-риски дают skipped с причиной, не ломая
    остальную пачку. force не поддерживается намеренно — override
    остаётся одиночным, аудируемым действием.
    """
    result = BatchApproveResult()
    for task_id in body.task_ids:
        row = await repo.get_task(db, task_id)
        if row is None:
            result.skipped.append(
                BatchApproveSkipped(task_id=task_id, reason="not_found")
            )
            continue
        task = dict(row)
        if task["status"] != "draft":
            result.skipped.append(
                BatchApproveSkipped(
                    task_id=task_id,
                    reason=f"not_draft:{task['status']}",
                )
            )
            continue

        dor_passed = task.get("dor_passed")
        score = task.get("readiness_score")
        if dor_passed is None or score is None:
            # Legacy row without persisted readiness (#250): compute lazily.
            report = await get_readiness(db, task_id)
            dor_passed = report.dor_passed
            score = report.score
        if body.require_dor_passed and not dor_passed:
            result.skipped.append(
                BatchApproveSkipped(task_id=task_id, reason="dor_failed")
            )
            continue
        if body.min_readiness is not None and (score or 0) < body.min_readiness:
            result.skipped.append(
                BatchApproveSkipped(
                    task_id=task_id,
                    reason=f"readiness_below_{body.min_readiness}",
                )
            )
            continue
        if body.exclude_high_risks:
            risks = db_module.deserialize_risks(task.get("risks"))
            if any(r.get("severity") == "high" for r in risks):
                result.skipped.append(
                    BatchApproveSkipped(task_id=task_id, reason="high_risk")
                )
                continue

        try:
            await approve_task(db, task_id, TaskApprove(comment=body.comment))
        except HTTPException as exc:
            reason = "approve_failed"
            detail = exc.detail
            if isinstance(detail, dict):
                reason = detail.get("reason") or detail.get("error") or reason
            result.skipped.append(
                BatchApproveSkipped(task_id=task_id, reason=f"{reason}")
            )
            continue
        result.approved.append(task_id)
    return result


async def reject_task(
    db: aiosqlite.Connection,
    task_id: int,
    body: TaskReject | None = None,
) -> TaskView:
    """Reject a draft task."""
    row = await repo.get_task(db, task_id)
    if not row:
        raise HTTPException(404, "task not found")
    task = dict(row)
    if task["status"] != "draft":
        raise HTTPException(
            400,
            f"can only reject draft tasks, current status: {task['status']}",
        )

    body = body or TaskReject()
    if body.comment:
        await repo.add_task_update(
            db, task_id, "", "status", f"Rejected: {body.comment}"
        )

    await repo.update_task(db, task_id, status="rejected")
    await repo.insert_event(db, kind="task_rejected", task_id=task_id, actor="human")
    # #742: a rejection can be the LAST terminal transition among the
    # siblings — without this call the parent stays open forever and needs
    # a manual force-complete (observed on #579).
    await maybe_rollup_parent(db, task_id)
    await db.commit()
    await log_activity(
        db,
        "task_rejected",
        f"Task #{task_id} rejected",
        detail=mutation_activity_detail(),
    )

    row = await repo.get_task(db, task_id)
    return row_to_task(row)  # type: ignore[arg-type]


async def warn_about_undelivered_blockers(
    db: aiosqlite.Connection, task_id: int
) -> list[dict[str, Any]]:
    """Say out loud that this task starts on top of undelivered work (#484).

    Advisory by design: the emergency flow and deliberate work on a branch
    stack must stay possible, so nothing here refuses a start. What it does
    refuse is silence — an unmet dependency that says nothing reads as
    readiness, which is how #830 got approved, claimed and pair-started
    before anyone noticed the code it needs was still in an open PR.

    Written into the task feed rather than into a new response field: the
    feed already travels to every reader — MCP, REST and the task card —
    and the dependency contracts that would carry a field of their own are
    #481, not this task.
    """
    blockers = await repo.undelivered_blockers(db, task_id)
    # #885: the gate's own merges are not the only way code reaches the base
    # branch. Ask the branch itself before calling a blocker undelivered —
    # otherwise the warning is wrong exactly where it is easiest to check,
    # and a reader who catches it lying once stops reading it.
    from hub.services.delivery_state import with_delivery

    blockers = [b for b in await with_delivery(db, blockers) if not b.get("delivered")]
    if not blockers:
        # Nothing to say. A task with no blockers must start exactly as it
        # did before this check existed.
        return []
    listed = "; ".join(
        f"#{b['task_id']} «{b['title']}» ({b['status']}) — {b['reason']}"
        for b in blockers
    )
    await repo.add_task_update(
        db,
        task_id,
        "hub",
        "alert",
        (
            f"Старт при недоставленных блокерах: {listed}. Готовность считается "
            "по факту доставки, а не по статусу: между отчётом о завершении и "
            "мержем гейтом есть окно, а PR может уйти на доработку (#484). "
            "Старт не запрещён — предупреждение, а не гейт."
        ),
    )
    await db.commit()
    return blockers


async def start_task(
    db: aiosqlite.Connection,
    task_id: int,
    body: TaskStart | None = None,
) -> TaskView:
    """Start an open task by dispatching it."""
    row = await repo.get_task(db, task_id)
    if not row:
        raise HTTPException(404, "task not found")
    task = dict(row)
    if task["status"] != "open":
        raise HTTPException(
            400,
            f"can only start open tasks, current status: {task['status']}",
        )

    body = body or TaskStart()

    if body.plan:
        await repo.add_task_update(
            db,
            task_id,
            task.get("assigned_agent", ""),
            "status",
            f"Plan: {body.plan}",
        )

    if not await repo.has_plan_updates(db, task_id):
        raise HTTPException(
            400,
            "Plan required before starting a task. "
            "Either pass 'plan' field in start request or create an update "
            "with kind='status' and content starting with 'Plan:'.",
        )

    if body.runtime:
        await repo.update_task(db, task_id, runtime=body.runtime.value)
        task["runtime"] = body.runtime.value

    await dispatch_task(db, task_id, task)
    await log_activity(
        db,
        "task_started",
        f"Task #{task_id} dispatched",
        detail=mutation_activity_detail(),
    )
    # #484: after the start, not before it — the check informs, it does not
    # gate. Reading the feed the caller gets back is how they learn.
    await warn_about_undelivered_blockers(db, task_id)

    row = await repo.get_task(db, task_id)
    updates = await repo.get_task_updates(db, task_id)
    return row_to_task(row, updates=updates)  # type: ignore[arg-type]


async def pair_start_task(
    db: aiosqlite.Connection,
    task_id: int,
    body: TaskPairStart | None = None,
    *,
    caller: str = "",
    implementer_principal_id: int | None = None,
    caller_is_agent: bool = False,
) -> TaskView:
    """Start an open task in pair mode: running without headless dispatch.

    ``implementer_principal_id`` records WHO implements as an authenticated
    principal (#320) so the self-review ban can compare identities instead
    of free-text agent names. None (env tokens, anonymous, humans) keeps
    the name-based fallback of #318.

    ``caller_is_agent`` gates the session requirement of #852: an agent must
    say WHICH of its sessions takes the task, because the holder check made of
    names passes for every session of that agent at once. Humans pair-start
    unchanged — there is no session to name and nothing to address.
    """
    row = await repo.get_task(db, task_id)
    if not row:
        raise HTTPException(404, "task not found")
    task = dict(row)
    # The status the decision below is made from; the transition must start
    # from exactly this value, not from whatever the row holds later.
    starting_status = task["status"]
    assigned_agent = (body.assigned_agent or "").strip() if body else ""
    if not assigned_agent:
        assigned_agent = (caller or "").strip() or task.get("assigned_agent", "")

    declared_session = (body.session_id or "").strip() if body else ""
    # #852: the session requirement lives BEFORE the branch is prepared —
    # prepare_pair_branch talks to git and creates a branch, and a task that
    # fails the check afterwards would leave that branch behind.
    if caller_is_agent and not declared_session:
        raise HTTPException(
            422,
            detail=claim_without_session_detail(task_id=task_id, tool="hub_pair_start"),
        )

    if task["status"] == "claimed":
        holder_session = (task.get("claim_session_id") or "").strip()
        # The name check below passes for EVERY session of the holding agent —
        # that is the hole #852 closes. Compared only when both sides name a
        # session: a legacy claim without one keeps the old behaviour rather
        # than becoming unstartable.
        if holder_session and declared_session and holder_session != declared_session:
            raise HTTPException(
                409,
                detail=pair_start_session_mismatch_detail(
                    task_id=task_id,
                    holder_session=holder_session,
                    caller_session=declared_session,
                ),
            )
        holder = (task.get("claimed_by") or "").strip()
        if holder and assigned_agent and holder != assigned_agent:
            # Principal is truth, name is presentational (#453): if the caller
            # authenticated as the same principal that holds the claim, allow
            # the pair-start even when the agent names differ.
            claim_principal = task.get("implementer_principal_id")
            same_principal = (
                implementer_principal_id is not None
                and claim_principal is not None
                and implementer_principal_id == claim_principal
            )
            if not same_principal:
                raise HTTPException(
                    409,
                    detail=pair_start_claim_mismatch_detail(
                        task_id=task_id,
                        holder=holder,
                        caller=assigned_agent,
                    ),
                )
    elif task["status"] != "open":
        raise HTTPException(
            400,
            f"can only pair-start open or own-claimed tasks, current status: {task['status']}",
        )

    body = body or TaskPairStart()

    if body.plan:
        await repo.add_task_update(
            db,
            task_id,
            task.get("assigned_agent", ""),
            "status",
            f"Plan: {body.plan}",
        )

    if not await repo.has_plan_updates(db, task_id):
        raise HTTPException(
            400,
            "Plan required before pair-start. "
            "Either pass 'plan' field in pair-start request or create an update "
            "with kind='status' and content starting with 'Plan:'.",
        )

    try:
        branch = await prepare_pair_branch(
            db, task_id, task, branch_slug=(body.branch_slug or "").strip()
        )
    except PairBranchConflictError as exc:
        raise HTTPException(422, detail=exc.to_detail()) from exc
    if branch:
        task["branch"] = branch

    assigned_agent = (body.assigned_agent or "").strip()
    if not assigned_agent:
        assigned_agent = (caller or "").strip() or task.get("assigned_agent", "")

    update_fields: dict[str, Any] = {
        "job_id": None,
        "assigned_agent": assigned_agent,
    }
    if implementer_principal_id is not None:
        update_fields["implementer_principal_id"] = implementer_principal_id
    if branch:
        update_fields["branch"] = branch
    # #852: pair-start from `open` skips the claim entirely, which is how a
    # task reached running with no session at all. Whoever starts it owns it,
    # so the address is written here too — not only on the claim path.
    if declared_session:
        update_fields["claim_session_id"] = declared_session

    # #365 K4: the status was written unconditionally, and everything between
    # the check above and this line is a window — branch preparation talks to
    # git and can take seconds. Another claim, a human decision or the poller
    # could move the task meanwhile, and the last writer simply won. Transition
    # from the status we actually read, so a lost race is reported instead of
    # overwriting somebody else's move. expected_from is that status and not a
    # literal: pair-start legitimately begins from `open` or from `claimed`.
    if not await repo.transition_status_if(
        db, task_id, expected_from=starting_status, new_status="running"
    ):
        raise HTTPException(
            409,
            f"Task #{task_id} left {starting_status!r} during pair-start; "
            "retry from its current status",
        )
    await repo.update_task(db, task_id, **update_fields)
    # The registry follows the task the same way it follows a claim (#771
    # AC-3): silent when the session is unregistered.
    if declared_session:
        await note_session_task(db, declared_session, task_id)
    await db.commit()
    await log_activity(
        db,
        "task_pair_started",
        f"Task #{task_id} pair session started",
        detail=mutation_activity_detail(),
    )

    # #484: the warning is written BEFORE the view is assembled, so it
    # travels back inside the same response the agent already reads.
    await warn_about_undelivered_blockers(db, task_id)

    row = await repo.get_task(db, task_id)
    updates = await repo.get_task_updates(db, task_id)
    tv = row_to_task(row, updates=updates)  # type: ignore[arg-type]
    # Tell the agent where its isolated worktree is and the active mode (#530).
    tv.workspace_mode, tv.worktree_path = await pair_worktree_info(db, task_id)
    # #615: the statement may be older than the work that invalidated it. Told
    # HERE, on the server, so CLI and REST callers see it too — computing it in
    # the MCP tool would leave every other client blind.
    from hub.services.statement_freshness import statement_freshness

    tv.statement_freshness = await statement_freshness(db, dict(row))  # type: ignore[arg-type]
    return tv


async def _resolve_branch_diff(
    db: aiosqlite.Connection, task: dict[str, Any]
) -> tuple[list[str] | None, str]:
    """The branch diff as the hub itself observes it, resolved ONCE (#583).

    Feeds both the surface check (#550) and the risk-class recompute: two
    consumers, one network walk. Returns ``(paths, reason)`` where ``None``
    means "could not look" and the reason says why — the same degradation
    contract as ``resolve_branch_tip``.
    """
    from hub.services.orchestration import project_git_context

    branch = (task.get("branch") or "").strip()
    if not branch:
        return None, "у задачи нет ветки"

    ctx = await project_git_context(db, task["id"])
    paths = await plugins.git_ops.branch_diff_paths(
        branch, base_branch=ctx.get("base_branch"), repo=ctx.get("repo")
    )
    if paths is None:
        return None, f"не удалось прочитать дифф ветки {branch!r}"
    return paths, ""


def _surface_check(
    task: dict[str, Any], diff_paths: list[str] | None, diff_reason: str
) -> tuple[str, list[str], str]:
    """Compare the branch diff with the task's declared areas (#550).

    Returns (verdict, undeclared, detail) where verdict is "ok", "undeclared"
    or "unknown". "unknown" is never "ok": if the branch or the base cannot be
    read, the check did not run, and saying nothing would read as agreement —
    the mistake #506 made when it treated an unavailable environment as an
    absent problem.

    The comparison is with the diff, not with a prediction. On submit the hub
    has the truth, so there is no name-matching heuristic here and no false
    positive by construction — which is exactly why this check lives at
    submission and not at DoR. The diff itself arrives precomputed (#583):
    the risk-class recompute reads the same one.
    """
    areas = deserialize_str_list(task.get("affected_areas"))
    if not areas:
        return "unknown", [], "у задачи не объявлены affected_areas — сверять не с чем"

    if diff_paths is None:
        return "unknown", [], diff_reason

    candidates = [p for p in diff_paths if p not in commit_scope.ROUTINE_PATHS]
    undeclared = commit_scope.foreign_paths(candidates, areas)
    if undeclared:
        return "undeclared", undeclared, ""
    return "ok", [], ""


def _risk_recompute_on_submit(
    task: dict[str, Any],
    diff_paths: list[str] | None,
    diff_reason: str,
    project_map: dict[str, str] | None = None,
) -> tuple[dict[str, Any], str, str]:
    """Recompute the risk class from the ACTUAL diff at submission (#583).

    The class is a property of the change, not the card: work that started
    as a small edit can wander into a migration or into auth — #559 did.
    Returns ``(fields_to_write, feed_alert, content_note)``:

    - upward divergence writes the new class and raises a feed alert naming
      both classes and the triggering features — the owner decides, the
      submission itself still lands in review (a legal path stays open, the
      #547 lesson);
    - downward is never automatic: doing less than promised is not grounds
      for dropping oversight;
    - an unresolvable diff degrades to a visible "not recomputed" note,
      never to a failed submission.
    """
    if diff_paths is None:
        # #762: name the failure as a failure. "Not recomputed" plus a reason
        # is a different statement from "recomputed, nothing there", and the
        # feed is where an owner decides whether to go look.
        return (
            {},
            "",
            f" Класс риска по диффу НЕ пересчитан: прочитать дифф не удалось "
            f"({diff_reason or 'причина не названа'}).",
        )

    candidates = [p for p in diff_paths if p not in commit_scope.ROUTINE_PATHS]
    new_class, reasons = derive_risk_class(candidates, project_map)
    if new_class is None:
        # An observation, not a degradation: the branch really does change
        # nothing the class cares about. Until #762 this printed the same way
        # as an unreadable diff, so a stale ref in the workspace was
        # indistinguishable from a branch with no changes in it.
        return (
            {},
            "",
            " Класс риска по диффу не менялся: ветка не меняет файлов, "
            "влияющих на класс.",
        )

    fields = {
        "risk_class": new_class.value,
        "risk_class_reasons": db_module.serialize_str_list(reasons),
    }
    try:
        old = RiskClass(task.get("risk_class")) if task.get("risk_class") else None
    except ValueError:
        old = None
    if old is None:
        return (
            fields,
            "",
            f" Класс риска посчитан по фактическому диффу: {new_class.value}.",
        )

    order = list(RiskClass)
    if order.index(new_class) > order.index(old):
        alert = (
            f"Класс риска повышен по фактическому диффу: {old.value} → "
            f"{new_class.value}. Признаки: {'; '.join(reasons)}. "
            "Расхождение вверх — решение владельца на ревью, не автоматический "
            "проход."
        )
        return (
            fields,
            alert,
            f" Класс риска повышен по диффу: {old.value} → {new_class.value}.",
        )
    return {}, "", ""


async def resolve_branch_tip(
    db: aiosqlite.Connection, task_id: int, branch: str
) -> tuple[str, str]:
    """The tip of ``origin/<branch>`` as the hub itself observes it (#572).

    Returns ``(sha, reason)`` where an empty sha means "could not look" and
    the reason says why. The hub resolves this — never the client: a value
    supplied by the same agent whose work is under review is a declaration,
    and this whole mechanism exists because declarations are not
    observations. Best effort by contract: a verdict must not become hostage
    to the network, so failures degrade to "unchecked", never to an error.
    """
    from hub.services.orchestration import project_git_context

    branch = (branch or "").strip()
    if not branch:
        return "", "task has no branch"
    try:
        ctx = await project_git_context(db, task_id)
        workspace = ctx.get("repo")
        if not workspace:
            return "", "project has no workspace to observe the branch from"
        ok, detail = await plugins.git_ops.fetch_base(workspace, branch)
        if not ok:
            return "", f"could not fetch {branch}: {detail or 'fetch failed'}"
        sha = await plugins.git_ops.head_sha(workspace, branch)
        if not sha:
            return "", f"origin/{branch} did not resolve to a commit"
        return sha, ""
    except Exception as exc:  # noqa: BLE001 - degradation is the contract
        log.warning("could not resolve tip of %s for #%s: %s", branch, task_id, exc)
        return "", f"tip resolution failed: {exc}"


def wait_baseline_for(task: dict[str, Any]) -> dict[str, Any]:
    """Fields to watch for a verdict on the CURRENT submission (#836).

    Deliberately NOT ``latest_review.verdict``: that field carries the
    previous generation's verdict across a resubmission, so watching it fires
    on work already judged. These two move only when a verdict is recorded
    for the generation being submitted now — an APPROVED flips the first, a
    CHANGES_REQUESTED still moves the second.
    """
    return {
        "review_approved_current": bool(task.get("review_approved_current")),
        "review_verdict_generation": task.get("review_verdict_generation"),
    }


async def submit_for_review(
    db: aiosqlite.Connection,
    task_id: int,
    body: TaskSubmitReview | None = None,
) -> TaskView:
    """Submit the current work of a pair task for client-driven review (#305).

    Valid only from pair ``running`` (no ``job_id``): headless tasks are
    submitted by their done report and reviewed by the poller conveyor.
    Bumps the submission generation — which invalidates any APPROVED verdict
    recorded for earlier work — and moves the task into ``status=review``
    with no ``review_job_id``, marking the review as client-driven.
    """
    row = await repo.get_task(db, task_id)
    if not row:
        raise HTTPException(404, "task not found")
    task = dict(row)
    body = body or TaskSubmitReview()

    if task["status"] != "running" or task.get("job_id"):
        if task.get("job_id"):
            raise HTTPException(
                400,
                "headless tasks are submitted for review by their done report; "
                "submit-for-review is only for pair tasks without a dispatch job",
            )
        raise HTTPException(
            400,
            f"can only submit running pair tasks for review, "
            f"current status: {task['status']}",
        )

    # #533: the task records a canonical branch; the client reports the one it
    # worked in. A mismatch means the hub, CI and the reviewer are looking at
    # a branch nobody wrote in.
    #
    # This compares a REPORT, not an observation. The hub has no working copy
    # of the project to inspect — on production the workspace holds a single
    # .placeholder file — so a client that names the right branch while
    # sitting in another passes. It catches forgetting to switch, which is
    # the failure that actually happens; it is not a guarantee, and the
    # policy document says so in the same words.
    #
    # Pair path only: a headless task's branch belongs to the dispatch job,
    # and the client never reports one.
    reported = (body.branch or "").strip()
    canonical = (task.get("branch") or "").strip()
    if reported and canonical and reported != canonical:
        raise HTTPException(
            409,
            {
                "error": "branch_mismatch",
                "task_id": task_id,
                "expected": canonical,
                "reported": reported,
                "hint": (
                    f"work in the branch this task owns: git switch {canonical} "
                    f"(create it from the base branch if it does not exist), or "
                    f"move the commits over. If {reported!r} is genuinely the "
                    "right branch, update the task's branch field first so the "
                    "hub, CI and the reviewer all point at the same place."
                ),
            },
        )

    # #583: one diff resolution feeds the surface check and the risk-class
    # recompute. Resolved BEFORE the write lock — this walks to the network.
    diff_paths, diff_reason = await _resolve_branch_diff(db, task)
    risk_fields, risk_alert, risk_note = _risk_recompute_on_submit(
        task, diff_paths, diff_reason, await risk_map_for_task(db, task_id)
    )

    # #550: before the transition, not after — a refusal has to happen while
    # there is still something to refuse.
    surfaces_mode = (config.SDD_SURFACES or "warn").strip().lower()
    surface_note = ""
    # #890: paths the submitter accepts as the real scope. Empty unless the
    # submission asked for it — the hub never widens affected_areas on its own.
    accepted_paths: list[str] = []
    if surfaces_mode != "off":
        verdict, undeclared, detail = _surface_check(task, diff_paths, diff_reason)
        if verdict == "undeclared":
            listed = ", ".join(undeclared[:10])
            if body.accept_areas:
                # #890: affected_areas is written at DoR as a PREDICTION, and
                # work discovers its own scope — #854 measured 46 of 104
                # submissions changing files outside the declared set, and
                # showed the residue is real surfaces, not routine noise.
                # Refusing that punishes imprecise foresight; what review,
                # commit-scope and the risk recompute actually need is that
                # declared and actual agree AT SUBMISSION. So the submitter
                # may accept the truth in one step — explicitly, and on the
                # record below.
                accepted_paths = list(undeclared)
            elif surfaces_mode == "require":
                raise HTTPException(
                    422,
                    f"ветка меняет файлы вне объявленной области: {listed}. "
                    "Допишите их в affected_areas, признайте фактические "
                    "области на сдаче (accept_areas) или объясните в сдаче, "
                    "почему они здесь. Проверка сравнивает с фактическим "
                    "диффом, а не с предсказанием.",
                )
            else:
                surface_note = (
                    f"Вне объявленной области изменены: {listed}. Режим "
                    "проверки — warn, сдача принята. Область стоит дописать "
                    "(или признать на сдаче через accept_areas): по ней "
                    "сверяется и commit-scope."
                )
        elif verdict == "unknown":
            # Nothing to accept: a check that did not run is not a divergence,
            # and accept_areas must never turn silence into agreement.
            surface_note = (
                f"Сверка объявленной области с диффом НЕ выполнялась: {detail}. "
                "Это не значит, что расхождений нет."
            )

    # #572: pin the code the reviewer will actually be judging. Resolved by
    # the hub BEFORE the write lock — this walks to the network. An empty
    # result is recorded as empty and the submission proceeds: the pin is
    # protection for the verdict, not a new gate on submitting.
    submission_sha, sha_reason = await resolve_branch_tip(
        db, task_id, task.get("branch") or ""
    )

    # #605: record which PR carries this work. The pair flow never sets
    # pr_number — only headless create_pr does — so the delivery gate would
    # have keyed on a field nobody fills. The hub looks it up itself; a
    # discovery failure records nothing and the submission proceeds, because
    # a task that genuinely has no PR (config work) must submit exactly as
    # before — the gate then completes it untouched.
    discovered_pr: int | None = None
    if not task.get("pr_number") and canonical:
        from hub.services.orchestration import project_git_context

        try:
            ctx = await project_git_context(db, task_id)
            discovered_pr = await plugins.git_ops.pr_for_branch(
                canonical, repo=ctx.get("repo"), gh_repo=ctx.get("gh_repo")
            )
        except Exception as exc:  # noqa: BLE001 - best effort by contract
            log.warning("PR discovery failed for #%s (%s): %s", task_id, canonical, exc)

    async with get_write_lock(db):
        if not await repo.transition_status_if(
            db, task_id, expected_from="running", new_status="review"
        ):
            raise HTTPException(
                409,
                f"Task #{task_id} left running state during submit; retry from "
                "its current status",
            )
        generation = await repo.bump_submission_generation(db, task_id)
        # #758: the declared implementing model rides the submission the
        # same way the branch does — a report, not an observation, kept
        # auditable next to the pinned sha.
        declared_model = (body.model or "").strip()[:100]
        await repo.update_task(
            db,
            task_id,
            submission_sha=submission_sha,
            submission_model=declared_model,
        )
        # #546: CI normally runs when the PR opens — before this submission
        # existed, when the generation was still 0 — so its evidence is stored
        # per commit and adopted here, the moment that commit becomes the one
        # under review. Without this the report would sit in the table while the
        # gate reported "never ran". Inside the lock, so it shares this commit.
        adopted = await adopt_ci_run_report(db, task_id, submission_sha, generation)
        if discovered_pr:
            await repo.update_task(db, task_id, pr_number=discovered_pr)
        # Client-driven review: no dispatch job. A stale review_job_id from a
        # previous headless cycle would make the poller treat this task as its
        # own, so clear it explicitly.
        await repo.update_task(db, task_id, review_job_id=None)
        # #583: apply the diff-based class INSIDE the transition — a refused
        # submission must leave the task exactly where it was, class included.
        if risk_fields:
            await repo.update_task(db, task_id, **risk_fields)
        agent = (body.agent or "").strip() or task.get("assigned_agent", "")
        summary = (body.summary or "").strip()
        content = f"Submitted for review (submission #{generation})."
        if discovered_pr:
            content += f" PR #{discovered_pr} recorded for delivery."
        if submission_sha:
            content += f" Branch tip at submission: {submission_sha[:12]}."
        else:
            # Unchecked is a state the reviewer must see, not an absence of
            # news — the same rule the drift guard follows (#534, #572).
            # #767: this line used to hang off ``if adopted`` — the CI-report
            # branch — while speaking about pinning. A submission with a
            # pinned sha and no CI report to adopt therefore printed both
            # "Branch tip at submission: 97e4707248ee" and "Branch tip NOT
            # pinned: " with an empty reason, contradicting itself in one
            # sentence (seen on #725 and #763). It belongs to the sha, so it
            # is bound to the sha.
            content += f" Branch tip NOT pinned: {sha_reason}."
        if adopted:
            ac_count = len(adopted.get("ac_recorded") or [])
            v_status = adopted.get("validation_status") or "—"
            content += (
                f" CI run report adopted for this commit: {ac_count} AC result(s), "
                f"validation {v_status}."
            )
        if risk_note:
            content += risk_note
        if declared_model:
            content += f" Модель исполнителя (декларация): {declared_model}."
        if summary:
            content += f" {summary}"
        await repo.add_task_update(db, task_id, agent, "status", content)
        # #890: the accepted scope is written INSIDE the same transaction as
        # the transition, so a task can never end up in review with the field
        # widened but the growth unrecorded — or the other way round.
        if accepted_paths:
            declared = deserialize_str_list(task.get("affected_areas"))
            merged = list(declared) + [p for p in accepted_paths if p not in declared]
            await repo.update_task_structured(
                db, task_id, TaskRefine(affected_areas=merged)
            )
            shown = ", ".join(accepted_paths[:10])
            more = (
                f" и ещё {len(accepted_paths) - 10}" if len(accepted_paths) > 10 else ""
            )
            # A separate, visible event on purpose. Without it affected_areas
            # would simply always equal the diff, and there would be nothing
            # left to compare: the reviewer must be able to see that half the
            # declared scope appeared at submission, not at DoR.
            await repo.add_task_update(
                db,
                task_id,
                "hub",
                "alert",
                f"{commit_scope.SCOPE_GROWTH_MARKER} "
                f"+{len(accepted_paths)} путь(ей) "
                f"признан(ы) на сдаче — {shown}{more}. Было заявлено "
                f"{len(declared)}, стало {len(merged)}. Это признание факта "
                "сдающим, а не предсказание из постановки.",
            )
        if surface_note:
            await repo.add_task_update(db, task_id, "hub", "alert", surface_note)
        if risk_alert:
            await repo.add_task_update(db, task_id, "hub", "alert", risk_alert)
        await db.commit()
        await log_activity(
            db,
            "task_submitted_for_review",
            f"Task #{task_id} submitted for review (generation {generation})",
            detail=mutation_activity_detail(),
        )

    # Advisory branch-stacking detection (#438): warn — never block — when
    # this branch carries commits of another unmerged task branch. A stack
    # can be a deliberate decision, so the finding is an alert update plus
    # a response hint, not a failed submission.
    stacking = await detect_branch_stacking(db, task_id, task.get("branch") or "")
    if stacking:
        await repo.add_task_update(db, task_id, "hub", "alert", stacking["message"])
        await db.commit()

    # #757: the hub — not the implementer — calls the cross-model reviewer.
    # Best-effort by contract: a failed dispatch alerts inside and must
    # never break the submission itself.
    try:
        from hub.services.review_dispatch import maybe_dispatch_review

        await maybe_dispatch_review(db, task_id)
    except Exception:  # noqa: BLE001 - dispatch must never break a submit
        log.exception("cross-model review dispatch failed for task #%s", task_id)

    row = _existing_task(await repo.get_task(db, task_id), task_id)
    updates = await repo.get_task_updates(db, task_id)
    view = row_to_task(row, updates=updates)

    # Machine-review policy (#382): tell the submitting agent right away
    # when the harness run is expected before the human verdict.
    from hub.services.orchestration import machine_review_gap

    gap = await machine_review_gap(db, dict(row))
    if gap:
        view.lifecycle_hint = (
            f"Machine-review требуется ({gap}): hub_get_skill('multi-agent-review') "
            "→ прогон → hub_submit_machine_review — до человеческого вердикта."
        )
    if stacking:
        view.lifecycle_hint = (
            f"{view.lifecycle_hint}\n{stacking['message']}"
            if view.lifecycle_hint
            else stacking["message"]
        )
    # #836: hand back the baseline for waiting on THIS submission's verdict.
    # A snapshot of the current values, never of the desired ones: a baseline
    # describing the future would be the same guess it replaces.
    view.wait_baseline = wait_baseline_for(dict(row))

    await _try_restore_pair_workspace(db, task_id)
    return view


def out_of_scope_draft_marker(task_id: int, finding_id: int) -> str:
    """Back-reference marker stamped into auto-created draft descriptions (#436).

    Encodes source task + finding id (NOT submission generation), so a
    resubmitted verdict maps the same finding to the same draft instead of
    creating a duplicate.
    """
    return f"[auto-draft: task #{task_id} finding #{finding_id}]"


async def create_drafts_for_out_of_scope_findings(
    db: aiosqlite.Connection,
    task: dict[str, Any],
    body: TaskReviewVerdict,
) -> list[int]:
    """Auto-create DRAFT follow-up tasks for unlinked out-of-scope findings (#436).

    For each ``out_of_scope`` finding without ``linked_task_id``, creates a
    draft task (source=agent → DoR gate stays: a human decides whether to
    take it into work) and stamps the created id into the finding, so the
    stored verdict references the follow-up. Returns ids created in THIS
    call. Idempotency: findings already linked are skipped, and an existing
    draft carrying the same back-reference marker is reused (incident #392:
    out-of-scope findings got lost until #424–#427 were created manually).
    """
    pending = [
        f
        for f in body.findings
        if f.scope == FindingScope.out_of_scope and not f.linked_task_id
    ]
    if not pending:
        return []

    # Drafts land under the reviewed task's feature parent so triage sees
    # them in context. Any other parent kind (or none) → top-level draft;
    # the strict hierarchy only allows feature as a task's parent.
    parent_id: int | None = None
    if task.get("parent_id"):
        parent_row = await repo.get_task(db, task["parent_id"])
        if parent_row is not None and parent_row["task_type"] == "feature":
            parent_id = int(parent_row["id"])

    source_task_id = int(task["id"])
    generation = task.get("submission_generation") or 0
    created: list[int] = []
    for f in pending:
        marker = out_of_scope_draft_marker(source_task_id, f.id)
        existing = await repo.find_task_id_by_description_marker(db, marker)
        if existing is not None:
            f.linked_task_id = existing
            continue

        place = f"{f.file}:{f.line}" if f.file and f.line else f.file
        lines = [
            f"Out-of-scope review finding #{f.id} from review of task "
            f"#{source_task_id} (submission #{generation}).",
            "",
            f"Severity: {f.severity.value}",
        ]
        if place:
            lines.append(f"Location: {place}")
        lines.append(f"Finding: {f.message}")
        if f.recommendation:
            lines.append(f"Recommendation: {f.recommendation}")
        lines.extend(["", marker])

        title = f"Review follow-up: {f.message}"
        if len(title) > 500:
            title = title[:499] + "…"

        view = (
            await create_task(
                db,
                TaskCreate(
                    title=title,
                    description="\n".join(lines),
                    task_type=TaskType.task,
                    parent_id=parent_id,
                    source=TaskSource.agent,
                    agent=(body.agent or "").strip() or "reviewer",
                    rationale=(
                        f"Auto-created from out-of-scope review finding #{f.id} "
                        f"on task #{source_task_id} (#436)"
                    ),
                    run_immediately=False,
                ),
            )
        ).task
        f.linked_task_id = view.id
        created.append(view.id)
    return created


async def record_review_verdict(
    db: aiosqlite.Connection,
    task_id: int,
    body: TaskReviewVerdict,
    *,
    self_approved: bool = False,
    principal_id: int | None = None,
) -> TaskView:
    """Record an explicit review verdict for the current submission (#305).

    Persists the verdict bound to the current submission generation, so a
    later resubmission automatically invalidates an APPROVED verdict. For
    client-driven review (status=review, no review_job_id) the task returns
    to ``running`` so the developer can fix findings or report done (#307);
    headless transitions remain with the poller. Never a completion path.

    Finding scope (#435): a ``changes_requested`` verdict with findings must
    include at least one ``in_scope`` finding — if everything is out of
    scope there is nothing to fix in this task, so the verdict should be
    ``approved`` with the out-of-scope findings kept as recommendations
    (incident #392: the source task hung in review while every finding went
    to parallel tasks). Out-of-scope findings without ``linked_task_id``
    produce a non-blocking warning in the review update.

    ``self_approved=True`` (the ensure_reviewer_independence solo opt-out
    result) marks the verdict as non-independent: the flag is persisted on
    the task row, echoed in the task update, and logged as a warning so a
    weakened Review Gate stays visible in hindsight (#434).

    ``create_tasks_for_out_of_scope`` (#436): opt-in auto-creation of DRAFT
    follow-up tasks for unlinked out-of-scope findings, so they cannot get
    lost when the reviewer forgets to create tasks manually. See
    :func:`create_drafts_for_out_of_scope_findings`.
    """
    row = await repo.get_task(db, task_id)
    if not row:
        raise HTTPException(404, "task not found")
    task = dict(row)

    if (task.get("submission_generation") or 0) == 0:
        raise HTTPException(
            400,
            "no submission to review yet: the task has never been submitted for review",
        )

    if body.verdict.value == "changes_requested" and body.findings:
        if all(f.scope == FindingScope.out_of_scope for f in body.findings):
            raise HTTPException(
                422,
                detail=enrich_error_payload(
                    {
                        "reason": "changes_requested_requires_in_scope_finding",
                        "message": (
                            "changes_requested requires at least one in_scope "
                            "finding; all findings are out_of_scope"
                        ),
                        "hint": (
                            "If nothing needs fixing in this task, submit "
                            "verdict=approved and keep out-of-scope findings "
                            "as recommendations (linked to follow-up tasks "
                            "via linked_task_id)."
                        ),
                        "suggested_tool": "hub_submit_review",
                    }
                ),
            )

    # Machine-review hard gate (#382): only in OPENCLAW_MACHINE_REVIEW=require,
    # and only for APPROVED — the reviewer must always be able to reject work
    # (changes_requested), harness or no harness. Default 'warn' keeps every
    # verdict available; the panel shows the gap.
    if config.MACHINE_REVIEW_MODE == "require" and body.verdict.value == "approved":
        from hub.services.orchestration import machine_review_gap

        gap = await machine_review_gap(db, task)
        if gap:
            raise HTTPException(
                422,
                f"machine-review обязателен для аппрува этой задачи: {gap}",
            )

    # Verifiable SDD (#508): under 'require', an APPROVED verdict needs every
    # current verifiable_by=test AC green. Only APPROVED is gated — a reviewer
    # must always be able to reject red work (lesson from #382).
    if config.SDD_AC_TESTS == "require" and body.verdict.value == "approved":
        from hub.services.ac_tests import ac_tests_gap

        ac_gap = await ac_tests_gap(db, task)
        if ac_gap:
            raise HTTPException(422, f"ac_tests_not_green: {ac_gap}")

    # #572: does the branch still stand where it stood at submission? Only
    # APPROVED is checked — it is the verdict that creates the false safety of
    # review_approved_current, while changes_requested returns the task to
    # work anyway. Three outcomes, never collapsed: diverged / match /
    # could-not-check with the reason. Resolved before the write lock (it
    # walks to the network), and a resolution failure degrades to a visible
    # "unchecked" — a verdict must not be hostage to the remote.
    pinned_sha = (task.get("submission_sha") or "").strip()
    diverged_tip = ""
    sha_note = ""
    if body.verdict.value == "approved":
        if not pinned_sha:
            sha_note = (
                "Сверка кода с моментом сдачи НЕ проводилась: вершина ветки "
                "не была записана при сдаче. Вердикт относится к номеру "
                "сдачи, не к коммиту."
            )
        else:
            current_tip, tip_reason = await resolve_branch_tip(
                db, task_id, task.get("branch") or ""
            )
            if not current_tip:
                sha_note = (
                    f"Сверка кода с моментом сдачи НЕ проводилась: {tip_reason}. "
                    f"Сдавался коммит {pinned_sha[:12]}."
                )
            elif current_tip != pinned_sha:
                diverged_tip = current_tip

    # Auto-draft follow-ups BEFORE persisting the verdict so the created
    # ids land in the stored findings (create_task commits on its own, so
    # it must run outside the verdict's write-lock critical section).
    auto_created: list[int] = []
    if body.create_tasks_for_out_of_scope and body.findings:
        auto_created = await create_drafts_for_out_of_scope_findings(db, task, body)

    async with get_write_lock(db):
        findings_json = json.dumps(
            [f.model_dump(exclude_none=True) for f in body.findings],
            ensure_ascii=False,
        )
        await repo.record_review_verdict(
            db,
            task_id,
            body.verdict.value,
            findings_json=findings_json,
            self_approved=self_approved,
        )
        agent = (body.agent or "").strip() or "reviewer"
        content = f"Review verdict: {body.verdict.value.upper()}"
        if diverged_tip:
            content += (
                f"\nКОД УШЁЛ ИЗ-ПОД ОДОБРЕНИЯ: сдавался {pinned_sha[:12]}, "
                f"вершина ветки теперь {diverged_tip[:12]}. Вердикт записан "
                f"для {pinned_sha[:12]} и НЕ распространяется на новые "
                "коммиты; задача возвращена в running — пересдайте, чтобы "
                "ревью увидело текущий код."
            )
        elif sha_note:
            content += f"\n{sha_note}"
        if self_approved:
            content += " [self-approved: solo mode, OPENCLAW_REVIEW_SELF_APPROVE=allow]"
            log.warning(
                "Task #%s: review verdict %s accepted via "
                "OPENCLAW_REVIEW_SELF_APPROVE=allow — reviewer '%s' "
                "implemented this task (no independent review)",
                task_id,
                body.verdict.value,
                agent,
            )
        if body.findings:
            # Human-readable echo only; the canonical structured findings
            # live on the task row, so the update text can stay compact.
            for f in body.findings[:20]:
                place = (
                    f" ({f.file}:{f.line})"
                    if f.file and f.line
                    else (f" ({f.file})" if f.file else "")
                )
                scope_mark = ""
                if f.scope == FindingScope.out_of_scope:
                    scope_mark = (
                        f" [out-of-scope → #{f.linked_task_id}]"
                        if f.linked_task_id
                        else " [out-of-scope]"
                    )
                content += (
                    f"\n{f.id}. [{f.severity.value}]{place}{scope_mark} {f.message}"
                )
            if len(body.findings) > 20:
                content += f"\n… and {len(body.findings) - 20} more findings"
            unlinked = [
                f.id
                for f in body.findings
                if f.scope == FindingScope.out_of_scope and not f.linked_task_id
            ]
            if unlinked:
                ids = ", ".join(str(i) for i in unlinked)
                content += (
                    f"\nWarning: out-of-scope finding(s) {ids} have no "
                    "linked_task_id — create follow-up task(s) and link them."
                )
            if auto_created:
                ids = ", ".join(f"#{i}" for i in auto_created)
                content += (
                    f"\nAuto-created draft task(s) for out-of-scope "
                    f"findings: {ids} (awaiting human DoR approval)."
                )
        if body.comments.strip():
            content += f"\n{body.comments.strip()}"
        # The verdict is authored by a principal — the endpoint already
        # resolved one to check reviewer independence. Without this it would
        # be filed as "hub", which is exactly the confusion #559 removes.
        await repo.add_task_update(
            db,
            task_id,
            agent,
            "review",
            content,
            principal_id=principal_id,
            author_kind="principal" if principal_id is not None else "anonymous",
        )

        # Client-driven review only (status=review, no review_job_id): hand
        # the task back to the developer so the loop continues — fix findings
        # on CHANGES_REQUESTED, or report done on APPROVED. Headless review
        # transitions stay with the poller, which owns tasks that carry a
        # review_job_id (#307).
        if task["status"] == "review" and not task.get("review_job_id"):
            await repo.transition_status_if(
                db, task_id, expected_from="review", new_status="running"
            )
            if diverged_tip:
                # The verdict stays recorded for the audit trail, but it must
                # not read as covering code the reviewer never saw. Bumping
                # the generation stales it by the SAME mechanism a
                # resubmission uses — review_approved_current goes false —
                # and the task is already back in running, so there is a
                # legal way forward: resubmit and let review see the real
                # tip (#572). A hard refusal would leave the task locked in
                # review with no exit — the dead end #547 hit.
                await repo.bump_submission_generation(db, task_id)
                await repo.update_task(db, task_id, submission_sha="")
            if body.verdict.value == "changes_requested":
                await repo.update_task(
                    db, task_id, review_cycle=(task.get("review_cycle") or 0) + 1
                )
                # #451 restored the workspace to base on submit; on rework put it
                # back on the task branch so fixes don't land on local base (#457).
                await _try_switch_pair_workspace_to_task(db, task_id)

        await repo.insert_event(
            db,
            kind="review_verdict_recorded",
            task_id=task_id,
            actor=agent,
            payload={
                "verdict": body.verdict.value,
                "submission_generation": task.get("submission_generation") or 0,
                "self_approved": self_approved,
            },
        )
        await db.commit()
        await log_activity(
            db,
            "task_review_verdict",
            f"Task #{task_id} review verdict: {body.verdict.value}",
            detail=mutation_activity_detail(),
        )

    row = await repo.get_task(db, task_id)
    updates = await repo.get_task_updates(db, task_id)
    view = row_to_task(row, updates=updates)  # type: ignore[arg-type]
    if diverged_tip:
        view.lifecycle_hint = (
            f"Код ушёл из-под одобрения: сдавался {pinned_sha[:12]}, вершина "
            f"ветки теперь {diverged_tip[:12]}. Вердикт не распространяется "
            "на новые коммиты; задача в running — пересдайте через "
            "hub_submit_for_review."
        )
    elif sha_note:
        view.lifecycle_hint = sha_note
    return view


async def claim_task(
    db: aiosqlite.Connection,
    task_id: int,
    body: TaskClaim,
    *,
    implementer_principal_id: int | None = None,
    caller_is_agent: bool = False,
) -> TaskView:
    """Claim an open task for a single Cursor agent/session.

    ``implementer_principal_id`` records the claiming agent's authenticated
    principal (#320) for the identity-based self-review ban.

    ``caller_is_agent`` says whether an authenticated AGENT is taking the task,
    and only then is ``session_id`` required (#852). Humans and env-token
    callers claim exactly as before: the address matters for agent sessions,
    which is who the registry, the message channel and the wake-up serve.
    """
    row = await repo.get_task(db, task_id)
    if not row:
        raise HTTPException(404, "task not found")
    task = dict(row)

    # #852: a claim without a session leaves a task nobody can be reached
    # about. Refused BEFORE the status transition — a refusal has to happen
    # while there is still something to refuse.
    if caller_is_agent and not body.session_id.strip():
        raise HTTPException(
            422,
            detail=claim_without_session_detail(task_id=task_id, tool="hub_claim_task"),
        )

    if task["status"] == "claimed":
        if (
            task.get("claimed_by") == body.agent
            and (task.get("claim_session_id") or "") == body.session_id
        ):
            row = await repo.get_task(db, task_id)
            updates = await repo.get_task_updates(db, task_id)
            return row_to_task(row, updates=updates)  # type: ignore[arg-type]
        holder = task.get("claimed_by") or "unknown"
        raise HTTPException(
            409,
            f"Task #{task_id} already claimed by {holder}",
        )

    if task["status"] != "open":
        raise HTTPException(
            400,
            f"can only claim open tasks, current status: {task['status']}",
        )

    if not await repo.transition_status_if(
        db, task_id, expected_from="open", new_status="claimed"
    ):
        row = _existing_task(await repo.get_task(db, task_id), task_id)
        task = dict(row)
        if task["status"] == "claimed" and task.get("claimed_by") == body.agent:
            updates = await repo.get_task_updates(db, task_id)
            return row_to_task(row, updates=updates)
        raise HTTPException(409, f"Task #{task_id} claim conflict")

    session_note = f" session={body.session_id}" if body.session_id else ""
    claim_fields: dict[str, Any] = {
        "claimed_by": body.agent,
        "claim_session_id": body.session_id or None,
        "claimed_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "assigned_agent": body.agent,
    }
    if implementer_principal_id is not None:
        claim_fields["implementer_principal_id"] = implementer_principal_id
    await repo.update_task(db, task_id, **claim_fields)
    # The registry follows the claim inside the same transaction (#771 AC-3):
    # two places may not disagree about which task a session holds. Silent when
    # the session is unregistered — the registry is optional.
    await note_session_task(db, body.session_id, task_id)
    await repo.add_task_update(
        db,
        task_id,
        body.agent,
        "status",
        f"Claimed by {body.agent}{session_note}",
    )
    await db.commit()
    await log_activity(
        db,
        "task_claimed",
        f"Task #{task_id} claimed by {body.agent}",
        detail=mutation_activity_detail(),
    )

    row = await repo.get_task(db, task_id)
    updates = await repo.get_task_updates(db, task_id)
    return row_to_task(row, updates=updates)  # type: ignore[arg-type]


async def release_task(
    db: aiosqlite.Connection,
    task_id: int,
    body: TaskRelease,
) -> TaskView:
    """Release an active claim and return the task to open."""
    row = await repo.get_task(db, task_id)
    if not row:
        raise HTTPException(404, "task not found")
    task = dict(row)

    if task["status"] != "claimed":
        raise HTTPException(
            400,
            f"can only release claimed tasks, current status: {task['status']}",
        )

    holder = task.get("claimed_by") or ""
    if holder != body.agent:
        raise HTTPException(
            409,
            f"Task #{task_id} is claimed by {holder}, not {body.agent}",
        )
    if body.session_id and (task.get("claim_session_id") or "") != body.session_id:
        raise HTTPException(
            409,
            f"Task #{task_id} claim session mismatch",
        )

    if not await repo.transition_status_if(
        db, task_id, expected_from="claimed", new_status="open"
    ):
        raise HTTPException(409, f"Task #{task_id} release conflict")

    await repo.update_task(
        db,
        task_id,
        claimed_by=None,
        claim_session_id=None,
        claimed_at=None,
        # The claim is the implementer's reservation: releasing it also
        # releases the recorded implementer identity (#320).
        implementer_principal_id=None,
    )
    # Same transaction, mirror image of the claim (#771 AC-3). The session id
    # comes from the request when given and from the task otherwise: the holder
    # is whoever the task says it is.
    await note_session_task(
        db, body.session_id or (task.get("claim_session_id") or ""), None
    )
    await repo.add_task_update(
        db,
        task_id,
        body.agent,
        "status",
        f"Claim released by {body.agent}",
    )
    await db.commit()
    await log_activity(
        db,
        "task_released",
        f"Task #{task_id} claim released",
        detail=mutation_activity_detail(),
    )

    await _try_restore_pair_workspace(db, task_id)

    row = await repo.get_task(db, task_id)
    updates = await repo.get_task_updates(db, task_id)
    return row_to_task(row, updates=updates)  # type: ignore[arg-type]


def _can_ask_question(task: dict) -> bool:
    """Pair agents may ask from ``open`` (pre pair-start) or ``running``."""
    status = task["status"]
    if status == "running":
        return True
    if status == "open" and not task.get("job_id"):
        return True
    if status == "claimed" and not task.get("job_id"):
        return True
    return False


async def ask_question(
    db: aiosqlite.Connection,
    task_id: int,
    body: TaskQuestion,
) -> TaskView:
    """Record an agent question and move task to needs_info."""
    row = await repo.get_task(db, task_id)
    if not row:
        raise HTTPException(404, "task not found")
    task = dict(row)
    if not _can_ask_question(task):
        raise HTTPException(
            400,
            "can only ask questions on running tasks or open pair tasks "
            f"(no job_id), current status: {task['status']}",
        )

    await repo.add_task_update(db, task_id, body.agent, "question", body.question)
    await repo.update_task(db, task_id, status="needs_info")
    await db.commit()
    await log_activity(
        db,
        "task_question",
        f"Task #{task_id}: agent asked a question",
        detail=mutation_activity_detail(),
    )

    row = await repo.get_task(db, task_id)
    updates = await repo.get_task_updates(db, task_id)
    return row_to_task(row, updates=updates)  # type: ignore[arg-type]


def _is_pair_task(task: dict) -> bool:
    """Pair tasks have no headless dispatch ``job_id``."""
    return not task.get("job_id")


def _pair_resume_status(task: dict) -> str:
    """After needs_info, return to ``running`` if pair-start ran, else ``open``."""
    if task.get("branch"):
        return "running"
    return "open"


async def answer_question(
    db: aiosqlite.Connection,
    task_id: int,
    body: TaskAnswer,
) -> TaskView:
    """Answer a question and optionally resume the task."""
    row = await repo.get_task(db, task_id)
    if not row:
        raise HTTPException(404, "task not found")
    task = dict(row)
    if task["status"] != "needs_info":
        raise HTTPException(
            400,
            f"can only answer needs_info tasks, current status: {task['status']}",
        )

    await repo.add_task_update(db, task_id, "", "answer", body.answer)
    await repo.insert_event(
        db,
        kind="question_answered",
        task_id=task_id,
        actor="human",
        payload={"resume": bool(body.resume)},
    )
    await db.commit()

    if body.resume:
        if _is_pair_task(task):
            resume_status = _pair_resume_status(task)
            await repo.update_task(db, task_id, status=resume_status, job_id=None)
            await db.commit()
            await log_activity(
                db,
                "task_answered",
                f"Task #{task_id}: answered, pair resumed to {resume_status}",
                detail=mutation_activity_detail(),
            )
        else:
            row = await repo.get_task(db, task_id)
            await dispatch_task(db, task_id, dict(row))  # type: ignore[arg-type]
            await log_activity(
                db,
                "task_answered",
                f"Task #{task_id}: answered and re-dispatched",
                detail=mutation_activity_detail(),
            )
    else:
        await repo.update_task(db, task_id, status="open")
        await db.commit()
        await log_activity(
            db,
            "task_answered",
            f"Task #{task_id}: answered, moved to open",
            detail=mutation_activity_detail(),
        )

    row = await repo.get_task(db, task_id)
    updates = await repo.get_task_updates(db, task_id)
    return row_to_task(row, updates=updates)  # type: ignore[arg-type]


async def decide_task(
    db: aiosqlite.Connection,
    task_id: int,
    body: TaskDecide,
) -> TaskView:
    """Human decision after arbiter review: accept or rework.

    When ``decision_summary`` is provided it is always recorded in the
    task update log regardless of the ``record_decision`` flag, so the
    context is visible even without a notes integration.

    When ``record_decision`` is True the summary is additionally persisted
    through the notes plugin (if configured). If the notes adapter is
    unavailable or returns None the core lifecycle continues unaffected.
    """
    row = await repo.get_task(db, task_id)
    if not row:
        raise HTTPException(404, "task not found")
    task = dict(row)
    if task["status"] != "needs_decision":
        raise HTTPException(
            400,
            f"can only decide on needs_decision tasks, current status: {task['status']}",
        )

    summary_text = body.decision_summary.strip() if body.decision_summary else ""

    if body.action == "accept":
        update_content = "Human accepted task after arbiter review."
        if summary_text:
            update_content += f"\nDecision: {summary_text}"
        await repo.add_task_update(db, task_id, "human", "decision", update_content)
        await repo.update_task(db, task_id, status="completed")
        await repo.insert_event(
            db,
            kind="task_completed",
            task_id=task_id,
            actor="human",
            payload={"via": "decide_accept"},
        )
        # #737: the decision gate is a human gate like approve and verdict,
        # but until this event it left no machine-readable trace of the
        # OUTCOME — accept vs rework — so its override-rate was uncountable.
        # entered_at is when the task hit needs_decision (status_entered_at
        # advances only on real status changes, #416), which makes the queue
        # wait computable.
        await repo.insert_event(
            db,
            kind="task_decided",
            task_id=task_id,
            actor="human",
            payload={
                "action": "accept",
                "entered_at": task.get("status_entered_at") or "",
            },
        )
        await db.commit()
        await maybe_rollup_parent(db, task_id)
        await log_activity(
            db,
            "task_decided",
            f"Task #{task_id}: accepted after arbitration",
            detail=mutation_activity_detail(),
        )
    else:
        instructions = body.instructions or "Fix remaining issues."
        update_content = f"Human requested rework after arbiter review: {instructions}"
        if summary_text:
            update_content += f"\nDecision: {summary_text}"
        await repo.add_task_update(db, task_id, "human", "decision", update_content)
        # Rework is the boundary that closes the old arbiter/verdict window
        # (#422): reset the cycle and clear the arbiter marker so the reworked
        # submission starts clean and the stale verdict cannot count as current.
        await repo.update_task(db, task_id, review_cycle=0)
        await repo.reset_arbiter_state(db, task_id)
        # #737: same trace as the accept branch — rework is the "override"
        # outcome of the decision gate.
        await repo.insert_event(
            db,
            kind="task_decided",
            task_id=task_id,
            actor="human",
            payload={
                "action": "rework",
                "entered_at": task.get("status_entered_at") or "",
            },
        )
        await db.commit()

        message = plugins.dispatch.build_fix_message(
            task_id=task_id,
            title=task["title"],
            description=task.get("description", ""),
            review_comments=instructions,
            review_cycle=0,
            max_cycles=config.MAX_REVIEW_CYCLES,
        )
        runtime = task.get("runtime", "auto")
        result = await plugins.dispatch.submit_task(
            message, runtime=runtime, task_id=task_id
        )
        job_id = result.get("job_id")
        if job_id:
            await repo.update_task(db, task_id, status="fix_requested", job_id=job_id)
        else:
            await repo.update_task(db, task_id, status="open")
        await db.commit()
        await log_activity(
            db,
            "task_decided",
            f"Task #{task_id}: rework requested after arbitration",
            detail=mutation_activity_detail(),
        )

    if body.record_decision and summary_text:
        try:
            await plugins.notes.save_decision(
                task_id=task_id,
                action=body.action,
                summary=summary_text,
                context=body.instructions or "",
            )
        except Exception:
            log.warning(
                "Failed to save decision to notes for task #%s (non-fatal)",
                task_id,
                exc_info=True,
            )

    row = await repo.get_task(db, task_id)
    updates = await repo.get_task_updates(db, task_id)
    return row_to_task(row, updates=updates)  # type: ignore[arg-type]


async def _release_done_flow(
    db: aiosqlite.Connection, *, rollback: bool = False
) -> None:
    """Close the done-flow savepoint, tolerating one that is already gone.

    Several branches of this block call ``log_activity``, which commits, and a
    commit ends the transaction and every savepoint in it. That is harmless —
    a committed branch has nothing to undo — but the release afterwards would
    then raise "no such savepoint". Found by the suite, not by reading: an
    earlier probe showed no commit before the git steps and I took that for the
    whole picture; it was true only of the path the probe walked (#364).
    """
    with contextlib.suppress(sqlite3.OperationalError):
        if rollback:
            await db.execute("ROLLBACK TO SAVEPOINT done_flow")
        await db.execute("RELEASE SAVEPOINT done_flow")


async def add_update(
    db: aiosqlite.Connection,
    task_id: int,
    body: TaskUpdateCreate,
    *,
    principal_id: int | None = None,
) -> TaskUpdateView:
    """Add an update to a task; route done reports through shared post-done logic.

    ``principal_id`` comes from the authenticated identity, never from the
    body: ``body.agent`` is a display name the client chooses freely, so an
    update claiming to be from a reviewer proved nothing (#559). The name is
    still stored as sent — a display name is allowed to differ from the
    principal — but now the fact sits next to it.
    """
    row = await repo.get_task(db, task_id)
    if not row:
        raise HTTPException(404, "task not found")
    task = dict(row)

    if body.kind == "done":
        _validate_done_report(task)

    # #498: work that never started its way out, named while the report is
    # being written rather than found weeks later. Advisory by design: the
    # completion is not blocked, and anything unknown stays silent.
    undelivered = ""
    if body.kind == "done":
        from hub.services.delivery_gate import undelivered_warning

        try:
            undelivered = await undelivered_warning(db, task)
        except Exception as exc:  # noqa: BLE001 - advisory, never fatal
            log.warning("delivery check for #%s failed: %s", task_id, exc)

    # Serialize the whole mutation (insert + status transition + commits) on the
    # shared connection so it cannot interleave with a refinement ``_atomic``
    # SAVEPOINT (see get_write_lock). Nothing inside acquires the lock again, so
    # there is no re-entrancy/deadlock. Note: log_activity commits inside here,
    # which is why it must run under the same lock.
    async with get_write_lock(db):
        # #364: the done row and the generation bump are written before the
        # git tail runs, and a git adapter that raises used to leave both
        # behind — reproduced: one done row and generation 1 after the
        # failure, then two and 2 after a retry, so the feed accumulated
        # completion reports that never happened. git itself cannot be rolled
        # back, but the database can, and it is the database that made the
        # false claim. Verified before writing this that no commit runs
        # between the insert and the git steps on this path; a commit would
        # have released the savepoint and defeated the rollback silently.
        await db.execute("SAVEPOINT done_flow")
        released = False
        try:
            update_id = await repo.add_task_update(
                db,
                task_id,
                body.agent,
                body.kind,
                body.content,
                principal_id=principal_id,
                # "anonymous" is not "hub": an open-mode request really has no
                # identity, while the hub writing its own alerts has none by
                # nature. Telling them apart is the point of the field.
                author_kind="principal" if principal_id is not None else "anonymous",
            )

            if body.kind == "done":
                # Verifiable SDD (#510): under 'require', a done report that would
                # complete the task is blocked while the current validation run is
                # red/absent. Only completion-bound reports are gated — one that
                # routes to review (completion_requires_review) is untouched, and
                # the poller path (transition_after_agent_done) is not affected.
                if (
                    config.SDD_VALIDATION == "require"
                    and not completion_requires_review(task)
                ):
                    from hub.services.validation_run import validation_gap

                    vgap = validation_gap(task)
                    if vgap:
                        raise HTTPException(422, f"validation_failed: {vgap}")
                if task["status"] == "pending_report":
                    if completion_requires_review(task):
                        # Universal Review Gate (#306): even the pending_report
                        # path may not complete unreviewed work — the done
                        # report becomes a submission for client-driven review.
                        generation = await repo.bump_submission_generation(db, task_id)
                        await repo.update_task(
                            db, task_id, status="review", review_job_id=None
                        )
                        await repo.add_task_update(
                            db,
                            task_id,
                            "hub",
                            "status",
                            f"Universal Review Gate: done report routed to review "
                            f"(submission #{generation}). Obtain an APPROVED "
                            "verdict via hub_submit_review, then report done "
                            "again.",
                        )
                        await log_activity(
                            db,
                            "task_review_required",
                            f"Task #{task_id} → review (gate) on report from {body.agent}",
                            detail=mutation_activity_detail(),
                        )
                    else:
                        await repo.update_task(db, task_id, status="completed")
                        await log_activity(
                            db,
                            "task_completed",
                            f"Task #{task_id} completed with report from {body.agent}",
                            detail=mutation_activity_detail(),
                        )
                        await maybe_rollup_parent(db, task_id)
                elif task["status"] in ("running", "claimed") and not task.get(
                    "job_id"
                ):
                    # A done report on a pair-running task OR on a reserved (claimed)
                    # task must never be silently dropped: route both through the
                    # shared post-done transition. A claimed task never pair-started,
                    # so it has no branch — transition_after_agent_done then routes
                    # it to completed (no branch ⇒ ci_check is skipped).
                    was_claimed = task["status"] == "claimed"
                    updates_rows = await repo.get_task_updates(db, task_id)
                    updates_list = [dict(r) for r in updates_rows]
                    if any(u.get("kind") == "blocker" for u in updates_list):
                        await repo.update_task(db, task_id, status="needs_decision")
                        await log_activity(
                            db,
                            "task_needs_decision",
                            f"Task #{task_id} → needs_decision (blocker in done flow)",
                            detail=mutation_activity_detail(),
                        )
                    else:
                        await transition_after_agent_done(db, task, has_done=True)
                    if was_claimed:
                        await repo.update_task(
                            db, task_id, claimed_by=None, claim_session_id=None
                        )
                    refreshed = await repo.get_task(db, task_id)
                    if refreshed and dict(refreshed)["status"] == "completed":
                        await maybe_rollup_parent(db, task_id)
                # open + done without pair-start: rejected before insert (AC-2)
            else:
                await repo.update_task(db, task_id)

            await db.commit()
            await log_activity(
                db,
                "task_update",
                f"Task #{task_id} update from {body.agent}: {body.content[:80]}",
                detail=mutation_activity_detail(),
            )

        except BaseException:
            # Roll back to before the done row. RELEASE after ROLLBACK TO is
            # required: rolling back to a savepoint does not remove it (#364).
            await _release_done_flow(db, rollback=True)
            released = True
            raise
        finally:
            if not released:
                await _release_done_flow(db)

    if body.kind == "done":
        await _try_restore_pair_workspace(db, task_id)

    if undelivered:
        await repo.add_task_update(db, task_id, "hub", "alert", undelivered)
        await db.commit()

    update_row = await repo.get_task_update_by_id(db, update_id)
    view = TaskUpdateView(**dict(update_row))  # type: ignore[arg-type]
    if undelivered:
        view.warnings = [undelivered]
    return view


async def refresh_task(
    db: aiosqlite.Connection,
    task_id: int,
) -> TaskView:
    """Sync task status with dispatch job status."""
    row = await repo.get_task(db, task_id)
    if not row:
        raise HTTPException(404, "task not found")
    task = dict(row)
    job_id = task.get("job_id")
    if not job_id:
        return row_to_task(row)

    job = plugins.dispatch.get_job(job_id)
    if job:
        if job.get("status") == "completed":
            # Universal Review Gate (#309): a finished dispatch job must go
            # through the same gate-checked post-done transition as the
            # poller, not straight to completed — otherwise a manual
            # refresh call bypasses the review requirement.
            has_done = await repo.has_done_updates(db, task_id)
            await transition_after_agent_done(
                db,
                task,
                has_done=has_done,
                exit_code=job.get("exit_code"),
                result_text=job.get("result_text"),
            )
            await db.commit()
        elif job.get("status") in ("failed", "running"):
            await repo.update_task(
                db,
                task_id,
                status=job["status"],
                exit_code=job.get("exit_code"),
                result_text=job.get("result_text"),
            )
            await db.commit()

    row = await repo.get_task(db, task_id)
    return row_to_task(row)  # type: ignore[arg-type]


async def reorder_task(
    db: aiosqlite.Connection,
    task_id: int,
    body: TaskReorder,
) -> TaskView:
    """Change the position of a task within its parent."""
    row = await repo.get_task(db, task_id)
    if not row:
        raise HTTPException(404, "task not found")
    await repo.update_task(db, task_id, position=body.position)
    await db.commit()
    row = await repo.get_task(db, task_id)
    return row_to_task(row)  # type: ignore[arg-type]


_TERMINAL_DISPATCH_JOB_STATUSES = frozenset({"completed", "failed"})


def _dispatch_job_blocks_force_complete(
    job_id: str,
    job: dict[str, Any] | None,
) -> tuple[bool, str | None]:
    """Return (blocks, dispatch_status_or_missing)."""
    if job is None:
        return False, "missing"
    job_status = (job.get("status") or "").strip() or "unknown"
    if job_status in _TERMINAL_DISPATCH_JOB_STATUSES:
        return False, job_status
    return True, job_status


def _force_complete_job_overlay_note(
    field: str,
    job_id: str,
    dispatch_status: str | None,
) -> str:
    if dispatch_status == "missing":
        return f"Closed over {field}={job_id!r} (dispatch job missing from registry)."
    return (
        f"Closed over {field}={job_id!r} "
        f"(dispatch job terminal status={dispatch_status!r})."
    )


def _build_force_complete_comment(
    base_comment: str,
    *,
    from_status: str,
    job_id: str | None,
    review_job_id: str | None,
    overlay_notes: list[str],
) -> str:
    audit_bits = [f"from_status={from_status}"]
    if job_id:
        audit_bits.append(f"job_id={job_id}")
    if review_job_id:
        audit_bits.append(f"review_job_id={review_job_id}")
    parts = [base_comment, "[force-complete audit] " + ", ".join(audit_bits)]
    parts.extend(overlay_notes)
    return "\n".join(parts)


async def _has_incomplete_descendants(
    db: aiosqlite.Connection,
    root_id: int,
) -> bool:
    rows = await fetchall(
        db,
        """
        WITH RECURSIVE sub(id) AS (
            SELECT id FROM tasks WHERE parent_id = ?
            UNION ALL
            SELECT t.id FROM tasks t JOIN sub ON t.parent_id = sub.id
        )
        SELECT 1 FROM tasks t
        JOIN sub ON t.id = sub.id
        WHERE t.archived = 0
          AND t.status NOT IN ('completed', 'failed', 'rejected')
        LIMIT 1
        """,
        (root_id,),
    )
    return bool(rows)


_FORCE_COMPLETE_DEFAULT_COMMENT_STATUSES = frozenset({"pending_report", "claimed"})


async def force_complete_task(
    db: aiosqlite.Connection,
    task_id: int,
    body: TaskForceComplete | None = None,
) -> TaskView:
    """Force-complete a stuck task without going through review.

    Human-only audited override: allowed from any non-terminal ``task`` or
    ``subtask`` when no *active* dispatch job backs ``job_id`` or
    ``review_job_id``. Missing or terminal dispatch jobs are permitted and
    noted in the audit trail. ``epic``/``feature`` rows are rejected when
    they still have incomplete descendants.

    The optional ``body.comment`` is recorded as the audit-trail message; if
    omitted, a default human-override message is used.
    """
    row = await repo.get_task(db, task_id)
    if not row:
        raise HTTPException(404, "task not found")
    task = dict(row)
    status = task["status"]
    final_values = {s.value for s in FINAL_STATUSES}
    if status in final_values:
        raise HTTPException(
            400,
            f"cannot force-complete terminal task, current status: {status}",
        )

    task_type = task.get("task_type") or "task"
    if task_type in ("epic", "feature"):
        if await _has_incomplete_descendants(db, task_id):
            raise HTTPException(
                400,
                f"cannot force-complete {task_type} with incomplete descendants",
            )

    overlay_notes: list[str] = []
    for field, label in (("job_id", "job_id"), ("review_job_id", "review_job_id")):
        job_ref = (task.get(field) or "").strip()
        if not job_ref:
            continue
        job = plugins.dispatch.get_job(job_ref)
        blocks, dispatch_status = _dispatch_job_blocks_force_complete(job_ref, job)
        if blocks:
            raise HTTPException(
                409,
                f"active dispatch {label} {job_ref!r} status={dispatch_status!r} "
                "blocks force-complete",
            )
        overlay_notes.append(
            _force_complete_job_overlay_note(label, job_ref, dispatch_status)
        )

    comment_raw = body.comment.strip() if body else ""
    active_statuses = {s.value for s in ACTIVE_STATUSES}
    if (
        status in active_statuses
        and status not in _FORCE_COMPLETE_DEFAULT_COMMENT_STATUSES
        and not comment_raw
    ):
        raise HTTPException(
            400,
            f"force-complete from active status {status!r} requires a non-empty comment",
        )

    base_comment = comment_raw or ("Force-completed by human without agent report.")
    comment = _build_force_complete_comment(
        base_comment,
        from_status=status,
        job_id=(task.get("job_id") or None),
        review_job_id=(task.get("review_job_id") or None),
        overlay_notes=overlay_notes,
    )

    update_fields: dict[str, Any] = {"status": "completed"}
    if task.get("claimed_by") or task.get("claim_session_id") or task.get("claimed_at"):
        update_fields["claimed_by"] = None
        update_fields["claim_session_id"] = None
        update_fields["claimed_at"] = None

    # Serialize against refinement _atomic savepoints on the shared connection.
    async with get_write_lock(db):
        await repo.add_task_update(db, task_id, "human", "done", comment)
        await repo.update_task(db, task_id, **update_fields)
        await db.commit()
        await maybe_rollup_parent(db, task_id)
    row = await repo.get_task(db, task_id)
    updates = await repo.get_task_updates(db, task_id)
    return row_to_task(row, updates=updates)  # type: ignore[arg-type]


async def withdraw_own_draft(
    db: aiosqlite.Connection,
    task_id: int,
    *,
    caller: str,
    caller_principal_id: int | None = None,
) -> TaskView:
    """Archive a single agent-owned draft (no cascade). Agent-only narrow path."""
    row = await repo.get_task(db, task_id)
    if not row:
        raise TaskNotFoundError(f"task {task_id} not found")
    task = dict(row)

    if task.get("source") != TaskSource.agent.value:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail=withdraw_own_draft_error_detail(
                reason="not_agent_draft",
                message="only agent-created drafts can be withdrawn",
                hint=(
                    "hub_withdraw_own_draft applies to source=agent drafts you own. "
                    "For other tasks ask a human to archive."
                ),
                suggested_tool="hub_archive_task",
                required_role="human",
            ),
        )

    if task.get("status") != "draft":
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail=withdraw_own_draft_error_detail(
                reason="invalid_status_for_withdraw",
                message=f"can only withdraw draft tasks, current: {task.get('status')}",
                hint="Only draft tasks can be withdrawn. Approved or active work cannot.",
                current_status=task.get("status"),
                required_status="draft",
                suggested_tool="hub_task_status",
            ),
        )

    assigned = (task.get("assigned_agent") or "").strip()
    principal_match = (
        caller_principal_id is not None
        and task.get("implementer_principal_id") is not None
        and task.get("implementer_principal_id") == caller_principal_id
    )
    if assigned != caller.strip() and not principal_match:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail=withdraw_own_draft_error_detail(
                reason="not_task_owner",
                message="caller is not the assigned agent for this draft",
                hint=(
                    "You can only withdraw drafts assigned to you "
                    "(assigned_agent must match your token identity)."
                ),
            ),
        )

    children = await db_module.get_children(db, task_id)
    if children:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail=withdraw_own_draft_error_detail(
                reason="withdraw_has_children",
                message="draft has non-archived child tasks",
                hint=(
                    "Archive or delete child tasks first, or ask a human to "
                    "hub_archive_task with cascade."
                ),
                suggested_tool="hub_archive_task",
                required_role="human",
            ),
        )

    await repo.set_tasks_archived(db, [task_id], 1)
    await db.commit()
    detail_payload = {
        **json.loads(mutation_activity_detail()),
        "actor": caller.strip(),
        "action": "withdraw_own_draft",
    }
    await log_activity(
        db,
        "task_withdrawn",
        f"Agent {caller} withdrew draft #{task_id}",
        json.dumps(detail_payload, ensure_ascii=False),
    )

    row = await repo.get_task(db, task_id)
    updates = await repo.get_task_updates(db, task_id)
    task_view = row_to_task(row, updates=updates)  # type: ignore[arg-type]
    return await enrich_task_view(db, task_view)


async def archive_task(
    db: aiosqlite.Connection,
    task_id: int,
    *,
    cascade: bool = True,
) -> TaskView:
    """Mark task (and optionally subtree) archived — excluded from default lists."""
    row = await repo.get_task(db, task_id)
    if not row:
        raise TaskNotFoundError(f"task {task_id} not found")
    ids = await repo.collect_subtree_ids(db, task_id) if cascade else [task_id]
    await repo.set_tasks_archived(db, ids, 1)
    await db.commit()
    await log_activity(
        db,
        "task_archived",
        f"Task #{task_id} archived (cascade={cascade}, count={len(ids)})",
        detail=mutation_activity_detail(),
    )
    row = await repo.get_task(db, task_id)
    updates = await repo.get_task_updates(db, task_id)
    task_view = row_to_task(row, updates=updates)  # type: ignore[arg-type]
    return await enrich_task_view(db, task_view)


async def unarchive_task(
    db: aiosqlite.Connection,
    task_id: int,
    *,
    cascade: bool = True,
) -> TaskView:
    """Clear archived flag for task and optionally its subtree."""
    row = await repo.get_task(db, task_id)
    if not row:
        raise TaskNotFoundError(f"task {task_id} not found")
    ids = await repo.collect_subtree_ids(db, task_id) if cascade else [task_id]
    await repo.set_tasks_archived(db, ids, 0)
    await db.commit()
    await log_activity(
        db,
        "task_unarchived",
        f"Task #{task_id} unarchived (cascade={cascade}, count={len(ids)})",
        detail=mutation_activity_detail(),
    )
    row = await repo.get_task(db, task_id)
    updates = await repo.get_task_updates(db, task_id)
    task_view = row_to_task(row, updates=updates)  # type: ignore[arg-type]
    return await enrich_task_view(db, task_view)


async def delete_task_tree(db: aiosqlite.Connection, task_id: int) -> None:
    """Permanently remove a task and all descendants (DB rows and updates)."""
    row = await repo.get_task(db, task_id)
    if not row:
        raise TaskNotFoundError(f"task {task_id} not found")
    n = await repo.delete_task_subtree(db, task_id)
    await db.commit()
    await log_activity(
        db,
        "task_deleted",
        f"Deleted task subtree rooted at #{task_id} ({n} tasks)",
        detail=mutation_activity_detail(),
    )
