from __future__ import annotations

import aiosqlite
import pytest

from hub import repository as repo
from hub.db import deserialize_str_list
from hub.services.risk_class import derive_risk_class
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
        affected_areas_count=0,
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
            affected_areas_count=1,
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
            affected_areas_count=1,
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
            affected_areas_count=1,
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
            affected_areas_count=1,
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
            affected_areas_count=1,
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
        affected_areas=["hub/services/dor.py"],
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


# --- Areas are part of readiness for code work (#842) ------------------------
#
# A statement could be complete by all eight checks and still not say what it
# touches. Four mechanisms read that field and all four degraded quietly: the
# risk class was never computed (#582), the review profile bought the
# expensive harness on "unknown" (#807/#820), the statement-freshness check
# had nothing to compare, and the submit-time area check could not tell
# whether the work strayed. Measured 21.08.2026: 86 of 96 spike-bo tasks had
# no class for exactly this reason.


def test_code_work_needs_affected_areas():
    # AC-1 (#842): complete in every other respect, and still not ready —
    # with that one check named, not a bare "not ready".
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
            affected_areas_count=0,
        )
    )

    assert result.passed is False
    assert result.missing_required == frozenset({"has_affected_areas"})


async def test_declared_areas_pass_and_yield_a_class(db: aiosqlite.Connection):
    # AC-2 (#842): the point of the requirement is what it unlocks — with
    # areas declared the task both passes and finally HAS a risk class,
    # which is what every downstream mechanism was waiting for.
    task_id = await _make_task(
        db,
        user_story="us",
        problem_statement="ps",
        business_value="bv",
        scope_in=["a"],
        validation_commands=["pytest"],
        size=TaskSize.M,
        wip_tag=WipTag.feature_work,
        affected_areas=["hub/services/dor.py"],
    )
    await repo.add_acceptance_criterion(db, task_id, _ac(1))
    await db.commit()

    result = await evaluate_dor(db, task_id)

    assert result.passed is True
    assert result.missing_required == frozenset()
    # And the point of the requirement: the declared areas are enough to
    # derive a class. (The recompute itself belongs to the create/refine
    # paths, #582 — here we check that the input it needs now exists.)
    row = dict(await repo.get_task(db, task_id))
    derived, reasons = derive_risk_class(deserialize_str_list(row["affected_areas"]))
    assert derived is not None, "declared areas must yield a computed class"
    assert reasons


@pytest.mark.parametrize(
    "work_type",
    [WorkType.docs.value, WorkType.chore.value, WorkType.spike.value],
)
def test_non_code_work_types_stay_unchanged(work_type: str):
    # AC-3 (#842): docs have nothing to declare, a spike does not yet know.
    # Requiring areas there would buy nothing and cost a gate.
    assert "has_affected_areas" not in DOR_REQUIRED_BY_WORK_TYPE[work_type]


@pytest.mark.parametrize(
    "work_type, wip_tag",
    [
        (WorkType.feature.value, "feature_work"),
        (WorkType.bug.value, "bugfix"),
        (WorkType.refactor.value, "tech_debt"),
        (WorkType.incident.value, "support"),
    ],
)
def test_every_code_work_type_requires_areas(work_type: str, wip_tag: str):
    # AC-4 (#842): the rule is about changing code, not about the word
    # "feature" — a bug fix touches the same files and hides the same risk.
    assert "has_affected_areas" in DOR_REQUIRED_BY_WORK_TYPE[work_type]

    result = evaluate_from_data(
        **_empty_kwargs(
            work_type=work_type,
            user_story="us",
            problem_statement="ps",
            business_value="bv",
            scope_in_count=1,
            validation_count=1,
            size="S",
            wip_tag=wip_tag,
            ac_count=1,
            affected_areas_count=0,
        )
    )

    assert "has_affected_areas" in result.missing_required


# --- #1456: пути постановки должны существовать или создаваться задачей ---

_BASE_TREE = {
    "hub/services/dor.py",
    "tests/test_dor.py",
    "tests/fixtures/home_basic/config",
}
_LIVE_1392 = "tool --ssh-dir tests/fixtures/home_basic/.ssh"


def _paths_check(**overrides):
    from hub.services.dor import check_statement_paths

    kwargs = dict(
        validation_commands=[],
        test_refs=[],
        affected_areas=["hub/services/dor.py"],
        tree=set(_BASE_TREE),
        mode="warn",
    )
    kwargs.update(overrides)
    return check_statement_paths(**kwargs)


