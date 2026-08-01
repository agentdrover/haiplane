"""An acceptance criterion records where its expectation came from (#595).

A criterion says what must be observably true. Nothing recorded who decided
it. When the answer is "the code already does this", the test can only
confirm the status quo — including a defect — which is how an assertion ends
up unable to fail.

The limit of this field, stated in the task and worth repeating here: it
would NOT have caught the four tautological assertions found on 31.07.2026
(#370 T5, #516, #519, and my own in #594). Those were older tests with no
acceptance criteria bound to them at all. This is prophylaxis at authoring
time; the detector for what is already written is #551.
"""

from __future__ import annotations

import aiosqlite
from httpx import AsyncClient

from hub import repository as repo
from hub import services
from hub.models import AcceptanceCriterion, ExpectationSource, TaskCreate


def _ac(idx: int = 1, source: str | None = None) -> AcceptanceCriterion:
    return AcceptanceCriterion(
        id=f"AC-{idx}",
        given="a criterion with a stated expectation",
        when="the source of that expectation matters",
        then="it is recorded and shown",
        verifiable_by="test",
        test_ref="tests/test_expectation_source.py::test_the_source_survives_a_round_trip",
        expectation_source=source,
    )


async def _task(db: aiosqlite.Connection) -> int:
    tv = await services.create_task(db, TaskCreate(title="t"))
    await db.commit()
    return tv.id


# --- AC-1: stored and returned, and the two "empty" states stay apart ------


async def test_the_source_survives_a_round_trip(db: aiosqlite.Connection):
    """Writing and reading acceptance criteria go through different
    functions. A field added to one and forgotten in the other is stored and
    never surfaces — the shape that bit #331."""
    task_id = await _task(db)

    await services.add_acceptance_criterion(db, task_id, _ac(1, "requirement"))

    listed = await services.list_acceptance_criteria(db, task_id)
    assert listed[0].expectation_source == ExpectationSource.requirement


async def test_unstated_and_implementation_are_different_answers(
    db: aiosqlite.Connection,
):
    """'Nobody said' is not 'taken from the code'. Storing both as an empty
    value would make one value carry two meanings — the defect class this
    project keeps hitting."""
    task_id = await _task(db)

    await services.add_acceptance_criterion(db, task_id, _ac(1, "implementation"))
    await services.add_acceptance_criterion(db, task_id, _ac(2, None))

    by_id = {ac.id: ac for ac in await services.list_acceptance_criteria(db, task_id)}
    assert by_id["AC-1"].expectation_source == ExpectationSource.implementation
    assert by_id["AC-2"].expectation_source is None


async def test_the_review_brief_shows_the_source(
    db: aiosqlite.Connection, client: AsyncClient
):
    """A reviewer has to see it. Stored-but-not-shown is the same as absent
    for the purpose this field exists."""
    task_id = await _task(db)
    await services.add_acceptance_criterion(db, task_id, _ac(1, "implementation"))
    await db.execute(
        "UPDATE tasks SET submission_generation=1, status='review' WHERE id=?",
        (task_id,),
    )
    await db.commit()

    brief = (await client.get(f"/api/tasks/{task_id}/review-brief")).json()

    assert brief["acceptance_criteria"][0]["expectation_source"] == "implementation"


async def test_every_source_value_round_trips(db: aiosqlite.Connection):
    """All five values, not just the two the other tests happen to use."""
    task_id = await _task(db)
    for idx, source in enumerate(ExpectationSource, start=1):
        await services.add_acceptance_criterion(db, task_id, _ac(idx, source.value))

    listed = await services.list_acceptance_criteria(db, task_id)
    assert {ac.expectation_source for ac in listed} == set(ExpectationSource)


# --- AC-2: advised, never charged ------------------------------------------


async def _recommendations(client: AsyncClient, task_id: int) -> list[dict]:
    resp = await client.get(f"/api/tasks/{task_id}/readiness")
    assert resp.status_code == 200, resp.text
    return resp.json()["recommendations"]


async def test_an_expectation_taken_from_the_code_is_flagged(
    db: aiosqlite.Connection, client: AsyncClient
):
    """AC-2. Allowed, but the reviewer is told — silence is what hides the
    difference between a checked requirement and a restatement of the code."""
    task_id = await _task(db)
    await services.add_acceptance_criterion(db, task_id, _ac(1, "implementation"))
    await db.commit()

    recs = await _recommendations(client, task_id)

    flagged = [r for r in recs if r["field"] == "expectation_source"]
    assert flagged, "a criterion derived from the code must be visible as such"
    assert all(r["severity"] == "low" for r in flagged)
    assert all(r["expected_score_delta"] == 0 for r in flagged)


async def test_an_unstated_source_is_flagged_too(
    db: aiosqlite.Connection, client: AsyncClient
):
    task_id = await _task(db)
    await services.add_acceptance_criterion(db, task_id, _ac(1, None))
    await db.commit()

    recs = await _recommendations(client, task_id)

    assert any(r["field"] == "expectation_source" for r in recs)


async def test_a_stated_source_produces_no_warning(
    db: aiosqlite.Connection, client: AsyncClient
):
    """The advice must stop once it has been taken, or it becomes wallpaper."""
    task_id = await _task(db)
    await services.add_acceptance_criterion(db, task_id, _ac(1, "requirement"))
    await db.commit()

    recs = await _recommendations(client, task_id)

    assert not [r for r in recs if r["field"] == "expectation_source"]


# --- AC-3: existing criteria are untouched ---------------------------------


async def test_the_score_of_an_existing_task_does_not_move(
    db: aiosqlite.Connection, client: AsyncClient
):
    """The regression this could most easily have caused, and the one #331
    nearly shipped: no criterion written before today can carry the field, so
    charging for it would drop every task in the backlog on release day."""
    task_id = await _task(db)
    await services.add_acceptance_criterion(db, task_id, _ac(1, None))
    await db.commit()

    body = (await client.get(f"/api/tasks/{task_id}/readiness")).json()
    with_warning = body["score"]

    await services.upsert_acceptance_criterion(db, task_id, _ac(1, "requirement"))
    await db.commit()
    body_after = (await client.get(f"/api/tasks/{task_id}/readiness")).json()

    assert body_after["score"] == with_warning, (
        "stating the source is advice, not points — the score must not move"
    )
    assert body_after["dor_passed"] == body["dor_passed"]


async def test_a_criterion_stored_without_the_column_still_reads(
    db: aiosqlite.Connection,
):
    """Rows written before the migration have NULL there. They must load as
    'not stated' rather than failing to parse."""
    task_id = await _task(db)
    await services.add_acceptance_criterion(db, task_id, _ac(1, "requirement"))
    await db.execute(
        "UPDATE acceptance_criteria SET expectation_source = NULL WHERE task_id=?",
        (task_id,),
    )
    await db.commit()

    listed = await services.list_acceptance_criteria(db, task_id)
    assert listed[0].expectation_source is None
    assert len(await repo.list_acceptance_criteria(db, task_id)) == 1
