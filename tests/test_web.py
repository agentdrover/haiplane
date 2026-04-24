from __future__ import annotations

from httpx import AsyncClient


async def test_dashboard_page(client: AsyncClient):
    resp = await client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


async def test_tasks_page(client: AsyncClient):
    resp = await client.get("/tasks")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


async def test_task_detail_page(client: AsyncClient):
    create = await client.post("/api/tasks", json={"title": "Detail page task"})
    task_id = create.json()["id"]

    resp = await client.get(f"/tasks/{task_id}")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "Detail page task" in resp.text


async def test_inbox_partial(client: AsyncClient):
    resp = await client.get("/partials/inbox")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


async def test_kanban_partial(client: AsyncClient):
    resp = await client.get("/partials/kanban")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


async def test_epics_partial(client: AsyncClient):
    resp = await client.get("/partials/epics")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


async def test_web_create_task(client: AsyncClient):
    resp = await client.post(
        "/tasks/create",
        data={"title": "Web-created task", "description": "desc", "runtime": "auto"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/tasks"


async def test_web_approve_task(client: AsyncClient):
    create = await client.post(
        "/api/tasks",
        json={"title": "Draft to approve", "source": "agent", "agent": "bot"},
    )
    task_id = create.json()["id"]
    assert create.json()["status"] == "draft"

    # Web-approve goes through the same DoR gate as the API. The task
    # has no structured fields filled in, so request force=true.
    resp = await client.post(
        f"/tasks/{task_id}/web-approve",
        data={"comment": "ok", "force": "true"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert f"/tasks/{task_id}" in resp.headers["location"]
