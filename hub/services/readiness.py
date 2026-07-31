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

import logging
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

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

    def penalty_for_risk(self, severity: RiskSeverity) -> int:
        return self.risk_penalties.get(severity, 0)


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
        penalty = config.penalty_for_risk(risk.severity)
        if penalty <= 0:
            continue
        score -= penalty
        components.append(
            ScoreComponent(
                field="risks",
                delta=-penalty,
                reason=(
                    f"risk #{idx + 1} {risk.kind.value} "
                    f"(severity={risk.severity.value})"
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
    # with score < 100 if it carries unmitigated risks — that's by
    # design, since the score is a refinement signal and the DoR gate
    # is a hard yes/no.
    return ReadinessReport(
        score=score,
        dor_passed=dor.passed,
        dor_checks=dor.checks,
        risks=risks,
        recommendations=[],
        explain=[c.to_dict() for c in components] if explain else None,
    )


__all__ = [
    "DEFAULT_CONFIG",
    "ReadinessConfig",
    "ScoreComponent",
    "calculate_readiness",
    "calculate_score_from_data",
    "parse_risks_from_row",
]
