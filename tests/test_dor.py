from __future__ import annotations

import aiosqlite
import pytest

from hub import repository as repo
from hub.models import (
    ACVerifiableBy,
    AcceptanceCriterion,
    ClassOfService,
    TaskCreate,
    TaskSize,
    WipTag,
    WorkType,
)
from hub.services.dor import (
    DOR_CHECK_KEYS,
    DOR_REQUIRED_BY_WORK_TYPE,
    DoREvaluation,
    evaluate_dor,
    evaluate_from_data,
)


# --- Sanity ---


def test_dor_check_keys_match_required_table_universe():
    """Every required key must be a known check key — no typos."""
    all_required: set[str] = set()
    for required in DOR_REQUIRED_BY_WORK_TYPE.values():
        all_required.update(required)
    assert all_required <= set(DOR_CHECK_KEYS)


def test_every_work_type_has_a_profile():
    for wt in WorkType:
        assert wt.value in DOR_REQUIRED_BY_WORK_TYPE


# --- evaluate_from_data: per-check behavior ---


def _empty_kwargs(**overrides):
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


@pytest.mark.parametrize(
    "field, value, key",
    [
        ("user_story", "as a user, I want X so that Y", "has_user_story"),
        ("problem_statement", "p", "has_problem_statement"),
        ("business_value", "b", "has_business_value"),
    ],
)
def test_string_fields_pass_when_filled(field: str, value: str, key: str):
    result = evaluate_from_data(**_empty_kwargs(**{field: value}))
    check = next(c for c in result.checks if c.key == key)
    assert check.passed is True


@pytest.mark.parametrize("value", [None, "", "   "])
def test_string_fields_fail_when_blank_or_whitespace(value):
    result = evaluate_from_data(**_empty_kwargs(user_story=value))
    check = next(c for c in result.checks if c.key == "has_user_story")
    assert check.passed is False


def test_count_based_checks():
    result = evaluate_from_data(
        **_empty_kwargs(scope_in_count=2, validation_count=1, ac_count=3)
    )
    by_key = {c.key: c for c in result.checks}
    assert by_key["has_scope_in"].passed is True
    assert "2" in by_key["has_scope_in"].detail
    assert by_key["has_validation_commands"].passed is True
    assert by_key["has_acceptance_criteria"].passed is True
    assert "3" in by_key["has_acceptance_criteria"].detail


def test_size_and_wip_tag_pass_when_set():
    result = evaluate_from_data(**_empty_kwargs(size="M", wip_tag="feature_work"))
    by_key = {c.key: c for c in result.checks}
    assert by_key["has_size"].passed is True
    assert by_key["has_wip_tag"].passed is True


def test_checks_returned_in_stable_order():
    result = evaluate_from_data(**_empty_kwargs())
    assert [c.key for c in result.checks] == list(DOR_CHECK_KEYS)


# --- passed / missing_required logic ---


def test_feature_with_nothing_filled_fails_dor():
    result = evaluate_from_data(**_empty_kwargs())
    assert result.passed is False
    assert result.missing_required == DOR_REQUIRED_BY_WORK_TYPE[WorkType.feature.value]


def test_feature_fully_filled_passes_dor():
    result = evaluate_from_data(
        **_empty_kwargs(
            user_story="us",
            problem_statement="ps",
            business_value="bv",
            scope_in_count=1,
            validation_count=1,
            size="S",
            wip_tag="feature_work",
            ac_count=1,
        )
    )
    assert result.passed is True
    assert result.missing_required == frozenset()


def test_chore_does_not_require_user_story_or_acs():
    """Chore profile is intentionally minimal."""
    result = evaluate_from_data(
        **_empty_kwargs(
            work_type=WorkType.chore.value,
            scope_in_count=1,
            validation_count=1,
            size="XS",
        )
    )
    assert result.passed is True
    # has_user_story is reported as failed but does not block chore
    by_key = {c.key: c for c in result.checks}
    assert by_key["has_user_story"].passed is False


def test_spike_requires_problem_statement_size_and_acceptance_criteria():
    """Spike must declare a completion criterion (AC) — see review fix #1.3."""
    # Without AC the spike is not ready: no way to know when it ends.
    result_no_ac = evaluate_from_data(
        **_empty_kwargs(
            work_type=WorkType.spike.value,
            problem_statement="explore lib X",
            size="S",
        )
    )
    assert result_no_ac.passed is False
    assert "has_acceptance_criteria" in result_no_ac.missing_required

    # With AC it passes.
    result_ok = evaluate_from_data(
        **_empty_kwargs(
            work_type=WorkType.spike.value,
            problem_statement="explore lib X",
            size="S",
            ac_count=1,
        )
    )
    assert result_ok.passed is True


