from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from hub.mcp_server import (
    hub_add_acceptance_criterion,
    hub_add_risk,
    hub_approve_task,
    hub_delete_acceptance_criterion,
    hub_force_complete_task,
    hub_get_readiness,
    hub_list_acceptance_criteria,
    hub_list_tasks,
    hub_propose_task,
    hub_refine_task,
    hub_replace_acceptance_criteria,
    hub_report_done,
    hub_start_task,
    hub_task_status,
    hub_task_update,
)


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
    lines = out.split("\n")
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
    assert "Task #42: Inspect me" in out
    assert "Status: running" in out
    assert "Agent: tester" in out
    assert "Job ID: job-9" in out
    assert "[2026-01-02T00:00:00Z] (status) a1: Started" in out
    assert "Log tail:" in out and "line1" in out and "line2" in out
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
            "parent_id": 7,
        },
    )


async def test_hub_start_task(mock_api_post: AsyncMock) -> None:
    mock_api_post.return_value = {"status": "running", "job_id": "dispatch-1"}
    msg = await hub_start_task(5, plan="Step one then two", runtime="openrouter")
    assert "Task #5 dispatched" in msg
    assert "dispatch-1" in msg
    mock_api_post.assert_awaited_once_with(
        "/api/tasks/5/start",
        {"plan": "Step one then two", "runtime": "openrouter"},
    )


async def test_hub_approve_task_passes_force(mock_api_post: AsyncMock) -> None:
    mock_api_post.return_value = {"status": "open"}
    msg = await hub_approve_task(
        5,
        comment="human override",
        run=True,
        runtime="vast",
        force=True,
    )
    assert "Task #5 approved" in msg
    mock_api_post.assert_awaited_once_with(
        "/api/tasks/5/approve",
        {
            "comment": "human override",
            "run": True,
            "force": True,
            "runtime": "vast",
        },
    )


async def test_hub_force_complete_task(mock_api_post: AsyncMock) -> None:
    mock_api_post.return_value = {"status": "completed"}
    msg = await hub_force_complete_task(9)
    assert "Task #9 force-completed" in msg
    mock_api_post.assert_awaited_once_with("/api/tasks/9/force-complete", None)


async def test_hub_force_complete_task_with_comment(mock_api_post: AsyncMock) -> None:
    mock_api_post.return_value = {"status": "completed"}
    await hub_force_complete_task(9, comment="reviewed manually")
    mock_api_post.assert_awaited_once_with(
        "/api/tasks/9/force-complete", {"comment": "reviewed manually"}
    )


async def test_hub_update(mock_api_post: AsyncMock) -> None:
    mock_api_post.return_value = {"id": 55}
    msg = await hub_task_update(4, "Plan: ship it", agent="dev", kind="status")
    assert "Update #55 added to task #4" in msg
    mock_api_post.assert_awaited_once_with(
        "/api/tasks/4/updates",
        {"agent": "dev", "kind": "status", "content": "Plan: ship it"},
    )


async def test_hub_report_done(mock_api_post: AsyncMock) -> None:
    mock_api_post.return_value = {"id": 77}
    msg = await hub_report_done(
        9,
        "Changed: tests. Validation: pytest -q",
        agent="qa",
    )
    assert "Done report #77 submitted for task #9" in msg
    call_args = mock_api_post.await_args
    assert call_args is not None
    assert call_args.args[0] == "/api/tasks/9/updates"
    assert call_args.args[1] == {
        "agent": "qa",
        "kind": "done",
        "content": "Changed: tests. Validation: pytest -q",
    }


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
    assert "Task #42 refined" in msg
    assert "work_type" in msg and "scope_in" in msg
    mock_api_post.assert_awaited_once_with(
        "/api/tasks/42/refine",
        {
            "work_type": "bug",
            "problem_statement": "login fails",
            "scope_in": ["auth", "session"],
        },
    )


async def test_hub_refine_task_empty_payload_is_a_no_op(
    mock_api_post: AsyncMock,
) -> None:
    msg = await hub_refine_task(42)
    assert "Nothing to refine" in msg
    mock_api_post.assert_not_called()


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
    mock_api_post: AsyncMock,
) -> None:
    mock_api_post.return_value = {"id": "AC-1"}
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
    mock_api_post.assert_awaited_once_with(
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


async def test_hub_delete_acceptance_criterion_url_encodes_id(
    mock_api_delete: AsyncMock,
) -> None:
    msg = await hub_delete_acceptance_criterion(7, "AC 1/v2")
    assert "Deleted AC 1/v2 from task #7" in msg
    mock_api_delete.assert_awaited_once_with(
        "/api/tasks/7/acceptance_criteria/AC%201%2Fv2"
    )


async def test_hub_add_risk_appends_to_existing(
    mock_api_get: AsyncMock, mock_api_post: AsyncMock
) -> None:
    """`hub_add_risk` is read-modify-write through /refine — existing
    risks must be preserved, the new one appended."""
    mock_api_get.return_value = {
        "id": 7,
        "risks": [
            {
                "kind": "security",
                "severity": "low",
                "description": "x",
                "mitigation": "y",
            }
        ],
    }
    mock_api_post.return_value = {"updated_columns": ["risks"]}
    msg = await hub_add_risk(
        task_id=7,
        kind="performance",
        severity="medium",
        description="slow loop",
        mitigation="add index",
    )
    assert "performance:medium" in msg
    assert "total: 2" in msg
    mock_api_get.assert_awaited_once_with("/api/tasks/7")
    mock_api_post.assert_awaited_once_with(
        "/api/tasks/7/refine",
        {
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
            ]
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


async def test_hub_get_readiness_explain_returns_full_json(
    mock_api_get: AsyncMock,
) -> None:
    payload = {"score": 100, "dor_passed": True, "explain": [{"k": "v"}]}
    mock_api_get.return_value = payload
    msg = await hub_get_readiness(12, explain=True)
    import json as _json

    assert _json.loads(msg) == payload
    mock_api_get.assert_awaited_once_with("/api/tasks/12/readiness?explain=true")
