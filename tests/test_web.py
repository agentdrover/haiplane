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
