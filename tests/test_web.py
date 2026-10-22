from __future__ import annotations

import html
import re
from httpx import AsyncClient
from hub import repository as repo
from hub.auth import CSRF_COOKIE_NAME

from tests.web_support import (
    _form_carries_policy_version,  # noqa: F401
    _draft_with_readiness,
    _web_task_in_review,
)


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
    # #630 moved the bare /tasks to the queue; this test is about TABLE markup,
    # so it asks for a mode that renders one. The subject is unchanged.
    resp = await client.get("/tasks", params={"state": "live"})
    assert resp.status_code == 200
    assert "task-table-actions" in resp.text
    assert "task-table-action" in resp.text


async def test_tasks_table_badges_have_consistent_markup(client: AsyncClient):
    await client.post("/api/tasks", json={"title": "Badge row"})
    # #630 moved the bare /tasks to the queue; this test is about TABLE markup,
    # so it asks for a mode that renders one. The subject is unchanged.
    resp = await client.get("/tasks", params={"state": "live"})
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
    assert "Передать в облачный чат" in resp.text
    assert f'action="/tasks/{task_id}/web-implementer-start"' in resp.text


async def test_second_task_card_get_reuses_csrf_cookie(client: AsyncClient):
    """#990 AC-4: a second task-card GET must keep the first CSRF cookie."""
    first = await client.post("/api/tasks", json={"title": "CSRF card A"})
    other = await client.post("/api/tasks", json={"title": "CSRF card B"})
    page_a = await client.get(f"/tasks/{first.json()['id']}")
    token = page_a.cookies.get(CSRF_COOKIE_NAME)
    assert token
    page_b = await client.get(f"/tasks/{other.json()['id']}")
    assert page_b.cookies.get(CSRF_COOKIE_NAME) == token


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
            "affected_areas": ["hub/services/dor.py"],
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
    # #630: this task is `open`, so it lives in a table mode now, not in the
    # queue that a bare /tasks opens. The claim under test — the badge shows up
    # in the list — is unchanged.
    table = await client.get("/tasks", params={"state": "live"})
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
            "affected_areas": ["hub/services/dor.py"],
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
            "affected_areas": ["hub/services/dor.py"],
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

    running_page = await client.get(f"/tasks/{task_id}")
    assert running_page.status_code == 200
    assert "Передать в облачный чат" not in running_page.text


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
    assert 'name="record_decision"' not in page.text
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


async def test_inbox_shows_risk_class_and_unset_state(client: AsyncClient, db):
    # AC-2 (#581): the class sits next to each inbox row; a task without a
    # computed class renders the explicit "not computed" badge, never R0.
    resp = await client.post(
        "/api/tasks", json={"title": "Classified draft", "source": "agent"}
    )
    classified_id = resp.json()["id"]
    await client.post(
        "/api/tasks", json={"title": "Unclassified draft", "source": "agent"}
    )
    await db.execute(
        "UPDATE tasks SET risk_class = 'R4' WHERE id = ?", (classified_id,)
    )
    await db.commit()

    resp = await client.get("/partials/inbox")
    assert resp.status_code == 200
    assert "badge-risk-r4" in resp.text
    assert ">R4<" in resp.text
    assert "badge-risk-unset" in resp.text
    assert "badge-risk-r0" not in resp.text


async def test_task_detail_shows_unset_risk_class_distinct_from_r0(
    client: AsyncClient,
):
    resp = await client.post("/api/tasks", json={"title": "No class card"})
    task_id = resp.json()["id"]
    resp = await client.get(f"/tasks/{task_id}")
    assert resp.status_code == 200
    assert "badge-risk-unset" in resp.text
    assert "badge-risk-r0" not in resp.text


# ---- #500: the delivery snapshot on the dashboard ----


