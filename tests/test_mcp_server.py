from __future__ import annotations

from unittest.mock import ANY, AsyncMock, patch

from typing import Any

import pytest
import json
from mcp.types import CallToolResult, TextContent

from hub.mcp_structured import MCP_STRUCTURED_SCHEMA_VERSION
from hub.mcp_server import (
    hub_add_acceptance_criterion,
    hub_add_risk,
    hub_approve_task,
    hub_ask_question,
    hub_answer_question,
    hub_claim_task,
    hub_create_task,
    hub_create_subtasks,
    hub_decide_task,
    hub_delete_acceptance_criterion,
    hub_force_complete_task,
    hub_get_readiness,
    hub_get_review_brief,
    hub_readiness_tree,
    hub_list_acceptance_criteria,
    hub_list_tasks,
    hub_prepare_developer_task,
    hub_propose_task,
    hub_pair_start,
    hub_refine_task,
    hub_refine_tasks,
    hub_upsert_acceptance_criterion,
    hub_release_task,
    hub_replace_acceptance_criteria,
    hub_report_done,
    hub_start_task,
    hub_submit_for_review,
    hub_submit_review,
    hub_task_status,
    hub_task_update,
)


def _mcp_text(result: CallToolResult | str) -> str:
    if isinstance(result, CallToolResult):
        return "\n".join(
            block.text for block in result.content if isinstance(block, TextContent)
        )
    return result


def _mcp_structured(result: CallToolResult | str) -> dict[str, Any] | None:
    if isinstance(result, CallToolResult):
        return result.structuredContent
    return None


@pytest.fixture
def mock_api_get() -> AsyncMock:
    with patch("hub.mcp_server._api_get", new_callable=AsyncMock) as m:
        yield m


@pytest.fixture
def mock_api_post() -> AsyncMock:
    with patch("hub.mcp_server._api_post", new_callable=AsyncMock) as m:
        yield m


@pytest.fixture
def mock_api_put() -> AsyncMock:
    with patch("hub.mcp_server._api_put", new_callable=AsyncMock) as m:
        yield m


@pytest.fixture
def mock_api_delete() -> AsyncMock:
    with patch("hub.mcp_server._api_delete", new_callable=AsyncMock) as m:
        yield m


async def test_hub_list_tasks(mock_api_get: AsyncMock) -> None:
    mock_api_get.return_value = [
        {
            "id": 1,
            "status": "open",
            "runtime": "auto",
            "title": "Alpha",
            "task_type": "task",
        },
        {
            "id": 2,
            "status": "running",
            "runtime": "vast",
            "title": "Beta epic",
            "task_type": "epic",
            "source": "agent",
            "assigned_agent": "coder",
        },
        {
            "id": 3,
            "status": "open",
            "runtime": "auto",
            "title": "Child",
            "task_type": "subtask",
            "parent_id": 2,
        },
    ]
    out = await hub_list_tasks()
    payload = json.loads(out)
    lines = payload["message"].split("\n")
    assert payload["instance"] in ("prod", "local")
    assert "base_url" in payload
    assert lines[0] == "#1 [open] (auto) Alpha"
    assert lines[1] == "#2 [epic] [running] (vast) [agent:coder] Beta epic"
    assert lines[2] == "#3 [subtask] [open] (auto) (parent #2) Child"
    mock_api_get.assert_awaited_once_with("/api/tasks?limit=20")


async def test_hub_task_detail(
    mock_api_get: AsyncMock, mock_api_post: AsyncMock
) -> None:
    mock_api_post.return_value = {}
    mock_api_get.return_value = {
        "id": 42,
        "title": "Inspect me",
        "status": "running",
        "source": "human",
        "runtime": "auto",
        "assigned_agent": "tester",
        "job_id": "job-9",
        "exit_code": None,
        "auto_review": True,
        "review_cycle": 1,
        "created_at": "2026-01-01T00:00:00Z",
        "updates": [
            {
                "created_at": "2026-01-02T00:00:00Z",
                "kind": "status",
                "agent": "a1",
                "content": "Started",
            },
        ],
        "result_text": "",
        "log_tail": ["line1", "line2"],
    }
    out = await hub_task_status(42)
    text = _mcp_text(out)
    assert "Task #42: Inspect me" in text
    assert "Status: running" in text
    assert "Agent: tester" in text
    assert "Job ID: job-9" in text
    assert "[2026-01-02T00:00:00Z] (status) a1: Started" in text
    assert "Log tail:" in text and "line1" in text and "line2" in text
    structured = _mcp_structured(out)
    assert structured is not None
    assert structured["schema_version"] == MCP_STRUCTURED_SCHEMA_VERSION
    assert structured["task"]["id"] == 42
    mock_api_post.assert_awaited_once_with("/api/tasks/42/refresh")
    mock_api_get.assert_awaited_once_with("/api/tasks/42")


async def test_hub_propose(mock_api_post: AsyncMock) -> None:
    mock_api_post.return_value = {"id": 100}
    msg = await hub_propose_task(
        "New thing",
        "Do the thing",
        agent="architect",
        rationale="Because",
        parent_id=7,
    )
    assert "Draft task #100 created" in msg
    mock_api_post.assert_awaited_once_with(
        "/api/tasks",
        {
            "title": "New thing",
            "description": "Do the thing",
            "source": "agent",
            "agent": "architect",
            "rationale": "Because",
            "human_owner": "",
            "human_reviewer": "",
            "parent_id": 7,
        },
    )


async def test_hub_start_task(
    mock_api_post: AsyncMock, mock_api_get: AsyncMock
) -> None:
    mock_api_post.return_value = {"status": "running", "job_id": "dispatch-1"}
    mock_api_get.side_effect = [
        {"id": 5, "status": "open"},
        {"id": 5, "status": "running", "job_id": "dispatch-1"},
    ]
    msg = await hub_start_task(5, plan="Step one then two", runtime="openrouter")
    payload = json.loads(msg)
    assert "Task #5 dispatched" in payload["message"]
    assert "dispatch-1" in payload["message"]
    assert payload["transition"] == {"from": "open", "to": "running"}
    mock_api_post.assert_awaited_once_with(
        "/api/tasks/5/start",
        {"plan": "Step one then two", "runtime": "openrouter"},
    )


async def test_hub_pair_start(
    mock_api_post: AsyncMock, mock_api_get: AsyncMock
) -> None:
    mock_api_post.return_value = {
        "status": "running",
        "branch": "task-37/pair-start",
        "assigned_agent": "composer-analyst",
        "job_id": None,
    }
    mock_api_get.side_effect = [
        {"id": 37, "status": "open"},
        {
            "id": 37,
            "status": "running",
            "branch": "task-37/pair-start",
            "assigned_agent": "composer-analyst",
        },
    ]
    msg = await hub_pair_start(
        37, plan="Plan: pair work", assigned_agent="composer-analyst"
    )
    payload = json.loads(msg)
    assert "Task #37 pair-started" in payload["message"]
    assert payload["status"] == "running"
    assert payload["transition"] == {"from": "open", "to": "running"}
    mock_api_post.assert_awaited_once_with(
        "/api/tasks/37/pair-start",
        {"plan": "Plan: pair work", "assigned_agent": "composer-analyst"},
    )


