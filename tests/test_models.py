from __future__ import annotations

import pytest
from pydantic import ValidationError

from hub.models import (
    ACVerifiableBy,
    AcceptanceCriterion,
    ClassOfService,
    DoRCheckItem,
    ReadinessReport,
    Recommendation,
    RiskKind,
    RiskSeverity,
    TaskApprove,
    TaskCreate,
    TaskRefine,
    TaskRisk,
    TaskSize,
    TaskView,
    WipTag,
    WorkType,
)


# --- AcceptanceCriterion ---


def test_acceptance_criterion_minimal_valid():
    ac = AcceptanceCriterion(
        id="AC-1",
        given="user is logged in",
        when="they open settings",
        then="theme toggle is visible",
        verifiable_by=ACVerifiableBy.ui_check,
    )
    assert ac.id == "AC-1"
    assert ac.test_ref is None


@pytest.mark.parametrize("bad_id", ["AC1", "ac-1", "AC-", "AC-abc", "1", "AC-1 "])
def test_acceptance_criterion_id_pattern_rejects_invalid(bad_id: str):
    with pytest.raises(ValidationError):
        AcceptanceCriterion(
            id=bad_id,
            given="g",
            when="w",
            then="t",
            verifiable_by=ACVerifiableBy.test,
        )


def test_acceptance_criterion_rejects_empty_clauses():
    with pytest.raises(ValidationError):
        AcceptanceCriterion(
            id="AC-1", given="", when="w", then="t", verifiable_by=ACVerifiableBy.test
        )


def test_acceptance_criterion_serializes_round_trip():
    payload = {
        "id": "AC-12",
        "given": "g",
        "when": "w",
        "then": "t",
        "verifiable_by": "test",
        "test_ref": "tests/test_x.py::test_y",
    }
    ac = AcceptanceCriterion.model_validate(payload)
    assert ac.model_dump() == {**payload, "verifiable_by": ACVerifiableBy.test}


# --- TaskRisk ---


def test_task_risk_requires_mitigation():
    with pytest.raises(ValidationError):
        TaskRisk(
            kind=RiskKind.security,
            severity=RiskSeverity.high,
            description="injection",
            mitigation="",
        )


def test_task_risk_valid():
    risk = TaskRisk(
        kind=RiskKind.external_dependency,
        severity=RiskSeverity.medium,
        description="Vast API may be unavailable",
        mitigation="Retry with backoff",
    )
    assert risk.severity == RiskSeverity.medium


# --- TaskCreate extensions ---


def test_task_create_defaults_for_structured_fields():
    tc = TaskCreate(title="Add toggle")
    assert tc.work_type == WorkType.feature
    assert tc.class_of_service == ClassOfService.standard
    assert tc.size is None
    assert tc.wip_tag is None
    assert tc.scope_in == []
    assert tc.scope_out == []
    assert tc.user_story == ""
    assert tc.constraints == []


def test_task_create_accepts_full_structured_payload():
    tc = TaskCreate(
        title="x",
        work_type=WorkType.bug,
        class_of_service=ClassOfService.expedite,
        size=TaskSize.M,
        wip_tag=WipTag.bugfix,
        user_story="as a user, I want X so that Y",
        scope_in=["a", "b"],
        validation_commands=["pytest -q"],
    )
    assert tc.work_type == WorkType.bug
    assert tc.size == TaskSize.M
    assert tc.scope_in == ["a", "b"]


def test_task_create_rejects_too_many_scope_items():
    with pytest.raises(ValidationError):
        TaskCreate(title="x", scope_in=[f"item-{i}" for i in range(21)])


def test_task_create_rejects_too_many_constraints():
    with pytest.raises(ValidationError):
        TaskCreate(title="x", constraints=[f"c-{i}" for i in range(11)])


def test_task_create_review_checklist_default_empty():
    tc = TaskCreate(title="x")
    assert tc.review_checklist == []


def test_task_create_accepts_review_checklist():
    tc = TaskCreate(title="x", review_checklist=["check migration", "verify rollback"])
    assert tc.review_checklist == ["check migration", "verify rollback"]


def test_task_create_rejects_too_many_review_checklist_items():
    with pytest.raises(ValidationError):
        TaskCreate(title="x", review_checklist=[f"c-{i}" for i in range(11)])


def test_task_create_backward_compatible_without_new_fields():
    """Existing clients sending only the legacy payload still validate."""
    tc = TaskCreate(title="x", description="d", priority="high")
    assert tc.work_type == WorkType.feature
    assert tc.class_of_service == ClassOfService.standard


def test_task_create_accepts_human_owner_and_reviewer():
    tc = TaskCreate(title="x", human_owner="alice", human_reviewer="bob")
    assert tc.human_owner == "alice"
    assert tc.human_reviewer == "bob"


def test_task_create_human_owner_defaults_to_empty():
    tc = TaskCreate(title="x")
    assert tc.human_owner == ""
    assert tc.human_reviewer == ""


def test_task_create_human_owner_max_length():
    with pytest.raises(ValidationError):
        TaskCreate(title="x", human_owner="a" * 101)


# --- TaskRefine ---


def test_task_refine_all_fields_optional():
    refine = TaskRefine()
    dumped = refine.model_dump(exclude_unset=True)
    assert dumped == {}


def test_task_refine_partial_update():
    refine = TaskRefine(scope_in=["x"], size=TaskSize.L)
    dumped = refine.model_dump(exclude_unset=True)
    assert dumped == {"scope_in": ["x"], "size": TaskSize.L}


def test_task_refine_validates_lists_and_enums():
    with pytest.raises(ValidationError):
        TaskRefine(work_type="not-a-real-work-type")
    with pytest.raises(ValidationError):
        TaskRefine(scope_in=[f"x-{i}" for i in range(21)])


