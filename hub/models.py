from __future__ import annotations

import json
from enum import Enum
from typing import Any, Literal

import re

from pydantic import BaseModel, Field, field_validator, model_validator

_SQLITE_DT_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


def to_iso_utc(value: str | None) -> str | None:
    """Normalize a timestamp to ISO8601 UTC at the serialization boundary (#255).

    SQLite ``datetime('now')`` stores naive ``YYYY-MM-DD HH:MM:SS`` in UTC;
    other writers (claim, verdicts) already store ISO8601 with an offset.
    Storage stays unchanged — only API/MCP contracts are normalized.
    """
    if not value or not isinstance(value, str):
        return value
    if _SQLITE_DT_RE.match(value):
        return value.replace(" ", "T") + "+00:00"
    return value


class TaskStatus(str, Enum):
    draft = "draft"
    open = "open"
    claimed = "claimed"
    running = "running"
    needs_info = "needs_info"
    review = "review"
    fix_requested = "fix_requested"
    ci_check = "ci_check"
    needs_decision = "needs_decision"
    pending_report = "pending_report"
    completed = "completed"
    failed = "failed"
    rejected = "rejected"


class ReviewVerdict(str, Enum):
    """Explicit review verdict bound to a specific submission generation.

    An ``approved`` verdict only applies to the submission generation it was
    recorded against; resubmitting work bumps the generation and makes any
    earlier approval stale (Universal Review Gate, #305).
    """

    approved = "approved"
    changes_requested = "changes_requested"


class ReviewSeverity(str, Enum):
    """Finding severity, ordered so agents can prioritize fixes (#308)."""

    high = "high"
    medium = "medium"
    low = "low"


class FindingScope(str, Enum):
    """Whether a review finding belongs to the reviewed task (#435).

    ``in_scope`` findings must be fixed in the same task via the
    CHANGES_REQUESTED loop; ``out_of_scope`` findings are moved to separate
    tasks (referenced by ``linked_task_id``) and never block the verdict.
    """

    in_scope = "in_scope"
    out_of_scope = "out_of_scope"


class TaskType(str, Enum):
    epic = "epic"
    feature = "feature"
    task = "task"
    subtask = "subtask"


class TaskPriority(str, Enum):
    critical = "critical"
    high = "high"
    medium = "medium"
    low = "low"


class TaskSource(str, Enum):
    human = "human"
    agent = "agent"


class RuntimeChoice(str, Enum):
    auto = "auto"
    openrouter = "openrouter"
    vast = "vast"


# --- Structured task form enums (Epic #32) ---


class WorkType(str, Enum):
    """High-level work classification (Kanban work item type)."""

    feature = "feature"
    bug = "bug"
    refactor = "refactor"
    chore = "chore"
    docs = "docs"
    spike = "spike"
    incident = "incident"


class ClassOfService(str, Enum):
    """Kanban class of service for prioritization and SLA."""

    standard = "standard"
    expedite = "expedite"
    fixed_date = "fixed_date"
    intangible = "intangible"


class TaskSize(str, Enum):
    """T-shirt sizing for relative effort estimation."""

    XS = "XS"
    S = "S"
    M = "M"
    L = "L"
    XL = "XL"


class WipTag(str, Enum):
    """WIP bucket tag for capacity allocation per category."""

    feature_work = "feature_work"
    bugfix = "bugfix"
    tech_debt = "tech_debt"
    support = "support"


class ACVerifiableBy(str, Enum):
    """How an acceptance criterion can be verified."""

    test = "test"
    manual = "manual"
    log_check = "log_check"
    ui_check = "ui_check"


class RiskKind(str, Enum):
    """Catalog of risk categories surfaced during DoR analysis."""

    ambiguous_requirements = "ambiguous_requirements"
    large_scope = "large_scope"
    external_dependency = "external_dependency"
    data_migration = "data_migration"
    breaking_change = "breaking_change"
    security = "security"
    performance = "performance"
    unknown_unknowns = "unknown_unknowns"
    other = "other"