def _paths_item(result):
    evaluation = evaluate_from_data(
        **_empty_kwargs(
            work_type=WorkType.chore.value,
            scope_in_count=1,
            validation_count=1,
            size="S",
            statement_paths=result,
        )
    )
    item = next(c for c in evaluation.checks if c.key == "statement_paths_resolve")
    return evaluation, item


async def _task_in_project(
    db: aiosqlite.Connection, mode: str | None, *, workspace: str = "/tmp/ws"
) -> int:
    import json

    pid = await repo.create_project(
        db, slug="p1456", name="P1456", workspace_path=workspace
    )
    if mode is not None:
        await repo.update_project(
            db, pid, gate_policy=json.dumps({"statement_paths": mode})
        )
    task_id = await _make_task(
        db,
        work_type=WorkType.chore,
        validation_commands=[_LIVE_1392],
        affected_areas=["hub/services/dor.py"],
        scope_in=["a"],
        size=TaskSize.S,
    )
    await repo.update_task(db, task_id, project_id=pid)
    await db.commit()
    return task_id


def _serve_tree(monkeypatch, tree):
    from hub.integrations.registry import plugins

    async def files_at_ref(repo_path, ref):
        return None if tree is None else set(tree)

    monkeypatch.setattr(plugins.git_ops, "files_at_ref", files_at_ref)


async def test_statement_path_missing_from_base_and_areas_is_flagged(
    db: aiosqlite.Connection, monkeypatch
):
    # AC-1: #1392 — каталога нет на базе и он не покрыт областями.
    result = _paths_check(validation_commands=[_LIVE_1392], mode="require")
    assert result.checked is True
    [violation] = result.violations
    assert violation.path == "tests/fixtures/home_basic/.ssh"
    assert _LIVE_1392 in violation.source

    evaluation, item = _paths_item(result)
    assert item.passed is False
    assert "tests/fixtures/home_basic/.ssh" in item.detail
    assert "statement_paths_resolve" in evaluation.missing_required

    # warn: нарушение названо, но одобрению не мешает.
    warn_eval, warn_item = _paths_item(
        _paths_check(validation_commands=[_LIVE_1392], mode="warn")
    )
    assert warn_item.passed is False
    assert "tests/fixtures/home_basic/.ssh" in warn_item.detail
    assert "statement_paths_resolve" not in warn_eval.missing_required
    assert warn_eval.passed is True

    # Тот же случай через БД: режим берётся из политики проекта.
    _serve_tree(monkeypatch, _BASE_TREE)
    required = await evaluate_dor(db, await _task_in_project(db, "require"))
    assert "statement_paths_resolve" in required.missing_required


async def test_statement_path_violation_is_recorded_on_approve_in_warn(
    db: aiosqlite.Connection, monkeypatch
):
    from hub.models import TaskApprove
    from hub.services.lifecycle import approve_task

    _serve_tree(monkeypatch, _BASE_TREE)
    task_id = await _task_in_project(db, "warn")
    await approve_task(db, task_id, TaskApprove())
    updates = await repo.get_task_updates(db, task_id)
    alerts = [
        u["content"]
        for u in updates
        if "tests/fixtures/home_basic/.ssh" in u["content"]
    ]
    assert alerts, "warn must leave a record naming the path"


async def test_statement_path_violation_refuses_approve_in_require(
    db: aiosqlite.Connection, monkeypatch
):
    from fastapi import HTTPException

    from hub.models import TaskApprove
    from hub.services.lifecycle import approve_task

    _serve_tree(monkeypatch, _BASE_TREE)
    task_id = await _task_in_project(db, "require")
    with pytest.raises(HTTPException) as refused:
        await approve_task(db, task_id, TaskApprove())
    assert refused.value.status_code == 422
    detail = refused.value.detail
    assert "statement_paths_resolve" in detail["missing_required"]
    assert "tests/fixtures/home_basic/.ssh" in str(detail["recommendations"])


async def test_paths_to_be_created_or_existing_are_not_flagged(
    db: aiosqlite.Connection,
):
    # AC-2: test_ref в affected_areas; флаги, URL и абсолютный путь — не пути репо.
    result = _paths_check(
        validation_commands=[
            "uv run pytest -q tests/test_dor.py --maxfail=1 -k explain",
            "curl -fsS https://example.com/api/health.json",
            "uv run python /opt/tool/run.py --out /tmp/report.json",
            "uv run pytest tests/fixtures/home_basic",
            "python -m hub.cli --ssh-dir=tests/fixtures/home_basic",
        ],
        test_refs=[("AC-2", "tests/test_explain.py::x")],
        affected_areas=["hub/services/dor.py", "tests/test_explain.py"],
        mode="require",
    )
    assert result.checked is True
    assert result.violations == ()
    evaluation, item = _paths_item(result)
    assert item.passed is True
    assert "statement_paths_resolve" not in evaluation.missing_required


