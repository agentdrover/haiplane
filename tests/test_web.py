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


async def test_task_detail_shows_finding_scope_and_linked_task(client: AsyncClient):
    # #435: out-of-scope findings render a scope badge and a link to the
    # follow-up task in the review panel.
    linked = await client.post("/api/tasks", json={"title": "Follow-up work"})
    linked_id = linked.json()["id"]
    task_id = await _web_task_in_review(client)
    resp = await client.post(
        f"/api/tasks/{task_id}/review-verdict",
        json={
            "verdict": "changes_requested",
            "agent": "reviewer",
            "findings": [
                {"id": 1, "severity": "high", "message": "Fix in this task"},
                {
                    "id": 2,
                    "severity": "low",
                    "message": "Handled separately",
                    "scope": "out_of_scope",
                    "linked_task_id": linked_id,
                },
            ],
        },
    )
    assert resp.status_code == 200

    # Resubmit so the task is back in client-driven review and the panel
    # (including the stale latest verdict with findings) is rendered.
    await client.post(f"/api/tasks/{task_id}/submit-review", json={})
    page = await client.get(f"/tasks/{task_id}")
    assert page.status_code == 200
    assert "Fix in this task" in page.text
    assert "Handled separately" in page.text
    assert "out of scope" in page.text
    assert f'href="/tasks/{linked_id}"' in page.text
    assert f"#{linked_id}" in page.text


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


async def test_web_solo_verdict_marked_and_badge_rendered(
    client: AsyncClient, monkeypatch
):
    """#434: a verdict accepted via OPENCLAW_REVIEW_SELF_APPROVE=allow is
    persisted as self-approved and badged next to the verdict in the panel."""
    from hub import config

    monkeypatch.setattr(config, "HUB_TOKENS", _web_project_tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    monkeypatch.setattr(config, "REVIEW_SELF_APPROVE", "allow")
    agent = {"Authorization": "Bearer agent-token"}

    # Created by the human (#360): an agent token cannot make ready work.
    resp = await client.post(
        "/api/tasks",
        json={"title": "Solo web"},
        headers={"Authorization": "Bearer human-token"},
    )
    task_id = resp.json()["id"]
    await client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": "bot", "kind": "status", "content": "Plan: work"},
        headers=agent,
    )
    resp = await client.post(
        f"/api/tasks/{task_id}/pair-start",
        json={"assigned_agent": "bot"},
        headers=agent,
    )
    assert resp.status_code == 200, resp.text
    resp = await client.post(
        f"/api/tasks/{task_id}/submit-review", json={}, headers=agent
    )
    assert resp.json()["status"] == "review"

    # The implementer reviews their own work through the web panel.
    resp = await client.post(
        f"/tasks/{task_id}/web-review-verdict",
        data={"verdict": "changes_requested", "comments": "self check"},
        headers=agent,
        follow_redirects=False,
    )
    assert resp.status_code == 303

    body = (await client.get(f"/api/tasks/{task_id}", headers=agent)).json()
    assert body["latest_review"]["self_approved"] is True

    # Back in review, the panel badges the solo verdict.
    resp = await client.post(
        f"/api/tasks/{task_id}/submit-review", json={}, headers=agent
    )
    assert resp.json()["status"] == "review"
    page = await client.get(f"/tasks/{task_id}", headers=agent)
    assert page.status_code == 200
    assert "badge-self-approved" in page.text