class RiskSeverity(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"


# Allowed parent task_type -> child task_type mapping
HIERARCHY_RULES: dict[TaskType, TaskType | None] = {
    TaskType.epic: None,
    TaskType.feature: TaskType.epic,
    TaskType.task: TaskType.feature,
    TaskType.subtask: TaskType.task,
}

# Statuses that count as "active" for progress calculation
ACTIVE_STATUSES = frozenset(
    {
        TaskStatus.open,
        TaskStatus.claimed,
        TaskStatus.running,
        TaskStatus.fix_requested,
        TaskStatus.ci_check,
        TaskStatus.review,
        TaskStatus.needs_info,
        TaskStatus.needs_decision,
        TaskStatus.pending_report,
    }
)

STALE_THRESHOLD_MINUTES = 30

MAX_ACCEPTANCE_CRITERIA = 50
MAX_RISKS = 50

FINAL_STATUSES = frozenset(
    {
        TaskStatus.completed,
        TaskStatus.failed,
        TaskStatus.rejected,
    }
)


# --- Request models ---


class TaskCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=500)
    # Bind an EPIC to a project at creation (#346); virtual field, epic-only.
    project: str | None = Field(default=None, max_length=60)
    description: str = Field("", max_length=10000)
    task_type: TaskType = TaskType.task
    parent_id: int | None = None
    priority: TaskPriority = TaskPriority.medium
    runtime: RuntimeChoice = RuntimeChoice.auto
    source: TaskSource = TaskSource.human
    agent: str = Field("", max_length=100)
    rationale: str = Field("", max_length=5000)
    human_owner: str = Field("", max_length=100)
    human_reviewer: str = Field("", max_length=100)
    run_immediately: bool = False
    auto_review: bool = True

    # Structured task form (Epic #32). All optional on create —
    # readiness gate evaluates them at approve time, not at creation.
    work_type: WorkType = WorkType.feature
    class_of_service: ClassOfService = ClassOfService.standard
    size: TaskSize | None = None
    wip_tag: WipTag | None = None
    due_date: str | None = Field(default=None, max_length=32)
    user_story: str = Field("", max_length=1000)
    problem_statement: str = Field("", max_length=2000)
    business_value: str = Field("", max_length=500)
    scope_in: list[str] = Field(default_factory=list, max_length=20)
    scope_out: list[str] = Field(default_factory=list, max_length=20)
    affected_areas: list[str] = Field(default_factory=list, max_length=20)
    technical_hints: str = Field("", max_length=3000)
    constraints: list[str] = Field(default_factory=list, max_length=10)
    assumptions: list[str] = Field(default_factory=list, max_length=10)
    validation_commands: list[str] = Field(default_factory=list, max_length=10)
    out_of_scope_for_review: list[str] = Field(default_factory=list, max_length=10)
    review_checklist: list[str] = Field(default_factory=list, max_length=10)
    client_request_id: str | None = Field(
        default=None,
        max_length=128,
        description="Optional idempotency key; duplicates return the original task",
    )


MAX_BULK_CHILD_TASKS = 20


class BulkChildTaskItem(BaseModel):
    title: str = Field(..., min_length=1, max_length=500)
    description: str = Field("", max_length=10000)
    priority: TaskPriority = TaskPriority.medium
    # Optional structured form set at creation so a child can be born closer to
    # DoR without a follow-up refine round-trip. Forward refs are resolved by
    # ``BulkChildTaskItem.model_rebuild()`` after AcceptanceCriterion/TaskRisk.
    acceptance_criteria: list["AcceptanceCriterion"] | None = None
    risks: list["TaskRisk"] | None = None


class BulkChildTasksCreate(BaseModel):
    """Atomic bulk create of child tasks under one parent."""

    items: list[BulkChildTaskItem] = Field(
        ...,
        min_length=1,
        max_length=MAX_BULK_CHILD_TASKS,
    )
    task_type: TaskType = TaskType.subtask
    source: TaskSource = TaskSource.agent
    agent: str = Field("", max_length=100)
    auto_review: bool = True


class TaskApprove(BaseModel):
    comment: str = ""
    run: bool = False
    runtime: RuntimeChoice | None = None
    # Bypass the DoR gate. Override is allowed but logged for audit.
    force: bool = False


class BatchApprove(BaseModel):
    """Approve a list of draft tasks in one human operation (#252).

    Guards default to safe: DoR must pass and high risks exclude a task.
    ``force`` deliberately does not exist here — overrides stay per-task.
    """

    task_ids: list[int] = Field(..., min_length=1, max_length=100)
    require_dor_passed: bool = True
    min_readiness: int | None = Field(default=None, ge=0, le=100)
    exclude_high_risks: bool = True
    comment: str = Field("", max_length=2000)


class BatchApproveSkipped(BaseModel):
    task_id: int
    reason: str


class BatchApproveResult(BaseModel):
    approved: list[int] = Field(default_factory=list)
    skipped: list[BatchApproveSkipped] = Field(default_factory=list)


class TaskReject(BaseModel):
    comment: str = ""


class TaskForceComplete(BaseModel):
    # Reason for the human override; recorded as the audit-trail message.
    comment: str = ""


class TaskStart(BaseModel):
    runtime: RuntimeChoice | None = None
    plan: str = Field("", max_length=10000)


class TaskPairStart(BaseModel):
    plan: str = Field("", max_length=10000)
    assigned_agent: str = Field("", max_length=100)
    branch_slug: str = Field("", max_length=80)


class TaskSubmitReview(BaseModel):
    """Submit the current work of a pair task for review (#305)."""

    agent: str = Field("", max_length=100)
    summary: str = Field("", max_length=10000)


