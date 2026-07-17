from __future__ import annotations

import asyncio

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


async def test_tasks_page_filter_controls_have_consistent_markup(client: AsyncClient):
    resp = await client.get("/tasks")
    assert resp.status_code == 200
    assert resp.text.count('class="filter-control"') >= 6
    assert "filter-reset-btn" in resp.text


async def test_tasks_list_filters_ignore_blank_parent_id(client: AsyncClient):
    await client.post("/api/tasks", json={"title": "Open visible"})
    await client.post(
        "/api/tasks",
        json={"title": "Draft visible", "source": "agent", "agent": "bot"},
    )

    resp = await client.get(
        "/tasks/list",
        params={"status": "draft", "parent_id": ""},
    )

    assert resp.status_code == 200
    assert "Draft visible" in resp.text
    assert "Open visible" not in resp.text


async def test_tasks_table_actions_have_consistent_button_markup(client: AsyncClient):
    await client.post(
        "/api/tasks",
        json={"title": "Draft action", "source": "agent", "agent": "bot"},
    )
    resp = await client.get("/tasks")
    assert resp.status_code == 200
    assert "task-table-actions" in resp.text
    assert "task-table-action" in resp.text


async def test_tasks_table_badges_have_consistent_markup(client: AsyncClient):
    await client.post("/api/tasks", json={"title": "Badge row"})
    resp = await client.get("/tasks")
    assert resp.status_code == 200
    assert "task-table-badge--type" in resp.text
    assert "task-table-badge--status" in resp.text
    assert "task-table-badge--priority" in resp.text


async def test_task_detail_page(client: AsyncClient):
    create = await client.post("/api/tasks", json={"title": "Detail page task"})
    task_id = create.json()["id"]

    resp = await client.get(f"/tasks/{task_id}")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "Detail page task" in resp.text
    assert "task-hero" in resp.text
    assert "task-detail-layout" in resp.text
    assert "task-actions-card" in resp.text
    assert "task-meta-list" in resp.text


async def test_inbox_partial(client: AsyncClient):
    resp = await client.get("/partials/inbox")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "inbox-filter-bar" in resp.text


async def test_inbox_partial_mine_filter(client: AsyncClient):
    await client.post(
        "/api/tasks",
        json={
            "title": "Alice inbox draft",
            "source": "agent",
            "agent": "bot",
            "human_owner": "alice",
        },
    )
    await client.post(
        "/api/tasks",
        json={
            "title": "Bob inbox draft",
            "source": "agent",
            "agent": "bot",
            "human_owner": "bob",
        },
    )

    resp = await client.get("/partials/inbox", params={"mine": "alice"})
    assert resp.status_code == 200
    assert "Alice inbox draft" in resp.text
    assert "Bob inbox draft" not in resp.text
    assert 'name="mine"' in resp.text
    assert 'setAttribute("hx-get", "/partials/inbox?mine=alice")' in resp.text


async def test_inbox_proposals_render_as_collapsible_details(client: AsyncClient):
    await client.post(
        "/api/tasks",
        json={"title": "Draft proposal", "source": "agent", "agent": "bot"},
    )
    resp = await client.get("/partials/inbox")
    assert resp.status_code == 200
    assert 'id="inbox-proposals"' in resp.text
    assert "<details" in resp.text
    assert "<summary" in resp.text
    assert "Proposals" in resp.text


async def test_dashboard_shows_approve_and_run_when_dispatch_available(
    client: AsyncClient,
):
    await client.post(
        "/api/tasks",
        json={
            "title": "Draft with runnable agent",
            "source": "agent",
            "agent": "bot",
        },
    )

    resp = await client.get("/")

    assert resp.status_code == 200
    assert "Draft with runnable agent" in resp.text
    assert "Approve &amp; Run" in resp.text
    assert '"run": "true"' in resp.text


async def test_dashboard_hides_approve_and_run_when_dispatch_unavailable(
    client: AsyncClient,
):
    from hub.integrations.noop import NoopDispatch
    from hub.integrations.registry import plugins

    plugins.dispatch = NoopDispatch()
    await client.post(
        "/api/tasks",
        json={
            "title": "Draft without runnable agent",
            "source": "agent",
            "agent": "bot",
        },
    )

    resp = await client.get("/")

    assert resp.status_code == 200
    assert "Draft without runnable agent" in resp.text
    assert "Approve &amp; Run" not in resp.text
    assert '"run": "true"' not in resp.text


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


async def test_task_detail_renders_review_checklist_when_present(
    client: AsyncClient,
):
    create = await client.post(
        "/api/tasks",
        json={
            "title": "Detail with checklist",
            "review_checklist": ["check migration path", "verify rollback safety"],
        },
    )
    task_id = create.json()["id"]
    resp = await client.get(f"/tasks/{task_id}")
    assert resp.status_code == 200
    assert "Review Checklist" in resp.text
    assert "check migration path" in resp.text
    assert "verify rollback safety" in resp.text


async def test_task_detail_omits_review_checklist_when_empty(
    client: AsyncClient,
):
    create = await client.post("/api/tasks", json={"title": "Detail no checklist"})
    task_id = create.json()["id"]
    resp = await client.get(f"/tasks/{task_id}")
    assert resp.status_code == 200
    assert "Review Checklist" not in resp.text


