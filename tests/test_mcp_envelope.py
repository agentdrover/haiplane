"""Tests for mutation response envelope (#171)."""

from __future__ import annotations

from hub.mcp_envelope import (
    build_mutation_envelope,
    compute_awaiting,
    enrich_error_payload,
)


def test_compute_awaiting_needs_decision() -> None:
    assert compute_awaiting("needs_decision") == "human_decision"


def test_build_mutation_envelope_completed_transition() -> None:
    env = build_mutation_envelope(
        {"status": "completed"},
        transition_from="pending_report",
        transition_to="completed",
    )
    assert env["status"] == "completed"
    assert env["awaiting"] == "none"
    assert env["actor_hint"] == "none"
    assert env["transition"] == {"from": "pending_report", "to": "completed"}


def test_enrich_error_payload_permission_without_status() -> None:
    payload = enrich_error_payload(
        {
            "reason": "permission_denied",
            "message": "missing permission: tasks.archive",
            "hint": "Use human token.",
            "required_role": "human",
            "suggested_tool": "hub_withdraw_own_draft",
        }
    )
    assert payload["status"] == "?"
    assert payload["awaiting"] == "none"
    assert payload["actor_hint"] == "human"
    assert "next_action" in payload
    assert payload["transition"] is None


def test_enrich_error_payload_human_decision() -> None:
    payload = enrich_error_payload(
        {
            "reason": "human_decision_required",
            "hint": "Task awaits hub_decide_task or human Decision Gate.",
            "required_status": "needs_decision",
            "current_status": "needs_decision",
        }
    )
    assert payload["awaiting"] == "human_decision"
    assert payload["actor_hint"] == "human"
    assert "hub_decide_task" in payload["next_action"]