async def test_hub_ask_question(mock_api_post: AsyncMock) -> None:
    mock_api_post.return_value = {"status": "needs_info"}
    msg = await hub_ask_question(39, "Which scope first?", agent="composer")
    assert "needs_info" in msg
    mock_api_post.assert_awaited_once_with(
        "/api/tasks/39/question",
        {"agent": "composer", "question": "Which scope first?"},
    )


async def test_hub_answer_question(mock_api_post: AsyncMock) -> None:
    mock_api_post.return_value = {"status": "open", "job_id": None}
    msg = await hub_answer_question(40, "Use REST", resume=True)
    assert "status: open" in msg
    mock_api_post.assert_awaited_once_with(
        "/api/tasks/40/answer",
        {"answer": "Use REST", "resume": True},
    )


async def test_hub_claim_task(
    mock_api_post: AsyncMock, mock_api_get: AsyncMock
) -> None:
    mock_api_post.return_value = {
        "status": "claimed",
        "claimed_by": "composer",
    }
    mock_api_get.side_effect = [
        {"id": 41, "status": "open"},
        {"id": 41, "status": "claimed", "claimed_by": "composer"},
    ]
    msg = await hub_claim_task(41, "composer", session_id="sess-1")
    payload = json.loads(msg)
    assert "claimed" in payload["message"]
    assert payload["status"] == "claimed"
    mock_api_post.assert_awaited_once_with(
        "/api/tasks/41/claim",
        {"agent": "composer", "session_id": "sess-1"},
    )


async def test_hub_release_task(
    mock_api_post: AsyncMock, mock_api_get: AsyncMock
) -> None:
    mock_api_post.return_value = {"status": "open"}
    mock_api_get.side_effect = [
        {"id": 41, "status": "claimed"},
        {"id": 41, "status": "open"},
    ]
    msg = await hub_release_task(41, "composer", session_id="sess-1")
    payload = json.loads(msg)
    assert "released" in payload["message"]
    assert payload["transition"] == {"from": "claimed", "to": "open"}
    mock_api_post.assert_awaited_once_with(
        "/api/tasks/41/release",
        {"agent": "composer", "session_id": "sess-1"},
    )


async def test_hub_approve_task_passes_force(
    mock_api_post: AsyncMock, mock_api_get: AsyncMock
) -> None:
    mock_api_post.return_value = {"status": "open"}
    mock_api_get.side_effect = [
        {"id": 5, "status": "draft"},
        {"id": 5, "status": "open"},
    ]
    msg = await hub_approve_task(
        5,
        comment="human override",
        run=True,
        runtime="vast",
        force=True,
    )
    payload = json.loads(msg)
    assert "Task #5 approved" in payload["message"]
    assert payload["transition"] == {"from": "draft", "to": "open"}
    mock_api_post.assert_awaited_once_with(
        "/api/tasks/5/approve",
        {
            "comment": "human override",
            "run": True,
            "force": True,
            "runtime": "vast",
        },
    )


async def test_hub_force_complete_task(
    mock_api_post: AsyncMock, mock_api_get: AsyncMock
) -> None:
    mock_api_post.return_value = {"status": "completed"}
    mock_api_get.side_effect = [
        {"id": 9, "status": "pending_report"},
        {"id": 9, "status": "completed"},
    ]
    msg = await hub_force_complete_task(9)
    payload = json.loads(msg)
    assert "Task #9 force-completed" in payload["message"]
    assert payload["transition"] == {"from": "pending_report", "to": "completed"}
    mock_api_post.assert_awaited_once_with("/api/tasks/9/force-complete", None)


async def test_hub_force_complete_task_with_comment(
    mock_api_post: AsyncMock, mock_api_get: AsyncMock
) -> None:
    mock_api_post.return_value = {"status": "completed"}
    mock_api_get.side_effect = [
        {"id": 9, "status": "pending_report"},
        {"id": 9, "status": "completed"},
    ]
    await hub_force_complete_task(9, comment="reviewed manually")
    mock_api_post.assert_awaited_once_with(
        "/api/tasks/9/force-complete", {"comment": "reviewed manually"}
    )


async def test_hub_force_complete_human_only_error(
    mock_api_post: AsyncMock, mock_api_get: AsyncMock
) -> None:
    from hub.mcp_server import HubApiError

    mock_api_get.return_value = {"id": 9, "status": "pending_report"}
    mock_api_post.side_effect = HubApiError(
        {
            "reason": "human_only_gate",
            "hint": "This operation requires a human or admin token, not an agent token.",
            "required_role": "human",
            "required_status": None,
            "message": "this operation requires human or admin role",
        }
    )
    msg = await hub_force_complete_task(9)
    payload = json.loads(msg)
    assert payload["reason"] == "human_only_gate"
    assert payload["required_role"] == "human"
    assert payload["actor_hint"] == "human"
    assert "next_action" in payload
    assert payload["instance"] in ("prod", "local")


async def test_hub_archive_permission_actionable_error(
    mock_api_post: AsyncMock,
    mock_api_get: AsyncMock,
) -> None:
    from hub.mcp_server import HubApiError, hub_archive_task

    mock_api_get.return_value = {"id": 12, "status": "draft"}
    mock_api_post.side_effect = HubApiError(
        {
            "reason": "permission_denied",
            "message": "missing permission: tasks.archive",
            "hint": "Agent tokens cannot archive tasks.",
            "required_role": "human",
            "suggested_tool": "hub_withdraw_own_draft",
            "required_permission": "tasks.archive",
        }
    )
    msg = await hub_archive_task(12)
    payload = json.loads(msg)
    assert payload["reason"] == "permission_denied"
    assert payload["suggested_tool"] == "hub_withdraw_own_draft"
    assert payload["actor_hint"] == "human"
    assert payload["awaiting"] == "none"
    assert "next_action" in payload


async def test_hub_withdraw_own_draft_success(
    mock_api_post: AsyncMock,
    mock_api_get: AsyncMock,
) -> None:
    from hub.mcp_server import hub_withdraw_own_draft

    mock_api_get.side_effect = [
        {"id": 21, "status": "draft", "archived": False},
        {"id": 21, "status": "draft", "archived": True},
    ]
    mock_api_post.return_value = {"id": 21, "status": "draft", "archived": True}
    msg = await hub_withdraw_own_draft(21)
    payload = json.loads(msg)
    assert "withdrawn" in payload["message"]
    assert payload["status"] == "draft"
    assert payload["instance"] in ("prod", "local")
    assert "next_action" in payload
    mock_api_post.assert_awaited_once_with("/api/tasks/21/withdraw")


