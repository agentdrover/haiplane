from __future__ import annotations

import json
from enum import Enum
from typing import Any, Literal

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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


class RedesignDecision(str, Enum):
    """Whether the task adapts the existing process or reshapes it (#331).

    Recorded so a spec cannot quietly automate yesterday's process on new
    technology: choosing ``adapt`` is legitimate, choosing it by default
    without noticing is not.
    """

    adapt = "adapt"
    redesign = "redesign"


class AgentFit(str, Enum):
    """How much agency the work wants (#331).

    deterministic — scripted, no model in the loop.
    assistant — a human drives, the model helps.
    sdd_native — spec-driven: the spec is the contract, the agent implements.
    agentic — the agent decides the steps, not just the code.
    """

    deterministic = "deterministic"
    assistant = "assistant"
    sdd_native = "sdd_native"
    agentic = "agentic"


class RiskClass(str, Enum):
    """Blast-radius class of a task, R0 (harmless) to R5 (irreversible) (#581).

    The class is DERIVED from observable facts (#582) — paths touched,
    migrations, contracts, irreversible operations — never declared by the
    task author, which is why no create/refine model carries this field.
    A task without a class is "not computed": that state lives as NULL/None
    and must never be read as R0 (see add_risk_class_column in hub/db.py).
    """

    r0 = "R0"
    r1 = "R1"
    r2 = "R2"
    r3 = "R3"
    r4 = "R4"
    r5 = "R5"


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

# Statuses whose next move belongs to a HUMAN (#567).
#
# ``draft`` is in here and that is the whole point: draft→open is a human-only
# gate (hub_approve_task), and on production it holds 39 tasks while the three
# statuses the task originally named hold 2 between them. It is also the only
# status that belongs to neither ACTIVE_STATUSES nor FINAL_STATUSES — the
# forgotten one, which is exactly how it got left out of the first formula.
#
# ``review`` is included with a caveat the queries must honour: a review with
# ``review_job_id`` set is a headless review owned by the poller, not a person
# (the same rule ``list_stale_by_status(require_null_review_job=...)`` already
# follows). Membership here is by status; the exclusion lives in the count.
AWAITING_HUMAN_STATUSES = frozenset(
    {
        TaskStatus.draft,
        TaskStatus.needs_info,
        TaskStatus.needs_decision,
        TaskStatus.review,
    }
)

# Approved, and nobody has started it: a QUEUE, not work (#619).
#
# `open` was inside "in flight" until a live page showed it-grade-dashboard with
# "в работе: 46" beside "активность: 20.07" — a month of silence. Those 46 were
# approved and untouched, which made the most abandoned project look like the
# busiest one in the hub. A queue is worth counting; it is just not the same
# question as "who is moving something right now".
#
# Its own named set rather than a hole: the test that asserts the sets partition
# TaskStatus exactly would otherwise pass with `open` belonging to nothing, and a
# status quietly outside every set is precisely the defect that test exists to
# catch (draft was that status until #567).
QUEUED_STATUSES = frozenset({TaskStatus.open})

# Work in flight — DERIVED, never retyped. Spelling this set out by hand would
# be the third copy of a status list in this codebase; the first two had to be
# unified in #571 (terminal statuses) and #570 (epic liveness), and one of them
# shipped wrong. A status added to TaskStatus later lands in none of the four
# sets and the test in tests/test_web.py fails, which is the point.
IN_FLIGHT_STATUSES = ACTIVE_STATUSES - AWAITING_HUMAN_STATUSES - QUEUED_STATUSES


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
    # --- Discovery block (#331): why this, and in what form ---
    outcome_metric: str = Field("", max_length=300)
    outcome_indicator: str = Field("", max_length=300)
    outcome_deadline: str = Field("", max_length=64)
    outcome_revisit_condition: str = Field("", max_length=500)
    redesign_decision: RedesignDecision | None = None
    redesign_rationale: str = Field("", max_length=1000)
    agent_fit: AgentFit | None = None
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
    # The branch the client actually worked in (#533). Reported, not observed:
    # the hub has no copy of the project to inspect, so this catches a client
    # that forgot to switch, never one that misreports. Optional so existing
    # callers keep working; omitting it skips the comparison.
    branch: str = Field("", max_length=200)


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


class ACTestResultView(BaseModel):
    """Recorded pass/fail of a verifiable_by=test AC's bound test (#507).

    ``is_current`` is True only while the result's generation matches the
    task's submission_generation — a resubmission makes it stale.
    """

    ac_id: str
    status: str  # pass | fail | not_found
    is_current: bool = False


