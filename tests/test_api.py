from __future__ import annotations

from httpx import AsyncClient

from hub import repository as repo


async def test_create_task_api(client: AsyncClient):
    resp = await client.post("/api/tasks", json={"title": "API test task"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["title"] == "API test task"
    assert data["status"] == "open"
    assert data["id"] > 0


async def test_create_task_persists_structured_fields(client: AsyncClient):
    """Regression for review C1: POST /api/tasks must persist every
    structured field accepted by the OpenAPI schema. Previously the
    payload reached TaskCreate but lifecycle.create_task only forwarded
    legacy columns to the repo, silently dropping the rest.
    """
    payload = {
        "title": "structured create",
        "source": "agent",
        "agent": "test",
        "work_type": "bug",
        "class_of_service": "expedite",
        "size": "L",
        "wip_tag": "feature_work",
        "user_story": "as a user, I want X so that Y",
        "problem_statement": "the problem",
        "business_value": "the value",
        "scope_in": ["frontend", "api"],
        "scope_out": ["backend"],
        "validation_commands": ["uv run pytest"],
        "constraints": ["no breaking change"],
        "assumptions": ["fastapi >= 0.110"],
        "technical_hints": "use existing helper",
    }
    resp = await client.post("/api/tasks", json=payload)
    assert resp.status_code == 200, resp.text
    task_id = resp.json()["id"]

    fetched = (await client.get(f"/api/tasks/{task_id}")).json()
    for key in (
        "work_type",
        "class_of_service",
        "size",
        "wip_tag",
        "user_story",
        "problem_statement",
        "business_value",
        "scope_in",
        "scope_out",
        "validation_commands",
        "constraints",
        "assumptions",
        "technical_hints",
    ):
        assert fetched[key] == payload[key], (key, fetched[key], payload[key])


async def test_get_task_api(client: AsyncClient):
    create_resp = await client.post("/api/tasks", json={"title": "Get me"})
    task_id = create_resp.json()["id"]

    resp = await client.get(f"/api/tasks/{task_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["title"] == "Get me"
    assert data["id"] == task_id


async def test_list_tasks_api(client: AsyncClient):
    await client.post("/api/tasks", json={"title": "List task 1"})
    await client.post("/api/tasks", json={"title": "List task 2"})

    resp = await client.get("/api/tasks")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 2


async def test_list_tasks_filtered_api(client: AsyncClient):
    await client.post(
        "/api/tasks",
        json={
            "title": "Agent draft",
            "source": "agent",
            "agent": "bot",
        },
    )
    await client.post("/api/tasks", json={"title": "Human open"})

    resp = await client.get("/api/tasks", params={"status": "draft"})
    assert resp.status_code == 200
    data = resp.json()
    assert all(t["status"] == "draft" for t in data)


async def test_approve_api(client: AsyncClient):
    create_resp = await client.post(
        "/api/tasks",
        json={
            "title": "To approve",
            "source": "agent",
        },
    )
    task_id = create_resp.json()["id"]
    assert create_resp.json()["status"] == "draft"

    # Legacy tests bypass the DoR gate introduced in #40 — DoR-aware
    # behavior is covered in test_api_approve_gate.py.
    resp = await client.post(f"/api/tasks/{task_id}/approve", json={"force": True})
    assert resp.status_code == 200
    assert resp.json()["status"] == "open"


async def test_reject_api(client: AsyncClient):
    create_resp = await client.post(
        "/api/tasks",
        json={
            "title": "To reject",
            "source": "agent",
        },
    )
    task_id = create_resp.json()["id"]

    resp = await client.post(
        f"/api/tasks/{task_id}/reject",
        json={"comment": "not needed"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"


async def test_404_not_found(client: AsyncClient):
    resp = await client.get("/api/tasks/99999")
    assert resp.status_code == 404


async def test_approve_non_draft_returns_400(client: AsyncClient):
    create_resp = await client.post("/api/tasks", json={"title": "Open task"})
    task_id = create_resp.json()["id"]
    assert create_resp.json()["status"] == "open"

    resp = await client.post(f"/api/tasks/{task_id}/approve")
    assert resp.status_code == 400


async def test_ask_question_from_open_api(client: AsyncClient):
    create_resp = await client.post("/api/tasks", json={"title": "Open pair task"})
    task_id = create_resp.json()["id"]
    assert create_resp.json()["status"] == "open"

    resp = await client.post(
        f"/api/tasks/{task_id}/question",
        json={"agent": "composer", "question": "Which API surface first?"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "needs_info"
    assert any(u["kind"] == "question" for u in data["updates"])


async def test_answer_question_pair_resume_api(client: AsyncClient):
    create_resp = await client.post("/api/tasks", json={"title": "Pair Q&A API"})
    task_id = create_resp.json()["id"]

    await client.post(
        f"/api/tasks/{task_id}/question",
        json={"agent": "composer", "question": "Scope?"},
    )

    resp = await client.post(
        f"/api/tasks/{task_id}/answer",
        json={"answer": "REST first", "resume": True},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "open"
    assert data["job_id"] is None


async def test_pair_start_api(client: AsyncClient):
    create_resp = await client.post("/api/tasks", json={"title": "Pair API task"})
    task_id = create_resp.json()["id"]

    resp = await client.post(
        f"/api/tasks/{task_id}/pair-start",
        json={
            "plan": "Plan: implement in Cursor",
            "assigned_agent": "composer-analyst",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "running"
    assert data["job_id"] is None
    assert data["branch"] == f"task-{task_id}/pair-api-task"
    assert data["assigned_agent"] == "composer-analyst"


async def test_claim_task_api(client: AsyncClient):
    create_resp = await client.post("/api/tasks", json={"title": "Claim API"})
    task_id = create_resp.json()["id"]

    resp = await client.post(
        f"/api/tasks/{task_id}/claim",
        json={"agent": "composer", "session_id": "sess-a"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "claimed"
    assert data["claimed_by"] == "composer"

    conflict = await client.post(
        f"/api/tasks/{task_id}/claim",
        json={"agent": "other", "session_id": "sess-b"},
    )
    assert conflict.status_code == 409


async def test_release_task_api(client: AsyncClient):
    create_resp = await client.post("/api/tasks", json={"title": "Release API"})
    task_id = create_resp.json()["id"]
    await client.post(
        f"/api/tasks/{task_id}/claim",
        json={"agent": "composer", "session_id": "sess-a"},
    )

    resp = await client.post(
        f"/api/tasks/{task_id}/release",
        json={"agent": "composer", "session_id": "sess-a"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "open"


async def test_force_complete_api(client: AsyncClient, db):
    create_resp = await client.post("/api/tasks", json={"title": "Pending report"})
    task_id = create_resp.json()["id"]
    await repo.update_task(db, task_id, status="pending_report")
    await db.commit()

    resp = await client.post(f"/api/tasks/{task_id}/force-complete")

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "completed"
    assert any(
        update["kind"] == "done" and "Force-completed by human" in update["content"]
        for update in data["updates"]
    )


async def test_force_complete_api_rejects_wrong_status(client: AsyncClient):
    create_resp = await client.post("/api/tasks", json={"title": "Open task"})
    task_id = create_resp.json()["id"]

    resp = await client.post(f"/api/tasks/{task_id}/force-complete")

    assert resp.status_code == 400
    assert "pending_report" in resp.text


async def test_force_complete_api_records_human_comment(client: AsyncClient, db):
    create_resp = await client.post("/api/tasks", json={"title": "Pending report"})
    task_id = create_resp.json()["id"]
    await repo.update_task(db, task_id, status="pending_report")
    await db.commit()

    resp = await client.post(
        f"/api/tasks/{task_id}/force-complete",
        json={"comment": "reviewed manually, accepting risk"},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "completed"
    done_updates = [u for u in data["updates"] if u["kind"] == "done"]
    assert len(done_updates) == 1
    assert done_updates[0]["content"] == "reviewed manually, accepting risk"
    assert done_updates[0]["agent"] == "human"


async def test_task_updates_api(client: AsyncClient):
    create_resp = await client.post("/api/tasks", json={"title": "With updates"})
    task_id = create_resp.json()["id"]

    resp = await client.post(
        f"/api/tasks/{task_id}/updates",
        json={
            "agent": "dev",
            "kind": "status",
            "content": "Working on it",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["kind"] == "status"

    resp = await client.get(f"/api/tasks/{task_id}/updates")
    assert resp.status_code == 200
    assert len(resp.json()) >= 1


async def test_activity_api(client: AsyncClient):
    await client.post("/api/tasks", json={"title": "Activity trigger"})

    resp = await client.get("/api/activity")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)


async def test_create_task_with_type_api(client: AsyncClient):
    resp = await client.post(
        "/api/tasks",
        json={
            "title": "My feature",
            "task_type": "epic",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["task_type"] == "epic"
    assert data["status"] == "open"


async def test_create_subtasks_bulk_api(client: AsyncClient):
    parent = await client.post(
        "/api/tasks",
        json={"title": "Parent task", "task_type": "task"},
    )
    assert parent.status_code == 200
    parent_id = parent.json()["id"]

    resp = await client.post(
        f"/api/tasks/{parent_id}/subtasks",
        json={
            "items": [
                {"title": "Sub A", "description": "first"},
                {"title": "Sub B", "priority": "high"},
            ],
            "source": "agent",
            "agent": "bot",
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert len(data) == 2
    assert all(t["task_type"] == "subtask" for t in data)
    assert all(t["status"] == "draft" for t in data)
    assert all(t["parent_id"] == parent_id for t in data)
    assert data[0]["title"] == "Sub A"
    assert data[1]["priority"] == "high"


async def test_create_subtasks_bulk_rejects_invalid_parent(client: AsyncClient):
    leaf = await client.post(
        "/api/tasks",
        json={"title": "Leaf", "task_type": "task"},
    )
    parent_id = leaf.json()["id"]
    sub = await client.post(
        "/api/tasks",
        json={
            "title": "Sub",
            "task_type": "subtask",
            "parent_id": parent_id,
        },
    )
    assert sub.status_code == 200
    sub_id = sub.json()["id"]

    resp = await client.post(
        f"/api/tasks/{sub_id}/subtasks",
        json={"items": [{"title": "Nope"}]},
    )
    assert resp.status_code == 400


async def test_create_subtasks_bulk_rejects_too_many_items(client: AsyncClient):
    parent = await client.post("/api/tasks", json={"title": "Parent"})
    parent_id = parent.json()["id"]
    items = [{"title": f"Item {i}"} for i in range(21)]
    resp = await client.post(
        f"/api/tasks/{parent_id}/subtasks",
        json={"items": items},
    )
    assert resp.status_code == 422


async def test_task_tree_api(client: AsyncClient):
    epic_resp = await client.post(
        "/api/tasks",
        json={
            "title": "Parent epic",
            "task_type": "epic",
        },
    )
    epic_id = epic_resp.json()["id"]

    resp = await client.get(f"/api/tasks/{epic_id}/tree")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == epic_id
    assert data["task_type"] == "epic"


async def test_task_context_api(client: AsyncClient):
    resp = await client.post("/api/tasks", json={"title": "Context task"})
    task_id = resp.json()["id"]

    resp = await client.get(f"/api/tasks/{task_id}/context")
    assert resp.status_code == 200
    data = resp.json()
    assert data["task_id"] == task_id
    assert "context_text" in data


async def test_create_task_with_owner_reviewer_api(client: AsyncClient):
    resp = await client.post(
        "/api/tasks",
        json={"title": "Owned task", "human_owner": "alice", "human_reviewer": "bob"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["human_owner"] == "alice"
    assert data["human_reviewer"] == "bob"

    fetched = (await client.get(f"/api/tasks/{data['id']}")).json()
    assert fetched["human_owner"] == "alice"
    assert fetched["human_reviewer"] == "bob"


async def test_list_tasks_filtered_by_human_owner_api(client: AsyncClient):
    await client.post(
        "/api/tasks",
        json={"title": "Alice task", "human_owner": "alice"},
    )
    await client.post(
        "/api/tasks",
        json={"title": "Bob task", "human_owner": "bob"},
    )

    resp = await client.get("/api/tasks", params={"human_owner": "alice"})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 1
    assert all(t["human_owner"] == "alice" for t in data)


async def test_list_tasks_filtered_by_claimed_by_api(client: AsyncClient):
    owned = await client.post(
        "/api/tasks",
        json={"title": "Claimed task", "human_owner": "bob"},
    )
    task_id = owned.json()["id"]
    await client.post(
        f"/api/tasks/{task_id}/claim",
        json={"agent": "composer", "session_id": "sess-1"},
    )

    other = await client.post(
        "/api/tasks",
        json={"title": "Other claim", "human_owner": "bob"},
    )
    other_id = other.json()["id"]
    await client.post(
        f"/api/tasks/{other_id}/claim",
        json={"agent": "other-agent", "session_id": "sess-2"},
    )

    resp = await client.get("/api/tasks", params={"claimed_by": "composer"})
    assert resp.status_code == 200
    data = resp.json()
    assert any(t["id"] == task_id for t in data)
    assert all(t.get("claimed_by") == "composer" for t in data)


async def test_list_tasks_filtered_by_mine_api(client: AsyncClient):
    owned = await client.post(
        "/api/tasks",
        json={"title": "Alice owned", "human_owner": "alice"},
    )
    owned_id = owned.json()["id"]

    claimed = await client.post(
        "/api/tasks",
        json={"title": "Bob owned claimed by alice", "human_owner": "bob"},
    )
    claimed_id = claimed.json()["id"]
    await client.post(
        f"/api/tasks/{claimed_id}/claim",
        json={"agent": "alice", "session_id": "sess-a"},
    )

    await client.post(
        "/api/tasks",
        json={"title": "Charlie only", "human_owner": "charlie"},
    )

    resp = await client.get("/api/tasks", params={"mine": "alice"})
    assert resp.status_code == 200
    ids = {t["id"] for t in resp.json()}
    assert owned_id in ids
    assert claimed_id in ids


async def test_reorder_task_api(client: AsyncClient):
    resp = await client.post("/api/tasks", json={"title": "Reorder me"})
    task_id = resp.json()["id"]

    resp = await client.patch(
        f"/api/tasks/{task_id}/reorder",
        json={"position": 5},
    )
    assert resp.status_code == 200
    assert resp.json()["position"] == 5


async def test_decide_task_accept_api(client: AsyncClient, db):
    resp = await client.post("/api/tasks", json={"title": "Decide me"})
    task_id = resp.json()["id"]
    await repo.update_task(db, task_id, status="needs_decision")
    await db.commit()

    resp = await client.post(
        f"/api/tasks/{task_id}/decide",
        json={"action": "accept"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "completed"


async def test_decide_task_with_summary_api(client: AsyncClient, db):
    resp = await client.post("/api/tasks", json={"title": "Decide summary"})
    task_id = resp.json()["id"]
    await repo.update_task(db, task_id, status="needs_decision")
    await db.commit()

    resp = await client.post(
        f"/api/tasks/{task_id}/decide",
        json={
            "action": "accept",
            "decision_summary": "Accepted: review comments are cosmetic.",
            "record_decision": False,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "completed"
    decision_updates = [u for u in data["updates"] if u["kind"] == "decision"]
    assert len(decision_updates) == 1
    assert "cosmetic" in decision_updates[0]["content"]


async def test_decide_task_rework_with_summary_api(client: AsyncClient, db):
    resp = await client.post("/api/tasks", json={"title": "Decide rework"})
    task_id = resp.json()["id"]
    await repo.update_task(db, task_id, status="needs_decision")
    await db.commit()

    resp = await client.post(
        f"/api/tasks/{task_id}/decide",
        json={
            "action": "rework",
            "instructions": "Fix the auth bug.",
            "decision_summary": "Auth bug must be resolved before merge.",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] in ("fix_requested", "open")
    decision_updates = [u for u in data["updates"] if u["kind"] == "decision"]
    assert len(decision_updates) == 1
    assert "Auth bug must be resolved" in decision_updates[0]["content"]


async def test_decide_task_record_decision_api(client: AsyncClient, db):
    """record_decision=True should not break flow with noop notes adapter."""
    resp = await client.post("/api/tasks", json={"title": "Record decision"})
    task_id = resp.json()["id"]
    await repo.update_task(db, task_id, status="needs_decision")
    await db.commit()

    resp = await client.post(
        f"/api/tasks/{task_id}/decide",
        json={
            "action": "accept",
            "decision_summary": "Ship it.",
            "record_decision": True,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "completed"


async def test_archive_task_hidden_from_list(client: AsyncClient):
    create_resp = await client.post("/api/tasks", json={"title": "Archivable"})
    task_id = create_resp.json()["id"]

    resp = await client.post(f"/api/tasks/{task_id}/archive", json={"cascade": True})
    assert resp.status_code == 200
    assert resp.json()["archived"] is True

    listed = (await client.get("/api/tasks")).json()
    assert all(t["id"] != task_id for t in listed)

    with_arch = (
        await client.get("/api/tasks", params={"include_archived": "true"})
    ).json()
    assert any(t["id"] == task_id for t in with_arch)


async def test_delete_task_returns_204(client: AsyncClient):
    create_resp = await client.post("/api/tasks", json={"title": "To delete"})
    task_id = create_resp.json()["id"]

    resp = await client.delete(f"/api/tasks/{task_id}")
    assert resp.status_code == 204

    get_resp = await client.get(f"/api/tasks/{task_id}")
    assert get_resp.status_code == 404


# ---------------------------------------------------------------------------
# Withdraw own draft (#173)
# ---------------------------------------------------------------------------


def _withdraw_tokens() -> dict:
    from hub.config import TokenIdentity

    return {
        "agent-token": TokenIdentity("bot", "agent"),
        "other-agent": TokenIdentity("other", "agent"),
        "human-token": TokenIdentity("denis", "human"),
    }


async def test_withdraw_own_draft_archives_and_hides_from_lists(
    client: AsyncClient, monkeypatch
):
    from hub import config

    monkeypatch.setattr(config, "HUB_TOKENS", _withdraw_tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    headers = {"Authorization": "Bearer agent-token"}

    create = await client.post(
        "/api/tasks",
        json={"title": "mistaken draft", "source": "agent", "agent": "bot"},
        headers=headers,
    )
    assert create.status_code == 200
    task_id = create.json()["id"]
    assert create.json()["status"] == "draft"

    resp = await client.post(f"/api/tasks/{task_id}/withdraw", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["archived"] is True
    assert body["status"] == "draft"

    listed = (
        await client.get("/api/tasks", params={"status": "draft"}, headers=headers)
    ).json()
    assert all(t["id"] != task_id for t in listed)

    activity = (await client.get("/api/activity", headers=headers)).json()
    withdrawn = [a for a in activity if a["kind"] == "task_withdrawn"]
    assert withdrawn
    assert "bot" in withdrawn[0]["summary"]
    assert f"#{task_id}" in withdrawn[0]["summary"]


async def test_withdraw_own_draft_rejects_foreign_or_invalid(
    client: AsyncClient, monkeypatch, db
):
    from hub import config
    from hub import repository as repo

    monkeypatch.setattr(config, "HUB_TOKENS", _withdraw_tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    agent_headers = {"Authorization": "Bearer agent-token"}
    other_headers = {"Authorization": "Bearer other-agent"}
    human_headers = {"Authorization": "Bearer human-token"}

    create = await client.post(
        "/api/tasks",
        json={"title": "owned draft", "source": "agent", "agent": "bot"},
        headers=agent_headers,
    )
    task_id = create.json()["id"]

    wrong_owner = await client.post(
        f"/api/tasks/{task_id}/withdraw", headers=other_headers
    )
    assert wrong_owner.status_code == 403
    detail = wrong_owner.json()["detail"]
    assert detail["reason"] == "not_task_owner"
    assert detail["required_role"] == "agent"
    assert detail["hint"]
    assert detail["instance"] in ("prod", "local")
    assert "next_action" in detail

    await repo.update_task(db, task_id, status="open")
    await db.commit()
    not_draft = await client.post(
        f"/api/tasks/{task_id}/withdraw", headers=agent_headers
    )
    assert not_draft.status_code == 403
    detail = not_draft.json()["detail"]
    assert detail["reason"] == "invalid_status_for_withdraw"
    assert detail["required_status"] == "draft"

    human_open = await client.post(
        "/api/tasks",
        json={"title": "human task"},
        headers=human_headers,
    )
    human_id = human_open.json()["id"]
    not_agent_source = await client.post(
        f"/api/tasks/{human_id}/withdraw", headers=agent_headers
    )
    assert not_agent_source.status_code == 403
    assert not_agent_source.json()["detail"]["reason"] == "not_agent_draft"

    await repo.update_task(db, task_id, status="draft")
    await db.commit()
    child = await client.post(
        "/api/tasks",
        json={
            "title": "child",
            "source": "agent",
            "agent": "bot",
            "parent_id": task_id,
            "task_type": "subtask",
        },
        headers=agent_headers,
    )
    assert child.status_code == 200
    has_children = await client.post(
        f"/api/tasks/{task_id}/withdraw", headers=agent_headers
    )
    assert has_children.status_code == 403
    assert has_children.json()["detail"]["reason"] == "withdraw_has_children"


async def test_withdraw_rejects_human_token(client: AsyncClient, monkeypatch):
    from hub import config

    monkeypatch.setattr(config, "HUB_TOKENS", _withdraw_tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)

    create = await client.post(
        "/api/tasks",
        json={"title": "draft", "source": "agent", "agent": "bot"},
        headers={"Authorization": "Bearer human-token"},
    )
    task_id = create.json()["id"]

    resp = await client.post(
        f"/api/tasks/{task_id}/withdraw",
        headers={"Authorization": "Bearer human-token"},
    )
    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert detail["reason"] == "withdraw_agent_only"
    assert detail["suggested_tool"] == "hub_archive_task"
    assert detail["required_role"] == "agent"
    assert detail["instance"] in ("prod", "local")
    assert "next_action" in detail


async def test_withdraw_own_draft_allows_archived_child(
    client: AsyncClient, monkeypatch, db
):
    from hub import config

    monkeypatch.setattr(config, "HUB_TOKENS", _withdraw_tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    agent_headers = {"Authorization": "Bearer agent-token"}
    human_headers = {"Authorization": "Bearer human-token"}

    parent = await client.post(
        "/api/tasks",
        json={"title": "parent draft", "source": "agent", "agent": "bot"},
        headers=agent_headers,
    )
    parent_id = parent.json()["id"]

    child = await client.post(
        "/api/tasks",
        json={
            "title": "child",
            "source": "agent",
            "agent": "bot",
            "parent_id": parent_id,
            "task_type": "subtask",
        },
        headers=agent_headers,
    )
    child_id = child.json()["id"]

    archive_child = await client.post(
        f"/api/tasks/{child_id}/archive",
        json={"cascade": False},
        headers=human_headers,
    )
    assert archive_child.status_code == 200

    resp = await client.post(f"/api/tasks/{parent_id}/withdraw", headers=agent_headers)
    assert resp.status_code == 200
    assert resp.json()["archived"] is True


async def test_withdraw_rejects_empty_assigned_agent(client: AsyncClient, monkeypatch):
    from hub import config

    monkeypatch.setattr(config, "HUB_TOKENS", _withdraw_tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    headers = {"Authorization": "Bearer agent-token"}

    create = await client.post(
        "/api/tasks",
        json={"title": "no agent field", "source": "agent"},
        headers=headers,
    )
    task_id = create.json()["id"]
    assert create.json()["assigned_agent"] == ""

    resp = await client.post(f"/api/tasks/{task_id}/withdraw", headers=headers)
    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert detail["reason"] == "not_task_owner"
    assert detail["required_role"] == "agent"


# ---- Universal Review Gate (#308): review brief ----


async def test_review_brief_api(client: AsyncClient):
    resp = await client.post(
        "/api/tasks", json={"title": "Brief me", "description": "the work"}
    )
    task_id = resp.json()["id"]

    resp = await client.post(
        f"/api/tasks/{task_id}/refine",
        json={
            "scope_in": ["hub/app.py"],
            "scope_out": ["web ui"],
            "validation_commands": ["uv run pytest -q"],
            "review_checklist": ["check findings are structured"],
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

    await client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": "dev", "kind": "status", "content": "Plan: implement"},
    )
    resp = await client.post(
        f"/api/tasks/{task_id}/pair-start", json={"assigned_agent": "dev"}
    )
    assert resp.status_code == 200, resp.text
    branch = resp.json()["branch"]

    resp = await client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": "dev", "kind": "done", "content": "Implemented the thing"},
    )
    assert resp.status_code == 200, resp.text

    resp = await client.get(f"/api/tasks/{task_id}/review-brief")
    assert resp.status_code == 200
    brief = resp.json()
    assert brief["task_id"] == task_id
    assert brief["title"] == "Brief me"
    assert [ac["id"] for ac in brief["acceptance_criteria"]] == ["AC-1"]
    assert brief["scope_in"] == ["hub/app.py"]
    assert brief["scope_out"] == ["web ui"]
    assert brief["validation_commands"] == ["uv run pytest -q"]
    assert brief["review_checklist"] == ["check findings are structured"]
    assert brief["branch"] == branch
    assert branch in brief["diff_command"]
    assert brief["submission_generation"] == 1
    assert brief["latest_submission_summary"] == "Implemented the thing"
    assert brief["latest_review"] is None


async def test_review_brief_works_without_branch_or_pr(client: AsyncClient):
    resp = await client.post("/api/tasks", json={"title": "Local brief"})
    task_id = resp.json()["id"]

    resp = await client.get(f"/api/tasks/{task_id}/review-brief")
    assert resp.status_code == 200
    brief = resp.json()
    assert brief["branch"] is None
    assert brief["pr_number"] is None
    assert brief["diff_command"] == ""
    assert brief["latest_submission_summary"] == ""


async def test_review_brief_404(client: AsyncClient):
    resp = await client.get("/api/tasks/99999/review-brief")
    assert resp.status_code == 404
