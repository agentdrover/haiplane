"""Tests for the DoR-gated approve endpoint (#40).

The approve gate enforces Definition of Ready: a task whose required DoR
checks are not satisfied cannot be approved unless ``force=true`` is
explicitly passed. Overrides are allowed but must leave an audit trail
(``alert`` update + activity log marker).
"""

from __future__ import annotations

from httpx import AsyncClient


# ---------------------------------------------------------------------------
# Helpers — mirror the contract from test_api_refine to stay consistent
# ---------------------------------------------------------------------------


async def _create_draft_task(client: AsyncClient, **overrides) -> dict:
    """Create a draft task via the agent-source path so status=='draft'."""
    body = {
        "title": "t",
        "source": "agent",
        "agent": "test",
        **overrides,
    }
    resp = await client.post("/api/tasks", json=body)
    assert resp.status_code == 200, resp.text
    task = resp.json()
    assert task["status"] == "draft", task
    return task


async def _make_dor_ready(client: AsyncClient, task_id: int) -> None:
    """Fill in every required field for the default 'feature' work_type."""
    resp = await client.post(
        f"/api/tasks/{task_id}/refine",
        json={
            "work_type": "feature",
            "user_story": "as a user, I want X so that Y",
            "problem_statement": "ps",
            "business_value": "bv",
            "scope_in": ["a"],
            "validation_commands": ["uv run pytest"],
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
        },
    )
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_approve_of_dor_ready_task_succeeds(client: AsyncClient):
    task = await _create_draft_task(client)
    await _make_dor_ready(client, task["id"])

    resp = await client.post(f"/api/tasks/{task['id']}/approve", json={})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "open"


# ---------------------------------------------------------------------------
# Gate rejects approval when DoR fails
# ---------------------------------------------------------------------------


async def test_approve_of_empty_draft_returns_422_with_structured_detail(
    client: AsyncClient,
):
    task = await _create_draft_task(client)

    resp = await client.post(f"/api/tasks/{task['id']}/approve", json={})
    assert resp.status_code == 422, resp.text

    detail = resp.json()["detail"]
    assert detail["error"] == "dor_failed"
    assert detail["task_id"] == task["id"]
    assert isinstance(detail["score"], int)
    assert "has_user_story" in detail["missing_required"]
    assert "has_acceptance_criteria" in detail["missing_required"]
    # Recommendations must mirror the readiness contract.
    fields = {rec["field"] for rec in detail["recommendations"]}
    assert {"user_story", "acceptance_criteria"} <= fields
    assert detail["hint"].startswith("pass force=true")

    # And the task must NOT have transitioned: it stays in draft.
    get_resp = await client.get(f"/api/tasks/{task['id']}")
    assert get_resp.json()["status"] == "draft"


# ---------------------------------------------------------------------------
# Force override: allowed, but audited
# ---------------------------------------------------------------------------


async def test_force_approve_overrides_gate_and_opens_task(client: AsyncClient):
    task = await _create_draft_task(client)

    resp = await client.post(f"/api/tasks/{task['id']}/approve", json={"force": True})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "open"


async def test_force_approve_leaves_alert_update_with_missing_fields(
    client: AsyncClient,
):
    task = await _create_draft_task(client)

    await client.post(
        f"/api/tasks/{task['id']}/approve",
        json={"force": True, "comment": "we know what we're doing"},
    )

    updates = await client.get(f"/api/tasks/{task['id']}/updates")
    alerts = [u for u in updates.json() if u["kind"] == "alert"]
    assert len(alerts) == 1
    content = alerts[0]["content"]
    assert "DoR failed" in content
    assert "has_user_story" in content
    assert "force=true" in content
    assert "we know what we're doing" in content


