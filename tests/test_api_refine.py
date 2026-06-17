"""HTTP-level tests for the structured task form, refine, ACs, and readiness.

These tests exercise the FastAPI routes registered in ``hub.app`` end-to-end
through the in-memory DB ``client`` fixture in ``conftest.py``. They guard
the public contract — request shape, response model, and status codes —
so that future refactors in the service layer cannot silently change it.
"""

from __future__ import annotations

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
        "given": f"given-{idx}",
        "when": f"when-{idx}",
        "then": f"then-{idx}",
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


async def test_add_ac_duplicate_returns_409(client: AsyncClient):
    task = await _create_task(client)
    await client.post(
        f"/api/tasks/{task['id']}/acceptance_criteria", json=_ac_payload(1)
    )
    resp = await client.post(
        f"/api/tasks/{task['id']}/acceptance_criteria", json=_ac_payload(1)
    )
    assert resp.status_code == 409
    assert "AC-1" in resp.text


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
