"""HTTP-level tests for the structured task form, refine, ACs, and readiness.

These tests exercise the FastAPI routes registered in ``hub.app`` end-to-end
through the in-memory DB ``client`` fixture in ``conftest.py``. They guard
the public contract — request shape, response model, and status codes —
so that future refactors in the service layer cannot silently change it.
"""

from __future__ import annotations

import asyncio

from httpx import AsyncClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _create_task(client: AsyncClient, **overrides) -> dict:
    """Create a task via POST /api/tasks and return the JSON view."""
    body = {"title": "t", **overrides}
    resp = await client.post("/api/tasks", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _ac_payload(idx: int = 1, **overrides) -> dict:
    base = {
        "id": f"AC-{idx}",
        "given": f"a logged-in user in context {idx}",
        "when": f"the user triggers action {idx}",
        "then": f"the system returns outcome {idx}",
        "verifiable_by": "test",
        "test_ref": f"tests/test_ac.py::test_{idx}",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# POST /api/tasks/{id}/refine
# ---------------------------------------------------------------------------


async def test_refine_writes_only_provided_fields(client: AsyncClient):
    task = await _create_task(client)
    resp = await client.post(
        f"/api/tasks/{task['id']}/refine",
        json={"problem_statement": "ps-1", "size": "M"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["problem_statement"] == "ps-1"
    assert body["size"] == "M"
    # untouched defaults stay untouched
    assert body["user_story"] in (None, "")


async def test_refine_partial_does_not_clobber_other_fields(client: AsyncClient):
    task = await _create_task(client)
    await client.post(
        f"/api/tasks/{task['id']}/refine",
        json={"user_story": "us-1", "business_value": "bv-1"},
    )
    # Second refine touches only size; user_story / business_value must persist.
    resp = await client.post(
        f"/api/tasks/{task['id']}/refine",
        json={"size": "S"},
    )
    body = resp.json()
    assert body["user_story"] == "us-1"
    assert body["business_value"] == "bv-1"
    assert body["size"] == "S"


async def test_refine_replaces_acceptance_criteria_atomically(client: AsyncClient):
    task = await _create_task(client)
    # Seed two ACs.
    resp = await client.post(
        f"/api/tasks/{task['id']}/refine",
        json={"acceptance_criteria": [_ac_payload(1), _ac_payload(2)]},
    )
    assert resp.status_code == 200
    listed = await client.get(f"/api/tasks/{task['id']}/acceptance_criteria")
    assert {ac["id"] for ac in listed.json()} == {"AC-1", "AC-2"}

    # Replace with a single different AC.
    await client.post(
        f"/api/tasks/{task['id']}/refine",
        json={"acceptance_criteria": [_ac_payload(9)]},
    )
    listed = await client.get(f"/api/tasks/{task['id']}/acceptance_criteria")
    assert [ac["id"] for ac in listed.json()] == ["AC-9"]


async def test_refine_with_empty_ac_list_clears_existing(client: AsyncClient):
    task = await _create_task(client)
    await client.post(
        f"/api/tasks/{task['id']}/refine",
        json={"acceptance_criteria": [_ac_payload(1)]},
    )
    # Empty list is intentional clear, not "untouched".
    await client.post(
        f"/api/tasks/{task['id']}/refine",
        json={"acceptance_criteria": []},
    )
    listed = await client.get(f"/api/tasks/{task['id']}/acceptance_criteria")
    assert listed.json() == []


async def test_refine_duplicate_ac_ids_in_payload_returns_422(client: AsyncClient):
    task = await _create_task(client)
    resp = await client.post(
        f"/api/tasks/{task['id']}/refine",
        json={"acceptance_criteria": [_ac_payload(1), _ac_payload(1)]},
    )
    assert resp.status_code == 422
    assert "AC-1" in resp.text


async def test_refine_rolls_back_structured_fields_when_ac_fails(
    client: AsyncClient,
):
    """Regression for review I6: a refine that updates structured fields
    AND fails on duplicate AC ids must roll back BOTH parts. Otherwise
    the task ends up half-mutated."""
    task = await _create_task(client)

    # Set a known initial state.
    pre = await client.post(
        f"/api/tasks/{task['id']}/refine",
        json={"problem_statement": "initial-ps", "scope_in": ["initial"]},
    )
    assert pre.status_code == 200, pre.text

    # Now try a refine with valid structured fields BUT a duplicate AC id —
    # the AC validation must abort the whole refine.
    bad = await client.post(
        f"/api/tasks/{task['id']}/refine",
        json={
            "problem_statement": "tampered-ps",
            "scope_in": ["tampered"],
            "acceptance_criteria": [_ac_payload(1), _ac_payload(1)],
        },
    )
    assert bad.status_code == 422

    after = (await client.get(f"/api/tasks/{task['id']}")).json()
    assert after["problem_statement"] == "initial-ps", (
        "structured fields must not persist when the same refine fails on ACs"
    )
    assert after["scope_in"] == ["initial"]


async def test_refine_updates_human_owner_and_reviewer(client: AsyncClient):
    task = await _create_task(client)
    resp = await client.post(
        f"/api/tasks/{task['id']}/refine",
        json={"human_owner": "alice", "human_reviewer": "bob"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["human_owner"] == "alice"
    assert body["human_reviewer"] == "bob"

    # Second refine touching only owner; reviewer must persist.
    resp2 = await client.post(
        f"/api/tasks/{task['id']}/refine",
        json={"human_owner": "charlie"},
    )
    body2 = resp2.json()
    assert body2["human_owner"] == "charlie"
    assert body2["human_reviewer"] == "bob"


async def test_refine_updates_title(client: AsyncClient):
    task = await _create_task(client)
    old_title = task["title"]

    resp = await client.post(
        f"/api/tasks/{task['id']}/refine",
        json={"title": "Renamed via refine"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["title"] == "Renamed via refine"
    assert body["title"] != old_title

    updates = (await client.get(f"/api/tasks/{task['id']}")).json()["updates"]
    assert any("Title refined" in u["content"] for u in updates)


async def test_refine_review_checklist_replace_omit_clear(client: AsyncClient):
    """PATCH semantics for review_checklist: replace, omit-keeps, []-clears."""
    task = await _create_task(client)

    # Replace
    resp = await client.post(
        f"/api/tasks/{task['id']}/refine",
        json={"review_checklist": ["check migration", "verify rollback"]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["review_checklist"] == ["check migration", "verify rollback"]

    # Omitted -> untouched
    resp = await client.post(
        f"/api/tasks/{task['id']}/refine",
        json={"size": "S"},
    )
    assert resp.status_code == 200
    assert resp.json()["review_checklist"] == ["check migration", "verify rollback"]

    # Explicit empty list -> cleared
    resp = await client.post(
        f"/api/tasks/{task['id']}/refine",
        json={"review_checklist": []},
    )
    assert resp.status_code == 200
    assert resp.json()["review_checklist"] == []


async def test_refine_unknown_task_returns_404(client: AsyncClient):
    resp = await client.post("/api/tasks/99999/refine", json={"size": "S"})
    assert resp.status_code == 404


async def test_refine_invalid_enum_returns_422(client: AsyncClient):
    task = await _create_task(client)
    resp = await client.post(
        f"/api/tasks/{task['id']}/refine",
        json={"size": "not-a-size"},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# POST /api/tasks/{id}/risks
# ---------------------------------------------------------------------------


async def test_add_risk_appends_without_replacing_existing(client: AsyncClient):
    task = await _create_task(client)
    first = await client.post(
        f"/api/tasks/{task['id']}/risks",
        json={
            "kind": "security",
            "severity": "low",
            "description": "first risk",
            "mitigation": "watch logs",
        },
    )
    assert first.status_code == 201, first.text

    second = await client.post(
        f"/api/tasks/{task['id']}/risks",
        json={
            "kind": "performance",
            "severity": "medium",
            "description": "slow loop",
            "mitigation": "add index",
        },
    )
    assert second.status_code == 201, second.text
    body = second.json()
    assert [r["kind"] for r in body["risks"]] == ["security", "performance"]
    assert body["risks"][1]["mitigation"] == "add index"


async def test_add_risk_unknown_task_returns_404(client: AsyncClient):
    resp = await client.post(
        "/api/tasks/99999/risks",
        json={
            "kind": "security",
            "severity": "high",
            "description": "unknown task",
            "mitigation": "create task first",
        },
    )
    assert resp.status_code == 404


async def test_add_risk_invalid_payload_returns_422(client: AsyncClient):
    task = await _create_task(client)
    resp = await client.post(
        f"/api/tasks/{task['id']}/risks",
        json={
            "kind": "security",
            "severity": "invalid",
            "description": "bad severity",
            "mitigation": "fix payload",
        },
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# CRUD: /api/tasks/{id}/acceptance_criteria
# ---------------------------------------------------------------------------


async def test_list_acs_for_unknown_task_returns_404(client: AsyncClient):
    resp = await client.get("/api/tasks/99999/acceptance_criteria")
    assert resp.status_code == 404


async def test_add_ac_returns_201_and_persists(client: AsyncClient):
    task = await _create_task(client)
    resp = await client.post(
        f"/api/tasks/{task['id']}/acceptance_criteria", json=_ac_payload(1)
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["id"] == "AC-1"

    listed = await client.get(f"/api/tasks/{task['id']}/acceptance_criteria")
    assert [ac["id"] for ac in listed.json()] == ["AC-1"]


async def test_add_ac_duplicate_returns_200_idempotent(client: AsyncClient):
    task = await _create_task(client)
    await client.post(
        f"/api/tasks/{task['id']}/acceptance_criteria", json=_ac_payload(1)
    )
    resp = await client.post(
        f"/api/tasks/{task['id']}/acceptance_criteria", json=_ac_payload(1)
    )
    assert resp.status_code == 200
    assert resp.json()["id"] == "AC-1"
    listed = await client.get(f"/api/tasks/{task['id']}/acceptance_criteria")
    assert len(listed.json()) == 1


async def test_put_replaces_acs_atomically(client: AsyncClient):
    task = await _create_task(client)
    await client.post(
        f"/api/tasks/{task['id']}/acceptance_criteria", json=_ac_payload(1)
    )
    resp = await client.put(
        f"/api/tasks/{task['id']}/acceptance_criteria",
        json=[_ac_payload(2), _ac_payload(3)],
    )
    assert resp.status_code == 200
    assert {ac["id"] for ac in resp.json()} == {"AC-2", "AC-3"}

    listed = await client.get(f"/api/tasks/{task['id']}/acceptance_criteria")
    assert {ac["id"] for ac in listed.json()} == {"AC-2", "AC-3"}


async def test_put_with_duplicate_ids_returns_422(client: AsyncClient):
    task = await _create_task(client)
    resp = await client.put(
        f"/api/tasks/{task['id']}/acceptance_criteria",
        json=[_ac_payload(1), _ac_payload(1)],
    )
    assert resp.status_code == 422


async def test_delete_ac_returns_204_and_removes(client: AsyncClient):
    task = await _create_task(client)
    await client.post(
        f"/api/tasks/{task['id']}/acceptance_criteria", json=_ac_payload(1)
    )
    resp = await client.delete(f"/api/tasks/{task['id']}/acceptance_criteria/AC-1")
    assert resp.status_code == 204
    listed = await client.get(f"/api/tasks/{task['id']}/acceptance_criteria")
    assert listed.json() == []


async def test_delete_unknown_ac_returns_404(client: AsyncClient):
    task = await _create_task(client)
    resp = await client.delete(f"/api/tasks/{task['id']}/acceptance_criteria/AC-404")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/tasks/{id}/readiness
# ---------------------------------------------------------------------------


async def test_readiness_minimal_task_below_100_with_recommendations(
    client: AsyncClient,
):
    task = await _create_task(client)
    resp = await client.get(f"/api/tasks/{task['id']}/readiness")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["score"] < 100
    assert body["dor_passed"] is False
    assert len(body["recommendations"]) > 0
    # Default explain is False.
    assert body["explain"] is None


async def test_readiness_explain_returns_components(client: AsyncClient):
    task = await _create_task(client)
    resp = await client.get(
        f"/api/tasks/{task['id']}/readiness", params={"explain": "true"}
    )
    body = resp.json()
    assert body["explain"] is not None
    assert all({"field", "delta", "reason"} <= comp.keys() for comp in body["explain"])
    assert sum(comp["delta"] for comp in body["explain"]) == body["score"] - 100


async def test_readiness_full_feature_returns_100(client: AsyncClient):
    task = await _create_task(client)
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
            "acceptance_criteria": [_ac_payload(1)],
        },
    )
    resp = await client.get(f"/api/tasks/{task['id']}/readiness")
    body = resp.json()
    assert body["score"] == 100
    assert body["dor_passed"] is True
    assert body["recommendations"] == []


async def test_readiness_unknown_task_returns_404(client: AsyncClient):
    resp = await client.get("/api/tasks/99999/readiness")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/tasks/{id}/readiness-tree
# ---------------------------------------------------------------------------


async def _make_dor_ready(client: AsyncClient, task_id: int) -> None:
    resp = await client.post(
        f"/api/tasks/{task_id}/refine",
        json={
            "user_story": "us",
            "problem_statement": "ps",
            "business_value": "bv",
            "scope_in": ["a"],
            "validation_commands": ["uv run pytest"],
            "size": "S",
            "wip_tag": "feature_work",
            "acceptance_criteria": [_ac_payload(1)],
        },
    )
    assert resp.status_code == 200, resp.text


async def _make_feature(client: AsyncClient) -> dict:
    """epic -> feature, returning the feature so tasks can hang under it."""
    epic = await _create_task(client, task_type="epic")
    return await _create_task(client, task_type="feature", parent_id=epic["id"])


async def test_readiness_tree_rolls_up_descendants(client: AsyncClient):
    feature = await _make_feature(client)
    ready = await _create_task(client, parent_id=feature["id"])
    not_ready = await _create_task(client, parent_id=feature["id"])
    await _make_dor_ready(client, ready["id"])

    resp = await client.get(f"/api/tasks/{feature['id']}/readiness-tree")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["root_id"] == feature["id"]
    assert body["total"] == 2
    assert body["ready"] == 1
    assert body["not_ready"] == 1
    ids = {n["id"] for n in body["nodes"]}
    assert ids == {ready["id"], not_ready["id"]}
    # Root is excluded by default.
    assert feature["id"] not in ids
    by_id = {n["id"]: n for n in body["nodes"]}
    assert by_id[ready["id"]]["dor_passed"] is True
    assert by_id[not_ready["id"]]["dor_passed"] is False
    assert by_id[not_ready["id"]]["missing_required"]


async def test_readiness_tree_include_root(client: AsyncClient):
    feature = await _make_feature(client)
    await _create_task(client, parent_id=feature["id"])

    resp = await client.get(
        f"/api/tasks/{feature['id']}/readiness-tree",
        params={"include_root": "true"},
    )
    body = resp.json()
    ids = {n["id"] for n in body["nodes"]}
    assert feature["id"] in ids
    assert body["total"] == 2


async def test_readiness_tree_collects_multiple_levels(client: AsyncClient):
    """BFS reaches grandchildren: epic -> feature -> task -> subtask."""
    epic = await _create_task(client, task_type="epic")
    feature = await _create_task(client, task_type="feature", parent_id=epic["id"])
    task = await _create_task(client, task_type="task", parent_id=feature["id"])
    subtask = await _create_task(client, task_type="subtask", parent_id=task["id"])

    resp = await client.get(f"/api/tasks/{epic['id']}/readiness-tree")
    assert resp.status_code == 200, resp.text
    ids = {n["id"] for n in resp.json()["nodes"]}
    # All descendants are still actionable (open) → present; root excluded.
    assert {feature["id"], task["id"], subtask["id"]} <= ids
    assert epic["id"] not in ids


async def test_readiness_tree_excludes_non_actionable_statuses(client: AsyncClient):
    """A completed child must not be counted as not_ready (DoR is a pre-gate)."""
    feature = await _make_feature(client)
    open_task = await _create_task(client, parent_id=feature["id"])
    done_task = await _create_task(client, parent_id=feature["id"])
    # Drive done_task to completed via pair-start + report done.
    await client.post(
        f"/api/tasks/{done_task['id']}/pair-start",
        json={"plan": "Plan: do it", "assigned_agent": "dev"},
    )
    await client.post(
        f"/api/tasks/{done_task['id']}/updates",
        json={"agent": "dev", "kind": "done", "content": "done"},
    )

    body = (await client.get(f"/api/tasks/{feature['id']}/readiness-tree")).json()
    ids = {n["id"] for n in body["nodes"]}
    # Only the still-open task is scored; the completed one is skipped.
    assert open_task["id"] in ids
    assert done_task["id"] not in ids
    assert body["total"] == 1
    assert body["not_ready"] == 1


async def test_readiness_tree_unknown_task_returns_404(client: AsyncClient):
    resp = await client.get("/api/tasks/99999/readiness-tree")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Collection payload limits
# ---------------------------------------------------------------------------


async def test_put_oversized_ac_list_returns_422(client: AsyncClient):
    """PUT /acceptance_criteria with >50 items must be rejected."""
    from hub.models import MAX_ACCEPTANCE_CRITERIA

    task = await _create_task(client)
    oversized = [_ac_payload(i) for i in range(MAX_ACCEPTANCE_CRITERIA + 1)]
    resp = await client.put(
        f"/api/tasks/{task['id']}/acceptance_criteria",
        json=oversized,
    )
    assert resp.status_code == 422
    assert "too many" in resp.text.lower()


async def test_refine_oversized_acs_returns_422(client: AsyncClient):
    from hub.models import MAX_ACCEPTANCE_CRITERIA

    task = await _create_task(client)
    oversized = [_ac_payload(i) for i in range(MAX_ACCEPTANCE_CRITERIA + 1)]
    resp = await client.post(
        f"/api/tasks/{task['id']}/refine",
        json={"acceptance_criteria": oversized},
    )
    assert resp.status_code == 422


async def test_refine_oversized_risks_returns_422(client: AsyncClient):
    from hub.models import MAX_RISKS

    task = await _create_task(client)
    oversized = [
        {
            "kind": "security",
            "severity": "low",
            "description": f"r-{i}",
            "mitigation": "m",
        }
        for i in range(MAX_RISKS + 1)
    ]
    resp = await client.post(
        f"/api/tasks/{task['id']}/refine",
        json={"risks": oversized},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# POST /api/tasks/refine-bulk
# ---------------------------------------------------------------------------


async def test_refine_bulk_applies_to_many_tasks(client: AsyncClient):
    t1 = await _create_task(client)
    t2 = await _create_task(client)
    resp = await client.post(
        "/api/tasks/refine-bulk",
        json={
            "items": [
                {
                    "task_id": t1["id"],
                    "problem_statement": "ps-1",
                    "size": "M",
                    "acceptance_criteria": [_ac_payload(1)],
                },
                {"task_id": t2["id"], "user_story": "us-2"},
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    results = resp.json()["results"]
    assert len(results) == 2
    by_id = {r["task_id"]: r for r in results}
    assert "problem_statement" in by_id[t1["id"]]["fields_set"]
    assert "acceptance_criteria" in by_id[t1["id"]]["fields_set"]
    assert by_id[t1["id"]]["acceptance_criteria_count"] == 1
    assert by_id[t1["id"]]["readiness_score"] is not None

    # Persisted: re-reading the tasks shows the applied fields.
    got1 = (await client.get(f"/api/tasks/{t1['id']}")).json()
    assert got1["problem_statement"] == "ps-1"
    assert got1["size"] == "M"
    got2 = (await client.get(f"/api/tasks/{t2['id']}")).json()
    assert got2["user_story"] == "us-2"


async def test_refine_bulk_is_atomic_on_missing_task(client: AsyncClient):
    t1 = await _create_task(client)
    resp = await client.post(
        "/api/tasks/refine-bulk",
        json={
            "items": [
                {"task_id": t1["id"], "problem_statement": "should-rollback"},
                {"task_id": 999999, "user_story": "missing"},
            ]
        },
    )
    assert resp.status_code == 404, resp.text
    # The first item must NOT have landed (whole batch rolled back).
    got1 = (await client.get(f"/api/tasks/{t1['id']}")).json()
    assert got1["problem_statement"] in (None, "")


async def test_refine_bulk_duplicate_ac_id_returns_422(client: AsyncClient):
    task = await _create_task(client)
    resp = await client.post(
        "/api/tasks/refine-bulk",
        json={
            "items": [
                {
                    "task_id": task["id"],
                    "acceptance_criteria": [_ac_payload(1), _ac_payload(1)],
                }
            ]
        },
    )
    assert resp.status_code == 422


async def test_refine_bulk_empty_items_returns_422(client: AsyncClient):
    resp = await client.post("/api/tasks/refine-bulk", json={"items": []})
    assert resp.status_code == 422


async def test_refine_bulk_locator_rejection_rolls_back_earlier_items(
    client: AsyncClient, monkeypatch
):
    # #573 moved the locator check ahead of the write inside the function the
    # batch path shares. The batch must still discard EVERY item, including the
    # ones that already validated and belong to a different task — otherwise the
    # reorder would quietly turn an all-or-nothing batch into a partial one.
    monkeypatch.setattr("hub.config.SDD_AC_LOCATOR", "require")
    t1 = await _create_task(client)
    t2 = await _create_task(client)
    resp = await client.post(
        "/api/tasks/refine-bulk",
        json={
            "items": [
                {"task_id": t1["id"], "problem_statement": "should-rollback"},
                {
                    "task_id": t2["id"],
                    "acceptance_criteria": [_ac_payload(1, test_ref=None)],
                },
            ]
        },
    )
    assert resp.status_code == 422, resp.text
    got1 = (await client.get(f"/api/tasks/{t1['id']}")).json()
    assert got1["problem_statement"] in (None, "")


# ---------------------------------------------------------------------------
# acceptance_criteria / risks at child-task creation
# ---------------------------------------------------------------------------


async def test_create_subtasks_bulk_accepts_acceptance_criteria(client: AsyncClient):
    parent = await _create_task(client, task_type="task")
    resp = await client.post(
        f"/api/tasks/{parent['id']}/subtasks",
        json={
            "items": [
                {
                    "title": "Sub with AC",
                    "acceptance_criteria": [_ac_payload(1), _ac_payload(2)],
                    "risks": [
                        {
                            "kind": "security",
                            "severity": "low",
                            "description": "r",
                            "mitigation": "m",
                        }
                    ],
                },
                {"title": "Sub without AC"},
            ],
            "source": "agent",
            "agent": "bot",
        },
    )
    assert resp.status_code == 200, resp.text
    created = resp.json()
    assert len(created) == 2

    acs = (
        await client.get(f"/api/tasks/{created[0]['id']}/acceptance_criteria")
    ).json()
    assert {ac["id"] for ac in acs} == {"AC-1", "AC-2"}
    full = (await client.get(f"/api/tasks/{created[0]['id']}")).json()
    assert len(full["risks"]) == 1
    # Second child has none.
    acs2 = (
        await client.get(f"/api/tasks/{created[1]['id']}/acceptance_criteria")
    ).json()
    assert acs2 == []


# ---------------------------------------------------------------------------
# PUT /api/tasks/{id}/acceptance_criteria/{ac_id} — idempotent upsert
# ---------------------------------------------------------------------------


async def test_upsert_ac_creates_then_updates_idempotently(client: AsyncClient):
    task = await _create_task(client)
    tid = task["id"]
    url = f"/api/tasks/{tid}/acceptance_criteria/AC-1"

    # First call creates (201).
    r1 = await client.put(url, json=_ac_payload(1, then="then-original"))
    assert r1.status_code == 201, r1.text

    # Re-sending a changed payload updates in place (200), no 409.
    r2 = await client.put(url, json=_ac_payload(1, then="then-updated"))
    assert r2.status_code == 200, r2.text
    assert r2.json()["then"] == "then-updated"

    # Exactly one AC remains, with the updated value.
    acs = (await client.get(f"/api/tasks/{tid}/acceptance_criteria")).json()
    assert len(acs) == 1
    assert acs[0]["then"] == "then-updated"


async def test_upsert_ac_rejects_id_mismatch(client: AsyncClient):
    task = await _create_task(client)
    resp = await client.put(
        f"/api/tasks/{task['id']}/acceptance_criteria/AC-9",
        json=_ac_payload(1),
    )
    assert resp.status_code == 422


async def test_upsert_ac_missing_task_returns_404(client: AsyncClient):
    resp = await client.put(
        "/api/tasks/999999/acceptance_criteria/AC-1",
        json=_ac_payload(1),
    )
    assert resp.status_code == 404


async def test_parallel_ac_writes_do_not_500(client: AsyncClient):
    """Regression for feedback #3: concurrent list-append writes on the shared
    connection must serialize, not return sporadic HTTP 500s."""
    task = await _create_task(client)
    tid = task["id"]

    # 12 distinct ACs added concurrently — the write lock serializes them.
    adds = [
        client.post(
            f"/api/tasks/{tid}/acceptance_criteria",
            json=_ac_payload(i),
        )
        for i in range(1, 13)
    ]
    results = await asyncio.gather(*adds)
    assert all(r.status_code in (200, 201, 409) for r in results), [
        r.status_code for r in results
    ]
    assert not any(r.status_code >= 500 for r in results)

    acs = (await client.get(f"/api/tasks/{tid}/acceptance_criteria")).json()
    assert len(acs) == 12

    # Concurrent upserts of the SAME id stay idempotent and never 500.
    upserts = [
        client.put(
            f"/api/tasks/{tid}/acceptance_criteria/AC-1",
            json=_ac_payload(1, then=f"v{i}"),
        )
        for i in range(8)
    ]
    up_results = await asyncio.gather(*upserts)
    assert not any(r.status_code >= 500 for r in up_results)
    acs_after = (await client.get(f"/api/tasks/{tid}/acceptance_criteria")).json()
    assert len(acs_after) == 12  # no duplicates created


async def test_add_ac_duplicate_ac_id_is_idempotent(client: AsyncClient):
    task = await _create_task(client)
    tid = task["id"]
    payload = _ac_payload(1)

    first = await client.post(f"/api/tasks/{tid}/acceptance_criteria", json=payload)
    assert first.status_code == 201

    second = await client.post(f"/api/tasks/{tid}/acceptance_criteria", json=payload)
    assert second.status_code == 200
    assert second.json()["id"] == "AC-1"

    acs = (await client.get(f"/api/tasks/{tid}/acceptance_criteria")).json()
    assert len(acs) == 1


# ---- Persisted readiness (#250) ----

_DOR_READY_PAYLOAD = {
    "work_type": "feature",
    "user_story": "as a user, I want X so that Y",
    "problem_statement": "ps",
    "business_value": "bv",
    "scope_in": ["module"],
    "validation_commands": ["uv run pytest -q"],
    "size": "S",
    "wip_tag": "feature_work",
    "acceptance_criteria": [
        {
            "id": "AC-1",
            "given": "g",
            "when": "w",
            "then": "t",
            "verifiable_by": "test",
        }
    ],
}


async def test_refine_persists_readiness_on_task_row(client: AsyncClient):
    # AC-1 (#250): after refine, score/dor_passed are visible on the task
    # itself — no /readiness call required.
    task = await _create_task(client)
    resp = await client.post(f"/api/tasks/{task['id']}/refine", json=_DOR_READY_PAYLOAD)
    assert resp.status_code == 200, resp.text

    body = (await client.get(f"/api/tasks/{task['id']}")).json()
    assert body["dor_passed"] is True
    assert body["readiness_score"] is not None and body["readiness_score"] > 0
    assert body["ready_at"]


async def test_deleting_required_ac_recomputes_persisted_readiness(
    client: AsyncClient,
):
    # AC-2 (#250): persisted values must not go stale after a regression.
    task = await _create_task(client)
    await client.post(f"/api/tasks/{task['id']}/refine", json=_DOR_READY_PAYLOAD)
    body = (await client.get(f"/api/tasks/{task['id']}")).json()
    assert body["dor_passed"] is True

    resp = await client.delete(f"/api/tasks/{task['id']}/acceptance_criteria/AC-1")
    assert resp.status_code in (200, 204), resp.text

    body = (await client.get(f"/api/tasks/{task['id']}")).json()
    assert body["dor_passed"] is False
    assert body["ready_at"] is None


async def test_get_readiness_lazily_repairs_stale_persisted_values(
    client: AsyncClient, db
):
    from hub import repository as repo_module

    task = await _create_task(client)
    await client.post(f"/api/tasks/{task['id']}/refine", json=_DOR_READY_PAYLOAD)
    # Simulate a legacy row with stale persisted values.
    await repo_module.update_task(
        db, task["id"], readiness_score=None, dor_passed=None, ready_at=None
    )
    await db.commit()

    resp = await client.get(f"/api/tasks/{task['id']}/readiness")
    assert resp.status_code == 200

    body = (await client.get(f"/api/tasks/{task['id']}")).json()
    assert body["dor_passed"] is True
    assert body["readiness_score"] == resp.json()["score"]


# ---- Verifiable SDD: AC test-locator enforcement (#505) ----


async def test_refine_ac_locator_required_rejects_invalid(
    client: AsyncClient, monkeypatch
):
    # AC-1 (#505): with SDD_AC_LOCATOR=require, a verifiable_by=test AC without a
    # valid pytest locator is rejected at refine time with an actionable error.
    monkeypatch.setattr("hub.config.SDD_AC_LOCATOR", "require")
    task = await _create_task(client)
    resp = await client.post(
        f"/api/tasks/{task['id']}/refine",
        json={"acceptance_criteria": [_ac_payload(1, test_ref=None)]},
    )
    assert resp.status_code == 422
    assert "AC-1" in resp.text


async def test_refine_ac_locator_required_allows_non_test(
    client: AsyncClient, monkeypatch
):
    # AC-2 (#505): a non-test AC never requires a locator, even under require.
    monkeypatch.setattr("hub.config.SDD_AC_LOCATOR", "require")
    task = await _create_task(client)
    resp = await client.post(
        f"/api/tasks/{task['id']}/refine",
        json={
            "acceptance_criteria": [
                _ac_payload(1, verifiable_by="manual", test_ref=None)
            ]
        },
    )
    assert resp.status_code == 200


async def test_refine_ac_locator_off_allows_free_text(client: AsyncClient, monkeypatch):
    # Default off (#505): no enforcement — legacy free-text test_ref still refines.
    monkeypatch.setattr("hub.config.SDD_AC_LOCATOR", "off")
    task = await _create_task(client)
    resp = await client.post(
        f"/api/tasks/{task['id']}/refine",
        json={"acceptance_criteria": [_ac_payload(1, test_ref="legacy free text")]},
    )
    assert resp.status_code == 200


# ---- Verifiable SDD: AC locator existence in review brief (#506) ----


async def test_review_brief_includes_locator_resolution(
    client: AsyncClient, db, monkeypatch
):
    # #506: the brief resolves each verifiable_by=test AC's locator against the
    # collected tests — present → resolvable, absent → missing.
    from hub import repository as repo

    async def _fake_collect(_repo_path):
        return {"tests/test_x.py::test_present"}

    async def _fake_head(repo=None):
        return "task-x/b"

    monkeypatch.setattr("hub.app.collect_test_nodeids", _fake_collect)
    monkeypatch.setattr("hub.app.plugins.git_ops.current_branch", _fake_head)
    task = await _create_task(client)
    # Collection is only trusted while the workspace HEAD matches the task
    # branch, so put both on the same branch.
    await repo.update_task(db, task["id"], branch="task-x/b")
    await db.commit()
    await client.post(
        f"/api/tasks/{task['id']}/refine",
        json={
            "acceptance_criteria": [
                _ac_payload(1, test_ref="tests/test_x.py::test_present"),
                _ac_payload(2, test_ref="tests/test_x.py::test_absent"),
            ]
        },
    )
    resp = await client.get(f"/api/tasks/{task['id']}/review-brief")
    assert resp.status_code == 200
    res = {r["ac_id"]: r["status"] for r in resp.json()["locator_resolution"]}
    assert res == {"AC-1": "resolvable", "AC-2": "missing"}


async def test_review_brief_locator_unknown_when_workspace_on_other_branch(
    client: AsyncClient, db, monkeypatch
):
    # #506 fix: the workspace is shared across a project's tasks. When its HEAD
    # sits on another branch the collection says nothing about THIS task, so the
    # status must be `unknown` — never a false `missing`.
    from hub import repository as repo

    called = {"collect": False}

    async def _fake_collect(_repo_path):
        called["collect"] = True
        return {"tests/test_x.py::test_present"}

    async def _fake_head(repo=None):
        return "some-other-task/branch"

    monkeypatch.setattr("hub.app.collect_test_nodeids", _fake_collect)
    monkeypatch.setattr("hub.app.plugins.git_ops.current_branch", _fake_head)
    task = await _create_task(client)
    await repo.update_task(db, task["id"], branch="task-x/b")
    await db.commit()
    await client.post(
        f"/api/tasks/{task['id']}/refine",
        json={
            "acceptance_criteria": [
                _ac_payload(1, test_ref="tests/test_x.py::test_absent")
            ]
        },
    )

    resp = await client.get(f"/api/tasks/{task['id']}/review-brief")
    assert resp.status_code == 200
    res = {r["ac_id"]: r["status"] for r in resp.json()["locator_resolution"]}
    assert res == {"AC-1": "unknown"}
    assert called["collect"] is False  # collection skipped entirely


# ---- Verifiable SDD: AC test results in review brief (#507) ----


async def test_review_brief_shows_ac_test_results(client: AsyncClient, monkeypatch):
    # AC-3 (#507): the brief shows recorded pass/fail per test-AC for the
    # current generation.
    async def _fake_default(nodeids, _repo_path):
        return {n: (i == 0) for i, n in enumerate(nodeids)}

    monkeypatch.setattr("hub.services.ac_tests.default_test_runner", _fake_default)
    task = await _create_task(client)
    await client.post(
        f"/api/tasks/{task['id']}/refine",
        json={
            "acceptance_criteria": [
                _ac_payload(1, test_ref="tests/test_x.py::test_a"),
                _ac_payload(2, test_ref="tests/test_x.py::test_b"),
            ]
        },
    )
    run = await client.post(f"/api/tasks/{task['id']}/run-ac-tests")
    assert run.status_code == 200

    brief = await client.get(f"/api/tasks/{task['id']}/review-brief")
    res = {
        r["ac_id"]: (r["status"], r["is_current"])
        for r in brief.json()["ac_test_results"]
    }
    assert res == {"AC-1": ("pass", True), "AC-2": ("fail", True)}