class ReviewFinding(BaseModel):
    """One structured review finding (#308).

    ``id`` is stable within a single review submission so a developer agent
    can address findings by number in the CHANGES_REQUESTED loop.

    ``scope``/``linked_task_id`` (#435): ``in_scope`` findings are fixed in
    the same task via resubmit; ``out_of_scope`` findings are moved to
    separate tasks and reference the created task via ``linked_task_id``.
    """

    id: int = Field(..., ge=1)
    severity: ReviewSeverity
    message: str = Field(..., min_length=1, max_length=2000)
    file: str = Field("", max_length=500)
    line: int | None = Field(default=None, ge=1)
    recommendation: str = Field("", max_length=2000)
    scope: FindingScope = FindingScope.in_scope
    linked_task_id: int | None = Field(default=None, ge=1)


class TaskReviewVerdict(BaseModel):
    """Record an explicit review verdict for the current submission (#305).

    Extended in #308 with structured findings: they are persisted on the
    task row (not in the update text), so the payload stays machine-readable
    without stressing TaskUpdate content limits.

    ``create_tasks_for_out_of_scope`` (#436): when true, every
    ``out_of_scope`` finding without a ``linked_task_id`` gets a DRAFT
    follow-up task auto-created (DoR gate stays — a human decides whether to
    take it into work) and the created id is stamped into the stored
    finding. Idempotent: already-linked findings are skipped and resubmits
    reuse the existing draft via a back-reference marker in its description.
    """

    verdict: ReviewVerdict
    agent: str = Field("", max_length=100)
    comments: str = Field("", max_length=50000)
    findings: list[ReviewFinding] = Field(default_factory=list, max_length=50)
    create_tasks_for_out_of_scope: bool = False


class LatestReview(BaseModel):
    """Projection of the most recent review verdict for status/context (#308).

    ``is_current`` is False when work was resubmitted after the verdict was
    recorded — the verdict is history, not a judgement of the latest work.
    ``self_approved`` is True when the verdict was accepted only because of
    the ``OPENCLAW_REVIEW_SELF_APPROVE=allow`` solo opt-out: the implementer
    reviewed their own work, so the verdict is not independent (#434).
    """

    verdict: ReviewVerdict
    submission_generation: int = 0
    is_current: bool = False
    self_approved: bool = False
    findings: list[ReviewFinding] = Field(default_factory=list)


class SelfReviewWarning(BaseModel):
    """Fail-fast self-review notice on the review brief (#433).

    Emitted when the caller requesting the brief is the agent that
    implemented the task, BEFORE any review effort is spent. Advisory, not
    a hard-fail: the implementer may still read the brief for self-checking,
    but hub_submit_review will reject the verdict (unless solo mode).
    """

    reason: str
    message: str
    hint: str
    required_role: str | None = None


class ReviewBrief(BaseModel):
    """Everything a reviewer agent needs in one response (#308).

    Assembled from the task row, its acceptance criteria, and the latest
    submission update — no scraping of task prose required. ``diff_command``
    is advisory and only present when branch metadata exists; a GitHub PR is
    never required for a local review brief.
    """

    task_id: int
    title: str
    status: TaskStatus
    description: str = ""
    project: TaskProjectRef | None = None
    acceptance_criteria: list[AcceptanceCriterion] = Field(default_factory=list)
    scope_in: list[str] = Field(default_factory=list)
    scope_out: list[str] = Field(default_factory=list)
    out_of_scope_for_review: list[str] = Field(default_factory=list)
    review_checklist: list[str] = Field(default_factory=list)
    validation_commands: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    technical_hints: str = ""
    branch: str | None = None
    pr_number: int | None = None
    diff_command: str = ""
    review_cycle: int = 0
    submission_generation: int = 0
    latest_submission_summary: str = ""
    latest_review: LatestReview | None = None
    # #381: latest machine-review report; forward ref — MachineReviewView is
    # declared later in this module, rebuilt below.
    machine_review: "MachineReviewView | None" = None
    # #433: fail-fast notice when the caller implemented this task.
    self_review_warning: SelfReviewWarning | None = None
    # #438: advisory — non-empty when the branch carries commits of another
    # unmerged task branch (stacked branches). Never blocks the review.
    stacking_warning: str = ""


class TaskClaim(BaseModel):
    agent: str = Field(..., min_length=1, max_length=100)
    session_id: str = Field("", max_length=200)


class TaskRelease(BaseModel):
    agent: str = Field(..., min_length=1, max_length=100)
    session_id: str = Field("", max_length=200)


class TaskQuestion(BaseModel):
    agent: str = Field("", max_length=100)
    question: str = Field(..., min_length=1, max_length=10000)


class TaskAnswer(BaseModel):
    answer: str = Field(..., min_length=1, max_length=10000)
    resume: bool = True


class TaskDecide(BaseModel):
    action: str = Field(..., pattern="^(accept|rework)$")
    instructions: str = Field("", max_length=10000)
    decision_summary: str = Field("", max_length=5000)
    record_decision: bool = False


REPORT_KINDS = frozenset({"done", "status", "blocker"})


class TaskUpdateCreate(BaseModel):
    agent: str = Field("", max_length=100)
    kind: str = Field("status", max_length=50)
    content: str = Field(..., min_length=1, max_length=10000)


class TaskReorder(BaseModel):
    position: int = Field(..., ge=0)