async def test_dashboard_shows_what_is_deployed(
    client: AsyncClient, db, tmp_path, monkeypatch
):
    # AC-1 (#500): the deployed commit and the tasks either side of it, in one
    # place. Until now the answer existed per-card and had to be walked.
    from hub import repository as repo_module
    from tests.test_delivery_state import _task_merged_at, _use_real_git
    from tests.conftest import _git_in

    root = tmp_path / "repo"
    root.mkdir()
    _git_in(root, "init", "-b", "main")
    (root / "a.py").write_text("a = 1\n")
    _git_in(root, "add", ".")
    _git_in(root, "commit", "-m", "shipped")
    shipped = _git_in(root, "rev-parse", "HEAD")
    (root / "b.py").write_text("b = 2\n")
    _git_in(root, "add", ".")
    _git_in(root, "commit", "-m", "released")
    released = _git_in(root, "rev-parse", "HEAD")

    _use_real_git(monkeypatch, str(root))
    from hub.integrations.git_ops import GitOpsIntegration
    from hub.integrations.registry import plugins

    monkeypatch.setattr(
        plugins.git_ops,
        "commit_exists",
        GitOpsIntegration().commit_exists,
        raising=False,
    )
    task_id = await _task_merged_at(client, db, shipped)
    await repo_module.update_task(db, task_id, status="completed")
    await repo_module.record_release(db, deployed_sha=released, ref="main", source="ci")
    await db.commit()

    page = (await client.get("/")).text

    assert "Что в проде" in page
    assert released[:12] in page
    assert f"/tasks/{task_id}" in page


async def test_dashboard_keeps_unknown_apart(client: AsyncClient, db):
    # AC-2 (#500): a task the hub never merged is "could not tell", and the
    # section must not file it under "did not ship" — the same line the whole
    # epic holds (#839, #497, #883, #498).
    from hub import repository as repo_module

    task_id = (await client.post("/api/tasks", json={"title": "Never merged"})).json()[
        "id"
    ]
    await repo_module.update_task(db, task_id, status="completed")
    await db.commit()

    page = (await client.get("/")).text

    assert "Состояние неизвестно" in page
    unknown_at = page.index("Состояние неизвестно")
    not_in_prod_at = page.index("Смёржено, но не раскатано")
    assert unknown_at != not_in_prod_at, "two lists, not one"


async def test_dashboard_states_the_window_and_the_absence(client: AsyncClient):
    # AC-3 (#500): an empty board must explain itself, and the window the
    # snapshot covers must be printed — a bounded list shown as the whole
    # board is what #824 refused to ship.
    page = (await client.get("/")).text

    assert "выкатов не записано" in page.lower() or "неизвестно" in page
    assert "окно" in page


async def test_defect_passport_block_rendered(client: AsyncClient):
    """A defect shows its passport, and an unrecorded stage says so out loud.

    Hiding 'unknown' would make an unfilled passport look like an absent one,
    which is the difference between "nobody wrote it down" and "not a defect".
    """
    create = await client.post(
        "/api/tasks", json={"title": "Дефект без паспорта", "work_type": "bug"}
    )
    task_id = create.json()["id"]

    resp = await client.get(f"/tasks/{task_id}")

    assert resp.status_code == 200
    assert "Паспорт дефекта" in resp.text
    assert 'data-found-in="unknown"' in resp.text
    assert "не записано" in resp.text


async def test_defect_passport_shows_cause_link(client: AsyncClient):
    cause = await client.post("/api/tasks", json={"title": "Изменение"})
    cause_id = cause.json()["id"]
    create = await client.post(
        "/api/tasks", json={"title": "Дефект с прода", "work_type": "bug"}
    )
    task_id = create.json()["id"]
    await client.post(
        f"/api/tasks/{task_id}/refine",
        json={"found_in": "prod", "caused_by_task_id": cause_id},
    )

    resp = await client.get(f"/tasks/{task_id}")

    assert 'data-found-in="prod"' in resp.text
    assert f'href="/tasks/{cause_id}"' in resp.text


async def test_no_passport_block_for_ordinary_feature(client: AsyncClient):
    """A feature with an empty passport must not carry an empty section.

    Every task in the backlog would otherwise grow a 'Паспорт дефекта' card
    saying nothing, and a card that always says nothing stops being read.
    """
    create = await client.post(
        "/api/tasks", json={"title": "Обычная фича", "work_type": "feature"}
    )
    task_id = create.json()["id"]

    resp = await client.get(f"/tasks/{task_id}")

    assert "Паспорт дефекта" not in resp.text


async def test_login_page_uses_haiplane_brand(client: AsyncClient) -> None:
    response = await client.get("/login")
    assert response.status_code == 200
    assert "Haiplane" in response.text
    assert ("Open" + "Claw") not in response.text
    assert "&#x1f980;" not in response.text