async def test_hub_withdraw_own_draft_actionable_error(
    mock_api_post: AsyncMock,
    mock_api_get: AsyncMock,
) -> None:
    from hub.mcp_server import HubApiError, hub_withdraw_own_draft

    mock_api_get.return_value = {"id": 22, "status": "draft"}
    mock_api_post.side_effect = HubApiError(
        {
            "reason": "not_task_owner",
            "message": "caller is not the assigned agent for this draft",
            "hint": "You can only withdraw drafts assigned to you.",
            "required_role": "agent",
            "suggested_tool": "hub_withdraw_own_draft",
        }
    )
    msg = await hub_withdraw_own_draft(22)
    payload = json.loads(msg)
    assert payload["reason"] == "not_task_owner"
    assert payload["required_role"] == "agent"
    assert payload["suggested_tool"] == "hub_withdraw_own_draft"
    assert payload["actor_hint"] == "agent"
    assert "next_action" in payload


async def test_hub_update(mock_api_post: AsyncMock, mock_api_get: AsyncMock) -> None:
    mock_api_post.return_value = {"id": 55}
    mock_api_get.return_value = {"id": 4, "status": "running"}
    msg = await hub_task_update(4, "Plan: ship it", agent="dev", kind="status")
    payload = json.loads(msg)
    assert "Update #55 added to task #4" in payload["message"]
    assert payload["status"] == "running"
    assert payload["awaiting"] == "none"
    assert "next_action" in payload
    mock_api_post.assert_awaited_once_with(
        "/api/tasks/4/updates",
        {"agent": "dev", "kind": "status", "content": "Plan: ship it"},
    )


async def test_hub_task_update_kind_done_matches_report_done(
    mock_api_post: AsyncMock, mock_api_get: AsyncMock
) -> None:
    mock_api_post.return_value = {"id": 88}
    mock_api_get.side_effect = [
        {"id": 3, "status": "pending_report"},
        {"id": 3, "status": "completed"},
    ]
    update_msg = await hub_task_update(
        3, "Changed: feature. Validation: pytest -q", agent="dev", kind="done"
    )
    mock_api_post.reset_mock()
    mock_api_get.side_effect = [
        {"id": 3, "status": "pending_report"},
        {"id": 3, "status": "completed"},
    ]
    report_msg = await hub_report_done(
        3, "Changed: feature. Validation: pytest -q", agent="dev"
    )
    update_payload = json.loads(update_msg)
    report_payload = json.loads(report_msg)
    assert "Done report #88" in update_payload["message"]
    assert update_payload["status"] == report_payload["status"] == "completed"
    assert update_payload["transition"] == report_payload["transition"]
    assert update_payload["awaiting"] == report_payload["awaiting"]


async def test_hub_report_done(
    mock_api_post: AsyncMock, mock_api_get: AsyncMock
) -> None:
    mock_api_post.return_value = {"id": 77}
    mock_api_get.side_effect = [
        {"id": 9, "status": "running"},
        {"id": 9, "status": "ci_check"},
    ]
    msg = await hub_report_done(
        9,
        "Changed: tests. Validation: pytest -q",
        agent="qa",
    )
    payload = json.loads(msg)
    assert "Done report #77 submitted for task #9" in payload["message"]
    assert "ci_check" in payload["message"]
    assert payload["status"] == "ci_check"
    assert payload["awaiting"] == "ci"
    assert payload["actor_hint"] == "ci"
    assert payload["transition"] == {"from": "running", "to": "ci_check"}
    assert mock_api_get.await_count == 2


async def test_hub_report_done_open_status_returns_structured_error(
    mock_api_post: AsyncMock, mock_api_get: AsyncMock
) -> None:
    from hub.mcp_server import HubApiError

    mock_api_get.return_value = {"id": 5, "status": "open"}
    mock_api_post.side_effect = HubApiError(
        {
            "reason": "pair_start_required",
            "hint": "Call hub_pair_start before hub_report_done.",
            "required_status": "running",
            "current_status": "open",
            "suggested_tool": "hub_pair_start",
            "message": "Call hub_pair_start before hub_report_done.",
        }
    )
    msg = await hub_report_done(5, "Changed: docs only")
    payload = json.loads(msg)
    assert payload["reason"] == "pair_start_required"
    assert payload["status"] == "open"
    assert payload["awaiting"] == "none"
    assert payload["actor_hint"] == "agent"
    assert payload["suggested_tool"] == "hub_pair_start"
    assert payload["instance"] in ("prod", "local")
    mock_api_get.assert_awaited_once_with("/api/tasks/5")


async def test_hub_report_done_completed_from_pending(
    mock_api_post: AsyncMock, mock_api_get: AsyncMock
) -> None:
    mock_api_post.return_value = {"id": 99}
    mock_api_get.side_effect = [
        {"id": 3, "status": "pending_report"},
        {"id": 3, "status": "completed"},
    ]
    msg = await hub_report_done(3, "Changed: feature. Validation: pytest -q")
    payload = json.loads(msg)
    assert payload["status"] == "completed"
    assert payload["awaiting"] == "none"
    assert payload["transition"] == {"from": "pending_report", "to": "completed"}
    assert "Task completed" in payload["message"]


async def test_hub_report_done_needs_decision_error_envelope(
    mock_api_post: AsyncMock, mock_api_get: AsyncMock
) -> None:
    from hub.mcp_server import HubApiError

    mock_api_get.return_value = {"id": 8, "status": "needs_decision"}
    mock_api_post.side_effect = HubApiError(
        {
            "reason": "human_decision_required",
            "hint": "Task awaits hub_decide_task or human Decision Gate.",
            "required_status": "needs_decision",
            "current_status": "needs_decision",
            "message": "Task awaits hub_decide_task or human Decision Gate.",
        }
    )
    msg = await hub_report_done(8, "Done")
    payload = json.loads(msg)
    assert payload["reason"] == "human_decision_required"
    assert payload["status"] == "needs_decision"
    assert payload["awaiting"] == "human_decision"
    assert payload["actor_hint"] == "human"
    assert "hub_decide_task" in payload["next_action"]


# ---------------------------------------------------------------------------
# Structured task form (#43)
# ---------------------------------------------------------------------------


async def test_hub_refine_task_only_includes_provided_fields(
    mock_api_post: AsyncMock,
) -> None:
    mock_api_post.return_value = {"updated_columns": ["work_type", "scope_in"]}
    msg = await hub_refine_task(
        42,
        work_type="bug",
        scope_in=["auth", "session"],
        problem_statement="login fails",
    )
    assert "Task #42 refined" in _mcp_text(msg)
    assert "work_type" in _mcp_text(msg) and "scope_in" in _mcp_text(msg)
    mock_api_post.assert_awaited_once_with(
        "/api/tasks/42/refine",
        {
            "work_type": "bug",
            "problem_statement": "login fails",
            "scope_in": ["auth", "session"],
        },
    )


async def test_hub_refine_task_passes_review_checklist(
    mock_api_post: AsyncMock,
) -> None:
    """review_checklist is forwarded as a list[str] body field; omission keeps key out."""
    mock_api_post.return_value = {"updated_columns": ["review_checklist"]}
    msg = await hub_refine_task(
        42,
        review_checklist=["check migration", "verify rollback"],
    )
    assert "Task #42 refined" in _mcp_text(msg)
    mock_api_post.assert_awaited_once_with(
        "/api/tasks/42/refine",
        {"review_checklist": ["check migration", "verify rollback"]},
    )


