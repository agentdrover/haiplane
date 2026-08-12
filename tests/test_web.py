from __future__ import annotations

import asyncio
import re

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
    # #628 removed the "Approve ready (N)" button this line used to pin: its
    # set was computed by the server, so the number it showed and the rows it
    # touched could differ — measured, "(1)" approved two. The group path is
    # now a selection, and the count starts at zero because nothing is ticked
    # yet. The ranking this test is actually about is asserted above and
    # unchanged.
    assert 'id="inbox-batch-count"' in html
    assert "Одобрить отмеченные" in html


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
    # The badge says "ждёт активации" since #623: the section speaks Russian, and
    # this state stayed English only because no project on prod is ever pending,
    # so nobody could see it. What is pinned is that the state is VISIBLE and its
    # action is offered — not which language the label happens to be in.
    assert "ждёт активации" in page.text and "web-activate" in page.text

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
    # #622 removed the green "ws ok" from the header: "everything is fine" is not
    # news. The provision BUTTON below is what this test is really about.
    assert "ws&nbsp;ok" not in page.text

    plugins.git_ops.clone_repo = AsyncMock(
        return_value=(False, "remote not accessible: no key")
    )
    resp = await client.post(f"/projects/{pid}/web-provision", follow_redirects=False)
    assert resp.status_code == 303
    assert "project_error=" in resp.headers["location"]
    page = await client.get(resp.headers["location"])
    assert "remote not accessible" in page.text
    page = await client.get("/projects")
    # #622: the badge is Russian now and the CAUSE is visible text — it used to
    # live only in a title attribute, which a phone never shows.
    assert "workspace: ошибка" in page.text
    assert "remote not accessible" in page.text, (
        "the reason must be readable on the card, not only on the redirect page"
    )


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
        QUEUED_STATUSES,
        TaskStatus,
    )

    # #619 took `open` out of "in flight" — approved-but-untouched is a queue, not
    # work. This assertion FAILED when that happened, which is the whole reason it
    # exists: `open` had to be given its own named set instead of falling into the
    # gap that hid `draft` until #567. Updated deliberately, not relaxed.
    assert IN_FLIGHT_STATUSES == (
        ACTIVE_STATUSES - AWAITING_HUMAN_STATUSES - QUEUED_STATUSES
    )
    assert TaskStatus.open in QUEUED_STATUSES
    assert TaskStatus.open not in IN_FLIGHT_STATUSES, (
        "an approved task nobody started is not work in progress"
    )
    assert not (IN_FLIGHT_STATUSES & AWAITING_HUMAN_STATUSES)
    assert not (IN_FLIGHT_STATUSES & QUEUED_STATUSES)
    assert not (AWAITING_HUMAN_STATUSES & FINAL_STATUSES)
    unclassified = (
        set(TaskStatus)
        - AWAITING_HUMAN_STATUSES
        - IN_FLIGHT_STATUSES
        - QUEUED_STATUSES
        - FINAL_STATUSES
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
    # #622 turned the numbers into the dashboard's chip pattern: label and value
    # are separate elements. The COUNT itself is asserted on the card above.
    assert ">Ждёт вас<" in page.text and ">4<" in page.text
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
    assert "workspace: ошибка" in header_of_broken, (
        "the failure must sit in the card header, above the collapsed block"
    )
    # #622 goes further than #568: the CAUSE is visible text now, not a title
    # attribute a phone never shows.
    assert "clone failed" in page, "the reason must be readable without hovering"
    header_of_pending = page.split("Waiting", 1)[1].split("</div>", 1)[0]
    assert "ждёт активации" in header_of_pending, (
        "the pending state belongs in the header too; the label is Russian since "
        "#623, which is when this state was first rendered and read"
    )


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


# ---------------------------------------------------------------------------
# The card's numbers lead into the work, and "in flight" stops lying (#619)
# ---------------------------------------------------------------------------


async def _project_with(client: AsyncClient, db, slug: str, name: str) -> int:
    from hub import repository as repo_module

    pid = await repo_module.create_project(db, slug=slug, name=name)
    await db.commit()
    return pid


async def test_open_is_a_queue_not_work_in_flight(client: AsyncClient, db):
    # AC-2 (#619). Live page, 10.08: it-grade-dashboard showed "в работе: 46"
    # beside "активность: 20.07" — a month of silence. All 46 were `open`:
    # approved and untouched. The most abandoned project looked like the busiest.
    from hub.db import seed_default_project
    from hub.services.dashboard import get_project_cards

    await seed_default_project(db)
    pid = await _project_with(client, db, "queue-heavy", "Queue Heavy")
    epic = (
        await client.post(
            "/api/tasks",
            json={"title": "E", "task_type": "epic", "project": "queue-heavy"},
        )
    ).json()["id"]

    async def _child(title: str, status: str) -> int:
        tid = (
            await client.post(
                "/api/tasks",
                json={"title": title, "task_type": "feature", "parent_id": epic},
            )
        ).json()["id"]
        await db.execute("UPDATE tasks SET status=? WHERE id=?", (status, tid))
        return tid

    for i in range(3):
        await _child(f"approved, untouched {i}", "open")
    await _child("someone is on it", "running")
    await db.commit()

    card = next(c for c in await get_project_cards(db) if c["project"]["id"] == pid)
    assert card["in_flight"] == 1, "only what someone is moving counts as work"
    assert card["queued"] == 3, "the queue is counted, not folded into work"

    page = (await client.get("/projects")).text
    # Chips after #622; the numbers themselves are asserted on the card above.
    assert ">В работе<" in page and ">В очереди<" in page
    assert ">1<" in page and ">3<" in page, (
        "hiding the queue would trade one lie for another"
    )


async def test_card_numbers_open_the_matching_list(client: AsyncClient, db):
    # AC-1 (#619): the page is an entry point, so its numbers must be doors. The
    # state modes from #617 make the number and the list agree by construction —
    # a link with a different rule behind it is the #571 defect (18 vs 51).
    from hub.db import seed_default_project

    await seed_default_project(db)
    await _project_with(client, db, "clickable", "Clickable")
    epic = (
        await client.post(
            "/api/tasks",
            json={"title": "E", "task_type": "epic", "project": "clickable"},
        )
    ).json()["id"]
    waiting = (
        await client.post(
            "/api/tasks",
            json={
                "title": "needs a decision",
                "task_type": "feature",
                "parent_id": epic,
            },
        )
    ).json()["id"]
    await db.execute("UPDATE tasks SET status='needs_decision' WHERE id=?", (waiting,))
    moving = (
        await client.post(
            "/api/tasks",
            json={"title": "being done", "task_type": "feature", "parent_id": epic},
        )
    ).json()["id"]
    await db.execute("UPDATE tasks SET status='running' WHERE id=?", (moving,))
    await db.commit()

    page = (await client.get("/projects")).text
    assert (
        "project=clickable&amp;state=awaiting" in page
        or "project=clickable&state=awaiting" in page
    )
    assert (
        "project=clickable&amp;state=inflight" in page
        or "project=clickable&state=inflight" in page
    )
    assert "Вся доска проекта" in page, (
        "the board needs an explicit door, not a hidden title link"
    )

    # Follow the links: the list must contain exactly what the number promised.
    awaiting_page = await client.get(
        "/tasks", params={"project": "clickable", "state": "awaiting"}
    )
    assert f"/tasks/{waiting}" in awaiting_page.text
    assert f"/tasks/{moving}" not in awaiting_page.text
    inflight_page = await client.get(
        "/tasks", params={"project": "clickable", "state": "inflight"}
    )
    assert f"/tasks/{moving}" in inflight_page.text
    assert f"/tasks/{waiting}" not in inflight_page.text


async def test_card_names_what_it_hides_and_adapts_its_label(client: AsyncClient, db):
    # AC-3 (#619): "ждёт вас: 30 (черновиков: 30)" is absurd when the numbers are
    # equal, and a card that shows 5 of 11 epics without saying so reads as "that
    # is all" — the silent-cap rule from #611 and #615.
    from hub.db import seed_default_project
    from hub.services.dashboard import PROJECT_CARD_EPIC_LIMIT

    await seed_default_project(db)
    await _project_with(client, db, "many-epics", "Many Epics")
    for i in range(PROJECT_CARD_EPIC_LIMIT + 2):
        eid = (
            await client.post(
                "/api/tasks",
                json={
                    "title": f"epic {i}",
                    "task_type": "epic",
                    "project": "many-epics",
                },
            )
        ).json()["id"]
        child = (
            await client.post(
                "/api/tasks",
                json={"title": f"child {i}", "task_type": "feature", "parent_id": eid},
            )
        ).json()["id"]
        await db.execute("UPDATE tasks SET status='draft' WHERE id=?", (child,))
    await db.commit()

    page = (await client.get("/projects")).text
    assert "И ещё 2" in page, "the hidden remainder must be named, not dropped"
    # #622: said ONCE in the section subtitle instead of once per card.
    assert page.count("свежие сверху") == 1, (
        "the order must be stated once for the section, not repeated per card"
    )
    assert "Всё ожидание — черновики" in page, (
        "when every waiting item is a draft, say so instead of '(черновиков: N)'"
    )
    assert "готово" in page, "a bare 1/2 does not say what it is a share of"


async def test_activity_is_relative_and_computed_once(client: AsyncClient, db):
    # AC-4 (#619): a raw date makes the reader do arithmetic, and the card's
    # question is "where is work happening". Computed on the server so the next
    # view needing the same phrase cannot word it differently.
    from hub.services.dashboard import humanize_since

    assert humanize_since("2026-08-10 09:00:00", now="2026-08-10 21:00:00") == "сегодня"
    assert humanize_since("2026-08-09 09:00:00", now="2026-08-10 21:00:00") == "вчера"
    assert (
        humanize_since("2026-07-20 09:00:00", now="2026-08-10 21:00:00")
        == "3 нед. назад"
    )
    assert (
        humanize_since("2026-06-24 09:00:00", now="2026-08-10 21:00:00")
        == "месяц назад"
    )
    assert humanize_since(None) == "не было", "silence is an answer, not an empty cell"
    # An ISO stamp with a T must not fall through as a raw string: prepared_at
    # taught that lesson in #616.
    assert (
        humanize_since("2026-08-09T09:00:00+00:00", now="2026-08-10 21:00:00")
        == "вчера"
    )

    from hub.db import seed_default_project

    await seed_default_project(db)
    # A project with REAL feed activity. The first version of this assertion
    # seeded an empty project, where the card says "не было" — so a mutation that
    # printed the raw date survived, because there was no date to print. Found by
    # mutation, not by re-reading.
    await _project_with(client, db, "has-activity", "Has Activity")
    epic = (
        await client.post(
            "/api/tasks",
            json={"title": "E", "task_type": "epic", "project": "has-activity"},
        )
    ).json()["id"]
    child = (
        await client.post(
            "/api/tasks", json={"title": "c", "task_type": "feature", "parent_id": epic}
        )
    ).json()["id"]
    await client.post(
        f"/api/tasks/{child}/updates",
        json={"agent": "dev", "kind": "status", "content": "worked"},
    )
    await db.execute(
        "UPDATE task_updates SET created_at='2026-07-20 09:00:00' WHERE task_id=?",
        (child,),
    )
    await db.commit()

    page = (await client.get("/projects")).text
    # #622 renamed the label to «Обновлялся:» — a verb says what the date means.
    assert "Обновлялся:" in page
    # The exact date now lives in a .u-sr-only span (a title attribute is unusable
    # on touch). So strip what the eye cannot see before judging what it sees —
    # the first version of this assertion looked at a 60-character window and
    # would have failed an honest implementation.
    visible = re.sub(r'<span class="u-sr-only">.*?</span>', "", page, flags=re.S)
    shown = visible.split("Обновлялся:", 1)[1][:60]
    assert "2026-07-20" not in shown, (
        "the visible value must be relative; the exact date belongs to assistive text"
    )
    assert "2026-07-20" in page, "and the exact date must still be available somewhere"
    assert "назад" in shown or "сегодня" in shown or "вчера" in shown, (
        f"expected a relative phrase, got: {shown.strip()[:40]!r}"
    )


# ---------------------------------------------------------------------------
# The stylesheet cannot reference what does not exist (#622)
# ---------------------------------------------------------------------------

# The debt list that used to live here held seven --clr-* names plus
# --radius-xl, and it was self-cleaning: the test asserted each entry was STILL
# undeclared. #624 fixed all seven, so that assertion failed and named every one
# of them — which is the whole point of writing an exception list by name and
# forcing it to be revisited. The list is gone because it is empty; no exception
# survives here silently.


def _stylesheet() -> str:
    from pathlib import Path

    return (
        Path(__file__).resolve().parent.parent / "hub" / "static" / "style.css"
    ).read_text()


async def test_every_css_token_and_utility_class_exists():
    # AC-1 (#622). This is the check whose ABSENCE let the defect exist: .muted
    # and .small were used 28 times across six templates while no rule defined
    # them, so every "quiet" caption rendered at body colour and size — the least
    # important text on the screen louder than the most important. And I myself
    # wrote var(--muted, #999), inventing a token that was never declared.
    import re
    from pathlib import Path

    css = _stylesheet()
    used = set(re.findall(r"var\(\s*(--[a-zA-Z0-9-]+)", css))
    declared = set(re.findall(r"^\s*(--[a-zA-Z0-9-]+)\s*:", css, re.M))
    assert used and declared, "guard the guard: an empty parse agrees with anything"

    # No allowlist any more (#624 emptied it). A var() that resolves to nothing
    # invalidates the whole declaration, so an undeclared token does not shift a
    # colour — it deletes the border, the background, the radius.
    undeclared = used - declared
    assert not undeclared, (
        f"CSS references tokens that do not exist: {sorted(undeclared)}"
    )

    # Utility classes used in templates must have a rule. Only the ones this task
    # is about; a general class linter is a different job.
    templates = Path(__file__).resolve().parent.parent / "hub" / "templates"
    for utility in ("muted", "small"):
        used_in = [
            p.name
            for p in templates.rglob("*.html")
            if re.search(rf'class="[^"]*\b{utility}\b', p.read_text())
        ]
        assert used_in, f"precondition: .{utility} is used somewhere"
        assert re.search(rf"^\s*\.{utility}\b[^{{]*{{", css, re.M), (
            f".{utility} is used in {used_in} but no CSS rule defines it"
        )


async def test_workspace_error_shows_its_reason_visibly(client: AsyncClient, db):
    # AC-3 (#622). #568 kept the failure in the header, which was right; what it
    # missed is that the CAUSE lived only in a title attribute — invisible on a
    # phone, unread by screen readers on touch. And the green "ws ok" left: a
    # state that needs no action is not news.
    from hub import repository as repo_module
    from hub.db import seed_default_project

    await seed_default_project(db)
    broken = await repo_module.create_project(
        db, slug="broken", name="Broken", repo_name="owner/x"
    )
    healthy = await repo_module.create_project(
        db, slug="healthy", name="Healthy", repo_name="owner/y"
    )
    await db.execute(
        "UPDATE projects SET provision_status='error', provision_detail='клон не удался: нет ключа' WHERE id=?",
        (broken,),
    )
    await db.execute(
        "UPDATE projects SET provision_status='ok', provision_detail='всё на месте' WHERE id=?",
        (healthy,),
    )
    await db.commit()

    page = (await client.get("/projects")).text
    assert "workspace: ошибка" in page
    assert "клон не удался: нет ключа" in page, (
        "the cause must be visible text, not a title attribute"
    )
    assert "ws&nbsp;ok" not in page and "ws ok" not in page, (
        "'everything is fine' is not news and does not belong in the header"
    )
    assert "всё на месте" not in page, "a healthy workspace needs no explanation"


async def test_projects_page_speaks_one_language(client: AsyncClient, db):
    # AC-4 (#622): a Russian body with English chrome makes the reader translate
    # while scanning. And the footnote used to sit ABOVE the h1 — the first tab
    # stop answered a question the arriving reader does not have yet.
    from hub.db import seed_default_project

    await seed_default_project(db)
    page = (await client.get("/projects")).text

    assert '<h1 class="page-title">Проекты' in page
    for english in (
        "Provision<",
        "Activate<",
        ">Archive<",
        ">edit<",
        ">Save<",
        "New Project<",
    ):
        assert english not in page, f"English chrome left on a Russian page: {english}"

    body = page.split("<main", 1)[1]
    h1_at = body.index("page-title")
    note_at = body.index("Без эпика") if "Без эпика" in body else h1_at + 1
    assert h1_at < note_at, "the section title must come before its footnote"


async def test_card_uses_the_dashboard_chip_pattern(client: AsyncClient, db):
    # AC-2 (#622): the same visual language the owner already reads on the
    # dashboard, not a second one invented for the same meaning. And zero is
    # dimmed rather than shouted.
    from hub.db import seed_default_project

    await seed_default_project(db)
    page = (await client.get("/projects")).text

    assert "topbar-stat-label" in page and "topbar-stat-value" in page
    assert "is-zero" in page, "a nothing must be dimmed, not equal to a number"
    assert "·" not in page.split("topbar-stats", 1)[1][:600], (
        "separators and nested brackets are what made the line unreadable"
    )
    # Screen readers list links out of context: five identical "в работе: 0" told
    # the user nothing about which project they belonged to.
    assert 'aria-label="Ждёт вашего решения:' in page


# ---------------------------------------------------------------------------
# A dark interface carries no light-theme patches, and a rule that never
# applies is not a rule (#624)
# ---------------------------------------------------------------------------

# Backgrounds are what gives a stolen palette away. This stylesheet
# legitimately hardcodes vivid accents for :hover — #00d2a4 at 0.49 relative
# luminance, #fbb034 at 0.52 — and white text on accent fills. Those are
# decisions. What it must not carry is a light SURFACE: the five blocks this
# task fixed sat at 0.74–0.90, i.e. nearly white, copied out of a light theme
# into a dark interface. 0.6 separates the two groups with room on both sides,
# so the threshold is not a knob tuned until the test passed.
_MAX_BACKGROUND_LUMINANCE = 0.6

# The five rules whose colours came from a light palette instead of the token
# system. Named, not counted: the statement said "one such block" because I had
# looked at one, and a number nobody can check is how that error survived.
_ONCE_LIGHT_THEME_RULES = (
    ".admin-warning",
    ".admin-notice",
    ".badge-active",
    ".badge-disabled",
    ".stat-card-warn",
)

# AC-3 forbids inventing tokens to fix these blocks — the semantic pairs
# (--orange/--orange-dim and friends) already exist. If a later task adds a
# token on purpose, update this number and say why; the point is that nobody
# adds one by accident while recolouring a warning.
_DECLARED_TOKEN_COUNT = 50


def _relative_luminance(colour: str) -> float:
    """WCAG relative luminance of a #rgb / #rrggbb colour."""
    digits = colour.lstrip("#")
    if len(digits) == 3:
        digits = "".join(c * 2 for c in digits)
    channels = [int(digits[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [
        c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _css_rules(css: str) -> list[tuple[str, str]]:
    """Flat (selector, body) pairs. @media wrappers surface as their own
    pseudo-rule, and the rules inside them are still seen — enough for a
    stylesheet with no deeper nesting."""
    import re

    return [
        (m.group(1).strip(), m.group(2))
        for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", css)
    ]


async def test_no_light_theme_hardcodes_in_a_dark_interface():
    # AC-3 (#624). The statement claimed ONE light-theme block. Counting by
    # luminance instead of by memory found FIVE, every one of them rendered on a
    # real page: an admin warning, an admin notice, two key badges and the warning
    # stat card. A near-white panel in a dark interface does not read as
    # "attention" — it reads as a rendering failure.
    import re

    css = _stylesheet()
    rules = _css_rules(css)
    assert rules, "guard the guard: an empty parse agrees with anything"

    too_light = []
    for selector, body in rules:
        for declaration in re.finditer(r"\bbackground(?:-color)?\s*:\s*([^;}]*)", body):
            for colour in re.findall(
                r"#[0-9a-fA-F]{3}\b|#[0-9a-fA-F]{6}\b", declaration.group(1)
            ):
                if _relative_luminance(colour) > _MAX_BACKGROUND_LUMINANCE:
                    too_light.append(
                        f"{selector} → {colour} ({_relative_luminance(colour):.2f})"
                    )
    assert not too_light, "light-theme surfaces inside a dark interface: " + "; ".join(
        too_light
    )

    # The fix had to come from the token system, not from a better-looking hex.
    for selector in _ONCE_LIGHT_THEME_RULES:
        bodies = [
            b
            for sel, b in rules
            if re.search(rf"(^|,|\s){re.escape(selector)}(\s|,|$)", sel)
        ]
        assert bodies, f"precondition: {selector} still exists to be checked"
        for body in bodies:
            leftovers = re.findall(r"#[0-9a-fA-F]{3,6}\b", body)
            assert not leftovers, (
                f"{selector} still hardcodes {leftovers} — the token pairs "
                "(--orange/--orange-dim and friends) are what makes it a theme"
            )

    root = re.search(r":root\s*\{([^}]*)\}", css, re.S)
    assert root, "precondition: the token block exists"
    declared = re.findall(r"^\s*(--[a-zA-Z0-9-]+)\s*:", root.group(1), re.M)
    assert len(declared) == _DECLARED_TOKEN_COUNT, (
        f"token count changed to {len(declared)}: recolouring five blocks must not "
        "invent tokens — if a token was added on purpose, update _DECLARED_TOKEN_COUNT"
    )


async def test_small_badge_size_actually_applies():
    # AC-4 (#624). .badge--xs asked for 9px and every one of its 17 uses rendered
    # at 11px. Not specificity — both selectors are a single class, so the FILE
    # ORDER decided it and .badge, sitting 1200 lines lower, won. The fix is the
    # order; raising specificity would have left the same trap for the next
    # person, who would also read the rule and believe it.
    import re
    from pathlib import Path

    css = _stylesheet()
    base = [m.start() for m in re.finditer(r"^\.badge\s*\{", css, re.M)]
    modifier = [m.start() for m in re.finditer(r"^\.badge--xs\s*\{", css, re.M)]
    assert len(base) == 1, f"precondition: one .badge rule, found {len(base)}"
    assert len(modifier) == 1, (
        f"precondition: one .badge--xs rule, found {len(modifier)}"
    )

    base_body = re.match(r"^\.badge\s*\{([^}]*)\}", css[base[0] :], re.S)
    assert base_body and "font-size" in base_body.group(1), (
        "precondition: .badge is the rule that sets the competing font-size"
    )
    assert modifier[0] > base[0], (
        "equal specificity means source order decides: .badge--xs must come "
        "AFTER .badge or its font-size is decoration in a text file"
    )
    modifier_body = re.match(r"^\.badge--xs\s*\{([^}]*)\}", css[modifier[0] :], re.S)
    assert modifier_body and "font-size: 9px" in modifier_body.group(1), (
        "the compact badge keeps the size its author wrote"
    )

    # Dead classes are removed, not fixed: a class no template uses cannot be
    # verified by looking at the product, so it rots into a second answer to a
    # question that already has one.
    templates = Path(__file__).resolve().parent.parent / "hub" / "templates"
    markup = "\n".join(p.read_text() for p in templates.rglob("*.html"))
    for dead in (".btn--xs", ".badge-locked"):
        # re.escape already escapes the leading dot; adding another backslash made
        # this pattern hunt for a literal backslash and match nothing, so the
        # assertion passed no matter what. A mutation putting .btn--xs back is what
        # exposed it — the test had been agreeing with everything.
        assert not re.search(rf"^{re.escape(dead)}\s*[,{{]", css, re.M), (
            f"{dead} is a second answer to a question .btn-xs already answers — "
            "delete it instead of styling it"
        )
    assert re.search(r"^\.btn-xs\s*\{", css, re.M), (
        "the surviving small button keeps its rule"
    )
    assert 'class="btn btn-xs' in markup or "btn-xs" in markup, (
        "precondition: .btn-xs is the one templates actually use"
    )


# ---------------------------------------------------------------------------
# The card order answers "where am I needed", not "where did something move"
# (#623)
# ---------------------------------------------------------------------------


async def _project_with_epic(db, *, slug: str, name: str, child_status: str):
    """A project whose epic carries one child in the given status."""
    from hub import repository as repo_module

    project_id = await repo_module.create_project(db, slug=slug, name=name)
    epic_id = await repo_module.create_task(
        db,
        title=f"Эпик проекта {name}",
        description="",
        task_type="epic",
        runtime="auto",
        source="agent",
        assigned_agent="",
        rationale="",
        status="open",
        auto_review=True,
        parent_id=None,
        priority="medium",
    )
    child_id = await repo_module.create_task(
        db,
        title=f"Задача проекта {name}",
        description="",
        task_type="task",
        runtime="auto",
        source="agent",
        assigned_agent="",
        rationale="",
        status=child_status,
        auto_review=True,
        parent_id=epic_id,
        priority="medium",
    )
    await db.execute(
        "UPDATE tasks SET project_id = ? WHERE id IN (?, ?)",
        (project_id, epic_id, child_id),
    )
    await db.commit()
    return project_id, child_id


async def test_cards_put_what_waits_for_a_human_first(db):
    # AC-2 (#623). The order used to answer "where did something move last",
    # which is the question the feed already answers. The card list is read to
    # decide where to GO, and a project holding a decision outranks a project
    # where an agent merged something an hour ago — the human is the scarce one.
    from hub.db import seed_default_project
    from hub.services.dashboard import get_project_cards
    from hub import repository as repo_module

    await seed_default_project(db)

    # Waits for a human (a draft needs approval) and has NO feed activity at all.
    waiting, _ = await _project_with_epic(
        db, slug="zhdet", name="Ждёт решения", child_status="draft"
    )
    # Nothing waits, but work moved just now — the loudest thing under the old key.
    fresh, fresh_child = await _project_with_epic(
        db, slug="svezhiy", name="Свежая активность", child_status="completed"
    )
    await repo_module.add_task_update(
        db, fresh_child, "hub", "status", "работа шла только что"
    )
    await db.commit()

    order = [c["project"]["id"] for c in await get_project_cards(db)]
    assert waiting in order and fresh in order, "precondition: both cards are built"
    assert order.index(waiting) < order.index(fresh), (
        "a project holding a human decision must come before a project that "
        f"merely saw recent activity; got order {order} "
        f"(waiting={waiting}, fresh={fresh})"
    )


async def test_activity_still_breaks_ties_within_the_same_group(db):
    # The new key must not throw the old one away: among projects that equally
    # do or do not wait for a human, "where did work happen last" is still the
    # right tiebreak — otherwise the list degenerates into an order by id, which
    # answers "which was created last" and nothing more.
    #
    # The fresher project is created FIRST on purpose, so its id is the SMALLER
    # one: with the ids agreeing with the activity, dropping the activity key
    # entirely would still pass and the test would prove nothing. A mutation
    # that removed it did exactly that until this fixture was rebuilt.
    from hub.db import seed_default_project
    from hub.services.dashboard import get_project_cards
    from hub import repository as repo_module

    await seed_default_project(db)
    newer, newer_child = await _project_with_epic(
        db, slug="novyy", name="Новая активность", child_status="completed"
    )
    older, older_child = await _project_with_epic(
        db, slug="staryy", name="Старая активность", child_status="completed"
    )
    await repo_module.add_task_update(db, older_child, "hub", "status", "давно")
    await db.execute(
        "UPDATE task_updates SET created_at = '2020-01-01 00:00:00' WHERE task_id = ?",
        (older_child,),
    )
    await repo_module.add_task_update(db, newer_child, "hub", "status", "недавно")
    await db.commit()

    order = [c["project"]["id"] for c in await get_project_cards(db)]
    assert order.index(newer) < order.index(older), (
        f"neither project waits for a human, so the fresher one leads; got {order}"
    )


async def test_a_bigger_backlog_does_not_outrank_fresher_work(db):
    # The first sort key is a BOOLEAN, and this pins why. Both projects wait for
    # a human, so the question is which to open first — and "the one where five
    # things are stuck" is not a better answer than "the one that moved today".
    # Sorting by the COUNT would nail the largest backlog to the top forever,
    # which is the same defect as ordering by id: a stable answer to a question
    # nobody asked. A mutation replacing bool(...) with the raw number survived
    # until this test existed.
    from hub.db import seed_default_project
    from hub.services.dashboard import get_project_cards
    from hub import repository as repo_module

    await seed_default_project(db)
    big, big_child = await _project_with_epic(
        db, slug="bolshoy", name="Большой бэклог", child_status="draft"
    )
    for i in range(4):
        extra = await repo_module.create_task(
            db,
            title=f"Ещё черновик {i}",
            description="",
            task_type="task",
            runtime="auto",
            source="agent",
            assigned_agent="",
            rationale="",
            status="draft",
            auto_review=True,
            parent_id=None,
            priority="medium",
        )
        await db.execute(
            "UPDATE tasks SET parent_id = (SELECT parent_id FROM tasks WHERE id = ?), "
            "project_id = ? WHERE id = ?",
            (big_child, big, extra),
        )
    await repo_module.add_task_update(db, big_child, "hub", "status", "давно")
    await db.execute(
        "UPDATE task_updates SET created_at = '2020-01-01 00:00:00' WHERE task_id = ?",
        (big_child,),
    )
    small, small_child = await _project_with_epic(
        db, slug="malenkiy", name="Одно решение", child_status="draft"
    )
    await repo_module.add_task_update(db, small_child, "hub", "status", "сегодня")
    await db.commit()

    cards = await get_project_cards(db)
    counts = {c["project"]["id"]: c["awaiting_human"] for c in cards}
    assert counts[big] > counts[small], (
        f"precondition: the big backlog really is bigger, got {counts}"
    )
    order = [c["project"]["id"] for c in cards]
    assert order.index(small) < order.index(big), (
        "both wait for a human, so the fresher one leads regardless of backlog "
        f"size; got {order} (small={small}, big={big})"
    )


async def test_the_keyboard_reaches_the_content_before_the_menu(
    client: AsyncClient, db
):
    # AC-4 (#623). Measured before this task: the first focus stop on EVERY page
    # was "Dashboard", and six sidebar links stood between the keyboard and the
    # content. The fix is a link that is invisible until focused — it costs a
    # mouse user nothing and saves everyone else six tab presses per page.
    import re

    from hub.db import seed_default_project

    await seed_default_project(db)
    page = (await client.get("/projects")).text

    focusable = re.findall(r"<(?:a\s[^>]*href|button|summary|input)[^>]*>", page)
    assert focusable, "precondition: the page has focusable elements at all"
    assert "skip-link" in focusable[0], (
        f"the skip link must be the FIRST focus stop, not somewhere after the "
        f"menu; first was {focusable[0][:80]}"
    )

    target = re.search(r'class="skip-link" href="#([\w-]+)"', page)
    assert target, "the skip link must point somewhere"
    assert f'id="{target.group(1)}"' in page, (
        f"#{target.group(1)} does not exist on the page — a skip link that lands "
        "nowhere is worse than none, because the keyboard user loses their place"
    )


# ---------------------------------------------------------------------------
# The number on a card and the list behind its link, part three (#621)
# ---------------------------------------------------------------------------


async def _bulk_noise_tasks(db, count: int, status: str = "open") -> None:
    """`count` tasks belonging to no project — pure limit pressure.

    Inserted through SQL rather than the API on purpose: this fixture exists to
    push the interesting rows PAST the page limit, and a hundred round trips
    through the create pipeline would buy nothing but seconds.

    ``status`` is a parameter because noise only presses where the filter under
    test lets it through: 150 `open` rows are invisible to a test of the
    `awaiting` mode, and a test whose noise gets filtered out for it passes on
    the broken code. That mistake was made once here and caught by a test that
    went green before its fix existed.
    """
    await db.executemany(
        f"INSERT INTO tasks (title, task_type, status) VALUES (?, 'task', '{status}')",  # noqa: E501, S608
        [(f"noise {status} {i}",) for i in range(count)],
    )
    await db.commit()


async def test_project_filter_survives_the_page_limit(client: AsyncClient, db):
    # AC-1 (#621). Live, 12.08: audit-evidence promised 17 queued and its link
    # opened ZERO rows, because /tasks took the newest 100 tasks GLOBALLY and
    # only then dropped the ones belonging to other projects. An empty page is
    # indistinguishable from "this project has no work" — the quiet kind of lie.
    #
    # The fixture puts the project's rows BEHIND a hundred newer ones on
    # purpose. A test whose rows all fit inside the limit passes on the broken
    # code too, which is exactly how this defect survived #339 and #370.
    from hub.db import seed_default_project

    await seed_default_project(db)
    await _project_with(client, db, "old-work", "Old Work")
    epic = (
        await client.post(
            "/api/tasks",
            json={"title": "E", "task_type": "epic", "project": "old-work"},
        )
    ).json()["id"]
    buried = (
        await client.post(
            "/api/tasks",
            json={
                "title": "buried by newer work",
                "task_type": "feature",
                "parent_id": epic,
            },
        )
    ).json()["id"]
    await db.execute("UPDATE tasks SET status='open' WHERE id=?", (buried,))
    await db.commit()

    await _bulk_noise_tasks(db, 150)

    unscoped = await client.get("/tasks")
    assert f"/tasks/{buried}" not in unscoped.text, (
        "precondition: the noise really does push the project past the limit"
    )

    for params in ({"project": "old-work"}, {"project": "old-work", "state": "queued"}):
        page = await client.get("/tasks", params=params)
        assert page.status_code == 200, page.text
        assert f"/tasks/{buried}" in page.text, (
            f"{params}: the project filter has to reach the query and run BEFORE "
            "the limit — filtering afterwards spends the whole limit on other "
            "projects' rows and returns an empty page"
        )


async def test_the_queue_counts_epics_the_same_way_its_link_does(
    client: AsyncClient, db
):
    # AC-2 (#621). Live, 12.08: notesforllm promised 4 queued and opened 5,
    # calc-kids promised 1 and opened 2. The counter excludes epics
    # (project_work_summary), the state filter did not — so a project's own epic
    # sitting in `open` appeared in the list but not in the number.
    #
    # Resolved in favour of excluding, the same asymmetry #567 settled for "in
    # flight": an epic lives its whole life in `open` as a container, so it is
    # not "approved and nobody started it".
    import re

    from hub.db import seed_default_project
    from hub.services.dashboard import get_project_cards

    await seed_default_project(db)
    pid = await _project_with(client, db, "one-epic", "One Epic")
    epic = (
        await client.post(
            "/api/tasks",
            json={
                "title": "a container, not a queue item",
                "task_type": "epic",
                "project": "one-epic",
            },
        )
    ).json()["id"]
    queued = (
        await client.post(
            "/api/tasks",
            json={"title": "really queued", "task_type": "feature", "parent_id": epic},
        )
    ).json()["id"]
    await db.execute("UPDATE tasks SET status='open' WHERE id IN (?,?)", (epic, queued))
    await db.commit()

    card = next(c for c in await get_project_cards(db) if c["project"]["id"] == pid)
    assert card["queued"] == 1, "precondition: the counter does not count the epic"

    for path in ("/tasks", "/tasks/list"):
        page = await client.get(path, params={"project": "one-epic", "state": "queued"})
        assert page.status_code == 200, page.text
        # Anchored on the ROW marker, not on a bare "/tasks/{id}": a child row
        # links to its own parent, so the loose form finds the epic in the very
        # row that proves it was excluded. Same marker the production walk
        # counted rows by.
        rows = re.findall(r'id="task-row-(\d+)"', page.text)
        assert rows == [str(queued)], (
            f"{path}: the list must hold exactly what the number promised — the "
            f"epic is a container in the count, so it cannot be a queue item in "
            f"the list; got rows {rows}"
        )


# ---------------------------------------------------------------------------
# The scope survives the first click on a filter (#626)
# ---------------------------------------------------------------------------


async def _scoped_project_fixture(client: AsyncClient, db) -> tuple[int, int]:
    """A project holding one draft and one running task, buried under newer rows.

    Returns (draft id, running id). The burial matters: #621 fixed the page by
    moving the project INTO the query, and a fixture whose rows fit inside the
    limit cannot tell a scoped fragment from an unscoped one.
    """
    from hub.db import seed_default_project

    await seed_default_project(db)
    await _project_with(client, db, "scoped", "Scoped")
    epic = (
        await client.post(
            "/api/tasks",
            json={"title": "E", "task_type": "epic", "project": "scoped"},
        )
    ).json()["id"]

    async def _child(title: str, status: str) -> int:
        tid = (
            await client.post(
                "/api/tasks",
                json={"title": title, "task_type": "feature", "parent_id": epic},
            )
        ).json()["id"]
        await db.execute("UPDATE tasks SET status=? WHERE id=?", (status, tid))
        return tid

    drafted = await _child("scoped draft", "draft")
    running = await _child("scoped running", "running")
    await db.commit()
    # Noise in BOTH statuses under test: `open` rows are invisible to the
    # `awaiting` mode, so open-only noise would leave that test proving nothing.
    await _bulk_noise_tasks(db, 150, status="open")
    await _bulk_noise_tasks(db, 150, status="draft")
    return drafted, running


async def test_filter_change_keeps_the_project_and_state_it_arrived_with(
    client: AsyncClient, db
):
    # AC-1 (#626). Live, 12.08: /tasks?project=default&state=awaiting showed 28
    # rows — exactly what the card promised. Changing ANY filter sent htmx to
    # /tasks/list, which knew nothing of either parameter, and the table became
    # 100 rows across every project while the selector still said "Default".
    # Widening a list looks exactly like filtering it, which is what makes this
    # quiet.
    import re

    drafted, running = await _scoped_project_fixture(client, db)

    # What the page shows when you arrive from a card.
    arrived = await client.get(
        "/tasks", params={"project": "scoped", "state": "awaiting"}
    )
    assert re.findall(r'id="task-row-(\d+)"', arrived.text) == [str(drafted)]

    # What htmx asks for when a filter changes — now carrying the same scope.
    after_click = await client.get(
        "/tasks/list",
        params={"project": "scoped", "state": "awaiting", "priority": "medium"},
    )
    assert after_click.status_code == 200, after_click.text
    rows = re.findall(r'id="task-row-(\d+)"', after_click.text)
    assert rows == [str(drafted)], (
        "changing a filter must NARROW what was on screen, not silently replace "
        f"it with every project's rows; got {len(rows)} rows"
    )
    assert str(running) not in rows, "the state mode has to survive the click too"


async def test_fragment_and_page_agree_on_the_project_scope(client: AsyncClient, db):
    # AC-2 (#626). Compared as SETS, not sizes: two lists of equal length can
    # hold different rows, and that mistake would let this pass on broken code.
    import re

    drafted, running = await _scoped_project_fixture(client, db)

    for params in ({"project": "scoped"}, {"project": "scoped", "state": "live"}):
        page = await client.get("/tasks", params=params)
        fragment = await client.get("/tasks/list", params=params)
        assert fragment.status_code == 200, fragment.text
        page_rows = set(re.findall(r'id="task-row-(\d+)"', page.text))
        frag_rows = set(re.findall(r'id="task-row-(\d+)"', fragment.text))
        assert page_rows == frag_rows, (
            f"{params}: the fragment and the page must answer the same question — "
            f"page had {len(page_rows)} rows, fragment {len(frag_rows)}"
        )
        assert {str(drafted), str(running)} <= page_rows, (
            "precondition: the project's own rows are in the scoped answer"
        )


async def test_the_filter_bar_carries_the_scope_it_was_opened_with(
    client: AsyncClient, db
):
    # AC-3 (#626). The server side alone is not a fix: hx-include collects
    # ".filter-bar [name]", and the project selector lives in the header, OUTSIDE
    # that container. Measured on production before this task, the bar sent
    # exactly eight fields and none of them was project, state or no_epic — so a
    # fixed handler would still never be told.
    import re

    await _scoped_project_fixture(client, db)
    page = await client.get(
        "/tasks", params={"project": "scoped", "state": "awaiting", "no_epic": "1"}
    )
    bar = page.text[
        page.text.index('class="filter-bar"') : page.text.index('id="task-table-wrap"')
    ]
    sent = set(re.findall(r'name="([^"]+)"', bar))
    assert {"project", "state", "no_epic"} <= sent, (
        f"the bar must send the scope it was opened with; it sends {sorted(sent)}"
    )
    assert 'value="scoped"' in bar and 'value="awaiting"' in bar, (
        "the hidden fields must carry the CURRENT values, not empty placeholders"
    )


async def test_project_selector_keeps_the_other_filters(client: AsyncClient, db):
    # AC-4 (#626). The selector is a GET form; submitting it replaces the whole
    # query string. Its own template said so: "keeps other query params out of
    # scope for V1". Narrowing by project should not clear the status you were
    # looking at.
    import re

    await _scoped_project_fixture(client, db)
    page = await client.get(
        "/tasks", params={"project": "scoped", "status": "draft", "priority": "high"}
    )
    form = re.search(r'<form[^>]*class="project-selector".*?</form>', page.text, re.S)
    assert form, "the selector form must be on the page"
    carried = dict(re.findall(r'name="(\w+)"\s+value="([^"]*)"', form.group(0)))
    assert carried.get("status") == "draft" and carried.get("priority") == "high", (
        f"switching project must keep the other filters; the form carries {carried}"
    )

    # ...and the dashboard, which SHARES this template, must not grow task-page
    # filter fields in its own URL.
    dash = await client.get("/", params={"project": "scoped"})
    dash_form = re.search(
        r'<form[^>]*class="project-selector".*?</form>', dash.text, re.S
    )
    assert dash_form, "the dashboard has the selector too"
    assert 'name="status"' not in dash_form.group(0), (
        "the dashboard has no task filters to preserve — carrying them there "
        "would put meaningless parameters in its URL"
    )


async def test_the_applied_state_mode_is_visible_and_removable(client: AsyncClient, db):
    # AC-5 (#626). A filter with no on-screen representation cannot be read or
    # undone: today the only way out of state=awaiting is Reset, which drops the
    # project as well. So the mode gets a control, and clearing it keeps the
    # project.
    import re

    await _scoped_project_fixture(client, db)
    page = (
        await client.get("/tasks", params={"project": "scoped", "state": "awaiting"})
    ).text
    assert "state-chip" in page, "an applied mode must be visible on the page"
    clear = re.search(r'class="[^"]*state-chip-clear[^"]*"\s+href="([^"]+)"', page)
    assert clear, "the mode must be removable without hunting for Reset"
    target = clear.group(1).replace("&amp;", "&")
    assert "project=scoped" in target, (
        f"clearing the mode must keep the project; it goes to {target}"
    )
    assert "state=" not in target, "…and must actually drop the mode"


# ---------------------------------------------------------------------------
# The dashboard and the inbox stop filtering by project after their limit (#627)
# ---------------------------------------------------------------------------


async def _dashboard_project_fixture(client: AsyncClient, db) -> dict[str, int]:
    """A project with one task per interesting status, buried under newer rows.

    40 noise rows PER STATUS, because these lists are capped at 20 — a fixture
    that fits inside the cap proves nothing, and this is the third task in a row
    where that trap was the real risk (#621, #626).
    """
    from hub.db import seed_default_project

    await seed_default_project(db)
    await _project_with(client, db, "dash", "Dash")
    epic = (
        await client.post(
            "/api/tasks", json={"title": "E", "task_type": "epic", "project": "dash"}
        )
    ).json()["id"]

    ids: dict[str, int] = {}
    for status in ("draft", "review", "running", "needs_decision"):
        tid = (
            await client.post(
                "/api/tasks",
                json={
                    "title": f"dash {status}",
                    "task_type": "feature",
                    "parent_id": epic,
                },
            )
        ).json()["id"]
        await db.execute("UPDATE tasks SET status=? WHERE id=?", (status, tid))
        ids[status] = tid
    await db.commit()
    for status in ("draft", "review", "running", "needs_decision"):
        await _bulk_noise_tasks(db, 40, status=status)
    return ids


async def test_dashboard_keeps_the_project_rows_past_its_limit(client: AsyncClient, db):
    # AC-1 (#627). Measured before the fix: with 40 newer rows per status, the
    # project's own tasks were absent from data.active_tasks, draft_tasks,
    # review_tasks, needs_decision_tasks AND inbox['decisions'] — every list is
    # capped at 20 in the service layer, and the project filter ran afterwards.
    # Choosing an empty board over a wrong one is not a choice a reader can see.
    from hub import services

    ids = await _dashboard_project_fixture(client, db)

    # Asserted at the SERVICE layer, where the cap lives. The first version of
    # this test read the whole dashboard page and passed on the broken code —
    # the epic list renders links to its own children, so the ids were on the
    # page for a reason that had nothing to do with the board.
    data = await services.get_dashboard_data(db, project="dash")
    inbox = await services.get_inbox_data(db, project="dash")

    def _ids(items):
        return {t.id if hasattr(t, "id") else t.get("id") for t in items}

    assert ids["running"] in _ids(data.active_tasks), "board lost the running task"
    assert ids["draft"] in _ids(data.draft_tasks), "board lost the draft"
    assert ids["review"] in _ids(data.review_tasks), "board lost the review"
    assert ids["needs_decision"] in _ids(data.needs_decision_tasks), (
        "board lost the decision"
    )
    assert ids["draft"] in _ids(inbox["drafts"]), "inbox lost the draft"
    assert ids["needs_decision"] in _ids(inbox["decisions"]), (
        "inbox lost the decision — this is the list that was measured empty"
    )

    # Nothing from outside the project rode along.
    assert not any(t.title.startswith("noise") for t in data.draft_tasks), (
        "a scoped board must not carry other projects' rows"
    )


async def test_no_consumer_filters_by_project_after_its_limit(client: AsyncClient, db):
    # AC-2 (#627). All four consumers, not three of four: the same defect left
    # in one place is the same defect. #370 fixed the API, #621 the tasks page,
    # and this is the third appearance — so the technique goes, not one more
    # caller.
    import re
    from pathlib import Path

    ids = await _dashboard_project_fixture(client, db)

    # Named per fragment, and deliberately NOT "at least one row survived": the
    # draft queue is ordered created_at ASC, so the project's oldest draft rides
    # at the front whether or not scoping works. A mutation that removed the
    # scoping passed against the loose version of this assertion. Each id below
    # is one the id-DESC ordering genuinely buries.
    expected = {
        "/partials/inbox": [ids["needs_decision"]],
        "/partials/kanban": [ids["running"], ids["review"], ids["needs_decision"]],
    }
    for path, must_appear in expected.items():
        frag = await client.get(path, params={"project": "dash"})
        assert frag.status_code == 200, frag.text
        found = set(re.findall(r"/tasks/(\d+)", frag.text))
        missing = [i for i in must_appear if str(i) not in found]
        assert not missing, (
            f"{path}: the project's rows {missing} did not survive — the "
            "fragment is capped at 20 before the project could be applied"
        )

    # And the technique itself is gone, so a fifth caller cannot reintroduce it.
    web = (Path(__file__).resolve().parent.parent / "hub" / "web.py").read_text()
    assert "_filter_by_ids" not in web, (
        "post-filtering a limited list by project is the defect itself; leaving "
        "the helper in place invites the next caller to repeat it"
    )


# ---------------------------------------------------------------------------
# Approving from the inbox acts on what was SELECTED (#628)
# ---------------------------------------------------------------------------


async def _ready_draft(
    client: AsyncClient, db, title: str, *, project_epic=None
) -> int:
    """A DoR-ready draft, optionally hung under an epic so it has a project."""
    task_id = await _draft_with_readiness(client, title, ready=True)
    if project_epic is not None:
        await db.execute(
            "UPDATE tasks SET parent_id=?, task_type='feature' WHERE id=?",
            (project_epic, task_id),
        )
        await db.commit()
    return task_id


async def _status_of(db, task_id: int) -> str:
    rows = await db.execute_fetchall("SELECT status FROM tasks WHERE id=?", (task_id,))
    return rows[0]["status"]


async def test_group_approve_touches_only_what_was_selected(client: AsyncClient, db):
    # AC-1 (#628). Measured on the old button: viewing a project whose inbox
    # held ONE ready draft, the click approved two — the second belonged to a
    # project the reader could not see. Both went draft → open. A label reading
    # "(1)" that moves two rows is not an off-by-one, it is an action whose
    # radius is invisible until afterwards.
    from hub.db import seed_default_project

    await seed_default_project(db)
    await _project_with(client, db, "picked", "Picked")
    epic = (
        await client.post(
            "/api/tasks",
            json={"title": "E", "task_type": "epic", "project": "picked"},
        )
    ).json()["id"]
    chosen_a = await _ready_draft(client, db, "chosen a", project_epic=epic)
    chosen_b = await _ready_draft(client, db, "chosen b", project_epic=epic)
    shown_not_chosen = await _ready_draft(
        client, db, "shown, not ticked", project_epic=epic
    )
    outside = await _ready_draft(client, db, "another project entirely")

    # The selection has to be reachable from the page, not only from a curl:
    # a server that honours task_ids while the inbox renders no checkboxes is
    # a fix nobody can use, and a mutation that removed them stayed green
    # until this assertion existed.
    page = (await client.get("/partials/inbox")).text
    for task_id in (chosen_a, shown_not_chosen):
        assert f'name="task_ids" value="{task_id}"' in page, (
            f"#{task_id} has no checkbox — the reader cannot tick it"
        )

    resp = await client.post(
        "/tasks/web-batch-approve-selected",
        data={"task_ids": [str(chosen_a), str(chosen_b)]},
    )
    assert resp.status_code in (200, 303), resp.text

    assert await _status_of(db, chosen_a) == "open"
    assert await _status_of(db, chosen_b) == "open"
    assert await _status_of(db, shown_not_chosen) == "draft", (
        "a task that was on screen but NOT ticked must not be approved"
    )
    assert await _status_of(db, outside) == "draft", (
        "a task the reader never saw must not be approved — this is the "
        "measured defect: the old button approved it too"
    )


async def test_empty_selection_approves_nothing(client: AsyncClient, db):
    # AC-2 (#628). The trap worth naming: an empty list read as "then approve
    # everything" would rebuild the same defect in new clothes. Nothing is the
    # only honest reading of nothing.
    from hub.db import seed_default_project

    await seed_default_project(db)
    ready = await _ready_draft(client, db, "ready and untouched")

    resp = await client.post("/tasks/web-batch-approve-selected", data={})
    assert resp.status_code in (200, 303, 400), resp.text
    assert await _status_of(db, ready) == "draft", (
        "an empty selection means nothing, never everything"
    )


async def test_single_approve_survives_the_group_path(client: AsyncClient, db):
    # AC-3 (#628). Two approaches, and the single one is not swallowed by the
    # group: one task, one decision, same gates.
    from hub.db import seed_default_project

    await seed_default_project(db)
    one = await _ready_draft(client, db, "approved on its own")
    other = await _ready_draft(client, db, "left alone")

    resp = await client.post(f"/tasks/{one}/web-approve")
    assert resp.status_code in (200, 303), resp.text
    assert await _status_of(db, one) == "open"
    assert await _status_of(db, other) == "draft"


async def test_skipped_tasks_are_named_with_their_reason(client: AsyncClient, db):
    # AC-4 (#628). Ticking five and getting three, with no word about the other
    # two, is its own lie — and the reasons already exist in BatchApproveResult;
    # the old handler simply threw the result away.
    from hub.db import seed_default_project

    await seed_default_project(db)
    good = await _ready_draft(client, db, "passes the gate")
    unrefined = await _draft_with_readiness(client, "never refined", ready=False)

    resp = await client.post(
        "/tasks/web-batch-approve-selected",
        data={"task_ids": [str(good), str(unrefined)]},
        headers={"HX-Request": "true"},
    )
    import re

    assert resp.status_code == 200, resp.text
    assert await _status_of(db, good) == "open"
    assert await _status_of(db, unrefined) == "draft"

    # Anchored INSIDE the result block, not anywhere on the page. The loose
    # version passed with the result thrown away, because a skipped draft stays
    # in the inbox and its id is on the page for an unrelated reason — the same
    # mistake as reading the whole dashboard to test the board.
    block = re.search(r'class="inbox-batch-skipped".*?</div>', resp.text, re.S)
    assert block, "skipped tasks must be reported, not silently dropped"
    assert str(unrefined) in block.group(0), (
        "a task the reader explicitly ticked and did not get must be named"
    )
    assert re.search(r"#\d+:\s*\w", block.group(0)), (
        "…and named WITH its reason, not merely listed"
    )
    assert str(good) in resp.text