class CallSiteEntry(BaseModel):
    """One changed symbol and where else it is called (#601)."""

    symbol: str
    defined_in: str
    state: str
    statement: str
    # Only the untouched sites are listed — the touched ones are noise. But
    # without the total, "one site left alone" cannot be told apart from "one
    # of one", and the completeness of the walk is unjudgeable (#601 review).
    total_sites: int = 0
    untouched: list[str] = Field(default_factory=list)


class CallSiteSection(BaseModel):
    """Call sites of everything the diff changes (#601).

    ``status`` is ``analysed`` or ``unknown``; ``unknown`` carries the reason,
    because "could not look" and "nothing to report" are different answers and
    a reviewer must be able to tell them apart.
    """

    status: str = "unknown"
    reason: str = ""
    summary: str = ""
    note: str = ""
    entries: list[CallSiteEntry] = Field(default_factory=list)
    unparsed: list[str] = Field(default_factory=list)


class ACLocatorResolution(BaseModel):
    """Whether a verifiable_by=test AC's locator resolves to a real test (#506).

    ``status`` is ``resolvable`` (test found by collection), ``missing`` (valid
    locator but no such test, or no valid locator at all), or ``unknown`` (test
    collection could not run in this environment — never a false ``missing``).
    """

    ac_id: str
    locator: str | None = None
    status: str
    reason: str = ""


class CIRunReportState(BaseModel):
    """Does run evidence exist for the commit under review (#546).

    ``state`` is ``current`` (CI reported this exact commit) or ``unknown``
    (nobody reported it, it was reported for a different commit, or no commit is
    pinned). There is no ``fail`` state on purpose: this field answers whether
    evidence exists, and missing evidence is not a failed run. ``reason`` is
    always filled for ``unknown`` so the reader gets a cause, not a blank.
    """

    state: str = "unknown"
    reason: str = "отчёт CI о прогоне не запрашивался"
    head_sha: str = ""


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
    # #506: per verifiable_by=test AC, whether its locator resolves to a real test.
    locator_resolution: list[ACLocatorResolution] = Field(default_factory=list)
    call_sites: CallSiteSection = Field(default_factory=CallSiteSection)
    # #507: recorded pass/fail of each test-AC's bound test for this generation.
    ac_test_results: list[ACTestResultView] = Field(default_factory=list)
    # #546: whether run evidence exists for the COMMIT under review. Two states
    # only — current, or unknown with a reason. Absence of a report is not a
    # failing run, and must never be shown as one.
    ci_run_report: CIRunReportState = Field(default_factory=lambda: CIRunReportState())
    # #615: the reviewer judges a statement too — and it may be older than the
    # work that invalidated it.
    statement_freshness: dict[str, Any] | None = None
    scope_in: list[str] = Field(default_factory=list)
    scope_out: list[str] = Field(default_factory=list)
    out_of_scope_for_review: list[str] = Field(default_factory=list)
    review_checklist: list[str] = Field(default_factory=list)
    validation_commands: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    technical_hints: str = ""
    # Discovery block (#331): a reviewer judging "does this do what it should"
    # needs the outcome hypothesis, not only the build instructions.
    outcome_metric: str = ""
    outcome_indicator: str = ""
    outcome_deadline: str = ""
    outcome_revisit_condition: str = ""
    redesign_decision: RedesignDecision | None = None
    redesign_rationale: str = ""
    agent_fit: AgentFit | None = None
    branch: str | None = None
    pr_number: int | None = None
    diff_command: str = ""
    review_cycle: int = 0
    submission_generation: int = 0
    # #572: what code the submission pinned, where the branch stands now, and
    # whether they agree. sha_check is "match" | "diverged" | "unknown" —
    # three states, never collapsed: "could not look" must not read as
    # "nothing moved". The reviewer sees this before spending an hour.
    submission_sha: str = ""
    current_branch_tip: str = ""
    sha_check: str = "unknown"
    sha_check_reason: str = ""
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


class ExpectationSource(str, Enum):
    """Where the expected behaviour in an acceptance criterion came from (#595).

    A criterion says what must be observably true; this says who decided it.
    When the answer is the implementation, the test can only confirm the
    status quo — including a defect — which is how an assertion ends up
    unable to fail. ``implementation`` is allowed rather than forbidden:
    sometimes it is the only source there is, and saying so plainly beats
    leaving it blank.
    """

    requirement = "requirement"
    contract = "contract"
    incident = "incident"
    bug_report = "bug_report"
    implementation = "implementation"


class AcceptanceCriterion(BaseModel):
    """A single Given/When/Then scenario verifiable by a concrete method."""

    id: str = Field(..., pattern=r"^AC-\d+$", max_length=20)
    given: str = Field(..., min_length=1, max_length=500)
    when: str = Field(..., min_length=1, max_length=500)
    then: str = Field(..., min_length=1, max_length=500)
    verifiable_by: ACVerifiableBy
    test_ref: str | None = Field(default=None, max_length=500)
    # None means "not stated", which is NOT the same as "taken from the
    # implementation" — the column is nullable so the two cannot be confused.
    expectation_source: ExpectationSource | None = None


