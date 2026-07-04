"""Tests for workflow discoverability (#175)."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from hub.models import HIERARCHY_RULES, TaskType
from hub.workflow_reference import (
    AGENT_COMPLETION_TOOL,
    LIFECYCLE_MAP_HEADER,
    build_mcp_instructions,
    hierarchy_edges,
    lifecycle_map_lines,
    workflow_reference_dict,
)


def test_hierarchy_edges_match_models() -> None:
    edges = {e["child"]: e["parent"] for e in hierarchy_edges()}
    assert edges["epic"] is None
    assert edges["feature"] == "epic"
    assert edges["task"] == "feature"
    assert edges["subtask"] == "task"
    for child, parent in HIERARCHY_RULES.items():
        expected = parent.value if parent else None
        assert edges[child.value] == expected


def test_workflow_reference_includes_gates_and_human_tools() -> None:
    ref = workflow_reference_dict()
    assert ref["agent_completion_tool"] == AGENT_COMPLETION_TOOL
    assert "hub_decide_task" in ref["human_only_tools"]
    assert ref["gates"]["dor"]["tool"] == "hub_approve_task"
    assert ref["gates"]["ci"]["status"] == "ci_check"
    assert ref["gates"]["decision"]["actor"] == "human"
    assert any(t["from"] == "draft" and t["to"] == "open" for t in ref["transitions"])


def test_mcp_instructions_document_hierarchy_and_lifecycle() -> None:
    text = build_mcp_instructions()
    assert "HIERARCHY_RULES" in text
    assert "epic" in text and "feature" in text and "subtask" in text
    assert "hub_approve_task" in text
    assert "hub_decide_task" in text
    assert "ci_check" in text
    assert "hub_report_done" in text
    assert len(text.encode("utf-8")) < 4096


def test_lifecycle_map_lines_header_and_transitions() -> None:
    lines = lifecycle_map_lines()
    assert lines[0] == LIFECYCLE_MAP_HEADER
    joined = "\n".join(lines)
    assert "task→parent:feature" in joined
    assert "needs_decision" in joined
    assert "hub_pair_start" in joined


async def test_context_full_includes_workflow_reference(client: AsyncClient) -> None:
    resp = await client.post("/api/tasks", json={"title": "wf ctx"})
    task_id = resp.json()["id"]

    full = await client.get(f"/api/tasks/{task_id}/context")
    assert full.status_code == 200
    assert LIFECYCLE_MAP_HEADER in full.json()["context_text"]
    assert "hub_report_done" in full.json()["context_text"]

    summary = await client.get(f"/api/tasks/{task_id}/context?mode=summary")
    assert summary.status_code == 200
    assert LIFECYCLE_MAP_HEADER not in summary.json()["context_text"]


async def test_propose_task_under_task_parent_returns_hierarchy_hint(
    client: AsyncClient,
) -> None:
    parent = await client.post(
        "/api/tasks",
        json={"title": "parent task", "task_type": "task"},
    )
    parent_id = parent.json()["id"]

    resp = await client.post(
        "/api/tasks",
        json={
            "title": "child",
            "task_type": "task",
            "parent_id": parent_id,
            "source": "agent",
            "agent": "bot",
        },
    )
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    if isinstance(detail, str):
        pytest.fail(f"expected structured detail, got: {detail}")
    assert detail["reason"] == "invalid_hierarchy"
    assert detail.get("required_parent_type") == TaskType.feature.value
    assert "feature" in detail.get("hint", "").lower()