async def test_hub_refine_task_empty_payload_is_a_no_op(
    mock_api_post: AsyncMock,
) -> None:
    msg = await hub_refine_task(42)
    assert "Nothing to refine" in _mcp_text(msg)
    structured = _mcp_structured(msg)
    assert structured is not None
    assert structured["no_op"] is True
    assert structured["task_id"] == 42
    mock_api_post.assert_not_called()


async def test_hub_create_task_structured_content_matches_rest(
    mock_api_post: AsyncMock,
) -> None:
    rest_task = {"id": 99, "status": "open", "title": "New", "task_type": "task"}
    mock_api_post.return_value = rest_task
    out = await hub_create_task("New", task_type="task")
    assert isinstance(out, CallToolResult)
    structured = _mcp_structured(out)
    assert structured is not None
    assert structured["schema_version"] == MCP_STRUCTURED_SCHEMA_VERSION
    assert structured["task"] == rest_task
    assert "Task #99 created" in _mcp_text(out)


async def test_hub_refine_task_structured_content_matches_rest(
    mock_api_post: AsyncMock,
) -> None:
    # REST /refine returns the full TaskView, not an audit dict.
    rest_task = {
        "id": 42,
        "work_type": "bug",
        "scope_in": ["auth"],
        "acceptance_criteria": [],
        "risks": [],
        "readiness_score": 70,
        "dor_passed": False,
    }
    mock_api_post.return_value = rest_task
    out = await hub_refine_task(42, work_type="bug", scope_in=["auth"])
    structured = _mcp_structured(out)
    assert structured is not None
    assert structured["schema_version"] == MCP_STRUCTURED_SCHEMA_VERSION
    assert structured["task_id"] == 42
    assert structured["fields_set"] == ["scope_in", "work_type"]
    assert structured["readiness_score"] == 70
    assert structured["dor_passed"] is False
    assert structured["task"] == rest_task


async def test_hub_refine_task_reports_ac_changes_without_false_no_op(
    mock_api_post: AsyncMock,
) -> None:
    """Regression: passing only acceptance_criteria must not report 'no changes'."""
    rest_task = {
        "id": 42,
        "acceptance_criteria": [
            {"id": "AC-1", "given": "g", "when": "w", "then": "t"},
            {"id": "AC-2", "given": "g", "when": "w", "then": "t"},
        ],
        "risks": [],
        "readiness_score": 90,
        "dor_passed": True,
    }
    mock_api_post.return_value = rest_task
    out = await hub_refine_task(
        42,
        acceptance_criteria=[
            {"id": "AC-1", "given": "g", "when": "w", "then": "t"},
            {"id": "AC-2", "given": "g", "when": "w", "then": "t"},
        ],
    )
    text = _mcp_text(out)
    assert "Task #42 refined" in text
    assert "no column changes" not in text
    assert "2 acceptance criteria" in text
    assert "readiness 90" in text
    structured = _mcp_structured(out)
    assert structured["acceptance_criteria_count"] == 2
    assert structured["fields_set"] == ["acceptance_criteria"]


async def test_hub_task_status_structured_content_matches_rest(
    mock_api_get: AsyncMock, mock_api_post: AsyncMock
) -> None:
    rest_task = {
        "id": 42,
        "title": "Inspect me",
        "status": "running",
        "source": "human",
        "runtime": "auto",
        "assigned_agent": "tester",
        "job_id": "job-9",
        "exit_code": None,
        "auto_review": True,
        "review_cycle": 0,
        "created_at": "2026-01-01T00:00:00Z",
        "updates": [],
        "result_text": "",
        "log_tail": [],
    }
    mock_api_get.return_value = rest_task
    out = await hub_task_status(42)
    structured = _mcp_structured(out)
    assert structured is not None
    assert structured["schema_version"] == MCP_STRUCTURED_SCHEMA_VERSION
    assert structured["task"] == rest_task


async def test_hub_list_acceptance_criteria_empty(mock_api_get: AsyncMock) -> None:
    mock_api_get.return_value = []
    msg = await hub_list_acceptance_criteria(7)
    assert "no acceptance criteria" in msg
    mock_api_get.assert_awaited_once_with("/api/tasks/7/acceptance_criteria")


async def test_hub_list_acceptance_criteria_renders_items(
    mock_api_get: AsyncMock,
) -> None:
    mock_api_get.return_value = [
        {
            "id": "AC-1",
            "given": "g1",
            "when": "w1",
            "then": "t1",
            "verifiable_by": "test",
            "test_ref": "tests/x.py::y",
        },
        {
            "id": "AC-2",
            "given": "g2",
            "when": "w2",
            "then": "t2",
            "verifiable_by": "manual",
        },
    ]
    msg = await hub_list_acceptance_criteria(7)
    assert "AC-1" in msg and "AC-2" in msg
    assert "Given: g1" in msg
    assert "tests/x.py::y" in msg


async def test_hub_add_acceptance_criterion_sends_full_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock = AsyncMock(return_value=({"id": "AC-1"}, 201))
    monkeypatch.setattr("hub.mcp_server._api_post_with_status", mock)
    msg = await hub_add_acceptance_criterion(
        task_id=7,
        ac_id="AC-1",
        given="g",
        when="w",
        then="t",
        verifiable_by="manual",
        test_ref="docs/x.md",
    )
    assert "Added AC-1 to task #7" in msg
    mock.assert_awaited_once_with(
        "/api/tasks/7/acceptance_criteria",
        {
            "id": "AC-1",
            "given": "g",
            "when": "w",
            "then": "t",
            "verifiable_by": "manual",
            "test_ref": "docs/x.md",
        },
    )


async def test_hub_add_acceptance_criterion_duplicate_is_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock = AsyncMock(return_value=({"id": "AC-1"}, 200))
    monkeypatch.setattr("hub.mcp_server._api_post_with_status", mock)
    msg = await hub_add_acceptance_criterion(
        task_id=7, ac_id="AC-1", given="g", when="w", then="t"
    )
    assert "already exists" in msg
    assert "no change" in msg


async def test_hub_replace_acceptance_criteria_sends_array(
    mock_api_put: AsyncMock,
) -> None:
    items = [
        {"id": "AC-1", "given": "g", "when": "w", "then": "t", "verifiable_by": "test"},
        {
            "id": "AC-2",
            "given": "g2",
            "when": "w2",
            "then": "t2",
            "verifiable_by": "manual",
        },
    ]
    mock_api_put.return_value = items
    msg = await hub_replace_acceptance_criteria(7, items)
    assert "Task #7 now has 2 acceptance criteria" in msg
    mock_api_put.assert_awaited_once_with("/api/tasks/7/acceptance_criteria", items)


