from __future__ import annotations

import aiosqlite

from hub import repository as repo
from hub.models import (
    ACVerifiableBy,
    AcceptanceCriterion,
    DoRCheckItem,
    RiskKind,
    RiskSeverity,
    TaskCreate,
    TaskRefine,
    TaskRisk,
    TaskSize,
    WipTag,
    WorkType,
)
from hub.services.dor import DoREvaluation, evaluate_from_data
from hub.services.readiness import (
    DEFAULT_CONFIG,
    ReadinessConfig,
    ScoreComponent,
    calculate_readiness,
    calculate_score_from_data,
)


# --- helpers ---


def _ac(idx: int = 1) -> AcceptanceCriterion:
    return AcceptanceCriterion(
        id=f"AC-{idx}",
        given="g",
        when="w",
        then="t",
        verifiable_by=ACVerifiableBy.test,
    )


def _all_passed_dor(work_type: WorkType = WorkType.feature) -> DoREvaluation:
    return evaluate_from_data(
        work_type=work_type.value,
        user_story="us",
        problem_statement="ps",
        business_value="bv",
        scope_in_count=1,
        validation_count=1,
        size="S",
        wip_tag="feature_work",
        ac_count=1,
    )


def _empty_dor(work_type: WorkType = WorkType.feature) -> DoREvaluation:
    return evaluate_from_data(
        work_type=work_type.value,
        user_story=None,
        problem_statement=None,
        business_value=None,
        scope_in_count=0,
        validation_count=0,
        size=None,
        wip_tag=None,
        ac_count=0,
    )


# --- pure scoring ---


def test_perfect_feature_no_risks_scores_100():
    score, components = calculate_score_from_data(dor=_all_passed_dor(), risks=[])
    assert score == 100
    assert components == []


def test_empty_feature_drops_score_by_required_count_times_penalty():
    dor = _empty_dor()
    score, components = calculate_score_from_data(dor=dor, risks=[])
    expected_required_failed = len(dor.required)  # all 8 fail
    assert score == 100 - expected_required_failed * DEFAULT_CONFIG.penalty_required
    assert all(c.delta == -DEFAULT_CONFIG.penalty_required for c in components)
    assert len(components) == expected_required_failed


def test_optional_check_failures_use_optional_penalty():
    """For docs: only scope+size required. user_story etc. failing is optional."""
    dor = evaluate_from_data(
        work_type=WorkType.docs.value,
        user_story=None,
        problem_statement=None,
        business_value=None,
        scope_in_count=1,
        validation_count=0,
        size="S",
        wip_tag=None,
        ac_count=0,
    )
    score, components = calculate_score_from_data(dor=dor, risks=[])
    # all required pass for docs, so dor.passed is True
    assert dor.passed is True
    optional_failed = sum(
        1 for c in components if c.delta == -DEFAULT_CONFIG.penalty_optional
    )
    assert optional_failed > 0
    # no required-failed in this scenario
    assert all(c.delta == -DEFAULT_CONFIG.penalty_optional for c in components)
    assert score == 100 - optional_failed * DEFAULT_CONFIG.penalty_optional


def test_risks_subtract_by_severity():
    dor = _all_passed_dor()
    risks = [
        TaskRisk(
            kind=RiskKind.security,
            severity=RiskSeverity.high,
            description="d",
            mitigation="m",
        ),
        TaskRisk(
            kind=RiskKind.unknown_unknowns,
            severity=RiskSeverity.medium,
            description="d",
            mitigation="m",
        ),
        TaskRisk(
            kind=RiskKind.large_scope,
            severity=RiskSeverity.low,
            description="d",
            mitigation="m",
        ),
    ]
    score, components = calculate_score_from_data(dor=dor, risks=risks)
    expected = 100 - (
        DEFAULT_CONFIG.risk_penalties[RiskSeverity.high]
        + DEFAULT_CONFIG.risk_penalties[RiskSeverity.medium]
        + DEFAULT_CONFIG.risk_penalties[RiskSeverity.low]
    )
    assert score == expected
    assert len(components) == 3
    assert all(c.field == "risks" for c in components)


def test_score_clamped_to_zero_minimum():
    dor = _empty_dor()
    big_risks = [
        TaskRisk(
            kind=RiskKind.security,
            severity=RiskSeverity.high,
            description="d",
            mitigation="m",
        )
    ] * 100
    score, _ = calculate_score_from_data(dor=dor, risks=big_risks)
    assert score == 0


def test_score_clamped_to_base_maximum():
    """Negative penalties (in custom config) shouldn't push above base."""
    config = ReadinessConfig(penalty_required=-5, penalty_optional=0)
    dor = _empty_dor()
    score, _ = calculate_score_from_data(dor=dor, risks=[], config=config)
    assert score == 100


def test_score_clamped_to_100_even_when_config_base_exceeds_100():
    """ReadinessReport.score has le=100 — see review fix #2.4.

    Without the upper clamp at 100 a misconfigured ``base=200`` would
    produce score=200 and crash Pydantic validation downstream.
    """
    config = ReadinessConfig(base=200, penalty_required=0, penalty_optional=0)
    dor = _all_passed_dor()
    score, _ = calculate_score_from_data(dor=dor, risks=[], config=config)
    assert score == 100