# ---- #955: аноним на корне видит продукт, а не форму логина ----------------


async def test_anonymous_root_shows_landing(client: AsyncClient, monkeypatch) -> None:
    # AC-1: с настроенными токенами аноним получает визитку, а не 303 на /login.
    from hub import config
    from hub.auth import TokenIdentity

    monkeypatch.setattr(
        config, "HUB_TOKENS", {"secret-token": TokenIdentity("alice", "human")}
    )
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)

    response = await client.get("/", headers={"Accept": "text/html"})

    assert response.status_code == 200, response.text
    body = response.text
    assert "Haiplane" in body
    assert "agentdrover/haiplane" in body, "нет ссылки на репозиторий"
    assert "docker compose up" in body, "нет команды быстрого старта"
    assert 'href="/login"' in body, "нет ссылки «Войти»"
    # Визитка публична: ни одной задачи из базы на ней быть не должно.
    assert "Inbox" not in body


async def test_session_root_still_shows_dashboard(
    client: AsyncClient, monkeypatch
) -> None:
    # AC-2: аутентифицированный и open-mode получают прежний дашборд.
    from hub import config
    from hub.auth import TokenIdentity

    monkeypatch.setattr(
        config, "HUB_TOKENS", {"secret-token": TokenIdentity("alice", "human")}
    )
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    authed = await client.get(
        "/",
        headers={"Accept": "text/html", "Authorization": "Bearer secret-token"},
    )
    assert authed.status_code == 200, authed.text
    assert "docker compose up" not in authed.text, "дашборд подменён визиткой"

    monkeypatch.setattr(config, "HUB_TOKENS", {})
    open_mode = await client.get("/", headers={"Accept": "text/html"})
    assert open_mode.status_code == 200, open_mode.text
    assert "docker compose up" not in open_mode.text, "open-mode подменён визиткой"


async def test_landing_renders_on_empty_db(client: AsyncClient, monkeypatch) -> None:
    # AC-3: свежая установка — визитка не трогает данные задач.
    from hub import config
    from hub.auth import TokenIdentity

    monkeypatch.setattr(
        config, "HUB_TOKENS", {"secret-token": TokenIdentity("alice", "human")}
    )
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)

    def _explode(*args, **kwargs):  # pragma: no cover - вызов означает провал
        raise AssertionError("визитка не должна читать задачи")

    monkeypatch.setattr("hub.services.get_dashboard_data", _explode, raising=False)

    response = await client.get("/", headers={"Accept": "text/html"})
    assert response.status_code == 200, response.text
    assert "Haiplane" in response.text


async def test_board_hides_dispatch_note_when_no_dispatcher_is_configured(
    client: AsyncClient, monkeypatch, tmp_path
):
    """#953: an absent dispatcher is not a fault, so the board says nothing.

    A hub that never had a dispatcher — the docker demo, a laptop, anyone
    evaluating the product — showed "Dispatch unavailable" on every open card.
    The note reads as breakage; nothing is broken.
    """
    from hub import brand, config
    from hub.integrations.noop import NoopDispatch
    from hub.integrations.registry import plugins

    plugins.dispatch = NoopDispatch()
    monkeypatch.delenv(brand.ENV_PREFIX + "DISPATCH_BIN", raising=False)
    monkeypatch.setattr(config, "DISPATCH_JOBS_DIR", tmp_path / "never-dispatched")

    await client.post("/api/tasks", json={"title": "Open card on a plain hub"})

    resp = await client.get("/")

    assert resp.status_code == 200
    assert "Open card on a plain hub" in resp.text
    assert "Dispatch unavailable" not in resp.text


async def test_board_shows_dispatch_note_when_a_configured_dispatcher_is_down(
    client: AsyncClient, monkeypatch, tmp_path
):
    """#953: the other half — a dispatcher this host DOES have, not answering.

    That is a fault, and hiding it would trade one lie for another. The signal
    is what the installation left behind: the dispatcher's own state directory.
    """
    from hub import brand, config
    from hub.integrations.noop import NoopDispatch
    from hub.integrations.registry import plugins

    plugins.dispatch = NoopDispatch()
    monkeypatch.delenv(brand.ENV_PREFIX + "DISPATCH_BIN", raising=False)
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    monkeypatch.setattr(config, "DISPATCH_JOBS_DIR", jobs)

    await client.post("/api/tasks", json={"title": "Open card on a dispatch host"})

    resp = await client.get("/")

    assert resp.status_code == 200
    assert "Open card on a dispatch host" in resp.text
    assert "Dispatch unavailable" in resp.text