async def test_hub_upsert_acceptance_criterion_puts_by_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ac = {
        "id": "AC-1",
        "given": "g",
        "when": "w",
        "then": "t",
        "verifiable_by": "test",
    }
    mock = AsyncMock(return_value=(ac, 201))
    monkeypatch.setattr("hub.mcp_server._api_put_with_status", mock)
    msg = await hub_upsert_acceptance_criterion(7, "AC-1", "g", "w", "t")
    assert "Created AC-1 on task #7" in msg
    mock.assert_awaited_once_with(
        "/api/tasks/7/acceptance_criteria/AC-1",
        {"id": "AC-1", "given": "g", "when": "w", "then": "t", "verifiable_by": "test"},
    )


async def test_hub_upsert_acceptance_criterion_update_says_updated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ac = {"id": "AC-1", "given": "g", "when": "w", "then": "t", "verifiable_by": "test"}
    mock = AsyncMock(return_value=(ac, 200))
    monkeypatch.setattr("hub.mcp_server._api_put_with_status", mock)
    msg = await hub_upsert_acceptance_criterion(7, "AC-1", "g", "w", "t")
    assert "Updated AC-1 on task #7" in msg


async def test_hub_delete_acceptance_criterion_url_encodes_id(
    mock_api_delete: AsyncMock,
) -> None:
    msg = await hub_delete_acceptance_criterion(7, "AC 1/v2")
    assert "Deleted AC 1/v2 from task #7" in msg
    mock_api_delete.assert_awaited_once_with(
        "/api/tasks/7/acceptance_criteria/AC%201%2Fv2"
    )


async def test_hub_add_risk_uses_dedicated_endpoint(
    mock_api_post: AsyncMock,
) -> None:
    mock_api_post.return_value = {
        "id": 7,
        "risks": [
            {
                "kind": "security",
                "severity": "low",
                "description": "x",
                "mitigation": "y",
            },
            {
                "kind": "performance",
                "severity": "medium",
                "description": "slow loop",
                "mitigation": "add index",
            },
        ],
    }
    msg = await hub_add_risk(
        task_id=7,
        kind="performance",
        severity="medium",
        description="slow loop",
        mitigation="add index",
    )
    assert "performance:medium" in msg
    assert "total: 2" in msg
    mock_api_post.assert_awaited_once_with(
        "/api/tasks/7/risks",
        {
            "kind": "performance",
            "severity": "medium",
            "description": "slow loop",
            "mitigation": "add index",
        },
    )


async def test_hub_get_readiness_compact_summary(mock_api_get: AsyncMock) -> None:
    mock_api_get.return_value = {
        "score": 65,
        "dor_passed": False,
        "missing_required": ["has_problem_statement"],
        "risks": [{"kind": "security", "severity": "high"}],
        "recommendations": [
            {
                "field": "problem_statement",
                "severity": "blocking",
                "message": "Add a problem",
            }
        ],
    }
    msg = await hub_get_readiness(12)
    assert "score=65" in msg
    assert "dor_passed=no" in msg
    assert "has_problem_statement" in msg
    assert "Add a problem" in msg
    mock_api_get.assert_awaited_once_with("/api/tasks/12/readiness")


async def test_hub_readiness_tree_summarizes_not_ready(
    mock_api_get: AsyncMock,
) -> None:
    mock_api_get.return_value = {
        "root_id": 46,
        "total": 3,
        "ready": 1,
        "not_ready": 2,
        "nodes": [
            {
                "id": 47,
                "title": "Ready one",
                "status": "draft",
                "score": 100,
                "dor_passed": True,
                "missing_required": [],
                "blocking_reasons": [],
            },
            {
                "id": 48,
                "title": "Needs problem",
                "status": "draft",
                "score": 40,
                "dor_passed": False,
                "missing_required": ["has_problem_statement"],
                "blocking_reasons": ["Add a problem statement"],
            },
            {
                "id": 49,
                "title": "Needs AC",
                "status": "draft",
                "score": 55,
                "dor_passed": False,
                "missing_required": ["has_acceptance_criteria"],
                "blocking_reasons": [],
            },
        ],
    }
    result = await hub_readiness_tree(46)
    msg = _mcp_text(result)
    assert "1/3 ready" in msg
    assert "2 not ready" in msg
    assert "#48" in msg and "has_problem_statement" in msg
    assert "#49" in msg
    # The ready task is not listed under "Not ready".
    assert "#47" not in msg
    structured = _mcp_structured(result)
    assert structured["report"]["not_ready"] == 2
    assert structured["schema_version"] == MCP_STRUCTURED_SCHEMA_VERSION
    mock_api_get.assert_awaited_once_with("/api/tasks/46/readiness-tree")


async def test_hub_readiness_tree_include_root_query(
    mock_api_get: AsyncMock,
) -> None:
    mock_api_get.return_value = {
        "root_id": 46,
        "total": 1,
        "ready": 1,
        "not_ready": 0,
        "nodes": [
            {
                "id": 46,
                "title": "Root",
                "status": "draft",
                "score": 100,
                "dor_passed": True,
                "missing_required": [],
                "blocking_reasons": [],
            }
        ],
    }
    result = await hub_readiness_tree(46, include_root=True)
    assert "All tasks in the subtree pass DoR." in _mcp_text(result)
    mock_api_get.assert_awaited_once_with(
        "/api/tasks/46/readiness-tree?include_root=true"
    )


async def test_hub_create_subtasks_posts_bulk_payload(
    mock_api_post: AsyncMock,
) -> None:
    mock_api_post.return_value = [
        {"id": 10, "status": "draft", "title": "Sub A"},
        {"id": 11, "status": "draft", "title": "Sub B"},
    ]
    items = [{"title": "Sub A"}, {"title": "Sub B", "priority": "high"}]
    msg = await hub_create_subtasks(42, items, task_type="subtask", agent="bot")
    assert "Created 2 subtask(s) under #42" in msg
    mock_api_post.assert_awaited_once_with(
        "/api/tasks/42/subtasks",
        {
            "items": items,
            "task_type": "subtask",
            "source": "agent",
            "agent": "bot",
        },
    )


async def test_hub_create_subtasks_forwards_acceptance_criteria(
    mock_api_post: AsyncMock,
) -> None:
    mock_api_post.return_value = [{"id": 10, "status": "draft", "title": "Sub"}]
    items = [
        {
            "title": "Sub",
            "acceptance_criteria": [
                {
                    "id": "AC-1",
                    "given": "g",
                    "when": "w",
                    "then": "t",
                    "verifiable_by": "test",
                }
            ],
        }
    ]
    await hub_create_subtasks(42, items, agent="bot")
    forwarded = mock_api_post.await_args.args[1]
    assert forwarded["items"][0]["acceptance_criteria"][0]["id"] == "AC-1"