def test_incident_requires_acceptance_criteria_and_does_not_require_size():
    """Incident: explicit "fixed when" criterion is mandatory — see #1.1.

    Incidents still skip size on purpose (urgency over estimation).
    """
    # Without AC: not ready.
    result_no_ac = evaluate_from_data(
        **_empty_kwargs(
            work_type=WorkType.incident.value,
            problem_statement="API 5xx spike",
            validation_count=1,
        )
    )
    assert result_no_ac.passed is False
    assert "has_acceptance_criteria" in result_no_ac.missing_required

    # With AC + problem_statement + validation: ready, and size is still optional.
    result_ok = evaluate_from_data(
        **_empty_kwargs(
            work_type=WorkType.incident.value,
            problem_statement="API 5xx spike",
            validation_count=1,
            ac_count=1,
        )
    )
    assert result_ok.passed is True
    assert "has_size" not in result_ok.missing_required


def test_bug_requires_business_value_and_problem_statement():
    """Bug profile now requires business_value (see review fix #1.2).

    Without it, a $1M-customer P1 cannot be told apart from cosmetic noise.
    """
    result = evaluate_from_data(
        **_empty_kwargs(
            work_type=WorkType.bug.value,
            scope_in_count=1,
            validation_count=1,
            size="S",
            wip_tag="bugfix",
            ac_count=1,
        )
    )
    assert result.passed is False
    assert result.missing_required == frozenset(
        {"has_problem_statement", "has_business_value"}
    )


def test_refactor_requires_wip_tag():
    """Refactor must carry wip_tag for capacity tracking — see review fix #1.5."""
    # Filled everything except wip_tag → still fails because of #1.5.
    result = evaluate_from_data(
        **_empty_kwargs(
            work_type=WorkType.refactor.value,
            problem_statement="ps",
            scope_in_count=1,
            validation_count=1,
            size="S",
            ac_count=1,
        )
    )
    assert result.passed is False
    assert result.missing_required == frozenset({"has_wip_tag"})

    # Add wip_tag → passes.
    result_ok = evaluate_from_data(
        **_empty_kwargs(
            work_type=WorkType.refactor.value,
            problem_statement="ps",
            scope_in_count=1,
            validation_count=1,
            size="S",
            wip_tag="tech_debt",
            ac_count=1,
        )
    )
    assert result_ok.passed is True


def test_unknown_work_type_falls_back_to_feature_profile():
    result = evaluate_from_data(**_empty_kwargs(work_type="not-a-real-type"))
    assert result.passed is False
    assert result.missing_required == DOR_REQUIRED_BY_WORK_TYPE[WorkType.feature.value]


def test_none_work_type_falls_back_to_feature_profile():
    result = evaluate_from_data(**_empty_kwargs(work_type=None))
    assert result.passed is False
    assert result.missing_required == DOR_REQUIRED_BY_WORK_TYPE[WorkType.feature.value]


def test_dor_evaluation_passed_property_independent_of_optional_checks():
    """Optional checks failing should not flip passed=True."""
    result = evaluate_from_data(
        **_empty_kwargs(
            work_type=WorkType.docs.value,
            scope_in_count=1,
            size="S",
        )
    )
    assert isinstance(result, DoREvaluation)
    assert result.passed is True
    failing = {c.key for c in result.checks if not c.passed}
    # plenty of optional checks fail, but docs profile only requires 2
    assert failing - result.required != set()


# --- async evaluate_dor: integration with repository ---


async def _make_task(db: aiosqlite.Connection, **overrides) -> int:
    payload = TaskCreate(title="t", **overrides)
    task_id = await repo.create_task_full(db, payload, status="draft")
    await db.commit()
    return task_id


def _ac(idx: int = 1) -> AcceptanceCriterion:
    return AcceptanceCriterion(
        id=f"AC-{idx}",
        given="g",
        when="w",
        then="t",
        verifiable_by=ACVerifiableBy.test,
    )


async def test_evaluate_dor_unknown_task_raises(db: aiosqlite.Connection):
    with pytest.raises(ValueError, match="not found"):
        await evaluate_dor(db, 99999)


async def test_evaluate_dor_minimal_task_fails(db: aiosqlite.Connection):
    task_id = await _make_task(db)
    result = await evaluate_dor(db, task_id)
    assert result.passed is False
    assert "has_user_story" in result.missing_required
    assert "has_acceptance_criteria" in result.missing_required


async def test_evaluate_dor_full_feature_passes(db: aiosqlite.Connection):
    task_id = await _make_task(
        db,
        user_story="us",
        problem_statement="ps",
        business_value="bv",
        scope_in=["a"],
        validation_commands=["pytest"],
        size=TaskSize.M,
        wip_tag=WipTag.feature_work,
        class_of_service=ClassOfService.standard,
    )
    await repo.add_acceptance_criterion(db, task_id, _ac(1))
    await db.commit()

    result = await evaluate_dor(db, task_id)
    assert result.passed is True
    assert result.missing_required == frozenset()


async def test_evaluate_dor_counts_acs_from_table(db: aiosqlite.Connection):
    task_id = await _make_task(db)
    await repo.add_acceptance_criterion(db, task_id, _ac(1))
    await repo.add_acceptance_criterion(db, task_id, _ac(2))
    await db.commit()

    result = await evaluate_dor(db, task_id)
    by_key = {c.key: c for c in result.checks}
    assert by_key["has_acceptance_criteria"].passed is True
    assert "2" in by_key["has_acceptance_criteria"].detail
