"""Discovery block on the task form (#331).

Six answers a spec should carry beyond "what to build": which number moves
(outcome_metric), what moves first (outcome_indicator), when we check
(outcome_deadline), what would reopen the question
(outcome_revisit_condition), whether the work adapts or reshapes the process
(redesign_decision + redesign_rationale), and how much agency it wants
(agent_fit).

Two of these tests exist because of things that went wrong while building
this, not because of the spec — see the module comments below.
"""

from __future__ import annotations

import aiosqlite
from httpx import AsyncClient

from hub import repository as repo
from hub.db import STRUCTURED_TASK_FIELDS
from hub.models import AgentFit, RedesignDecision, TaskCreate, TaskRefine
from hub.services import lifecycle
from hub.services.dor import DOR_ADVISORY_KEYS, evaluate_from_data
from hub.services.readiness import calculate_score_from_data
from hub.services.recommendations import build_recommendations

DISCOVERY_PAYLOAD = {
    "outcome_metric": "median lead time from open to first commit, 3d -> 1d",
    "outcome_indicator": "share of tasks carrying a filled hypothesis",
    "outcome_deadline": "2026-09-01",
    "outcome_revisit_condition": "if fewer than a third of features fill it",
    "redesign_decision": "redesign",
    "redesign_rationale": "the current form records what to build, not why",
    "agent_fit": "sdd_native",
}


async def _create(client: AsyncClient, **overrides) -> dict:
    resp = await client.post("/api/tasks", json={"title": "t", **overrides})
    assert resp.status_code == 200, resp.text
    return resp.json()


# --- AC-1: the fields round-trip through refine, the task view, and the brief


async def test_refine_stores_discovery_and_task_view_returns_it(client: AsyncClient):
    """AC-1, first half: what refine accepts, GET returns."""
    task = await _create(client)
    resp = await client.post(
        f"/api/tasks/{task['id']}/refine", json=dict(DISCOVERY_PAYLOAD)
    )
    assert resp.status_code == 200, resp.text

    body = (await client.get(f"/api/tasks/{task['id']}")).json()
    for field, expected in DISCOVERY_PAYLOAD.items():
        assert body[field] == expected, f"{field} did not survive the round trip"


async def test_review_brief_carries_discovery(client: AsyncClient):
    """AC-1, second half: a reviewer sees the hypothesis, not only the build
    instructions."""
    task = await _create(client)
    await client.post(f"/api/tasks/{task['id']}/refine", json=dict(DISCOVERY_PAYLOAD))

    brief = (await client.get(f"/api/tasks/{task['id']}/review-brief")).json()
    for field, expected in DISCOVERY_PAYLOAD.items():
        assert brief[field] == expected, f"{field} is missing from the review brief"


async def test_refine_keeps_patch_semantics(client: AsyncClient):
    """AC-1: refine is PATCH — an omitted Discovery field is left alone, and
    filling one does not wipe the others."""
    task = await _create(client)
    await client.post(f"/api/tasks/{task['id']}/refine", json=dict(DISCOVERY_PAYLOAD))

    await client.post(
        f"/api/tasks/{task['id']}/refine", json={"outcome_metric": "something else"}
    )

    body = (await client.get(f"/api/tasks/{task['id']}")).json()
    assert body["outcome_metric"] == "something else"
    assert body["outcome_indicator"] == DISCOVERY_PAYLOAD["outcome_indicator"]
    assert body["redesign_decision"] == DISCOVERY_PAYLOAD["redesign_decision"]
    assert body["agent_fit"] == DISCOVERY_PAYLOAD["agent_fit"]


async def test_every_discovery_field_is_in_the_read_path(db: aiosqlite.Connection):
    """A field can be written to the DB and never come back out.

    ``structured_fields_to_db`` writes whatever the model carries, but
    ``structured_fields_from_row`` — and so ``TaskView`` — only returns what
    ``STRUCTURED_TASK_FIELDS`` lists. A name added to the model and forgotten
    in that tuple is stored silently and never surfaces. Probed on this
    codebase before writing the fix; this test is the guard.
    """
    discovery = {
        "outcome_metric",
        "outcome_indicator",
        "outcome_deadline",
        "outcome_revisit_condition",
        "redesign_decision",
        "redesign_rationale",
        "agent_fit",
    }
    assert discovery <= set(STRUCTURED_TASK_FIELDS)

    tv = await repo.create_task_full(db, TaskCreate(title="t"), status="draft")
    await repo.update_task_structured(
        db,
        tv,
        TaskRefine(
            outcome_metric="m",
            outcome_indicator="i",
            outcome_deadline="d",
            outcome_revisit_condition="r",
            redesign_decision=RedesignDecision.redesign,
            redesign_rationale="why",
            agent_fit=AgentFit.agentic,
        ),
    )
    await db.commit()

    view = lifecycle.row_to_task(await repo.get_task(db, tv))
    assert view.outcome_metric == "m"
    assert view.outcome_indicator == "i"
    assert view.outcome_deadline == "d"
    assert view.outcome_revisit_condition == "r"
    assert view.redesign_decision == RedesignDecision.redesign
    assert view.redesign_rationale == "why"
    assert view.agent_fit == AgentFit.agentic