class TaskRisk(BaseModel):
    """A concrete risk, with a mitigation when one is known (#610).

    ``mitigation`` is deliberately optional. Requiring it left an author who
    could see a risk but not yet its remedy with two bad moves: invent filler
    text to satisfy validation, or say nothing. Worse, a risk that failed
    validation was dropped by parse_risks_from_row — so the least-handled
    risks were the ones that vanished from the score entirely. An empty
    mitigation is now a statement in its own right ("seen, not yet solved")
    and costs more in the readiness score than a mitigated one.
    """

    kind: RiskKind
    severity: RiskSeverity
    description: str = Field(..., min_length=1, max_length=1000)
    mitigation: str = Field("", max_length=1000)


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
    # --- Discovery block (#331) ---
    outcome_metric: str | None = Field(default=None, max_length=300)
    outcome_indicator: str | None = Field(default=None, max_length=300)
    outcome_deadline: str | None = Field(default=None, max_length=64)
    outcome_revisit_condition: str | None = Field(default=None, max_length=500)
    redesign_decision: RedesignDecision | None = None
    redesign_rationale: str | None = Field(default=None, max_length=1000)
    agent_fit: AgentFit | None = None
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
    # When the statement was last SHAPED, not who shaped it. Refine stamps this
    # server-side whenever it writes a statement field and the caller left it
    # unset (#616) — and it deliberately does NOT touch prepared_by, since a
    # refine caller is not necessarily an analyst. A date without an author is
    # therefore expected. Format matters: the same space-separated form as
    # created_at, because the freshness check (#615) compares these as text.
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
    # Authorship (#559). ``agent`` is a display name the client picks; these
    # two are the fact. author_kind says WHY principal_id is absent when it is:
    # "legacy" predates the field, "hub" is the hub writing its own updates,
    # "anonymous" is a request with no authenticated identity. One NULL could
    # not have told those apart.
    principal_id: int | None = None
    author_kind: str = "legacy"

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
    # #572: the branch tip the hub observed at submission. The generation
    # binds the verdict to a NUMBER; this binds it to the CODE. Empty means
    # the tip could not be pinned (no branch, no workspace, network) — that
    # degrades to the pre-#572 behaviour, never to a refusal.
    submission_sha: str = ""
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
    # --- Discovery block (#331) ---
    outcome_metric: str = ""
    outcome_indicator: str = ""
    outcome_deadline: str = ""
    outcome_revisit_condition: str = ""
    redesign_decision: RedesignDecision | None = None
    redesign_rationale: str = ""
    agent_fit: AgentFit | None = None
    # Shadow-mode risk class (#581): read-only surface, None = "not
    # computed" and is NOT the same thing as RiskClass.r0.
    risk_class: RiskClass | None = None
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
    # #615: what was delivered in these areas since the statement was written.
    # Computed, never stored: the answer changes as work lands, and a stored copy
    # would be one more thing to go stale — which is the very defect this
    # addresses. Absent means "not computed on this path", not "fresh".
    statement_freshness: dict[str, Any] | None = None
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
    """PATCH semantics: omitted fields stay unchanged (#338).

    ``None`` here means "not sent", never "set this to null" — every column
    on ``projects`` is NOT NULL, so an explicit null has no valid meaning and
    is rejected below (#366).
    """

    name: str | None = Field(default=None, min_length=1, max_length=200)
    repo: str | None = Field(default=None, max_length=200)
    workspace_path: str | None = Field(default=None, max_length=500)
    default_branch: str | None = Field(default=None, max_length=100)
    default_branch_policy: dict[str, Any] | None = None
    archived: bool | None = None
    status: str | None = Field(default=None, pattern="^(pending|active)$")

    @model_validator(mode="before")
    @classmethod
    def _reject_explicit_null(cls, data: Any) -> Any:
        """Refuse a key that was sent with an explicit null.

        The optional types above exist to express "omitted", which is what
        PATCH needs. They also, accidentally, accepted a literal null, which
        then travelled through model_dump(exclude_unset=True) into a NOT NULL
        column and surfaced as a raw 500 from IntegrityError. Sending null is
        a malformed request, so it belongs in 422 — and the distinction has
        to be made here, on the raw input, because by the time the model is
        built "sent as null" and "not sent" both read as None.
        """
        if not isinstance(data, dict):
            return data
        nulls = sorted(
            k for k, v in data.items() if v is None and k in cls.model_fields
        )
        if nulls:
            raise ValueError(
                "null is not a valid value for "
                + ", ".join(nulls)
                + "; omit the field to leave it unchanged"
            )
        return data