def test_task_refine_accepts_human_owner_and_reviewer():
    refine = TaskRefine(human_owner="alice", human_reviewer="bob")
    dumped = refine.model_dump(exclude_unset=True)
    assert dumped == {"human_owner": "alice", "human_reviewer": "bob"}


def test_task_refine_human_owner_none_means_untouched():
    refine = TaskRefine()
    dumped = refine.model_dump(exclude_unset=True)
    assert "human_owner" not in dumped
    assert "human_reviewer" not in dumped


def test_task_refine_human_owner_max_length():
    with pytest.raises(ValidationError):
        TaskRefine(human_owner="a" * 101)


def test_task_refine_review_checklist_omitted_means_untouched():
    refine = TaskRefine()
    dumped = refine.model_dump(exclude_unset=True)
    assert "review_checklist" not in dumped


def test_task_refine_review_checklist_replace():
    refine = TaskRefine(review_checklist=["x", "y"])
    dumped = refine.model_dump(exclude_unset=True)
    assert dumped == {"review_checklist": ["x", "y"]}


def test_task_refine_review_checklist_explicit_clear():
    refine = TaskRefine(review_checklist=[])
    dumped = refine.model_dump(exclude_unset=True)
    assert dumped == {"review_checklist": []}


def test_task_refine_rejects_too_many_review_checklist_items():
    with pytest.raises(ValidationError):
        TaskRefine(review_checklist=[f"x-{i}" for i in range(11)])


def test_task_refine_accepts_acs_and_risks():
    refine = TaskRefine(
        acceptance_criteria=[
            AcceptanceCriterion(
                id="AC-1",
                given="g",
                when="w",
                then="t",
                verifiable_by=ACVerifiableBy.test,
            )
        ],
        risks=[
            TaskRisk(
                kind=RiskKind.large_scope,
                severity=RiskSeverity.high,
                description="too big",
                mitigation="split",
            )
        ],
    )
    assert refine.acceptance_criteria[0].id == "AC-1"
    assert refine.risks[0].kind == RiskKind.large_scope


# --- TaskApprove force flag ---


def test_task_approve_force_default_false():
    ap = TaskApprove()
    assert ap.force is False
    assert ap.run is False


def test_task_approve_force_true():
    ap = TaskApprove(force=True, run=True, comment="DoR override: hotfix")
    assert ap.force is True


# --- ReadinessReport ---


def test_readiness_report_requires_score_in_range():
    with pytest.raises(ValidationError):
        ReadinessReport(score=101, dor_passed=False)
    with pytest.raises(ValidationError):
        ReadinessReport(score=-1, dor_passed=False)


def test_readiness_report_minimal_defaults():
    report = ReadinessReport(score=42, dor_passed=False)
    assert report.dor_checks == []
    assert report.risks == []
    assert report.recommendations == []
    assert report.explain is None


def test_readiness_report_full_payload():
    report = ReadinessReport(
        score=88,
        dor_passed=True,
        dor_checks=[DoRCheckItem(key="has_user_story", passed=True, detail="ok")],
        risks=[
            TaskRisk(
                kind=RiskKind.unknown_unknowns,
                severity=RiskSeverity.low,
                description="d",
                mitigation="m",
            )
        ],
        recommendations=[
            Recommendation(
                field="acceptance_criteria",
                severity="high",
                message="add at least one AC",
                expected_score_delta=15,
                estimated_minutes=5,
            )
        ],
    )
    assert report.dor_passed is True
    assert report.recommendations[0].severity == "high"


def test_recommendation_rejects_unknown_severity():
    with pytest.raises(ValidationError):
        Recommendation(
            field="x",
            severity="urgent",
            message="m",
            expected_score_delta=0,
            estimated_minutes=0,
        )


# --- TaskView extensions ---


def _minimal_task_view_payload(**overrides):
    base = {
        "id": 1,
        "title": "t",
        "description": "",
        "status": "open",
        "runtime": "auto",
        "created_at": "2026-04-17T00:00:00",
        "updated_at": "2026-04-17T00:00:00",
    }
    base.update(overrides)
    return base


def test_task_view_defaults_for_structured_fields():
    view = TaskView(**_minimal_task_view_payload())
    assert view.work_type is None
    assert view.scope_in == []
    assert view.review_checklist == []
    assert view.risks == []
    assert view.acceptance_criteria is None
    assert view.readiness_score is None
    assert view.dor_passed is None
    assert view.ready_at is None
    assert view.started_at is None
    assert view.completed_at is None


def test_task_view_accepts_review_checklist():
    view = TaskView(
        **_minimal_task_view_payload(review_checklist=["check A", "check B"])
    )
    assert view.review_checklist == ["check A", "check B"]


def test_task_view_defaults_for_human_owner_reviewer():
    view = TaskView(**_minimal_task_view_payload())
    assert view.human_owner == ""
    assert view.human_reviewer == ""


def test_task_view_accepts_human_owner_reviewer():
    view = TaskView(
        **_minimal_task_view_payload(human_owner="alice", human_reviewer="bob")
    )
    assert view.human_owner == "alice"
    assert view.human_reviewer == "bob"


def test_task_view_accepts_structured_payload():
    view = TaskView(
        **_minimal_task_view_payload(
            work_type="bug",
            size="L",
            scope_in=["a"],
            risks=[
                TaskRisk(
                    kind=RiskKind.security,
                    severity=RiskSeverity.high,
                    description="d",
                    mitigation="m",
                )
            ],
            readiness_score=72,
            dor_passed=True,
        )
    )
    assert view.work_type == WorkType.bug
    assert view.readiness_score == 72
    assert view.dor_passed is True
    assert view.risks[0].kind == RiskKind.security
