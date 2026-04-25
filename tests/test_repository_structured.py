from __future__ import annotations

import aiosqlite
import pytest

from hub import repository as repo
from hub.db import (
    row_to_ac_kwargs,
    structured_fields_from_row,
    structured_fields_to_db,
)
from hub.models import (
    ACVerifiableBy,
    AcceptanceCriterion,
    ClassOfService,
    RiskKind,
    RiskSeverity,
    TaskCreate,
    TaskRefine,
    TaskRisk,
    TaskSize,
    WipTag,
    WorkType,
)


def _ac(
    idx: int = 1, *, by: ACVerifiableBy = ACVerifiableBy.test
) -> AcceptanceCriterion:
    return AcceptanceCriterion(
        id=f"AC-{idx}",
        given=f"given-{idx}",
        when=f"when-{idx}",
        then=f"then-{idx}",
        verifiable_by=by,
        test_ref=f"tests/test_ac.py::test_{idx}",
    )


# --- structured_fields_to_db ---


def test_structured_fields_to_db_full_payload_serializes_lists_and_enums():
    tc = TaskCreate(
        title="x",
        work_type=WorkType.bug,
        class_of_service=ClassOfService.expedite,
        size=TaskSize.M,
        wip_tag=WipTag.bugfix,
        scope_in=["a", "b"],
        validation_commands=["pytest -q"],
        constraints=["python>=3.11"],
    )
    out = structured_fields_to_db(tc)
    assert out["work_type"] == "bug"
    assert out["class_of_service"] == "expedite"
    assert out["size"] == "M"
    assert out["wip_tag"] == "bugfix"
    assert out["scope_in"] == '["a", "b"]'
    assert out["validation_commands"] == '["pytest -q"]'
    assert out["constraints"] == '["python>=3.11"]'
    assert "acceptance_criteria" not in out
    # TaskCreate has no risks field — risks are added later via refine.
    assert "risks" not in out


def test_structured_fields_to_db_refine_exclude_unset():
    refine = TaskRefine(scope_in=["only-this"], size=TaskSize.L)
    out = structured_fields_to_db(refine, exclude_unset=True)
    assert out == {"scope_in": '["only-this"]', "size": "L"}


def test_structured_fields_to_db_skips_acceptance_criteria():
    refine = TaskRefine(acceptance_criteria=[_ac(1)])
    out = structured_fields_to_db(refine, exclude_unset=True)
    assert out == {}


def test_structured_fields_to_db_serializes_risks():
    refine = TaskRefine(
        risks=[
            TaskRisk(
                kind=RiskKind.security,
                severity=RiskSeverity.high,
                description="d",
                mitigation="m",
            )
        ]
    )
    out = structured_fields_to_db(refine, exclude_unset=True)
    assert "risks" in out
    assert '"kind": "security"' in out["risks"]
    assert '"severity": "high"' in out["risks"]


# --- create_task_full + structured_fields_from_row ---


async def _insert_full_task(db: aiosqlite.Connection, **overrides):
    tc = TaskCreate(
        title="Full task",
        description="d",
        work_type=WorkType.refactor,
        class_of_service=ClassOfService.fixed_date,
        size=TaskSize.S,
        wip_tag=WipTag.tech_debt,
        user_story="as a dev, I want X so that Y",
        problem_statement="db is slow",
        business_value="reduce p95 by 30%",
        scope_in=["query optimizer", "indexes"],
        scope_out=["UI"],
        affected_areas=["hub/db.py"],
        technical_hints="add covering index on tasks(status, priority)",
        constraints=["no breaking schema change"],
        assumptions=["sqlite stays default backend"],
        validation_commands=["uv run pytest -q"],
        out_of_scope_for_review=["docs"],
        review_checklist=["check migration path", "verify rollback safety"],
        **overrides,
    )
    task_id = await repo.create_task_full(db, tc, status="draft")
    await db.commit()
    return task_id, tc