async def test_web_review_verdict_rejects_self_review(client: AsyncClient, monkeypatch):
    """#358 T6: the implementer cannot pass a verdict on their own work through
    the web panel.

    The protection already lives in the shared service, but until now it was
    asserted only through REST and MCP — self_review_forbidden appeared nowhere
    in this file. The two neighbours below cover the other branches: the solo
    opt-out (REVIEW_SELF_APPROVE=allow) and an independent reviewer.

    Verified by mutation: drop the raise from ensure_reviewer_independence and
    keep its self_approved return, and only this test fails — both neighbours
    stay green. So the refusal on the web route was genuinely unguarded, not
    merely covered somewhere less obvious.
    """
    from hub import config

    monkeypatch.setattr(config, "HUB_TOKENS", _web_project_tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    # The default, spelled out: this test is about the forbidding branch, and
    # a neighbour flipping the global would silently turn it into a no-op.
    monkeypatch.setattr(config, "REVIEW_SELF_APPROVE", "forbid")
    agent = {"Authorization": "Bearer agent-token"}

    # Created by the human (#360): an agent token cannot make ready work.
    resp = await client.post(
        "/api/tasks",
        json={"title": "Self review web"},
        headers={"Authorization": "Bearer human-token"},
    )
    task_id = resp.json()["id"]
    await client.post(
        f"/api/tasks/{task_id}/updates",
        json={"agent": "bot", "kind": "status", "content": "Plan: work"},
        headers=agent,
    )
    resp = await client.post(
        f"/api/tasks/{task_id}/pair-start",
        json={"assigned_agent": "bot"},
        headers=agent,
    )
    assert resp.status_code == 200, resp.text
    resp = await client.post(
        f"/api/tasks/{task_id}/submit-review", json={}, headers=agent
    )
    assert resp.json()["status"] == "review"

    # The implementer approves their own work through the web panel.
    resp = await client.post(
        f"/tasks/{task_id}/web-review-verdict",
        data={"verdict": "approved", "comments": "looks good to me"},
        headers=agent,
        follow_redirects=False,
    )

    assert resp.status_code == 403
    assert resp.json()["detail"]["reason"] == "self_review_forbidden"

    # No verdict recorded, and the task is still waiting for a real reviewer.
    body = (await client.get(f"/api/tasks/{task_id}", headers=agent)).json()
    assert body["latest_review"] is None
    assert body["review_approved_current"] is False
    assert body["status"] == "review"


async def test_web_independent_verdict_has_no_solo_badge(client: AsyncClient):
    """#434: without the opt-out nothing changes — no badge, no mark."""
    task_id = await _web_task_in_review(client)
    resp = await client.post(
        f"/tasks/{task_id}/web-review-verdict",
        data={"verdict": "changes_requested", "comments": "rework"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    body = (await client.get(f"/api/tasks/{task_id}")).json()
    assert body["latest_review"]["self_approved"] is False

    resp = await client.post(f"/api/tasks/{task_id}/submit-review", json={})
    assert resp.json()["status"] == "review"
    page = await client.get(f"/tasks/{task_id}")
    assert "badge-self-approved" not in page.text


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


async def test_web_create_form_rejects_agent_token(
    client: AsyncClient, monkeypatch, db
):
    """#360: the web create form is the third door onto ready work.

    The REST endpoints were gated first; this form was not, and it builds
    TaskCreate with the default source=human while honouring run_immediately —
    so an agent token landed a task in `running` with a dispatched job. Found
    while assembling context for a machine review of the REST-only fix.
    """
    from hub import config

    monkeypatch.setattr(config, "HUB_TOKENS", _web_project_tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    agent = {"Authorization": "Bearer agent-token"}

    resp = await client.post(
        "/tasks/create",
        data={"title": "Agent via web form", "run_immediately": "true"},
        headers=agent,
        follow_redirects=False,
    )

    assert resp.status_code == 403
    # Round-2 machine-review finding: the refusal must point at the alternative
    # the agent can actually take. human_only_gate says "retry with a human
    # token" and offers no tool — a dead end for the only actor that can hit
    # this. The REST endpoints answer agent_create_forbidden with
    # hub_propose_task for the identical violation, so this route must too.
    detail = resp.json()["detail"]
    assert detail["reason"] == "agent_create_forbidden"
    assert detail["suggested_tool"] == "hub_propose_task"
    assert "hub_propose_task" in detail["next_action"]
    assert detail["actor_hint"] == "human"

    listing = await client.get("/api/tasks", headers=agent)
    assert all(t["title"] != "Agent via web form" for t in listing.json())


async def test_web_create_form_human_token_still_works(
    client: AsyncClient, monkeypatch
):
    # The gate must not close the form for the people it exists for.
    from hub import config

    monkeypatch.setattr(config, "HUB_TOKENS", _web_project_tokens())
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    human = {"Authorization": "Bearer human-token"}

    resp = await client.post(
        "/tasks/create",
        data={"title": "Human via web form"},
        headers=human,
        follow_redirects=False,
    )

    assert resp.status_code == 303
    listing = await client.get("/api/tasks", headers=human)
    made = [t for t in listing.json() if t["title"] == "Human via web form"]
    assert len(made) == 1
    assert made[0]["status"] == "open"


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
            "incomplete": False,
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


# ---- Roles for machine identities are assignable from the admin UI (#613) ----
#
# The role model was already complete — set_principal_roles takes a LIST of
# slugs, guards the last admin, and writes audit — but the UI reached none of
# it: agent creation hardcoded the `agent` role and role editing lived only on
# the users page. #546 made that concrete: its ci_runner role exists on
# production carrying exactly tasks.read + tasks.ci_report, and an admin
# looking at the admin UI had no way to grant it. A narrow role nobody can
# grant is a narrow role nobody uses.


def _admin_headers(monkeypatch) -> dict:
    """An admin identity for the web admin pages, which require one."""
    from hub import config

    monkeypatch.setattr(
        config,
        "HUB_TOKENS",
        config.parse_tokens("root:admin-token:admin,denis:human-token:human"),
    )
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    return {"Authorization": "Bearer admin-token"}


async def _roles_of(db, username: str) -> list[str]:
    from hub.services import admin as admin_svc

    for p in await admin_svc.list_principals(db):
        if p["username"] == username:
            return sorted(p["roles"])
    raise AssertionError(f"principal {username!r} not found")


async def test_agent_is_created_with_the_chosen_role(
    client: AsyncClient, db, monkeypatch
):
    # AC-1 (#613): the role chosen in the form is the role the identity gets.
    # Before this, every identity created through the UI was an `agent` — the
    # one role that deliberately does NOT carry tasks.ci_report (#546).
    admin = _admin_headers(monkeypatch)

    resp = await client.post(
        "/admin/agents/create",
        data={
            "username": "ci-runner",
            "display_name": "GitHub Actions",
            "role": "ci_runner",
        },
        headers=admin,
    )
    assert resp.status_code == 200, resp.text
    assert await _roles_of(db, "ci-runner") == ["ci_runner"], (
        "the identity must carry exactly the chosen role, not the agent default"
    )

    # The CREATE picker must be built from the roles in the database. Assert
    # inside the <select> itself: the row-level checkbox list mentions every role
    # too, so a page-wide search passes even when the picker is hardcoded — the
    # first version of this assertion did exactly that and let the mutation live.
    page = await client.get("/admin/agents", headers=admin)
    picker = page.text.split('id="ca-role"', 1)[1].split("</select>", 1)[0]
    assert 'value="ci_runner"' in picker, "the create form must offer DB roles"
    assert 'value="viewer"' in picker


async def test_creating_an_agent_without_a_role_still_gets_agent(
    client: AsyncClient, db, monkeypatch
):
    # AC-5 (#613): the default is unchanged. Adding a choice must not silently
    # change what happens when nobody chooses.
    admin = _admin_headers(monkeypatch)

    resp = await client.post(
        "/admin/agents/create",
        data={"username": "plain-agent"},
        headers=admin,
    )
    assert resp.status_code == 200, resp.text
    assert await _roles_of(db, "plain-agent") == ["agent"]


async def test_agent_roles_can_be_changed_from_the_agents_page(
    client: AsyncClient, db, monkeypatch
):
    # AC-2 (#613): roles are editable where the identity is visible, the change
    # is recorded in audit, and the response re-renders the AGENTS table. That
    # last part is not cosmetic: htmx swaps by hx-select, so a handler returning
    # the users page would silently replace the agents table with human users.
    from hub.services import admin as admin_svc

    admin = _admin_headers(monkeypatch)
    created = await admin_svc.create_principal(
        db, kind="agent", username="promoted-bot", role_slug="agent"
    )

    resp = await client.post(
        f"/admin/agents/{created['id']}/edit-roles",
        data={"roles": ["ci_runner", "viewer"]},
        headers=admin,
    )
    assert resp.status_code == 200, resp.text
    assert await _roles_of(db, "promoted-bot") == ["ci_runner", "viewer"]

    assert 'id="agents-table"' in resp.text, "the agents table must come back"
    assert 'id="users-table"' not in resp.text, (
        "returning the users page would let htmx swap the wrong table in"
    )

    rows = await db.execute_fetchall(
        "SELECT action, target_id FROM admin_audit_log WHERE action='set_roles'"
    )
    assert [dict(r)["target_id"] for r in rows] == [str(created["id"])], (
        "a role change is an audited event on every path, not just the users one"
    )


async def test_service_principals_are_visible_in_the_admin_ui(
    client: AsyncClient, db, monkeypatch
):
    # AC-3 (#613): a `service` identity — which is what #546's CI reporter is —
    # used to be invisible everywhere except the keys page, because the agents
    # page filtered kind='agent'. An identity you can create but never see
    # again cannot be administered at all.
    from hub.services import admin as admin_svc

    admin = _admin_headers(monkeypatch)
    svc = await admin_svc.create_principal(
        db, kind="service", username="ci-service", role_slug="ci_runner"
    )

    page = await client.get("/admin/agents", headers=admin)
    assert page.status_code == 200, page.text
    assert "ci-service" in page.text, "a service identity must be listed"

    # And it must be administrable, not merely listed.
    resp = await client.post(
        f"/admin/agents/{svc['id']}/edit-roles",
        data={"roles": ["viewer"]},
        headers=admin,
    )
    assert resp.status_code == 200, resp.text
    assert await _roles_of(db, "ci-service") == ["viewer"]


async def test_the_last_admin_cannot_lose_the_admin_role(
    client: AsyncClient, db, monkeypatch
):
    # AC-4 (#613): the guard that already exists must hold on the NEW path too.
    # A second way into set_principal_roles is a second way to lock everyone out.
    from hub.services import admin as admin_svc

    admin = _admin_headers(monkeypatch)
    only_admin = await admin_svc.create_principal(
        db, kind="human", username="sole-admin", role_slug="admin"
    )

    resp = await client.post(
        f"/admin/agents/{only_admin['id']}/edit-roles",
        data={"roles": ["viewer"]},
        headers=admin,
    )
    assert resp.status_code == 200, resp.text
    assert await _roles_of(db, "sole-admin") == ["admin"], (
        "the last admin keeps the role no matter which page asked"
    )
    assert "last active admin" in resp.text.lower() or "admin" in resp.text.lower()


async def test_role_editing_requires_an_admin_identity(
    client: AsyncClient, db, monkeypatch
):
    # The new routes are a path to privilege escalation if they sit outside the
    # admin gate. Every other /admin handler is behind _require_admin_web;
    # these must be too.
    from hub.services import admin as admin_svc

    _admin_headers(monkeypatch)
    target = await admin_svc.create_principal(
        db, kind="agent", username="untouchable", role_slug="agent"
    )
    human = {"Authorization": "Bearer human-token"}

    create = await client.post(
        "/admin/agents/create",
        data={"username": "sneaky", "role": "super_admin"},
        headers=human,
    )
    assert create.status_code == 403, create.text

    edit = await client.post(
        f"/admin/agents/{target['id']}/edit-roles",
        data={"roles": ["super_admin"]},
        headers=human,
    )
    assert edit.status_code == 403, edit.text
    assert await _roles_of(db, "untouchable") == ["agent"]


async def test_the_roles_page_marks_permissions_that_gate_nothing(
    client: AsyncClient, monkeypatch
):
    # AC-4 (#614): an admin reading the roles page must be able to tell a real
    # limit from a decorative one. Reading narrowness off this list is exactly
    # how a false claim about the CI token reached the owner in #613.
    from hub.db import DECLARED_ONLY_PERMISSIONS, ENFORCED_PERMISSIONS

    admin = _admin_headers(monkeypatch)
    page = await client.get("/admin/roles", headers=admin)
    assert page.status_code == 200, page.text

    # super_admin carries every permission, so both kinds are on the page.
    for decorative in ("tasks.create", "tasks.update"):
        assert decorative in DECLARED_ONLY_PERMISSIONS  # guards the premise
        assert f"{decorative} ⚠" in page.text, (
            f"{decorative} gates nothing and must be marked as such"
        )
    for real in ("tasks.delete", "tasks.ci_report"):
        assert real in ENFORCED_PERMISSIONS
        assert f"{real} ⚠" not in page.text, (
            f"{real} does gate something — marking it would be a new lie"
        )
    assert "не проверяются кодом" in page.text, "explain the mark, not just show it"


# ---------------------------------------------------------------------------
# Tasks outside every epic are named, not just findable (#571)
# ---------------------------------------------------------------------------


async def _orphan(client: AsyncClient, title: str, status: str | None = None) -> int:
    """A task with no parent — which is what POST /api/tasks makes by default."""
    task_id = (await client.post("/api/tasks", json={"title": title})).json()["id"]
    if status:
        await client.post(f"/api/tasks/{task_id}/status", json={"status": status})
    return task_id


async def test_tasks_outside_any_epic_are_shown_as_their_own_group(client: AsyncClient):
    # AC-1 (#571): the epic-shaped views are built on list_live_epics, so a task
    # with no parent appears in none of them. It IS reachable in the flat list —
    # the original statement claiming "invisible everywhere" was wrong — but
    # nothing said HOW MANY there were. On production that was 51 rows.
    await _orphan(client, "Orphan one")
    await _orphan(client, "Orphan two")

    for path in ("/partials/epics", "/projects"):
        page = await client.get(path)
        assert page.status_code == 200, page.text
        assert "Без эпика" in page.text, f"{path} must name the group"
        assert "no_epic=1" in page.text, f"{path} must link to the filtered list"

    # And the number must be the count, not a placeholder.
    assert "2 живых" in (await client.get("/partials/epics")).text


async def test_orphan_group_counts_live_tasks_only(client: AsyncClient, db):
    # AC-2 (#571): archived and terminal rows are not live work. `failed` is
    # terminal in FINAL_STATUSES, and #569 shipped a criterion that forgot it —
    # spec review caught that one, so it is asserted here rather than trusted.
    live = await _orphan(client, "Still open")
    done = await _orphan(client, "Finished")
    failed = await _orphan(client, "Broken")
    archived = await _orphan(client, "Put away")
    await db.execute("UPDATE tasks SET status='completed' WHERE id=?", (done,))
    await db.execute("UPDATE tasks SET status='failed' WHERE id=?", (failed,))
    await db.execute("UPDATE tasks SET archived=1 WHERE id=?", (archived,))
    await db.commit()

    from hub import repository as repo

    assert await repo.count_live_orphan_tasks(db) == 1, (
        "only the open task counts: completed, failed and archived are not live"
    )
    assert "1 живых" in (await client.get("/partials/epics")).text

    # An epic with no parent is not an orphan TASK — it is the top of a tree.
    epic = (
        await client.post("/api/tasks", json={"title": "An epic", "task_type": "epic"})
    ).json()["id"]
    assert epic
    assert await repo.count_live_orphan_tasks(db) == 1, "epics are not orphans"
    assert live


async def test_orphan_filter_is_a_separate_parameter(client: AsyncClient, db):
    # AC-3 (#571): the link needed a filter that did not exist. Overloading
    # parent_id was the tempting route and it would have broken
    # test_tasks_list_filters_ignore_blank_parent_id, which pins that a blank
    # parent_id means "no filter" — and with it every link that passes an empty
    # box. So the mode arrives as its own flag.
    epic = (
        await client.post(
            "/api/tasks", json={"title": "Parent epic", "task_type": "epic"}
        )
    ).json()["id"]
    child = (
        await client.post(
            "/api/tasks",
            json={"title": "Child of epic", "task_type": "feature", "parent_id": epic},
        )
    ).json()["id"]
    orphan = await _orphan(client, "No parent at all")

    filtered = await client.get("/tasks/list", params={"no_epic": "1"})
    assert filtered.status_code == 200, filtered.text
    assert f"/tasks/{orphan}" in filtered.text
    assert f"/tasks/{child}" not in filtered.text, (
        "a task under an epic is not an orphan"
    )
    assert f"/tasks/{epic}" not in filtered.text, "the epic itself is not an orphan"

    # The old contract is untouched: blank parent_id still disables filtering,
    # so both rows come back.
    unfiltered = await client.get("/tasks/list", params={"parent_id": ""})
    assert f"/tasks/{orphan}" in unfiltered.text
    assert f"/tasks/{child}" in unfiltered.text

    # And the full page accepts the same flag, not just the HTMX fragment —
    # the link in the group points at /tasks, so a flag wired into only one of
    # the two routes would give a link that quietly ignores it.
    page = await client.get("/tasks", params={"no_epic": "1"})
    assert page.status_code == 200
    assert f"/tasks/{child}" not in page.text


# ---------------------------------------------------------------------------
# Epics grouped by project, ordered by real activity, finished ones collapsed (#570)
# ---------------------------------------------------------------------------


async def _epic(client: AsyncClient, title: str, project: str | None = None) -> int:
    body: dict = {"title": title, "task_type": "epic"}
    if project:
        body["project"] = project
    resp = await client.post("/api/tasks", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


async def test_epic_list_groups_by_project_and_keeps_unassigned(
    client: AsyncClient, db
):
    # AC-1 (#570): grouping only became worth building today — until the epics
    # were split across products, 30 of 31 sat in "default" and a group-by would
    # have divided by a constant. An epic with project_id NULL must land in a
    # NAMED group: the column has no NOT NULL constraint, so "it cannot happen"
    # is not an argument.
    from hub.db import seed_default_project
    from hub.services.dashboard import UNASSIGNED_PROJECT, get_epic_board

    await seed_default_project(db)
    await db.execute(
        "INSERT INTO projects (slug, name, status) VALUES ('proj-a', 'A', 'active')"
    )
    await db.commit()
    grouped = await _epic(client, "Epic in A", project="proj-a")
    homeless = await _epic(client, "Epic with no project")
    await db.execute("UPDATE tasks SET project_id=NULL WHERE id=?", (homeless,))
    await db.commit()

    board = await get_epic_board(db)
    by_project = {g["project"]: [e.id for e in g["epics"]] for g in board["groups"]}

    assert grouped in by_project.get("proj-a", []), (
        "the epic must sit under its project"
    )
    assert homeless in by_project.get(UNASSIGNED_PROJECT, []), (
        "an epic without a project must be named, not dropped from the view"
    )
    assert board["live_total"] == sum(len(v) for v in by_project.values()), (
        "every live epic belongs to exactly one group"
    )


async def test_epics_are_ordered_by_last_subtree_activity(client: AsyncClient, db):
    # AC-2 (#570): before this the order was position ASC, id DESC — and since
    # position is 0 on every epic, effectively "newest id first", which put a
    # June-dead epic above yesterday's work.
    from hub.services.dashboard import get_epic_board

    older = await _epic(client, "Older id, fresh work")
    newer = await _epic(client, "Newer id, stale work")
    for task_id, when in (
        (older, "2026-08-09 12:00:00"),
        (newer, "2026-07-01 12:00:00"),
    ):
        child = (
            await client.post(
                "/api/tasks",
                json={
                    "title": f"child of {task_id}",
                    "task_type": "feature",
                    "parent_id": task_id,
                },
            )
        ).json()["id"]
        await client.post(
            f"/api/tasks/{child}/updates",
            json={"agent": "dev", "kind": "status", "content": "worked"},
        )
        await db.execute(
            "UPDATE task_updates SET created_at=? WHERE task_id=?", (when, child)
        )
        await db.execute("UPDATE tasks SET updated_at=? WHERE id=?", (when, child))
    await db.commit()

    order = [e.id for g in (await get_epic_board(db))["groups"] for e in g["epics"]]
    assert order.index(older) < order.index(newer), (
        f"fresh work must outrank a bigger id: got {order}"
    )


async def test_an_administrative_touch_is_not_activity(client: AsyncClient, db):
    # AC-4 (#570). The obvious sort key was MAX(updated_at), and it was a trap:
    # updated_at is bumped by ANY write (#616). On production, ordering by it
    # floated #182, #192, #209, #371 and #394 to the top — the five epics whose
    # project had just been reassigned twenty minutes earlier. A rename must not
    # look like work.
    from hub.services.dashboard import get_epic_board

    worked_on = await _epic(client, "Someone reported progress here")
    just_renamed = await _epic(client, "Only a field was edited here")

    child = (
        await client.post(
            "/api/tasks",
            json={"title": "c", "task_type": "feature", "parent_id": worked_on},
        )
    ).json()["id"]
    await client.post(
        f"/api/tasks/{child}/updates",
        json={"agent": "dev", "kind": "status", "content": "real work"},
    )
    await db.execute(
        "UPDATE task_updates SET created_at='2026-08-05 10:00:00' WHERE task_id=?",
        (child,),
    )
    await db.execute(
        "UPDATE tasks SET updated_at='2026-08-05 10:00:00' WHERE id IN (?,?)",
        (child, worked_on),
    )
    # The administrative touch is LATER in wall-clock time, which is exactly what
    # makes this test meaningful: only the feed distinguishes them.
    await db.execute(
        "UPDATE tasks SET updated_at='2026-08-10 18:00:00' WHERE id=?", (just_renamed,)
    )
    await db.commit()

    order = [e.id for g in (await get_epic_board(db))["groups"] for e in g["epics"]]
    assert order.index(worked_on) < order.index(just_renamed), (
        f"a touched-but-idle epic must not outrank one with real work: {order}"
    )

    # An epic with no feed entry anywhere has no activity to report, so it sorts
    # LAST — and is still in the list. The first design borrowed updated_at for
    # these rows; this assertion is what killed it, because the borrowed date is
    # precisely the administrative touch above.
    assert just_renamed in order, "no activity must not mean no place in the list"
    assert order[-1] == just_renamed


async def test_done_epics_are_collapsed_not_hidden(client: AsyncClient, db):
    # AC-3 (#570): #569 filtered finished epics out entirely, so there was no way
    # back to them. The count and the contents come from ONE query — this morning
    # #571 shipped a counter that disagreed with its own link, and the whole point
    # of a number is that it can be trusted.
    from hub.services.dashboard import get_epic_board

    live = await _epic(client, "Still working")
    done = await _epic(client, "All finished")
    await db.execute("UPDATE tasks SET status='completed' WHERE id=?", (done,))
    await db.commit()

    board = await get_epic_board(db)
    live_ids = [e.id for g in board["groups"] for e in g["epics"]]
    done_ids = [e.id for e in board["done"]]

    assert live in live_ids and done not in live_ids
    assert done in done_ids, "a finished epic must remain reachable"
    assert board["done_total"] == len(done_ids), (
        "the number must be the length of what the block contains"
    )
    # Exact complement: no epic may fall out of both lists.
    all_rows = await db.execute_fetchall(
        "SELECT id FROM tasks WHERE task_type='epic' AND archived=0"
    )
    assert {r["id"] for r in all_rows} == set(live_ids) | set(done_ids), (
        "an epic matching neither condition would vanish from the UI entirely"
    )

    page = await client.get("/partials/epics")
    assert "Доделанные эпики: 1" in page.text
    assert f'href="/tasks/{done}"' in page.text.replace("'", '"'), (
        "collapsed means present in the markup, not omitted"
    )


# ---------------------------------------------------------------------------
# /projects answers "what is happening", not "where to push" (#567)
# ---------------------------------------------------------------------------


async def test_status_sets_are_derived_not_retyped():
    # AC-5 (#567): this codebase has already unified two hand-copied status
    # lists — terminal statuses in #571 and epic liveness in #570 — and one of
    # them shipped WRONG (it forgot `failed`). So "in flight" is subtraction, and
    # a status added to the enum later must land in some set or fail here.
    from hub.models import (
        ACTIVE_STATUSES,
        AWAITING_HUMAN_STATUSES,
        FINAL_STATUSES,
        IN_FLIGHT_STATUSES,
        TaskStatus,
    )

    assert IN_FLIGHT_STATUSES == ACTIVE_STATUSES - AWAITING_HUMAN_STATUSES
    assert not (IN_FLIGHT_STATUSES & AWAITING_HUMAN_STATUSES)
    assert not (AWAITING_HUMAN_STATUSES & FINAL_STATUSES)
    unclassified = (
        set(TaskStatus) - AWAITING_HUMAN_STATUSES - IN_FLIGHT_STATUSES - FINAL_STATUSES
    )
    assert not unclassified, f"a status belonging to no set: {unclassified}"
    # draft is the one that was outside both pre-existing sets, and the one the
    # original statement forgot. Pin it so it cannot quietly leave again.
    assert TaskStatus.draft in AWAITING_HUMAN_STATUSES


async def _project(db, slug: str, name: str, status: str = "active") -> int:
    cur = await db.execute(
        "INSERT INTO projects (slug, name, status) VALUES (?,?,?)", (slug, name, status)
    )
    await db.commit()
    return cur.lastrowid


async def test_projects_page_lists_only_live_epics_per_project(client: AsyncClient, db):
    # AC-1 (#567): the card shows work, so a finished epic has no place on it —
    # it is reachable through the collapsed block delivered by #570.
    from hub.db import seed_default_project
    from hub.services.dashboard import get_project_cards

    await seed_default_project(db)
    pid = await _project(db, "proj-live", "Live Project")
    live = (
        await client.post(
            "/api/tasks",
            json={"title": "Live epic", "task_type": "epic", "project": "proj-live"},
        )
    ).json()["id"]
    done = (
        await client.post(
            "/api/tasks",
            json={"title": "Done epic", "task_type": "epic", "project": "proj-live"},
        )
    ).json()["id"]
    await db.execute("UPDATE tasks SET status='completed' WHERE id=?", (done,))
    await db.commit()

    card = next(c for c in await get_project_cards(db) if c["project"]["id"] == pid)
    ids = [e.id for e in card["live_epics"]]
    assert ids == [live], f"only live epics belong on the card, got {ids}"

    page = await client.get("/projects")
    assert page.status_code == 200
    assert "Live Project" in page.text


async def test_project_card_counts_only_what_waits_for_a_human(client: AsyncClient, db):
    # AC-2 (#567). Two corrections live in this test, both found by fact:
    # (1) draft belongs in the count — draft→open is human-only, and production
    #     holds 39 drafts against 2 in the three statuses first named;
    # (2) a review with review_job_id set is a HEADLESS review owned by the
    #     poller, not a person waiting — the exclusion
    #     list_stale_by_status(require_null_review_job=True) already makes.
    from hub.db import seed_default_project
    from hub.services.dashboard import get_project_cards

    await seed_default_project(db)
    pid = await _project(db, "proj-count", "Counting")
    epic = (
        await client.post(
            "/api/tasks",
            json={"title": "E", "task_type": "epic", "project": "proj-count"},
        )
    ).json()["id"]

    async def _child(
        title: str, status: str, *, job: str | None = None, archived: int = 0
    ) -> int:
        tid = (
            await client.post(
                "/api/tasks",
                json={"title": title, "task_type": "feature", "parent_id": epic},
            )
        ).json()["id"]
        await db.execute(
            "UPDATE tasks SET status=?, review_job_id=?, archived=? WHERE id=?",
            (status, job, archived, tid),
        )
        return tid

    await _child("a draft", "draft")
    await _child("a question", "needs_info")
    await _child("a decision", "needs_decision")
    await _child("a human review", "review")
    await _child("a headless review", "review", job="job-abc")
    await _child("archived draft", "draft", archived=1)
    await _child("running", "running")
    await _child("in ci", "ci_check")
    await db.commit()

    card = next(c for c in await get_project_cards(db) if c["project"]["id"] == pid)
    assert card["awaiting_human"] == 4, (
        "draft + needs_info + needs_decision + human review; NOT the headless "
        "one and NOT the archived one"
    )
    assert card["drafts"] == 1, "the label needs the draft share to be honest"
    assert card["in_flight"] == 2, (
        "running + ci_check only. The EPIC is `open` too, and counting it would "
        "add a permanent +1 to every project — an epic is a container, not work "
        "in flight. This assertion is what forced that rule out into the open."
    )

    page = await client.get("/projects")
    assert "ждёт вас: 4" in page.text
    assert "черновиков: 1" in page.text, (
        "a bare number reads as urgency; the label must name its composition"
    )


async def test_project_card_with_no_live_work_says_so(client: AsyncClient, db):
    # AC-3 (#567): an empty card reads as "broken", not as "nothing to do" —
    # the same rule #615 settled: silence is not an answer.
    from hub.db import seed_default_project

    await seed_default_project(db)
    await _project(db, "proj-quiet", "Quiet Project")

    page = await client.get("/projects")
    assert page.status_code == 200
    assert "Quiet Project" in page.text
    assert "Живой работы нет" in page.text


# ---------------------------------------------------------------------------
# Admin fields live with their project, and the second list is gone (#568)
# ---------------------------------------------------------------------------


async def test_projects_page_keeps_every_admin_action_available(
    client: AsyncClient, db
):
    # AC-1 (#568). The statement said "all five actions available"; spec review
    # showed that reading it literally would STRIP the guards, because the
    # template hides provision without a repo, activate outside pending, and
    # archive for `default`. So the criterion is "each action available under its
    # OWN current conditions" — and the proof is that six existing tests pass
    # untouched. This test pins the guards themselves.
    from hub import repository as repo_module
    from hub.db import seed_default_project

    await seed_default_project(db)
    with_repo = await repo_module.create_project(
        db, slug="has-repo", name="Has Repo", repo_name="owner/repo"
    )
    no_repo = await repo_module.create_project(db, slug="no-repo", name="No Repo")
    await db.execute("UPDATE projects SET status='pending' WHERE id=?", (no_repo,))
    await db.commit()

    page = (await client.get("/projects")).text

    assert f"/projects/{with_repo}/web-provision" in page, (
        "repo present → provision offered"
    )
    assert f"/projects/{no_repo}/web-provision" not in page, (
        "no repo → provision must stay hidden (test_web_provision_hidden_without_repo)"
    )
    assert f"/projects/{no_repo}/web-activate" in page, "pending → activate offered"
    assert f"/projects/{with_repo}/web-activate" not in page, "active → no activate"
    assert f"/projects/{no_repo}/web-archive" in page
    assert "/projects/web-create" in page, "creating a project is not per-project"
    assert f"/projects/{with_repo}/web-edit" in page

    # `default` must never offer archive: the routing fallback cannot be removed.
    default_id = (await repo_module.get_project_by_slug(db, "default"))["id"]
    assert f"/projects/{default_id}/web-archive" not in page


async def test_project_card_surfaces_pending_and_provision_error(
    client: AsyncClient, db
):
    # AC-2 (#568): a broken workspace is news, not administration. Behind the
    # disclosure it would be one unopened click away from invisible — the same
    # reasoning as #615: silence must not pass for "fine".
    from hub import repository as repo_module
    from hub.db import seed_default_project

    await seed_default_project(db)
    broken = await repo_module.create_project(
        db, slug="broken-ws", name="Broken WS", repo_name="owner/x"
    )
    await db.execute(
        "UPDATE projects SET provision_status='error', provision_detail='clone failed' "
        "WHERE id=?",
        (broken,),
    )
    pending = await repo_module.create_project(db, slug="waiting", name="Waiting")
    await db.execute("UPDATE projects SET status='pending' WHERE id=?", (pending,))
    await db.commit()

    page = (await client.get("/projects")).text
    header_of_broken = page.split("Broken WS", 1)[1].split("</div>", 1)[0]
    assert "ws" in header_of_broken and "error" in header_of_broken, (
        "the failure must sit in the card header, above the collapsed block"
    )
    header_of_pending = page.split("Waiting", 1)[1].split("</div>", 1)[0]
    assert "pending" in header_of_pending


async def test_projects_page_lists_each_project_once(client: AsyncClient, db):
    # AC-3 (#568): the actual defect. Until now /projects showed every project
    # TWICE — cards on top, the routing table below — which is why the page read
    # like a draft with the old version left in. Removing the table must not lose
    # a project, and an archived one is the case that would go first.
    from hub import repository as repo_module
    from hub.db import seed_default_project

    await seed_default_project(db)
    kept = await repo_module.create_project(db, slug="kept", name="Kept Project")
    gone = await repo_module.create_project(db, slug="retired", name="Retired Project")
    await db.execute("UPDATE projects SET archived=1 WHERE id=?", (gone,))
    await db.commit()

    page = (await client.get("/projects")).text
    # Match the ARTICLE, not the prefix: 'class="project-card' also hits
    # project-cards, project-card-numbers and project-card-waiting — my first
    # version of this assertion counted 10 for three projects.
    assert page.count('<article class="project-card') == 3, (
        "one card per project (default, kept, retired) and no second listing"
    )
    assert "<table" not in page, "the duplicate routing table is gone"
    assert "Retired Project" in page, "an archived project must still be reachable"
    assert kept and gone


# ---------------------------------------------------------------------------
# A counter and the list behind its link tell the same story (#617)
# ---------------------------------------------------------------------------


async def _orphan_with_status(client: AsyncClient, db, title: str, status: str) -> int:
    task_id = (await client.post("/api/tasks", json={"title": title})).json()["id"]
    await db.execute("UPDATE tasks SET status=? WHERE id=?", (status, task_id))
    await db.commit()
    return task_id


async def test_orphan_counter_and_its_link_agree(client: AsyncClient, db):
    # AC-1 (#617). Found by looking at production, not by a failing test: the
    # line said "Без эпика: 19 живых" and its link opened 51 rows, because the
    # list filter took a single status while "live" is the negation of a set.
    live = [
        await _orphan_with_status(client, db, "still open", "open"),
        await _orphan_with_status(client, db, "in review", "review"),
    ]
    done = [
        await _orphan_with_status(client, db, "finished", "completed"),
        await _orphan_with_status(client, db, "broken", "failed"),
        await _orphan_with_status(client, db, "dropped", "rejected"),
    ]

    from hub import repository as repo

    counted = await repo.count_live_orphan_tasks(db)
    assert counted == len(live), "precondition: the counter counts live ones"

    page = await client.get("/tasks/list", params={"no_epic": "1", "state": "live"})
    assert page.status_code == 200, page.text
    for task_id in live:
        assert f"/tasks/{task_id}" in page.text
    for task_id in done:
        assert f"/tasks/{task_id}" not in page.text, (
            "a finished orphan must appear neither in the number nor in the list"
        )

    # And the markup actually carries that link, so the two cannot drift apart.
    for path in ("/projects", "/partials/epics"):
        assert "state=live" in (await client.get(path)).text, (
            f"{path} link must carry it"
        )


async def test_state_filter_works_on_both_routes_and_refuses_junk(
    client: AsyncClient, db
):
    # AC-2 (#617): a parameter honoured by only one of the two routes gives a
    # link that silently ignores it — #571 shipped exactly that shape and a
    # mutation caught it. And an unknown value must fail loudly: a filter that
    # quietly does nothing leaves the caller believing the list was narrowed.
    running = await _orphan_with_status(client, db, "being worked on", "running")
    drafted = await _orphan_with_status(client, db, "a draft", "draft")

    for path in ("/tasks/list", "/tasks"):
        inflight = await client.get(path, params={"state": "inflight"})
        assert inflight.status_code == 200, inflight.text
        assert f"/tasks/{running}" in inflight.text, f"{path}: running is in flight"
        assert f"/tasks/{drafted}" not in inflight.text, f"{path}: a draft is not"

        awaiting = await client.get(path, params={"state": "awaiting"})
        assert f"/tasks/{drafted}" in awaiting.text, f"{path}: a draft awaits a human"
        assert f"/tasks/{running}" not in awaiting.text

        junk = await client.get(path, params={"state": "nonsense"})
        assert junk.status_code == 400, (
            f"{path}: an unknown mode must be refused, not ignored"
        )
        assert "nonsense" in junk.text and "live" in junk.text, (
            "the refusal must name what was passed and what is accepted"
        )


async def test_state_is_independent_and_excludes_headless_review(
    client: AsyncClient, db
):
    # AC-3 (#617): the new mode composes with the old filters instead of
    # replacing them, and a headless review belongs to the poller conveyor, not
    # to a person — the rule list_stale_by_status(require_null_review_job) and
    # the #567 card counter already follow. Disagreeing here would recreate the
    # counter/link mismatch this task removes.
    human = await _orphan_with_status(client, db, "waiting for a person", "review")
    headless = await _orphan_with_status(client, db, "conveyor review", "review")
    await db.execute("UPDATE tasks SET review_job_id='job-1' WHERE id=?", (headless,))
    await db.commit()

    awaiting = await client.get("/tasks/list", params={"state": "awaiting"})
    assert f"/tasks/{human}" in awaiting.text
    assert f"/tasks/{headless}" not in awaiting.text, (
        "a review owned by the conveyor is not a person waiting"
    )

    # The pre-existing contract is untouched: a blank parent_id still disables
    # filtering (test_tasks_list_filters_ignore_blank_parent_id pins this), and
    # status still works on its own.
    blank = await client.get("/tasks/list", params={"parent_id": "", "state": "live"})
    assert blank.status_code == 200
    assert f"/tasks/{human}" in blank.text
    by_status = await client.get("/tasks/list", params={"status": "review"})
    assert f"/tasks/{headless}" in by_status.text, (
        "state must not hijack the plain status filter"
    )