# ---- #1356: кнопка «вернуть в работу» на странице задачи ----


async def test_return_to_work_button_on_the_task_page(client: AsyncClient, db):
    # AC-4: кнопка есть в review (доска ревью) и в fix_requested (карточка
    # действий), причина обязательна в самой форме, и нажатие зовёт тот же
    # сервис, что REST: задача в open, захват снят, в ленте причина.
    task_id = await _web_task_in_review(client)
    await repo.update_task(db, task_id, claimed_by="agent-a", claim_session_id="sess-a")
    await db.commit()
    action = f'action="/tasks/{task_id}/web-return-to-work"'

    page = (await client.get(f"/tasks/{task_id}")).text
    assert action in page, "в review кнопка на доске ревью"
    form = page[page.index(action) :]
    form = form[: form.index("</form>")]
    assert 'name="reason"' in form and "required" in form, (
        "причина обязательна уже в форме"
    )

    # fix_requested, как его ставит сервис: с job_id. Кнопка несёт галочку
    # «бросить активный job» и честно говорит про 409.
    await repo.update_task(db, task_id, status="fix_requested", job_id="job-fix-1")
    await db.commit()
    page = (await client.get(f"/tasks/{task_id}")).text
    assert action in page, "в fix_requested кнопка в карточке действий"
    form = page[page.index(action) :]
    form = form[: form.index("</form>")]
    assert 'name="abandon_active_job"' in form and "job-fix-1" in form
    assert "409" in form, "help-текст называет отказ без галочки"

    # Из running форма есть с #1675: см. test_return_to_work_form_for_running_task.
    await repo.update_task(db, task_id, status="review", job_id=None)
    await db.commit()
    resp = await client.post(
        f"/tasks/{task_id}/web-return-to-work",
        data={"reason": "Держатель молчит"},
        follow_redirects=False,
    )
    assert resp.status_code in (200, 303), resp.text
    data = (await client.get(f"/api/tasks/{task_id}")).json()
    assert data["status"] == "open"
    assert data["claimed_by"] is None
    assert any(
        "Возвращена в работу" in (u["content"] or "")
        and "Держатель молчит" in (u["content"] or "")
        for u in data["updates"]
    )


async def test_web_return_to_work_rejects_agent_token(
    client: AsyncClient, db, monkeypatch
):
    from hub import config
    from hub.config import TokenIdentity

    monkeypatch.setattr(
        config,
        "HUB_TOKENS",
        {
            "agent-token": TokenIdentity("bot", "agent"),
            "human-token": TokenIdentity("denis", "human"),
        },
    )
    monkeypatch.setattr(config, "HUB_AUTH_DISABLED", False)
    human = {"Authorization": "Bearer human-token"}
    resp = await client.post("/api/tasks", json={"title": "Web return"}, headers=human)
    task_id = resp.json()["id"]
    await repo.update_task(db, task_id, status="review", claimed_by="bot")
    await db.commit()

    resp = await client.post(
        f"/tasks/{task_id}/web-return-to-work",
        data={"reason": "перехват"},
        headers={"Authorization": "Bearer agent-token"},
        follow_redirects=False,
    )
    assert resp.status_code == 403
    assert (await repo.get_task(db, task_id))["status"] == "review"


