from __future__ import annotations

import aiosqlite

from hub import repository as repo
from hub.models import (
    AgentFit,
    RedesignDecision,
    ACVerifiableBy,
    AcceptanceCriterion,
    Recommendation,
    RiskKind,
    RiskSeverity,
    TaskCreate,
    TaskRefine,
    TaskRisk,
    TaskSize,
    WipTag,
    WorkType,
)
from hub.services.dor import DOR_CHECK_KEYS, evaluate_from_data
from hub.services.readiness import DEFAULT_CONFIG, ReadinessConfig
from hub.services.recommendations import (
    CHECK_RECOMMENDATIONS,
    SEVERITY_ORDER,
    build_ac_quality_warnings,
    build_for_task,
    build_recommendations,
    calculate_readiness_with_recommendations,
)


# --- helpers ---


def _empty_dor_kwargs(**overrides):
    base = dict(
        work_type=WorkType.feature.value,
        user_story=None,
        problem_statement=None,
        business_value=None,
        scope_in_count=0,
        validation_count=0,
        size=None,
        wip_tag=None,
        ac_count=0,
    )
    base.update(overrides)
    return base


def _ac(idx: int = 1) -> AcceptanceCriterion:
    return AcceptanceCriterion(
        id=f"AC-{idx}",
        given="a logged-in user on the dashboard",
        when="they click the export button",
        then="a CSV download starts within 2 seconds",
        verifiable_by=ACVerifiableBy.test,
        # A complete criterion now says where its expectation came from
        # (#595). Without it these "nothing to suggest" cases would carry the
        # unstated-source warning, which is the point of the warning.
        expectation_source="requirement",
    )


def _thin_ac(idx: int = 1) -> AcceptanceCriterion:
    """Formally valid but empty-by-meaning AC (placeholder clauses)."""
    return AcceptanceCriterion(
        id=f"AC-{idx}",
        given="g",
        when="w",
        then="t",
        verifiable_by=ACVerifiableBy.test,
    )


# --- coverage / static config ---


def test_every_dor_check_has_a_recommendation_template():
    assert set(CHECK_RECOMMENDATIONS) == set(DOR_CHECK_KEYS)


def test_severity_order_covers_all_severities():
    assert set(SEVERITY_ORDER) == {"blocking", "high", "medium", "low"}


def test_each_recommendation_template_has_required_keys():
    for key, tpl in CHECK_RECOMMENDATIONS.items():
        assert {"field", "message", "minutes"} <= tpl.keys(), key
        assert isinstance(tpl["minutes"], int)
        assert tpl["minutes"] >= 0


# --- build_recommendations: pure ---


def test_perfect_task_yields_no_recommendations():
    dor = evaluate_from_data(
        **_empty_dor_kwargs(
            user_story="us",
            problem_statement="ps",
            business_value="bv",
            scope_in_count=1,
            validation_count=1,
            size="S",
            wip_tag="feature_work",
            ac_count=1,
            affected_areas_count=1,
            outcome_metric="median lead time, 3d -> 1d",
            redesign_decision="adapt",
            agent_fit="sdd_native",
        )
    )
    assert build_recommendations(dor) == []


def test_empty_feature_yields_blocking_per_required_check():
    dor = evaluate_from_data(**_empty_dor_kwargs())
    recs = build_recommendations(dor)
    blocking = [r for r in recs if r.severity == "blocking"]
    assert len(blocking) == len(dor.required)
    assert all(
        r.expected_score_delta == DEFAULT_CONFIG.penalty_required for r in blocking
    )


def test_optional_failures_yield_low_severity():
    """For docs work_type, only scope+size are required; everything else is optional."""
    dor = evaluate_from_data(
        **_empty_dor_kwargs(
            work_type=WorkType.docs.value,
            scope_in_count=1,
            size="S",
        )
    )
    recs = build_recommendations(dor)
    # All produced recommendations are low (no required failures left).
    assert all(r.severity == "low" for r in recs)
    assert all(r.expected_score_delta == DEFAULT_CONFIG.penalty_optional for r in recs)


def test_recommendations_sorted_blocking_first():
    """Mix of blocking and low — blocking must come first.

    For ``bug`` work_type after fix #1.2: user_story is optional but
    business_value is required, so we still get a mix of severities.
    """
    dor = evaluate_from_data(
        **_empty_dor_kwargs(
            work_type=WorkType.bug.value,
            problem_statement=None,
            scope_in_count=0,
            validation_count=0,
            size=None,
            wip_tag=None,
            ac_count=0,
        )
    )
    recs = build_recommendations(dor)
    severities = [SEVERITY_ORDER[r.severity] for r in recs]
    assert severities == sorted(severities)
    assert recs[0].severity == "blocking"