class MachineFinding(BaseModel):
    """One machine-review finding (#381). Mirrors ReviewFinding plus a
    free-slug category feeding the recurrence metrics (#384)."""

    # A key we do not know is a key we would drop, and a dropped key is
    # invisible (#553). Harness output routinely carries extra fields —
    # dimensions, duplicates, failure_scenario — and the submitter is expected
    # to map them onto this shape rather than hope they land somewhere.
    model_config = ConfigDict(extra="forbid")

    title: str = Field(..., min_length=1, max_length=300)
    severity: ReviewSeverity
    category: str = Field("", max_length=60)
    file: str = Field("", max_length=500)
    line: int | None = Field(default=None, ge=1)
    detail: str = Field("", max_length=4000)


class MachineRejectedFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(..., min_length=1, max_length=300)
    category: str = Field("", max_length=60)
    reason: str = Field("", max_length=2000)


class MachineUnresolvedFinding(BaseModel):
    """A finding no verifier managed to judge (#549).

    Deliberately NOT a rejected finding: "nobody voted" and "someone refuted
    it" are opposite outcomes, and collapsing them is how a run with dead
    agents reads as clean. ``why`` records what stopped the verification.
    """

    model_config = ConfigDict(extra="forbid")

    title: str = Field(..., min_length=1, max_length=300)
    why: str = Field("", max_length=2000)

    @model_validator(mode="before")
    @classmethod
    def _name_the_expected_field(cls, data: Any) -> Any:
        """Point at ``why`` when the neighbouring field's name was used.

        ``findings_rejected`` items carry ``reason`` and are documented on the
        line directly above this one, so the two get swapped. Plain
        extra="forbid" would say only that ``reason`` is not permitted, which
        leaves the caller to guess — trading a silent loss for a loud puzzle.
        Reproduced on machine_review#34: both unresolved findings were stored
        with an empty explanation, which is the whole content of an unresolved
        finding.
        """
        if isinstance(data, dict) and "reason" in data and "why" not in data:
            raise ValueError(
                "unresolved findings explain themselves in 'why', not "
                "'reason' — 'reason' belongs to findings_rejected"
            )
        return data


class CIRunReportSubmit(BaseModel):
    """What a CI run reports back to the hub (#546).

    ``head_sha`` is required and load-bearing: the hub decides whether the report
    counts by comparing it with the commit it pinned at submission (#572), so a
    report cannot claim to cover code it did not run. ``submission_generation``
    is optional and advisory — when the reporter states one and it no longer
    matches, the report is refused as stale instead of silently applied to newer
    work. ``reason`` carries why a value is unknown, which is the difference
    between "did not run" and "ran and failed".
    """

    head_sha: str = Field(..., min_length=7, max_length=64)
    submission_generation: int | None = Field(default=None, ge=0)
    ac_results: dict[str, str] = Field(default_factory=dict)
    validation_status: str = Field("", max_length=20)
    validation_log: str = Field("", max_length=4000)
    reason: str = Field("", max_length=500)
    reported_by: str = Field("", max_length=100)


class CIRunReportResult(BaseModel):
    """Outcome of accepting a CI run report (#546)."""

    applied: bool
    reason: str
    head_sha: str
    submission_generation: int | None = None
    ac_recorded: list[dict[str, Any]] = Field(default_factory=list)
    ac_ignored: list[str] = Field(default_factory=list)
    validation_status: str = ""


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
    # Required with NO default (#549). A default of false would be filled in
    # silently by every client that forgot the field, which reproduces exactly
    # the substitution this field exists to prevent: a run that lost agents
    # reading as a clean one. Forgetting must fail loudly at the schema.
    incomplete: bool
    unresolved: list[MachineUnresolvedFinding] = Field(
        default_factory=list, max_length=200
    )
    lost_dimensions: list[str] = Field(default_factory=list, max_length=50)
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
    # None means "never stated", not "complete" — reports written before this
    # field existed made no such claim, and back-filling false would put words
    # in their mouth (#549).
    incomplete: bool | None = None
    unresolved: list[MachineUnresolvedFinding] = Field(default_factory=list)
    lost_dimensions: list[str] = Field(default_factory=list)
    submitted_by: str = ""
    created_at: str = ""

    @field_validator("created_at", mode="before")
    @classmethod
    def _mr_iso_ts(cls, v: str | None) -> str | None:
        return to_iso_utc(v)

    @field_validator(
        "findings_confirmed",
        "findings_rejected",
        "unresolved",
        "lost_dimensions",
        mode="before",
    )
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