async def test_inbox_partial_renders_action_and_grounds(client: AsyncClient, db):
    from tests.test_auto_verdict import _post_review, _submitted_task

    draft = await client.post(
        "/api/tasks",
        json={"title": "Inbox approve row", "source": "agent", "agent": "bot"},
    )
    draft_id = draft.json()["id"]
    question = await repo.create_task(
        db,
        title="Inbox answer row",
        description="",
        runtime="auto",
        source="human",
        assigned_agent="dev",
        rationale="",
        status="needs_info",
        auto_review=True,
        task_type="task",
        parent_id=None,
        priority="medium",
    )
    await repo.add_task_update(db, question, "dev", "question", "Which branch?")
    await db.commit()
    verdict = await _submitted_task(client, db, "inbox-web-human", {"verdict": "human"})
    await _post_review(client, verdict)

    page = (await client.get("/partials/inbox")).text

    def block(task_id: int) -> str:
        start = page.index(f'id="inbox-task-{task_id}"')
        return page[start : page.index("</article>", start)]

    approve = block(draft_id)
    assert 'data-decision-action="approve"' in approve
    assert "inbox-decision-grounds" in approve and "readiness" in approve
    assert f"/tasks/{draft_id}/web-approve" in approve

    answer = block(question)
    assert 'data-decision-action="answer"' in answer
    assert "Which branch?" in answer
    assert f"/tasks/{question}/web-answer" in answer

    assert "All clear" not in page
    ruling = block(verdict)
    assert 'data-decision-action="verdict"' in ruling
    assert "0/0/1" in ruling and "CI зелёный" in ruling
    assert f'href="/tasks/{verdict}#review-verdict-form"' in ruling


async def test_dashboard_inbox_total_counts_a_verdict_waiting_for_the_human(
    client: AsyncClient, db
):
    from tests.test_auto_verdict import _post_review, _submitted_task

    def total(page: str) -> int:
        m = re.search(
            r'topbar-stat-value">\s*(\d+)\s*<', page[page.index("topbar-stat--inbox") :]
        )
        assert m, "плашка Inbox не найдена"
        return int(m.group(1))

    before = total((await client.get("/")).text)
    task_id = await _submitted_task(
        client, db, "inbox-total-human", {"verdict": "human"}
    )
    await _post_review(client, task_id)
    page = (await client.get("/")).text
    assert total(page) == before + 1
    assert "is-zero" not in page[page.index("topbar-stat--inbox") :][:120]