async def test_hub_refine_tasks_bulk_summarizes_results(
    mock_api_post: AsyncMock,
) -> None:
    mock_api_post.return_value = {
        "results": [
            {
                "task_id": 1,
                "fields_set": ["problem_statement", "acceptance_criteria"],
                "acceptance_criteria_count": 2,
                "risks_count": None,
                "readiness_score": 90,
                "dor_passed": True,
            },
            {
                "task_id": 2,
                "fields_set": ["user_story"],
                "acceptance_criteria_count": None,
                "risks_count": None,
                "readiness_score": 40,
                "dor_passed": False,
            },
        ]
    }
    items = [
        {"task_id": 1, "problem_statement": "ps"},
        {"task_id": 2, "user_story": "us"},
    ]
    out = await hub_refine_tasks(items)
    text = _mcp_text(out)
    assert "Refined 2 task(s)" in text
    assert "#1" in text and "2 AC" in text and "readiness 90" in text and "DoR" in text
    structured = _mcp_structured(out)
    assert len(structured["results"]) == 2
    mock_api_post.assert_awaited_once_with("/api/tasks/refine-bulk", {"items": items})


async def test_hub_refine_tasks_empty_is_no_op(mock_api_post: AsyncMock) -> None:
    out = await hub_refine_tasks([])
    assert "Nothing to refine" in _mcp_text(out)
    assert _mcp_structured(out)["no_op"] is True
    mock_api_post.assert_not_called()


async def test_hub_create_task_passes_owner_and_reviewer(
    mock_api_post: AsyncMock,
) -> None:
    mock_api_post.return_value = {"id": 50, "status": "open"}
    await hub_create_task("Test", human_owner="alice", human_reviewer="bob")
    body = mock_api_post.await_args.args[1]
    assert body["human_owner"] == "alice"
    assert body["human_reviewer"] == "bob"


async def test_hub_list_tasks_passes_owner_and_reviewer_filters(
    mock_api_get: AsyncMock,
) -> None:
    mock_api_get.return_value = []
    await hub_list_tasks(human_owner="alice", human_reviewer="bob")
    mock_api_get.assert_awaited_once_with(
        "/api/tasks?limit=20&human_owner=alice&human_reviewer=bob"
    )


async def test_hub_list_tasks_passes_claimed_by_and_mine_filters(
    mock_api_get: AsyncMock,
) -> None:
    mock_api_get.return_value = []
    await hub_list_tasks(claimed_by="composer", mine="alice")
    mock_api_get.assert_awaited_once_with(
        "/api/tasks?limit=20&claimed_by=composer&mine=alice"
    )


async def test_hub_refine_task_passes_owner_and_reviewer(
    mock_api_post: AsyncMock,
) -> None:
    mock_api_post.return_value = {"updated_columns": ["human_owner", "human_reviewer"]}
    msg = await hub_refine_task(42, human_owner="alice", human_reviewer="bob")
    assert "Task #42 refined" in _mcp_text(msg)
    mock_api_post.assert_awaited_once_with(
        "/api/tasks/42/refine",
        {"human_owner": "alice", "human_reviewer": "bob"},
    )


async def test_hub_refine_task_passes_title(
    mock_api_post: AsyncMock,
) -> None:
    mock_api_post.return_value = {"title": "New title"}
    await hub_refine_task(42, title="New title")
    mock_api_post.assert_awaited_once_with(
        "/api/tasks/42/refine",
        {"title": "New title"},
    )


async def test_hub_refine_task_passes_acceptance_criteria_and_risks(
    mock_api_post: AsyncMock,
) -> None:
    ac = [
        {
            "id": "AC-1",
            "given": "Task open",
            "when": "refine with AC",
            "then": "criteria stored",
            "verifiable_by": "test",
        }
    ]
    risks = [
        {
            "kind": "security",
            "severity": "low",
            "description": "Agents can replace risks",
            "mitigation": "Audit updates",
        }
    ]
    mock_api_post.return_value = {"updated_columns": ["risks"], "ac_count": 1}
    await hub_refine_task(42, acceptance_criteria=ac, risks=risks)
    mock_api_post.assert_awaited_once_with(
        "/api/tasks/42/refine",
        {"acceptance_criteria": ac, "risks": risks},
    )


async def test_hub_propose_task_passes_owner_and_reviewer(
    mock_api_post: AsyncMock,
) -> None:
    mock_api_post.return_value = {"id": 200}
    await hub_propose_task(
        "New thing",
        "Do the thing",
        agent="architect",
        human_owner="alice",
        human_reviewer="bob",
    )
    body = mock_api_post.await_args.args[1]
    assert body["human_owner"] == "alice"
    assert body["human_reviewer"] == "bob"


async def test_hub_decide_task_sends_all_params(
    mock_api_post: AsyncMock, mock_api_get: AsyncMock
) -> None:
    mock_api_post.return_value = {"status": "completed"}
    mock_api_get.side_effect = [
        {"id": 10, "status": "needs_decision"},
        {"id": 10, "status": "completed"},
    ]
    msg = await hub_decide_task(
        task_id=10,
        action="accept",
        instructions="",
        decision_summary="Accepted after manual review.",
        record_decision=True,
    )
    payload = json.loads(msg)
    assert "Task #10" in payload["message"]
    assert "accept" in payload["message"]
    assert "decision recorded" in payload["message"]
    assert payload["transition"] == {"from": "needs_decision", "to": "completed"}
    mock_api_post.assert_awaited_once_with(
        "/api/tasks/10/decide",
        {
            "action": "accept",
            "instructions": "",
            "decision_summary": "Accepted after manual review.",
            "record_decision": True,
        },
    )


async def test_hub_decide_task_rework_without_summary(
    mock_api_post: AsyncMock, mock_api_get: AsyncMock
) -> None:
    mock_api_post.return_value = {"status": "fix_requested"}
    mock_api_get.side_effect = [
        {"id": 11, "status": "needs_decision"},
        {"id": 11, "status": "fix_requested"},
    ]
    msg = await hub_decide_task(task_id=11, action="rework", instructions="Fix X")
    payload = json.loads(msg)
    assert "Task #11" in payload["message"]
    assert "rework" in payload["message"]
    assert "decision recorded" not in payload["message"]
    mock_api_post.assert_awaited_once_with(
        "/api/tasks/11/decide",
        {
            "action": "rework",
            "instructions": "Fix X",
            "decision_summary": "",
            "record_decision": False,
        },
    )


async def test_hub_get_readiness_explain_returns_full_json(
    mock_api_get: AsyncMock,
) -> None:
    payload = {"score": 100, "dor_passed": True, "explain": [{"k": "v"}]}
    mock_api_get.return_value = payload
    msg = await hub_get_readiness(12, explain=True)
    parsed = json.loads(msg)
    assert parsed["score"] == 100
    assert parsed["dor_passed"] is True
    assert parsed["explain"] == [{"k": "v"}]
    assert parsed["instance"] in ("prod", "local")
    assert parsed["base_url"]
    mock_api_get.assert_awaited_once_with("/api/tasks/12/readiness?explain=true")


