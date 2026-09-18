"""Readiness calculator.

Deterministic, non-LLM scoring of how ready a task is to be picked up by
a Developer agent. The score is a flat 0..100 number computed from:

- the DoR evaluation (#36) — penalty per failed required/optional check;
- the explicit risk list on the task — penalty per risk by severity.

Defaults were chosen so that an entirely empty feature task lands near
zero and a fully-described feature with no risks is exactly 100. The
constants live in ``ReadinessConfig`` so the gate can be tuned later
without touching the algorithm.

Recommendations are intentionally produced by a separate engine (#38).
This module exposes only the score, the DoR result echoed back, and the
risk list — Recommendations consumes that output to suggest actions.
"""

from __future__ import annotations

import ast
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from hub import config
from hub import repository as repo
from hub.db import deserialize_risks
from hub.models import (
    ReadinessReport,
    RiskSeverity,
    TaskRisk,
)
from hub.services.dor import DOR_ADVISORY_KEYS, DoREvaluation, evaluate_dor

log = logging.getLogger("hub.services.readiness")


@dataclass(frozen=True)
class ReadinessConfig:
    """Tunable scoring parameters.

    Defaults are calibrated so that:

    - a missing required DoR check is the second-most-painful event
      (10 pts), behind only a high-severity risk;
    - a high risk outweighs a missing required check (15 > 10) — the
      review explicitly flagged the prior 8<10 ordering as inverted
      severity, so risks now dominate;
    - missing optional checks are noticeable (5 pts) but not blocking;
    - the score band [0..base] always renders meaningfully — note that
      ``ReadinessReport.score`` is hard-clamped to [0, 100] in the
      Pydantic model, so raising ``base`` above 100 has no UI effect
      and is intentionally not encouraged.

    See `n4l decision: readiness scoring weights v1` for the rationale
    and the explicit deferral of weighted/non-linear scoring.
    """

    base: int = 100
    penalty_required: int = 10
    penalty_optional: int = 5
    risk_penalties: dict[RiskSeverity, int] = field(
        default_factory=lambda: {
            RiskSeverity.low: 3,
            RiskSeverity.medium: 8,
            RiskSeverity.high: 15,
        }
    )
    # #610: a risk that carries a mitigation costs less than the same risk
    # left open — but never nothing, because residual risk is still risk.
    # Written as an explicit table rather than a fraction of the one above:
    # a rounding rule ("half, rounded down") is unreadable six weeks later,
    # while two columns can be compared at a glance.
    mitigated_risk_penalties: dict[RiskSeverity, int] = field(
        default_factory=lambda: {
            RiskSeverity.low: 1,
            RiskSeverity.medium: 3,
            RiskSeverity.high: 6,
        }
    )

    def penalty_for_risk(self, severity: RiskSeverity, *, mitigated: bool) -> int:
        table = self.mitigated_risk_penalties if mitigated else self.risk_penalties
        return table.get(severity, 0)


DEFAULT_CONFIG = ReadinessConfig()


@dataclass(frozen=True)
class ScoreComponent:
    """One line of the score breakdown — used in the ``explain`` payload."""

    field: str
    delta: int
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"field": self.field, "delta": self.delta, "reason": self.reason}


