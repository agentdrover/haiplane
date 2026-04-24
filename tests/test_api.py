from __future__ import annotations

from httpx import AsyncClient


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


async def test_reorder_task_api(client: AsyncClient):
    resp = await client.post("/api/tasks", json={"title": "Reorder me"})
    task_id = resp.json()["id"]

    resp = await client.patch(
        f"/api/tasks/{task_id}/reorder",
        json={"position": 5},
    )
    assert resp.status_code == 200
    assert resp.json()["position"] == 5