async def test_create_task_full_persists_structured_fields(db: aiosqlite.Connection):
    task_id, tc = await _insert_full_task(db)
    row = await repo.get_task(db, task_id)
    assert row is not None
    fields = structured_fields_from_row(row)
    assert fields["work_type"] == "refactor"
    assert fields["class_of_service"] == "fixed_date"
    assert fields["size"] == "S"
    assert fields["wip_tag"] == "tech_debt"
    assert fields["scope_in"] == ["query optimizer", "indexes"]
    assert fields["scope_out"] == ["UI"]
    assert fields["affected_areas"] == ["hub/db.py"]
    assert fields["constraints"] == ["no breaking schema change"]
    assert fields["validation_commands"] == ["uv run pytest -q"]
    assert fields["review_checklist"] == [
        "check migration path",
        "verify rollback safety",
    ]
    assert fields["risks"] == []
    assert fields["readiness_score"] is None
    assert fields["dor_passed"] is None


async def test_create_task_full_persists_human_owner_and_reviewer(
    db: aiosqlite.Connection,
):
    tc = TaskCreate(
        title="Owner/reviewer task",
        human_owner="alice",
        human_reviewer="bob",
    )
    task_id = await repo.create_task_full(db, tc, status="draft")
    await db.commit()
    row = await repo.get_task(db, task_id)
    assert row is not None
    assert row["human_owner"] == "alice"
    assert row["human_reviewer"] == "bob"


async def test_create_task_full_works_with_minimal_payload(db: aiosqlite.Connection):
    tc = TaskCreate(title="minimal")
    task_id = await repo.create_task_full(db, tc, status="draft")
    await db.commit()
    row = await repo.get_task(db, task_id)
    assert row is not None
    fields = structured_fields_from_row(row)
    assert fields["work_type"] == "feature"
    assert fields["scope_in"] == []
    assert fields["risks"] == []


async def test_structured_fields_from_row_dor_passed_coerced_to_bool(
    db: aiosqlite.Connection,
):
    task_id, _ = await _insert_full_task(db)
    await repo.update_task(db, task_id, dor_passed=1, readiness_score=85)
    await db.commit()
    row = await repo.get_task(db, task_id)
    fields = structured_fields_from_row(row)
    assert fields["dor_passed"] is True
    assert fields["readiness_score"] == 85


# --- update_task_structured ---


async def test_update_task_structured_partial_update(db: aiosqlite.Connection):
    task_id, _ = await _insert_full_task(db)
    refine = TaskRefine(size=TaskSize.XL, scope_in=["new", "scope"])
    applied = await repo.update_task_structured(db, task_id, refine)
    await db.commit()
    assert applied == {"size": "XL", "scope_in": '["new", "scope"]'}
    row = await repo.get_task(db, task_id)
    fields = structured_fields_from_row(row)
    assert fields["size"] == "XL"
    assert fields["scope_in"] == ["new", "scope"]
    # untouched fields stay intact
    assert fields["work_type"] == "refactor"
    assert fields["affected_areas"] == ["hub/db.py"]


async def test_update_task_structured_empty_refine_is_noop(db: aiosqlite.Connection):
    task_id, _ = await _insert_full_task(db)
    applied = await repo.update_task_structured(db, task_id, TaskRefine())
    await db.commit()
    assert applied == {}


async def test_update_task_structured_bumps_updated_at(
    db: aiosqlite.Connection,
):
    """Regression for review I11: any non-empty structured update must
    bump tasks.updated_at so pollers and stale-detection logic see the
    change. An empty refine, on the other hand, is a no-op and must
    leave updated_at alone."""
    import asyncio

    task_id, _ = await _insert_full_task(db)
    before_row = await repo.get_task(db, task_id)
    before_ts = before_row["updated_at"]

    # SQLite datetime('now') has 1-second resolution; wait so the
    # difference is observable.
    await asyncio.sleep(1.1)

    await repo.update_task_structured(db, task_id, TaskRefine(size=TaskSize.XL))
    await db.commit()
    after_row = await repo.get_task(db, task_id)
    assert after_row["updated_at"] > before_ts, (
        "non-empty update_task_structured must bump updated_at"
    )

    # Empty refine: same updated_at after another commit cycle.
    after_first = after_row["updated_at"]
    await asyncio.sleep(1.1)
    await repo.update_task_structured(db, task_id, TaskRefine())
    await db.commit()
    final_row = await repo.get_task(db, task_id)
    assert final_row["updated_at"] == after_first, (
        "empty refine must not bump updated_at (no UPDATE issued)"
    )