# --- AC-2: visible, suggested, and free


def _dor(**overrides):
    base = dict(
        work_type="feature",
        user_story="us",
        problem_statement="ps",
        business_value="bv",
        scope_in_count=1,
        validation_count=1,
        size="S",
        wip_tag="feature_work",
        ac_count=1,
    )
    base.update(overrides)
    return evaluate_from_data(**base)


async def test_missing_discovery_does_not_block_dor(client: AsyncClient):
    """AC-2: a task with no Discovery still passes the gate."""
    task = await _create(client)
    await client.post(
        f"/api/tasks/{task['id']}/refine",
        json={
            "user_story": "us",
            "problem_statement": "ps",
            "business_value": "bv",
            "scope_in": ["a"],
            "validation_commands": ["uv run pytest"],
            "size": "S",
            "wip_tag": "feature_work",
            "acceptance_criteria": [
                {
                    "id": "AC-1",
                    "given": "a prepared task",
                    "when": "readiness is calculated",
                    "then": "the gate does not block on Discovery",
                    "verifiable_by": "test",
                }
            ],
        },
    )
    body = (await client.get(f"/api/tasks/{task['id']}/readiness")).json()
    assert body["dor_passed"] is True
    assert any(r["field"] == "outcome_metric" for r in body["recommendations"]), (
        "AC-2 asks for a suggestion to fill the hypothesis"
    )


def test_missing_discovery_costs_no_readiness_points():
    """The regression this feature could most easily have caused.

    A non-required DoR check still costs ``penalty_optional``. No task in an
    existing backlog can carry a field that did not exist yesterday, so
    scoring these checks the ordinary way would have dropped every task in
    the system on the day this shipped — the whole backlog looking worse
    without anyone touching it.
    """
    without = _dor()
    with_discovery = _dor(
        outcome_metric="m", redesign_decision="adapt", agent_fit="sdd_native"
    )

    score_without, _ = calculate_score_from_data(dor=without, risks=[])
    score_with, _ = calculate_score_from_data(dor=with_discovery, risks=[])

    assert score_without == 100
    assert score_with == score_without


def test_discovery_suggestions_promise_the_zero_they_deliver():
    """A suggestion claiming +5 that never arrives teaches the author to
    distrust the number."""
    recs = build_recommendations(_dor())
    discovery = [
        r
        for r in recs
        if r.field in {"outcome_metric", "redesign_decision", "agent_fit"}
    ]
    assert len(discovery) == 3
    assert all(r.severity == "low" for r in discovery)
    assert all(r.expected_score_delta == 0 for r in discovery)


def test_discovery_is_asked_of_features_not_of_chores():
    """Three permanent suggestions on every task in the backlog would be
    wallpaper within a week. The spec scopes Discovery to the feature
    profile, and so does the engine."""
    chore = _dor(work_type="chore")
    fields = {r.field for r in build_recommendations(chore)}
    assert not fields & {"outcome_metric", "redesign_decision", "agent_fit"}
    # ...while the checks themselves stay visible in the DoR table.
    assert DOR_ADVISORY_KEYS <= {c.key for c in chore.checks}


async def test_unset_enum_reads_as_unset_not_as_a_value(db: aiosqlite.Connection):
    """The enum columns are nullable on purpose: NULL is the only honest
    "not chosen". An empty string would be a third state that is neither a
    valid choice nor absent."""
    tv = await repo.create_task_full(db, TaskCreate(title="t"), status="draft")
    await db.commit()
    view = lifecycle.row_to_task(await repo.get_task(db, tv))
    assert view.redesign_decision is None
    assert view.agent_fit is None
