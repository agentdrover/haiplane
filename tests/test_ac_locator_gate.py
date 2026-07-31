"""The locator policy applies to every AC write path, not just bulk refine (#596).

``validate_test_locators`` guarded only ``refine_task``'s ``acceptance_criteria``
payload. The three single-AC paths — add, upsert, replace — never called it, so
with ``SDD_AC_LOCATOR=require`` an unresolvable locator was accepted in silence
and only surfaced later as ``missing`` in a review brief.

Measured on production when this was found: of 912 acceptance criteria with
``verifiable_by=test``, 695 carried a locator and 425 of those were
unresolvable. The mechanisms built on locators — the existence check (#506) and
the test run (#507) — were therefore operating on 39% of the criteria while
reporting nothing about the rest.

Same shape as the limits in #366 and the raw_count reconciliation in #519: a
rule placed on the batch path and forgotten on the single one.
"""

from __future__ import annotations

import aiosqlite
import pytest
from fastapi import HTTPException

from hub import config
from hub import repository as repo
from hub import services
from hub.models import AcceptanceCriterion, TaskCreate, TaskRefine

BAD = "tests/a.py::test_x, tests/a.py::test_y"
GOOD = "tests/test_ac_locator_gate.py::test_add_rejects_an_unresolvable_locator"


def _ac(idx: int = 1, ref: str = BAD) -> AcceptanceCriterion:
    return AcceptanceCriterion(
        id=f"AC-{idx}",
        given="a criterion bound to a test",
        when="it is written through some path",
        then="the locator policy decides",
        verifiable_by="test",
        test_ref=ref,
    )


@pytest.fixture
def require(monkeypatch):
    monkeypatch.setattr(config, "SDD_AC_LOCATOR", "require")


async def _task(db: aiosqlite.Connection) -> int:
    tv = await services.create_task(db, TaskCreate(title="t"))
    await db.commit()
    return tv.id


# --- AC-1: every write path refuses ----------------------------------------


async def test_add_rejects_an_unresolvable_locator(db: aiosqlite.Connection, require):
    task_id = await _task(db)

    with pytest.raises(HTTPException) as exc:
        await services.add_acceptance_criterion(db, task_id, _ac())

    assert exc.value.status_code == 422
    assert not await repo.list_acceptance_criteria(db, task_id), (
        "a refused write must leave nothing behind"
    )


async def test_upsert_rejects_an_unresolvable_locator(
    db: aiosqlite.Connection, require
):
    task_id = await _task(db)

    with pytest.raises(HTTPException) as exc:
        await services.upsert_acceptance_criterion(db, task_id, _ac())

    assert exc.value.status_code == 422
    assert not await repo.list_acceptance_criteria(db, task_id)


async def test_replace_rejects_an_unresolvable_locator(
    db: aiosqlite.Connection, require
):
    task_id = await _task(db)
    await services.add_acceptance_criterion(db, task_id, _ac(1, GOOD))

    with pytest.raises(HTTPException) as exc:
        await services.replace_acceptance_criteria(db, task_id, [_ac(2)])

    assert exc.value.status_code == 422
    rows = await repo.list_acceptance_criteria(db, task_id)
    assert [dict(r)["ac_id"] for r in rows] == ["AC-1"], (
        "the existing criteria must survive a refused replace"
    )


async def test_a_valid_locator_still_goes_through_every_path(
    db: aiosqlite.Connection, require
):
    """The gate must refuse malformed input, not the work itself."""
    task_id = await _task(db)

    await services.add_acceptance_criterion(db, task_id, _ac(1, GOOD))
    await services.upsert_acceptance_criterion(db, task_id, _ac(2, GOOD))
    await services.replace_acceptance_criteria(db, task_id, [_ac(3, GOOD)])
    await services.refine_task(
        db, task_id, TaskRefine(acceptance_criteria=[_ac(4, GOOD)])
    )

    rows = await repo.list_acceptance_criteria(db, task_id)
    assert [dict(r)["ac_id"] for r in rows] == ["AC-4"]


# --- AC-3: the comma-separated near-miss is named --------------------------


async def test_the_refusal_says_a_locator_is_a_single_nodeid(
    db: aiosqlite.Connection, require
):
    """A refusal that only says "no valid locator" reads as if the field were
    empty. The commonest mistake is naming several tests, and the author has
    no way to guess that the field holds exactly one."""
    task_id = await _task(db)

    with pytest.raises(HTTPException) as exc:
        await services.add_acceptance_criterion(db, task_id, _ac())

    detail = str(exc.value.detail)
    assert "ONE" in detail or "one nodeid" in detail
    assert "AC-1" in detail


# --- AC-2: off and warn are untouched, and reads never fail -----------------


@pytest.mark.parametrize("policy", ["off", "warn"])
async def test_off_and_warn_are_unchanged(
    db: aiosqlite.Connection, monkeypatch, policy: str
):
    """This task closes a hole in require. It must not turn into a new gate
    for installations that never opted in."""
    monkeypatch.setattr(config, "SDD_AC_LOCATOR", policy)
    task_id = await _task(db)

    await services.add_acceptance_criterion(db, task_id, _ac(1))
    await services.upsert_acceptance_criterion(db, task_id, _ac(2))
    await services.replace_acceptance_criteria(db, task_id, [_ac(3)])

    rows = await repo.list_acceptance_criteria(db, task_id)
    assert [dict(r)["ac_id"] for r in rows] == ["AC-3"]


async def test_existing_unresolvable_rows_still_read(
    db: aiosqlite.Connection, monkeypatch
):
    """The 425 rows already stored must keep loading with the policy on.

    Enforcing on reads would take out most of the backlog — the gate belongs
    on writes only, and that is the difference between fixing the hole and
    breaking the board."""
    monkeypatch.setattr(config, "SDD_AC_LOCATOR", "off")
    task_id = await _task(db)
    await services.add_acceptance_criterion(db, task_id, _ac(1))
    await db.commit()

    monkeypatch.setattr(config, "SDD_AC_LOCATOR", "require")

    listed = await services.list_acceptance_criteria(db, task_id)
    assert [ac.id for ac in listed] == ["AC-1"]
    assert dict(await repo.get_task(db, task_id))["id"] == task_id


async def test_non_test_criteria_never_need_a_locator(
    db: aiosqlite.Connection, require
):
    """manual / ui_check / log_check criteria have no test to point at."""
    task_id = await _task(db)
    manual = AcceptanceCriterion(
        id="AC-1",
        given="a criterion checked by hand",
        when="nobody wrote a test for it",
        then="no locator is required",
        verifiable_by="manual",
    )

    await services.add_acceptance_criterion(db, task_id, manual)

    assert len(await repo.list_acceptance_criteria(db, task_id)) == 1
