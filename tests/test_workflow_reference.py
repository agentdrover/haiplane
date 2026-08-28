"""Tests for workflow discoverability (#175)."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from hub.models import HIERARCHY_RULES, TaskType
from hub.workflow_reference import (
    AGENT_COMPLETION_TOOL,
    LIFECYCLE_MAP_HEADER,
    LIFECYCLE_TRANSITIONS,
    build_mcp_instructions,
    hierarchy_edges,
    lifecycle_map_lines,
    workflow_reference_dict,
)

# The transition table as it stands on develop before #988 (AC-7).
FROZEN_TRANSITIONS = [
    ("draft", "open", "hub_approve_task", "human"),
    ("draft", "rejected", "hub_reject_task", "human"),
    ("open", "claimed", "hub_claim_task", "agent"),
    ("open", "running", "hub_start_task", "human"),
    ("open", "running", "hub_pair_start", "agent"),
    ("claimed", "running", "hub_pair_start", "agent"),
    ("claimed", "open", "hub_release_task", "agent"),
    ("running", "review", "hub_submit_for_review", "agent"),
    ("review", "running", "hub_submit_review", "agent"),
    ("running", "completed", "hub_report_done", "agent"),
    ("running", "review", "hub_report_done", "agent"),
    ("running", "ci_check", "hub_report_done", "agent"),
    ("running", "needs_decision", "hub_report_done", "agent"),
    ("running", "needs_info", "hub_ask_question", "agent"),
    ("needs_info", "open", "hub_answer_question", "human"),
    ("running", "pending_report", "hub_report_done", "agent"),
    ("pending_report", "review", "hub_report_done", "agent"),
    ("ci_check", "review", "ci_poller", "ci"),
    ("needs_decision", "completed", "hub_decide_task", "human"),
    ("needs_decision", "fix_requested", "hub_decide_task", "human"),
    ("pending_report", "completed", "hub_report_done", "agent"),
]


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


def test_instructions_point_at_the_map_instead_of_copying_it() -> None:
    """#988 AC-1: the instruction stopped being a second copy of the map.

    It used to enumerate the hierarchy, the gates and ten ``from→to via tool``
    transitions that ``hub_my_context(mode=full)`` already prints, so every
    session paid for the same text twice. What must survive is the part a
    caller cannot look up without knowing where to look: identity, the
    canonical completion tool, the human-only list, the envelope, and a
    pointer.
    """
    text = build_mcp_instructions()

    # Still there: what only the instruction can say.
    assert "hub_report_done" in text
    assert "Human-only tools:" in text
    assert "hub_approve_task" in text and "hub_decide_task" in text
    assert "hub_my_context(mode=full)" in text
    assert "Workflow reference" in text

    # Gone: the enumeration that hub_my_context already carries.
    assert "HIERARCHY_RULES" not in text
    assert "Key transitions" not in text
    for transition in LIFECYCLE_TRANSITIONS:
        arrow = f"{transition['from']}→{transition['to']} via {transition['tool']}"
        assert arrow not in text, arrow

    assert len(text.encode("utf-8")) < 4096


def test_map_splits_author_and_reviewer() -> None:
    """#988 AC-5: two actors, not one four-step chain for the author.

    The gate line used to read ``hub_submit_for_review → hub_get_review_brief
    → hub_submit_review``, which describes a self-review: it hands the brief
    and the verdict to the agent that just submitted.
    """
    joined = "\n".join(lifecycle_map_lines())

    assert "hub_submit_for_review → hub_get_review_brief" not in joined

    author = next(ln for ln in lifecycle_map_lines() if ln.startswith("Author lane:"))
    reviewer = next(
        ln for ln in lifecycle_map_lines() if ln.startswith("Reviewer lane")
    )
    assert author is not reviewer
    assert "hub_get_review_brief" not in author
    assert "hub_submit_review" not in author
    assert "hub_get_review_brief" in reviewer and "hub_submit_review" in reviewer
    assert "NOT the assigned agent" in reviewer


def test_submit_for_review_docstring_names_the_other_actor() -> None:
    """#988 AC-6: the published description says who writes the verdict."""
    from hub.mcp_server import hub_submit_for_review

    doc = hub_submit_for_review.__doc__ or ""
    assert "does NOT complete the task" in doc
    assert "different actor" in doc
    assert "hub_submit_review" in doc


def test_lifecycle_transitions_unchanged() -> None:
    """#988 AC-7: the prose was rewritten, the table was not.

    Pinned as literals rather than compared to another branch: a test on a
    branch cannot measure ``develop``.
    """
    assert [
        (t["from"], t["to"], t["tool"], t["actor"]) for t in LIFECYCLE_TRANSITIONS
    ] == FROZEN_TRANSITIONS


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


def test_machine_review_gate_in_reference():
    # #383: the machine-review step is discoverable in the workflow schema.
    from hub.workflow_reference import build_mcp_instructions, workflow_reference_dict

    gates = workflow_reference_dict()["gates"]
    assert gates["machine_review"]["tool"] == "hub_submit_machine_review"
    assert "machine-review-cycle" in gates["machine_review"]["rule"]
    # base MCP instructions stay within the documented size budget
    assert len(build_mcp_instructions()) < 4096