class TaskArchive(BaseModel):
    """Archive hides tasks from default lists; optional subtree cascade."""

    cascade: bool = True


class TaskUnarchive(BaseModel):
    """Restore archived tasks; optional subtree cascade."""

    cascade: bool = True


# --- Structured task form: ACs, risks, refine, readiness (Epic #32) ---


class AcceptanceCriterion(BaseModel):
    """A single Given/When/Then scenario verifiable by a concrete method."""

    id: str = Field(..., pattern=r"^AC-\d+$", max_length=20)
    given: str = Field(..., min_length=1, max_length=500)
    when: str = Field(..., min_length=1, max_length=500)
    then: str = Field(..., min_length=1, max_length=500)
    verifiable_by: ACVerifiableBy
    test_ref: str | None = Field(default=None, max_length=500)


class TaskRisk(BaseModel):
    """A concrete risk with severity and mitigation plan."""

    kind: RiskKind
    severity: RiskSeverity
    description: str = Field(..., min_length=1, max_length=1000)
    mitigation: str = Field(..., min_length=1, max_length=1000)


class TaskRefine(BaseModel):
    """PATCH payload for structured fields. Every field is optional —
    omitted keys leave the existing value untouched."""

    # Bind an EPIC to a project by slug (#338); rejected for other types.
    project: str | None = Field(default=None, max_length=60)

    title: str | None = Field(default=None, min_length=1, max_length=500)
    work_type: WorkType | None = None
    class_of_service: ClassOfService | None = None
    size: TaskSize | None = None
    wip_tag: WipTag | None = None
    due_date: str | None = Field(default=None, max_length=32)
    user_story: str | None = Field(default=None, max_length=1000)
    problem_statement: str | None = Field(default=None, max_length=2000)
    business_value: str | None = Field(default=None, max_length=500)
    scope_in: list[str] | None = Field(default=None, max_length=20)
    scope_out: list[str] | None = Field(default=None, max_length=20)
    affected_areas: list[str] | None = Field(default=None, max_length=20)
    technical_hints: str | None = Field(default=None, max_length=3000)
    constraints: list[str] | None = Field(default=None, max_length=10)
    assumptions: list[str] | None = Field(default=None, max_length=10)
    validation_commands: list[str] | None = Field(default=None, max_length=10)
    out_of_scope_for_review: list[str] | None = Field(default=None, max_length=10)
    review_checklist: list[str] | None = Field(default=None, max_length=10)
    risks: list[TaskRisk] | None = None
    acceptance_criteria: list[AcceptanceCriterion] | None = None
    prepared_by: str | None = Field(default=None, max_length=100)
    prepared_at: str | None = Field(default=None, max_length=100)
    human_owner: str | None = Field(default=None, max_length=100)
    human_reviewer: str | None = Field(default=None, max_length=100)

    @field_validator("risks")
    @classmethod
    def _cap_risks(cls, v: list[TaskRisk] | None) -> list[TaskRisk] | None:
        if v is not None and len(v) > MAX_RISKS:
            raise ValueError(f"too many risks: {len(v)} exceeds limit of {MAX_RISKS}")
        return v

    @field_validator("acceptance_criteria")
    @classmethod
    def _cap_acs(
        cls, v: list[AcceptanceCriterion] | None
    ) -> list[AcceptanceCriterion] | None:
        if v is not None and len(v) > MAX_ACCEPTANCE_CRITERIA:
            raise ValueError(
                f"too many acceptance criteria: {len(v)} exceeds limit of {MAX_ACCEPTANCE_CRITERIA}"
            )
        return v

    @model_validator(mode="after")
    def _ac_ids_must_be_unique(self) -> "TaskRefine":
        """Catch duplicate AC ids at parse time so they surface as 422
        BEFORE we touch the DB and partially apply structured-field
        updates (review I12). The repository would catch this too, but
        only after a wasted roundtrip plus a SAVEPOINT rollback.
        """
        if self.acceptance_criteria is None:
            return self
        seen: set[str] = set()
        dups: list[str] = []
        for ac in self.acceptance_criteria:
            if ac.id in seen:
                dups.append(ac.id)
            seen.add(ac.id)
        if dups:
            raise ValueError(f"duplicate acceptance criterion ids: {sorted(set(dups))}")
        return self


# Bulk child items may carry AC/risks defined later in this module.
BulkChildTaskItem.model_rebuild()


MAX_BULK_REFINE = 50


class BulkRefineItem(TaskRefine):
    """One task's refine payload inside a bulk request (TaskRefine + task_id)."""

    task_id: int


class BulkRefine(BaseModel):
    """Atomic bulk refine: apply a TaskRefine PATCH to many tasks at once."""

    items: list[BulkRefineItem] = Field(..., min_length=1, max_length=MAX_BULK_REFINE)


class TaskRefineOutcome(BaseModel):
    """Per-task audit of what a (bulk) refine applied."""

    task_id: int
    fields_set: list[str] = Field(default_factory=list)
    acceptance_criteria_count: int | None = None
    risks_count: int | None = None
    readiness_score: int | None = None
    dor_passed: bool | None = None