async def test_force_approve_marks_activity_log(client: AsyncClient):
    task = await _create_draft_task(client)

    await client.post(f"/api/tasks/{task['id']}/approve", json={"force": True})

    activity = await client.get("/api/activity")
    approves = [a for a in activity.json() if a["kind"] == "task_approved"]
    assert approves, "expected a task_approved entry in activity"
    # The most recent one is ours.
    latest = approves[0]
    assert f"#{task['id']}" in latest["summary"]
    assert "force=true" in latest["summary"]
    assert "missing=" in latest["summary"]


# ---------------------------------------------------------------------------
# Unchanged legacy guardrails
# ---------------------------------------------------------------------------


async def test_approve_of_non_draft_task_still_returns_400(client: AsyncClient):
    task = await _create_draft_task(client)
    await _make_dor_ready(client, task["id"])

    first = await client.post(f"/api/tasks/{task['id']}/approve", json={})
    assert first.status_code == 200

    # Second approve should fail with 400 (status is no longer 'draft').
    second = await client.post(f"/api/tasks/{task['id']}/approve", json={})
    assert second.status_code == 400
    assert "draft" in second.text


async def test_approve_missing_task_returns_404(client: AsyncClient):
    resp = await client.post("/api/tasks/99999/approve", json={})
    assert resp.status_code == 404


async def test_force_approve_when_dor_passes_still_records_override_alert(
    client: AsyncClient,
):
    """Regression for review I7: force=true must always leave a human-
    override audit trail, even if DoR happened to pass anyway. Otherwise
    a postmortem can't tell who deliberately bypassed the gate."""
    task = await _create_draft_task(client)
    await _make_dor_ready(client, task["id"])

    resp = await client.post(
        f"/api/tasks/{task['id']}/approve",
        json={"force": True, "comment": "I'm in a hurry"},
    )
    assert resp.status_code == 200, resp.text

    updates = await client.get(f"/api/tasks/{task['id']}/updates")
    alerts = [u for u in updates.json() if u["kind"] == "alert"]
    assert len(alerts) == 1
    assert "force=true" in alerts[0]["content"]
    assert "I'm in a hurry" in alerts[0]["content"]

    activity = await client.get("/api/activity")
    approves = [a for a in activity.json() if a["kind"] == "task_approved"]
    assert "(force=true)" in approves[0]["summary"]


async def test_concurrent_approve_409_when_status_changed_under_us(
    client: AsyncClient, monkeypatch
):
    """Regression for review I5: if status flips from 'draft' between the
    pre-check read and the conditional UPDATE, approve must return 409."""
    task = await _create_draft_task(client)
    await _make_dor_ready(client, task["id"])

    from hub import repository as repo

    real_transition = repo.transition_status_if

    async def racing_transition(db, task_id, *, expected_from, new_status):
        # Simulate a concurrent writer that closes the window just before
        # our UPDATE lands, by mutating status to something else first.
        await db.execute("UPDATE tasks SET status='rejected' WHERE id=?", (task_id,))
        return await real_transition(
            db, task_id, expected_from=expected_from, new_status=new_status
        )

    monkeypatch.setattr(repo, "transition_status_if", racing_transition)

    resp = await client.post(f"/api/tasks/{task['id']}/approve", json={})
    assert resp.status_code == 409
    assert "no longer draft" in resp.text


async def test_approve_422_missing_required_excludes_optional_checks_for_work_type(
    client: AsyncClient,
):
    """Regression for review I1: 'has_user_story' is optional for bugs,
    so a draft bug task must NOT see it in missing_required even though
    the underlying check failed.
    """
    task = await _create_draft_task(client)
    # Switch the draft to work_type=bug; user_story is not required there.
    refine = await client.post(
        f"/api/tasks/{task['id']}/refine", json={"work_type": "bug"}
    )
    assert refine.status_code == 200, refine.text

    resp = await client.post(f"/api/tasks/{task['id']}/approve", json={})
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    # 'has_user_story' is optional for bugs and must be filtered out.
    assert "has_user_story" not in detail["missing_required"]
    # 'has_problem_statement' IS required for bugs and must be present.
    assert "has_problem_statement" in detail["missing_required"]