async def test_task_detail_renders_analyst_handoff_fields(client: AsyncClient):
    create = await client.post(
        "/api/tasks",
        json={
            "title": "Analyst handoff detail",
            "description": "Detail description",
        },
    )
    task_id = create.json()["id"]
    await client.post(
        f"/api/tasks/{task_id}/refine",
        json={
            "work_type": "feature",
            "class_of_service": "standard",
            "size": "S",
            "wip_tag": "feature_work",
            "user_story": "Detail user story",
            "problem_statement": "Detail problem statement",
            "business_value": "Detail business value",
            "technical_hints": "Detail technical hints",
            "scope_in": ["Detail scope in"],
            "scope_out": ["Detail scope out"],
            "affected_areas": ["hub/templates/task_detail.html"],
            "validation_commands": ["uv run pytest tests/test_web.py -q"],
            "constraints": ["Detail constraint"],
            "assumptions": ["Detail assumption"],
            "out_of_scope_for_review": ["Detail ignored review item"],
            "review_checklist": ["Detail review check"],
        },
    )
    await client.put(
        f"/api/tasks/{task_id}/acceptance_criteria",
        json=[
            {
                "id": "AC-1",
                "given": "Detail AC given",
                "when": "Detail AC when",
                "then": "Detail AC then",
                "verifiable_by": "test",
                "test_ref": "tests/test_web.py::test_task_detail_renders_analyst_handoff_fields",
            }
        ],
    )
    await client.post(
        f"/api/tasks/{task_id}/risks",
        json={
            "kind": "security",
            "severity": "medium",
            "description": "Detail risk description",
            "mitigation": "Detail risk mitigation",
        },
    )

    resp = await client.get(f"/tasks/{task_id}")

    assert resp.status_code == 200
    expected = [
        "Readiness",
        "score=",
        "Developer Brief",
        "Detail user story",
        "Detail problem statement",
        "Detail business value",
        "Detail technical hints",
        "Detail scope in",
        "Detail scope out",
        "hub/templates/task_detail.html",
        "uv run pytest tests/test_web.py -q",
        "Detail constraint",
        "Detail assumption",
        "Detail ignored review item",
        "Acceptance Criteria",
        "AC-1",
        "Detail AC given",
        "Detail AC when",
        "Detail AC then",
        "tests/test_web.py::test_task_detail_renders_analyst_handoff_fields",
        "Risks",
        "security",
        "medium",
        "Detail risk description",
        "Detail risk mitigation",
    ]
    for item in expected:
        assert item in resp.text


async def test_task_detail_and_list_show_analyst_ready_badge(client: AsyncClient):
    create = await client.post(
        "/api/tasks",
        json={"title": "Prepared analyst task", "description": "ready"},
    )
    task_id = create.json()["id"]
    await client.post(
        f"/api/tasks/{task_id}/refine",
        json={
            "work_type": "feature",
            "size": "S",
            "wip_tag": "feature_work",
            "prepared_by": "analyst-agent",
            "prepared_at": "2026-04-30 00:00:00",
            "user_story": "As a reviewer I want a ready badge.",
            "problem_statement": "Prepared tasks need a clear signal.",
            "business_value": "Humans can trust prepared tasks faster.",
            "scope_in": ["Show Analyst Ready"],
            "validation_commands": ["uv run pytest tests/test_web.py -q"],
        },
    )
    await client.put(
        f"/api/tasks/{task_id}/acceptance_criteria",
        json=[
            {
                "id": "AC-1",
                "given": "A task is prepared",
                "when": "A human views it",
                "then": "Analyst Ready is visible",
                "verifiable_by": "test",
                "test_ref": "tests/test_web.py",
            }
        ],
    )
    detail = await client.get(f"/tasks/{task_id}")
    table = await client.get("/tasks")
    kanban = await client.get("/partials/kanban")

    assert "Analyst Ready" in detail.text
    assert "Prepared by analyst-agent" in detail.text
    assert "Analyst Ready" in table.text
    assert "Analyst Ready" in kanban.text


async def test_task_detail_does_not_show_analyst_ready_without_preparation_update(
    client: AsyncClient,
):
    create = await client.post(
        "/api/tasks",
        json={"title": "Ready but not analyst prepared", "description": "raw"},
    )
    task_id = create.json()["id"]
    await client.post(
        f"/api/tasks/{task_id}/refine",
        json={
            "work_type": "feature",
            "size": "S",
            "wip_tag": "feature_work",
            "user_story": "As a reviewer I want no false badge.",
            "problem_statement": "DoR alone is not analyst preparation.",
            "business_value": "Avoid misleading humans.",
            "scope_in": ["Avoid false ready badge"],
            "validation_commands": ["uv run pytest tests/test_web.py -q"],
        },
    )
    await client.put(
        f"/api/tasks/{task_id}/acceptance_criteria",
        json=[
            {
                "id": "AC-1",
                "given": "No analyst update exists",
                "when": "A human views the task",
                "then": "The ready badge is not shown",
                "verifiable_by": "test",
            }
        ],
    )

    detail = await client.get(f"/tasks/{task_id}")

    assert detail.status_code == 200
    assert "Analyst Ready" not in detail.text


async def test_tasks_page_filters_analyst_ready_tasks(client: AsyncClient):
    prepared = await client.post(
        "/api/tasks",
        json={"title": "Prepared filter task", "description": "ready"},
    )
    prepared_id = prepared.json()["id"]
    await client.post(
        f"/api/tasks/{prepared_id}/refine",
        json={
            "work_type": "feature",
            "size": "S",
            "wip_tag": "feature_work",
            "user_story": "As a reviewer I want a ready filter.",
            "problem_statement": "Prepared tasks need a list.",
            "business_value": "Humans find ready work quickly.",
            "scope_in": ["Filter Analyst Ready"],
            "validation_commands": ["uv run pytest tests/test_web.py -q"],
        },
    )
    await client.put(
        f"/api/tasks/{prepared_id}/acceptance_criteria",
        json=[
            {
                "id": "AC-1",
                "given": "A prepared task exists",
                "when": "The ready filter is applied",
                "then": "The task is listed",
                "verifiable_by": "test",
            }
        ],
    )
    await client.post(
        f"/api/tasks/{prepared_id}/updates",
        json={
            "agent": "analyst-agent",
            "kind": "status",
            "content": "Analyst preparation complete: ready for developer.",
        },
    )
    await client.post(
        "/api/tasks",
        json={"title": "Unprepared filter task", "description": "raw"},
    )

    page = await client.get("/tasks", params={"analyst_ready": "1"})
    partial = await client.get("/tasks/list", params={"analyst_ready": "1"})

    assert page.status_code == 200
    assert partial.status_code == 200
    assert 'name="analyst_ready"' in page.text
    assert "Prepared filter task" in page.text
    assert "Prepared filter task" in partial.text
    assert "Unprepared filter task" not in page.text
    assert "Unprepared filter task" not in partial.text
    assert "Analyst Ready" in page.text
    assert "Analyst Ready" in partial.text


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