class BulkRefineResult(BaseModel):
    results: list[TaskRefineOutcome] = Field(default_factory=list)


class DoRCheckItem(BaseModel):
    """Single Definition of Ready check result."""

    key: str
    passed: bool
    detail: str = ""


RecommendationSeverity = Literal["blocking", "high", "medium", "low"]


class Recommendation(BaseModel):
    """Actionable suggestion to improve task readiness."""

    field: str
    severity: RecommendationSeverity
    message: str
    expected_score_delta: int = 0
    estimated_minutes: int = 0


class ReadinessTreeNode(BaseModel):
    """One task's DoR status inside a subtree readiness report."""

    id: int
    title: str
    task_type: TaskType = TaskType.task
    status: TaskStatus
    score: int = Field(..., ge=0, le=100)
    dor_passed: bool
    missing_required: list[str] = Field(default_factory=list)
    blocking_reasons: list[str] = Field(default_factory=list)


class ReadinessTreeReport(BaseModel):
    """DoR rollup for a subtree (epic/feature and its descendants).

    Lets a caller see, in ONE request, which tasks under a root are not
    DoR-ready and why — instead of calling ``/readiness`` per task.
    """

    root_id: int
    total: int = 0
    ready: int = 0
    not_ready: int = 0
    nodes: list[ReadinessTreeNode] = Field(default_factory=list)


class ReadinessReport(BaseModel):
    """Result of a deterministic (non-LLM) readiness analysis for a task.

    ``missing_required`` is a sorted list of DoR check keys that BOTH
    failed AND are required for the task's work_type. Consumers must
    use this field instead of filtering ``dor_checks`` themselves —
    otherwise they'll mistakenly include optional checks that happen
    to be unsatisfied for the given work_type (e.g. ``has_user_story``
    on a bug). See review I1 for context.
    """

    score: int = Field(..., ge=0, le=100)
    dor_passed: bool
    dor_checks: list[DoRCheckItem] = Field(default_factory=list)
    missing_required: list[str] = Field(default_factory=list)
    risks: list[TaskRisk] = Field(default_factory=list)
    recommendations: list[Recommendation] = Field(default_factory=list)
    explain: list[dict[str, Any]] | None = None


# --- Response models ---


class TaskUpdateView(BaseModel):
    id: int
    task_id: int
    agent: str
    kind: str
    content: str
    created_at: str

    @field_validator("created_at", mode="before")
    @classmethod
    def _iso_ts(cls, v: str | None) -> str | None:
        return to_iso_utc(v)


class TaskBreadcrumb(BaseModel):
    id: int
    title: str
    task_type: TaskType


class TaskChildSummary(BaseModel):
    id: int
    title: str
    task_type: TaskType
    status: TaskStatus
    priority: TaskPriority = TaskPriority.medium


class TaskProgress(BaseModel):
    total: int = 0
    completed: int = 0
    failed: int = 0
    active: int = 0
    percent: int = 0


class TaskProjectRef(BaseModel):
    """Compact project reference on task contracts (#336)."""

    id: int
    slug: str


class TaskView(BaseModel):
    id: int
    title: str
    description: str
    status: TaskStatus
    task_type: TaskType = TaskType.task
    parent_id: int | None = None
    priority: TaskPriority = TaskPriority.medium
    position: int = 0
    runtime: RuntimeChoice
    source: TaskSource = TaskSource.human
    assigned_agent: str = ""
    rationale: str = ""
    human_owner: str = ""
    human_reviewer: str = ""
    job_id: str | None = None
    exit_code: int | None = None
    result_text: str | None = None
    log_tail: list[str] | None = None
    updates: list[TaskUpdateView] | None = None
    review_cycle: int = 0
    ci_fix_cycle: int = 0
    auto_review: bool = True
    review_job_id: str | None = None
    # Universal Review Gate (#305): headless review is marked by a present
    # review_job_id; client-driven review has status=review with no job.
    # A verdict only counts while review_verdict_generation matches
    # submission_generation.
    submission_generation: int = 0
    review_verdict: ReviewVerdict | None = None
    review_verdict_generation: int | None = None
    review_approved_current: bool = False
    latest_review: LatestReview | None = None
    branch: str | None = None
    pr_number: int | None = None
    # Pair-start workspace signal (#530): set only on pair-start so an agent
    # learns where its isolated worktree is. "" elsewhere.
    workspace_mode: str = ""
    worktree_path: str = ""
    claimed_by: str | None = None
    claim_session_id: str | None = None
    claimed_at: str | None = None
    project: TaskProjectRef | None = None
    breadcrumb: list[TaskBreadcrumb] | None = None
    children: list[TaskChildSummary] | None = None
    progress: TaskProgress | None = None
    archived: bool = False
    created_at: str
    updated_at: str

    # Structured task form (Epic #32). Optional so list-views can omit
    # heavy fields (ACs/risks) without breaking existing consumers.
    work_type: WorkType | None = None
    class_of_service: ClassOfService | None = None
    size: TaskSize | None = None
    wip_tag: WipTag | None = None
    due_date: str | None = None
    user_story: str = ""
    problem_statement: str = ""
    business_value: str = ""
    scope_in: list[str] = Field(default_factory=list)
    scope_out: list[str] = Field(default_factory=list)
    affected_areas: list[str] = Field(default_factory=list)
    technical_hints: str = ""
    constraints: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    validation_commands: list[str] = Field(default_factory=list)
    out_of_scope_for_review: list[str] = Field(default_factory=list)
    review_checklist: list[str] = Field(default_factory=list)
    risks: list[TaskRisk] = Field(default_factory=list)
    acceptance_criteria: list[AcceptanceCriterion] | None = None
    lifecycle_hint: str | None = None
    readiness_score: int | None = None
    dor_passed: bool | None = None
    ready_at: str | None = None
    prepared_by: str = ""
    prepared_at: str | None = None
    started_at: str | None = None
    completed_at: str | None = None

    @field_validator(
        "created_at",
        "updated_at",
        "claimed_at",
        "ready_at",
        "prepared_at",
        "started_at",
        "completed_at",
        mode="before",
    )
    @classmethod
    def _iso_ts(cls, v: str | None) -> str | None:
        return to_iso_utc(v)