async def test_update_task_structured_review_checklist_patch_semantics(
    db: aiosqlite.Connection,
):
    """Replace, omit-keeps, and explicit-clear semantics for review_checklist."""
    task_id, _ = await _insert_full_task(db)

    # Replace
    await repo.update_task_structured(
        db, task_id, TaskRefine(review_checklist=["A", "B"])
    )
    await db.commit()
    fields = structured_fields_from_row(await repo.get_task(db, task_id))
    assert fields["review_checklist"] == ["A", "B"]

    # Omitted -> untouched
    await repo.update_task_structured(db, task_id, TaskRefine(size=TaskSize.S))
    await db.commit()
    fields = structured_fields_from_row(await repo.get_task(db, task_id))
    assert fields["review_checklist"] == ["A", "B"]

    # Explicit empty list -> cleared
    await repo.update_task_structured(db, task_id, TaskRefine(review_checklist=[]))
    await db.commit()
    fields = structured_fields_from_row(await repo.get_task(db, task_id))
    assert fields["review_checklist"] == []


async def test_update_task_structured_writes_risks(db: aiosqlite.Connection):
    task_id, _ = await _insert_full_task(db)
    refine = TaskRefine(
        risks=[
            TaskRisk(
                kind=RiskKind.large_scope,
                severity=RiskSeverity.high,
                description="too many files",
                mitigation="split into 3 PRs",
            )
        ]
    )
    await repo.update_task_structured(db, task_id, refine)
    await db.commit()
    row = await repo.get_task(db, task_id)
    fields = structured_fields_from_row(row)
    assert len(fields["risks"]) == 1
    assert fields["risks"][0]["kind"] == "large_scope"


async def test_append_task_risk_preserves_existing_risks(db: aiosqlite.Connection):
    task_id, _ = await _insert_full_task(db)
    first = TaskRisk(
        kind=RiskKind.security,
        severity=RiskSeverity.low,
        description="first",
        mitigation="watch logs",
    )
    second = TaskRisk(
        kind=RiskKind.performance,
        severity=RiskSeverity.medium,
        description="second",
        mitigation="add index",
    )

    assert await repo.append_task_risk(db, task_id, first) is True
    assert await repo.append_task_risk(db, task_id, second) is True
    await db.commit()

    row = await repo.get_task(db, task_id)
    fields = structured_fields_from_row(row)
    assert [risk["kind"] for risk in fields["risks"]] == ["security", "performance"]
    assert fields["risks"][1]["mitigation"] == "add index"


async def test_append_task_risk_unknown_task_returns_false(
    db: aiosqlite.Connection,
):
    risk = TaskRisk(
        kind=RiskKind.security,
        severity=RiskSeverity.high,
        description="unknown",
        mitigation="create task first",
    )
    assert await repo.append_task_risk(db, 99999, risk) is False


# --- AC CRUD ---


async def test_add_and_list_acceptance_criteria(db: aiosqlite.Connection):
    task_id, _ = await _insert_full_task(db)
    await repo.add_acceptance_criterion(db, task_id, _ac(1))
    await repo.add_acceptance_criterion(db, task_id, _ac(2, by=ACVerifiableBy.manual))
    await db.commit()

    rows = await repo.list_acceptance_criteria(db, task_id)
    assert [r["ac_id"] for r in rows] == ["AC-1", "AC-2"]
    # round-trip through Pydantic
    acs = [AcceptanceCriterion(**row_to_ac_kwargs(r)) for r in rows]
    assert acs[0].when == "when-1"
    assert acs[0].then == "then-1"
    assert acs[1].verifiable_by == ACVerifiableBy.manual


async def test_add_acceptance_criterion_duplicate_raises_integrity_error(
    db: aiosqlite.Connection,
):
    task_id, _ = await _insert_full_task(db)
    await repo.add_acceptance_criterion(db, task_id, _ac(1))
    await db.commit()
    with pytest.raises(aiosqlite.IntegrityError):
        await repo.add_acceptance_criterion(db, task_id, _ac(1))
        await db.commit()