def calculate_score_from_data(
    *,
    dor: DoREvaluation,
    risks: list[TaskRisk],
    config: ReadinessConfig = DEFAULT_CONFIG,
) -> tuple[int, list[ScoreComponent]]:
    """Pure scoring: DoR + risks → (score, components).

    - Each failed REQUIRED check costs ``penalty_required``.
    - Each failed OPTIONAL check (in DOR_CHECK_KEYS but not required for
      this work_type) costs ``penalty_optional`` — small nudge, not a block.
    - Each risk costs by severity.
    - Final score is clamped to [0, 100].

    Returns components in the order they were applied so that ``explain``
    mirrors the calculation top-down.
    """
    score = config.base
    components: list[ScoreComponent] = []

    for check in dor.checks:
        if check.passed:
            continue
        if check.key in DOR_ADVISORY_KEYS:
            # Advisory checks are visible but free (#331): they surface in the
            # DoR table and earn a recommendation, but never move the score.
            # Charging even penalty_optional here would drop every task in an
            # existing backlog on the day the check ships, since no task can
            # have a field that did not exist yesterday.
            continue
        is_required = check.key in dor.required
        penalty = config.penalty_required if is_required else config.penalty_optional
        score -= penalty
        components.append(
            ScoreComponent(
                field=check.key,
                delta=-penalty,
                reason=(
                    f"DoR required check '{check.key}' failed"
                    if is_required
                    else f"DoR optional check '{check.key}' failed"
                ),
            )
        )

    for idx, risk in enumerate(risks):
        # A declared risk with a plan is not the same liability as one left
        # open, and pricing them alike made disclosure itself expensive: on
        # #546 a fifth risk — named AND mitigated — cost 15 points, so the
        # cheapest way to raise the number was to stay quiet. That is the
        # opposite of what the field is for (#610).
        mitigated = bool((risk.mitigation or "").strip())
        penalty = config.penalty_for_risk(risk.severity, mitigated=mitigated)
        if penalty <= 0:
            continue
        score -= penalty
        components.append(
            ScoreComponent(
                field="risks",
                delta=-penalty,
                reason=(
                    f"risk #{idx + 1} {risk.kind.value} "
                    f"(severity={risk.severity.value}, "
                    f"{'mitigated' if mitigated else 'unmitigated'})"
                ),
            )
        )

    # Hard upper bound at 100 because ReadinessReport.score has le=100;
    # raising config.base above 100 would otherwise blow up Pydantic
    # validation downstream. Lower bound at 0 so heavy risk penalties
    # cannot produce a negative score.
    upper = min(100, config.base)
    score = max(0, min(upper, score))
    return score, components


def parse_risks_from_row(raw: str | None) -> list[TaskRisk]:
    """Validate JSON-stored risks via Pydantic, drop malformed entries.

    Public because the recommendations engine reuses it to keep both
    score and recommendations talking about the same risk set.
    """
    out: list[TaskRisk] = []
    for item in deserialize_risks(raw):
        try:
            out.append(TaskRisk(**item))
        except (ValidationError, TypeError) as exc:
            log.warning("dropping malformed risk %r: %s", item, exc)
    return out


async def calculate_readiness(
    db,
    task_id: int,
    *,
    explain: bool = False,
    config: ReadinessConfig = DEFAULT_CONFIG,
) -> ReadinessReport:
    """End-to-end readiness for a task.

    Loads the task and ACs through the repository, runs DoR (#36),
    parses persisted risks, computes the score, and returns a
    ReadinessReport. Recommendations are left empty here — the
    Recommendations engine (#38) populates them.
    """
    dor = await evaluate_dor(db, task_id)
    row = await repo.get_task(db, task_id)
    # 'risks' is a guaranteed column post-migrations (review I10).
    risks_raw = row["risks"] if row is not None else None
    risks = parse_risks_from_row(risks_raw)

    score, components = calculate_score_from_data(dor=dor, risks=risks, config=config)
    # NB: ``dor_passed`` and ``score`` are independent signals.
    # ``dor_passed`` is the binary gate (all required checks satisfied);
    # ``score`` reflects DoR + risks. A task can be ``dor_passed=True``
    # with score < 100 if it carries risks — that's by design, since the
    # score is a refinement signal and the DoR gate is a hard yes/no.
    # Risks are priced by severity AND by whether a mitigation is written:
    # an open risk costs the full rate, a mitigated one costs less but
    # never nothing (#610). Until then this comment claimed the code
    # looked at mitigation while the code never did.
    return ReadinessReport(
        score=score,
        dor_passed=dor.passed,
        dor_checks=dor.checks,
        risks=risks,
        recommendations=[],
        explain=[c.to_dict() for c in components] if explain else None,
    )


# --- Is the subject of this task already in the base branch? (#1232) ---
#
# Readiness answered one question — is this statement WELL WRITTEN — and never
# the other one: is it still TRUE. On 09.09.2026 four tasks were opened in a
# single day whose subject had not reached ``develop`` yet. Three of them were
# empty work: #1210 (the fix and all three AC tests already lived inside the
# unmerged branch of #1198), #1209 (list_undelivered_completed_branch_tasks,
# base_can_deliver_itself and stranded_base existed only on the branch of
# #1204) and #1159 (the draft evidence pack existed only on the branch of
# #1158, so the only way to write the gatekeeper was to build a SECOND
# computation of the same facts — the very risk that statement forbids).
# #1172 passed the same check on the same day and turned out to be perfectly
# implementable, which is the whole point: a check that refuses everything is
# worse than no check.
#
# THE DISTINCTION EVERYTHING RESTS ON. "The symbol is absent from the base
# branch" is ordinary new work and must open. "The symbol is absent from the
# base branch AND present in somebody else's unfinished branch" is waiting for
# another delivery, dressed up as readiness, and must not.
#
# NOT A GREP. A textual search finds the name in a comment, in a docstring, in
# a SQL string and in a dead call to something nobody ever defined. What the
# executors did by hand on 09.09 was import the module from a fresh
# origin/develop and ask ``hasattr``. Asked here of the syntax tree of the file
# AS IT IS IN THE BASE REF rather than by importing it: the answer to
# "would hasattr find this after import" is the set of names the module BINDS —
# defs, classes, module- and class-level assignments, imports — and reading it
# from ``git show`` needs no checkout of the base branch, runs no project code
# inside the hub, and cannot be fooled by a name that only appears in prose.