def test_components_describe_each_failure():
    dor = _empty_dor()
    _, components = calculate_score_from_data(
        dor=dor,
        risks=[
            TaskRisk(
                kind=RiskKind.external_dependency,
                severity=RiskSeverity.medium,
                description="d",
                mitigation="m",
            )
        ],
    )
    failed_keys = {c.field for c in components if c.field != "risks"}
    assert failed_keys == set(dor.required)
    assert any("DoR required" in c.reason for c in components)
    assert any("external_dependency" in c.reason for c in components)


def test_score_component_to_dict_shape():
    sc = ScoreComponent(field="x", delta=-3, reason="r")
    assert sc.to_dict() == {"field": "x", "delta": -3, "reason": "r"}


def test_custom_config_overrides_defaults():
    dor = _empty_dor()
    cfg = ReadinessConfig(
        base=50,
        penalty_required=1,
        penalty_optional=0,
        risk_penalties={
            RiskSeverity.low: 0,
            RiskSeverity.medium: 0,
            RiskSeverity.high: 0,
        },
    )
    score, _ = calculate_score_from_data(dor=dor, risks=[], config=cfg)
    assert score == 50 - len(dor.required)


def test_unknown_risk_severity_does_not_subtract():
    """Defensive: a missing severity in the penalty dict is treated as 0."""
    dor = _all_passed_dor()
    cfg = ReadinessConfig(risk_penalties={RiskSeverity.high: 7})
    risks = [
        TaskRisk(
            kind=RiskKind.other,
            severity=RiskSeverity.low,
            description="d",
            mitigation="m",
        )
    ]
    score, components = calculate_score_from_data(dor=dor, risks=risks, config=cfg)
    assert score == 100
    assert components == []


# --- async integration with repository ---


async def _make_task_with_full_dor(db: aiosqlite.Connection) -> int:
    payload = TaskCreate(
        title="t",
        user_story="us",
        problem_statement="ps",
        business_value="bv",
        scope_in=["a"],
        validation_commands=["pytest"],
        size=TaskSize.S,
        wip_tag=WipTag.feature_work,
    )
    task_id = await repo.create_task_full(db, payload, status="draft")
    await repo.add_acceptance_criterion(db, task_id, _ac(1))
    await db.commit()
    return task_id


async def test_calculate_readiness_perfect_task(db: aiosqlite.Connection):
    task_id = await _make_task_with_full_dor(db)
    report = await calculate_readiness(db, task_id)
    assert report.score == 100
    assert report.dor_passed is True
    assert report.recommendations == []
    assert report.explain is None


async def test_calculate_readiness_with_explain_returns_components(
    db: aiosqlite.Connection,
):
    payload = TaskCreate(title="t")
    task_id = await repo.create_task_full(db, payload, status="draft")
    await db.commit()

    report = await calculate_readiness(db, task_id, explain=True)
    assert report.dor_passed is False
    assert report.explain is not None
    assert all({"field", "delta", "reason"} <= e.keys() for e in report.explain)
    assert sum(e["delta"] for e in report.explain) == report.score - 100


async def test_calculate_readiness_includes_persisted_risks(db: aiosqlite.Connection):
    task_id = await _make_task_with_full_dor(db)
    await repo.update_task_structured(
        db,
        task_id,
        TaskRefine(
            risks=[
                TaskRisk(
                    kind=RiskKind.breaking_change,
                    severity=RiskSeverity.high,
                    description="api change",
                    mitigation="versioned route",
                )
            ]
        ),
    )
    await db.commit()
    report = await calculate_readiness(db, task_id)
    assert report.dor_passed is True
    assert len(report.risks) == 1
    assert report.risks[0].kind == RiskKind.breaking_change
    assert report.score == 100 - DEFAULT_CONFIG.risk_penalties[RiskSeverity.high]


async def test_calculate_readiness_drops_malformed_risks(db: aiosqlite.Connection):
    task_id = await _make_task_with_full_dor(db)
    # Bypass Pydantic and write raw garbage into risks column.
    await repo.update_task(
        db,
        task_id,
        risks='[{"kind": "security", "severity": "high", "description": "d", "mitigation": "m"}, '
        '{"kind": "not-a-real-kind"}, "string", 42]',
    )
    await db.commit()

    report = await calculate_readiness(db, task_id)
    # Only the first risk validates cleanly.
    assert len(report.risks) == 1
    assert report.risks[0].kind == RiskKind.security


async def test_calculate_readiness_dor_checks_echoed_in_report(
    db: aiosqlite.Connection,
):
    task_id = await _make_task_with_full_dor(db)
    report = await calculate_readiness(db, task_id)
    keys = [c.key for c in report.dor_checks]
    assert "has_user_story" in keys
    assert all(isinstance(c, DoRCheckItem) for c in report.dor_checks)