async def test_web_approve_dor_failed_form_redirects_with_flag(client: AsyncClient):
    create = await client.post(
        "/api/tasks",
        json={"title": "Unready draft", "source": "agent", "agent": "bot"},
    )
    task_id = create.json()["id"]

    # Plain approve (no force) on a draft that fails DoR must not surface a
    # raw 422 JSON error. The form flow redirects back with a flag instead.
    resp = await client.post(
        f"/tasks/{task_id}/web-approve",
        data={"comment": ""},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "approve_error=dor_failed" in resp.headers["location"]

    page = await client.get(f"/tasks/{task_id}?approve_error=dor_failed")
    assert "не готова к одобрению" in page.text
    assert "Не хватает" in page.text
    assert "Force Approve" not in page.text


async def test_web_approve_dor_failed_htmx_returns_force_fragment(
    client: AsyncClient,
):
    create = await client.post(
        "/api/tasks",
        json={"title": "Unready htmx draft", "source": "agent", "agent": "bot"},
    )
    task_id = create.json()["id"]

    resp = await client.post(
        f"/tasks/{task_id}/web-approve",
        data={"comment": ""},
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert resp.status_code == 200
    assert "DoR не пройден" in resp.text
    # Shows what's missing to move further, not an override affordance.
    assert "acceptance_criteria" in resp.text or "user_story" in resp.text
    assert "override" not in resp.text.lower()
    assert "force" not in resp.text.lower()


async def test_web_start_dispatches_without_manual_plan(client: AsyncClient):
    create = await client.post("/api/tasks", json={"title": "Start from UI"})
    task_id = create.json()["id"]

    resp = await client.post(
        f"/tasks/{task_id}/web-start",
        data={"runtime": "auto"},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    detail = await client.get(f"/api/tasks/{task_id}")
    task = detail.json()
    assert task["status"] == "running"
    assert task["job_id"] == "test-job-1"
    assert task["assigned_agent"] == "developer-agent"


async def test_web_decide_task_with_summary(client: AsyncClient, db):
    create = await client.post("/api/tasks", json={"title": "Decision task"})
    task_id = create.json()["id"]
    await repo.update_task(db, task_id, status="needs_decision")
    await db.commit()

    resp = await client.post(
        f"/tasks/{task_id}/web-decide",
        data={
            "action": "accept",
            "decision_summary": "Cosmetic issues only.",
            "record_decision": "false",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    detail = await client.get(f"/api/tasks/{task_id}")
    data = detail.json()
    assert data["status"] == "completed"
    decision_updates = [u for u in data["updates"] if u["kind"] == "decision"]
    assert len(decision_updates) == 1
    assert "Cosmetic issues only" in decision_updates[0]["content"]


async def test_web_decide_task_form_has_summary_field(client: AsyncClient, db):
    create = await client.post("/api/tasks", json={"title": "Decision form"})
    task_id = create.json()["id"]
    await repo.update_task(db, task_id, status="needs_decision")
    await db.commit()

    page = await client.get(f"/tasks/{task_id}")
    assert page.status_code == 200
    assert 'name="decision_summary"' in page.text
    assert 'name="record_decision"' in page.text
    assert "task-action-state--danger" in page.text


async def test_task_detail_renders_archive_delete_controls(client: AsyncClient):
    create = await client.post("/api/tasks", json={"title": "Danger controls"})
    task_id = create.json()["id"]
    resp = await client.get(f"/tasks/{task_id}")
    assert resp.status_code == 200
    assert "task-danger-card" in resp.text
    assert f'action="/tasks/{task_id}/web-archive"' in resp.text
    assert f'action="/tasks/{task_id}/web-delete"' in resp.text


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
    assert done_updates[0]["content"].startswith("reviewed manually, accepting risk")
    assert "from_status=pending_report" in done_updates[0]["content"]
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
    assert done_updates[0]["content"].startswith("form-based override")
    assert "from_status=pending_report" in done_updates[0]["content"]


async def test_htmx_done_fragment_escapes_xss_title(client: AsyncClient, db):
    """Stored XSS in task title must be escaped in the HTMX done fragment."""
    xss_title = "<img src=x onerror=alert(1)>"
    create = await client.post("/api/tasks", json={"title": xss_title})
    task_id = create.json()["id"]
    await repo.update_task(db, task_id, status="pending_report")
    await db.commit()

    resp = await client.post(
        f"/tasks/{task_id}/web-force-complete",
        headers={"HX-Request": "true", "HX-Prompt": "ok"},
    )
    assert resp.status_code == 200
    assert "<img" not in resp.text
    assert "&lt;img" in resp.text


# ---- Web review verdict panel (#321) ----


async def _web_task_in_review(client: AsyncClient) -> int:
    resp = await client.post("/api/tasks", json={"title": "Web review task"})
    task_id = resp.json()["id"]
    await client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": "dev", "kind": "status", "content": "Plan: work"},
    )
    resp = await client.post(
        f"/api/tasks/{task_id}/pair-start", json={"assigned_agent": "dev"}
    )
    assert resp.status_code == 200, resp.text
    resp = await client.post(f"/api/tasks/{task_id}/submit-review", json={})
    assert resp.json()["status"] == "review"
    return task_id


async def test_review_panel_visible_for_client_driven_review(client: AsyncClient):
    task_id = await _web_task_in_review(client)
    resp = await client.get(f"/tasks/{task_id}")
    assert resp.status_code == 200
    assert "Review Required" in resp.text
    assert f"/tasks/{task_id}/web-review-verdict" in resp.text
    assert 'name="verdict" value="approved"' in resp.text
    assert 'name="verdict" value="changes_requested"' in resp.text


async def test_review_panel_hidden_for_headless_review(client: AsyncClient, db):
    from hub import repository as repo_module

    task_id = await _web_task_in_review(client)
    await repo_module.update_task(db, task_id, review_job_id="rev-42")
    await db.commit()

    resp = await client.get(f"/tasks/{task_id}")
    assert resp.status_code == 200
    assert "Review Required" not in resp.text
    assert "In Progress" in resp.text


async def test_web_approve_verdict_returns_task_to_running(client: AsyncClient):
    task_id = await _web_task_in_review(client)
    resp = await client.post(
        f"/tasks/{task_id}/web-review-verdict",
        data={"verdict": "approved", "comments": "LGTM from the dashboard"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    body = (await client.get(f"/api/tasks/{task_id}")).json()
    assert body["status"] == "running"
    assert body["review_approved_current"] is True
    assert body["latest_review"]["verdict"] == "approved"


async def test_web_changes_requested_with_findings(client: AsyncClient):
    task_id = await _web_task_in_review(client)
    resp = await client.post(
        f"/tasks/{task_id}/web-review-verdict",
        data={
            "verdict": "changes_requested",
            "comments": "see findings",
            "findings_text": "high: race in bump\njust a plain note\nlow: typo",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    body = (await client.get(f"/api/tasks/{task_id}")).json()
    assert body["status"] == "running"
    assert body["review_cycle"] == 1
    findings = body["latest_review"]["findings"]
    assert [f["id"] for f in findings] == [1, 2, 3]
    assert findings[0]["severity"] == "high"
    assert findings[0]["message"] == "race in bump"
    assert findings[1]["severity"] == "medium"  # default for plain lines
    assert findings[2]["severity"] == "low"


async def test_web_verdict_invalid_form_shows_error_not_500(client: AsyncClient):
    task_id = await _web_task_in_review(client)
    resp = await client.post(
        f"/tasks/{task_id}/web-review-verdict",
        data={"verdict": "maybe", "comments": ""},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "review_error=" in resp.headers["location"]

    # Task untouched.
    body = (await client.get(f"/api/tasks/{task_id}")).json()
    assert body["status"] == "review"
    assert body["review_verdict"] is None


# ---- Draft queue ranking (#253) ----


async def _draft_with_readiness(client: AsyncClient, title: str, *, ready: bool) -> int:
    resp = await client.post(
        "/api/tasks", json={"title": title, "source": "agent", "agent": "bot"}
    )
    task_id = resp.json()["id"]
    if ready:
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


async def test_inbox_ranks_ready_drafts_first_with_badges(client: AsyncClient):
    # AC-1 (#253): ready drafts sort above unready ones and carry
    # score/risk/age markers.
    bare_id = await _draft_with_readiness(client, "Bare zzz", ready=False)
    ready_id = await _draft_with_readiness(client, "Ready aaa", ready=True)

    resp = await client.get("/partials/inbox")
    assert resp.status_code == 200
    html = resp.text
    # Ready draft is rendered before the bare one despite a higher id.
    assert html.index(f"inbox-task-{ready_id}") < html.index(f"inbox-task-{bare_id}")
    assert "ready · score" in html
    assert "not refined" in html
    assert "Approve ready (1)" in html


# ---- Project selector and filtering in Web UI (#339) ----


async def _web_two_projects(client: AsyncClient, db) -> tuple[int, int]:
    from hub import repository as repo_module
    from hub.db import seed_default_project

    await seed_default_project(db)
    pid_a = await repo_module.create_project(db, slug="ui-a", name="UI A")
    resp = await client.post(
        "/api/tasks", json={"title": "Epic UI-A", "task_type": "epic"}
    )
    epic_a = resp.json()["id"]
    await repo_module.update_task(db, epic_a, project_id=pid_a)
    resp = await client.post(
        "/api/tasks", json={"title": "Epic UI-B", "task_type": "epic"}
    )
    epic_b = resp.json()["id"]
    await db.commit()
    return epic_a, epic_b


async def test_tasks_page_filters_by_project(client: AsyncClient, db):
    # AC-1 (#339)
    epic_a, epic_b = await _web_two_projects(client, db)

    resp = await client.get("/tasks?project=ui-a&type=epic")
    assert resp.status_code == 200
    assert "Epic UI-A" in resp.text
    assert "Epic UI-B" not in resp.text
    assert 'id="project-select"' in resp.text
    assert "selected" in resp.text

    resp = await client.get("/tasks?type=epic")
    assert "Epic UI-A" in resp.text and "Epic UI-B" in resp.text


async def test_dashboard_and_inbox_filter_by_project(client: AsyncClient, db):
    epic_a, epic_b = await _web_two_projects(client, db)

    resp = await client.get("/?project=ui-a")
    assert resp.status_code == 200
    assert "Epic UI-A" in resp.text
    # Task cards/lists link to /tasks/{id}; the global activity feed keeps
    # plain-text mentions and is intentionally not project-scoped in V1.
    assert f'href="/tasks/{epic_a}"' in resp.text.replace("'", '"')
    assert f'href="/tasks/{epic_b}"' not in resp.text.replace("'", '"')

    resp = await client.get("/partials/kanban?project=ui-a")
    assert "Epic UI-A" in resp.text and "Epic UI-B" not in resp.text


async def test_projects_page_lists_and_links(client: AsyncClient, db):
    from hub.db import seed_default_project

    await seed_default_project(db)
    resp = await client.get("/projects")
    assert resp.status_code == 200
    assert "default" in resp.text
    assert "/tasks?project=default" in resp.text


async def test_task_detail_shows_project_badge(client: AsyncClient, db):
    epic_a, _ = await _web_two_projects(client, db)
    resp = await client.get(f"/tasks/{epic_a}")
    assert resp.status_code == 200
    assert "Project" in resp.text
    assert "ui-a" in resp.text


# ---------------------------------------------------------------------------
# Project forms: create / edit / archive / activate (#344)
# ---------------------------------------------------------------------------


async def test_web_create_project_form(client: AsyncClient, db):
    # AC-1: form submit creates the project; it shows up in the table.
    from hub.db import seed_default_project

    await seed_default_project(db)
    resp = await client.post(
        "/projects/web-create",
        data={
            "slug": "calc-kids",
            "name": "Calc Kids",
            "repo": "mrPDA/calc-kids",
            "workspace_path": "/srv/calc",
            "default_branch": "master",
            "default_branch_policy": '{"release_base": "main"}',
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/projects"

    page = await client.get("/projects")
    assert "calc-kids" in page.text
    assert "master" in page.text

    from hub import repository as repo_module

    row = await repo_module.get_project_by_slug(db, "calc-kids")
    assert row is not None
    assert row["status"] == "active"  # web session = human path
    assert '"release_base"' in row["default_branch_policy"]


async def test_web_create_project_duplicate_slug(client: AsyncClient, db):
    # AC-2: duplicate slug lands as a form error, not a 500.
    from hub.db import seed_default_project

    await seed_default_project(db)
    resp = await client.post(
        "/projects/web-create",
        data={"slug": "default", "name": "Dup"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "project_error=" in resp.headers["location"]

    page = await client.get(resp.headers["location"])
    assert page.status_code == 200
    assert "already exists" in page.text


async def test_web_create_project_bad_policy_json(client: AsyncClient, db):
    # AC-2: malformed policy JSON is a form error, not a 500.
    # #351 AC-3: the project must NOT be created and the error must name policy.
    from hub import repository as repo_module

    resp = await client.post(
        "/projects/web-create",
        data={"slug": "poly", "name": "Poly", "default_branch_policy": "{oops"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "project_error=" in resp.headers["location"]
    assert await repo_module.get_project_by_slug(db, "poly") is None

    page = await client.get(resp.headers["location"])
    assert "policy" in page.text


async def test_web_create_project_bad_slug(client: AsyncClient, db):
    # #351 AC-3: no project row appears and the error names the slug field.
    from hub import repository as repo_module

    resp = await client.post(
        "/projects/web-create",
        data={"slug": "Bad Slug!", "name": "X"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "project_error=" in resp.headers["location"]
    rows = await repo_module.list_projects(db, include_archived=True)
    assert all(r["name"] != "X" for r in rows)

    page = await client.get(resp.headers["location"])
    assert "slug" in page.text


async def test_web_create_project_deeply_nested_policy_json(client: AsyncClient, db):
    # #350 AC-1: '['*20000 makes json.loads raise RecursionError on py<3.13 —
    # must land as a form error redirect, never a 500.
    from hub import repository as repo_module

    resp = await client.post(
        "/projects/web-create",
        data={"slug": "deep", "name": "Deep", "default_branch_policy": "[" * 20000},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "project_error=" in resp.headers["location"]
    assert await repo_module.get_project_by_slug(db, "deep") is None


async def test_web_edit_project_deeply_nested_policy_json(client: AsyncClient, db):
    from hub import repository as repo_module

    pid = await repo_module.create_project(db, slug="deep-ed", name="DeepEd")
    await db.commit()

    resp = await client.post(
        f"/projects/{pid}/web-edit",
        data={"name": "Changed", "default_branch_policy": "[" * 20000},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "project_error=" in resp.headers["location"]
    row = await repo_module.get_project(db, pid)
    assert row["name"] == "DeepEd"  # nothing applied


async def test_web_create_project_concurrent_duplicate_slug(client: AsyncClient, db):
    # #350 AC-2: double-submit race — both requests pass the slug check before
    # the first INSERT unless check+insert is serialized; the loser must get a
    # 409-driven form error, not an IntegrityError 500.
    from hub import repository as repo_module

    def post():
        return client.post(
            "/projects/web-create",
            data={"slug": "race", "name": "Race"},
            follow_redirects=False,
        )

    r1, r2 = await asyncio.gather(post(), post())
    assert {r1.status_code, r2.status_code} == {303}
    locations = sorted([r1.headers["location"], r2.headers["location"]])
    assert locations[0] == "/projects"
    assert "project_error=" in locations[1]

    rows = await repo_module.list_projects(db, include_archived=True)
    assert sum(1 for r in rows if r["slug"] == "race") == 1


async def test_web_edit_project(client: AsyncClient, db):
    from hub import repository as repo_module
    from hub.db import seed_default_project

    await seed_default_project(db)
    pid = await repo_module.create_project(
        db, slug="edit-me", name="Before", repo_name=""
    )
    await db.commit()

    resp = await client.post(
        f"/projects/{pid}/web-edit",
        data={
            "name": "After",
            "repo": "mrPDA/after",
            "workspace_path": "",
            "default_branch": "trunk",
            "default_branch_policy": "",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    row = await repo_module.get_project(db, pid)
    assert row["name"] == "After"
    assert row["repo"] == "mrPDA/after"
    assert row["default_branch"] == "trunk"


async def test_web_archive_and_unarchive_project(client: AsyncClient, db):
    from hub import repository as repo_module
    from hub.db import seed_default_project

    await seed_default_project(db)
    pid = await repo_module.create_project(db, slug="arch", name="Arch")
    await db.commit()

    resp = await client.post(
        f"/projects/{pid}/web-archive",
        data={"archived": "true"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    row = await repo_module.get_project(db, pid)
    assert row["archived"] == 1

    resp = await client.post(
        f"/projects/{pid}/web-archive",
        data={"archived": "false"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    row = await repo_module.get_project(db, pid)
    assert row["archived"] == 0


async def test_web_activate_pending_project(client: AsyncClient, db):
    # #345 loop closed in UI: pending proposal → Activate button → active.
    from hub import repository as repo_module
    from hub.db import seed_default_project

    await seed_default_project(db)
    pid = await repo_module.create_project(
        db, slug="pend", name="Pending", status="pending"
    )
    await db.commit()

    page = await client.get("/projects")
    assert "pending" in page.text and "web-activate" in page.text

    resp = await client.post(f"/projects/{pid}/web-activate", follow_redirects=False)
    assert resp.status_code == 303
    row = await repo_module.get_project(db, pid)
    assert row["status"] == "active"


def _web_project_tokens():
    from hub.config import TokenIdentity

    return {
        "agent-token": TokenIdentity("bot", "agent"),
        "human-token": TokenIdentity("op", "human"),
    }


async def test_web_patch_routes_reject_agent_token(
    client: AsyncClient, monkeypatch, db
):
    # #351 AC-1: the human gate on web-activate/edit/archive must hold for an
    # agent Bearer — the web path is guarded only by require_human_or_admin.
    from hub import config
    from hub import repository as repo_module

    monkeypatch.setattr(config, "HUB_TOKENS", _web_project_tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    agent = {"Authorization": "Bearer agent-token"}

    pid = await repo_module.create_project(
        db, slug="gate", name="Gate", status="pending"
    )
    await db.commit()

    for url, data in (
        (f"/projects/{pid}/web-activate", {}),
        (f"/projects/{pid}/web-edit", {"name": "Hacked"}),
        (f"/projects/{pid}/web-archive", {"archived": "true"}),
    ):
        resp = await client.post(url, data=data, headers=agent, follow_redirects=False)
        assert resp.status_code == 403, url

    row = await repo_module.get_project(db, pid)
    assert row["status"] == "pending"
    assert row["name"] == "Gate"
    assert row["archived"] == 0


async def test_web_human_only_routes_reject_agent_token(
    client: AsyncClient, monkeypatch, db
):
    """#427: agent bearer must not bypass human-only web lifecycle gates."""
    from hub import config
    from hub import repository as repo_module

    monkeypatch.setattr(config, "HUB_TOKENS", _web_project_tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    agent = {"Authorization": "Bearer agent-token"}

    draft_id = await repo_module.create_task(
        db,
        title="Draft gate",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status="draft",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    open_id = await repo_module.create_task(
        db,
        title="Open gate",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status="open",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    needs_info_id = await repo_module.create_task(
        db,
        title="Needs info gate",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status="needs_info",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    needs_decision_id = await repo_module.create_task(
        db,
        title="Needs decision gate",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status="needs_decision",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    pending_id = await repo_module.create_task(
        db,
        title="Pending report gate",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status="pending_report",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    proposal_id = await repo_module.create_task(
        db,
        title="Agent proposal",
        description="",
        runtime="auto",
        source="agent",
        assigned_agent="bot",
        rationale="",
        status="draft",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await db.commit()

    routes = [
        (f"/tasks/{draft_id}/web-approve", {}),
        (f"/tasks/{draft_id}/web-reject", {}),
        (f"/tasks/{open_id}/web-start", {}),
        (f"/tasks/{needs_info_id}/web-answer", {"answer": "ok"}),
        (
            f"/tasks/{needs_decision_id}/web-decide",
            {"action": "accept", "decision_summary": "yes"},
        ),
        (f"/tasks/{pending_id}/web-force-complete", {"comment": "override"}),
        (f"/proposals/{proposal_id}/approve", {}),
        (f"/proposals/{proposal_id}/reject", {}),
    ]
    for url, data in routes:
        resp = await client.post(url, data=data, headers=agent, follow_redirects=False)
        assert resp.status_code == 403, url

    assert (await repo_module.get_task(db, draft_id))["status"] == "draft"
    assert (await repo_module.get_task(db, open_id))["status"] == "open"
    assert (await repo_module.get_task(db, pending_id))["status"] == "pending_report"


async def test_web_force_complete_human_token_still_works(
    client: AsyncClient, monkeypatch, db
):
    from hub import config
    from hub import repository as repo_module

    monkeypatch.setattr(config, "HUB_TOKENS", _web_project_tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    human = {"Authorization": "Bearer human-token"}

    task_id = await repo_module.create_task(
        db,
        title="Human override",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="",
        rationale="",
        status="pending_report",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await db.commit()

    resp = await client.post(
        f"/tasks/{task_id}/web-force-complete",
        data={"comment": "human approved override"},
        headers=human,
        follow_redirects=False,
    )
    assert resp.status_code in (200, 303)
    row = await repo_module.get_task(db, task_id)
    assert row["status"] == "completed"


async def test_web_create_project_agent_token_creates_pending(
    client: AsyncClient, monkeypatch, db
):
    # #351 AC-2: agent→pending must survive through the web-create wrapper,
    # not only through POST /api/projects.
    from hub import config
    from hub import repository as repo_module

    monkeypatch.setattr(config, "HUB_TOKENS", _web_project_tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    monkeypatch.setattr(config, "ALLOW_AGENT_PROJECTS", "propose")
    agent = {"Authorization": "Bearer agent-token"}

    resp = await client.post(
        "/projects/web-create",
        data={"slug": "agent-made", "name": "Agent Made"},
        headers=agent,
        follow_redirects=False,
    )
    assert resp.status_code == 303
    row = await repo_module.get_project_by_slug(db, "agent-made")
    assert row is not None
    assert row["status"] == "pending"


async def test_web_edit_project_name_too_long_is_form_error(client: AsyncClient, db):
    # #351 AC-4: ProjectPatch max_length=200 — the ValidationError branch of
    # web-edit must redirect with project_error, never 500.
    from hub import repository as repo_module

    pid = await repo_module.create_project(db, slug="longname", name="Long")
    await db.commit()

    resp = await client.post(
        f"/projects/{pid}/web-edit",
        data={"name": "x" * 201},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "project_error=" in resp.headers["location"]
    row = await repo_module.get_project(db, pid)
    assert row["name"] == "Long"


async def test_web_edit_project_bad_policy_json(client: AsyncClient, db):
    # Review finding (#344): the edit route has its own _parse_policy_form
    # call — malformed JSON must be a form error there too, row untouched.
    from hub import repository as repo_module
    from hub.db import seed_default_project

    await seed_default_project(db)
    pid = await repo_module.create_project(db, slug="polyed", name="PolyEd")
    await db.commit()

    resp = await client.post(
        f"/projects/{pid}/web-edit",
        data={"name": "Hacked", "default_branch_policy": "{oops"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "project_error=" in resp.headers["location"]
    row = await repo_module.get_project(db, pid)
    assert row["name"] == "PolyEd"  # ничего не применилось


async def test_web_edit_project_policy_roundtrip(client: AsyncClient, db):
    # Review finding (#344): pin 'empty textarea = keep policy' and the
    # stored-JSON → textarea → resubmit round-trip.
    import json as json_module

    from hub import repository as repo_module
    from hub.db import seed_default_project

    await seed_default_project(db)
    await client.post(
        "/projects/web-create",
        data={
            "slug": "rt",
            "name": "RT",
            "default_branch_policy": '{"release_base": "main"}',
        },
        follow_redirects=False,
    )
    row = await repo_module.get_project_by_slug(db, "rt")
    pid = row["id"]

    page = await client.get("/projects")
    assert "release_base" in page.text  # policy prefilled in the edit textarea

    resp = await client.post(
        f"/projects/{pid}/web-edit",
        data={"default_branch_policy": '{"release_base": "trunk"}'},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    row = await repo_module.get_project(db, pid)
    assert json_module.loads(row["default_branch_policy"]) == {"release_base": "trunk"}

    resp = await client.post(
        f"/projects/{pid}/web-edit",
        data={"name": "RT2", "default_branch_policy": ""},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    row = await repo_module.get_project(db, pid)
    assert row["name"] == "RT2"
    assert json_module.loads(row["default_branch_policy"]) == {
        "release_base": "trunk"
    }  # пусто = не менять


async def test_web_edit_project_empty_required_fields_keep_values(
    client: AsyncClient, db
):
    # Review finding (#344): blank name/default_branch mean 'keep', while
    # blank repo/workspace_path intentionally clear.
    from hub import repository as repo_module
    from hub.db import seed_default_project

    await seed_default_project(db)
    pid = await repo_module.create_project(
        db,
        slug="keep",
        name="Keeper",
        repo_name="mrPDA/keep",
        workspace_path="/srv/keep",
        default_branch="trunk",
    )
    await db.commit()

    resp = await client.post(
        f"/projects/{pid}/web-edit",
        data={
            "name": "",
            "default_branch": "",
            "repo": "",
            "workspace_path": "",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    row = await repo_module.get_project(db, pid)
    assert row["name"] == "Keeper"  # пустое обязательное — не меняем
    assert row["default_branch"] == "trunk"
    assert row["repo"] == ""  # пустое необязательное — очищаем
    assert row["workspace_path"] == ""


async def test_web_provision_button_and_status(client: AsyncClient, db):
    # AC-1 (#348): Provision button for repo projects; status lands on page.
    from unittest.mock import AsyncMock

    from hub import repository as repo_module
    from hub.db import seed_default_project
    from hub.integrations.registry import plugins

    await seed_default_project(db)
    pid = await repo_module.create_project(
        db,
        slug="provui",
        name="ProvUI",
        repo_name="mrPDA/provui",
        workspace_path="/srv/provui",
    )
    await db.commit()

    page = await client.get("/projects")
    assert f"/projects/{pid}/web-provision" in page.text  # кнопка есть
    assert "ws" not in page.text or "ws ok" not in page.text

    plugins.git_ops.clone_repo = AsyncMock(return_value=(True, "cloned"))
    resp = await client.post(f"/projects/{pid}/web-provision", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/projects"
    page = await client.get("/projects")
    assert "ws&nbsp;ok" in page.text

    plugins.git_ops.clone_repo = AsyncMock(
        return_value=(False, "remote not accessible: no key")
    )
    resp = await client.post(f"/projects/{pid}/web-provision", follow_redirects=False)
    assert resp.status_code == 303
    assert "project_error=" in resp.headers["location"]
    page = await client.get(resp.headers["location"])
    assert "remote not accessible" in page.text
    page = await client.get("/projects")
    assert "ws&nbsp;error" in page.text


async def test_web_provision_hidden_without_repo(client: AsyncClient, db):
    from hub import repository as repo_module
    from hub.db import seed_default_project

    await seed_default_project(db)
    pid = await repo_module.create_project(db, slug="norepo-ui", name="NoRepo")
    await db.commit()
    page = await client.get("/projects")
    assert f"/projects/{pid}/web-provision" not in page.text


async def test_web_skills_pages(client: AsyncClient, db):
    # #380: list page, detail page, Activate button for drafts.
    from hub import repository as repo_module
    from hub.db import seed_default_skills

    await seed_default_skills(db)
    await repo_module.create_skill_version(
        db,
        name="multi-agent-review",
        content="v2 draft",
        status="draft",
        created_by="bot",
    )
    await db.commit()

    page = await client.get("/skills")
    assert page.status_code == 200
    assert "multi-agent-review" in page.text

    detail = await client.get("/skills/multi-agent-review")
    assert detail.status_code == 200
    assert "web-activate" in detail.text  # кнопка для драфта v2

    resp = await client.post(
        "/skills/multi-agent-review/versions/2/web-activate",
        follow_redirects=False,
    )
    assert resp.status_code == 303
    row = await repo_module.get_active_skill(db, "multi-agent-review")
    assert row["version"] == 2


async def test_review_panel_shows_machine_review(client: AsyncClient, db):
    # #381: summary and confirmed findings render above the verdict buttons.
    from hub import repository as repo_module
    from hub import services as services_module
    from hub.models import TaskCreate

    tv = await services_module.create_task(db, TaskCreate(title="MR panel task"))
    await repo_module.add_task_update(db, tv.id, "dev", "status", "Plan: x")
    await db.commit()
    await services_module.pair_start_task(db, tv.id, caller="dev")
    await services_module.submit_for_review(db, tv.id)

    resp = await client.post(
        f"/api/tasks/{tv.id}/machine-review",
        json={
            "raw_count": 5,
            "findings_confirmed": [
                {"title": "missing test", "severity": "low", "category": "tests"}
            ],
            "findings_rejected": [{"title": "noise", "reason": "unreachable"}],
        },
    )
    assert resp.status_code == 200

    page = await client.get(f"/tasks/{tv.id}")
    assert "Machine review" in page.text
    assert "5 raw" in page.text
    assert "1 confirmed" in page.text
    assert "missing test" in page.text


async def test_review_panel_gap_warning_and_request_button(client: AsyncClient, db):
    # #382: warning + Request machine review button; button sets override.
    from hub import repository as repo_module
    from hub import services as services_module
    from hub.models import TaskCreate

    tv = await services_module.create_task(db, TaskCreate(title="Gap task"))
    await repo_module.add_task_update(db, tv.id, "dev", "status", "Plan: x")
    await repo_module.update_task(db, tv.id, size="M", work_type="feature")
    await db.commit()
    await services_module.pair_start_task(db, tv.id, caller="dev")
    await services_module.submit_for_review(db, tv.id)

    page = await client.get(f"/tasks/{tv.id}")
    assert "machine-review отсутствует" in page.text
    assert "web-request-machine-review" in page.text

    resp = await client.post(
        f"/tasks/{tv.id}/web-request-machine-review", follow_redirects=False
    )
    assert resp.status_code == 303
    row = await repo_module.get_task(db, tv.id)
    assert row["machine_review_override"] == "require"
    events = [
        dict(e)
        for e in await repo_module.list_events(
            db, since=0, kinds=["machine_review_requested"]
        )
    ]
    assert events and events[0]["task_id"] == tv.id


async def test_web_create_skill_from_form(client: AsyncClient, db):
    # AC-1 (#385): create form → active skill (human path) in the list.
    from hub import repository as repo_module

    resp = await client.post(
        "/skills/web-create",
        data={
            "name": "dor-checklist",
            "kind": "checklist",
            "tags": "dor, quality",
            "content": "- AC present?\n- risks listed?",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/skills/dor-checklist"

    row = await repo_module.get_active_skill(db, "dor-checklist")
    assert row is not None and row["version"] == 1 and row["status"] == "active"

    page = await client.get("/skills")
    assert "dor-checklist" in page.text


async def test_web_edit_as_new_version(client: AsyncClient, db):
    # AC-2 (#385): active content prefilled; save creates N+1, history kept.
    from hub import repository as repo_module
    from hub.db import seed_default_skills

    await seed_default_skills(db)

    detail = await client.get("/skills/multi-agent-review")
    assert "refuted" in detail.text  # textarea prefilled with active v1
    assert 'name="content"' in detail.text  # Edit-as-new-version form present

    resp = await client.post(
        "/skills/multi-agent-review/web-new-version",
        data={"content": "v2 harness body", "tags": "review"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    versions = await repo_module.list_skill_versions(db, "multi-agent-review")
    assert len(versions) == 2
    assert versions[0]["version"] == 2  # newest first
    # v1 стала неактивной? нет — новая версия у человека сразу active,
    # активной считается старшая active-версия
    active = await repo_module.get_active_skill(db, "multi-agent-review")
    assert active["version"] == 2
    assert active["content"] == "v2 harness body"
    # история цела: v1 всё ещё существует
    assert any(v["version"] == 1 for v in versions)


async def test_web_create_skill_bad_slug(client: AsyncClient, db):
    # AC-3 (#385): invalid slug → form error, not a 500.
    resp = await client.post(
        "/skills/web-create",
        data={"name": "Bad Slug!", "kind": "prompt", "content": "x"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "skill_error=" in resp.headers["location"]


async def test_web_new_version_unknown_skill_404(client: AsyncClient, db):
    resp = await client.post(
        "/skills/nope/web-new-version",
        data={"content": "x"},
        follow_redirects=False,
    )
    assert resp.status_code == 404