async def test_unreadable_base_is_not_checked_not_clean(
    db: aiosqlite.Connection, monkeypatch
):
    # AC-3: база не читается — «не проверено» с причиной, а не «пройдено».
    result = _paths_check(validation_commands=[_LIVE_1392], tree=None, mode="require")
    assert result.checked is False
    assert result.reason
    evaluation, item = _paths_item(result)
    assert item.passed is False
    assert "не проверено" in item.detail
    assert result.reason in item.detail
    # Неизвестность не блокирует одобрение: отказывает только найденное нарушение.
    assert "statement_paths_resolve" not in evaluation.missing_required

    # Через БД: git ответил None, и проект без клона.
    _serve_tree(monkeypatch, None)
    unread = await evaluate_dor(db, await _task_in_project(db, "require"))
    check = next(c for c in unread.checks if c.key == "statement_paths_resolve")
    assert check.passed is False and "не проверено" in check.detail
    assert "statement_paths_resolve" not in unread.missing_required


def test_statement_paths_off_and_no_paths_are_quiet():
    _, off_item = _paths_item(
        _paths_check(validation_commands=[_LIVE_1392], mode="off")
    )
    assert off_item.passed is True
    _, none_item = _paths_item(_paths_check(validation_commands=["uv run ruff check"]))
    assert none_item.passed is True


def test_statement_paths_mode_reader_follows_the_policy_rule():
    from hub.services.project_policy import statement_paths_of

    assert statement_paths_of({}) == "warn"
    assert statement_paths_of({"statement_paths": "off"}) == "off"
    assert statement_paths_of({"statement_paths": "require"}) == "require"
    assert statement_paths_of({"statement_paths": "typo"}) == "warn"


def test_unchecked_paths_ask_nothing_but_violations_name_the_path():
    from hub.services.recommendations import build_recommendations

    unread, _ = _paths_item(_paths_check(validation_commands=[_LIVE_1392], tree=None))
    assert [
        r for r in build_recommendations(unread) if r.field == "validation_commands"
    ] == []

    flagged, _ = _paths_item(_paths_check(validation_commands=[_LIVE_1392]))
    [rec] = [
        r for r in build_recommendations(flagged) if r.field == "validation_commands"
    ]
    assert "tests/fixtures/home_basic/.ssh" in rec.message
    assert rec.expected_score_delta == 0


def test_statement_paths_extraction_skips_redirect_targets_and_dedups():
    from hub.services.dor import statement_paths_in

    assert statement_paths_in("make report > build/out.txt && cat tests/a.py") == [
        "tests/a.py"
    ]
    assert statement_paths_in("pytest tests/a.py::t1 tests/a.py::t2 ./tests/a.py") == [
        "tests/a.py"
    ]


async def test_test_ref_of_a_stored_ac_is_checked_and_named_by_ac_id(
    db: aiosqlite.Connection, monkeypatch
):
    _serve_tree(monkeypatch, _BASE_TREE)
    task_id = await _task_in_project(db, "require")
    await repo.update_task(db, task_id, validation_commands="[]")
    ac = _ac(1).model_copy(update={"test_ref": "tests/test_gone.py::test_x"})
    await repo.add_acceptance_criterion(db, task_id, ac)
    await db.commit()
    result = await evaluate_dor(db, task_id)
    item = next(c for c in result.checks if c.key == "statement_paths_resolve")
    assert "tests/test_gone.py" in item.detail and "test_ref AC-1" in item.detail
    assert "statement_paths_resolve" in result.missing_required


def test_statement_paths_policy_write_refuses_a_typo_and_flag_is_not_a_path():
    from hub.models import validated_gate_policy
    from hub.services.dor import statement_paths_in
    from hub.services.project_policy import statement_paths_of

    assert validated_gate_policy({"statement_paths": "require"}) == {
        "statement_paths": "require"
    }
    with pytest.raises(ValueError, match="statement_paths"):
        validated_gate_policy({"statement_paths": "strict"})
    assert statement_paths_of(None) == "warn"  # type: ignore[arg-type]
    assert statement_paths_in("tool -tests/a.py --out/b.json") == []
