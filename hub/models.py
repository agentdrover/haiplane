from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class TaskStatus(str, Enum):
    draft = "draft"
    open = "open"
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


class TaskApprove(BaseModel):
    comment: str = ""
    run: bool = False
    runtime: RuntimeChoice | None = None
    # Bypass the DoR gate. Override is allowed but logged for audit.
    force: bool = False


class TaskReject(BaseModel):
    comment: str = ""


class TaskForceComplete(BaseModel):
    # Reason for the human override; recorded as the audit-trail message.
    comment: str = ""


class TaskStart(BaseModel):
    runtime: RuntimeChoice | None = None
    plan: str = Field("", max_length=10000)


class TaskQuestion(BaseModel):
    agent: str = Field("", max_length=100)
    question: str = Field(..., min_length=1, max_length=10000)


class TaskAnswer(BaseModel):
    answer: str = Field(..., min_length=1, max_length=10000)
    resume: bool = True


class TaskDecide(BaseModel):
    action: str = Field(..., pattern="^(accept|rework)$")
    instructions: str = Field("", max_length=10000)


REPORT_KINDS = frozenset({"done", "status", "blocker"})


class TaskUpdateCreate(BaseModel):
    agent: str = Field("", max_length=100)
    kind: str = Field("status", max_length=50)
    content: str = Field(..., min_length=1, max_length=10000)


class TaskReorder(BaseModel):
    position: int = Field(..., ge=0)


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
    risks: list[TaskRisk] | None = None
    acceptance_criteria: list[AcceptanceCriterion] | None = None
    human_owner: str | None = Field(default=None, max_length=100)
    human_reviewer: str | None = Field(default=None, max_length=100)

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
    branch: str | None = None
    pr_number: int | None = None
    breadcrumb: list[TaskBreadcrumb] | None = None
    children: list[TaskChildSummary] | None = None
    progress: TaskProgress | None = None
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
    risks: list[TaskRisk] = Field(default_factory=list)
    acceptance_criteria: list[AcceptanceCriterion] | None = None
    readiness_score: int | None = None
    dor_passed: bool | None = None
    ready_at: str | None = None
    started_at: str | None = None
    completed_at: str | None = None


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


class ActivityItem(BaseModel):
    kind: str
    summary: str
    detail: dict[str, Any] | None = None
    timestamp: str


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


# --- Deprecated aliases for backward compatibility ---

ProposalStatus = TaskStatus
ProposalCreate = TaskCreate
ProposalView = TaskView
