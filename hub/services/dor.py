"""Definition of Ready (DoR) evaluator.

Pure deterministic logic — no LLM involvement. The required check set is
table-driven per ``WorkType`` so that the gate can be relaxed for chores
or tightened for incidents without touching call sites.

Severity (blocking/high/medium/low) intentionally lives in the
Recommendations engine (#38), not here. DoR only answers "is this check
satisfied?". It is the readiness/recommendations layer that decides how
much a missing piece costs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from hub import repository as repo
from hub.db import deserialize_str_list
from hub.models import DoRCheckItem, WorkType

log = logging.getLogger("hub.services.dor")

# All known DoR check keys. Anything outside this tuple is a typo somewhere.
DOR_CHECK_KEYS: tuple[str, ...] = (
    "has_user_story",
    "has_problem_statement",
    "has_business_value",
    "has_scope_in",
    "has_acceptance_criteria",
    "has_validation_commands",
    "has_size",
    "has_wip_tag",
    # Discovery checks (#331). Advisory — see DOR_ADVISORY_KEYS below.
    "has_outcome_hypothesis",
    "has_redesign_decision",
    "has_agent_fit",
)

# Checks that are visible but free. They appear in the DoR table and earn a
# recommendation, but cost nothing in the readiness score.
#
# Why a separate set rather than "just leave them out of every required
# profile": a non-required failing check still costs penalty_optional
# (readiness.py). Since no task in an existing installation can have these
# fields — they did not exist until this change — every task in the backlog
# would silently lose points on the day this ships, and would look like it
# had got worse without anyone touching it. Advisory keys are scored at
# zero, following the precedent of the AC-quality warnings (#331).
DOR_ADVISORY_KEYS: frozenset[str] = frozenset(
    {
        "has_outcome_hypothesis",
        "has_redesign_decision",
        "has_agent_fit",
    }
)

# Which work types are actually ASKED for Discovery. The spec scopes the
# Discovery block to the feature profile, and that scoping is what keeps the
# nudges meaningful: a bugfix or a chore has no outcome hypothesis to state,
# and three permanent suggestions on every task in the backlog would become
# wallpaper within a week. Checks outside this set are still evaluated and
# still rendered — they just do not generate a suggestion (#331).
DOR_ADVISORY_WORK_TYPES: frozenset[str] = frozenset({WorkType.feature.value})

# Required check sets per work type.
#
# Rationale per profile:
# - feature: full DoR — most expensive to do wrong, must be ready.
# - bug: skip user_story (problem_statement is the "what broke"), but keep
#   business_value so we can distinguish a $1M-customer P1 from a cosmetic
#   glitch. AC + validation are mandatory so the fix is verifiable.
# - refactor: no user_story / business_value (internal change). wip_tag
#   required for capacity tracking — refactors usually load tech_debt.
# - chore: minimal — scope, validation, size. Lots of chores would never
#   pass full DoR and would just clutter the inbox.
# - docs: scope + size only. Adding ACs to a doc change is overkill.
# - spike: time-boxed exploration. AC required as a proxy for the
#   completion criterion (e.g. "we have a documented answer to <Q>"); a
#   first-class ``timebox_hours`` field is on the post-MVP backlog.
# - incident: must explain what broke (problem_statement) and how we'll
#   verify the fix (validation_commands + AC). Even under fire, the team
#   needs an explicit "fixed when" criterion so the postmortem is honest.
DOR_REQUIRED_BY_WORK_TYPE: dict[str, frozenset[str]] = {
    WorkType.feature.value: frozenset(
        {
            "has_user_story",
            "has_problem_statement",
            "has_business_value",
            "has_scope_in",
            "has_acceptance_criteria",
            "has_validation_commands",
            "has_size",
            "has_wip_tag",
        }
    ),
    WorkType.bug.value: frozenset(
        {
            "has_problem_statement",
            "has_business_value",
            "has_scope_in",
            "has_acceptance_criteria",
            "has_validation_commands",
            "has_size",
            "has_wip_tag",
        }
    ),
    WorkType.refactor.value: frozenset(
        {
            "has_problem_statement",
            "has_scope_in",
            "has_acceptance_criteria",
            "has_validation_commands",
            "has_size",
            "has_wip_tag",
        }
    ),
    WorkType.chore.value: frozenset(
        {"has_scope_in", "has_validation_commands", "has_size"}
    ),
    WorkType.docs.value: frozenset({"has_scope_in", "has_size"}),
    WorkType.spike.value: frozenset(
        {"has_problem_statement", "has_acceptance_criteria", "has_size"}
    ),
    WorkType.incident.value: frozenset(
        {
            "has_problem_statement",
            "has_acceptance_criteria",
            "has_validation_commands",
        }
    ),
}


@dataclass(frozen=True)
class DoREvaluation:
    """Result of a DoR evaluation for one task.

    - ``checks`` always contains the full ``DOR_CHECK_KEYS`` set so
      consumers can render a complete table.
    - ``required`` is the subset that must pass for ``passed=True``.
    - ``missing_required`` is the easy lookup for what to fix first.
    """

    checks: list[DoRCheckItem]
    required: frozenset[str]
    missing_required: frozenset[str]
    # Advisory keys that this work type is asked about (#331). Never part of
    # ``required``, so never blocking and never scored — they only decide
    # whether a suggestion is offered.
    advisory: frozenset[str] = frozenset()

    @property
    def passed(self) -> bool:
        return not self.missing_required


def _required_for(work_type: str | None) -> frozenset[str]:
    """Resolve required checks for a work type, defaulting to feature.

    Unknown / missing work_type is treated as 'feature' on purpose:
    strict-by-default avoids accidentally letting under-specified tasks
    sneak past the gate. Unknown values are logged so a missing profile
    after a WorkType extension does not stay silent in production.
    """
    if not work_type:
        return DOR_REQUIRED_BY_WORK_TYPE[WorkType.feature.value]
    profile = DOR_REQUIRED_BY_WORK_TYPE.get(work_type)
    if profile is None:
        log.warning(
            "unknown work_type %r — falling back to 'feature' DoR profile", work_type
        )
        return DOR_REQUIRED_BY_WORK_TYPE[WorkType.feature.value]
    return profile


def evaluate_from_data(
    *,
    work_type: str | None,
    user_story: str | None,
    problem_statement: str | None,
    business_value: str | None,
    scope_in_count: int,
    validation_count: int,
    size: str | None,
    wip_tag: str | None,
    ac_count: int,
    outcome_metric: str | None = None,
    redesign_decision: str | None = None,
    agent_fit: str | None = None,
) -> DoREvaluation:
    """Pure, side-effect-free DoR evaluation from explicit data.

    Always returns checks for every key in ``DOR_CHECK_KEYS``, regardless
    of whether the work type cares about each one. ``passed`` is computed
    only against the required subset for the given ``work_type``.
    """
    checks_by_key: dict[str, DoRCheckItem] = {
        "has_user_story": DoRCheckItem(
            key="has_user_story",
            passed=bool(user_story and user_story.strip()),
            detail="user_story is filled" if user_story else "user_story is empty",
        ),
        "has_problem_statement": DoRCheckItem(
            key="has_problem_statement",
            passed=bool(problem_statement and problem_statement.strip()),
            detail=(
                "problem_statement is filled"
                if problem_statement
                else "problem_statement is empty"
            ),
        ),
        "has_business_value": DoRCheckItem(
            key="has_business_value",
            passed=bool(business_value and business_value.strip()),
            detail=(
                "business_value is filled"
                if business_value
                else "business_value is empty"
            ),
        ),
        "has_scope_in": DoRCheckItem(
            key="has_scope_in",
            passed=scope_in_count > 0,
            detail=f"scope_in has {scope_in_count} item(s)",
        ),
        "has_acceptance_criteria": DoRCheckItem(
            key="has_acceptance_criteria",
            passed=ac_count > 0,
            detail=f"{ac_count} acceptance criteria defined",
        ),
        "has_validation_commands": DoRCheckItem(
            key="has_validation_commands",
            passed=validation_count > 0,
            detail=f"{validation_count} validation command(s) defined",
        ),
        "has_size": DoRCheckItem(
            key="has_size",
            passed=bool(size),
            detail=f"size = {size}" if size else "size is not set",
        ),
        "has_wip_tag": DoRCheckItem(
            key="has_wip_tag",
            passed=bool(wip_tag),
            detail=f"wip_tag = {wip_tag}" if wip_tag else "wip_tag is not set",
        ),
        "has_outcome_hypothesis": DoRCheckItem(
            key="has_outcome_hypothesis",
            passed=bool(outcome_metric and outcome_metric.strip()),
            detail=(
                "outcome_metric is filled"
                if outcome_metric
                else "outcome_metric is empty"
            ),
        ),
        "has_redesign_decision": DoRCheckItem(
            key="has_redesign_decision",
            passed=bool(redesign_decision),
            detail=(
                f"redesign_decision = {redesign_decision}"
                if redesign_decision
                else "redesign_decision is not set"
            ),
        ),
        "has_agent_fit": DoRCheckItem(
            key="has_agent_fit",
            passed=bool(agent_fit),
            detail=f"agent_fit = {agent_fit}" if agent_fit else "agent_fit is not set",
        ),
    }

    # Stable order — always DOR_CHECK_KEYS — for deterministic UI rendering.
    checks = [checks_by_key[k] for k in DOR_CHECK_KEYS]
    required = _required_for(work_type)
    missing = frozenset(c.key for c in checks if c.key in required and not c.passed)
    advisory = (
        DOR_ADVISORY_KEYS
        if (work_type or WorkType.feature.value) in DOR_ADVISORY_WORK_TYPES
        else frozenset()
    )
    return DoREvaluation(
        checks=checks,
        required=required,
        missing_required=missing,
        advisory=advisory,
    )


async def evaluate_dor(db, task_id: int) -> DoREvaluation:
    """Load task data + ACs from the repository and evaluate DoR."""
    row = await repo.get_task(db, task_id)
    if row is None:
        raise ValueError(f"task {task_id} not found")
    acs = await repo.list_acceptance_criteria(db, task_id)

    # The structured columns are guaranteed to exist after migration #46
    # for tasks-table. With strict migrations (review I2) it's safer to
    # let a KeyError propagate than to silently return None and hide a
    # missing-column bug behind a "task is empty" diagnostic.
    return evaluate_from_data(
        work_type=row["work_type"],
        user_story=row["user_story"],
        problem_statement=row["problem_statement"],
        business_value=row["business_value"],
        scope_in_count=len(deserialize_str_list(row["scope_in"])),
        validation_count=len(deserialize_str_list(row["validation_commands"])),
        size=row["size"],
        wip_tag=row["wip_tag"],
        ac_count=len(acs),
        outcome_metric=row["outcome_metric"],
        redesign_decision=row["redesign_decision"],
        agent_fit=row["agent_fit"],
    )


__all__ = [
    "DOR_ADVISORY_KEYS",
    "DOR_ADVISORY_WORK_TYPES",
    "DOR_CHECK_KEYS",
    "DOR_REQUIRED_BY_WORK_TYPE",
    "DoREvaluation",
    "evaluate_dor",
    "evaluate_from_data",
]