class TaskTreeNode(BaseModel):
    id: int
    title: str
    task_type: TaskType
    status: TaskStatus
    priority: TaskPriority = TaskPriority.medium
    assigned_agent: str = ""
    progress: TaskProgress | None = None
    children: list[TaskTreeNode] = []


class ContextReadinessSummary(BaseModel):
    """Compact readiness view embedded in /context.

    Purpose: give the Developer agent just enough signal to decide whether
    to start — full breakdown is available via GET /readiness?explain=true.
    """

    score: int = Field(..., ge=0, le=100)
    dor_passed: bool
    missing_required: list[str] = Field(default_factory=list)
    blocking_recommendations: list[Recommendation] = Field(default_factory=list)


class ContextParentGoal(BaseModel):
    """Nearest non-task ancestor (epic/feature) that grounds the work."""

    id: int
    task_type: TaskType
    title: str
    problem_statement: str = ""
    business_value: str = ""


class TaskContextView(BaseModel):
    """Response for GET /api/tasks/{task_id}/context.

    Extended in #41 from a lightweight breadcrumb+siblings envelope into a
    full "developer contract": the current task with its structured fields,
    ACs, a compact readiness summary, and the parent goal. ``context_text``
    remains a human/LLM-friendly markdown digest of the same data.

    The legacy fields (task_id, breadcrumb, siblings, children, progress,
    context_text) are preserved for backward compatibility.
    """

    task_id: int
    breadcrumb: list[dict[str, Any]]
    siblings: list[dict[str, Any]]
    children: list[dict[str, Any]]
    progress: dict[str, Any] | None = None
    context_text: str

    # Added in #41 — full developer contract.
    task: TaskView | None = None
    readiness: ContextReadinessSummary | None = None
    parent_goal: ContextParentGoal | None = None


class ProjectCreate(BaseModel):
    """Create a project (#335). ``create`` is a human gate at the API layer."""

    slug: str = Field(..., min_length=1, max_length=60, pattern=r"^[a-z0-9][a-z0-9-]*$")
    name: str = Field(..., min_length=1, max_length=200)
    repo: str = Field("", max_length=200)
    workspace_path: str = Field("", max_length=500)
    default_branch: str = Field("develop", max_length=100)
    default_branch_policy: dict[str, Any] = Field(default_factory=dict)


