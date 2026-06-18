"""Recommendations engine for task readiness.

Given a DoR evaluation (#36) and the readiness scoring config (#37),
this module produces a sorted, actionable list of suggestions for the
human or AI Analyst preparing the task.

Design choices:
- We do NOT generate recommendations for risks. Risks already cost
  score in the readiness calculator; suggesting "remove this risk"
  would either be dishonest (you can't wish risks away) or trivial
  ("write a better mitigation"). Mitigation quality lives at task
  authoring level, not in the engine.
- We do NOT use an LLM. Every message is a deterministic template.
  This keeps recommendations cheap, repeatable, and reviewable.
- ``expected_score_delta`` mirrors the ReadinessConfig penalty so the
  numbers shown to a user actually match what the score would become
  after fixing the field.
"""

from __future__ import annotations

from typing import Any

from hub.models import (
    DoRCheckItem,
    Recommendation,
    RecommendationSeverity,
)
from hub.services.dor import DoREvaluation, evaluate_dor
from hub import repository as repo
from hub.models import ReadinessReport
from hub.services.readiness import (
    DEFAULT_CONFIG,
    ReadinessConfig,
    calculate_score_from_data,
    parse_risks_from_row,
)

# Static templates per check key. ``field`` is the task field a user
# would edit to satisfy the check; ``minutes`` is a rough per-item
# effort hint shown to the author. The total time to refine a task is
# NOT a sum of these numbers — many recommendations share context and
# can be answered together. Treat ``minutes`` as a per-step upper
# bound, never as a scheduling input.
#
# Style guide for ``message``:
#   1) Tell the author what to do (verb-first).
#   2) Tell them WHY in plain English (no internal jargon like
#      "verifiable_by", "DoR", "WIP" without expansion).
#   3) Give a concrete example so the author isn't blocked on form.
CHECK_RECOMMENDATIONS: dict[str, dict[str, Any]] = {
    "has_user_story": {
        "field": "user_story",
        "message": (
            "Add a user story so the developer knows who the change is for "
            "and what outcome they want. Use the form: "
            "'As a <role>, I want <action>, so that <value>.'"
        ),
        "minutes": 5,
    },
    "has_problem_statement": {
        "field": "problem_statement",
        "message": (
            "Describe the problem this task solves and why it matters now, "
            "so the developer can judge trade-offs without asking back."
        ),
        "minutes": 5,
    },
    "has_business_value": {
        "field": "business_value",
        "message": (
            "Explain why this task is worth doing right now. One concrete "
            "outcome is enough — for example: 'unblocks 3 paying customers', "
            "'cuts onboarding time from 10 to 2 minutes', or "
            "'eliminates daily on-call alert about queue X'."
        ),
        "minutes": 3,
    },
    "has_scope_in": {
        "field": "scope_in",
        "message": (
            "List in-scope items (modules, files, behaviors) so the "
            "developer knows where to act and where to stop."
        ),
        "minutes": 5,
    },
    "has_acceptance_criteria": {
        "field": "acceptance_criteria",
        "message": (
            "Define at least one acceptance criterion using Given/When/Then "
            "and say HOW it will be checked (a test name, a CLI command, a "
            "manual UI step, or a metric). This is the contract the "
            "developer will sign off on."
        ),
        "minutes": 10,
    },
    "has_validation_commands": {
        "field": "validation_commands",
        "message": (
            "Add the commands that prove the change actually works "
            "(e.g. 'uv run pytest hub/tests/test_dor.py', "
            "'curl -fsS http://localhost:8765/healthz'). Linters alone "
            "do not count — pick something that exercises behavior."
        ),
        "minutes": 3,
    },
    "has_size": {
        "field": "size",
        "message": (
            "Pick a T-shirt size (XS/S/M/L/XL) so we can plan capacity "
            "and avoid taking on more work than the team can finish."
        ),
        "minutes": 1,
    },
    "has_wip_tag": {
        "field": "wip_tag",
        "message": (
            "Set a wip_tag (feature_work / bugfix / tech_debt / support) "
            "so this task counts against the right capacity bucket."
        ),
        "minutes": 1,
    },
}

# Sort order for rendering — blocking first, low last.
SEVERITY_ORDER: dict[RecommendationSeverity, int] = {
    "blocking": 0,
    "high": 1,
    "medium": 2,
    "low": 3,
}

# Acceptance-criteria quality heuristics (feedback #6). DoR only checks that
# ACs EXIST, not whether they say anything meaningful — so a task can pass DoR
# with formally-valid but empty-by-meaning criteria. These warnings are
# strictly NON-blocking (severity="low", expected_score_delta=0): they never
# change the score or dor_passed, they just nudge the author. Final quality
# judgement still belongs to the reviewer.
AC_QUALITY_MIN_LEN = 12
_AC_PLACEHOLDER_TOKENS = {
    "tbd",
    "tba",
    "todo",
    "na",
    "n/a",
    "-",
    "--",
    "xxx",
    "?",
    "...",
    "none",
}