async def test_web_create_takes_work_type_and_freeze_rationale(client: AsyncClient):
    # #1594: форма даёт тип и обоснование; не-task больше не становится feature.
    page = await client.get("/")
    assert 'name="freeze_rationale"' in page.text
    assert 'name="work_type"' in page.text
    resp = await client.post(
        "/tasks/create",
        data={
            "title": "Typed epic",
            "task_type": "epic",
            "work_type": "chore",
            "freeze_rationale": "чистка долга",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    tasks = (await client.get("/api/tasks")).json()
    epic = next(t for t in tasks if t["title"] == "Typed epic")
    assert epic["work_type"] == "chore"
    assert epic["freeze_rationale"] == "чистка долга"


async def test_web_archive_of_active_task_shows_the_refusal(client: AsyncClient, db):
    from tests.archive_support import archived, mk

    epic = await mk(db, "Эпик", kind="epic")
    kid = await mk(db, "Живой потомок", "running", parent=epic)
    url = f"/tasks/{epic}/web-archive"

    page = await client.post(
        url, data={"cascade": "true", "reason": "  "}, follow_redirects=False
    )
    assert page.status_code in (200, 409), page.status_code
    assert f"#{kid}" in page.text and "archive_active_work" in html.unescape(page.text)
    banner = page.text.split('id="archive-refusal"')[1].split("</form>")[0]
    assert re.search(r'name="cascade"[^>]*value="true"', banner)
    assert 'name="reason"' in banner, "поле причины осталось на странице"
    assert not await archived(db, epic) and not await archived(db, kid)

    solo = await mk(db, "Одиночная", "running")
    alone = await client.post(
        f"/tasks/{solo}/web-archive", data={"cascade": "false", "reason": "  "}
    )
    assert alone.status_code in (200, 409)
    banner = alone.text.split('id="archive-refusal"')[1].split("</form>")[0]
    assert re.search(r'name="cascade"[^>]*value="false"', banner)

    done = await client.post(
        url,
        data={"cascade": "true", "reason": "владелец закрыл эпик"},
        follow_redirects=False,
    )
    assert done.status_code == 303, done.text
    assert await archived(db, epic) and await archived(db, kid)

    fresh = await mk(db, "Свежая", "open")
    card = (await client.get(f"/tasks/{fresh}")).text
    forms = re.findall(r"<form[^>]*web-archive.*?</form>", card, flags=re.S)
    assert len(forms) == 2, "обе формы архива"
    for form in forms:
        assert 'name="reason"' in form and "confirm(" in form, form


async def test_return_to_work_form_for_running_task(client: AsyncClient, db):
    # AC-6 (#1675): форма видна у running commit-задачи; без признаков возврат
    # открывает задачу; с признаком или job - отказ называет его (не 500), а
    # галочки abandon_active_job у running нет вовсе.
    async def _running() -> int:
        task_id = (
            await client.post("/api/tasks", json={"title": "Web running"})
        ).json()["id"]
        await repo.update_task(db, task_id, status="running")
        await db.commit()
        return task_id

    def _form(page: str, task_id: int) -> str:
        action = f'action="/tasks/{task_id}/web-return-to-work"'
        assert action in page, "форма возврата у running-задачи"
        form = page[page.index(action) :]
        return form[: form.index("</form>")]

    idle = await _running()
    form = _form((await client.get(f"/tasks/{idle}")).text, idle)
    assert 'name="reason"' in form and "required" in form
    assert 'name="abandon_active_job"' not in form

    resp = await client.post(
        f"/tasks/{idle}/web-return-to-work",
        data={"reason": "Исполнитель ушёл"},
        follow_redirects=False,
    )
    assert resp.status_code in (200, 303), resp.text
    assert (await client.get(f"/api/tasks/{idle}")).json()["status"] == "open"

    held = await _running()
    await repo.update_task(db, held, claimed_by="agent-a")
    await db.commit()
    _form((await client.get(f"/tasks/{held}")).text, held)
    resp = await client.post(
        f"/tasks/{held}/web-return-to-work", data={"reason": "Попытка"}
    )
    assert resp.status_code == 409, resp.status_code
    text = html.unescape(resp.text)
    assert "return_running_has_work" in text and "claimed_by" in text, text
    assert (await client.get(f"/api/tasks/{held}")).json()["status"] == "running"

    jobbed = await _running()
    await repo.update_task(db, jobbed, job_id="job-web-1")
    await db.commit()
    form = _form((await client.get(f"/tasks/{jobbed}")).text, jobbed)
    assert 'name="abandon_active_job"' not in form, "для running галочки нет"
    assert "Блокирует возврат" in form and "job-web-1" in form, (
        "job назван блокером в самой форме"
    )
    resp = await client.post(
        f"/tasks/{jobbed}/web-return-to-work",
        data={"reason": "Попытка", "abandon_active_job": "true"},
    )
    assert resp.status_code == 409 and "job_id" in html.unescape(resp.text)
    assert (await client.get(f"/api/tasks/{jobbed}")).json()["status"] == "running"


async def test_gate_card_names_brief_failure_reason(
    client: AsyncClient, monkeypatch, db
):
    # AC-4 (#1622): a brief that failed to assemble is not "no gate". The card
    # of a task in review names the cause and keeps the decision form; a task
    # outside the gate gets no false error.
    from hub.services import review_brief

    task_id = await _web_task_in_review(client)

    async def _boom(*args, **kwargs):
        raise RuntimeError("git diff застрял")

    monkeypatch.setattr(review_brief, "build_review_brief", _boom)
    page = await client.get(f"/tasks/{task_id}")

    assert page.status_code == 200
    assert "бриф не собран: RuntimeError (внутренняя ошибка)" in page.text
    assert "git diff застрял" not in page.text, "exception text stays in the log"
    assert "/web-review-verdict" in page.text, "decision form must stay"

    outside = await client.post("/api/tasks", json={"title": "Not at the gate"})
    quiet = await client.get(f"/tasks/{outside.json()['id']}")
    assert quiet.status_code == 200
    assert "бриф не собран" not in quiet.text
    # The assembler is still broken here: outside the gate it is not even asked,
    # so no failure object is invented for a task nobody decides on.
    outside_row = await repo.get_task(db, outside.json()["id"])
    assert await review_brief.gate_evidence(db, dict(outside_row)) is None
    gate_row = await repo.get_task(db, task_id)
    failure = await review_brief.gate_evidence(db, dict(gate_row))
    assert isinstance(failure, review_brief.BriefFailure)


async def test_style_tokens_both_themes():
    """AC-2 (#1812): каждый цвет из токена и есть в обеих темах (разбор CSS)."""
    from tests.card_c2_css_support import check_style_tokens_both_themes

    check_style_tokens_both_themes()