async def test_replace_acceptance_criteria_overwrites_all(db: aiosqlite.Connection):
    task_id, _ = await _insert_full_task(db)
    await repo.add_acceptance_criterion(db, task_id, _ac(1))
    await repo.add_acceptance_criterion(db, task_id, _ac(2))
    await db.commit()

    new_items = [_ac(10), _ac(11, by=ACVerifiableBy.log_check)]
    inserted = await repo.replace_acceptance_criteria(db, task_id, new_items)
    await db.commit()

    assert inserted == 2
    rows = await repo.list_acceptance_criteria(db, task_id)
    assert [r["ac_id"] for r in rows] == ["AC-10", "AC-11"]
    assert rows[1]["verifiable_by"] == "log_check"


async def test_replace_acceptance_criteria_with_empty_list_clears(
    db: aiosqlite.Connection,
):
    task_id, _ = await _insert_full_task(db)
    await repo.add_acceptance_criterion(db, task_id, _ac(1))
    await db.commit()
    inserted = await repo.replace_acceptance_criteria(db, task_id, [])
    await db.commit()
    assert inserted == 0
    rows = await repo.list_acceptance_criteria(db, task_id)
    assert rows == []


async def test_replace_acceptance_criteria_rejects_duplicates_in_payload(
    db: aiosqlite.Connection,
):
    task_id, _ = await _insert_full_task(db)
    await repo.add_acceptance_criterion(db, task_id, _ac(1))
    await db.commit()
    with pytest.raises(ValueError, match="duplicate ac_id"):
        await repo.replace_acceptance_criteria(db, task_id, [_ac(1), _ac(1)])
    # original unaffected (we raise before DELETE)
    rows = await repo.list_acceptance_criteria(db, task_id)
    assert [r["ac_id"] for r in rows] == ["AC-1"]


async def test_delete_acceptance_criterion_removes_existing(db: aiosqlite.Connection):
    task_id, _ = await _insert_full_task(db)
    await repo.add_acceptance_criterion(db, task_id, _ac(1))
    await repo.add_acceptance_criterion(db, task_id, _ac(2))
    await db.commit()

    removed = await repo.delete_acceptance_criterion(db, task_id, "AC-1")
    await db.commit()
    assert removed is True

    rows = await repo.list_acceptance_criteria(db, task_id)
    assert [r["ac_id"] for r in rows] == ["AC-2"]


async def test_delete_acceptance_criterion_missing_returns_false(
    db: aiosqlite.Connection,
):
    task_id, _ = await _insert_full_task(db)
    removed = await repo.delete_acceptance_criterion(db, task_id, "AC-999")
    await db.commit()
    assert removed is False


async def test_acs_isolated_per_task(db: aiosqlite.Connection):
    task_a, _ = await _insert_full_task(db)
    task_b, _ = await _insert_full_task(db)
    await repo.add_acceptance_criterion(db, task_a, _ac(1))
    await repo.add_acceptance_criterion(db, task_b, _ac(1))
    await db.commit()
    rows_a = await repo.list_acceptance_criteria(db, task_a)
    rows_b = await repo.list_acceptance_criteria(db, task_b)
    assert len(rows_a) == 1 and len(rows_b) == 1
    assert rows_a[0]["task_id"] == task_a
    assert rows_b[0]["task_id"] == task_b


async def test_replace_acs_does_not_affect_other_task(db: aiosqlite.Connection):
    task_a, _ = await _insert_full_task(db)
    task_b, _ = await _insert_full_task(db)
    await repo.add_acceptance_criterion(db, task_a, _ac(1))
    await repo.add_acceptance_criterion(db, task_b, _ac(1))
    await repo.add_acceptance_criterion(db, task_b, _ac(2))
    await db.commit()
    await repo.replace_acceptance_criteria(db, task_a, [_ac(99)])
    await db.commit()
    rows_b = await repo.list_acceptance_criteria(db, task_b)
    assert [r["ac_id"] for r in rows_b] == ["AC-1", "AC-2"]