def test_recommendation_field_matches_template():
    dor = evaluate_from_data(**_empty_dor_kwargs())
    by_field = {r.field for r in build_recommendations(dor)}
    expected_fields = {tpl["field"] for tpl in CHECK_RECOMMENDATIONS.values()}
    # all 8 templates fail for an empty task (some required, some optional)
    assert by_field == expected_fields


def test_estimated_minutes_propagated_from_template():
    dor = evaluate_from_data(**_empty_dor_kwargs())
    recs = build_recommendations(dor)
    by_field = {r.field: r for r in recs}
    for key, tpl in CHECK_RECOMMENDATIONS.items():
        assert by_field[tpl["field"]].estimated_minutes == tpl["minutes"]


def test_build_recommendations_uses_custom_config():
    cfg = ReadinessConfig(penalty_required=20, penalty_optional=5)
    dor = evaluate_from_data(
        **_empty_dor_kwargs(work_type=WorkType.docs.value, scope_in_count=1, size="S")
    )
    recs = build_recommendations(dor, config=cfg)
    for r in recs:
        if r.severity == "blocking":
            assert r.expected_score_delta == 20
        elif r.severity == "low":
            assert r.expected_score_delta == 5


def test_recommendations_are_pydantic_models():
    dor = evaluate_from_data(**_empty_dor_kwargs())
    recs = build_recommendations(dor)
    assert all(isinstance(r, Recommendation) for r in recs)


def test_no_recommendations_for_risks_only():
    """Risks affect score, not the recommendation list."""
    dor = evaluate_from_data(
        **_empty_dor_kwargs(
            user_story="us",
            problem_statement="ps",
            business_value="bv",
            scope_in_count=1,
            validation_count=1,
            size="S",
            wip_tag="feature_work",
            ac_count=1,
            affected_areas_count=1,
            outcome_metric="median lead time, 3d -> 1d",
            redesign_decision="adapt",
            agent_fit="sdd_native",
        )
    )
    recs = build_recommendations(dor)
    assert recs == []
    # (risks are passed to readiness, not to recommendations)


# --- async build_for_task ---


async def _make_minimal_task(db: aiosqlite.Connection) -> int:
    payload = TaskCreate(title="t")
    task_id = await repo.create_task_full(db, payload, status="draft")
    await db.commit()
    return task_id


async def _make_full_task(db: aiosqlite.Connection) -> int:
    payload = TaskCreate(
        title="t",
        user_story="us",
        problem_statement="ps",
        business_value="bv",
        scope_in=["a"],
        validation_commands=["pytest"],
        size=TaskSize.S,
        wip_tag=WipTag.feature_work,
        affected_areas=["hub/services/dor.py"],
        # Discovery (#331) is part of a complete feature task now: without it
        # the task is not "full", it is merely fully specified.
        outcome_metric="median lead time, 3d -> 1d",
        redesign_decision=RedesignDecision.adapt,
        agent_fit=AgentFit.sdd_native,
    )
    task_id = await repo.create_task_full(db, payload, status="draft")
    await repo.add_acceptance_criterion(db, task_id, _ac(1))
    await db.commit()
    return task_id


async def test_build_for_task_minimal(db: aiosqlite.Connection):
    task_id = await _make_minimal_task(db)
    recs = await build_for_task(db, task_id)
    assert {r.field for r in recs} >= {"user_story", "scope_in", "size"}
    discovery = {"outcome_metric", "redesign_decision", "agent_fit"}
    assert all(r.severity == "blocking" for r in recs if r.field not in discovery)
    # Discovery suggestions are offered but never charged for (#331).
    assert all(
        r.severity == "low" and r.expected_score_delta == 0
        for r in recs
        if r.field in discovery
    )


async def test_build_for_task_full_returns_empty(db: aiosqlite.Connection):
    task_id = await _make_full_task(db)
    recs = await build_for_task(db, task_id)
    assert recs == []


# --- end-to-end: report with recommendations ---


async def test_calculate_readiness_with_recommendations_perfect(
    db: aiosqlite.Connection,
):
    task_id = await _make_full_task(db)
    report = await calculate_readiness_with_recommendations(db, task_id)
    assert report.score == 100
    assert report.dor_passed is True
    assert report.recommendations == []
    assert report.explain is None