SUBJECT_PRESENT = "present"
SUBJECT_NEW_WORK = "new_work"
SUBJECT_STRANDED = "stranded"
SUBJECT_NOT_APPLICABLE = "not_applicable"
SUBJECT_UNKNOWN = "unknown"

# Statuses a branch can be in while its work has NOT been delivered. All four
# incidents came from exactly these: review (#1210, #1159), needs_decision
# (#1209). ``running`` is here because a task can be resubmitting after
# changes_requested and still own a pushed branch.
SUBJECT_UNFINISHED_STATUSES = (
    "running",
    "fix_requested",
    "ci_check",
    "review",
    "needs_decision",
)
# Bounds on the work one opening may cost. Names cost nothing per name — the
# trees are read once — so the cap is about branches, which cost a git call each.
MAX_SUBJECT_BRANCHES = 25
MAX_SUBJECT_NAMES = 40

# Paths are named by affected_areas; identifiers are named in prose. Strip the
# paths first so that ``hub/services/delivery_state.py`` does not also donate
# ``delivery_state`` as if the statement had named a symbol.
_PATHY = re.compile(r"\S*[/\\]\S*")
_SNAKE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")
_CAMEL = re.compile(r"\b[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+\b")


@dataclass(frozen=True)
class SubjectPresence:
    """Where the things this statement names actually live right now."""

    verdict: str
    missing: tuple[str, ...] = ()
    found_in_branch: str = ""
    found_in_task_id: int = 0
    found_in_title: str = ""
    found_in_status: str = ""
    reason: str = ""

    @property
    def blocks_opening(self) -> bool:
        """Only the stranded case refuses. Everything else opens.

        ``not_applicable`` (no names to check, no clone to ask) and
        ``unknown`` (the base ref would not answer) open on purpose: a
        statement whose hints carry no identifiers is a statement this check
        has nothing to say about, and silence dressed as a refusal would cost
        more than the empty task it is trying to prevent.
        """
        return self.verdict == SUBJECT_STRANDED


def subject_names(technical_hints: str | None) -> list[str]:
    """Identifiers a statement names in its hints, in first-seen order.

    Deliberately conservative. The hints are free text in Russian, so an
    ASCII ``snake_case`` or ``CamelCase`` token in them is almost always code;
    a plain English word is not, and requiring an underscore (or a second
    hump) is what keeps ordinary prose out. Over-collection is the cheap
    failure here — a name that is not really a symbol is missing from the base
    branch AND missing from every branch, which is the ``new_work`` verdict,
    which opens the task.
    """
    text = _PATHY.sub(" ", technical_hints or "")
    out: list[str] = []
    for match in (*_SNAKE.finditer(text), *_CAMEL.finditer(text)):
        name = match.group(0)
        if name not in out:
            out.append(name)
    return out[:MAX_SUBJECT_NAMES]


def _bound_names(body: list[ast.stmt], out: set[str]) -> None:
    """Names ``body`` binds as attributes of its module or class."""
    for node in body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            out.add(node.name)
        elif isinstance(node, ast.ClassDef):
            out.add(node.name)
            # Methods are what an executor finds with a second hasattr, so a
            # symbol named as ``Class.method`` or as a bare method name is
            # found either way.
            _bound_names(node.body, out)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    out.add(target.id)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name):
                out.add(node.target.id)
        elif isinstance(node, ast.Import | ast.ImportFrom):
            for alias in node.names:
                out.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.If | ast.Try):
            # Conditional imports and TYPE_CHECKING blocks bind at module
            # level too; a body nested inside a function does not, and is
            # deliberately not walked.
            _bound_names(node.body, out)
            _bound_names(node.orelse, out)
            for handler in getattr(node, "handlers", []):
                _bound_names(handler.body, out)
            _bound_names(getattr(node, "finalbody", []), out)