class ProjectPatch(BaseModel):
    """PATCH semantics: omitted fields stay unchanged (#338)."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    repo: str | None = Field(default=None, max_length=200)
    workspace_path: str | None = Field(default=None, max_length=500)
    default_branch: str | None = Field(default=None, max_length=100)
    default_branch_policy: dict[str, Any] | None = None
    archived: bool | None = None
    status: str | None = Field(default=None, pattern="^(pending|active)$")


class MachineFinding(BaseModel):
    """One machine-review finding (#381). Mirrors ReviewFinding plus a
    free-slug category feeding the recurrence metrics (#384)."""

    title: str = Field(..., min_length=1, max_length=300)
    severity: ReviewSeverity
    category: str = Field("", max_length=60)
    file: str = Field("", max_length=500)
    line: int | None = Field(default=None, ge=1)
    detail: str = Field("", max_length=4000)


class MachineRejectedFinding(BaseModel):
    title: str = Field(..., min_length=1, max_length=300)
    category: str = Field("", max_length=60)
    reason: str = Field("", max_length=2000)


class MachineReviewSubmit(BaseModel):
    """Structured multi-agent review report (#381).

    Metrics fields (#384) are optional — a client that cannot count
    tokens still reports the review itself.
    """

    harness_skill: str = Field("", max_length=80)
    harness_version: int | None = Field(default=None, ge=1)
    agent_count: int | None = Field(default=None, ge=1)
    tokens_spent: int | None = Field(default=None, ge=0)
    duration_ms: int | None = Field(default=None, ge=0)
    orchestrator: str = Field("", max_length=100)
    model: str = Field("", max_length=100)
    raw_count: int = Field(0, ge=0)
    findings_confirmed: list[MachineFinding] = Field(
        default_factory=list, max_length=100
    )
    findings_rejected: list[MachineRejectedFinding] = Field(
        default_factory=list, max_length=200
    )
    agent: str = Field("", max_length=100)


class MachineReviewView(BaseModel):
    id: int
    task_id: int
    submission_generation: int
    is_current: bool = True
    harness_skill: str = ""
    harness_version: int | None = None
    agent_count: int | None = None
    tokens_spent: int | None = None
    duration_ms: int | None = None
    orchestrator: str = ""
    model: str = ""
    raw_count: int = 0
    findings_confirmed: list[MachineFinding] = Field(default_factory=list)
    findings_rejected: list[MachineRejectedFinding] = Field(default_factory=list)
    submitted_by: str = ""
    created_at: str = ""

    @field_validator("created_at", mode="before")
    @classmethod
    def _mr_iso_ts(cls, v: str | None) -> str | None:
        return to_iso_utc(v)

    @field_validator("findings_confirmed", "findings_rejected", mode="before")
    @classmethod
    def _mr_findings_json(cls, v: Any) -> Any:
        if isinstance(v, str):
            try:
                parsed = json.loads(v or "[]")
                return parsed if isinstance(parsed, list) else []
            except ValueError:
                return []
        return v


class SkillCreate(BaseModel):
    """New skill version (#380). Agents create drafts; humans activate."""

    name: str = Field(..., min_length=1, max_length=80, pattern=r"^[a-z0-9][a-z0-9-]*$")
    kind: str = Field("prompt", pattern="^(prompt|skill|checklist|workflow)$")
    content: str = Field(..., min_length=1, max_length=100_000)
    tags: list[str] = Field(default_factory=list)
    project_id: int | None = None


class SkillView(BaseModel):
    id: int
    name: str
    kind: str
    version: int
    content: str = ""
    tags: list[str] = Field(default_factory=list)
    project_id: int | None = None
    status: str
    created_by: str = ""
    created_at: str = ""

    @field_validator("created_at", mode="before")
    @classmethod
    def _skill_iso_ts(cls, v: str | None) -> str | None:
        return to_iso_utc(v)

    @field_validator("tags", mode="before")
    @classmethod
    def _skill_tags_json(cls, v: Any) -> Any:
        if isinstance(v, str):
            try:
                parsed = json.loads(v or "[]")
                return parsed if isinstance(parsed, list) else []
            except ValueError:
                return []
        return v


class ProjectView(BaseModel):
    id: int
    slug: str
    name: str
    status: str = "active"
    repo: str = ""
    workspace_path: str = ""
    default_branch: str = "develop"
    default_branch_policy: dict[str, Any] = Field(default_factory=dict)
    archived: bool = False
    provision_status: str = "none"
    provision_detail: str = ""
    created_at: str = ""
    updated_at: str = ""

    @field_validator("created_at", "updated_at", mode="before")
    @classmethod
    def _iso_ts(cls, v: str | None) -> str | None:
        return to_iso_utc(v)

    @field_validator("default_branch_policy", mode="before")
    @classmethod
    def _policy_json(cls, v: Any) -> Any:
        if isinstance(v, str):
            try:
                return json.loads(v) if v else {}
            except ValueError:
                return {}
        return v or {}


class ActivityItem(BaseModel):
    kind: str
    summary: str
    detail: dict[str, Any] | None = None
    timestamp: str

    @field_validator("timestamp", mode="before")
    @classmethod
    def _iso_ts(cls, v: str | None) -> str | None:
        return to_iso_utc(v)


class DashboardData(BaseModel):
    recent_commits: list[dict[str, Any]]
    open_prs: list[dict[str, Any]]
    active_tasks: list[TaskView]
    draft_tasks: list[TaskView] = []
    needs_info_tasks: list[TaskView] = []
    review_tasks: list[TaskView] = []
    needs_decision_tasks: list[TaskView] = []
    pending_report_tasks: list[TaskView] = []
    stale_tasks: list[TaskView] = []
    epics: list[TaskView] = []
    recent_decisions: list[dict[str, Any]]
    recent_activity: list[ActivityItem] = []
    vast_status: str | None = None


# ---------------------------------------------------------------------------
# Admin section models (Stage 4)
# ---------------------------------------------------------------------------


class PrincipalKind(str, Enum):
    human = "human"
    agent = "agent"
    service = "service"


class PrincipalStatus(str, Enum):
    active = "active"
    disabled = "disabled"
    locked = "locked"


def _check_password_complexity(v: str) -> str:
    if not re.search(r"[a-zA-Z]", v):
        raise ValueError("password must contain at least one letter")
    if not re.search(r"\d", v):
        raise ValueError("password must contain at least one digit")
    if not re.search(r"[^a-zA-Z0-9]", v):
        raise ValueError("password must contain at least one special character")
    return v


class PrincipalCreate(BaseModel):
    kind: PrincipalKind
    username: str = Field(
        ..., min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_\-\.]+$"
    )
    display_name: str = Field("", max_length=200)
    email: str = Field("", max_length=320)
    password: str | None = Field(default=None, min_length=8, max_length=200)
    role: str = Field("", max_length=50)
    notes: str = Field("", max_length=2000)

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: str | None) -> str | None:
        if v is not None:
            _check_password_complexity(v)
        return v