async def test_calculate_readiness_with_recommendations_empty_task(
    db: aiosqlite.Connection,
):
    task_id = await _make_minimal_task(db)
    report = await calculate_readiness_with_recommendations(db, task_id, explain=True)
    assert report.score < 100
    assert report.dor_passed is False
    assert len(report.recommendations) > 0
    # First recommendation must be blocking (sorting invariant).
    assert report.recommendations[0].severity == "blocking"
    # explain must mirror the score delta
    assert sum(e["delta"] for e in report.explain) == report.score - 100


async def test_calculate_readiness_with_recommendations_includes_risks(
    db: aiosqlite.Connection,
):
    task_id = await _make_full_task(db)
    await repo.update_task_structured(
        db,
        task_id,
        TaskRefine(
            risks=[
                TaskRisk(
                    kind=RiskKind.security,
                    severity=RiskSeverity.high,
                    description="d",
                    mitigation="m",
                )
            ]
        ),
    )
    await db.commit()
    report = await calculate_readiness_with_recommendations(db, task_id)
    assert len(report.risks) == 1
    # Risk lowers score but does not generate a recommendation.
    assert (
        report.score == 100 - DEFAULT_CONFIG.mitigated_risk_penalties[RiskSeverity.high]
    ), "the risk carries a mitigation, so the softer rate applies (#610)"
    assert report.recommendations == []


async def test_ac_quality_warning_for_thin_ac_is_non_blocking(
    db: aiosqlite.Connection,
):
    """A task with hollow ACs still passes DoR (presence-only), but gets a
    low-severity, score-neutral nudge about AC quality (#6)."""
    payload = TaskCreate(
        title="t",
        user_story="us",
        problem_statement="ps",
        business_value="bv",
        scope_in=["a"],
        validation_commands=["pytest"],
        size=TaskSize.S,
        wip_tag=WipTag.feature_work,
        affected_areas=["hub/services/dor.py"],
    )
    task_id = await repo.create_task_full(db, payload, status="draft")
    await repo.add_acceptance_criterion(db, task_id, _thin_ac(1))
    await db.commit()

    report = await calculate_readiness_with_recommendations(db, task_id)
    # Presence-only DoR is unaffected.
    assert report.score == 100
    assert report.dor_passed is True
    quality = [
        r
        for r in report.recommendations
        if r.field == "acceptance_criteria" and r.severity == "low"
    ]
    assert len(quality) == 1
    assert "AC-1" in quality[0].message
    assert quality[0].expected_score_delta == 0


async def test_no_ac_quality_warning_for_substantive_ac(db: aiosqlite.Connection):
    task_id = await _make_full_task(db)
    report = await calculate_readiness_with_recommendations(db, task_id)
    assert report.recommendations == []


def test_build_ac_quality_warnings_unit():
    thin_rows = [
        {"ac_id": "AC-1", "given": "g", "when_clause": "w", "then_clause": "t"}
    ]
    good_rows = [
        {
            "ac_id": "AC-2",
            "given": "a logged-in user on the dashboard",
            "when_clause": "they click the export button",
            "then_clause": "a CSV download starts within 2 seconds",
        }
    ]
    assert len(build_ac_quality_warnings(thin_rows)) == 1
    assert build_ac_quality_warnings(good_rows) == []
    assert build_ac_quality_warnings([]) == []


def test_build_ac_quality_warnings_detects_long_placeholder():
    """A clause longer than the length cutoff but made only of placeholder
    tokens (e.g. 'n/a n/a n/a') must still be flagged — guards against the
    previously-dead placeholder check."""
    placeholder_rows = [
        {
            "ac_id": "AC-9",
            "given": "n/a n/a n/a",
            "when_clause": "a substantive precondition clause here",
            "then_clause": "a substantive outcome clause here too",
        }
    ]
    warnings = build_ac_quality_warnings(placeholder_rows)
    assert len(warnings) == 1
    assert "AC-9" in warnings[0].message


async def test_score_after_applying_all_recommendations_returns_to_max(
    db: aiosqlite.Connection,
):
    """Sanity: if a user actually filled in every recommended field,
    expected_score_delta sums must explain the gap to 100."""
    task_id = await _make_minimal_task(db)
    report = await calculate_readiness_with_recommendations(db, task_id)
    total_recoverable = sum(r.expected_score_delta for r in report.recommendations)
    assert report.score + total_recoverable == 100