def defined_names(source: str) -> set[str] | None:
    """What ``hasattr`` would find on this module, read without importing it.

    ``None`` when the source will not parse — never an empty set, because
    "this file says nothing" and "this file could not be read" lead to
    opposite accusations and only one of them is fair.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None
    found: set[str] = set()
    _bound_names(tree.body, found)
    return found


def _py_paths(affected_areas: Any) -> list[str]:
    areas = affected_areas if isinstance(affected_areas, list) else []
    return [str(a).strip() for a in areas if str(a).strip().endswith(".py")]


def _task_areas(task: dict[str, Any]) -> list[str]:
    raw = task.get("affected_areas")
    if isinstance(raw, str):
        try:
            import json

            raw = json.loads(raw)
        except (TypeError, ValueError):
            raw = []
    return [str(a).strip() for a in raw or [] if str(a).strip()]


async def _tree_at(
    repo_path: str, refs: tuple[str, ...]
) -> tuple[str, set[str]] | None:
    """First of ``refs`` whose tree can be read, with that tree.

    ``origin/<branch>`` is tried before ``<branch>`` because the shared clone
    sits on the base branch but may be behind, and the question is what has
    landed upstream — the same rule ``merged_into_base`` follows. The local
    name is the fallback so a clone with no remote (a test fixture, a mirror)
    still answers instead of reporting ``unknown``.
    """
    from hub.integrations.registry import plugins

    for ref in refs:
        tree = await plugins.git_ops.files_at_ref(repo_path, ref)
        if tree is not None:
            return ref, tree
    return None


async def _supplied_by(
    repo_path: str,
    ref: str,
    tree: set[str],
    *,
    want_paths: list[str],
    want_names: list[str],
    read_paths: list[str],
) -> tuple[set[str], bool]:
    """Which of the wanted paths/names this ref carries, and whether it read.

    ``read_paths`` is the search surface — always EVERY .py area the statement
    declares, never only the wanted subset. The branch pass asks about the
    names the base branch lacks, and those names live in the same modules the
    statement already names: narrowing the files to the missing ones would
    parse nothing at all and answer "nobody carries it" to every question.

    The second value is False when a file that should have been parsed could
    not be — the caller turns that into ``unknown`` rather than into a claim
    that the symbol is absent (#725: "we could not look" must never print as
    "we looked and it is not there").
    """
    from hub.integrations.registry import plugins

    supplied = {p for p in want_paths if p in tree}
    if not want_names:
        return supplied, True
    bound: set[str] = set()
    readable = True
    for path in _py_paths(read_paths):
        if path not in tree:
            continue
        source = await plugins.git_ops.file_at_ref(repo_path, ref, path)
        if source is None:
            readable = False
            continue
        names_here = defined_names(source)
        if names_here is None:
            readable = False
            continue
        bound |= names_here
    supplied |= {n for n in want_names if n.split(".")[-1] in bound or n in bound}
    return supplied, readable


def _stranded_reason(
    missing: list[str],
    base: str,
    *,
    branch: str,
    other_id: int,
    title: str,
    status: str,
    supplied: set[str],
) -> str:
    return (
        f"Задача не открыта: её предмета нет в базовой ветке {base}. "
        f"Не найдено в базе: {', '.join(missing)}. "
        f"Найдено в ветке {branch} задачи #{other_id} "
        f"«{title}» ({status}): {', '.join(sorted(supplied))} — "
        f"надо дождаться её доставки. "
        "Зависимость НЕ проставлена автоматически: назвать номер — не то же, "
        "что завести depends_on, и это решение человека. "
        "Если это новая работа, а имена попали в подсказки случайно, поправьте "
        "technical_hints или affected_areas."
    )


async def subject_presence(db: Any, task: dict[str, Any]) -> SubjectPresence:
    """Is what this statement talks about in the base branch yet (#1232)?

    Reads the base branch and, only when something is missing there, the
    branches of unfinished tasks that have a submission. Never goes to the
    network: every ref is read from the clone the project already declares,
    so an opening costs zero fetches — well inside the "not more than one" the
    statement allows.
    """
    from hub.services.orchestration import project_git_context

    task_id = int(task.get("id") or 0)
    names = subject_names(task.get("technical_hints"))
    paths = _task_areas(task)
    if not names and not paths:
        return SubjectPresence(
            verdict=SUBJECT_NOT_APPLICABLE,
            reason="подсказки постановки не называют ни имён, ни путей — проверять нечего",
        )

    ctx = await project_git_context(db, task_id)
    repo_path = str(ctx.get("repo") or "").strip()
    base = str(ctx.get("base_branch") or "").strip() or config.PAIR_BASE_BRANCH
    if not repo_path:
        return SubjectPresence(
            verdict=SUBJECT_NOT_APPLICABLE,
            reason="у проекта не задан workspace_path — клона, у которого можно спросить, нет",
        )

    at_base = await _tree_at(repo_path, (f"origin/{base}", base))
    if at_base is None:
        return SubjectPresence(
            verdict=SUBJECT_UNKNOWN,
            reason=f"базовую ветку {base} в клоне {repo_path} прочитать не удалось",
        )
    base_ref, base_tree = at_base
    supplied, readable = await _supplied_by(
        repo_path,
        base_ref,
        base_tree,
        want_paths=paths,
        want_names=names,
        read_paths=paths,
    )
    if not readable:
        return SubjectPresence(
            verdict=SUBJECT_UNKNOWN,
            reason=f"часть файлов в {base_ref} не прочиталась — судить об отсутствии нельзя",
        )
    wanted = [*paths, *names]
    missing = [w for w in wanted if w not in supplied]
    if not missing:
        return SubjectPresence(
            verdict=SUBJECT_PRESENT,
            reason=f"предмет постановки есть в {base_ref}",
        )

    rows = await repo.list_unmerged_branch_tasks(
        db, exclude_task_id=task_id, statuses=list(SUBJECT_UNFINISHED_STATUSES)
    )
    missing_paths = [m for m in missing if m in paths]
    missing_names = [m for m in missing if m in names]
    looked = 0
    for row in rows:
        if looked >= MAX_SUBJECT_BRANCHES:
            break
        other = dict(row)
        branch = str(other.get("branch") or "").strip()
        if not branch:
            continue
        # "Unfinished branch" means a branch that has something ON it. A task
        # that never submitted has nothing to wait for, and treating it as a
        # blocker would refuse openings on the strength of an empty branch.
        full = await repo.get_task(db, int(other["id"]))
        if full is None or not str(dict(full).get("submission_sha") or "").strip():
            continue
        looked += 1
        at_branch = await _tree_at(repo_path, (f"origin/{branch}", branch))
        if at_branch is None:
            continue
        branch_ref, branch_tree = at_branch
        there, _ = await _supplied_by(
            repo_path,
            branch_ref,
            branch_tree,
            want_paths=missing_paths,
            want_names=missing_names,
            read_paths=paths,
        )
        if not there:
            continue
        other_id = int(other["id"])
        other_title = str(other.get("title") or "")
        other_status = str(other.get("status") or "")
        return SubjectPresence(
            verdict=SUBJECT_STRANDED,
            missing=tuple(missing),
            found_in_branch=branch,
            found_in_task_id=other_id,
            found_in_title=other_title,
            found_in_status=other_status,
            reason=_stranded_reason(
                missing,
                base,
                branch=branch,
                other_id=other_id,
                title=other_title,
                status=other_status,
                supplied=there,
            ),
        )

    return SubjectPresence(
        verdict=SUBJECT_NEW_WORK,
        missing=tuple(missing),
        reason=(
            f"в {base_ref} нет: {', '.join(missing)}; "
            "ни в одной незавершённой ветке этого тоже нет — обычная новая работа"
        ),
    )


__all__ = [
    "DEFAULT_CONFIG",
    "MAX_SUBJECT_BRANCHES",
    "MAX_SUBJECT_NAMES",
    "SUBJECT_NEW_WORK",
    "SUBJECT_NOT_APPLICABLE",
    "SUBJECT_PRESENT",
    "SUBJECT_STRANDED",
    "SUBJECT_UNFINISHED_STATUSES",
    "SUBJECT_UNKNOWN",
    "SubjectPresence",
    "ReadinessConfig",
    "ScoreComponent",
    "calculate_readiness",
    "calculate_score_from_data",
    "defined_names",
    "parse_risks_from_row",
    "subject_names",
    "subject_presence",
]