class PrincipalUpdate(BaseModel):
    display_name: str | None = Field(default=None, max_length=200)
    email: str | None = Field(default=None, max_length=320)
    notes: str | None = Field(default=None, max_length=2000)


class PrincipalView(BaseModel):
    id: int
    kind: PrincipalKind
    username: str
    display_name: str = ""
    email: str = ""
    status: PrincipalStatus = PrincipalStatus.active
    notes: str = ""
    roles: list[str] = Field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    last_seen_at: str | None = None
    created_by: int | None = None

    @field_validator("created_at", "updated_at", "last_seen_at", mode="before")
    @classmethod
    def _iso_ts(cls, v: str | None) -> str | None:
        return to_iso_utc(v)


class RoleView(BaseModel):
    id: int
    slug: str
    name: str
    description: str = ""
    system: bool = False
    permissions: list[str] = Field(default_factory=list)


class ApiKeyCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    expires_days: int | None = Field(default=None, ge=1, le=3650)


class ApiKeyView(BaseModel):
    id: int
    principal_id: int
    name: str
    key_prefix: str
    expires_at: str | None = None
    last_used_at: str | None = None
    revoked_at: str | None = None
    created_at: str = ""
    created_by: int | None = None

    @field_validator(
        "expires_at", "last_used_at", "revoked_at", "created_at", mode="before"
    )
    @classmethod
    def _iso_ts(cls, v: str | None) -> str | None:
        return to_iso_utc(v)


class ApiKeyCreated(ApiKeyView):
    """Returned only once at creation time — includes the plaintext key."""

    plaintext_key: str


class AuditEntry(BaseModel):
    id: int
    actor_principal_id: int | None = None
    actor_username: str | None = ""
    action: str
    target_type: str
    target_id: str = ""
    summary: str
    detail: str | None = None
    created_at: str = ""

    @field_validator("created_at", mode="before")
    @classmethod
    def _iso_ts(cls, v: str | None) -> str | None:
        return to_iso_utc(v)


class AdminSummary(BaseModel):
    active_users: int = 0
    disabled_users: int = 0
    active_agents: int = 0
    active_api_keys: int = 0
    expiring_keys_7d: int = 0
    expiring_keys_30d: int = 0
    locked_users: int = 0
    recent_audit: list[AuditEntry] = Field(default_factory=list)
    env_tokens_active: bool = False
    admin_bootstrap_required: bool = False


class RolesUpdatePayload(BaseModel):
    roles: list[str] = Field(..., min_length=1)


class PasswordSetPayload(BaseModel):
    password: str = Field(..., min_length=8, max_length=200)

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        return _check_password_complexity(v)


class AdminBootstrap(BaseModel):
    username: str = Field(
        ..., min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_\-\.]+$"
    )
    password: str = Field(..., min_length=8, max_length=200)
    display_name: str = Field("", max_length=200)
    email: str = Field("", max_length=320)

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        return _check_password_complexity(v)


class WhoamiView(BaseModel):
    username: str
    role: str
    permissions_summary: list[str] = Field(default_factory=list)
    permissions_count: int = 0
    auth_source: str
    api_key_id: int | None = None
    principal_id: int | None = None
    app_version: str


class HealthView(BaseModel):
    status: str = "ok"
    app_version: str
    bind_host: str
    bind_port: int
    auth_required: bool
    auth_disabled: bool
    env_tokens_configured: bool
    vast_enabled: bool


class IdentityDiagnosticsView(BaseModel):
    """One-call identity + environment truth for agents (#452).

    ``connected_via`` is the base URL the client actually reached (from the
    request Host), independent of ``base_url`` echoed from OPENCLAW_HUB_URL;
    ``config_mismatch`` flags when the two disagree so an operator never acts
    on the wrong instance. ``workspace_path``/``workspace_branch`` expose the
    server-side git workspace state in the same response.
    """

    username: str
    role: str
    principal_id: int | None = None
    auth_source: str
    permissions_count: int = 0
    instance: str
    base_url: str
    server_id: str = ""
    connected_via: str = ""
    config_mismatch: bool = False
    workspace_path: str = ""
    workspace_branch: str = ""
    workspace_mode: str = "legacy"
    app_version: str


# --- Deprecated aliases for backward compatibility ---

ProposalStatus = TaskStatus
ProposalCreate = TaskCreate
ProposalView = TaskView

# Forward-ref rebuild: ReviewBrief.machine_review points at MachineReviewView,
# which is declared after ReviewBrief (#381).
ReviewBrief.model_rebuild()
