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
    # expectation_source (#595) defaults to None — "not stated", which the
    # payload above deliberately omits.
    assert ac.model_dump() == {
        **payload,
        "verifiable_by": ACVerifiableBy.test,
        "expectation_source": None,
    }


# --- TaskRisk ---


def test_task_risk_accepts_an_honest_absence_of_mitigation():
    # #610 inverted this deliberately, so the test is rewritten rather than
    # deleted: requiring a mitigation left an author who saw a risk without a
    # remedy only two moves — invent filler, or say nothing — and a risk that
    # failed validation was DROPPED before scoring, so the least-handled risks
    # vanished. An empty mitigation is now a statement: seen, not yet solved.
    risk = TaskRisk(
        kind=RiskKind.security,
        severity=RiskSeverity.high,
        description="injection",
        mitigation="",
    )
    assert risk.mitigation == ""


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


def test_task_refine_accepts_title():
    refine = TaskRefine(title="Renamed task")
    dumped = refine.model_dump(exclude_unset=True)
    assert dumped == {"title": "Renamed task"}


def test_task_refine_title_omitted_means_untouched():
    refine = TaskRefine()
    dumped = refine.model_dump(exclude_unset=True)
    assert "title" not in dumped


def test_task_refine_title_max_length():
    with pytest.raises(ValidationError):
        TaskRefine(title="a" * 501)


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


def test_task_refine_rejects_too_many_acs():
    from hub.models import MAX_ACCEPTANCE_CRITERIA

    acs = [
        AcceptanceCriterion(
            id=f"AC-{i}",
            given="g",
            when="w",
            then="t",
            verifiable_by=ACVerifiableBy.test,
        )
        for i in range(MAX_ACCEPTANCE_CRITERIA + 1)
    ]
    with pytest.raises(ValidationError, match="too many acceptance criteria"):
        TaskRefine(acceptance_criteria=acs)


def test_task_refine_rejects_too_many_risks():
    from hub.models import MAX_RISKS

    risks = [
        TaskRisk(
            kind=RiskKind.security,
            severity=RiskSeverity.low,
            description=f"risk {i}",
            mitigation="handle it",
        )
        for i in range(MAX_RISKS + 1)
    ]
    with pytest.raises(ValidationError, match="too many risks"):
        TaskRefine(risks=risks)


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


# --- #553: an unresolved finding without its explanation is empty ---


def test_unresolved_finding_keeps_its_explanation() -> None:
    from hub.models import MachineUnresolvedFinding

    f = MachineUnresolvedFinding(title="voices diverged", why="cheap said no")
    assert f.why == "cheap said no"


def test_unresolved_finding_with_no_explanation_still_loads() -> None:
    """Reports written before the field existed carry an empty why (#549)."""
    from hub.models import MachineUnresolvedFinding

    assert MachineUnresolvedFinding(title="old report").why == ""


def test_reason_instead_of_why_is_refused_and_names_the_right_field() -> None:
    """Reproduced on machine_review#34: both unresolved findings were stored
    with an empty explanation, which is the entire content of an unresolved
    finding. The two field names sit on adjacent lines of the same docstring —
    findings_rejected takes `reason` — so they get swapped.

    Plain extra="forbid" would only say `reason` is not permitted, leaving the
    caller to guess: that trades a silent loss for a loud puzzle. The message
    has to name `why`.
    """
    import pytest
    from pydantic import ValidationError

    from hub.models import MachineUnresolvedFinding

    with pytest.raises(ValidationError) as excinfo:
        MachineUnresolvedFinding(title="t", reason="the explanation that was lost")

    message = excinfo.value.errors()[0]["msg"]
    assert "why" in message
    assert "reason" in message


def test_unknown_keys_on_findings_are_refused_not_dropped() -> None:
    """Harness output carries dimensions/duplicates/failure_scenario. Mapping
    those onto the stored shape is the submitter's job; silently dropping them
    is how a field ends up empty with nobody noticing."""
    import pytest
    from pydantic import ValidationError

    from hub.models import MachineFinding, MachineRejectedFinding

    with pytest.raises(ValidationError):
        MachineFinding(title="t", severity="high", dimensions=["correctness"])
    with pytest.raises(ValidationError):
        MachineRejectedFinding(title="t", why="wrong field for this model")


def test_unresolved_refuses_unknown_keys_beyond_the_reason_confusion() -> None:
    """The targeted validator fires before extra="forbid" is ever consulted,
    so testing only the reason/why swap leaves the model's own guard uncovered:
    removing it survived the suite until this test existed. Any other stray key
    would have gone back to being dropped in silence.
    """
    import pytest
    from pydantic import ValidationError

    from hub.models import MachineUnresolvedFinding

    with pytest.raises(ValidationError):
        MachineUnresolvedFinding(title="t", why="ok", severity="high")


def test_seeded_skill_names_every_unresolved_field_the_model_has() -> None:
    """The skill text is the contract agents actually read (#553).

    #549 updated the tool docstring and the docs but not this constant, so an
    agent taking the contract from the hub kept submitting without honest
    incompleteness — reproducing the defect the change had just fixed. Found by
    an independent review of the task statement, not by me.

    Derived from the model rather than hard-coded, so a field added later is
    caught here instead of drifting the same way again.
    """
    from hub.db import MACHINE_REVIEW_CYCLE_SKILL
    from hub.models import MachineReviewSubmit, MachineUnresolvedFinding

    for field in MachineUnresolvedFinding.model_fields:
        assert field in MACHINE_REVIEW_CYCLE_SKILL, (
            f"unresolved field {field!r} missing from the seeded skill"
        )
    for field in ("incomplete", "unresolved", "lost_dimensions"):
        assert field in MachineReviewSubmit.model_fields, (
            f"{field} is expected on the submit model"
        )
        assert field in MACHINE_REVIEW_CYCLE_SKILL, (
            f"{field!r} missing from the seeded skill agents read"
        )
