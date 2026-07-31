"""Malformed input answers 4xx, and one limit holds on every path (#366).

Three defects that shared a shape: a rule enforced in one place and absent in
the neighbouring one.

D1 — every column on ``projects`` is NOT NULL, but ``ProjectPatch`` typed its
fields optional to express "omitted". An explicit null passed validation,
survived ``model_dump(exclude_unset=True)``, and reached the column as a raw
IntegrityError — a 500 for what is a malformed request.

D2/D3 — the 50-item caps on risks and acceptance criteria were checked in the
full-replace path only. Adding one at a time walked past them: 55 single adds
produced 55 rows.
"""

from __future__ import annotations

import json

import aiosqlite
import pytest
from httpx import AsyncClient

from hub import repository as repo
from hub import services
from hub.models import (
    MAX_ACCEPTANCE_CRITERIA,
    MAX_RISKS,
    AcceptanceCriterion,
    RiskKind,
    RiskSeverity,
    TaskCreate,
    TaskRisk,
)


def _ac(idx: int) -> AcceptanceCriterion:
    return AcceptanceCriterion(
        id=f"AC-{idx}",
        given=f"a task in state {idx}",
        when=f"the client sends request {idx}",
        then=f"the server answers {idx}",
        verifiable_by="test",
    )


def _risk(idx: int) -> TaskRisk:
    return TaskRisk(
        kind=RiskKind.other,
        severity=RiskSeverity.low,
        description=f"risk {idx}",
        mitigation="mitigated",
    )


# --- D1: explicit null on PATCH /api/projects -------------------------------


async def _project(client: AsyncClient) -> dict:
    resp = await client.post("/api/projects", json={"slug": "proj", "name": "Proj"})
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


async def _reload(client: AsyncClient, project_id: int) -> dict:
    """Read a project back. There is no GET /api/projects/{id}; the list is
    the only read route."""
    projects = (await client.get("/api/projects")).json()
    return next(p for p in projects if p["id"] == project_id)


@pytest.mark.parametrize(
    "field, value",
    [
        ("name", None),
        ("status", None),
        ("default_branch", None),
        ("default_branch_policy", None),
    ],
)
async def test_explicit_null_is_rejected_not_crashed(
    client: AsyncClient, field: str, value: None
):
    """AC-1. Before the fix this raised IntegrityError out of the route —
    'NOT NULL constraint failed: projects.name' — i.e. a 500."""
    project = await _project(client)

    resp = await client.patch(f"/api/projects/{project['id']}", json={field: value})

    assert resp.status_code == 422, f"{field}=null should be a bad request, not a crash"
    after = await _reload(client, project["id"])
    assert after["name"] == project["name"], "a rejected patch must change nothing"


async def test_omitting_a_field_still_leaves_it_alone(client: AsyncClient):
    """The constraint the null-check could easily have broken: PATCH means
    'omitted stays unchanged', and omitted arrives as None too. The rejection
    has to look at the raw request, not at the parsed model."""
    project = await _project(client)

    resp = await client.patch(
        f"/api/projects/{project['id']}", json={"repo": "git@example.com:x.git"}
    )

    assert resp.status_code == 200, resp.text
    after = await _reload(client, project["id"])
    assert after["repo"] == "git@example.com:x.git"
    assert after["name"] == project["name"]
    assert after["default_branch"] == project["default_branch"]


# --- D2: the risk cap on the single-add path --------------------------------


async def test_risks_stop_at_the_limit_when_added_one_at_a_time(
    db: aiosqlite.Connection, client: AsyncClient
):
    """AC-2. Before the fix, 55 single adds produced 55 risks."""
    tv = await services.create_task(db, TaskCreate(title="t"))
    await db.commit()
    for i in range(MAX_RISKS):
        await services.add_risk(db, tv.id, _risk(i))

    resp = await client.post(
        f"/api/tasks/{tv.id}/risks", json=json.loads(_risk(999).model_dump_json())
    )

    assert resp.status_code == 422, resp.text
    row = dict(await repo.get_task(db, tv.id))
    assert len(json.loads(row["risks"])) == MAX_RISKS


# --- D3: the acceptance-criteria cap on single add and upsert ---------------


async def _task_at_the_ac_limit(db: aiosqlite.Connection) -> int:
    tv = await services.create_task(db, TaskCreate(title="t"))
    await db.commit()
    for i in range(MAX_ACCEPTANCE_CRITERIA):
        await services.add_acceptance_criterion(db, tv.id, _ac(i))
    return tv.id


async def test_acceptance_criteria_stop_at_the_limit_on_single_add(
    db: aiosqlite.Connection, client: AsyncClient
):
    """AC-3. Before the fix, 55 single adds produced 55 criteria."""
    task_id = await _task_at_the_ac_limit(db)

    resp = await client.post(
        f"/api/tasks/{task_id}/acceptance_criteria",
        json=json.loads(_ac(999).model_dump_json()),
    )

    assert resp.status_code == 422, resp.text
    assert len(await repo.list_acceptance_criteria(db, task_id)) == (
        MAX_ACCEPTANCE_CRITERIA
    )


async def test_a_task_at_the_limit_can_still_edit_its_criteria(
    db: aiosqlite.Connection, client: AsyncClient
):
    """Overwriting an existing criterion does not add a row, so the cap must
    not apply. A count check written without this distinction would make a
    task with 50 criteria impossible to correct — the limit would stop being
    a cap and start being a freeze."""
    task_id = await _task_at_the_ac_limit(db)
    edited = _ac(0).model_copy(update={"then": "the server answers differently"})

    resp = await client.put(
        f"/api/tasks/{task_id}/acceptance_criteria/AC-0",
        json=json.loads(edited.model_dump_json()),
    )

    assert resp.status_code == 200, resp.text
    rows = await repo.list_acceptance_criteria(db, task_id)
    assert len(rows) == MAX_ACCEPTANCE_CRITERIA
    assert any(dict(r)["then_clause"] == "the server answers differently" for r in rows)


async def test_resending_an_existing_criterion_at_the_limit_is_still_a_no_op(
    db: aiosqlite.Connection, client: AsyncClient
):
    """Add is idempotent by ac_id. Being at the cap must not turn a harmless
    resend into an error."""
    task_id = await _task_at_the_ac_limit(db)

    resp = await client.post(
        f"/api/tasks/{task_id}/acceptance_criteria",
        json=json.loads(_ac(0).model_dump_json()),
    )

    assert resp.status_code == 200, resp.text
    assert len(await repo.list_acceptance_criteria(db, task_id)) == (
        MAX_ACCEPTANCE_CRITERIA
    )