def _ac_clause_is_thin(text: str | None) -> bool:
    """True when a Given/When/Then clause is too short or a placeholder.

    Placeholder tokens are checked BEFORE the length cutoff: every token in
    ``_AC_PLACEHOLDER_TOKENS`` is shorter than ``AC_QUALITY_MIN_LEN``, so a
    length-first check would short-circuit and the set would be dead code. We
    also treat a clause built only from placeholder tokens (e.g. "n/a n/a n/a")
    as thin even though it clears the length cutoff.
    """
    t = (text or "").strip().lower()
    if not t:
        return True
    tokens = t.split()
    if t in _AC_PLACEHOLDER_TOKENS or all(
        tok in _AC_PLACEHOLDER_TOKENS for tok in tokens
    ):
        return True
    return len(t) < AC_QUALITY_MIN_LEN


def build_ac_quality_warnings(ac_rows: list[Any]) -> list[Recommendation]:
    """Emit at most one low-severity warning when some ACs look hollow.

    ``ac_rows`` are rows from ``repo.list_acceptance_criteria`` (columns
    ``ac_id``/``given``/``when_clause``/``then_clause``). Returns an empty
    list when every AC has substantive clauses.
    """
    weak: list[str] = []
    for row in ac_rows:
        if (
            _ac_clause_is_thin(row["given"])
            or _ac_clause_is_thin(row["when_clause"])
            or _ac_clause_is_thin(row["then_clause"])
        ):
            weak.append(row["ac_id"])
    if not weak:
        return []
    return [
        Recommendation(
            field="acceptance_criteria",
            severity="low",
            message=(
                f"Acceptance criteria {', '.join(weak)} look thin (very short "
                "or placeholder Given/When/Then). The Definition of Ready only "
                "checks that criteria exist, not their quality — strengthen "
                "them so a reviewer can actually sign off."
            ),
            expected_score_delta=0,
            estimated_minutes=5,
        )
    ]


def _recommendation_for(
    check: DoRCheckItem,
    *,
    is_required: bool,
    config: ReadinessConfig,
) -> Recommendation | None:
    """Build a recommendation for one failed DoR check."""
    template = CHECK_RECOMMENDATIONS.get(check.key)
    if template is None:
        return None
    severity: RecommendationSeverity = "blocking" if is_required else "low"
    delta = config.penalty_required if is_required else config.penalty_optional
    return Recommendation(
        field=template["field"],
        severity=severity,
        message=template["message"],
        expected_score_delta=delta,
        estimated_minutes=template["minutes"],
    )


def build_recommendations(
    dor: DoREvaluation,
    *,
    config: ReadinessConfig = DEFAULT_CONFIG,
) -> list[Recommendation]:
    """Build a sorted recommendation list from a DoR evaluation.

    - Failed REQUIRED checks → severity='blocking', delta = penalty_required.
    - Failed OPTIONAL checks → severity='low', delta = penalty_optional.
    - Passed checks → no recommendation (nothing to suggest).
    - Unknown check keys (shouldn't happen) → silently skipped.

    Sort: blocking → high → medium → low (within a severity, original
    DOR_CHECK_KEYS order is preserved for stable rendering).
    """
    recs: list[Recommendation] = []
    for check in dor.checks:
        if check.passed:
            continue
        rec = _recommendation_for(
            check, is_required=check.key in dor.required, config=config
        )
        if rec is not None:
            recs.append(rec)
    recs.sort(key=lambda r: SEVERITY_ORDER[r.severity])
    return recs


async def build_for_task(
    db,
    task_id: int,
    *,
    config: ReadinessConfig = DEFAULT_CONFIG,
) -> list[Recommendation]:
    """Async wrapper: load the task, evaluate DoR, build recommendations."""
    dor = await evaluate_dor(db, task_id)
    return build_recommendations(dor, config=config)


async def calculate_readiness_with_recommendations(
    db,
    task_id: int,
    *,
    explain: bool = False,
    config: ReadinessConfig = DEFAULT_CONFIG,
) -> ReadinessReport:
    """End-to-end: ReadinessReport with score, dor checks, risks, and
    populated recommendations — single DB roundtrip per data source.

    Lives here (not in readiness.py) to keep readiness free of any
    knowledge of the recommendation engine. The dependency direction
    stays one-way: recommendations -> readiness/dor.
    """
    dor = await evaluate_dor(db, task_id)
    row = await repo.get_task(db, task_id)
    # 'risks' is a guaranteed column post-migrations (review I10).
    risks_raw = row["risks"] if row is not None else None
    risks = parse_risks_from_row(risks_raw)

    score, components = calculate_score_from_data(dor=dor, risks=risks, config=config)
    recs = build_recommendations(dor, config=config)
    # Non-blocking AC-quality nudge (#6): does not affect score/dor_passed.
    ac_rows = await repo.list_acceptance_criteria(db, task_id)
    recs.extend(build_ac_quality_warnings(ac_rows))
    recs.sort(key=lambda r: SEVERITY_ORDER[r.severity])

    return ReadinessReport(
        score=score,
        dor_passed=dor.passed,
        dor_checks=dor.checks,
        missing_required=sorted(dor.missing_required),
        risks=risks,
        recommendations=recs,
        explain=[c.to_dict() for c in components] if explain else None,
    )


__all__ = [
    "CHECK_RECOMMENDATIONS",
    "SEVERITY_ORDER",
    "build_ac_quality_warnings",
    "build_for_task",
    "build_recommendations",
    "calculate_readiness_with_recommendations",
]
