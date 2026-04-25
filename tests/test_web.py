from __future__ import annotations

from httpx import AsyncClient

from hub import repository as repo


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


async def test_web_create_task_with_owner_reviewer(client: AsyncClient):
    page = await client.get("/")
    assert 'name="human_owner"' in page.text
    assert 'name="human_reviewer"' in page.text

    resp = await client.post(
        "/tasks/create",
        data={
            "title": "Web owned task",
            "description": "",
            "runtime": "auto",
            "human_owner": "alice",
            "human_reviewer": "bob",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    tasks = (await client.get("/api/tasks")).json()
    owned = [t for t in tasks if t["title"] == "Web owned task"]
    assert len(owned) == 1
    assert owned[0]["human_owner"] == "alice"
    assert owned[0]["human_reviewer"] == "bob"


async def test_task_detail_shows_owner_reviewer(client: AsyncClient):
    create = await client.post(
        "/api/tasks",
        json={"title": "Detail owner", "human_owner": "alice", "human_reviewer": "bob"},
    )
    task_id = create.json()["id"]
    resp = await client.get(f"/tasks/{task_id}")
    assert resp.status_code == 200
    assert "alice" in resp.text
    assert "bob" in resp.text


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


async def test_web_force_complete_records_hx_prompt(client: AsyncClient, db):
    create = await client.post("/api/tasks", json={"title": "Pending report via web"})
    task_id = create.json()["id"]
    await repo.update_task(db, task_id, status="pending_report")
    await db.commit()

    resp = await client.post(
        f"/tasks/{task_id}/web-force-complete",
        headers={"HX-Prompt": "reviewed manually, accepting risk"},
        follow_redirects=False,
    )

    assert resp.status_code in (200, 303)
    detail = await client.get(f"/api/tasks/{task_id}")
    data = detail.json()
    assert data["status"] == "completed"
    done_updates = [u for u in data["updates"] if u["kind"] == "done"]
    assert len(done_updates) == 1
    assert done_updates[0]["content"] == "reviewed manually, accepting risk"
    assert done_updates[0]["agent"] == "human"


async def test_web_force_complete_falls_back_to_form_comment(client: AsyncClient, db):
    create = await client.post("/api/tasks", json={"title": "Pending report form"})
    task_id = create.json()["id"]
    await repo.update_task(db, task_id, status="pending_report")
    await db.commit()

    resp = await client.post(
        f"/tasks/{task_id}/web-force-complete",
        data={"comment": "form-based override"},
        follow_redirects=False,
    )

    assert resp.status_code in (200, 303)
    detail = await client.get(f"/api/tasks/{task_id}")
    data = detail.json()
    assert data["status"] == "completed"
    done_updates = [u for u in data["updates"] if u["kind"] == "done"]
    assert len(done_updates) == 1
    assert done_updates[0]["content"] == "form-based override"
