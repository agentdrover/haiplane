from __future__ import annotations

from pathlib import Path

import pytest
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
        "affected_areas": ["hub/services/dor.py"],
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
    assert data.get("git_mode", "hub") == "hub"


async def test_pair_start_api_remote_git_mode(client: AsyncClient):
    """#975: REST pair-start accepts git_mode=remote and returns it."""
    create_resp = await client.post("/api/tasks", json={"title": "Remote API pair"})
    task_id = create_resp.json()["id"]

    resp = await client.post(
        f"/api/tasks/{task_id}/pair-start",
        json={
            "plan": "Plan: work in own clone",
            "assigned_agent": "cloud",
            "git_mode": "remote",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "running"
    assert data["git_mode"] == "remote"
    assert data["branch"] == f"task-{task_id}/remote-api-pair"
    assert data["job_id"] is None


def _enable_live_worktree(monkeypatch, *, path: str, registered: bool = True):
    from unittest.mock import AsyncMock, MagicMock

    from hub.integrations.registry import plugins

    monkeypatch.setenv("HAIPLANE_WORKTREE_PER_TASK", "1")
    plugins.git_ops.pair_prepare_worktree = AsyncMock(return_value="task-wt")
    plugins.git_ops.worktree_path = MagicMock(return_value=path)
    plugins.git_ops.worktree_is_registered = AsyncMock(return_value=registered)
    return {"registered": registered}


async def test_get_task_echoes_live_pair_worktree(client: AsyncClient, monkeypatch):
    """AC-1 (#989): GET after pair-start names the same live worktree path."""
    create_resp = await client.post("/api/tasks", json={"title": "Echo WT get"})
    task_id = create_resp.json()["id"]
    path = f"/srv/.ws-worktrees/task-{task_id}"
    _enable_live_worktree(monkeypatch, path=path)

    started = await client.post(
        f"/api/tasks/{task_id}/pair-start",
        json={"plan": "Plan: pair", "assigned_agent": "dev"},
    )
    assert started.status_code == 200
    assert started.json()["worktree_path"] == path

    got = await client.get(f"/api/tasks/{task_id}")
    assert got.status_code == 200
    assert got.json()["worktree_path"] == path
    assert got.json()["workspace_mode"] == "worktree"


async def test_get_task_claimed_only_has_empty_worktree(
    client: AsyncClient, monkeypatch
):
    """AC-4 (#989): claimed without pair-start does not invent a directory."""
    create_resp = await client.post("/api/tasks", json={"title": "Claimed no WT"})
    task_id = create_resp.json()["id"]
    invented = f"/srv/.ws-worktrees/task-{task_id}"
    _enable_live_worktree(monkeypatch, path=invented, registered=False)

    claimed = await client.post(
        f"/api/tasks/{task_id}/claim",
        json={"agent": "dev", "session_id": "sess-claim"},
    )
    assert claimed.status_code == 200
    got = await client.get(f"/api/tasks/{task_id}")
    assert got.json()["worktree_path"] == ""
    assert invented not in got.text


async def test_get_task_headless_start_has_empty_worktree(
    client: AsyncClient, db, monkeypatch
):
    """AC-8 (#989): hub_start_task never prepares a pair worktree."""
    from hub import services

    create_resp = await client.post("/api/tasks", json={"title": "Headless start WT"})
    task_id = create_resp.json()["id"]
    invented = f"/srv/.ws-worktrees/task-{task_id}"
    _enable_live_worktree(monkeypatch, path=invented, registered=False)
    await repo.add_task_update(db, task_id, "dev", "status", "Plan: headless")
    await db.commit()
    await services.start_task(db, task_id)

    got = await client.get(f"/api/tasks/{task_id}")
    assert got.status_code == 200
    assert got.json()["status"] == "running"
    assert got.json()["worktree_path"] == ""
    assert invented not in got.text


async def test_get_task_empty_worktree_after_submit_then_back_on_rework(
    client: AsyncClient, db, monkeypatch
):
    """AC-7 (#989): submit removes the tree; rework names it again."""
    from unittest.mock import AsyncMock

    from hub import services
    from hub.integrations.registry import plugins
    from hub.models import ReviewVerdict, TaskReviewVerdict

    create_resp = await client.post("/api/tasks", json={"title": "Submit clears WT"})
    task_id = create_resp.json()["id"]
    path = f"/srv/.ws-worktrees/task-{task_id}"
    state = {"registered": True}
    monkeypatch.setenv("HAIPLANE_WORKTREE_PER_TASK", "1")
    plugins.git_ops.pair_prepare_worktree = AsyncMock(return_value="task-wt")
    plugins.git_ops.worktree_path = lambda task_id, repo=None: path

    async def _reg(p, repo=None):
        return state["registered"]

    plugins.git_ops.worktree_is_registered = _reg

    started = await client.post(
        f"/api/tasks/{task_id}/pair-start",
        json={"plan": "Plan: pair", "assigned_agent": "dev"},
    )
    assert started.json()["worktree_path"] == path
    assert (await client.get(f"/api/tasks/{task_id}")).json()["worktree_path"] == path

    state["registered"] = False
    submitted = await client.post(
        f"/api/tasks/{task_id}/submit-review", json={"agent": "dev"}
    )
    assert submitted.status_code == 200, submitted.text
    after = (await client.get(f"/api/tasks/{task_id}")).json()
    assert after["status"] == "review"
    assert after["worktree_path"] == ""
    ctx = (await client.get(f"/api/tasks/{task_id}/context")).json()
    assert ctx["task"]["worktree_path"] == ""
    assert path not in ctx["context_text"]

    state["registered"] = True
    await services.record_review_verdict(
        db,
        task_id,
        TaskReviewVerdict(
            verdict=ReviewVerdict.changes_requested,
            agent="reviewer",
            comments="1. fix",
        ),
    )
    rework = (await client.get(f"/api/tasks/{task_id}")).json()
    assert rework["status"] == "running"
    assert rework["worktree_path"] == path


async def test_list_summary_cards_omit_worktree_path(client: AsyncClient, monkeypatch):
    """Out of scope (#989): REST summary cards do not gain a worktree field."""
    create_resp = await client.post("/api/tasks", json={"title": "Summary no WT field"})
    task_id = create_resp.json()["id"]
    path = f"/srv/.ws-worktrees/task-{task_id}"
    _enable_live_worktree(monkeypatch, path=path)
    await client.post(
        f"/api/tasks/{task_id}/pair-start",
        json={"plan": "Plan: pair", "assigned_agent": "dev"},
    )
    listed = await client.get("/api/tasks?mode=summary")
    assert listed.status_code == 200
    cards = listed.json()["tasks"]
    match = next(c for c in cards if c["id"] == task_id)
    assert "worktree_path" not in match


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
    # #370 T5: accepting both branches meant this assertion could not fail.
    assert data["status"] == "fix_requested"
    assert data["job_id"]
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


async def test_the_brief_carries_the_call_site_section(client: AsyncClient):
    """The section has to reach the reviewer, not merely exist as a service.

    Five findings in one session were mechanisms that worked and were never
    wired; a unit test of the analyser alone would repeat that mistake here.
    This asks the endpoint the reviewer actually calls.
    """
    resp = await client.post("/api/tasks", json={"title": "Wired?"})
    task_id = resp.json()["id"]

    brief = (await client.get(f"/api/tasks/{task_id}/review-brief")).json()

    assert "call_sites" in brief, "the reviewer never sees a section the brief omits"
    section = brief["call_sites"]
    # Without a branch there is nothing to diff, and the section must SAY that
    # rather than return an empty list that reads as "no other call sites".
    assert section["status"] == "unknown"
    assert section["reason"], "an unknown with no reason is a shrug"


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


async def test_review_verdict_api_finding_scope_roundtrip(client: AsyncClient):
    # #435: scope defaults to in_scope; out_of_scope findings carry the
    # linked follow-up task through the review brief.
    task_id = await _running_pair_task(client, "Scope roundtrip API")
    await client.post(f"/api/tasks/{task_id}/submit-review", json={})

    resp = await client.post(
        f"/api/tasks/{task_id}/review-verdict",
        json={
            "verdict": "changes_requested",
            "agent": "reviewer",
            "findings": [
                {"id": 1, "severity": "high", "message": "Fix here"},
                {
                    "id": 2,
                    "severity": "low",
                    "message": "Move elsewhere",
                    "scope": "out_of_scope",
                    "linked_task_id": 436,
                },
            ],
        },
    )
    assert resp.status_code == 200
    findings = resp.json()["latest_review"]["findings"]
    assert findings[0]["scope"] == "in_scope"
    assert findings[0]["linked_task_id"] is None
    assert findings[1]["scope"] == "out_of_scope"
    assert findings[1]["linked_task_id"] == 436

    brief = (await client.get(f"/api/tasks/{task_id}/review-brief")).json()
    brief_findings = brief["latest_review"]["findings"]
    assert brief_findings[1]["scope"] == "out_of_scope"
    assert brief_findings[1]["linked_task_id"] == 436


async def test_review_verdict_api_auto_creates_drafts_for_out_of_scope(
    client: AsyncClient,
):
    # #436: create_tasks_for_out_of_scope=true auto-creates a DRAFT
    # follow-up task and stamps its id into the stored finding.
    task_id = await _running_pair_task(client, "Auto draft API")
    await client.post(f"/api/tasks/{task_id}/submit-review", json={})

    resp = await client.post(
        f"/api/tasks/{task_id}/review-verdict",
        json={
            "verdict": "changes_requested",
            "agent": "reviewer",
            "create_tasks_for_out_of_scope": True,
            "findings": [
                {"id": 1, "severity": "high", "message": "Fix here"},
                {
                    "id": 2,
                    "severity": "low",
                    "message": "Move elsewhere",
                    "scope": "out_of_scope",
                },
            ],
        },
    )
    assert resp.status_code == 200
    findings = resp.json()["latest_review"]["findings"]
    linked_id = findings[1]["linked_task_id"]
    assert linked_id is not None
    assert findings[0]["linked_task_id"] is None

    draft = (await client.get(f"/api/tasks/{linked_id}")).json()
    assert draft["status"] == "draft"
    assert draft["task_type"] == "task"
    assert draft["parent_id"] is None
    assert f"from review of task #{task_id}" in draft["description"]


async def test_review_verdict_api_flag_off_keeps_findings_unlinked(
    client: AsyncClient,
):
    task_id = await _running_pair_task(client, "Flag off API")
    await client.post(f"/api/tasks/{task_id}/submit-review", json={})

    resp = await client.post(
        f"/api/tasks/{task_id}/review-verdict",
        json={
            "verdict": "changes_requested",
            "agent": "reviewer",
            "findings": [
                {"id": 1, "severity": "high", "message": "Fix here"},
                {
                    "id": 2,
                    "severity": "low",
                    "message": "Move elsewhere",
                    "scope": "out_of_scope",
                },
            ],
        },
    )
    assert resp.status_code == 200
    assert resp.json()["latest_review"]["findings"][1]["linked_task_id"] is None


async def test_review_verdict_api_rejects_all_out_of_scope_findings(
    client: AsyncClient,
):
    task_id = await _running_pair_task(client, "All out of scope API")
    await client.post(f"/api/tasks/{task_id}/submit-review", json={})

    resp = await client.post(
        f"/api/tasks/{task_id}/review-verdict",
        json={
            "verdict": "changes_requested",
            "agent": "reviewer",
            "findings": [
                {
                    "id": 1,
                    "severity": "high",
                    "message": "Different subsystem",
                    "scope": "out_of_scope",
                },
            ],
        },
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["reason"] == "changes_requested_requires_in_scope_finding"
    assert "approved" in detail["hint"]


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


async def _task_in_review(
    client: AsyncClient,
    title: str,
    headers: dict,
    agent: str = "impl-bot",
    human_headers: dict | None = None,
) -> int:
    # Ready work is created by a human (#360) — an agent token here would get
    # 403 agent_create_forbidden. These tests are about the review gate, so the
    # task is born under the human token and everything after it runs as the
    # agent in ``headers``. Most token maps here name that token "human-token";
    # _provisioned_reviewer_tokens spells it "human-tok", which is what
    # ``human_headers`` is for — so do not collapse this back to a default.
    resp = await client.post(
        "/api/tasks",
        json={"title": title},
        headers=human_headers or {"Authorization": "Bearer human-token"},
    )
    task_id = resp.json()["id"]
    await client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": agent, "kind": "status", "content": "Plan: work"},
        headers=headers,
    )
    resp = await client.post(
        f"/api/tasks/{task_id}/pair-start",
        # #852: an agent names the session that takes the task; the properties
        # under test here (self-review by name and by principal) are unchanged.
        json={"assigned_agent": agent, "session_id": f"s-{agent}"},
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
        json={
            "verdict": "changes_requested",
            "agent": "reviewer-bot",
            # #1010: the subject is reviewer independence, but a verdict that
            # sends work back has to say what to redo.
            "comments": "rework the branch handling",
        },
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
    # Independent verdicts are never marked as self-approved (#434).
    assert resp.json()["latest_review"]["self_approved"] is False


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
    body = resp.json()
    assert body["review_approved_current"] is True
    # #434: the weakened gate is audited — the verdict is marked.
    assert body["latest_review"]["self_approved"] is True
    assert any(
        u["kind"] == "review" and "[self-approved: solo mode" in u["content"]
        for u in body["updates"] or []
    )


# ---- Fail-fast self-review warning in the review brief (#433) ----


async def test_review_brief_warns_implementer_of_self_review(
    client: AsyncClient, monkeypatch
):
    # AC-1: the implementer gets a structured warning BEFORE running the
    # review; independent reviewers and humans get a clean brief.
    from hub import config

    monkeypatch.setattr(config, "HUB_TOKENS", _review_tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    monkeypatch.setattr(config, "REVIEW_SELF_APPROVE", "forbid")
    impl = {"Authorization": "Bearer impl-token"}
    reviewer = {"Authorization": "Bearer reviewer-token"}
    human = {"Authorization": "Bearer human-token"}

    task_id = await _task_in_review(client, "Brief self-review warning", impl)

    resp = await client.get(f"/api/tasks/{task_id}/review-brief", headers=impl)
    assert resp.status_code == 200
    warning = resp.json()["self_review_warning"]
    assert warning is not None
    assert warning["reason"] == "self_review_forbidden"
    assert "impl-bot" in warning["message"]
    assert "independent" in warning["hint"]
    assert warning["required_role"] == "independent_reviewer"

    resp = await client.get(f"/api/tasks/{task_id}/review-brief", headers=reviewer)
    assert resp.status_code == 200
    assert resp.json()["self_review_warning"] is None

    resp = await client.get(f"/api/tasks/{task_id}/review-brief", headers=human)
    assert resp.status_code == 200
    assert resp.json()["self_review_warning"] is None


async def test_review_brief_solo_mode_note_for_implementer(
    client: AsyncClient, monkeypatch
):
    # AC-2: with HAIPLANE_REVIEW_SELF_APPROVE=allow the warning turns into
    # an informational solo-mode note instead.
    from hub import config

    monkeypatch.setattr(config, "HUB_TOKENS", _review_tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    monkeypatch.setattr(config, "REVIEW_SELF_APPROVE", "allow")
    impl = {"Authorization": "Bearer impl-token"}

    task_id = await _task_in_review(client, "Brief solo-mode note", impl)

    resp = await client.get(f"/api/tasks/{task_id}/review-brief", headers=impl)
    assert resp.status_code == 200
    warning = resp.json()["self_review_warning"]
    assert warning is not None
    assert warning["reason"] == "solo_mode_self_review"
    assert "HAIPLANE_REVIEW_SELF_APPROVE=allow" in warning["hint"]
    assert ("OPEN" + "CLAW") not in warning["hint"], (
        "Wave 5: the legacy name must be gone from operator hints"
    )
    assert warning["required_role"] is None


async def test_review_brief_warns_implementer_by_principal_id(
    client: AsyncClient, monkeypatch
):
    # AC-3: principal binding (#320) — the warning fires even when
    # assigned_agent holds a different free-text name than the token
    # username, matching ensure_reviewer_independence semantics.
    from hub import config

    monkeypatch.setattr(config, "HUB_TOKENS", _principal_tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    monkeypatch.setattr(config, "REVIEW_SELF_APPROVE", "forbid")
    impl = {"Authorization": "Bearer impl-pid-token"}

    resp = await client.post(
        "/api/tasks",
        json={"title": "Brief principal warning"},
        # Created by the human (#360); the subject is principal matching.
        headers={"Authorization": "Bearer human-token"},
    )
    task_id = resp.json()["id"]
    await client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": "someone", "kind": "status", "content": "Plan: work"},
        headers=impl,
    )
    resp = await client.post(
        f"/api/tasks/{task_id}/pair-start",
        json={"assigned_agent": "claude-code", "session_id": "s-impl"},
        headers=impl,
    )
    assert resp.status_code == 200, resp.text
    resp = await client.post(
        f"/api/tasks/{task_id}/submit-review", json={}, headers=impl
    )
    assert resp.json()["status"] == "review"

    resp = await client.get(f"/api/tasks/{task_id}/review-brief", headers=impl)
    assert resp.status_code == 200
    warning = resp.json()["self_review_warning"]
    assert warning is not None
    assert warning["reason"] == "self_review_forbidden"

    other = {"Authorization": "Bearer other-pid-token"}
    resp = await client.get(f"/api/tasks/{task_id}/review-brief", headers=other)
    assert resp.status_code == 200
    assert resp.json()["self_review_warning"] is None


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
        "/api/tasks",
        json={"title": "Principal gate"},
        # Created by the human (#360); the subject is principal matching.
        headers={"Authorization": "Bearer human-token"},
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
        json={"assigned_agent": "claude-code", "session_id": "s-impl"},
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

    # Created by the human (#360); the subject is what claiming records.
    resp = await client.post(
        "/api/tasks",
        json={"title": "Claim principal"},
        headers={"Authorization": "Bearer human-token"},
    )
    task_id = resp.json()["id"]
    resp = await client.post(
        f"/api/tasks/{task_id}/claim",
        json={"agent": "display-name-x", "session_id": "s-impl"},
        headers=impl,
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


# ---- Reviewer identity provisioning (#432) ----
#
# The documented deploy setup (deploy/local-hub.env.example,
# docs/agent-onboarding.md): TWO env tokens with role `agent` — the
# implementer (`cursor`) and a dedicated reviewer (`cursor-reviewer`).
# Parsed straight from the HAIPLANE_HUB_TOKENS format so the tests pin the
# exact configuration operators are told to provision.


def _provisioned_reviewer_tokens() -> dict:
    from hub import config

    return config.parse_tokens(
        "denis:human-tok:human,"
        "cursor:cursor-tok:agent,"
        "cursor-reviewer:reviewer-tok:agent"
    )


async def test_provisioned_reviewer_identity_verdict_passes_gate(
    client: AsyncClient, monkeypatch
):
    # AC (#432): a verdict from a DIFFERENT agent principal passes the gate.
    from hub import config

    monkeypatch.setattr(config, "HUB_TOKENS", _provisioned_reviewer_tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    monkeypatch.setattr(config, "REVIEW_SELF_APPROVE", "forbid")
    impl = {"Authorization": "Bearer cursor-tok"}
    reviewer = {"Authorization": "Bearer reviewer-tok"}

    task_id = await _task_in_review(
        client,
        "Reviewer identity passes",
        impl,
        agent="cursor",
        # This map spells its human token "human-tok" (#360).
        human_headers={"Authorization": "Bearer human-tok"},
    )
    resp = await client.post(
        f"/api/tasks/{task_id}/review-verdict",
        json={"verdict": "approved", "agent": "cursor-reviewer"},
        headers=reviewer,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["review_approved_current"] is True
    assert body["status"] == "running"


async def test_provisioned_implementer_identity_verdict_rejected(
    client: AsyncClient, monkeypatch
):
    # AC (#432): the implementing principal is rejected with a structured
    # reason; the gate compares principals, not the token role — both tokens
    # here share role `agent`.
    from hub import config

    monkeypatch.setattr(config, "HUB_TOKENS", _provisioned_reviewer_tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    monkeypatch.setattr(config, "REVIEW_SELF_APPROVE", "forbid")
    impl = {"Authorization": "Bearer cursor-tok"}

    task_id = await _task_in_review(
        client,
        "Implementer verdict blocked",
        impl,
        agent="cursor",
        # This map spells its human token "human-tok" (#360).
        human_headers={"Authorization": "Bearer human-tok"},
    )
    resp = await client.post(
        f"/api/tasks/{task_id}/review-verdict",
        json={"verdict": "approved", "agent": "cursor"},
        headers=impl,
    )
    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert detail["reason"] == "self_review_forbidden"
    assert detail["required_role"] == "independent_reviewer"
    assert "independent reviewer" in detail["hint"]
    # No verdict recorded; the task stays in review for a real reviewer.
    body = (await client.get(f"/api/tasks/{task_id}", headers=impl)).json()
    assert body["review_verdict"] is None
    assert body["status"] == "review"


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
            "affected_areas": ["hub/services/dor.py"],
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
            # #549: required, no default — a submission that omits it is a 422.
            "incomplete": False,
            "agent_count": 36,
            "tokens_spent": 1428876,
            "duration_ms": 716827,
            "harness_skill": "multi-agent-review",
            "harness_version": 1,
            "orchestrator": "claude-code-workflow",
            "findings_confirmed": [
                {
                    "locator": "lines",
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
        json={
            "raw_count": 3,
            "incomplete": False,
            "findings_confirmed": [],
            "findings_rejected": [],
        },
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
        json={"raw_count": 1, "incomplete": False},
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
            "incomplete": False,
            "tokens_spent": 1_000_000,
            "duration_ms": 60_000,
            "harness_skill": "multi-agent-review",
            "harness_version": 1,
            "findings_confirmed": [
                {
                    "locator": "none",
                    "title": "a",
                    "severity": "low",
                    "category": "tests",
                },
                {
                    "locator": "none",
                    "title": "b",
                    "severity": "medium",
                    "category": "consistency",
                },
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
            "incomplete": False,
            "harness_skill": "multi-agent-review",
            "harness_version": 2,
            "findings_confirmed": [
                {
                    "locator": "none",
                    "title": "e",
                    "severity": "low",
                    "category": "tests",
                },
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
    # #516: cost per finding divides by the findings of the reports that
    # actually reported a cost — 2 of the 3 here. Dividing by all 3 mixed a
    # report that named its cost with one that did not, and understated the
    # answer; this assertion used to pin that understatement in place.
    assert mr["confirmed_with_tokens"] == 2
    assert mr["tokens_per_confirmed"] == round(1_000_000 / 2)
    assert mr["reports_without_tokens"] == 1  # второй отчёт без токенов
    # #519: filtration divides by the findings actually adjudicated — here
    # 3 confirmed + 2 rejected — not by the self-reported raw_count of 16.
    # Against raw it read 0.813 while 11 of the 16 raw findings were never
    # sorted into any bucket; those were being counted as filtered noise.
    assert abs(mr["filtration_rate"] - (1 - 3 / 5)) < 0.001
    assert mr["findings_unaccounted"] == 11

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

    # #810: the completion has to be stamped. These rows used to carry only
    # ready_at and updated_at, and the median was built from the latter.
    for hours in (2, 4, 100):
        tv = await services_module.create_task(
            db, TaskCreate(title=f"Cycle {hours}", work_type="bug")
        )
        await db.execute(
            "UPDATE tasks SET status='completed', ready_at=datetime('now', ?), "
            "completed_at=datetime('now'), updated_at=datetime('now') WHERE id=?",
            (f"-{hours} hours", tv.id),
        )
    await db.commit()
    assert await repo_module.get_task(db, tv.id) is not None

    resp = await client.get("/api/metrics/practices")
    cycles = {c["work_type"]: c for c in resp.json()["cycle_times"]}
    assert cycles["bug"]["tasks"] == 3
    assert 3.5 <= cycles["bug"]["median_hours"] <= 4.5  # медиана, не среднее


async def test_practice_metrics_cycle_times_measure_completion(client: AsyncClient, db):
    # AC-2 (#517): cycle time is measured from completed_at, and the same clock
    # filters the window. Each case uses its own work_type so one median cannot
    # hide another's error.
    from hub import services as services_module
    from hub.models import TaskCreate

    async def _mk(work_type: str, ready: str, completed: str | None, updated: str):
        tv = await services_module.create_task(
            db, TaskCreate(title=f"Cycle {work_type}", work_type=work_type)
        )
        await db.execute(
            "UPDATE tasks SET status='completed', ready_at=datetime('now', ?), "
            "completed_at=CASE WHEN ? IS NULL THEN NULL "
            "ELSE datetime('now', ?) END, updated_at=datetime('now', ?) WHERE id=?",
            (ready, completed, completed, updated, tv.id),
        )
        return tv.id

    # Finished 4h ago, edited since. updated_at would report 10h — the whole
    # point is that a later edit is not a later completion.
    await _mk("refactor", "-10 hours", "-4 hours", "-1 minutes")
    # Finished 100 days ago, edited yesterday. Under the old query updated_at
    # both admitted it to the 90-day window and gave it a 100-day cycle time.
    await _mk("spike", "-200 days", "-100 days", "-1 days")
    # Finished before completed_at was ever written. It used to be estimated
    # from updated_at; since #810 it is counted, not estimated.
    await _mk("docs", "-8 hours", None, "-2 hours")
    await db.commit()

    resp = await client.get("/api/metrics/practices")
    cycles = {c["work_type"]: c for c in resp.json()["cycle_times"]}

    assert 5.5 <= cycles["refactor"]["median_hours"] <= 6.5
    assert cycles["refactor"]["no_completion_tasks"] == 0
    assert "spike" not in cycles, "row completed outside the window must not count"
    # #810: no completion stamp, so no duration — the row is counted and named
    # instead of being given a plausible median built from updated_at.
    assert cycles["docs"]["median_hours"] is None
    assert cycles["docs"]["tasks"] == 0
    assert cycles["docs"]["no_completion_tasks"] == 1


async def test_metrics_page_renders(client: AsyncClient, db):
    resp = await client.get("/metrics")
    assert resp.status_code == 200
    assert "Practice Metrics" in resp.text


# ---- Deprecated proposal action route: human gate (#359) ------------------


def _proposal_action_tokens() -> dict:
    from hub.config import TokenIdentity

    return {
        "agent-token": TokenIdentity("bot", "agent"),
        "human-token": TokenIdentity("denis", "human"),
    }


async def _draft_ready_for_approval(client: AsyncClient, headers: dict) -> int:
    """A draft that already passes DoR, refined by the agent itself.

    This detail is the whole point of the test. The pre-fix route was not
    unreachable for an agent — it answered 422 dor_failed, which reads like a
    gate but is only an unready task. An agent may refine its own draft
    (tasks.refine), so it clears DoR by itself and the route then approves.
    A test built on an unrefined draft would pass against the vulnerable code.
    """
    resp = await client.post(
        "/api/tasks",
        json={"title": "Proposal action draft", "source": "agent"},
        headers=headers,
    )
    task_id = resp.json()["id"]
    await client.post(
        f"/api/tasks/{task_id}/refine",
        json={
            "user_story": "As an agent I want my draft ready so that DoR passes.",
            "problem_statement": "Draft must clear DoR before approval.",
            "business_value": "Makes the gate the only thing left standing.",
            "scope_in": ["approval path"],
            "size": "S",
            "wip_tag": "bugfix",
            "affected_areas": ["hub/services/dor.py"],
            "validation_commands": ["uv run pytest -q"],
            "acceptance_criteria": [
                {
                    "id": "AC-1",
                    "given": "g",
                    "when": "w",
                    "then": "t",
                    "verifiable_by": "manual",
                }
            ],
        },
        headers=headers,
    )
    return task_id


async def test_proposal_action_compat_rejects_agent_token(
    client: AsyncClient, monkeypatch
):
    # AC-1 (#359): the agent must not approve its own draft through the
    # deprecated route. Before the gate this returned 200 and the task went
    # straight to running with a dispatched job — approve and dispatch in one
    # call, because the route builds TaskApprove(run=True).
    from hub import config

    monkeypatch.setattr(config, "HUB_TOKENS", _proposal_action_tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    agent = {"Authorization": "Bearer agent-token"}

    task_id = await _draft_ready_for_approval(client, agent)

    resp = await client.post(
        f"/api/proposals/{task_id}/action",
        json={"action": "approved"},
        headers=agent,
    )

    assert resp.status_code == 403
    assert resp.json()["detail"]["reason"] == "human_only_gate"
    after = await client.get(f"/api/tasks/{task_id}", headers=agent)
    assert after.json()["status"] == "draft"


async def test_proposal_action_compat_rejects_agent_rejection_too(
    client: AsyncClient, monkeypatch
):
    # The gate covers the whole route, not just the approve branch: letting an
    # agent reject drafts is the same human decision in the other direction.
    from hub import config

    monkeypatch.setattr(config, "HUB_TOKENS", _proposal_action_tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    agent = {"Authorization": "Bearer agent-token"}

    task_id = await _draft_ready_for_approval(client, agent)

    resp = await client.post(
        f"/api/proposals/{task_id}/action",
        json={"action": "rejected"},
        headers=agent,
    )

    assert resp.status_code == 403
    after = await client.get(f"/api/tasks/{task_id}", headers=agent)
    assert after.json()["status"] == "draft"


async def test_proposal_action_compat_human_still_approves(
    client: AsyncClient, monkeypatch
):
    # AC-2 (#359): the route is gated, not removed — a human keeps the
    # deprecated path working.
    from hub import config

    monkeypatch.setattr(config, "HUB_TOKENS", _proposal_action_tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    agent = {"Authorization": "Bearer agent-token"}
    human = {"Authorization": "Bearer human-token"}

    task_id = await _draft_ready_for_approval(client, agent)

    resp = await client.post(
        f"/api/proposals/{task_id}/action",
        json={"action": "approved"},
        headers=human,
    )

    assert resp.status_code == 200
    assert resp.json()["status"] != "draft"


# ---- Agents propose, humans create (#360) ---------------------------------


def _create_gate_tokens() -> dict:
    from hub.config import TokenIdentity

    return {
        "agent-token": TokenIdentity("bot", "agent"),
        "human-token": TokenIdentity("denis", "human"),
    }


async def test_agent_cannot_create_ready_task(client: AsyncClient, monkeypatch):
    # AC-1 (#360): source used to be trusted from the body, so an agent could
    # call itself human and land a task in open — with run_immediately, straight
    # into running plus a dispatch. Refused outright, not downgraded to a draft:
    # a silent draft would look like the call succeeded.
    from hub import config

    monkeypatch.setattr(config, "HUB_TOKENS", _create_gate_tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    agent = {"Authorization": "Bearer agent-token"}

    resp = await client.post(
        "/api/tasks",
        json={"title": "Agent made this", "source": "human", "run_immediately": True},
        headers=agent,
    )

    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert detail["reason"] == "agent_create_forbidden"
    assert detail["suggested_tool"] == "hub_propose_task"

    listing = await client.get("/api/tasks", headers=agent)
    assert all(t["title"] != "Agent made this" for t in listing.json())


async def test_agent_default_source_is_also_refused(client: AsyncClient, monkeypatch):
    # TaskCreate defaults source to human, so omitting the field is the same
    # request in disguise — the gate must not depend on it being spelled out.
    from hub import config

    monkeypatch.setattr(config, "HUB_TOKENS", _create_gate_tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    agent = {"Authorization": "Bearer agent-token"}

    resp = await client.post(
        "/api/tasks", json={"title": "No source field"}, headers=agent
    )

    assert resp.status_code == 403
    assert resp.json()["detail"]["reason"] == "agent_create_forbidden"


async def test_agent_may_still_propose_a_draft(client: AsyncClient, monkeypatch):
    # The gate must not close the path hub_propose_task uses: source=agent is
    # how an agent legitimately asks for work, and it lands in draft.
    from hub import config

    monkeypatch.setattr(config, "HUB_TOKENS", _create_gate_tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    agent = {"Authorization": "Bearer agent-token"}

    resp = await client.post(
        "/api/tasks",
        json={"title": "Proposed properly", "source": "agent"},
        headers=agent,
    )

    assert resp.status_code == 200
    assert resp.json()["status"] == "draft"


async def test_human_creation_still_lands_open(client: AsyncClient, monkeypatch):
    # AC-2 (#360): the human path is untouched — this is the whole point of
    # gating by identity rather than removing the endpoint.
    from hub import config

    monkeypatch.setattr(config, "HUB_TOKENS", _create_gate_tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    human = {"Authorization": "Bearer human-token"}

    resp = await client.post(
        "/api/tasks", json={"title": "Human made this"}, headers=human
    )

    assert resp.status_code == 200
    assert resp.json()["status"] == "open"


async def test_agent_cannot_create_ready_subtasks(client: AsyncClient, monkeypatch):
    # AC-3 (#360): the bulk route takes source from the body too, and
    # hub_create_subtasks lets the caller name it — same hole, same gate.
    from hub import config

    monkeypatch.setattr(config, "HUB_TOKENS", _create_gate_tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    agent = {"Authorization": "Bearer agent-token"}
    human = {"Authorization": "Bearer human-token"}

    parent = await client.post(
        "/api/tasks", json={"title": "Parent for subtasks"}, headers=human
    )
    parent_id = parent.json()["id"]

    resp = await client.post(
        f"/api/tasks/{parent_id}/subtasks",
        json={"items": [{"title": "Ready child"}], "source": "human"},
        headers=agent,
    )

    assert resp.status_code == 403
    assert resp.json()["detail"]["reason"] == "agent_create_forbidden"

    children = await client.get(f"/api/tasks?parent_id={parent_id}", headers=agent)
    assert children.json() == []


async def test_agent_create_refusal_envelope_points_at_a_human(
    client: AsyncClient, monkeypatch
):
    """Machine-review finding (#360): a refusal must not tell the refused caller
    that it is still the actor.

    enrich_error_payload only forces actor_hint="human" for reasons listed in
    its special-case tuple. agent_create_forbidden was missing from it, so the
    403 carried required_role="human" and actor_hint="agent" at the same time —
    and per the MCP envelope contract actor_hint is what a client reads to
    decide who acts next. The obvious response to "you are still the actor" is
    to retry a call that can never succeed.
    """
    from hub import config

    monkeypatch.setattr(config, "HUB_TOKENS", _create_gate_tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    agent = {"Authorization": "Bearer agent-token"}

    resp = await client.post(
        "/api/tasks", json={"title": "Envelope check"}, headers=agent
    )

    detail = resp.json()["detail"]
    assert detail["actor_hint"] == "human"
    assert detail["awaiting"] == "none"
    assert detail["suggested_tool"] == "hub_propose_task"
    assert "hub_propose_task" in detail["next_action"]


async def test_admin_token_may_still_create_ready_work(
    client: AsyncClient, monkeypatch
):
    """Machine-review finding (#360): only the 'human' role was covered.

    is_agent is not simply role == "agent" — it falls back to
    ``not is_human and principal_id``. Admin passes because is_human covers
    admin/super_admin, but nothing pinned that, so a future change to either
    property could lock admins out of task creation silently.
    """
    from hub import config
    from hub.config import TokenIdentity

    monkeypatch.setattr(
        config,
        "HUB_TOKENS",
        {"admin-token": TokenIdentity("root", "admin")},
    )
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    admin = {"Authorization": "Bearer admin-token"}

    resp = await client.post(
        "/api/tasks", json={"title": "Admin made this"}, headers=admin
    )

    assert resp.status_code == 200
    assert resp.json()["status"] == "open"


async def test_agent_may_still_bulk_create_default_source_subtasks(
    client: AsyncClient, monkeypatch
):
    """Machine-review finding left UNRESOLVED in round 1 (its only refuter died).

    BulkChildTasksCreate.source defaults to agent, so the sanctioned shape —
    hub_create_subtasks with no explicit source — must still succeed. Only the
    refusal was covered, and a wiring mistake at this call site (a hardcoded
    source, or an inverted condition that still happens to reject the explicit
    source=human case) would leave that negative test green while breaking
    every agent's bulk subtask creation.
    """
    from hub import config

    monkeypatch.setattr(config, "HUB_TOKENS", _create_gate_tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    agent = {"Authorization": "Bearer agent-token"}
    human = {"Authorization": "Bearer human-token"}

    parent = await client.post(
        "/api/tasks",
        json={"title": "Parent for default-source children"},
        headers=human,
    )
    parent_id = parent.json()["id"]

    resp = await client.post(
        f"/api/tasks/{parent_id}/subtasks",
        json={"items": [{"title": "Proposed child"}]},
        headers=agent,
    )

    assert resp.status_code == 200, resp.text
    created = resp.json()
    assert len(created) == 1
    assert created[0]["status"] == "draft"


# ---- Machine-review honest incompleteness (#549) --------------------------


async def test_machine_review_carries_incomplete_and_unresolved(
    client: AsyncClient, db
):
    """AC-1: a run that lost agents says so in fields, not in prose.

    Before this, the harness skill instructed authors to write incompleteness as
    the first line of findings_confirmed, and a finding nobody could judge went
    into findings_rejected with a note saying "this is NOT a refutation". Both
    are readable by a human and invisible to anything automated.
    """
    task_id = await _reviewable_task(client, db)

    resp = await client.post(
        f"/api/tasks/{task_id}/machine-review",
        json={
            "raw_count": 5,
            "incomplete": True,
            "agent_count": 10,
            "findings_confirmed": [],
            "findings_rejected": [
                {"title": "really refuted", "category": "perf", "reason": "traced"}
            ],
            "unresolved": [
                {"title": "no verifier survived", "why": "connection closed"}
            ],
            "lost_dimensions": ["safety-resources"],
        },
    )
    assert resp.status_code == 200, resp.text

    mr = resp.json()
    assert mr["incomplete"] is True
    assert [u["title"] for u in mr["unresolved"]] == ["no verifier survived"]
    assert mr["unresolved"][0]["why"] == "connection closed"
    assert mr["lost_dimensions"] == ["safety-resources"]
    # The whole point: unresolved is not rejected.
    assert [f["title"] for f in mr["findings_rejected"]] == ["really refuted"]


async def test_review_brief_exposes_machine_review_incompleteness(
    client: AsyncClient, db
):
    """AC-2: two runs, both "0 confirmed", must be machine-distinguishable."""
    clean_id = await _reviewable_task(client, db)
    await client.post(
        f"/api/tasks/{clean_id}/machine-review",
        json={"raw_count": 0, "incomplete": False},
    )
    dirty_id = await _reviewable_task(client, db)
    await client.post(
        f"/api/tasks/{dirty_id}/machine-review",
        json={
            "raw_count": 0,
            "incomplete": True,
            "unresolved": [{"title": "unjudged", "why": "agent died"}],
        },
    )

    clean = (await client.get(f"/api/tasks/{clean_id}/review-brief")).json()
    dirty = (await client.get(f"/api/tasks/{dirty_id}/review-brief")).json()

    assert clean["machine_review"]["findings_confirmed"] == []
    assert dirty["machine_review"]["findings_confirmed"] == []
    # Identical on findings, opposite on trustworthiness — without reading prose.
    assert clean["machine_review"]["incomplete"] is False
    assert dirty["machine_review"]["incomplete"] is True
    assert len(dirty["machine_review"]["unresolved"]) == 1


async def test_machine_review_requires_explicit_incomplete(client: AsyncClient, db):
    """AC-3: omitting the field is a 422, not a silent False.

    A default would be filled in by every client that forgot it, which is the
    substitution the field exists to prevent.
    """
    task_id = await _reviewable_task(client, db)

    resp = await client.post(
        f"/api/tasks/{task_id}/machine-review",
        json={"raw_count": 0, "findings_confirmed": []},
    )

    assert resp.status_code == 422
    assert "incomplete" in resp.text


# ---- Update authorship: principal, not the name in the body (#559) ----


async def test_task_update_records_authenticated_principal(
    client: AsyncClient, monkeypatch
):
    """#559 AC-1. The body may claim any name; the principal is the fact.

    Observed live on 30.07.2026: a done report appeared "from pda_claude" out
    of a parallel session, and telling whose it was took guesswork over
    timestamps and writing style.
    """
    from hub import config

    monkeypatch.setattr(config, "HUB_TOKENS", _principal_tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    human = {"Authorization": "Bearer human-token"}
    impl = {"Authorization": "Bearer impl-pid-token"}

    created = await client.post(
        "/api/tasks", json={"title": "Authorship"}, headers=human
    )
    task_id = created.json()["id"]

    resp = await client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": "somebody-elses-name", "kind": "status", "content": "x"},
        headers=impl,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["principal_id"] == 7, "the principal must come from the token"
    assert body["author_kind"] == "principal"
    assert body["agent"] == "somebody-elses-name", (
        "the display name is stored as sent — the task does not forbid it"
    )


async def test_update_feed_exposes_principal_when_known(
    client: AsyncClient, monkeypatch
):
    """#559 AC-3. A hub-written update and a principal-written one must be
    distinguishable without guessing from the name."""
    from hub import config
    from hub import repository as repo_module

    monkeypatch.setattr(config, "HUB_TOKENS", _principal_tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    human = {"Authorization": "Bearer human-token"}
    impl = {"Authorization": "Bearer impl-pid-token"}

    created = await client.post("/api/tasks", json={"title": "Feed"}, headers=human)
    task_id = created.json()["id"]
    await client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": "hub", "kind": "status", "content": "by a principal"},
        headers=impl,
    )

    feed = (await client.get(f"/api/tasks/{task_id}/updates", headers=human)).json()
    mine = [u for u in feed if u["content"] == "by a principal"][0]
    assert mine["principal_id"] == 7 and mine["author_kind"] == "principal"

    # An update the hub writes itself has no principal, and says why.
    assert repo_module.add_task_update  # writer used by the conveyor
    hub_written = [u for u in feed if u["author_kind"] == "hub"]
    for u in hub_written:
        assert u["principal_id"] is None


async def test_open_mode_update_has_no_principal(client: AsyncClient, monkeypatch):
    """#559 AC-4. No tokens configured: the write still works, and "no identity"
    is recorded as such rather than as the hub or as history."""
    from hub import config

    monkeypatch.setattr(config, "HUB_TOKENS", {})
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", True)

    created = await client.post("/api/tasks", json={"title": "Open mode"})
    task_id = created.json()["id"]

    resp = await client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": "anyone", "kind": "status", "content": "x"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["principal_id"] is None
    assert resp.json()["author_kind"] == "anonymous"


async def test_review_verdict_update_records_the_reviewer_principal(
    client: AsyncClient, monkeypatch
):
    """#559 scope item 5: the verdict is authored by a principal too.

    The endpoint already resolved an identity to check reviewer independence.
    Filing the verdict without it would have labelled a principal-authored
    update as hub-written — the very confusion this task removes, reintroduced
    one function over.
    """
    from hub import config

    monkeypatch.setattr(config, "HUB_TOKENS", _principal_tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    monkeypatch.setattr(config, "REVIEW_SELF_APPROVE", "forbid")
    human = {"Authorization": "Bearer human-token"}
    reviewer = {"Authorization": "Bearer other-pid-token"}

    created = await client.post("/api/tasks", json={"title": "Verdict"}, headers=human)
    task_id = created.json()["id"]
    await client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": "dev", "kind": "status", "content": "Plan: implement"},
        headers=human,
    )
    await client.post(
        f"/api/tasks/{task_id}/pair-start",
        json={"assigned_agent": "dev", "session_id": "s-impl-pid"},
        headers={"Authorization": "Bearer impl-pid-token"},
    )
    await client.post(
        f"/api/tasks/{task_id}/submit-review",
        json={},
        headers={"Authorization": "Bearer impl-pid-token"},
    )

    resp = await client.post(
        f"/api/tasks/{task_id}/review-verdict",
        json={"verdict": "approved", "agent": "any-display-name"},
        headers=reviewer,
    )
    assert resp.status_code == 200, resp.text

    feed = (await client.get(f"/api/tasks/{task_id}/updates", headers=human)).json()
    verdicts = [u for u in feed if u["kind"] == "review"]
    assert verdicts, "the verdict must appear in the feed"
    assert verdicts[-1]["principal_id"] == 8
    assert verdicts[-1]["author_kind"] == "principal"


# ---- CI reports the run, the hub checks and keeps it (#546) ----
#
# Execution belongs to CI: the production hub has no test runner by decision of
# 31.07.2026, so it must never be the thing that runs task-supplied commands.
# What it owes in exchange is judgement — a report counts only for the commit it
# pinned at submission (#572), and silence is never read as success.


def _ci_headers(monkeypatch) -> dict:
    """An identity that holds tasks.ci_report.

    Production grants it through a DB principal with the ci_runner role; among env
    tokens only ``admin`` carries every permission. Human and agent tokens must be
    refused — asserted in test_ci_report_rejects_bad_token_and_stale_generation.
    """
    from hub import config

    monkeypatch.setattr(
        config,
        "HUB_TOKENS",
        config.parse_tokens(
            "denis:human-token:human,cursor:agent-token:agent,ci:ci-token:admin"
        ),
    )
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    return {"Authorization": "Bearer ci-token"}


async def _ci_reporting_task(client: AsyncClient, monkeypatch, tip: str) -> int:
    """A submitted pair task whose pinned tip is ``tip``, with one test-AC."""
    from unittest.mock import AsyncMock

    from hub.integrations.noop import NoopGitOps
    from hub.integrations.registry import plugins
    from hub.services import orchestration

    class _Git(NoopGitOps):
        async def fetch_base(self, repo: str, base: str):
            return (True, "")

        async def head_sha(self, repo: str, base: str) -> str:
            return tip

    monkeypatch.setattr(plugins, "git_ops", _Git())
    monkeypatch.setattr(
        orchestration,
        "project_git_context",
        AsyncMock(return_value={"repo": "/srv/ws", "base_branch": "develop"}),
    )
    resp = await client.post("/api/tasks", json={"title": "Report me"})
    task_id = resp.json()["id"]
    await client.post(
        f"/api/tasks/{task_id}/refine",
        json={
            "acceptance_criteria": [
                {
                    "id": "AC-1",
                    "given": "g",
                    "when": "w",
                    "then": "t",
                    "verifiable_by": "test",
                    "test_ref": "tests/test_x.py::test_a",
                }
            ],
            "validation_commands": ["uv run pytest -q"],
        },
    )
    await client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": "dev", "kind": "status", "content": "Plan: do it"},
    )
    await client.post(
        f"/api/tasks/{task_id}/pair-start", json={"assigned_agent": "dev"}
    )
    resp = await client.post(f"/api/tasks/{task_id}/submit-review", json={})
    assert resp.status_code == 200, resp.text
    return task_id


async def test_ci_reports_ac_test_results(client: AsyncClient, monkeypatch):
    # AC-1 (#546): a run reported by CI for the pinned commit lands as the AC
    # result for the current generation — nobody calls run-ac-tests by hand.
    task_id = await _ci_reporting_task(client, monkeypatch, "sha-report-one")

    ci = _ci_headers(monkeypatch)
    resp = await client.post(
        f"/api/tasks/{task_id}/ci-run-report",
        json={"head_sha": "sha-report-one", "ac_results": {"AC-1": "pass"}},
        headers=ci,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["applied"] is True, body["reason"]

    brief = (await client.get(f"/api/tasks/{task_id}/review-brief", headers=ci)).json()
    assert brief["ac_test_results"] == [
        {"ac_id": "AC-1", "status": "pass", "is_current": True}
    ], "the reported result must be the current generation's AC evidence"
    assert brief["ci_run_report"]["state"] == "current"


async def test_ci_reports_validation_result(client: AsyncClient, db, monkeypatch):
    # AC-2 (#546): the validation outcome is recorded against the current
    # generation, so validation_gap (#510) stops seeing a gap — without anyone
    # calling run-validation, and without the hub executing the commands.
    from hub.services.validation_run import validation_gap

    task_id = await _ci_reporting_task(client, monkeypatch, "sha-report-two")

    ci = _ci_headers(monkeypatch)
    resp = await client.post(
        f"/api/tasks/{task_id}/ci-run-report",
        json={
            "head_sha": "sha-report-two",
            "validation_status": "pass",
            "validation_log": "$ uv run pytest -q\n1358 passed",
        },
        headers=ci,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["applied"] is True

    # Read the stored row, not TaskView: these columns are the gate's input and
    # are deliberately not part of the public task payload.
    from hub import repository as _repo

    task = dict(await _repo.get_task(db, task_id))
    assert task["validation_status"] == "pass"
    assert task["validation_generation"] == task["submission_generation"]
    assert validation_gap(task) is None, "a green reported run must close the gap"


async def test_ci_report_rejects_bad_token_and_stale_generation(
    client: AsyncClient, monkeypatch
):
    # AC-4 (#546): the intake is a write path, so it is guarded three ways —
    # by permission, by generation, and by commit. An agent token is the
    # interesting negative case: it is a legitimate hub identity that must
    # still not be able to write run evidence, because this token lives in a
    # CI secret and its blast radius has to stay at "report a run".
    task_id = await _ci_reporting_task(client, monkeypatch, "sha-refusal-case")
    ci = _ci_headers(monkeypatch)

    forbidden = await client.post(
        f"/api/tasks/{task_id}/ci-run-report",
        json={"head_sha": "sha-refusal-case", "ac_results": {"AC-1": "pass"}},
        headers={"Authorization": "Bearer agent-token"},
    )
    assert forbidden.status_code == 403, forbidden.text

    unauthenticated = await client.post(
        f"/api/tasks/{task_id}/ci-run-report",
        json={"head_sha": "sha-refusal-case", "ac_results": {"AC-1": "pass"}},
    )
    assert unauthenticated.status_code == 401, unauthenticated.text

    stale = await client.post(
        f"/api/tasks/{task_id}/ci-run-report",
        json={
            "head_sha": "sha-refusal-case",
            "submission_generation": 99,
            "ac_results": {"AC-1": "pass"},
        },
        headers=ci,
    )
    assert stale.status_code == 409, stale.text
    assert "stale report" in stale.json()["detail"]

    other_commit = await client.post(
        f"/api/tasks/{task_id}/ci-run-report",
        json={"head_sha": "sha-other-commit", "ac_results": {"AC-1": "pass"}},
        headers=ci,
    )
    assert other_commit.status_code == 200, other_commit.text
    assert other_commit.json()["applied"] is False
    assert "закреплён" in other_commit.json()["reason"]

    # None of the three refusals may have written evidence for this submission.
    brief = (await client.get(f"/api/tasks/{task_id}/review-brief", headers=ci)).json()
    assert brief["ac_test_results"] == []
    assert brief["ci_run_report"]["state"] == "unknown"


async def test_zero_raw_machine_review_warns_but_is_accepted(client: AsyncClient):
    # #750 AC-3: a zero-candidate report is accepted (evidence is worth
    # keeping) but the hub says out loud that "clean" was not demonstrated —
    # once per generation, so repeated stubs do not turn into noise.
    task_id = await _running_pair_task(client, "Zero raw review")
    await client.post(f"/api/tasks/{task_id}/submit-review", json={"agent": "dev"})

    body = {
        "harness_skill": "multi-agent-review",
        "harness_version": 7,
        "agent_count": 1,
        "raw_count": 0,
        "findings_confirmed": [],
        "findings_rejected": [],
        "incomplete": False,
        "unresolved": [],
        "lost_dimensions": [],
        "agent": "cursor_cloud",
    }
    resp = await client.post(f"/api/tasks/{task_id}/machine-review", json=body)
    assert resp.status_code == 200, resp.text

    data = (await client.get(f"/api/tasks/{task_id}")).json()
    alerts = [
        u["content"]
        for u in data["updates"] or []
        if u["kind"] == "alert" and "raw_count=0" in u["content"]
    ]
    assert len(alerts) == 1, "the stub shape must be named, once"
    assert "харнесс не запускался" in alerts[0]

    resp = await client.post(f"/api/tasks/{task_id}/machine-review", json=body)
    assert resp.status_code == 200, resp.text
    data = (await client.get(f"/api/tasks/{task_id}")).json()
    alerts = [
        u["content"]
        for u in data["updates"] or []
        if u["kind"] == "alert" and "raw_count=0" in u["content"]
    ]
    assert len(alerts) == 1, "a repeated stub must not repeat the alert"


# ---- #806: declared size stops excusing a change to code ----
#
# The cascade let XS/S out before it ever looked at what the work touches.
# #522 was S, changed orchestration.py, mcp_server.py and a template, and
# reached the human gate with no report and no notice that one was missing.


def _mr_size_task(**overrides) -> dict:
    task = {
        "work_type": "feature",
        "size": "S",
        "risks": "[]",
        "machine_review_override": "",
    }
    task.update(overrides)
    return task


def test_small_code_task_still_requires_machine_review():
    from hub.services.orchestration import machine_review_required

    for work_type in ("feature", "bug", "refactor", "incident"):
        for size in ("XS", "S"):
            task = _mr_size_task(work_type=work_type, size=size)
            assert machine_review_required(task, "auto") is True, (
                f"{work_type}/{size} changes code: the declared size is the "
                "author's own estimate, not a reason to skip review"
            )


def test_docs_task_stays_exempt_from_machine_review():
    from hub.services.orchestration import machine_review_required

    for work_type in ("docs", "chore", "spike"):
        task = _mr_size_task(work_type=work_type, size="S")
        assert machine_review_required(task, "auto") is False, (
            "the exemption that survives is the one about the nature of the "
            "work, not about its declared size"
        )


def test_task_override_skip_still_wins():
    from hub.services.orchestration import machine_review_required

    task = _mr_size_task(work_type="feature", size="S", machine_review_override="skip")
    assert machine_review_required(task, "auto") is False
    assert machine_review_required(task, "always") is False, (
        "an explicit override stays above both the new rule and the policy"
    )


# ---- #836: the hub hands back the baseline for waiting on this verdict ----


async def _submitted_pair_task(client: AsyncClient) -> tuple[int, dict]:
    created = await client.post("/api/tasks", json={"title": "Wait baseline"})
    task_id = created.json()["id"]
    await client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": "dev", "kind": "status", "content": "Plan: work"},
    )
    await client.post(
        f"/api/tasks/{task_id}/pair-start", json={"assigned_agent": "dev"}
    )
    resp = await client.post(f"/api/tasks/{task_id}/submit-review", json={})
    assert resp.status_code == 200, resp.text
    return task_id, resp.json()


def _diverges(baseline: dict, task: dict) -> dict:
    """Ask the REAL hook whether this baseline diverges from the live task.

    Importing the shipped hook rather than reimplementing its comparison: a
    copy of the logic would pass while the hook that actually wakes agents
    behaves differently, which is precisely the class of defect this task
    exists to close. Skipped, never silently reimplemented, if the hook is
    absent from a checkout.
    """
    import importlib.util

    hook_path = Path(__file__).resolve().parents[1] / ".claude/hooks/hub_wait_hook.py"
    if not hook_path.exists():
        pytest.skip(f"wait hook not present at {hook_path}")
    spec = importlib.util.spec_from_file_location("hub_wait_hook", hook_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module.changed_fields(task, baseline)


async def test_submit_for_review_returns_wait_baseline(client: AsyncClient):
    # AC-1 (#836): the agent should not have to infer which fields mark "my
    # verdict arrived" — the hub states them, with the values it just wrote.
    _task_id, submitted = await _submitted_pair_task(client)

    baseline = submitted["wait_baseline"]

    assert set(baseline) == {"review_approved_current", "review_verdict_generation"}
    assert baseline["review_approved_current"] is False
    assert "verdict" not in baseline, (
        "the raw verdict field carries the previous generation across a "
        "resubmission — watching it is the defect this closes"
    )


async def test_wait_baseline_is_quiet_across_resubmission(client: AsyncClient):
    # AC-2 (#836): the exact sequence that misfired on #826 — approve, then
    # resubmit. The old approval must not read as an event.
    task_id, _submitted = await _submitted_pair_task(client)
    await client.post(
        f"/api/tasks/{task_id}/review-verdict",
        json={"verdict": "approved", "agent": "reviewer"},
    )
    resubmitted = (
        await client.post(f"/api/tasks/{task_id}/submit-review", json={})
    ).json()

    baseline = resubmitted["wait_baseline"]
    live = (await client.get(f"/api/tasks/{task_id}")).json()

    assert live["latest_review"]["verdict"] == "approved", "the stale verdict is there"
    assert live["review_approved_current"] is False
    assert _diverges(baseline, live) == {}, "a resubmission must not wake the waiter"


async def test_wait_baseline_fires_on_current_verdict(client: AsyncClient):
    # AC-3 (#836): and it must wake on the verdict that is actually about
    # this submission — a baseline that never fires is worse than none.
    task_id, submitted = await _submitted_pair_task(client)
    baseline = submitted["wait_baseline"]

    await client.post(
        f"/api/tasks/{task_id}/review-verdict",
        json={"verdict": "approved", "agent": "reviewer"},
    )
    live = (await client.get(f"/api/tasks/{task_id}")).json()

    assert _diverges(baseline, live), "the verdict on this generation must wake it"
    assert live["review_approved_current"] is True


# --- Who may judge a finding (#876) -----------------------------------------


async def _task_with_machine_report(client: AsyncClient) -> int:
    """A submitted task carrying one report with two confirmed findings."""
    resp = await client.post("/api/tasks", json={"title": "Disposition gate"})
    task_id = resp.json()["id"]
    await client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": "dev", "kind": "status", "content": "Plan: work"},
    )
    await client.post(
        f"/api/tasks/{task_id}/pair-start", json={"assigned_agent": "dev"}
    )
    await client.post(f"/api/tasks/{task_id}/submit-review", json={})
    resp = await client.post(
        f"/api/tasks/{task_id}/machine-review",
        json={
            "harness_skill": "lite-diff-review",
            "model": "grok-4.6",
            "raw_count": 2,
            "findings_confirmed": [
                {"locator": "none", "title": "boundary lost", "severity": "medium"},
                {"locator": "none", "title": "race on retry", "severity": "high"},
            ],
            "findings_rejected": [],
            "incomplete": False,
            "unresolved": [],
            "lost_dimensions": [],
            "agent": "cursor-cloud-reviewer",
        },
    )
    assert resp.status_code == 200, resp.text
    return task_id


async def test_agent_cannot_set_finding_disposition(
    client: AsyncClient, monkeypatch, db
):
    # AC-2 (#876): an agent marking a finding false is the reviewed party
    # grading its reviewer. The refusal names its cause and changes nothing.
    from hub import config
    from hub import repository as repo_module

    task_id = await _task_with_machine_report(client)
    monkeypatch.setattr(config, "HUB_TOKENS", _review_tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    body = {"items": [{"finding_index": 0, "disposition": "false_positive"}]}

    resp = await client.post(
        f"/api/tasks/{task_id}/finding-dispositions",
        json=body,
        headers={"Authorization": "Bearer impl-token"},
    )

    assert resp.status_code == 403
    review = await repo_module.get_latest_machine_review(db, task_id)
    assert not await repo_module.list_finding_dispositions(db, review["id"])

    resp = await client.post(
        f"/api/tasks/{task_id}/finding-dispositions",
        json=body,
        headers={"Authorization": "Bearer human-token"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"review_id": review["id"], "confirmed_total": 2, "judged": 1}
    rows = [
        dict(r) for r in await repo_module.list_finding_dispositions(db, review["id"])
    ]
    assert rows[0]["decided_by"] == "denis", (
        "the author comes from the token, not the body"
    )


async def test_disposition_outside_the_report_is_refused(client: AsyncClient, db):
    # A judgement of finding #7 in a two-finding report is not partial success:
    # the caller judged something else, and storing it would poison precision.
    from hub import repository as repo_module

    task_id = await _task_with_machine_report(client)

    resp = await client.post(
        f"/api/tasks/{task_id}/finding-dispositions",
        json={
            "items": [
                {"finding_index": 0, "disposition": "fixed"},
                {"finding_index": 7, "disposition": "fixed"},
            ]
        },
    )

    assert resp.status_code == 400
    assert "outside" in resp.json()["detail"]
    review = await repo_module.get_latest_machine_review(db, task_id)
    assert not await repo_module.list_finding_dispositions(db, review["id"]), (
        "a rejected batch writes nothing at all, not its valid half"
    )


async def test_dispositions_without_a_report_are_refused(client: AsyncClient):
    # There is nothing to judge before a report exists, and inventing an empty
    # one would make "no review" and "review with no findings" the same fact.
    resp = await client.post("/api/tasks", json={"title": "No report yet"})
    task_id = resp.json()["id"]

    resp = await client.post(
        f"/api/tasks/{task_id}/finding-dispositions",
        json={"items": [{"finding_index": 0, "disposition": "fixed"}]},
    )

    assert resp.status_code == 404
    assert "no machine review" in resp.json()["detail"]


async def test_review_brief_shows_scope_growth(client, db, monkeypatch):
    """#890 AC-4: the reviewer sees which half of the scope is a fact.

    affected_areas mixes two different claims once areas can be accepted at
    submission: what was predicted at DoR and what was recorded after the code
    existed. A brief that showed only the merged list would let the second
    pass for the first — and #854 measured that the second is 44% of real
    work, not an edge case.
    """
    from hub import commit_scope, config
    from hub import repository as repo_module
    from hub import services as services_module
    from hub.integrations.noop import NoopGitOps
    from hub.integrations.registry import plugins
    from hub.models import TaskSubmitReview

    monkeypatch.setattr(config, "SDD_SURFACES", "warn")

    resp = await client.post("/api/tasks", json={"title": "Grew while working"})
    task_id = resp.json()["id"]
    await client.post(
        f"/api/tasks/{task_id}/refine", json={"affected_areas": ["hub/app.py"]}
    )
    await client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": "dev", "kind": "status", "content": "Plan: implement"},
    )
    await client.post(
        f"/api/tasks/{task_id}/pair-start", json={"assigned_agent": "dev"}
    )

    class _Diff(NoopGitOps):
        async def branch_diff_paths(self, branch, base_branch=None, repo=None):
            return ["hub/app.py", "hub/services/sessions.py"]

    plugins.git_ops = _Diff()
    await services_module.submit_for_review(
        db, task_id, TaskSubmitReview(accept_areas=True)
    )

    brief = (await client.get(f"/api/tasks/{task_id}/review-brief")).json()
    growth = brief["scope_growth"]
    assert len(growth) == 1, growth
    assert growth[0].startswith(commit_scope.SCOPE_GROWTH_MARKER)
    assert "+1" in growth[0] and "hub/services/sessions.py" in growth[0]

    task = dict(await repo_module.get_task(db, task_id))
    assert "hub/services/sessions.py" in (task["affected_areas"] or "")


# ---- #855: the cheap rule layer that runs before the paid reviewer ----


async def _rules_task(db, client, *, areas, work_type="feature"):
    """A running pair task with declared areas, ready to submit."""
    from hub import services as services_module
    from hub.models import TaskCreate, TaskRefine

    tv = await services_module.create_task(db, TaskCreate(title="Rules under test"))
    await repo_module.update_task_structured(
        db, tv.id, TaskRefine(affected_areas=areas, work_type=work_type)
    )
    await repo_module.add_task_update(db, tv.id, "dev", "status", "Plan: work")
    await db.commit()
    await services_module.pair_start_task(db, tv.id, caller="dev")
    return tv.id


from hub import repository as repo_module  # noqa: E402
from hub.integrations.noop import NoopGitOps  # noqa: E402


class _RulesDiff(NoopGitOps):
    # push/create succeed: since #967 a confirmed diff gets its PR opened at
    # submission, and a refusal would add its own alert to tests about rules.
    def __init__(self, paths):
        self._paths = paths

    async def branch_diff_paths(self, branch, base_branch=None, repo=None):
        return self._paths

    async def push_branch(self, branch, repo=None, force=False):
        return True

    async def create_pr(
        self,
        task_id,
        title,
        description,
        branch,
        repo=None,
        gh_repo=None,
        base_branch=None,
        forge: str = "",
    ):
        return 41


async def _rule_alerts(db, task_id):
    updates = await repo_module.get_task_updates(db, task_id)
    return [u["content"] for u in updates if u["kind"] == "alert"]


async def test_code_without_tests_is_reported_in_warn(client, db, monkeypatch):
    """#855 AC-1: the rule names the code it is speaking about, and lets it through."""
    from hub import config
    from hub import services as services_module
    from hub.integrations.registry import plugins

    monkeypatch.setattr(config, "SUBMIT_RULES", "warn")
    monkeypatch.setattr(config, "SDD_SURFACES", "warn")
    task_id = await _rules_task(db, client, areas=["hub"])
    plugins.git_ops = _RulesDiff(["hub/app.py", "hub/db.py", "uv.lock"])

    view = await services_module.submit_for_review(db, task_id)

    assert view.status.value == "review", "warn never blocks"
    alerts = await _rule_alerts(db, task_id)
    report = [a for a in alerts if "Отчёт проверок на сдаче" in a]
    assert len(report) == 1, alerts
    assert "Тесты рядом с кодом: СРАБОТАЛО" in report[0]
    assert "hub/app.py" in report[0] and "hub/db.py" in report[0]
    assert "uv.lock" not in report[0], "routine paths are not code"


async def test_code_without_tests_blocks_in_require(client, db, monkeypatch):
    """#855 AC-2: require refuses, and the refusal names both ways out."""
    import pytest as _pytest
    from fastapi import HTTPException as _HTTPException

    from hub import config
    from hub import services as services_module
    from hub.integrations.registry import plugins

    monkeypatch.setattr(config, "SUBMIT_RULES", "require")
    monkeypatch.setattr(config, "SDD_SURFACES", "off")
    task_id = await _rules_task(db, client, areas=["hub"])
    plugins.git_ops = _RulesDiff(["hub/app.py"])

    with _pytest.raises(_HTTPException) as exc_info:
        await services_module.submit_for_review(db, task_id)

    assert exc_info.value.status_code == 422
    detail = str(exc_info.value.detail)
    assert "hub/app.py" in detail
    assert "Принесите тест" in detail and "причину" in detail
    task = dict(await repo_module.get_task(db, task_id))
    assert task["status"] == "running", "a refused submission stays put"

    # The same diff with a test alongside passes untouched.
    plugins.git_ops = _RulesDiff(["hub/app.py", "tests/test_app.py"])
    view = await services_module.submit_for_review(db, task_id)
    assert view.status.value == "review"


async def test_rules_report_unknown_is_not_green(client, db, monkeypatch):
    """#855 AC-3: a diff that could not be read says so, in every mode."""
    from hub import config
    from hub import services as services_module
    from hub.integrations.registry import plugins

    monkeypatch.setattr(config, "SUBMIT_RULES", "require")
    monkeypatch.setattr(config, "SDD_SURFACES", "off")
    task_id = await _rules_task(db, client, areas=["hub"])
    plugins.git_ops = _RulesDiff(None)  # could not be determined

    view = await services_module.submit_for_review(db, task_id)

    assert view.status.value == "review", "unknown is not a refusal"
    report = " ".join(await _rule_alerts(db, task_id))
    assert "Правила по диффу НЕ проверялись" in report
    assert "Это не значит, что нарушений нет" in report


async def test_area_verdict_folds_into_rules_report(client, db, monkeypatch):
    """#855 AC-4: one report, not two independent alerts."""
    from hub import config
    from hub import services as services_module
    from hub.integrations.registry import plugins

    monkeypatch.setattr(config, "SUBMIT_RULES", "warn")
    monkeypatch.setattr(config, "SDD_SURFACES", "warn")
    task_id = await _rules_task(db, client, areas=["hub/app.py"])
    plugins.git_ops = _RulesDiff(["hub/app.py", "hub/db.py"])

    await services_module.submit_for_review(db, task_id)

    alerts = await _rule_alerts(db, task_id)
    combined = [a for a in alerts if "Отчёт проверок на сдаче" in a]
    assert len(combined) == 1, alerts
    # Both subjects live in the SAME message...
    assert "Вне объявленной области изменены" in combined[0]
    assert "Тесты рядом с кодом: СРАБОТАЛО" in combined[0]
    # ...and neither arrives as its own separate alert any more.
    assert [a for a in alerts if "Класс риска" not in a] == combined


async def test_bug_touching_only_tests_is_flagged_not_blocked(client, db, monkeypatch):
    """#855 AC-5: a signal on bug fixes, never a refusal."""
    from hub import config
    from hub import services as services_module
    from hub.integrations.registry import plugins

    monkeypatch.setattr(config, "SUBMIT_RULES", "require")
    monkeypatch.setattr(config, "SDD_SURFACES", "off")
    task_id = await _rules_task(db, client, areas=["tests"], work_type="bug")
    plugins.git_ops = _RulesDiff(["tests/test_api.py"])

    view = await services_module.submit_for_review(db, task_id)

    assert view.status.value == "review", "even require does not block this one"
    report = " ".join(await _rule_alerts(db, task_id))
    assert "Баг правит только тесты: отмечено" in report


# ---- #899: a refusal must be obeyable by the one it is addressed to ----
#
# A client fixes its tool schemas when the session starts. A session opened
# before #852 shipped has no session_id parameter, so "pass your session id"
# asks for something that session cannot do — observed on #498 and twice more
# the next day, each time rescued only because the agent knew REST by hand.


def _agent_headers(monkeypatch) -> dict[str, str]:
    from hub import config
    from hub.config import TokenIdentity

    monkeypatch.setattr(
        config,
        "HUB_TOKENS",
        {"agent-token": TokenIdentity("bot", "agent", principal_id=7)},
    )
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    return {"Authorization": "Bearer agent-token"}


async def test_missing_session_names_a_workable_path(
    client: AsyncClient, monkeypatch, db
):
    # AC-1 (#899): the refusal names a call the caller can make TODAY, with
    # the endpoint and the body — not a parameter its schema does not have.
    from hub import services
    from hub.models import TaskCreate

    task_id = (await services.create_task(db, TaskCreate(title="no session"))).id
    headers = _agent_headers(monkeypatch)

    claim = await client.post(
        f"/api/tasks/{task_id}/claim",
        json={"agent": "bot", "session_id": ""},
        headers=headers,
    )
    assert claim.status_code == 422, claim.text
    hint = claim.json()["detail"]["hint"]
    assert f"POST /api/tasks/{task_id}/claim" in hint
    assert '"agent"' in hint and '"session_id"' in hint

    # The other door into the task takes a different body, and the hint says
    # which — a reader left to guess the shape is stuck the same way.
    pair = await client.post(
        f"/api/tasks/{task_id}/pair-start",
        json={"assigned_agent": "bot", "session_id": ""},
        headers=headers,
    )
    assert pair.status_code == 422, pair.text
    pair_hint = pair.json()["detail"]["hint"]
    assert f"POST /api/tasks/{task_id}/pair-start" in pair_hint
    assert '"assigned_agent"' in pair_hint and '"plan"' in pair_hint


async def test_refusal_names_the_version_mismatch(client: AsyncClient, monkeypatch, db):
    # AC-2 (#899): the cause is stated as two versions disagreeing, not as
    # the caller getting it wrong. An agent that reads "your mistake" looks
    # for a mistake it cannot find, and stops.
    from hub import services
    from hub.models import TaskCreate

    task_id = (await services.create_task(db, TaskCreate(title="old schema"))).id
    headers = _agent_headers(monkeypatch)

    resp = await client.post(
        f"/api/tasks/{task_id}/claim",
        json={"agent": "bot", "session_id": ""},
        headers=headers,
    )
    detail = resp.json()["detail"]

    assert "version mismatch" in detail["hint"]
    assert "not your mistake" in detail["hint"]
    # And the way out is marked temporary: a workaround that reads as the
    # normal route becomes the normal route.
    assert "not the normal route" in detail["hint"]
    # The message carries the pointer too — a reader who only sees the one
    # line still learns that the hint has something usable in it.
    assert "predates the requirement" in detail["message"]
    # Passing the field is still the first thing asked for: this is a
    # fallback for schemas that cannot, not a replacement for the tool.
    assert detail["hint"].index("Pass your session id") < detail["hint"].index(
        "IF YOUR TOOL SCHEMA"
    )