async def test_hub_prepare_developer_task_batches_analyst_handoff(
    mock_api_post: AsyncMock,
    mock_api_put: AsyncMock,
    mock_api_get: AsyncMock,
) -> None:
    mock_api_post.side_effect = [
        {"updated_columns": ["problem_statement", "scope_in"]},
        {"risks": [{"kind": "security", "severity": "medium"}]},
        {"id": 88},
    ]
    mock_api_put.return_value = [
        {"id": "AC-1", "given": "g", "when": "w", "then": "t", "verifiable_by": "test"}
    ]
    mock_api_get.return_value = {
        "id": 25,
        "risks": [],
    }
    mock_api_get.side_effect = [
        {
            "id": 25,
            "risks": [],
            "problem_statement": "old",
            "scope_in": [],
        },
        [],
        {
            "score": 91,
            "dor_passed": True,
            "missing_required": [],
            "recommendations": [],
            "risks": [{"kind": "security", "severity": "medium"}],
        },
    ]

    msg = await hub_prepare_developer_task(
        task_id=25,
        work_type="feature",
        size="M",
        user_story="As an operator I want one analyst handoff so that dev work is ready.",
        problem_statement="Analyst preparation takes too many manual calls.",
        business_value="Faster and safer developer handoff.",
        scope_in=["Add MCP tool", "Return readiness summary"],
        scope_out=["Change database schema"],
        affected_areas=["hub/mcp_server.py", "tests/test_mcp_server.py"],
        validation_commands=["uv run pytest tests/test_mcp_server.py -q"],
        review_checklist=["Verify AC replacement is atomic"],
        acceptance_criteria=[
            {
                "id": "AC-1",
                "given": "g",
                "when": "w",
                "then": "t",
                "verifiable_by": "test",
            }
        ],
        risks=[
            {
                "kind": "security",
                "severity": "medium",
                "description": "tool can overwrite ACs",
                "mitigation": "document replace semantics",
            }
        ],
        analyst="analyst-agent",
    )

    summary = json.loads(msg)
    assert summary["task_id"] == 25
    assert summary["dor_passed"] is True
    assert summary["readiness_score"] == 91
    assert summary["acceptance_criteria_count"] == 1
    assert summary["risks_added"] == 1
    assert summary["duplicate_risks_count"] == 0
    assert summary["next_action"] == "ready_for_developer"
    assert "developer_handoff_text" in summary
    assert "Add MCP tool" in summary["developer_handoff_text"]
    assert summary["quality_warnings"] == [
        "acceptance_criteria replace existing criteria; review before apply",
        "acceptance criterion AC-1 has no test_ref",
        "risks are deduped by kind/severity/description/mitigation",
    ]
    assert summary["diff"]["will_replace_acceptance_criteria"] is True
    assert summary["diff"]["existing_acceptance_criteria_count"] == 0
    assert summary["diff"]["new_acceptance_criteria_count"] == 1
    assert "problem_statement" in summary["diff"]["structured_fields_to_change"]

    mock_api_post.assert_any_await(
        "/api/tasks/25/refine",
        {
            "work_type": "feature",
            "size": "M",
            "wip_tag": "feature_work",
            "user_story": "As an operator I want one analyst handoff so that dev work is ready.",
            "problem_statement": "Analyst preparation takes too many manual calls.",
            "business_value": "Faster and safer developer handoff.",
            "scope_in": ["Add MCP tool", "Return readiness summary"],
            "scope_out": ["Change database schema"],
            "affected_areas": ["hub/mcp_server.py", "tests/test_mcp_server.py"],
            "validation_commands": ["uv run pytest tests/test_mcp_server.py -q"],
            "review_checklist": ["Verify AC replacement is atomic"],
            "prepared_by": "analyst-agent",
            "prepared_at": ANY,
        },
    )
    mock_api_put.assert_awaited_once_with(
        "/api/tasks/25/acceptance_criteria",
        [
            {
                "id": "AC-1",
                "given": "g",
                "when": "w",
                "then": "t",
                "verifiable_by": "test",
            }
        ],
    )
    mock_api_post.assert_any_await(
        "/api/tasks/25/risks",
        {
            "kind": "security",
            "severity": "medium",
            "description": "tool can overwrite ACs",
            "mitigation": "document replace semantics",
        },
    )
    update_call = mock_api_post.await_args_list[-1]
    assert update_call.args[0] == "/api/tasks/25/updates"
    assert update_call.args[1]["agent"] == "analyst-agent"
    assert update_call.args[1]["kind"] == "status"
    assert "Analyst preparation complete" in update_call.args[1]["content"]
    assert "Developer handoff:" in update_call.args[1]["content"]
    assert "risks_added=1" in update_call.args[1]["content"]
    assert mock_api_get.await_args_list[0].args[0] == "/api/tasks/25"
    assert (
        mock_api_get.await_args_list[1].args[0] == "/api/tasks/25/acceptance_criteria"
    )
    assert mock_api_get.await_args_list[2].args[0] == "/api/tasks/25/readiness"


async def test_hub_prepare_developer_task_dedupes_existing_risks(
    mock_api_post: AsyncMock,
    mock_api_get: AsyncMock,
) -> None:
    duplicate_risk = {
        "kind": "security",
        "severity": "medium",
        "description": "duplicate risk",
        "mitigation": "same mitigation",
    }
    mock_api_post.side_effect = [
        {"updated_columns": ["problem_statement"]},
        {"id": 90},
    ]
    mock_api_get.side_effect = [
        {"id": 25, "risks": [duplicate_risk], "problem_statement": ""},
        {
            "score": 88,
            "dor_passed": True,
            "missing_required": [],
            "recommendations": [],
            "risks": [duplicate_risk],
        },
    ]

    msg = await hub_prepare_developer_task(
        task_id=25,
        problem_statement="Updated problem",
        risks=[duplicate_risk],
    )

    summary = json.loads(msg)
    assert summary["risks_added"] == 0
    assert summary["duplicate_risks_count"] == 1
    assert summary["diff"]["risks_to_add_count"] == 0
    assert summary["quality_warnings"] == [
        "risks are deduped by kind/severity/description/mitigation",
        "duplicate risk skipped: security:medium duplicate risk",
    ]
    assert all(
        call.args[0] != "/api/tasks/25/risks" for call in mock_api_post.await_args_list
    )


async def test_hub_prepare_developer_task_risk_replace_uses_refine(
    mock_api_post: AsyncMock,
    mock_api_get: AsyncMock,
) -> None:
    risk = {
        "kind": "security",
        "severity": "medium",
        "description": "replace risk",
        "mitigation": "replace mitigation",
    }
    mock_api_post.side_effect = [
        {"updated_columns": ["risks"]},
        {"id": 91},
    ]
    mock_api_get.side_effect = [
        {"id": 25, "risks": [], "problem_statement": ""},
        {
            "score": 80,
            "dor_passed": False,
            "missing_required": [],
            "recommendations": [],
        },
    ]

    await hub_prepare_developer_task(task_id=25, risk_mode="replace", risks=[risk])

    mock_api_post.assert_any_await(
        "/api/tasks/25/refine",
        {
            "wip_tag": "feature_work",
            "risks": [risk],
            "prepared_by": "analyst-agent",
            "prepared_at": ANY,
        },
    )
    assert all(
        call.args[0] != "/api/tasks/25/risks" for call in mock_api_post.await_args_list
    )


