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


async def test_force_complete_api_rejects_terminal_status(client: AsyncClient, db):
    create_resp = await client.post("/api/tasks", json={"title": "Completed task"})
    task_id = create_resp.json()["id"]
    await repo.update_task(db, task_id, status="completed")
    await db.commit()

    resp = await client.post(f"/api/tasks/{task_id}/force-complete")

    assert resp.status_code == 400
    assert "terminal" in resp.text
    assert "completed" in resp.text


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
    assert done_updates[0]["content"].startswith("reviewed manually, accepting risk")
    assert "from_status=pending_report" in done_updates[0]["content"]
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


# ---- Universal Review Gate (#307): review API operations ----


async def _running_pair_task(client: AsyncClient, title: str) -> int:
    resp = await client.post("/api/tasks", json={"title": title})
    task_id = resp.json()["id"]
    await client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": "dev", "kind": "status", "content": "Plan: implement"},
    )
    resp = await client.post(
        f"/api/tasks/{task_id}/pair-start", json={"assigned_agent": "dev"}
    )
    assert resp.status_code == 200, resp.text
    return task_id


async def test_submit_review_api_enters_review(client: AsyncClient):
    task_id = await _running_pair_task(client, "Submit via API")

    resp = await client.post(
        f"/api/tasks/{task_id}/submit-review",
        json={"agent": "dev", "summary": "ready"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "review"
    assert body["submission_generation"] == 1
    assert body["review_job_id"] is None


async def test_submit_review_api_rejected_from_open(client: AsyncClient):
    resp = await client.post("/api/tasks", json={"title": "Never started"})
    task_id = resp.json()["id"]
    resp = await client.post(f"/api/tasks/{task_id}/submit-review", json={})
    assert resp.status_code == 400


async def test_review_verdict_api_changes_requested_returns_to_running(
    client: AsyncClient,
):
    task_id = await _running_pair_task(client, "Verdict loop")
    await client.post(f"/api/tasks/{task_id}/submit-review", json={})

    resp = await client.post(
        f"/api/tasks/{task_id}/review-verdict",
        json={
            "verdict": "changes_requested",
            "agent": "reviewer",
            "findings": [{"id": 1, "severity": "high", "message": "Fix the race"}],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "running"
    assert body["review_cycle"] == 1
    assert body["latest_review"]["verdict"] == "changes_requested"
    assert body["latest_review"]["findings"][0]["message"] == "Fix the race"
    assert body["review_approved_current"] is False


async def test_review_verdict_api_approved_returns_to_running_current(
    client: AsyncClient,
):
    task_id = await _running_pair_task(client, "Approve loop")
    await client.post(f"/api/tasks/{task_id}/submit-review", json={})

    resp = await client.post(
        f"/api/tasks/{task_id}/review-verdict",
        json={"verdict": "approved", "agent": "reviewer"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "running"
    assert body["review_cycle"] == 0
    assert body["review_approved_current"] is True


async def test_review_verdict_api_rejects_without_submission(client: AsyncClient):
    task_id = await _running_pair_task(client, "No submission yet")
    resp = await client.post(
        f"/api/tasks/{task_id}/review-verdict", json={"verdict": "approved"}
    )
    assert resp.status_code == 400


async def test_review_verdict_api_rejects_invalid_verdict(client: AsyncClient):
    task_id = await _running_pair_task(client, "Bad verdict")
    await client.post(f"/api/tasks/{task_id}/submit-review", json={})
    resp = await client.post(
        f"/api/tasks/{task_id}/review-verdict", json={"verdict": "maybe"}
    )
    assert resp.status_code == 422


# ---- Regression coverage for Universal Review Gate (#311) ----


async def test_full_rest_review_cycle_completes_only_after_approval(
    client: AsyncClient,
):
    # AC-1 (#311): the complete REST loop — done blocked, verdict, done passes.
    task_id = await _running_pair_task(client, "Full REST gate cycle")

    # Unreviewed done report does not complete: routed by the gate.
    resp = await client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": "dev", "kind": "done", "content": "v1"},
    )
    assert resp.status_code == 200
    resp = await client.get(f"/api/tasks/{task_id}")
    body = resp.json()
    assert body["status"] in ("review", "ci_check")  # branch → ci_check path
    assert body["status"] != "completed"

    # Route to client-driven review deterministically: drop branch, rerun.
    task_id2 = await _running_pair_task(client, "Full REST gate cycle no branch")

    # (db access through the app state is not available here; use the API
    # instead: submit-review makes the review path explicit)
    resp = await client.post(
        f"/api/tasks/{task_id2}/submit-review", json={"agent": "dev"}
    )
    assert resp.json()["status"] == "review"

    resp = await client.post(
        f"/api/tasks/{task_id2}/review-verdict",
        json={"verdict": "approved", "agent": "reviewer"},
    )
    body = resp.json()
    assert body["status"] == "running"
    assert body["review_approved_current"] is True

    resp = await client.post(
        f"/api/tasks/{task_id2}/updates",
        json={"agent": "dev", "kind": "done", "content": "approved work"},
    )
    assert resp.status_code == 200
    resp = await client.get(f"/api/tasks/{task_id2}")
    assert resp.json()["status"] == "completed"


# ---- Separation of duties: no self-approve (#318) ----


def _review_tokens() -> dict:
    from hub.config import TokenIdentity

    return {
        "impl-token": TokenIdentity("impl-bot", "agent"),
        "reviewer-token": TokenIdentity("reviewer-bot", "agent"),
        "human-token": TokenIdentity("denis", "human"),
    }


async def _task_in_review(client: AsyncClient, title: str, headers: dict) -> int:
    resp = await client.post("/api/tasks", json={"title": title}, headers=headers)
    task_id = resp.json()["id"]
    await client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": "impl-bot", "kind": "status", "content": "Plan: work"},
        headers=headers,
    )
    resp = await client.post(
        f"/api/tasks/{task_id}/pair-start",
        json={"assigned_agent": "impl-bot"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    resp = await client.post(
        f"/api/tasks/{task_id}/submit-review", json={}, headers=headers
    )
    assert resp.json()["status"] == "review"
    return task_id


async def test_self_review_verdict_rejected(client: AsyncClient, monkeypatch):
    from hub import config

    monkeypatch.setattr(config, "HUB_TOKENS", _review_tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    monkeypatch.setattr(config, "REVIEW_SELF_APPROVE", "forbid")
    impl = {"Authorization": "Bearer impl-token"}

    task_id = await _task_in_review(client, "Self review blocked", impl)

    resp = await client.post(
        f"/api/tasks/{task_id}/review-verdict",
        json={"verdict": "approved", "agent": "someone-else"},
        headers=impl,
    )
    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert detail["reason"] == "self_review_forbidden"
    assert "independent reviewer" in detail["hint"]
    # Verdict must not be recorded.
    resp = await client.get(f"/api/tasks/{task_id}", headers=impl)
    body = resp.json()
    assert body["review_verdict"] is None
    assert body["status"] == "review"


async def test_other_agent_and_human_verdicts_pass(client: AsyncClient, monkeypatch):
    from hub import config

    monkeypatch.setattr(config, "HUB_TOKENS", _review_tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    monkeypatch.setattr(config, "REVIEW_SELF_APPROVE", "forbid")
    impl = {"Authorization": "Bearer impl-token"}
    reviewer = {"Authorization": "Bearer reviewer-token"}
    human = {"Authorization": "Bearer human-token"}

    task_id = await _task_in_review(client, "Independent review ok", impl)
    resp = await client.post(
        f"/api/tasks/{task_id}/review-verdict",
        json={"verdict": "changes_requested", "agent": "reviewer-bot"},
        headers=reviewer,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "running"

    # Resubmit and let the human approve.
    resp = await client.post(
        f"/api/tasks/{task_id}/submit-review", json={}, headers=impl
    )
    assert resp.json()["status"] == "review"
    resp = await client.post(
        f"/api/tasks/{task_id}/review-verdict",
        json={"verdict": "approved", "agent": "denis"},
        headers=human,
    )
    assert resp.status_code == 200
    assert resp.json()["review_approved_current"] is True


async def test_self_review_allowed_with_solo_opt_out(client: AsyncClient, monkeypatch):
    from hub import config

    monkeypatch.setattr(config, "HUB_TOKENS", _review_tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    monkeypatch.setattr(config, "REVIEW_SELF_APPROVE", "allow")
    impl = {"Authorization": "Bearer impl-token"}

    task_id = await _task_in_review(client, "Solo mode", impl)
    resp = await client.post(
        f"/api/tasks/{task_id}/review-verdict",
        json={"verdict": "approved", "agent": "impl-bot"},
        headers=impl,
    )
    assert resp.status_code == 200
    assert resp.json()["review_approved_current"] is True


# ---- Implementer principal binding (#320) ----


def _principal_tokens() -> dict:
    from hub.config import TokenIdentity

    return {
        # Same principal id 7, different display username than assigned_agent.
        "impl-pid-token": TokenIdentity("display-name-x", "agent", principal_id=7),
        "other-pid-token": TokenIdentity("reviewer-y", "agent", principal_id=8),
        "human-token": TokenIdentity("denis", "human"),
    }


async def test_self_review_blocked_by_principal_despite_name_mismatch(
    client: AsyncClient, monkeypatch
):
    # AC-1: the implementer principal is rejected even when assigned_agent
    # holds a different free-text name than the token's username.
    from hub import config

    monkeypatch.setattr(config, "HUB_TOKENS", _principal_tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    monkeypatch.setattr(config, "REVIEW_SELF_APPROVE", "forbid")
    impl = {"Authorization": "Bearer impl-pid-token"}

    resp = await client.post(
        "/api/tasks", json={"title": "Principal gate"}, headers=impl
    )
    task_id = resp.json()["id"]
    await client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": "someone", "kind": "status", "content": "Plan: work"},
        headers=impl,
    )
    # Pair-start with an assigned_agent name that does NOT match the token
    # username — exactly the prod scenario that motivated #320.
    resp = await client.post(
        f"/api/tasks/{task_id}/pair-start",
        json={"assigned_agent": "claude-code"},
        headers=impl,
    )
    assert resp.status_code == 200, resp.text
    resp = await client.post(
        f"/api/tasks/{task_id}/submit-review", json={}, headers=impl
    )
    assert resp.json()["status"] == "review"

    resp = await client.post(
        f"/api/tasks/{task_id}/review-verdict",
        json={"verdict": "approved", "agent": "claude-code"},
        headers=impl,
    )
    assert resp.status_code == 403
    assert resp.json()["detail"]["reason"] == "self_review_forbidden"

    # AC-3: a different principal passes.
    other = {"Authorization": "Bearer other-pid-token"}
    resp = await client.post(
        f"/api/tasks/{task_id}/review-verdict",
        json={"verdict": "approved", "agent": "reviewer-y"},
        headers=other,
    )
    assert resp.status_code == 200
    assert resp.json()["review_approved_current"] is True


async def test_claim_records_and_release_clears_implementer_principal(
    client: AsyncClient, monkeypatch, db
):
    from hub import config
    from hub import repository as repo_module

    monkeypatch.setattr(config, "HUB_TOKENS", _principal_tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    impl = {"Authorization": "Bearer impl-pid-token"}

    resp = await client.post(
        "/api/tasks", json={"title": "Claim principal"}, headers=impl
    )
    task_id = resp.json()["id"]
    resp = await client.post(
        f"/api/tasks/{task_id}/claim", json={"agent": "display-name-x"}, headers=impl
    )
    assert resp.status_code == 200
    d = dict(await repo_module.get_task(db, task_id))
    assert d["implementer_principal_id"] == 7

    resp = await client.post(
        f"/api/tasks/{task_id}/release",
        json={"agent": "display-name-x"},
        headers=impl,
    )
    assert resp.status_code == 200
    d = dict(await repo_module.get_task(db, task_id))
    assert d["implementer_principal_id"] is None


# ---- Batch approve (#252) ----


async def _make_dor_ready_draft(client: AsyncClient, title: str) -> int:
    resp = await client.post(
        "/api/tasks", json={"title": title, "source": "agent", "agent": "bot"}
    )
    task_id = resp.json()["id"]
    resp = await client.post(
        f"/api/tasks/{task_id}/refine",
        json={
            "work_type": "feature",
            "user_story": "as a user, I want X so that Y",
            "problem_statement": "ps",
            "business_value": "bv",
            "scope_in": ["m"],
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
        },
    )
    assert resp.status_code == 200, resp.text
    return task_id


async def test_batch_approve_mixed_queue(client: AsyncClient, monkeypatch):
    # AC-1 (#252): ready drafts approved, unready skipped with reasons.
    from hub import config

    ready_id = await _make_dor_ready_draft(client, "Ready draft")
    resp = await client.post(
        "/api/tasks", json={"title": "Bare draft", "source": "agent", "agent": "bot"}
    )
    bare_id = resp.json()["id"]
    risky_id = await _make_dor_ready_draft(client, "Risky draft")
    await client.post(
        f"/api/tasks/{risky_id}/risks",
        json={
            "kind": "security",
            "severity": "high",
            "description": "d",
            "mitigation": "m",
        },
    )

    monkeypatch.setattr(config, "HUB_TOKENS", _review_tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    human = {"Authorization": "Bearer human-token"}
    resp = await client.post(
        "/api/tasks/batch-approve",
        json={"task_ids": [ready_id, bare_id, risky_id, 99999]},
        headers=human,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["approved"] == [ready_id]
    reasons = {s["task_id"]: s["reason"] for s in body["skipped"]}
    assert reasons[bare_id] == "dor_failed"
    assert reasons[risky_id] == "high_risk"
    assert reasons[99999] == "not_found"

    status_now = (await client.get(f"/api/tasks/{ready_id}", headers=human)).json()
    assert status_now["status"] == "open"


async def test_batch_approve_agent_token_forbidden(client: AsyncClient, monkeypatch):
    # AC-2 (#252): human-only gate.
    from hub import config

    monkeypatch.setattr(config, "HUB_TOKENS", _review_tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    agent = {"Authorization": "Bearer impl-token"}

    resp = await client.post(
        "/api/tasks/batch-approve", json={"task_ids": [1]}, headers=agent
    )
    assert resp.status_code == 403
    assert resp.json()["detail"]["reason"] == "human_only_gate"


# ---- Cursor pagination and summary mode (#254) ----


async def test_cursor_walk_visits_each_task_exactly_once(client: AsyncClient):
    # AC-1 (#254): full walk via cursor — no gaps, no duplicates, last page
    # without next_cursor.
    created = []
    for i in range(5):
        resp = await client.post("/api/tasks", json={"title": f"Page task {i}"})
        created.append(resp.json()["id"])

    seen: list[int] = []
    cursor = 0
    pages = 0
    while True:
        resp = await client.get(f"/api/tasks?limit=2&after_id={cursor}")
        assert resp.status_code == 200
        body = resp.json()
        assert set(body.keys()) == {"tasks", "next_cursor"}
        seen.extend(t["id"] for t in body["tasks"])
        pages += 1
        if body["next_cursor"] is None:
            break
        cursor = body["next_cursor"]
        assert pages < 20

    assert len(seen) == len(set(seen))  # no duplicates
    assert set(created) <= set(seen)  # no gaps
    assert sorted(seen, reverse=True) == seen  # stable id DESC order


async def test_summary_mode_returns_compact_fields(client: AsyncClient):
    # AC-2 (#254): summary mode strips to compact fields.
    resp = await client.post("/api/tasks", json={"title": "Summary me"})
    assert resp.status_code == 200
    resp = await client.get("/api/tasks?mode=summary&limit=5")
    body = resp.json()
    task = body["tasks"][0]
    assert set(task.keys()) == {
        "id",
        "title",
        "status",
        "task_type",
        "parent_id",
        "priority",
        "readiness_score",
        "dor_passed",
    }


async def test_list_tasks_without_cursor_keeps_plain_list(client: AsyncClient):
    await client.post("/api/tasks", json={"title": "Plain list"})
    resp = await client.get("/api/tasks?limit=5")
    body = resp.json()
    assert isinstance(body, list)  # backward compatible
    assert "description" in body[0]


# ---- ISO8601 UTC timestamps (#255) ----

_ISO_UTC_RE = __import__("re").compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(\+00:00|Z)$"
)


async def test_all_task_timestamps_are_iso8601_utc(client: AsyncClient):
    # AC-1 (#255): every timestamp field in REST contracts matches ISO8601
    # with a UTC marker.
    resp = await client.post("/api/tasks", json={"title": "Timestamps"})
    task_id = resp.json()["id"]
    await client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": "dev", "kind": "status", "content": "Plan: t"},
    )
    await client.post(f"/api/tasks/{task_id}/claim", json={"agent": "dev"})

    body = (await client.get(f"/api/tasks/{task_id}")).json()
    for field in ("created_at", "updated_at", "claimed_at"):
        assert body[field], field
        assert _ISO_UTC_RE.match(body[field]), (field, body[field])
    for u in body["updates"] or []:
        assert _ISO_UTC_RE.match(u["created_at"]), u["created_at"]

    activity = (await client.get("/api/activity")).json()
    assert activity
    assert _ISO_UTC_RE.match(activity[0]["timestamp"]), activity[0]["timestamp"]


async def test_already_iso_timestamp_not_double_converted(client: AsyncClient):
    from hub.models import to_iso_utc

    assert to_iso_utc("2026-07-11 10:00:00") == "2026-07-11T10:00:00+00:00"
    assert to_iso_utc("2026-07-11T10:00:00+00:00") == "2026-07-11T10:00:00+00:00"
    assert to_iso_utc(None) is None
    assert to_iso_utc("") == ""


async def test_notes_availability_endpoint(client: AsyncClient):
    # (#251) diagnostic endpoint; test plugins are Noop → disabled.
    resp = await client.get("/api/integrations/notes")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] in ("available", "no_binary", "no_space", "error")
    assert "detail" in body


# ---- Project read layer (#336) ----


async def _project_with_epic_task(client: AsyncClient, db, slug: str) -> tuple:
    from hub import repository as repo_module

    pid = await repo_module.create_project(db, slug=slug, name=slug.title())
    resp = await client.post(
        "/api/tasks", json={"title": f"Epic {slug}", "task_type": "epic"}
    )
    epic_id = resp.json()["id"]
    await repo_module.update_task(db, epic_id, project_id=pid)
    resp = await client.post(
        "/api/tasks",
        json={"title": f"Feat {slug}", "task_type": "feature", "parent_id": epic_id},
    )
    feat_id = resp.json()["id"]
    resp = await client.post(
        "/api/tasks",
        json={"title": f"Task {slug}", "task_type": "task", "parent_id": feat_id},
    )
    await db.commit()
    return pid, epic_id, feat_id, resp.json()["id"]


async def test_project_filter_returns_only_subtree(client: AsyncClient, db):
    # AC-1 (#336)
    from hub.db import seed_default_project

    await seed_default_project(db)
    _, epic_a, feat_a, task_a = await _project_with_epic_task(client, db, "prod-a")
    _, epic_b, _, _ = await _project_with_epic_task(client, db, "prod-b")

    resp = await client.get("/api/tasks?project=prod-a&limit=100")
    ids = {t["id"] for t in resp.json()}
    assert {epic_a, feat_a, task_a} <= ids
    assert epic_b not in ids

    # Unknown slug → empty; no filter → old behavior (everything visible).
    resp = await client.get("/api/tasks?project=nope&limit=100")
    assert resp.json() == []
    resp = await client.get("/api/tasks?limit=100")
    assert epic_b in {t["id"] for t in resp.json()}


async def test_task_contracts_carry_project_ref(client: AsyncClient, db):
    from hub.db import seed_default_project

    await seed_default_project(db)
    _, _, _, task_id = await _project_with_epic_task(client, db, "prod-c")

    body = (await client.get(f"/api/tasks/{task_id}")).json()
    assert body["project"] == {
        "id": body["project"]["id"],
        "slug": "prod-c",
    }
    ctx = (await client.get(f"/api/tasks/{task_id}/context")).json()
    assert ctx["task"]["project"]["slug"] == "prod-c"
    brief = (await client.get(f"/api/tasks/{task_id}/review-brief")).json()
    assert brief["project"]["slug"] == "prod-c"


# ---- Projects CRUD (#338) ----


async def test_project_create_human_gate(client: AsyncClient, monkeypatch):
    # AC-1 (#338)
    from hub import config

    monkeypatch.setattr(config, "HUB_TOKENS", _review_tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    agent = {"Authorization": "Bearer impl-token"}
    human = {"Authorization": "Bearer human-token"}

    body = {"slug": "calc-kids", "name": "Calc Kids", "repo": "mrPDA/calc-kids"}
    # Since #345 an agent CAN create — but only as a pending proposal.
    resp = await client.post(
        "/api/projects", json={**body, "slug": "agent-ck"}, headers=agent
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "pending"

    resp = await client.post("/api/projects", json=body, headers=human)
    assert resp.status_code == 200, resp.text
    created = resp.json()
    assert created["slug"] == "calc-kids"
    assert created["status"] == "active"

    # Duplicate slug → 409; list returns it.
    resp = await client.post("/api/projects", json=body, headers=human)
    assert resp.status_code == 409
    resp = await client.get("/api/projects", headers=human)
    assert "calc-kids" in {p["slug"] for p in resp.json()}

    # PATCH updates and archives.
    resp = await client.patch(
        f"/api/projects/{created['id']}",
        json={"default_branch": "trunk", "archived": True},
        headers=human,
    )
    assert resp.status_code == 200
    assert resp.json()["default_branch"] == "trunk"
    resp = await client.get("/api/projects", headers=human)
    assert "calc-kids" not in {p["slug"] for p in resp.json()}


async def test_epic_binds_to_project_via_refine(client: AsyncClient, db):
    # AC-2 (#338)
    from hub import repository as repo_module

    await repo_module.create_project(db, slug="prod-z", name="Z")
    await db.commit()
    resp = await client.post(
        "/api/tasks", json={"title": "Z epic", "task_type": "epic"}
    )
    epic_id = resp.json()["id"]

    resp = await client.post(f"/api/tasks/{epic_id}/refine", json={"project": "prod-z"})
    assert resp.status_code == 200, resp.text
    body = (await client.get(f"/api/tasks/{epic_id}")).json()
    assert body["project"]["slug"] == "prod-z"

    # Unknown slug and non-epic target → 422.
    resp = await client.post(f"/api/tasks/{epic_id}/refine", json={"project": "ghost"})
    assert resp.status_code == 422
    resp = await client.post("/api/tasks", json={"title": "Plain task"})
    task_id = resp.json()["id"]
    resp = await client.post(f"/api/tasks/{task_id}/refine", json={"project": "prod-z"})
    assert resp.status_code == 422
    assert "epic" in resp.json()["detail"]


async def test_deprecated_tool_telemetry_endpoint(client: AsyncClient):
    # (#325) counted in activity_log.
    resp = await client.post(
        "/api/telemetry/deprecated-tool",
        json={"tool": "hub_approve_proposal", "replacement": "hub_approve_task"},
    )
    assert resp.status_code == 200
    activity = (await client.get("/api/activity")).json()
    assert any(
        a["kind"] == "deprecated_tool_call" and "hub_approve_proposal" in a["summary"]
        for a in activity
    )


async def test_withdraw_matches_by_principal_id(client: AsyncClient, monkeypatch, db):
    # (#325 bonus, gap from #322): withdraw works when the principal matches
    # even if the display name differs from assigned_agent.
    from hub import config
    from hub import repository as repo_module

    monkeypatch.setattr(config, "HUB_TOKENS", _principal_tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    impl = {"Authorization": "Bearer impl-pid-token"}  # principal 7

    resp = await client.post(
        "/api/tasks",
        json={"title": "Withdraw me", "source": "agent", "agent": "other-name"},
        headers=impl,
    )
    task_id = resp.json()["id"]
    await repo_module.update_task(db, task_id, implementer_principal_id=7)
    await db.commit()

    resp = await client.post(f"/api/tasks/{task_id}/withdraw", headers=impl)
    assert resp.status_code == 200, resp.text
    assert resp.json()["archived"] is True


# ---- Project proposals (#345) ----


async def test_agent_project_proposal_pending_then_activated(
    client: AsyncClient, monkeypatch, db
):
    # AC-1 (#345)
    from hub import config
    from hub import repository as repo_module

    monkeypatch.setattr(config, "HUB_TOKENS", _review_tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    monkeypatch.setattr(config, "ALLOW_AGENT_PROJECTS", "propose")
    agent = {"Authorization": "Bearer impl-token"}
    human = {"Authorization": "Bearer human-token"}

    resp = await client.post(
        "/api/projects",
        json={"slug": "agent-idea", "name": "Agent Idea"},
        headers=agent,
    )
    assert resp.status_code == 200, resp.text
    created = resp.json()
    assert created["status"] == "pending"

    # Pending is invisible to routing: epic binding rejected, resolver falls
    # back to default, selector list skips it.
    from hub.db import seed_default_project

    await seed_default_project(db)
    resp = await client.post(
        "/api/tasks",
        json={"title": "E for pending", "task_type": "epic"},
        headers=human,
    )
    epic_id = resp.json()["id"]
    resp = await client.post(
        f"/api/tasks/{epic_id}/refine",
        json={"project": "agent-idea"},
        headers=human,
    )
    assert resp.status_code == 422
    assert "pending" in resp.json()["detail"]
    active_rows = await repo_module.list_projects(db, only_active=True)
    assert "agent-idea" not in {dict(r)["slug"] for r in active_rows}

    # Human activation flips it to active and binding works.
    resp = await client.patch(
        f"/api/projects/{created['id']}",
        json={"status": "active"},
        headers=human,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "active"
    resp = await client.post(
        f"/api/tasks/{epic_id}/refine",
        json={"project": "agent-idea"},
        headers=human,
    )
    assert resp.status_code == 200


async def test_agent_project_direct_mode(client: AsyncClient, monkeypatch):
    # AC-2 (#345)
    from hub import config

    monkeypatch.setattr(config, "HUB_TOKENS", _review_tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    monkeypatch.setattr(config, "ALLOW_AGENT_PROJECTS", "direct")
    agent = {"Authorization": "Bearer impl-token"}

    resp = await client.post(
        "/api/projects",
        json={"slug": "solo-direct", "name": "Solo"},
        headers=agent,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "active"


# ---------------------------------------------------------------------------
# Events feed (#349)
# ---------------------------------------------------------------------------


async def test_events_cursor(client: AsyncClient, db):
    # AC-3: ASC order, next_cursor, idempotent repeat, kinds filter.
    from hub import repository as repo_module

    for i in range(3):
        await repo_module.insert_event(db, kind=f"k{i}", task_id=i + 1)
    await db.commit()

    resp = await client.get("/api/events?since=0")
    assert resp.status_code == 200
    data = resp.json()
    assert [e["kind"] for e in data["events"]] == ["k0", "k1", "k2"]
    assert data["events"][0]["payload"] == {}
    cursor = data["next_cursor"]
    assert cursor == data["events"][-1]["id"]

    resp2 = await client.get(f"/api/events?since={cursor}")
    assert resp2.json() == {"events": [], "next_cursor": cursor}

    resp3 = await client.get("/api/events?since=0&kinds=k1")
    assert [e["kind"] for e in resp3.json()["events"]] == ["k1"]


async def test_events_long_poll(client: AsyncClient, db):
    # AC-4: long-poll returns early when an event lands during the wait.
    import asyncio
    import time as time_module

    from hub import repository as repo_module

    async def emit_later():
        await asyncio.sleep(0.3)
        await repo_module.insert_event(db, kind="late_event", task_id=9)
        await db.commit()

    emitter = asyncio.create_task(emit_later())
    started = time_module.monotonic()
    resp = await client.get("/api/events?since=0&wait=5")
    elapsed = time_module.monotonic() - started
    await emitter

    assert [e["kind"] for e in resp.json()["events"]] == ["late_event"]
    assert elapsed < 4.5  # returned well before the 5s deadline

    # No events: empty answer after the wait expires.
    cursor = resp.json()["next_cursor"]
    started = time_module.monotonic()
    resp2 = await client.get(f"/api/events?since={cursor}&wait=1")
    elapsed2 = time_module.monotonic() - started
    assert resp2.json()["events"] == []
    assert elapsed2 >= 0.9


async def test_events_auth(client: AsyncClient, monkeypatch):
    # AC-5: 401 without a token once tokens are configured; 200 with one.
    from hub import config
    from hub.config import TokenIdentity

    monkeypatch.setattr(
        config, "HUB_TOKENS", {"events-token": TokenIdentity("bot", "agent")}
    )
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)

    resp = await client.get("/api/events")
    assert resp.status_code == 401

    resp2 = await client.get(
        "/api/events", headers={"Authorization": "Bearer events-token"}
    )
    assert resp2.status_code == 200


# ---------------------------------------------------------------------------
# Workspace provisioning (#347)
# ---------------------------------------------------------------------------


async def _provision_project_row(db) -> int:
    from hub import repository as repo_module
    from hub.db import seed_default_project

    await seed_default_project(db)
    pid = await repo_module.create_project(
        db,
        slug="prov-api",
        name="Prov API",
        repo_name="mrPDA/prov-api",
        workspace_path="/srv/prov-api",
    )
    await db.commit()
    return pid


async def test_provision_endpoint_human_gate(client: AsyncClient, db, monkeypatch):
    # Human gate: agent tokens get 403, human tokens get an answer.
    from hub import config
    from hub.config import TokenIdentity

    pid = await _provision_project_row(db)
    monkeypatch.setattr(
        config,
        "HUB_TOKENS",
        {
            "agent-tok": TokenIdentity("bot", "agent"),
            "human-tok": TokenIdentity("denis", "human"),
        },
    )
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)

    resp = await client.post(
        f"/api/projects/{pid}/provision",
        headers={"Authorization": "Bearer agent-tok"},
    )
    assert resp.status_code == 403

    resp = await client.post(
        f"/api/projects/{pid}/provision",
        headers={"Authorization": "Bearer human-tok"},
    )
    assert resp.status_code == 200
    data = resp.json()
    # Mock/noop git ops → readable error, never a 500.
    assert data["provision_status"] == "error"
    assert "git ops disabled" in data["provision_detail"]
    assert data["project"]["provision_status"] == "error"


async def test_provision_endpoint_success_with_mock(client: AsyncClient, db):
    from unittest.mock import AsyncMock

    from hub.integrations.registry import plugins

    pid = await _provision_project_row(db)
    plugins.git_ops.clone_repo = AsyncMock(return_value=(True, "cloned"))
    resp = await client.post(f"/api/projects/{pid}/provision")
    assert resp.status_code == 200
    assert resp.json()["provision_status"] == "ok"


async def test_create_active_project_auto_provisions(client: AsyncClient, db):
    # #347: creating an active project with repo+workspace kicks provisioning
    # in the background; with noop git ops the outcome is a readable error.
    from hub import repository as repo_module
    from hub.db import seed_default_project

    await seed_default_project(db)
    resp = await client.post(
        "/api/projects",
        json={
            "slug": "auto-prov",
            "name": "Auto Prov",
            "repo": "mrPDA/auto-prov",
            "workspace_path": "/srv/auto-prov",
        },
    )
    assert resp.status_code == 200
    row = await repo_module.get_project_by_slug(db, "auto-prov")
    assert row["provision_status"] == "error"  # background task ran (noop)
    assert "git ops disabled" in row["provision_detail"]


async def test_create_project_without_workspace_skips_auto_provision(
    client: AsyncClient, db
):
    from hub import repository as repo_module
    from hub.db import seed_default_project

    await seed_default_project(db)
    resp = await client.post(
        "/api/projects",
        json={"slug": "no-auto", "name": "No Auto", "repo": "mrPDA/no-auto"},
    )
    assert resp.status_code == 200
    row = await repo_module.get_project_by_slug(db, "no-auto")
    assert row["provision_status"] == "none"


# ---------------------------------------------------------------------------
# Skills library (#380)
# ---------------------------------------------------------------------------


async def test_skills_seed_and_get(client: AsyncClient, db):
    # AC-3: seed puts multi-agent-review v1 active.
    from hub.db import seed_default_skills

    await seed_default_skills(db)
    resp = await client.get("/api/skills/multi-agent-review")
    assert resp.status_code == 200
    skill = resp.json()
    assert skill["version"] == 1
    assert skill["status"] == "active"
    assert "опровергатель" in skill["content"].lower() or "refuted" in skill["content"]

    listing = await client.get("/api/skills")
    assert any(s["name"] == "multi-agent-review" for s in listing.json())


async def test_skill_agent_proposes_draft_human_activates(
    client: AsyncClient, db, monkeypatch
):
    # AC-1/AC-2: agent POST → draft, active untouched; human activates.
    from hub import config
    from hub.config import TokenIdentity
    from hub.db import seed_default_skills

    await seed_default_skills(db)
    monkeypatch.setattr(
        config,
        "HUB_TOKENS",
        {
            "agent-tok": TokenIdentity("bot", "agent"),
            "human-tok": TokenIdentity("denis", "human"),
        },
    )
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    agent = {"Authorization": "Bearer agent-tok"}
    human = {"Authorization": "Bearer human-tok"}

    resp = await client.post(
        "/api/skills",
        json={"name": "multi-agent-review", "content": "v2 harness", "kind": "prompt"},
        headers=agent,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "draft"
    assert resp.json()["version"] == 2

    # active stays v1 until a human activates v2
    resp = await client.get("/api/skills/multi-agent-review", headers=agent)
    assert resp.json()["version"] == 1

    resp = await client.patch(
        "/api/skills/multi-agent-review/versions/2/activate", headers=agent
    )
    assert resp.status_code == 403  # human gate

    resp = await client.patch(
        "/api/skills/multi-agent-review/versions/2/activate", headers=human
    )
    assert resp.status_code == 200
    resp = await client.get("/api/skills/multi-agent-review", headers=agent)
    assert resp.json()["version"] == 2

    # activation emitted a feed event (#349)
    events = await client.get(
        "/api/events?since=0&kinds=skill_activated", headers=human
    )
    assert any(e["payload"].get("version") == 2 for e in events.json()["events"])


async def test_skill_human_creates_active(client: AsyncClient, db):
    resp = await client.post(
        "/api/skills",
        json={
            "name": "dor-checklist",
            "content": "- AC?\n- risks?",
            "kind": "checklist",
            "tags": ["dor"],
        },
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "active"  # open mode = human path
    got = await client.get("/api/skills/dor-checklist")
    assert got.json()["tags"] == ["dor"]


# ---------------------------------------------------------------------------
# Machine review reports (#381)
# ---------------------------------------------------------------------------


async def _reviewable_task(client: AsyncClient, db) -> int:
    from hub import repository as repo_module
    from hub import services as services_module
    from hub.models import TaskCreate

    tv = await services_module.create_task(db, TaskCreate(title="MR task"))
    await repo_module.add_task_update(db, tv.id, "dev", "status", "Plan: mr")
    await db.commit()
    await services_module.pair_start_task(db, tv.id, caller="dev")
    await services_module.submit_for_review(db, tv.id)
    return tv.id


async def test_machine_review_submit_and_brief(client: AsyncClient, db):
    task_id = await _reviewable_task(client, db)
    resp = await client.post(
        f"/api/tasks/{task_id}/machine-review",
        json={
            "raw_count": 16,
            "agent_count": 36,
            "tokens_spent": 1428876,
            "duration_ms": 716827,
            "harness_skill": "multi-agent-review",
            "harness_version": 1,
            "orchestrator": "claude-code-workflow",
            "findings_confirmed": [
                {
                    "title": "policy JSON error path untested",
                    "severity": "medium",
                    "category": "tests",
                    "file": "hub/web.py",
                    "line": 633,
                }
            ],
            "findings_rejected": [
                {"title": "slug XSS", "category": "security", "reason": "unreachable"}
            ],
            "agent": "claude-code",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["is_current"] is True
    assert data["submission_generation"] == 1
    assert data["tokens_spent"] == 1428876

    brief = await client.get(f"/api/tasks/{task_id}/review-brief")
    mr = brief.json()["machine_review"]
    assert mr["raw_count"] == 16
    assert mr["is_current"] is True

    events = await client.get("/api/events?since=0&kinds=machine_review_completed")
    assert any(
        e["task_id"] == task_id and e["payload"]["confirmed"] == 1
        for e in events.json()["events"]
    )


async def test_machine_review_stale_after_resubmit(client: AsyncClient, db):
    from hub import repository as repo_module
    from hub import services as services_module

    task_id = await _reviewable_task(client, db)
    await client.post(
        f"/api/tasks/{task_id}/machine-review",
        json={"raw_count": 3, "findings_confirmed": [], "findings_rejected": []},
    )
    # resubmit bumps generation → report goes stale
    await repo_module.update_task(db, task_id, status="running")
    await db.commit()
    await services_module.submit_for_review(db, task_id)

    brief = await client.get(f"/api/tasks/{task_id}/review-brief")
    assert brief.json()["machine_review"]["is_current"] is False


async def test_machine_review_requires_submission(client: AsyncClient, db):
    from hub import services as services_module
    from hub.models import TaskCreate

    tv = await services_module.create_task(db, TaskCreate(title="No submission"))
    resp = await client.post(
        f"/api/tasks/{tv.id}/machine-review",
        json={"raw_count": 1},
    )
    assert resp.status_code == 400
    assert "submit_for_review" in resp.text


async def test_seed_machine_review_cycle_skill(client: AsyncClient, db):
    # #383: the portable cycle contract ships as an active seed skill.
    from hub.db import seed_default_skills

    await seed_default_skills(db)
    resp = await client.get("/api/skills/machine-review-cycle")
    assert resp.status_code == 200
    skill = resp.json()
    assert skill["kind"] == "skill"
    assert skill["status"] == "active"
    for marker in (
        "hub_get_skill",
        "hub_submit_machine_review",
        "submission_generation",
    ):
        assert marker in skill["content"]

    # both seeds coexist and re-seeding is idempotent
    await seed_default_skills(db)
    listing = await client.get("/api/skills")
    names = [s["name"] for s in listing.json()]
    assert names.count("machine-review-cycle") == 1
    assert "multi-agent-review" in names


# ---------------------------------------------------------------------------
# Practice metrics (#384)
# ---------------------------------------------------------------------------


async def test_practice_metrics_aggregates(client: AsyncClient, db):
    # AC-1/AC-2: cost per confirmed, filtration, harness split, recurrence.
    from hub import repository as repo_module

    t1 = await _reviewable_task(client, db)
    await client.post(
        f"/api/tasks/{t1}/machine-review",
        json={
            "raw_count": 10,
            "tokens_spent": 1_000_000,
            "duration_ms": 60_000,
            "harness_skill": "multi-agent-review",
            "harness_version": 1,
            "findings_confirmed": [
                {"title": "a", "severity": "low", "category": "tests"},
                {"title": "b", "severity": "medium", "category": "consistency"},
            ],
            "findings_rejected": [
                {"title": "c", "reason": "noise"},
                {"title": "d", "reason": "noise"},
            ],
        },
    )
    t2 = await _reviewable_task(client, db)
    await client.post(
        f"/api/tasks/{t2}/machine-review",
        json={
            "raw_count": 6,
            "harness_skill": "multi-agent-review",
            "harness_version": 2,
            "findings_confirmed": [
                {"title": "e", "severity": "low", "category": "tests"},
            ],
            "findings_rejected": [],
        },
    )
    assert t1 != t2
    assert await repo_module.get_latest_machine_review(db, t2) is not None

    resp = await client.get("/api/metrics/practices?since_days=30")
    assert resp.status_code == 200
    data = resp.json()
    mr = data["machine_reviews"]
    assert mr["reviews"] == 2
    assert mr["raw_total"] == 16
    assert mr["confirmed_total"] == 3
    assert mr["tokens_total"] == 1_000_000
    assert mr["tokens_per_confirmed"] == round(1_000_000 / 3)
    assert mr["reports_without_tokens"] == 1  # второй отчёт без токенов
    assert abs(mr["filtration_rate"] - (1 - 3 / 16)) < 0.001

    versions = {(h["harness_skill"], h["harness_version"]) for h in data["by_harness"]}
    assert ("multi-agent-review", 1) in versions
    assert ("multi-agent-review", 2) in versions

    cats = {c["category"]: c for c in data["recurring_categories"]}
    assert cats["tests"]["tasks"] == 2 and cats["tests"]["recurring"] is True
    assert cats["consistency"]["recurring"] is False


async def test_practice_metrics_cycle_times(client: AsyncClient, db):
    # AC-3: median ready→completed hours by work_type from existing stamps.
    from hub import repository as repo_module
    from hub import services as services_module
    from hub.models import TaskCreate

    for hours in (2, 4, 100):
        tv = await services_module.create_task(
            db, TaskCreate(title=f"Cycle {hours}", work_type="bug")
        )
        await db.execute(
            "UPDATE tasks SET status='completed', "
            "ready_at=datetime('now', ?), updated_at=datetime('now') WHERE id=?",
            (f"-{hours} hours", tv.id),
        )
    await db.commit()
    assert await repo_module.get_task(db, tv.id) is not None

    resp = await client.get("/api/metrics/practices")
    cycles = {c["work_type"]: c for c in resp.json()["cycle_times"]}
    assert cycles["bug"]["tasks"] == 3
    assert 3.5 <= cycles["bug"]["median_hours"] <= 4.5  # медиана, не среднее


async def test_metrics_page_renders(client: AsyncClient, db):
    resp = await client.get("/metrics")
    assert resp.status_code == 200
    assert "Practice Metrics" in resp.text
