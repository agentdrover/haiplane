"""Task lifecycle transitions: create, approve, reject, start, Q&A, decide, etc."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import UTC, datetime
from typing import Any

import contextlib
import sqlite3

import aiosqlite
from fastapi import HTTPException, status

from hub import commit_scope, config
from hub import db as db_module
from hub.actionable_errors import (
    changes_requested_requires_content_detail,
    verdict_contradicts_its_text_detail,
    verdict_repeats_previous_detail,
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
    write_transaction,
)
from hub.integrations.registry import plugins
from hub.services.gate_pipeline import Step, capped_at_warn, policy, run_steps
from hub.services import verdict_text
from hub.services.project_policy import risk_map_for_task
from hub.services.risk_class import derive_risk_class
from hub.models import RiskClass, TaskDeclareWait
from hub.mcp_envelope import enrich_error_payload
from hub.services import finding_outcome
from hub.services.ci_report import adopt_ci_run_report
from hub.services.delivery_state import note_completion_without_delivery
from hub.services.review_evidence import inflight_verdict_note
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
    PairGitMode,
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
from hub.integrations.git_ops import PairBranchConflictError, canonical_task_branch
from hub.services.orchestration import (
    apply_live_worktree,
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
    list_acceptance_criteria,
)

log = logging.getLogger("hub")

_ROLLUP_PARENT_TYPES = frozenset({"feature", "epic"})


def _git_mode_is_remote(task: dict[str, Any] | aiosqlite.Row | None) -> bool:
    """True when pair-start asked the hub host not to touch git (#975)."""
    if task is None:
        return False
    d = dict(task) if not isinstance(task, dict) else task
    return (d.get("git_mode") or PairGitMode.hub.value) == PairGitMode.remote.value


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


def blocker_holding_the_done_report(updates: list[dict[str, Any]]) -> dict | None:
    """Блокер, из-за которого сдачу нельзя вести в доставку, или None (#948).

    Значим не факт, что блокер когда-то был, а то, что он записан ПОСЛЕ
    последней сдачи на ревью. Сдача — это утверждение «работа готова», и всё,
    что было до неё, ревьюер видел вместе с ней: либо препятствие снято, либо
    оно и есть находка ревью. Блокер после сдачи — другое дело: он про
    состояние работы, которое ревью уже не смотрело.

    До этого правила проверка читала всю историю и не знала слова «снят». Один
    блокер за жизнь задачи навсегда уводил её done-отчёт мимо гейта доставки к
    человеку — 25.08 так ушли #851 и #947, обе с APPROVED и зелёным CI, обе с
    блокером, написанным и снятым за часы до сдачи. Хуже прямого убытка был
    стимул: единственный способ не попасть под правило — не писать блокеров,
    то есть молчать ровно о том, ради чего они существуют.

    Задача, которая ни разу не сдавалась, границы не имеет — там значим любой
    блокер, как и раньше. Это не послабление: без сдачи никто на препятствие
    и не смотрел.
    """
    boundary = 0
    for update in updates:
        content = str(update.get("content") or "")
        if update.get("kind") == "status" and content.startswith(
            repo.SUBMISSION_UPDATE_PREFIX
        ):
            boundary = max(boundary, int(update.get("id") or 0))
    for update in reversed(updates):
        if update.get("kind") != "blocker":
            continue
        if int(update.get("id") or 0) > boundary:
            return update
    return None


def blocker_note(blocker: dict[str, Any]) -> str:
    """Как назвать блокер тому, кто теперь должен принимать решение (#948).

    Раньше и апдейт, и лента говорили «blocker in done flow» — читателю
    сообщали, что причина есть, но не какая. На задаче с полусотней апдейтов
    это отправляет человека искать её руками.
    """
    first_line = str(blocker.get("content") or "").strip().splitlines()
    head = first_line[0] if first_line else "(без текста)"
    if len(head) > 160:
        head = head[:157] + "…"
    when = str(blocker.get("created_at") or "").strip() or "время неизвестно"
    return f"апдейт #{blocker.get('id')} от {when}: {head}"


async def _try_restore_pair_workspace(
    db: aiosqlite.Connection,
    task_id: int,
) -> None:
    """Best-effort workspace restore; must not break lifecycle transitions (#451)."""
    row = await repo.get_task(db, task_id)
    if _git_mode_is_remote(row):
        return
    try:
        await restore_pair_workspace_base(db, task_id)
    except Exception as exc:
        log.warning(
            "Failed to restore pair workspace base for task #%s",
            task_id,
            exc_info=True,
        )
        # #1045: the TypeError from a signature mismatch lived only in this
        # stack. The feed is what a person (or the next agent) reads.
        try:
            await repo.add_task_update(
                db,
                task_id,
                "hub",
                "alert",
                f"Уборка worktree не удалась: {exc}. Сдача не прервана.",
            )
            await db.commit()
        except Exception:  # noqa: BLE001 - naming the failure must not fail the caller
            log.warning(
                "could not record worktree cleanup failure for task #%s",
                task_id,
                exc_info=True,
            )


async def _try_switch_pair_workspace_to_task(
    db: aiosqlite.Connection,
    task_id: int,
) -> None:
    """Best-effort workspace switch to the task branch for rework (#457)."""
    row = await repo.get_task(db, task_id)
    if _git_mode_is_remote(row):
        return
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


def _parent_has_own_work(parent: dict[str, Any]) -> bool:
    """True when a feature/epic is itself in progress, not only a container (#1043).

    Umbrella parents (#742) have none of these: empty ``claimed_by``,
    ``claim_session_id``, ``branch``, and no ``pr_number``. A parent that
    was pair-started or claimed is work of its own — children finishing
    does not mean that work was reported.
    """
    if (parent.get("claimed_by") or "").strip():
        return True
    if (parent.get("claim_session_id") or "").strip():
        return True
    if (parent.get("branch") or "").strip():
        return True
    return bool(parent.get("pr_number"))


async def _note_rollup_awaits_own_report(
    db: aiosqlite.Connection, parent_id: int, parent: dict[str, Any]
) -> None:
    """Write the skip to the task tape so the parent is not silently stuck."""
    branch = (parent.get("branch") or "").strip() or "—"
    claimed = (parent.get("claimed_by") or "").strip() or "—"
    await repo.add_task_update(
        db,
        parent_id,
        "hub",
        "status",
        "Родитель готов к сдаче и ждёт своего отчёта: роллап не закрыл "
        f"задачу, потому что у неё есть собственная работа "
        f"(branch={branch}, claimed_by={claimed}).",
    )
    await log_activity(
        db,
        "task_updated",
        f"Task #{parent_id} rollup skipped: parent has its own work, "
        "awaiting its own report",
    )


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
    if _parent_has_own_work(parent):
        await _note_rollup_awaits_own_report(db, parent_id, parent)
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
        parent = dict(parent_row)
        children = await db_module.get_children(db, parent_id)
        if not _children_allow_rollup(children):
            continue
        if _parent_has_own_work(parent):
            continue
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
    Humans and the solo opt-out (HAIPLANE_REVIEW_SELF_APPROVE=allow) pass.

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
    verdict. With HAIPLANE_REVIEW_SELF_APPROVE=allow the warning becomes an
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
                "HAIPLANE_REVIEW_SELF_APPROVE=allow is active: "
                "hub_submit_review will accept your verdict. "
                "This note is informational."
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
            "Solo mode: set HAIPLANE_REVIEW_SELF_APPROVE=allow."
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
        statement_generation=d.get("statement_generation", 0) or 0,
        submission_sha=d.get("submission_sha") or "",
        submission_model=d.get("submission_model") or "",
        review_verdict=d.get("review_verdict"),
        review_verdict_generation=d.get("review_verdict_generation"),
        review_approved_current=review_approved_for_current_submission(d),
        latest_review=latest_review_projection(d),
        waiting_for=d.get("waiting_for") or "",
        waiting_until=d.get("waiting_until") or "",
        waiting_declared_by=d.get("waiting_declared_by") or "",
        branch=d.get("branch"),
        pr_number=d.get("pr_number"),
        git_mode=d.get("git_mode") or PairGitMode.hub,
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

    return await apply_live_worktree(db, task_view)


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
    async with write_transaction(db):
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

    await _warn_on_dead_locators(db, task_id, task.get("task_type"))

    row = await repo.get_task(db, task_id)
    updates = await repo.get_task_updates(db, task_id)
    return row_to_task(row, updates=updates)  # type: ignore[arg-type]


# Which levels get the warning (#1032). The locator gate lives on submission,
# and an epic or a feature never submits — it folds up when its children are
# done. So their acceptance criteria were the one place a test_ref could name
# a test that does not exist and nobody would ever look: #985 closed with
# AC-3 pointing at tests/test_api.py::test_worktree_path_only_when_tree_exists,
# which collects zero tests, under a project policy of sdd_ac_locator=require.
_LOCATOR_WARNED_TYPES = frozenset({"epic", "feature"})


async def _warn_on_dead_locators(
    db: aiosqlite.Connection, task_id: int, task_type: str | None
) -> None:
    """Name AC whose test does not exist, once, at approval (#1032).

    A WARNING and never a gate: at the moment an epic is approved its children
    do not exist yet, so the tests their criteria will be verified by usually
    do not either. Blocking there would either stop the normal order of work
    or teach people to invent test names in advance — both worse than the
    silence being fixed.

    Best-effort in the strict sense: any failure leaves the approval exactly as
    it was. The one thing this must never do is turn "could not check" into
    "checked and clean", so a collection that could not run says nothing at all
    rather than reporting an empty list of dead locators.
    """
    if (task_type or "") not in _LOCATOR_WARNED_TYPES:
        return
    try:
        from hub.services.ac_tests import unresolved_locators

        dead, checked = await unresolved_locators(db, task_id)
    except Exception:  # noqa: BLE001 - the approval must not depend on this
        log.warning("locator check failed for task #%s", task_id, exc_info=True)
        return
    if not checked or not dead:
        return
    named = "; ".join(f"{ac_id} → {nodeid}" for ac_id, nodeid in sorted(dead.items()))
    await repo.add_task_update(
        db,
        task_id,
        "hub",
        "alert",
        "Критерии приёмки ссылаются на несуществующие тесты: "
        f"{named}. Апрув не заблокирован — на верхнем уровне теста может ещё "
        "не быть. Но пока локатор не разрешается, критерий проверить нечем, и "
        "узнать об этом иначе можно только вручную (#1032).",
    )
    await db.commit()


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

    git_mode = body.git_mode
    slug = (body.branch_slug or "").strip()
    if git_mode == PairGitMode.remote:
        # Record the canonical name only. The caller creates this branch in
        # its own clone; the hub host must not checkout, clean, or worktree.
        branch = canonical_task_branch(task_id, slug, task.get("title") or "")
    else:
        try:
            branch = await prepare_pair_branch(db, task_id, task, branch_slug=slug)
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
        "git_mode": git_mode.value,
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
    # Remote pair-start has no hub-host worktree (#975).
    if git_mode != PairGitMode.remote:
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


@dataclass
class SubmitContext:
    """Что шаги сдачи читают и производят до перехода (#1067).

    Двадцать значений пересекают границу транзакции — они и собраны здесь.
    Дата-класс, а не двадцать параметров: шаг, которому понадобилось новое
    поле, дописывает его сюда, а не в сигнатуру каждого соседа.
    """

    db: aiosqlite.Connection
    task_id: int
    task: dict[str, Any]
    # Пустое тело по умолчанию — headless-путь сдаёт без него (#1122): у
    # done-отчёта нет ни ветки, ни accept_areas, ни исходов находок. Общий
    # контекст позволяет обоим путям использовать ОДНИ шаги, и только поэтому
    # два списка можно сравнивать по существу, а не по названиям.
    body: TaskSubmitReview = dc_field(default_factory=TaskSubmitReview)

    resubmitted_from_review: bool = False
    replaced_sha: str = ""
    canonical: str = ""
    reported: str = ""
    diff_paths: list[str] | None = None
    diff_reason: str = ""
    risk_fields: dict[str, Any] = dc_field(default_factory=dict)
    risk_alert: str = ""
    risk_note: str = ""
    surface_note: str = ""
    accepted_paths: list[str] = dc_field(default_factory=list)
    outcome_note: str = ""
    outcome_writes: list[Any] = dc_field(default_factory=list)
    outcome_generation: int = 0
    # Читается из политики при создании контекста, а НЕ внутри шага (#1067,
    # находка ревью PR #247). Присваивание жило в теле _step_submit_rules, и
    # при SUBMIT_RULES=off шаг пропускался целиком — заголовок отчёта выходил
    # «режим правил: » с пустым местом там, где раньше стояло off. Режим
    # политики есть всегда, даже когда шаг по ней не выполняется; это разные
    # вещи, и путать их не должно.
    rules_mode: str = dc_field(
        default_factory=lambda: (config.SUBMIT_RULES or "warn").strip().lower()
    )
    #: Действующий режим ТЕКУЩЕГО шага, выставляется конвейером (#1122).
    #: Пустая строка — шаг выполняется вне конвейера; тогда читается политика.
    gate_mode: str = ""
    rule_lines: list[str] = dc_field(default_factory=list)
    clean_lines: list[str] = dc_field(default_factory=list)
    unchecked_lines: list[str] = dc_field(default_factory=list)
    submission_sha: str = ""
    sha_reason: str = ""
    discovered_pr: int | None = None
    pr_opened_by_hub: bool = False
    pr_ensure_note: str = ""


async def _step_task_is_submittable(state: SubmitContext) -> None:
    """Задача pair и в статусе, из которого сдают (#305, #1054)."""
    if state.task.get("job_id"):
        raise HTTPException(
            400,
            "headless tasks are submitted for review by their done report; "
            "submit-for-review is only for pair tasks without a dispatch job",
        )
    if state.task["status"] not in ("running", "review"):
        raise HTTPException(
            400,
            f"can only submit running or under-review pair tasks for review, "
            f"current status: {state.task['status']}",
        )
    # #1054: what this submission replaces, read before anything is written.
    state.resubmitted_from_review = state.task["status"] == "review"
    state.replaced_sha = (state.task.get("submission_sha") or "").strip()


async def _step_canonical_branch(state: SubmitContext) -> None:
    """Прочитать ветку задачи и ту, что назвал клиент (#1122).

    Отделено от сверки, потому что ветка нужна ОБОИМ путям — её читает шаг
    обнаружения PR, — а сверять её headless-пути не с чем: клиент там ветку
    не сообщает вовсе. Пока чтение и сверка были одним шагом, headless не мог
    взять первое, не взяв второе.
    """
    state.reported = (state.body.branch or "").strip()
    state.canonical = (state.task.get("branch") or "").strip()


async def _step_branch_matches(state: SubmitContext) -> None:
    """Клиент работал в ветке, которой владеет задача (#533)."""
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
    if state.reported and state.canonical and state.reported != state.canonical:
        raise HTTPException(
            409,
            {
                "error": "branch_mismatch",
                "task_id": state.task_id,
                "expected": state.canonical,
                "reported": state.reported,
                "hint": (
                    f"work in the branch this task owns: git switch {state.canonical} "
                    f"(create it from the base branch if it does not exist), or "
                    f"move the commits over. If {state.reported!r} is genuinely the "
                    "right branch, update the task's branch field first so the "
                    "hub, CI and the reviewer all point at the same place."
                ),
            },
        )


async def _step_resolve_diff(state: SubmitContext) -> None:
    """Дифф ветки и пересчёт риск-класса — контекст двух следующих шагов (#583)."""
    # #583: one diff resolution feeds the surface check and the risk-class
    # recompute. Resolved BEFORE the write lock — this walks to the network.
    state.diff_paths, state.diff_reason = await _resolve_branch_diff(
        state.db, state.task
    )
    state.risk_fields, state.risk_alert, state.risk_note = _risk_recompute_on_submit(
        state.task,
        state.diff_paths,
        state.diff_reason,
        await risk_map_for_task(state.db, state.task_id),
    )


async def _step_surfaces(state: SubmitContext) -> None:
    """Объявленная область против фактического диффа (#550, #890)."""
    # #550: before the transition, not after — a refusal has to happen while
    # there is still something to refuse.
    # Режим берётся у конвейера, если тот его назвал: headless объявляет
    # потолок warn, и шаг обязан его соблюдать, а не перечитывать политику
    # мимо потолка (#1122).
    surfaces_mode = (state.gate_mode or (config.SDD_SURFACES or "warn")).strip().lower()
    state.surface_note = ""
    # #890: paths the submitter accepts as the real scope. Empty unless the
    # submission asked for it — the hub never widens affected_areas on its own.
    state.accepted_paths = []
    if surfaces_mode != "off":
        verdict, undeclared, detail = _surface_check(
            state.task, state.diff_paths, state.diff_reason
        )
        if verdict == "undeclared":
            listed = ", ".join(undeclared[:10])
            if state.body.accept_areas:
                # #890: affected_areas is written at DoR as a PREDICTION, and
                # work discovers its own scope — #854 measured 46 of 104
                # submissions changing files outside the declared set, and
                # showed the residue is real surfaces, not routine noise.
                # Refusing that punishes imprecise foresight; what review,
                # commit-scope and the risk recompute actually need is that
                # declared and actual agree AT SUBMISSION. So the submitter
                # may accept the truth in one step — explicitly, and on the
                # record below.
                state.accepted_paths = list(undeclared)
                # Deliberately NOT the "Вне объявленной области" wording: an
                # accepted scope is a recorded fact, not an open divergence.
                state.surface_note = (
                    f"Область: объём признан на сдаче — +{len(undeclared)} "
                    "путь(ей) дописан(ы) в affected_areas."
                )
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
                state.surface_note = (
                    f"Вне объявленной области изменены: {listed}. Режим "
                    "проверки — warn, сдача принята. Область стоит дописать "
                    "(или признать на сдаче через accept_areas): по ней "
                    "сверяется и commit-scope."
                )
        elif verdict == "unknown":
            # Nothing to accept: a check that did not run is not a divergence,
            # and accept_areas must never turn silence into agreement.
            state.surface_note = (
                f"Сверка объявленной области с диффом НЕ выполнялась: {detail}. "
                "Это не значит, что расхождений нет."
            )


async def _step_finding_outcomes(state: SubmitContext) -> None:
    """Исходы находок предыдущей сдачи (#911)."""
    # #911: what became of the findings the PREVIOUS submission was sent back
    # over. Placed with the other pre-transition gates for the same reason they
    # are here — a refusal has to happen while there is still something to
    # refuse — and before the paid layers, because it costs one indexed read.
    #
    # The generation asked about is the CURRENT one, before the bump below: the
    # report for the submission being made does not exist yet. On a first
    # submission there are no reports and the gate is silent, which is the
    # point — it asks only where an answer is owed.
    outcome_mode = (config.FINDING_OUTCOME or "warn").strip().lower()
    state.outcome_note = ""
    state.outcome_writes = []
    state.outcome_generation = int(state.task.get("submission_generation") or 0)
    if outcome_mode != "off":
        open_items = await finding_outcome.open_findings(
            state.db, state.task_id, state.outcome_generation
        )
        try:
            state.outcome_writes, still_open = finding_outcome.plan_outcomes(
                open_items, state.body.finding_outcomes
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if still_open:
            if outcome_mode == "require":
                raise HTTPException(422, finding_outcome.refusal_text(still_open))
            state.outcome_note = finding_outcome.warn_note(still_open)


async def _step_submit_rules(state: SubmitContext) -> None:
    """Дешёвый детерминированный слой по диффу (#855)."""
    # #855: the cheap deterministic layer, on the diff this submission already
    # resolved (#583) — no extra git call and no tokens. It runs BEFORE the
    # paid reviewer for a measured reason: over 30 days the harness confirmed
    # findings at 124k tokens apiece and 61% of its raw findings were
    # rejected, while the categories test-coverage, test-adequacy and
    # missing-test-hides-defect follow from the diff by rule, not by
    # reasoning. Refusals happen here, still before the transition.
    state.rule_lines = []
    state.clean_lines = []
    state.unchecked_lines = []
    if state.rules_mode != "off":
        if state.diff_paths is None:
            # The honesty contract of #550/#725: a check that could not run
            # says so. Silence here would read as "no rule fired".
            state.unchecked_lines.append(
                f"Правила по диффу НЕ проверялись: {state.diff_reason or 'дифф не разрешён'}. "
                "Это не значит, что нарушений нет."
            )
        else:
            code_no_tests = commit_scope.code_without_tests(state.diff_paths)
            if code_no_tests:
                listed = ", ".join(code_no_tests[:10])
                more = (
                    f" и ещё {len(code_no_tests) - 10}"
                    if len(code_no_tests) > 10
                    else ""
                )
                if (state.gate_mode or state.rules_mode) == "require":
                    raise HTTPException(
                        422,
                        f"дифф меняет код и не трогает ни одного теста: "
                        f"{listed}{more}. Принесите тест либо назовите в сдаче "
                        "причину, по которой его здесь быть не должно. "
                        "Правило смотрит на пути в диффе, не на содержимое.",
                    )
                state.rule_lines.append(
                    f"Тесты рядом с кодом: СРАБОТАЛО — дифф меняет код и не "
                    f"трогает ни одного теста: {listed}{more}. Принесите тест "
                    "или назовите причину в сдаче."
                )
            else:
                # Silence on a clean run is deliberate. A line on every
                # submission would be the noise this layer is supposed to
                # replace — and #593 already names the failure mode: a gate
                # that speaks constantly stops being read. What ran cleanly
                # is listed below only when the report exists for some other
                # reason.
                state.clean_lines.append("Тесты рядом с кодом: проверено, чисто.")
            if (state.task.get("work_type") or "") == "bug" and commit_scope.tests_only(
                state.diff_paths
            ):
                # Named, never refused: a missing test genuinely is the whole
                # fix often enough, and a rule that cannot tell the two apart
                # must not be the one deciding.
                state.rule_lines.append(
                    "Баг правит только тесты: отмечено. Тест, написанный под "
                    "уже изменённое поведение, ничего не доказывает о "
                    "починке — но это признак, а не запрет."
                )


async def _step_pin_submission_sha(state: SubmitContext) -> None:
    """Код, который будет судить ревьюер (#572)."""
    # #572: pin the code the reviewer will actually be judging. Resolved by
    # the hub BEFORE the write lock — this walks to the network. An empty
    # result is recorded as empty and the submission proceeds: the pin is
    # protection for the verdict, not a new gate on submitting.
    state.submission_sha, state.sha_reason = await resolve_branch_tip(
        state.db, state.task_id, state.task.get("branch") or ""
    )


async def _step_delivery_pr(state: SubmitContext) -> None:
    """PR, который повезёт работу (#605, #967, #975)."""
    # #605: record which PR carries this work. The pair flow never sets
    # pr_number — only headless create_pr does — so the delivery gate would
    # have keyed on a field nobody fills. The hub looks it up itself; a
    # discovery failure records nothing and the submission proceeds, because
    # a task that genuinely has no PR (config work) must submit exactly as
    # before — the gate then completes it untouched.
    state.discovered_pr = None
    state.pr_opened_by_hub = False
    state.pr_ensure_note = ""
    if not state.task.get("pr_number") and state.canonical:
        from hub.services.orchestration import ensure_delivery_pr, project_git_context

        try:
            ctx = await project_git_context(state.db, state.task_id)
            state.discovered_pr = await plugins.git_ops.pr_for_branch(
                state.canonical,
                repo=ctx.get("repo"),
                gh_repo=ctx.get("gh_repo"),
                forge=ctx.get("forge", ""),
            )
        except Exception as exc:  # noqa: BLE001 - best effort by contract
            log.warning(
                "PR discovery failed for #%s (%s): %s",
                state.task_id,
                state.canonical,
                exc,
            )
        # #967: discovery finding nothing used to end the question — and four
        # tasks in one week ended completed with commits stranded on a branch.
        # When THIS submission's diff (#583, resolved once above) positively
        # shows changes, the hub opens the PR itself: CI then runs in parallel
        # with the review instead of starting after done. A refusal is a
        # warning, never a failed submission — the review in the hub is valid
        # without a PR; the done gate is where the missing PR becomes a block.
        if not state.discovered_pr and state.diff_paths:
            state.discovered_pr, state.pr_ensure_note = await ensure_delivery_pr(
                state.db, state.task, state.canonical, state.diff_paths
            )
            state.pr_opened_by_hub = bool(state.discovered_pr)

    # #975 AC-6: remote pair-start never has a hub-host diff, so None/[] is
    # the normal observation — not the #498 "could not look, stay silent"
    # case. A placeholder project (no gh_repo) cannot open a PR either.
    # Name that on the submit response; do not look like empty success.
    if (
        _git_mode_is_remote(state.task)
        and not state.task.get("pr_number")
        and not state.discovered_pr
        and not state.pr_ensure_note
        and state.canonical
    ):
        from hub.services.orchestration import project_git_context as _git_ctx

        remote_ctx = await _git_ctx(state.db, state.task_id)
        if not (remote_ctx.get("gh_repo") or "").strip():
            state.pr_ensure_note = (
                f"diff/PR для ветки {state.canonical} открыть не удалось: "
                "у проекта нет origin/repo (placeholder workspace). "
                "git_mode=remote — хаб не читает clone на своём хосте."
            )


# Порядок сдачи. Он и был несущим — сетевые резолвы до транзакции, отказ до
# записи, — но держался тем, что никто не переставил блоки. Теперь его можно
# сверить тестом и сравнить с набором headless-пути.
SUBMIT_STEPS: tuple[Step[SubmitContext], ...] = (
    Step("task_is_submittable", _step_task_is_submittable),
    Step("canonical_branch", _step_canonical_branch, refuses=False),
    Step("branch_matches", _step_branch_matches),
    Step("resolve_diff", _step_resolve_diff, refuses=False),
    Step("surfaces", _step_surfaces, mode=policy("SDD_SURFACES")),
    Step("finding_outcomes", _step_finding_outcomes, mode=policy("FINDING_OUTCOME")),
    Step("submit_rules", _step_submit_rules, mode=policy("SUBMIT_RULES")),
    Step("pin_submission_sha", _step_pin_submission_sha, refuses=False),
    Step("delivery_pr", _step_delivery_pr, refuses=False),
)


# Порядок headless-пути (#1122). Тот же список шагов и те же функции, что у
# сдачи pair-задачи: общий SubmitContext с пустым телом позволяет обоим путям
# исполнять ОДНИ шаги, и только поэтому наборы можно сравнивать по существу, а
# не по совпадению названий.
#
# Решение по каждому гейту принято владельцем 01.09.2026 (матрица в апдейте
# #5206 задачи #1122). Два шага объявлены и НЕ выполняются — с причиной прямо
# здесь: молчание в списке неотличимо от «забыли», и именно так расхождение
# двух путей прожило незамеченным до #1067.
#
# Поверхности и правила стоят под потолком warn: путь получает эти гейты
# впервые, и включить их сразу отказом значит остановить поток на первой же
# задаче, которая раньше проходила.
HEADLESS_STEPS: tuple[Step[SubmitContext], ...] = (
    Step("canonical_branch", _step_canonical_branch, refuses=False),
    Step(
        "branch_matches",
        _step_branch_matches,
        inactive_reason=(
            "headless-клиент ветку не сообщает: она принадлежит dispatch-job. "
            "Сравнивать отчёт не с чем, а проверка, которая всегда проходит, "
            "хуже отсутствующей — она выглядит гарантией"
        ),
    ),
    Step("resolve_diff", _step_resolve_diff, refuses=False),
    Step("surfaces", _step_surfaces, mode=capped_at_warn("SDD_SURFACES")),
    Step(
        "finding_outcomes",
        _step_finding_outcomes,
        inactive_reason=(
            "у done-отчёта нет поля finding_outcomes — ответить негде, а гейт, "
            "который спрашивает там, где ответить нечем, либо молчит всегда, "
            "либо ругается всегда. Поле заводится задачей #1155"
        ),
    ),
    Step("submit_rules", _step_submit_rules, mode=capped_at_warn("SUBMIT_RULES")),
    Step("pin_submission_sha", _step_pin_submission_sha, refuses=False),
    Step("delivery_pr", _step_delivery_pr, refuses=False),
)


async def run_headless_submit_gates(
    db: aiosqlite.Connection, task: dict[str, Any]
) -> SubmitContext:
    """Прогнать гейты сдачи для headless-задачи, уходящей в ревью (#1122).

    Вызывается из orchestration в момент Universal Review Gate — там, где
    done-отчёт превращается в сдачу. Возвращает контекст: вызывающий пишет из
    него submission_sha и pr_number в той же транзакции, что и переход, а
    заметки кладёт в ленту.

    Ни один шаг здесь не отказывает: у headless-пути отказ означал бы
    застрявшую задачу без человека рядом, и решение владельца — warn.
    """
    state = SubmitContext(db=db, task_id=task["id"], task=task)
    await run_steps(state, HEADLESS_STEPS)
    return state


async def submit_for_review(
    db: aiosqlite.Connection,
    task_id: int,
    body: TaskSubmitReview | None = None,
) -> TaskView:
    """Submit the current work of a pair task for client-driven review (#305).

    Valid from pair ``running`` and, since #1054, from pair ``review`` as
    well (never with a ``job_id``: headless tasks are submitted by their done
    report and reviewed by the poller conveyor). Bumps the submission
    generation — which invalidates any verdict recorded for earlier work —
    and leaves the task in ``status=review`` with no ``review_job_id``,
    marking the review as client-driven.

    #1054: resubmitting from ``review`` used to be refused, and the refusal
    had no third move behind it. An author who found a defect in his own
    submission — #1042 on 29.08, a parser that read a deleted file as an
    untouched one — could either say nothing, and let the gate merge a branch
    tip no verdict had read (exactly what #612 and #1019 exist to prevent), or
    ask the reviewer to look past the submission at the tip, which is the same
    breach by hand. The task stood until someone else's CHANGES_REQUESTED
    returned it to running. The invariant that makes the transition safe was
    already in place: a verdict is bound to the generation it was written for
    (repository.record_review_verdict binds it in SQL) and only the current
    generation counts, so the bump here retires the previous verdict rather
    than smuggling work past it.
    """
    row = await repo.get_task(db, task_id)
    if not row:
        raise HTTPException(404, "task not found")
    task = dict(row)
    body = body or TaskSubmitReview()

    state = SubmitContext(db=db, task_id=task_id, task=task, body=body)
    await run_steps(state, SUBMIT_STEPS)

    return await _apply_submission(state)


def _submission_update_text(
    state: SubmitContext,
    *,
    generation: int,
    adopted: dict[str, Any] | None,
    declared_model: str,
) -> tuple[str, str]:
    """Текст записи о сдаче — сборка строки, а не запись (#1067).

    Пятнадцать ветвлений из двадцати трёх у ``_apply_submission`` были здесь,
    и все они про то, ЧТО написать в ленту: открыт ли PR хабом, закреплён ли
    SHA, пересдача ли это, принят ли найденный PR, сменился ли риск-класс.
    Ни одна из них ничего не пишет — запись делает вызывающий одной строкой
    ниже, внутри той же транзакции.

    Три аргумента идут отдельно от контекста намеренно: ``generation``,
    ``adopted`` и ``declared_model`` вычисляются ВНУТРИ транзакции и до неё
    не существуют. Класть их в SubmitContext значило бы завести полям место,
    где они половину жизни пустые.
    """
    agent = (state.body.agent or "").strip() or state.task.get("assigned_agent", "")
    summary = (state.body.summary or "").strip()
    content = f"{repo.SUBMISSION_UPDATE_PREFIX}{generation})."
    if state.pr_opened_by_hub:
        content += f" PR #{state.discovered_pr} открыт хабом для доставки (#967)."
    elif state.discovered_pr:
        content += f" PR #{state.discovered_pr} recorded for delivery."
    if state.submission_sha:
        content += f" Branch tip at submission: {state.submission_sha[:12]}."
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
        content += f" Branch tip NOT pinned: {state.sha_reason}."
    if state.resubmitted_from_review:
        # #1054: someone may be reading the previous submission right now.
        # Both commits are named, so what he is looking at and what
        # replaced it are in the feed rather than in his assumptions.
        replaced = state.replaced_sha[:12] if state.replaced_sha else "—"
        content += (
            f" Пересдача из review: сдача {replaced} заменена на "
            f"{state.submission_sha[:12] if state.submission_sha else '—'}; вердикт "
            "по заменённой сдаче больше не текущий."
        )
    if adopted:
        # #1056: the count used to stand here, so five not_found results
        # printed exactly like five passing ones. The reader of the feed —
        # the human at the gate — got "5 AC result(s)" as if it were
        # evidence. What is printed now is the outcome.
        from hub.services.ac_tests import describe_recorded_results

        ac_phrase = describe_recorded_results(adopted.get("ac_recorded"))
        v_status = adopted.get("validation_status") or "—"
        content += (
            f" CI run report adopted for this commit: {ac_phrase}, "
            f"validation {v_status}."
        )
    if state.risk_note:
        content += state.risk_note
    if declared_model:
        content += f" Модель исполнителя (декларация): {declared_model}."
    if summary:
        content += f" {summary}"
    return agent, content


async def write_submission_notices(state: SubmitContext) -> None:
    """Публичное имя: заметки пишет и headless-путь (#1122).

    Раньше функция была приватной, потому что вызывающий был один. Теперь их
    два, и подчёркивание врало бы о границе.

    Здесь же — сорвавшийся пиннинг, и только здесь. Пара говорит о нём в
    тексте самой сдачи («Branch tip NOT pinned: …»), а у headless такого
    текста нет вовсе: он зовёт только эти заметки. Без этой записи вершина не
    закреплялась МОЛЧА — вердикт относился бы к номеру сдачи, а не к коду,
    ровно та дыра, которую задача и закрывает (#572, #767).

    Задвоения на pair-пути нет по устройству: тот зовёт приватную
    ``_write_submission_notices`` напрямую, минуя эту обёртку.
    """
    if not state.submission_sha:
        # Непроверенное — это состояние, которое читатель обязан видеть, а не
        # отсутствие новостей (#534, #572). Причина называется: «не смогли
        # посмотреть» и «нечего было закреплять» лечатся по-разному.
        await repo.add_task_update(
            state.db,
            state.task_id,
            "hub",
            "alert",
            "Вершина ветки НЕ закреплена: "
            + (state.sha_reason or "причина не названа")
            + ". Вердикт будет относиться к номеру сдачи, а не к коду.",
        )
    await _write_submission_notices(state)


async def _write_submission_notices(state: SubmitContext) -> None:
    """Заметки о сдаче в ленту: что проверено, чего не хватает (#1067).

    Вынесено вместе с записями, а не только со сборкой текста: блок целиком
    про уведомление читателя и ничего не решает о переходе. Все входы —
    из контекста, поэтому это перемещение, а не переработка.

    Вызывается ВНУТРИ транзакции: заметка о сдаче и сама сдача обязаны
    попасть в базу вместе, иначе лента расскажет о переходе, которого не
    было, или умолчит о состоявшемся.
    """
    report_lines = [
        ln for ln in ([state.surface_note, state.outcome_note] + state.rule_lines) if ln
    ]
    report_lines += state.unchecked_lines
    if report_lines:
        # Only now is "what ran and found nothing" worth printing: inside
        # a report the reader is already looking at.
        report_lines += state.clean_lines
        header = f"Отчёт проверок на сдаче (режим правил: {state.rules_mode})."
        await repo.add_task_update(
            state.db,
            state.task_id,
            "hub",
            "alert",
            header + "\n— " + "\n— ".join(report_lines),
        )
    if state.pr_ensure_note:
        # #967: the refusal half of AC-1 — the reader learns the PR is
        # missing NOW, at submission, not from the done gate later.
        await repo.add_task_update(
            state.db,
            state.task_id,
            "hub",
            "alert",
            f"{state.pr_ensure_note}. Сдача принята — ревью валидно и без PR, "
            "но done без него не завершится: гейт откроет PR сам или "
            "спросит человека (#967).",
        )
    if state.risk_alert:
        await repo.add_task_update(
            state.db, state.task_id, "hub", "alert", state.risk_alert
        )


async def _dispatch_cross_model_review(db: aiosqlite.Connection, task_id: int) -> None:
    """Кросс-модельного ревьюера зовёт хаб, а не исполнитель (#757).

    Best-effort по контракту: неудача диспетча пишет в лог и НЕ ломает сдачу.
    Отдельной функцией — чтобы это обещание было видно в имени и подписи, а не
    только в комментарии посреди перехода.
    """
    try:
        from hub.services.review_dispatch import maybe_dispatch_review

        await maybe_dispatch_review(db, task_id)
    except Exception:  # noqa: BLE001 - dispatch must never break a submit
        log.exception("cross-model review dispatch failed for task #%s", task_id)


async def _warn_on_branch_stacking(
    db: aiosqlite.Connection, task_id: int, branch: str
) -> dict[str, Any] | None:
    """Ветка везёт коммиты другой несмерженной задачи — предупредить (#438).

    Предупреждение, никогда не запрет: стек может быть осознанным решением,
    поэтому находка — это алерт в ленту и подсказка в ответе, а не отказ в
    сдаче. Возвращает находку, чтобы вызывающий дописал подсказку в ответ.
    """
    stacking = await detect_branch_stacking(db, task_id, branch)
    if stacking:
        await repo.add_task_update(db, task_id, "hub", "alert", stacking["message"])
        await db.commit()
    return stacking


async def _apply_submission(state: SubmitContext) -> TaskView:
    """Сам переход: запись статуса, пиннинг сдачи и всё, что за ними (#1067).

    Отделено от гейтов не ради красоты, а по замеру: из 82 ветвлений
    submit_for_review 37 несло именно тело перехода — больше, чем все гейты
    вместе. Вынос одних гейтов оставлял функцию с цикломатикой 24 при цели
    15, и порог из #1066 её бы не пропустил.

    Перенесено ЦЕЛИКОМ, строка в строку: это перемещение, а не переработка.
    Единственный отказ внутри транзакции — compare-and-swap
    ``transition_status_if`` — остаётся здесь и гейтом не является: это
    оптимистическая блокировка самого перехода, и вынести её наружу значило
    бы её сломать.
    """
    db = state.db
    task_id = state.task_id
    task = state.task
    body = state.body

    # Контракт между конвейером и переходом, выписанный целиком: шесть
    # значений — ровно те, что шаги производят, а транзакция ниже читает.
    # Список здесь, а не по месту, чтобы шаг, забывший что-то заполнить, был
    # виден одним взглядом; ruff же ловит обратное — значение, которое больше
    # никому не нужно (так отсюда ушли четыре: их «использование» ниже
    # оказалось упоминанием в комментарии, а не чтением).
    # Тело перехода при этом не изменилось ни на строку.
    risk_fields = state.risk_fields
    accepted_paths = state.accepted_paths
    outcome_writes = state.outcome_writes
    outcome_generation = state.outcome_generation
    submission_sha = state.submission_sha
    discovered_pr = state.discovered_pr

    async with write_transaction(db):
        if not await repo.transition_status_if(
            db,
            task_id,
            expected_from=task["status"],
            new_status="review",
        ):
            raise HTTPException(
                409,
                f"Task #{task_id} left {task['status']} state during submit; "
                "retry from its current status",
            )
        # #911: only now, past every gate that could still refuse. The write
        # goes to the process-wide connection, and a refusal after it would
        # leave the author's outcomes recorded for a submission that never
        # happened — their corrected retry would then be told the finding is
        # already closed. Written BEFORE the bump so the rows belong to the
        # generation they answer, not to the one starting here.
        outcome_drafts: list[int] = []
        if outcome_writes:
            outcome_drafts = await finding_outcome.apply_outcomes(
                db,
                task_id,
                outcome_generation,
                outcome_writes,
                reported_by=(body.agent or task.get("assigned_agent") or ""),
            )
        if outcome_drafts:
            # Пишем В КОНТЕКСТ: заметку ниже печатает
            # _write_submission_notices, и она читает его, а не локальную.
            state.outcome_note = (
                (state.outcome_note + " " if state.outcome_note else "")
                + f"Исходы находок: заведено дефект-драфтов {len(outcome_drafts)} — "
                + ", ".join(f"#{i}" for i in outcome_drafts)
                + ". Находка, которую не чинят, остаётся работой, а не исчезает."
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
        # #880: the ledger of what each generation pinned. Written here, in the
        # same transaction that pins the sha, because tasks.submission_sha is
        # overwritten by the next submission and the previous commit would
        # otherwise survive only as prose in an update.
        from hub.services.orchestration import project_git_context as _git_ctx

        ledger_ctx = await _git_ctx(db, task_id)
        await repo.record_submission(
            db,
            task_id=task_id,
            generation=generation,
            sha=submission_sha,
            base_branch=(ledger_ctx.get("base_branch") or config.PAIR_BASE_BRANCH),
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
        agent, content = _submission_update_text(
            state,
            generation=generation,
            adopted=adopted,
            declared_model=declared_model,
        )
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
        # #855: ONE report instead of scattered alerts. The area verdict is a
        # line in it, not a second independent message — a submission should
        # leave the reader with a single list of what was checked. The wording
        # of each line is preserved verbatim, so anything that read the old
        # alerts still finds its phrase.
        await _write_submission_notices(state)
        await db.commit()
        await log_activity(
            db,
            "task_submitted_for_review",
            f"Task #{task_id} submitted for review (generation {generation})",
            detail=mutation_activity_detail(),
        )

    stacking = await _warn_on_branch_stacking(db, task_id, task.get("branch") or "")

    await _dispatch_cross_model_review(db, task_id)

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
    if state.pr_ensure_note:
        # Agent-facing copy of the feed alert: MCP/REST must not look like
        # a silent success when the PR could not be made (#975 AC-6, #967).
        view.lifecycle_hint = (
            f"{view.lifecycle_hint}\n{state.pr_ensure_note}"
            if view.lifecycle_hint
            else state.pr_ensure_note
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


async def _previous_verdict_update(db: Any, task_id: int) -> dict | None:
    """The last verdict recorded on this task, or None when there is none (#1057)."""
    rows = await repo.get_task_updates(db, task_id)
    verdicts = [dict(r) for r in rows if dict(r).get("kind") == "review"]
    return verdicts[-1] if verdicts else None


@dataclass
class VerdictContext:
    """Что шаги вердикта читают и производят до его записи (#1067).

    Шесть значений пересекают границу транзакции. Форма та же, что у
    ``SubmitContext``, и намеренно: два конвейера, читаемые рядом, должны
    выглядеть одинаково — иначе сравнивать их наборы шагов будет некому.
    """

    db: aiosqlite.Connection
    task_id: int
    task: dict[str, Any]
    body: TaskReviewVerdict
    # Флаги вызова, а не производное шагов: приходят из ручки и нужны записи.
    self_approved: bool = False
    principal_id: int | None = None

    body_text: str = ""
    pinned_sha: str = ""
    diverged_tip: str = ""
    sha_note: str = ""
    undisposed_note: str = ""
    inflight_note: str = ""
    auto_created: list[int] = dc_field(default_factory=list)


async def _vstep_has_a_submission(state: VerdictContext) -> None:
    """Есть что рецензировать: сдача хотя бы одна была."""
    if (state.task.get("submission_generation") or 0) == 0:
        raise HTTPException(
            400,
            "no submission to review yet: the task has never been submitted for review",
        )


async def _vstep_changes_requested_has_content(state: VerdictContext) -> None:
    """«Вернуть и переделать» обязано сказать, что переделать (#1010)."""
    # #1010: "take it back and redo it" must say what to redo. On 28.08 a verdict
    # came in with no findings and no comments: the task went back to running,
    # the feed said "Review verdict: CHANGES_REQUESTED" and nothing else, and
    # the developer's only options were to guess or to ask the human the gate
    # exists to spare. Every neighbouring gate already demands content — DoR
    # refuses a task without acceptance criteria, submission refuses a branch
    # it cannot name, delivery refuses a task without a live verdict — and
    # this one asked for nothing. Either field satisfies it: one sentence is a
    # reason, and demanding structured findings for "tests are red" would buy
    # a formality. APPROVED stays free of the requirement (it is
    # self-sufficient), which is why it is now filed under its author instead
    # — see the principal_id passed from the web form.
    if (
        state.body.verdict.value == "changes_requested"
        and not state.body.findings
        and not state.body.comments.strip()
    ):
        raise HTTPException(422, detail=changes_requested_requires_content_detail())


async def _vstep_verdict_matches_its_text(state: VerdictContext) -> None:
    """Вердикт не противоречит собственному тексту (#1057)."""
    # #1057: the same gate, two more things it can see. Both refusals happen
    # here, before any write, so a rejected verdict leaves the task in review
    # with its submission generation untouched — the property #1010 established
    # and the reason these checks belong beside it rather than in the caller.
    state.body_text = verdict_text.verdict_body(
        state.body.comments, [f.message for f in state.body.findings]
    )
    declared = verdict_text.declared_outcome(state.body_text)
    if declared and declared != state.body.verdict.value:
        raise HTTPException(
            422,
            detail=verdict_contradicts_its_text_detail(
                state.body.verdict.value, declared
            ),
        )


async def _vstep_verdict_is_not_a_repeat(state: VerdictContext) -> None:
    """Вердикт не повторяет дословно предыдущий (#1057)."""
    if state.body_text.strip() and not state.body.acknowledge_repeat:
        previous = await _previous_verdict_update(state.db, state.task_id)
        if previous is not None:
            said_before = verdict_text.fingerprint(
                verdict_text.previous_verdict_body(previous["content"])
            )
            if said_before and said_before == verdict_text.fingerprint(state.body_text):
                raise HTTPException(
                    422,
                    detail=verdict_repeats_previous_detail(str(previous["created_at"])),
                )


async def _vstep_changes_requested_has_in_scope_finding(state: VerdictContext) -> None:
    """У возврата есть хотя бы одна находка в объёме."""
    if state.body.verdict.value == "changes_requested" and state.body.findings:
        if all(f.scope == FindingScope.out_of_scope for f in state.body.findings):
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


async def _vstep_machine_review_present(state: VerdictContext) -> None:
    """Машинное ревью обязательно для аппрува (#382)."""
    # Machine-review hard gate (#382): only in HAIPLANE_MACHINE_REVIEW=require,
    # and only for APPROVED — the reviewer must always be able to reject work
    # (changes_requested), harness or no harness. Default 'warn' keeps every
    # verdict available; the panel shows the gap.
    if (
        config.MACHINE_REVIEW_MODE == "require"
        and state.body.verdict.value == "approved"
    ):
        from hub.services.orchestration import machine_review_gap

        gap = await machine_review_gap(state.db, state.task)
        if gap:
            raise HTTPException(
                422,
                f"machine-review обязателен для аппрува этой задачи: {gap}",
            )


async def _vstep_ac_tests_green(state: VerdictContext) -> None:
    """Аппрув требует зелёных AC-тестов (#508)."""
    # Verifiable SDD (#508): under 'require', an APPROVED verdict needs every
    # current verifiable_by=test AC green. Only APPROVED is gated — a reviewer
    # must always be able to reject red work (lesson from #382).
    if config.SDD_AC_TESTS == "require" and state.body.verdict.value == "approved":
        from hub.services.ac_tests import ac_tests_gap

        ac_gap = await ac_tests_gap(state.db, state.task)
        if ac_gap:
            raise HTTPException(422, f"ac_tests_not_green: {ac_gap}")


async def _vstep_branch_tip_matches(state: VerdictContext) -> None:
    """Ветка стоит там же, где на сдаче (#572). Не отказывает — называет."""
    # #572: does the branch still stand where it stood at submission? Only
    # APPROVED is checked — it is the verdict that creates the false safety of
    # review_approved_current, while changes_requested returns the task to
    # work anyway. Three outcomes, never collapsed: diverged / match /
    # could-not-check with the reason. Resolved before the write lock (it
    # walks to the network), and a resolution failure degrades to a visible
    # "unchecked" — a verdict must not be hostage to the remote.
    state.pinned_sha = (state.task.get("submission_sha") or "").strip()
    state.diverged_tip = ""
    state.sha_note = ""
    if state.body.verdict.value == "approved":
        if not state.pinned_sha:
            state.sha_note = (
                "Сверка кода с моментом сдачи НЕ проводилась: вершина ветки "
                "не была записана при сдаче. Вердикт относится к номеру "
                "сдачи, не к коммиту."
            )
        else:
            current_tip, tip_reason = await resolve_branch_tip(
                state.db, state.task_id, state.task.get("branch") or ""
            )
            if not current_tip:
                state.sha_note = (
                    f"Сверка кода с моментом сдачи НЕ проводилась: {tip_reason}. "
                    f"Сдавался коммит {state.pinned_sha[:12]}."
                )
            elif current_tip != state.pinned_sha:
                state.diverged_tip = current_tip


async def _vstep_approval_blind_spots(state: VerdictContext) -> None:
    """Что аппрув может молча перекрыть (#1012). Не отказывает — называет."""
    # #1012: the other thing an approval can quietly override. The report is
    # already bound to a submission and already knows whether the gate judged
    # its findings; what was missing is anyone saying so at the moment the
    # verdict is written. Read here, next to the sha check, for the same
    # reason: both are questions about what the approver may not have seen,
    # and neither may hold the write lock while it answers.
    state.undisposed_note = ""
    state.inflight_note = ""
    if state.body.verdict.value == "approved":
        from hub.services.review_evidence import (
            attach_dispositions,
            undisposed_confirmed,
        )
        from hub.services.review_evidence import undisposed_note as _undisposed_note
        from hub.models import MachineReviewView

        mr_row = await repo.get_latest_machine_review(state.db, state.task_id)
        if mr_row is not None:
            mr_view = MachineReviewView(**dict(mr_row))
            mr_view.is_current = mr_view.submission_generation == (
                state.task.get("submission_generation") or 0
            )
            await attach_dispositions(state.db, mr_view)
            state.undisposed_note = _undisposed_note(*undisposed_confirmed(mr_view))

        has_current_report = False
        if mr_row is not None:
            has_current_report = int(mr_row["submission_generation"] or 0) == (
                state.task.get("submission_generation") or 0
            )
        state.inflight_note = await inflight_verdict_note(
            state.db, state.task, has_current_report=has_current_report
        )


async def _vstep_auto_draft_out_of_scope(state: VerdictContext) -> None:
    """Драфты по внеобъёмным находкам — до записи вердикта."""
    # Auto-draft follow-ups BEFORE persisting the verdict so the created
    # ids land in the stored findings (create_task commits on its own, so
    # it must run outside the verdict's write-lock critical section).
    state.auto_created = []
    if state.body.create_tasks_for_out_of_scope and state.body.findings:
        state.auto_created = await create_drafts_for_out_of_scope_findings(
            state.db, state.task, state.body
        )


# Порядок вердикта. Дешёвые проверки — до сетевых: сверка вершины ветки и
# чтение отчёта машинного ревью ходят наружу, и гонять их ради отказа,
# который уже случился, значит платить за него временем.
VERDICT_STEPS: tuple[Step[VerdictContext], ...] = (
    Step("has_a_submission", _vstep_has_a_submission),
    Step("changes_requested_has_content", _vstep_changes_requested_has_content),
    Step("verdict_matches_its_text", _vstep_verdict_matches_its_text),
    Step("verdict_is_not_a_repeat", _vstep_verdict_is_not_a_repeat),
    Step(
        "changes_requested_has_in_scope_finding",
        _vstep_changes_requested_has_in_scope_finding,
    ),
    Step(
        "machine_review_present",
        _vstep_machine_review_present,
        mode=policy("MACHINE_REVIEW_MODE"),
    ),
    Step("ac_tests_green", _vstep_ac_tests_green, mode=policy("SDD_AC_TESTS")),
    Step("branch_tip_matches", _vstep_branch_tip_matches, refuses=False),
    Step("approval_blind_spots", _vstep_approval_blind_spots, refuses=False),
    Step("auto_draft_out_of_scope", _vstep_auto_draft_out_of_scope, refuses=False),
)


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

    Content (#1010): ``changes_requested`` is refused without a reason —
    either one finding or a non-empty ``comments``. The refusal happens
    before any write, so the task stays in ``review`` and the submission
    generation is untouched: a rejected verdict must not consume the
    submission it was rejected on. ``approved`` carries no such requirement.

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

    state = VerdictContext(
        db=db,
        task_id=task_id,
        task=task,
        body=body,
        self_approved=self_approved,
        principal_id=principal_id,
    )
    await run_steps(state, VERDICT_STEPS)

    return await _apply_verdict(state)


def _verdict_update_text(state: VerdictContext) -> tuple[str, str]:
    """Текст записи о вердикте — сборка строки, а не запись (#1067).

    Девятнадцать ветвлений из двадцати у ``_apply_verdict`` были здесь, и все
    про то, ЧТО написать в ленту: разошлась ли ветка с моментом сдачи, есть
    ли неразмеченные находки, идёт ли ещё харнесс, сам ли себя одобрил автор,
    что за находки и комментарии. Ни одна ничего не пишет — запись делает
    вызывающий строкой ниже, внутри той же транзакции.
    """
    agent = (state.body.agent or "").strip() or "reviewer"
    content = f"Review verdict: {state.body.verdict.value.upper()}"
    if state.diverged_tip:
        content += (
            f"\nКОД УШЁЛ ИЗ-ПОД ОДОБРЕНИЯ: сдавался {state.pinned_sha[:12]}, "
            f"вершина ветки теперь {state.diverged_tip[:12]}. Вердикт записан "
            f"для {state.pinned_sha[:12]} и НЕ распространяется на новые "
            "коммиты; задача возвращена в running — пересдайте, чтобы "
            "ревью увидело текущий код."
        )
    elif state.sha_note:
        content += f"\n{state.sha_note}"
    if state.undisposed_note:
        content += f"\n{state.undisposed_note}"
    if state.inflight_note:
        content += f"\n{state.inflight_note}"
    if state.self_approved:
        content += " [self-approved: solo mode, HAIPLANE_REVIEW_SELF_APPROVE=allow]"
        log.warning(
            "Task #%s: review verdict %s accepted via "
            "HAIPLANE_REVIEW_SELF_APPROVE=allow — reviewer '%s' "
            "implemented this task (no independent review)",
            state.task_id,
            state.body.verdict.value,
            agent,
        )
    if state.body.findings:
        # Human-readable echo only; the canonical structured findings
        # live on the task row, so the update text can stay compact.
        for f in state.body.findings[:20]:
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
            content += f"\n{f.id}. [{f.severity.value}]{place}{scope_mark} {f.message}"
        if len(state.body.findings) > 20:
            content += f"\n… and {len(state.body.findings) - 20} more findings"
        unlinked = [
            f.id
            for f in state.body.findings
            if f.scope == FindingScope.out_of_scope and not f.linked_task_id
        ]
        if unlinked:
            ids = ", ".join(str(i) for i in unlinked)
            content += (
                f"\nWarning: out-of-scope finding(s) {ids} have no "
                "linked_task_id — create follow-up task(s) and link them."
            )
        if state.auto_created:
            ids = ", ".join(f"#{i}" for i in state.auto_created)
            content += (
                f"\nAuto-created draft task(s) for out-of-scope "
                f"findings: {ids} (awaiting human DoR approval)."
            )
    if state.body.comments.strip():
        content += f"\n{state.body.comments.strip()}"
    return agent, content


async def _apply_verdict(state: VerdictContext) -> TaskView:
    """Запись вердикта и всё, что за ней (#1067).

    Отделено от гейтов по тому же шву и по той же причине, что у сдачи: из
    68 ветвлений record_review_verdict гейты несли 36, запись — 32. Вынос
    одних гейтов оставлял функцию с цикломатикой 21 при пороге 15.

    Перенесено целиком, строка в строку: перемещение, не переработка.
    """
    db = state.db
    task_id = state.task_id
    task = state.task
    body = state.body
    self_approved = state.self_approved
    principal_id = state.principal_id

    # Контракт между конвейером и записью вердикта.
    pinned_sha = state.pinned_sha
    diverged_tip = state.diverged_tip
    sha_note = state.sha_note
    undisposed_note = state.undisposed_note
    inflight_note = state.inflight_note

    async with write_transaction(db):
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
        agent, content = _verdict_update_text(state)
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
    if undisposed_note:
        # Appended, never replacing: a diverged tip and unanswered findings are
        # two different things the approver may not have seen, and dropping one
        # to make room for the other is how a warning becomes decorative.
        view.lifecycle_hint = (
            f"{view.lifecycle_hint}\n{undisposed_note}"
            if view.lifecycle_hint
            else undisposed_note
        )
    if inflight_note:
        view.lifecycle_hint = (
            f"{view.lifecycle_hint}\n{inflight_note}"
            if view.lifecycle_hint
            else inflight_note
        )
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


async def declare_task_wait(
    db: aiosqlite.Connection, task_id: int, body: "TaskDeclareWait"
) -> TaskView:
    """Declare or clear a task's wait, with the deadline made non-optional (#957).

    The deadline is the entire safety of the feature: a wait without one is
    just silence with better manners, and the risk register names it first —
    "declare a wait and vanish". So a declaration is refused without a
    parseable future deadline, while clearing needs nothing at all.
    """
    row = await repo.get_task(db, task_id)
    task = dict(_existing_task(row, task_id))
    if bool(task.get("archived")):
        raise HTTPException(400, "cannot declare a wait on an archived task")

    waiting_for = (body.waiting_for or "").strip()
    waiting_until = (body.waiting_until or "").strip()
    agent = (body.agent or "").strip() or "agent"
    if waiting_for:
        try:
            deadline = datetime.strptime(waiting_until, "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=UTC
            )
        except ValueError as exc:
            raise HTTPException(
                422,
                detail=enrich_error_payload(
                    {
                        "reason": "wait_needs_deadline",
                        "actor_hint": "agent",
                        "message": (
                            "объявленное ожидание требует срока в формате "
                            "YYYY-MM-DD HH:MM:SS (UTC); получено: "
                            f"'{waiting_until or 'пусто'}'"
                        ),
                        "hint": (
                            "Бессрочное «жду» неотличимо от брошенной задачи — "
                            "ровно та путаница, от которой лечится вахта. "
                            "Назовите момент, после которого молчание станет "
                            "просрочкой."
                        ),
                    }
                ),
            ) from exc
        if deadline <= datetime.now(UTC):
            raise HTTPException(
                422,
                detail=enrich_error_payload(
                    {
                        "reason": "wait_deadline_in_past",
                        "actor_hint": "agent",
                        "message": f"срок ожидания уже прошёл: {waiting_until}",
                        "hint": (
                            "Ожидание объявляют вперёд. Прошедший срок — это "
                            "уже просрочка, и вахта скажет об этом сама."
                        ),
                    }
                ),
            )
    await repo.declare_wait(
        db,
        task_id,
        waiting_for=waiting_for,
        waiting_until=waiting_until if waiting_for else "",
        agent=agent,
    )
    fresh = await repo.get_task(db, task_id)
    return row_to_task(_existing_task(fresh, task_id))


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
        # #897: the acceptance is done and stays done — this only refuses to
        # let it be silent. On 21.08.2026 exactly this transition left #878 and
        # #885 completed with their PRs open, and nothing in the task said so.
        await deliver_on_disposition(
            db, task_id, getattr(body, "pr_disposition", "") or "", via="decide_accept"
        )
        await note_completion_without_delivery(
            db,
            task_id,
            via="decide_accept",
            disposition=getattr(body, "pr_disposition", "") or "",
        )
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
        # #971: the hub can now deliver approved pair work on its own, so an
        # agent that went away and came back may find its task already
        # complete. Refusing its report with "task_already_terminal" would be
        # true and useless: nothing is wrong, the work shipped, and the only
        # thing still missing is the account of it. The report is taken as
        # what it now is — a record — and the terminal guard stays exactly as
        # strict for every other way a task can end.
        if task.get(
            "status"
        ) == "completed" and await repo.completed_by_poller_delivery(db, task_id):
            body = body.model_copy(update={"kind": "status"})
        else:
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
    async with write_transaction(db):
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
                    # #948: значим блокер, записанный ПОСЛЕ последней сдачи, а
                    # не любой в истории. Снятое препятствие больше не уводит
                    # сдачу мимо доставки, а живое — уводит, как и уводило.
                    holding = blocker_holding_the_done_report(updates_list)
                    if holding is not None:
                        note = blocker_note(holding)
                        await repo.update_task(db, task_id, status="needs_decision")
                        await repo.add_task_update(
                            db,
                            task_id,
                            "hub",
                            "alert",
                            # #952: same rule as the merge-gate alert — the
                            # task is entering needs_decision, where a fresh
                            # done report is refused, so the hint must route
                            # through the decision, not around it.
                            f"Отчёт о готовности не пошёл в доставку: после "
                            f"последней сдачи записан блокер — {note}. Решение "
                            "за человеком (hub_decide_task): rework вернёт "
                            "задачу в running — снимите препятствие и "
                            "пересдайте done; accept завершит без доставки.",
                        )
                        await log_activity(
                            db,
                            "task_needs_decision",
                            f"Task #{task_id} → needs_decision ({note})",
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
        # #897, the third door: a ``pending_report`` task with auto_review off
        # completes right here, without the delivery gate ever running. The
        # gate's own path arrives here too and answers "delivered" for free
        # (the merge it recorded is the first thing checked), so this is one
        # call for both — and the entrance that has no gate is not left out
        # because the two obvious ones were closed.
        after = await repo.get_task(db, task_id)
        if after is not None and dict(after)["status"] == "completed":
            await note_completion_without_delivery(
                db, task_id, via="report_done", actor=body.agent or "agent"
            )

    if undelivered:
        # #967 (review finding): the snapshot above predates the transition,
        # and the transition can now ANSWER it — the hub opens the PR itself
        # (and may merge it within the same done report), and a refusal
        # writes its own, louder needs_decision alert. "Хаб не знает ни PR"
        # over a freshly recorded PR, or "задача завершена" next to
        # needs_decision, is the lie that teaches readers to skip the
        # warning — the exact failure AC-2 of #498 names. The review-routing
        # path is untouched: there the snapshot is still true.
        fresh_row = await repo.get_task(db, task_id)
        fresh = dict(fresh_row) if fresh_row is not None else {}
        if fresh.get("pr_number") or fresh.get("status") == "needs_decision":
            undelivered = ""
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
    async with write_transaction(db):
        await repo.add_task_update(db, task_id, "human", "done", comment)
        await repo.update_task(db, task_id, **update_fields)
        await db.commit()
        await maybe_rollup_parent(db, task_id)
    # Outside the write lock: this asks git and possibly GitHub, and holding a
    # database lock across a network call is how a slow provider becomes an
    # outage. The completion is already committed — this is a note about it.
    #
    # #1037: force-complete writes the same pr_disposition as decide, so it
    # gets the same delivery. Left out, it would stay the second door with the
    # same silence behind it — and the one people reach for when the first
    # refuses.
    await deliver_on_disposition(
        db,
        task_id,
        ((body.pr_disposition if body else "") or ""),
        via="force_complete",
    )
    await note_completion_without_delivery(
        db,
        task_id,
        via="force_complete",
        disposition=((body.pr_disposition if body else "") or ""),
    )
    row = await repo.get_task(db, task_id)
    updates = await repo.get_task_updates(db, task_id)
    return row_to_task(row, updates=updates)  # type: ignore[arg-type]


DELIVER_DISPOSITION = "deliver"


async def deliver_on_disposition(
    db: aiosqlite.Connection, task_id: int, disposition: str, *, via: str
) -> tuple[bool, str]:
    """Act on ``pr_disposition=deliver``: merge, or refuse and say why (#1037).

    Until now the field was recorded and never acted on, so a human who
    accepted a task with its PR still open had no way to finish the delivery
    — the gate only looks at tasks inside the conveyor, and an accepted task
    is not one. On 28.08 that left #1036 completed with its code in an open
    PR, and the only way out was a manual merge: an exception to the rule
    that the gate, not a person, merges into the base branch.

    The one thing this must never become is a way AROUND the gate. A task
    reaches the human decision along several paths, and some of them ARE a
    failed condition: red CI, an exhausted review cycle, an arbiter
    escalation. So the delivery here is not a new door — it is the same door,
    opened from a different room:

    * the approved-review check is made HERE, because
      ``merge_before_completion`` does not make it. The gate only ever calls
      that function on an already-approved path, while this one is reached
      with no verdict at all. Skipping it would let a human decision stand in
      for a reviewer's, which is the whole hole;
    * everything else — the tip still matching what was approved (#612), the
      idempotence against a second merge (#363), the green CI, the merge
      itself and the pipeline_merges record the undelivered-work report reads
      — comes from calling the gate's own function rather than re-deriving
      its conditions. A second set would drift, and the weaker one would
      become the real one (#519, #546).

    Returns ``(delivered, reason)``. A refusal never undoes the acceptance:
    those are two decisions, and the human made only one of them here.
    """
    if (disposition or "").strip() != DELIVER_DISPOSITION:
        return False, ""
    row = await repo.get_task(db, task_id)
    if row is None:
        return False, "task not found"
    task = dict(row)
    if not task.get("pr_number"):
        return False, "no_pr"

    from hub.services.orchestration import (
        merge_before_completion,
        review_approved_for_current_submission,
    )

    if not review_approved_for_current_submission(task):
        await repo.add_task_update(
            db,
            task_id,
            "hub",
            "alert",
            "Доставка по решению человека НЕ выполнена: у текущей сдачи нет "
            "одобренного ревью. Решение человека принимает задачу, но не "
            "заменяет вердикт ревьюера — PR остался открытым (#1037).",
        )
        await db.commit()
        return False, "no_approved_review"

    ok, reason = await merge_before_completion(db, task)
    if ok:
        await repo.add_task_update(
            db,
            task_id,
            "hub",
            "status",
            f"Доставлено по решению человека ({via}, pr_disposition=deliver): "
            f"PR #{task['pr_number']} влит теми же условиями, что применяет "
            "гейт — одобренное ревью, неизменившийся с апрува код, зелёный CI "
            "(#1037).",
        )
    else:
        await repo.add_task_update(
            db,
            task_id,
            "hub",
            "alert",
            f"Доставка по решению человека НЕ выполнена: {reason}. Условия те "
            "же, что у гейта, и обойти их решением нельзя — PR остался "
            "открытым, задача остаётся принятой (#1037).",
        )
    await db.commit()
    return ok, reason


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