async def test_hub_prepare_developer_task_preview_does_not_write(
    mock_api_post: AsyncMock,
    mock_api_put: AsyncMock,
    mock_api_get: AsyncMock,
) -> None:
    mock_api_get.side_effect = [
        {
            "id": 25,
            "risks": [],
            "problem_statement": "",
            "scope_in": [],
        },
        [
            {
                "id": "AC-0",
                "given": "old",
                "when": "old",
                "then": "old",
                "verifiable_by": "manual",
            }
        ],
    ]

    msg = await hub_prepare_developer_task(
        task_id=25,
        mode="preview",
        work_type="feature",
        problem_statement="Need a safer analyst workflow.",
        scope_in=["Add preview mode"],
        acceptance_criteria=[
            {
                "id": "AC-1",
                "given": "g",
                "when": "w",
                "then": "t",
                "verifiable_by": "test",
                "test_ref": "tests/test_mcp_server.py::test_preview",
            }
        ],
        risks=[],
    )

    summary = json.loads(msg)
    assert summary["mode"] == "preview"
    assert summary["task_id"] == 25
    assert summary["next_action"] == "preview_only"
    assert "developer_handoff_text" in summary
    assert "Need a safer analyst workflow" in summary["developer_handoff_text"]
    assert summary["diff"]["existing_acceptance_criteria_count"] == 1
    assert summary["diff"]["new_acceptance_criteria_count"] == 1
    assert summary["diff"]["risk_mode"] == "dedupe"
    assert summary["planned_operations"] == [
        "refine_task",
        "replace_acceptance_criteria",
        "write_analyst_update",
    ]
    mock_api_post.assert_not_called()
    mock_api_put.assert_not_called()
    assert mock_api_get.await_args_list[0].args[0] == "/api/tasks/25"
    assert (
        mock_api_get.await_args_list[1].args[0] == "/api/tasks/25/acceptance_criteria"
    )


async def test_hub_prepare_developer_task_preserves_explicit_wip_tag(
    mock_api_post: AsyncMock,
    mock_api_get: AsyncMock,
) -> None:
    mock_api_post.side_effect = [
        {"updated_columns": ["wip_tag"]},
        {"id": 89},
    ]
    mock_api_get.return_value = {
        "score": 70,
        "dor_passed": False,
        "missing_required": ["has_acceptance_criteria"],
        "recommendations": [],
        "risks": [],
    }

    await hub_prepare_developer_task(
        task_id=25,
        work_type="bug",
        wip_tag="bugfix",
        problem_statement="Bug needs detail.",
    )

    mock_api_post.assert_any_await(
        "/api/tasks/25/refine",
        {
            "work_type": "bug",
            "wip_tag": "bugfix",
            "problem_statement": "Bug needs detail.",
            "prepared_by": "analyst-agent",
            "prepared_at": ANY,
        },
    )


async def test_hub_task_status_renders_latest_review(
    mock_api_get: AsyncMock, mock_api_post: AsyncMock
) -> None:
    mock_api_post.return_value = {}
    mock_api_get.return_value = {
        "id": 77,
        "title": "Reviewed",
        "status": "review",
        "created_at": "2026-01-01T00:00:00Z",
        "latest_review": {
            "verdict": "changes_requested",
            "submission_generation": 2,
            "is_current": True,
            "findings": [
                {"id": 1, "severity": "high", "message": "Fix the race"},
                {"id": 2, "severity": "low", "message": "Polish docs"},
            ],
        },
    }
    out = await hub_task_status(77)
    text = _mcp_text(out)
    assert "Latest review: CHANGES_REQUESTED for submission #2 (current)" in text
    assert "1. [high] Fix the race" in text
    assert "2. [low] Polish docs" in text
    structured = _mcp_structured(out)
    assert structured["task"]["latest_review"]["findings"][0]["id"] == 1


async def test_hub_submit_for_review(
    mock_api_get: AsyncMock, mock_api_post: AsyncMock
) -> None:
    mock_api_get.return_value = {"id": 42, "status": "running"}
    mock_api_post.return_value = {
        "id": 42,
        "status": "review",
        "submission_generation": 2,
    }
    out = await hub_submit_for_review(42, agent="dev", summary="pass 2")
    payload = json.loads(out)
    assert "submitted for review (submission #2" in payload["message"]
    assert payload["status"] == "review"
    assert payload["awaiting"] == "review"
    assert payload["actor_hint"] == "agent"
    assert payload["transition"] == {"from": "running", "to": "review"}
    mock_api_post.assert_awaited_once_with(
        "/api/tasks/42/submit-review", {"agent": "dev", "summary": "pass 2"}
    )


async def test_hub_get_review_brief(mock_api_get: AsyncMock) -> None:
    mock_api_get.return_value = {
        "task_id": 42,
        "title": "Reviewed task",
        "status": "review",
        "submission_generation": 1,
        "review_cycle": 0,
        "acceptance_criteria": [
            {
                "id": "AC-1",
                "given": "g",
                "when": "w",
                "then": "t",
                "verifiable_by": "test",
            }
        ],
        "scope_in": ["hub/app.py"],
        "review_checklist": ["check envelopes"],
        "validation_commands": ["uv run pytest -q"],
        "branch": "task-42/x",
        "pr_number": None,
        "diff_command": "git diff develop...task-42/x",
        "latest_submission_summary": "Implemented",
        "latest_review": None,
    }
    out = await hub_get_review_brief(42)
    payload = json.loads(out)
    text = payload["message"]
    assert "Review brief for task #42" in text
    assert "AC-1" in text
    assert "In scope: hub/app.py" in text
    assert "uv run pytest -q" in text
    assert "git diff develop...task-42/x" in text
    assert "hub_submit_review" in text
    assert payload["brief"]["task_id"] == 42
    mock_api_get.assert_awaited_once_with("/api/tasks/42/review-brief")


async def test_hub_submit_review_changes_requested(
    mock_api_get: AsyncMock, mock_api_post: AsyncMock
) -> None:
    mock_api_get.return_value = {"id": 42, "status": "review"}
    mock_api_post.return_value = {"id": 42, "status": "running"}
    findings = [{"id": 1, "severity": "high", "message": "Fix"}]
    out = await hub_submit_review(
        42,
        verdict="changes_requested",
        comments="see findings",
        agent="reviewer",
        findings=findings,
    )
    payload = json.loads(out)
    assert "CHANGES_REQUESTED" in payload["message"]
    assert payload["status"] == "running"
    assert payload["actor_hint"] == "agent"
    assert payload["transition"] == {"from": "review", "to": "running"}
    mock_api_post.assert_awaited_once_with(
        "/api/tasks/42/review-verdict",
        {
            "verdict": "changes_requested",
            "comments": "see findings",
            "agent": "reviewer",
            "findings": findings,
        },
    )
